"""LiteLLM client and prompt contract for patch-entry classification."""

from __future__ import annotations

import json
import logging
import re
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

LABELS = ('buff', 'nerf', 'change', 'rework')
CONFIDENCE_LEVELS = ('VERY_UNCERTAIN', 'UNCERTAIN', 'AMBIGUOUS', 'CERTAIN', 'VERY_CERTAIN')
LOGGER = logging.getLogger(__name__)
_RETRYABLE_STATUS_CODES = frozenset({408, 409, 429, 500, 502, 503, 504})
_JSON_FENCE = re.compile(r'^```(?:json)?\s*(?P<body>.*?)\s*```$', re.DOTALL | re.IGNORECASE)

SYSTEM_PROMPT = """You classify one normalized League of Legends champion patch-history entry.

Return one JSON object with exactly these fields:
- "label": one of "buff", "nerf", "change", or "rework"
- "confidence": one of "VERY_UNCERTAIN", "UNCERTAIN", "AMBIGUOUS", "CERTAIN", or "VERY_CERTAIN"

Definitions:
- buff: increases the champion's power, consistency, availability, or usability.
- nerf: decreases the champion's power, consistency, availability, or usability.
- change: neutral or mixed mechanical work, compensation with no clear direction, presentation/audio/text work,
  or a bug fix without a clear power direction.
- rework: the target entry belongs to an explicit, broad champion gameplay overhaul or relaunch that
  substantially replaces mechanics across the kit.

Use rework conservatively. Do not infer it merely from a large number of balance changes, a champion's initial
release, a visual update, a bug-fix patch, or a small/mid-scope adjustment. When the supplied context is not enough
to establish a rework, classify the target entry by its direct effect. Classify only target_change; the event-level
fields are supporting context.

Confidence describes how certain you are that the selected label is correct, not the size or gameplay impact of the
change:
- VERY_UNCERTAIN: the label is mostly a guess because essential context is missing.
- UNCERTAIN: one label is favored, but there is substantial doubt.
- AMBIGUOUS: two or more labels are similarly plausible from the supplied wording.
- CERTAIN: the direction or nature of the change is clear.
- VERY_CERTAIN: the wording makes the selected label explicit or unmistakable.

Return JSON only, with no markdown.
"""


class ClassificationError(RuntimeError):
    """Raised when a classification request or response is invalid."""


class _RetryableClassificationError(ClassificationError):
    """An endpoint or response failure that can reasonably be retried."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True)
class ClassificationCandidate:
    """One database patch entry plus enough event context for classification."""

    entry_id: int
    event_id: int
    champion_name: str
    patch_id: str
    patch_heading: str
    patch_release_date: str | None
    effective_date: str | None
    context: str
    change_text: str
    event_entry_count: int
    event_contexts: tuple[str, ...]


@dataclass(frozen=True)
class ClassificationResult:
    """Validated classification output suitable for local persistence."""

    label: str
    confidence: str
    classified_at: str


class LiteLLMClient:
    """Sequential client for an OpenAI-compatible LiteLLM proxy endpoint."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        delay: float = 0.0,
        timeout: float = 120.0,
        retries: int = 3,
        verify: bool = True,
    ) -> None:
        """Configure the endpoint, model, request spacing, and retry policy."""
        if not api_key:
            msg = 'LITELLM_KEY must not be empty.'
            raise ValueError(msg)
        if not model:
            msg = 'LITELLM_MODEL must not be empty.'
            raise ValueError(msg)
        if delay < 0 or timeout <= 0 or retries < 0:
            msg = 'Delay/retries must be non-negative and timeout must be positive.'
            raise ValueError(msg)
        self.api_key = api_key
        self.endpoint_url = _chat_completions_url(base_url)
        self.model = model
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self._last_request_at: float | None = None
        self._use_response_format = True
        self.verify = verify
        self.ssl_context = ssl.create_default_context()
        if not verify:
            self.ssl_context.check_hostname = False
            self.ssl_context.verify_mode = ssl.CERT_NONE

    def classify(self, candidate: ClassificationCandidate) -> ClassificationResult:
        """Classify one entry, retrying transient and malformed responses."""
        input_json = classification_input_json(candidate)
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                response = self._post(self._request_payload(input_json))
                return _parse_result(response)
            except _RetryableClassificationError as error:
                last_error = error
                if attempt == self.retries:
                    break
                backoff = error.retry_after if error.retry_after is not None else float(2**attempt)
                LOGGER.warning('Classification failed; retrying in %.1f seconds: %s', backoff, error)
                time.sleep(backoff)
        msg = f'Classification failed after {self.retries + 1} attempts.'
        raise ClassificationError(msg) from last_error

    def _request_payload(self, input_json: str) -> Mapping[str, Any]:
        payload: dict[str, Any] = {
            'messages': [
                {'role': 'system', 'content': SYSTEM_PROMPT},
                {'role': 'user', 'content': input_json},
            ],
            'model': self.model,
        }
        if self._use_response_format:
            payload['response_format'] = {'type': 'json_object'}
        return payload

    def _post(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self._wait_for_rate_limit()
        body = json.dumps(payload, ensure_ascii=False).encode()
        request = Request(  # noqa: S310 - URL scheme is validated before use.
            self.endpoint_url,
            data=body,
            headers={
                'Authorization': f'Bearer {self.api_key}',
                'Content-Type': 'application/json',
            },
            method='POST',
        )
        try:
            self._last_request_at = time.monotonic()
            with urlopen(  # noqa: S310 - validated HTTP(S) endpoint.
                request,
                timeout=self.timeout,
                context=self.ssl_context,
            ) as response:
                raw = response.read().decode('utf-8')
        except HTTPError as error:
            error_body = error.read().decode('utf-8', errors='replace')
            if self._disable_unsupported_response_format(error.code, error_body):
                message = 'Endpoint rejected response_format; retrying without it.'
                raise _RetryableClassificationError(message) from error
            message = f'LiteLLM returned HTTP {error.code}: {error_body[:1_000]}'
            if error.code in _RETRYABLE_STATUS_CODES:
                raise _RetryableClassificationError(message, _retry_after(error)) from error
            raise ClassificationError(message) from error
        except (TimeoutError, URLError) as error:
            message = f'LiteLLM request failed: {error}'
            raise _RetryableClassificationError(message) from error
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as error:
            message = 'LiteLLM returned non-JSON response data.'
            raise _RetryableClassificationError(message) from error
        if not isinstance(decoded, Mapping):
            msg = 'LiteLLM response is not a JSON object.'
            raise _RetryableClassificationError(msg)
        return decoded

    def _disable_unsupported_response_format(self, status_code: int, error_body: str) -> bool:
        unsupported_status_codes = {400, 422}
        indicators = ('response_format', 'json_object', 'structured output')
        if self._use_response_format and status_code in unsupported_status_codes:
            normalized = error_body.casefold()
            if any(indicator in normalized for indicator in indicators):
                self._use_response_format = False
                return True
        return False

    def _wait_for_rate_limit(self) -> None:
        if self._last_request_at is None:
            return
        remaining = self.delay - (time.monotonic() - self._last_request_at)
        if remaining > 0:
            time.sleep(remaining)


def classification_input_json(candidate: ClassificationCandidate) -> str:
    """Serialize the stable entry input supplied to the model."""
    data = {
        'champion': candidate.champion_name,
        'event_contexts': candidate.event_contexts,
        'event_entry_count': candidate.event_entry_count,
        'patch_effective_date': candidate.effective_date,
        'patch_heading': candidate.patch_heading,
        'patch_id': candidate.patch_id,
        'patch_release_date': candidate.patch_release_date,
        'target_change': {
            'context': candidate.context,
            'text': candidate.change_text,
        },
    }
    return json.dumps(data, ensure_ascii=False, separators=(',', ':'), sort_keys=True)


def _chat_completions_url(base_url: str) -> str:
    value = base_url.strip().rstrip('/')
    parsed = urlsplit(value)
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc or parsed.query or parsed.fragment:
        msg = 'LITELLM_URL must be an HTTP(S) base URL without a query or fragment.'
        raise ValueError(msg)
    if not parsed.path.rstrip('/').endswith('/chat/completions'):
        value = f'{value}/chat/completions'
    return value


def _parse_result(response: Mapping[str, Any]) -> ClassificationResult:
    content = _response_content(response)
    parsed = _parse_content_json(content)
    required_fields = {'label', 'confidence'}
    if set(parsed) != required_fields:
        msg = f'Classification fields must be exactly {sorted(required_fields)}.'
        raise _RetryableClassificationError(msg)
    label = parsed.get('label')
    confidence = parsed.get('confidence')
    if label not in LABELS:
        msg = f'Invalid classification label: {label!r}.'
        raise _RetryableClassificationError(msg)
    if confidence not in CONFIDENCE_LEVELS:
        msg = f'Invalid classification confidence: {confidence!r}.'
        raise _RetryableClassificationError(msg)
    return ClassificationResult(
        label=str(label),
        confidence=str(confidence),
        classified_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    )


def _response_content(response: Mapping[str, Any]) -> str:
    choices = response.get('choices')
    if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes)) or not choices:
        msg = 'LiteLLM response does not contain choices.'
        raise _RetryableClassificationError(msg)
    first_choice = choices[0]
    if not isinstance(first_choice, Mapping) or not isinstance(first_choice.get('message'), Mapping):
        msg = 'LiteLLM response does not contain choices[0].message.'
        raise _RetryableClassificationError(msg)
    message = first_choice['message']
    parsed = message.get('parsed')
    if isinstance(parsed, Mapping):
        return json.dumps(parsed, ensure_ascii=False)
    content = message.get('content')
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence):
        text_parts = [
            part.get('text') for part in content if isinstance(part, Mapping) and isinstance(part.get('text'), str)
        ]
        if text_parts:
            return ''.join(text_parts)
    msg = 'LiteLLM response message has no text content.'
    raise _RetryableClassificationError(msg)


def _parse_content_json(content: str) -> Mapping[str, Any]:
    value = content.strip()
    fence_match = _JSON_FENCE.match(value)
    if fence_match:
        value = fence_match.group('body').strip()
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        start = value.find('{')
        end = value.rfind('}')
        if start < 0 or end <= start:
            msg = 'Model response does not contain a JSON object.'
            raise _RetryableClassificationError(msg) from None
        try:
            parsed = json.loads(value[start : end + 1])
        except json.JSONDecodeError as error:
            message = 'Model response contains invalid JSON.'
            raise _RetryableClassificationError(message) from error
    if not isinstance(parsed, Mapping):
        msg = 'Model response JSON is not an object.'
        raise _RetryableClassificationError(msg)
    return parsed


def _retry_after(error: HTTPError) -> float | None:
    value = error.headers.get('Retry-After')
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None
