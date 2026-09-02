"""MediaWiki API client and parsers for League of Legends Wiki data."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from hashlib import sha256
from typing import Any, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode
from urllib.request import Request, urlopen

import luadata
from bs4 import BeautifulSoup, NavigableString, Tag

API_URL = 'https://wiki.leagueoflegends.com/en-us/api.php'
WIKI_ROOT = 'https://wiki.leagueoflegends.com/en-us'
LOGGER = logging.getLogger(__name__)
_PATCH_PATH = re.compile(r'^/en-us/(?P<title>[^#?]+)')
_REDIRECT = re.compile(r'^\s*#REDIRECT\s*\[\[(?P<title>[^\]|#]+)', re.IGNORECASE)
_DATE_IN_TEXT = re.compile(
    r'(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)'
    r'\s+(?P<day>\d{1,2})\s*(?:st|nd|rd|th)?(?:,\s*(?P<year>\d{4}))?',
)
_NIL_SENTINEL = '__LOL_WIKI_LUA_NIL__'
_MINIMUM_CHAMPION_COUNT = 150
_MINIMUM_SKIN_COUNT = 2_000
_YEAR_ROLLOVER_THRESHOLD_DAYS = 300


class WikiError(RuntimeError):
    """Raised when the Wiki response cannot be fetched or parsed."""


@dataclass(frozen=True)
class Champion:
    """Canonical champion identity discovered from the Wiki."""

    game_id: int
    name: str
    wiki_title: str
    wiki_data_name: str
    wiki_url: str


@dataclass(frozen=True)
class SkinChroma:
    """A chroma belonging to a base skin."""

    name: str
    chroma_id: int | None
    availability: str | None
    source: str | None
    distribution: str | None


@dataclass(frozen=True)
class Skin:
    """Structured skin metadata from the SkinData module."""

    champion_name: str
    internal_name: str
    display_name: str
    skin_id: int | None
    is_base: bool
    release_date: date | None
    release_date_raw: str
    retired_date: date | None
    availability: str | None
    cost: int | None
    loot_eligible: bool | None
    sets: tuple[str, ...]
    chromas: tuple[SkinChroma, ...]


@dataclass(frozen=True)
class PatchEntry:
    """One leaf-level balance change under a patch-history context."""

    context: str
    change_text: str
    source_order: int


@dataclass(frozen=True)
class PatchEvent:
    """One patch or hotfix section in a champion patch history."""

    patch_id: str
    wiki_title: str | None
    wiki_url: str | None
    heading: str
    source_order: int
    entries: tuple[PatchEntry, ...]


@dataclass(frozen=True)
class ApiResponse:
    """Raw and decoded result of a MediaWiki API request."""

    payload: Mapping[str, Any]
    raw: bytes
    url: str
    retrieved_at: str
    content_hash: str


class WikiClient:
    """Small rate-limited client for the public MediaWiki API."""

    def __init__(self, user_agent: str, delay: float = 0.75, retries: int = 3) -> None:
        """Configure identification, minimum request spacing, and retries."""
        if not user_agent.strip():
            msg = 'A non-empty user agent is required.'
            raise ValueError(msg)
        if delay < 0:
            msg = 'The request delay cannot be negative.'
            raise ValueError(msg)
        self.user_agent = user_agent
        self.delay = delay
        self.retries = retries
        self._last_request_at: float | None = None

    def parse_page(self, title: str, properties: Sequence[str]) -> ApiResponse:
        """Fetch parsed page properties for one MediaWiki title."""
        params = {
            'action': 'parse',
            'format': 'json',
            'formatversion': '2',
            'origin': '*',
            'page': title,
            'prop': '|'.join(properties),
            'redirects': '1',
        }
        url = f'{API_URL}?{urlencode(params)}'
        return self._get(url)

    def _get(self, url: str) -> ApiResponse:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            self._wait_for_rate_limit()
            request = Request(url, headers={'User-Agent': self.user_agent})  # noqa: S310
            try:
                self._last_request_at = time.monotonic()
                with urlopen(request, timeout=45) as response:  # noqa: S310 - URL is the fixed HTTPS Wiki API.
                    raw = response.read()
                payload = json.loads(raw)
                if 'error' in payload:
                    msg = f'MediaWiki API error for {url}: {payload["error"]}'
                    raise WikiError(msg)
                now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
                return ApiResponse(payload, raw, url, now, sha256(raw).hexdigest())
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
                last_error = error
                if attempt == self.retries:
                    break
                backoff = 2**attempt
                LOGGER.warning('Wiki request failed; retrying in %s seconds: %s', backoff, error)
                time.sleep(backoff)
        msg = f'Unable to retrieve {url} after {self.retries + 1} attempts.'
        raise WikiError(msg) from last_error

    def _wait_for_rate_limit(self) -> None:
        if self._last_request_at is None:
            return
        remaining = self.delay - (time.monotonic() - self._last_request_at)
        if remaining > 0:
            time.sleep(remaining)


def parse_champions(html: str, champion_ids: Mapping[str, int]) -> list[Champion]:
    """Extract canonical champion names and URLs from the champion list."""
    soup = BeautifulSoup(html, 'html.parser')
    champions: dict[str, Champion] = {}
    for row in soup.select('tr'):
        marker = row.select_one('[data-champion][data-game="lol"]')
        if marker is None:
            continue
        name_cell = row.find('td', attrs={'data-sort-value': True})
        if not isinstance(name_cell, Tag):
            continue
        anchor = name_cell.find('a', href=True)
        if not isinstance(anchor, Tag):
            continue
        href = str(anchor['href'])
        match = _PATCH_PATH.match(href)
        if match is None or '/' in match.group('title'):
            continue
        wiki_title = _decode_wiki_title(match.group('title'))
        name = str(name_cell.get('data-sort-value', '')).strip()
        wiki_data_name = str(marker.get('data-champion', '')).strip()
        game_id = champion_ids.get(name)
        if not name or not wiki_data_name or game_id is None:
            continue
        champions[wiki_title] = Champion(
            game_id,
            name,
            wiki_title,
            wiki_data_name,
            f'{WIKI_ROOT}/{quote(wiki_title)}',
        )
    if len(champions) < _MINIMUM_CHAMPION_COUNT:
        msg = f'Champion-list structure changed: found only {len(champions)} champions.'
        raise WikiError(msg)
    missing = sorted(champion_ids.keys() - {champion.name for champion in champions.values()})
    if missing:
        msg = f'Champion list is missing SkinData champions: {missing}'
        raise WikiError(msg)
    return sorted(champions.values(), key=lambda champion: champion.game_id)


def parse_champion_ids(wikitext: str) -> dict[str, int]:
    """Extract Riot's stable numeric champion IDs from the SkinData module."""
    table = _parse_lua_table(wikitext)
    champion_ids = {
        str(name): game_id
        for name, raw_data in table.items()
        if (game_id := _parse_int(_as_mapping(raw_data, f'champion {name}').get('id'))) is not None
    }
    if len(champion_ids) < _MINIMUM_CHAMPION_COUNT:
        msg = f'SkinData structure changed: found only {len(champion_ids)} champion IDs.'
        raise WikiError(msg)
    return champion_ids


def parse_skins(wikitext: str) -> list[Skin]:
    """Parse the Wiki's generated Lua SkinData table without executing Lua."""
    table = _parse_lua_table(wikitext)
    skins: list[Skin] = []
    for champion_name, champion_data in table.items():
        champion = _as_mapping(champion_data, f'champion {champion_name}')
        skin_table = _as_mapping(champion.get('skins'), f'skins for {champion_name}')
        for internal_name, skin_data in skin_table.items():
            metadata = _as_mapping(skin_data, f'{champion_name} {internal_name}')
            release_raw = str(metadata.get('release') or '')
            release = _parse_iso_date(release_raw)
            display_name = str(metadata.get('formatname') or internal_name)
            skins.append(
                Skin(
                    champion_name=str(champion_name),
                    internal_name=str(internal_name),
                    display_name=display_name,
                    skin_id=_parse_int(metadata.get('id')),
                    is_base=internal_name == 'Original',
                    release_date=release,
                    release_date_raw=release_raw,
                    retired_date=_parse_iso_date(metadata.get('retired')),
                    availability=_optional_string(metadata.get('availability')),
                    cost=_parse_int(metadata.get('cost')),
                    loot_eligible=_parse_bool(metadata.get('looteligible')),
                    sets=_parse_string_tuple(metadata.get('set')),
                    chromas=_parse_chromas(metadata.get('chromas')),
                ),
            )
    if len(skins) < _MINIMUM_SKIN_COUNT:
        msg = f'SkinData structure changed: found only {len(skins)} skins.'
        raise WikiError(msg)
    return skins


def parse_patch_history(html: str) -> list[PatchEvent]:
    """Extract patch sections and individual leaf changes from rendered history HTML."""
    soup = BeautifulSoup(html, 'html.parser')
    current_heading = soup.find(id='Current_version') or soup.find(id='Release_version')
    if not isinstance(current_heading, Tag) or not isinstance(current_heading.parent, Tag):
        msg = 'Patch history does not contain a Current version or Release version section.'
        raise WikiError(msg)

    events: list[PatchEvent] = []
    for node in current_heading.parent.next_siblings:
        if not isinstance(node, Tag) or node.name != 'dl':
            continue
        event = _parse_patch_event(node, len(events))
        if event is not None:
            events.append(event)
    if not events:
        msg = 'Patch history contained no patch events.'
        raise WikiError(msg)
    return events


def parse_patch_release_date(html: str, wiki_title: str) -> date | None:
    """Read the US release date from a patch page, with a title-date fallback."""
    soup = BeautifulSoup(html, 'html.parser')
    for label in soup.select('.infobox-data-label'):
        if not _clean_text(label).casefold().startswith('release date'):
            continue
        value = label.find_next_sibling(class_='infobox-data-value')
        if isinstance(value, Tag):
            parsed = parse_natural_date(_clean_text(value))
            if parsed is not None:
                return parsed
    return parse_natural_date(wiki_title)


def parse_redirect_title(wikitext: str) -> str | None:
    """Extract a target title from cached redirect wikitext."""
    match = _REDIRECT.match(wikitext)
    return match.group('title').strip() if match else None


def parse_hotfix_date(heading: str, patch_release_date: date | None) -> date | None:
    """Derive a hotfix date from its heading and the base patch's release year."""
    if patch_release_date is None:
        return None
    match = _DATE_IN_TEXT.search(heading)
    if match is None:
        return patch_release_date
    year = int(match.group('year') or patch_release_date.year)
    candidate = datetime.strptime(  # noqa: DTZ007 - immediately converted to a date with an explicit inferred year.
        f'{match.group("month")} {match.group("day")} {year}',
        '%B %d %Y',
    ).date()
    if candidate < patch_release_date and (patch_release_date - candidate).days > _YEAR_ROLLOVER_THRESHOLD_DAYS:
        candidate = candidate.replace(year=candidate.year + 1)
    return candidate


def parse_natural_date(value: str) -> date | None:
    """Parse an English month/day/year embedded in Wiki text."""
    match = _DATE_IN_TEXT.search(value)
    if match is None or match.group('year') is None:
        return None
    normalized = f'{match.group("month")} {match.group("day")} {match.group("year")}'
    return datetime.strptime(normalized, '%B %d %Y').date()  # noqa: DTZ007


def wiki_page_url(title: str) -> str:
    """Return the canonical Wiki URL for a page title."""
    encoded_title = quote(title.replace(' ', '_'), safe="/:_'()")
    return f'{WIKI_ROOT}/{encoded_title}'


def _parse_patch_event(dl: Tag, event_order: int) -> PatchEvent | None:
    dt = dl.find('dt')
    if not isinstance(dt, Tag):
        return None
    changes = dl.find_next_sibling('ul')
    if not isinstance(changes, Tag):
        return None
    heading = _clean_text(dt)
    anchor = dt.find('a', href=True, recursive=False)
    if isinstance(anchor, Tag) and (match := _PATCH_PATH.match(str(anchor['href']))) is not None:
        wiki_title = str(anchor.get('title') or _decode_wiki_title(match.group('title')))
        patch_id = _clean_text(anchor)
        wiki_url = f'{WIKI_ROOT}/{match.group("title")}'
    else:
        wiki_title = None
        patch_id = heading
        wiki_url = None
    entries: list[PatchEntry] = []
    _collect_patch_entries(changes, (), entries)
    return PatchEvent(
        patch_id=patch_id,
        wiki_title=wiki_title,
        wiki_url=wiki_url,
        heading=heading,
        source_order=event_order,
        entries=tuple(entries),
    )


def _collect_patch_entries(node: Tag, context: tuple[str, ...], entries: list[PatchEntry]) -> None:
    for item in node.find_all('li', recursive=False):
        child_list = item.find(['ul', 'ol'], recursive=False)
        direct_text = _direct_text(item)
        if isinstance(child_list, Tag):
            next_context = (*context, direct_text) if direct_text else context
            _collect_patch_entries(child_list, next_context, entries)
        elif direct_text:
            entries.append(PatchEntry(' > '.join(context), direct_text, len(entries)))


def _direct_text(item: Tag) -> str:
    fragments: list[str] = []
    for child in item.children:
        if isinstance(child, NavigableString):
            fragments.append(str(child))
        elif isinstance(child, Tag) and child.name not in {'ul', 'ol'}:
            fragments.append(child.get_text(' ', strip=True))
    return _normalize_text(' '.join(fragments))


def _clean_text(tag: Tag) -> str:
    return _normalize_text(tag.get_text(' ', strip=True))


def _normalize_text(value: str) -> str:
    value = re.sub(r'(?<=\d)\.\s+(?=\d)', '.', value)
    value = re.sub(r'\s+([,.;:%)])', r'\1', value)
    value = re.sub(r'([(])\s+', r'\1', value)
    return re.sub(r'\s+', ' ', value).strip()


def _parse_lua_table(wikitext: str) -> Mapping[str, Any]:
    return_position = wikitext.find('return')
    if return_position < 0:
        msg = 'SkinData wikitext has no Lua return statement.'
        raise WikiError(msg)
    expression = wikitext[return_position + len('return') :]
    expression = expression.rsplit('-- </pre>', maxsplit=1)[0]
    expression = re.sub(r'=\s*nil(?=\s*[,}])', f'= "{_NIL_SENTINEL}"', expression)
    try:
        parsed = luadata.unserialize(expression)
    except Exception as error:
        msg = 'Unable to parse Module:SkinData/data as a Lua table.'
        raise WikiError(msg) from error
    return _as_mapping(parsed, 'SkinData root')


def _parse_chromas(value: Any) -> tuple[SkinChroma, ...]:
    if value is None:
        return ()
    chroma_table = _as_mapping(value, 'skin chromas')
    chromas: list[SkinChroma] = []
    for name, raw_metadata in chroma_table.items():
        metadata = _as_mapping(raw_metadata, f'chroma {name}')
        chromas.append(
            SkinChroma(
                name=str(name),
                chroma_id=_parse_int(metadata.get('id')),
                availability=_optional_string(metadata.get('availability')),
                source=_optional_string(metadata.get('source')),
                distribution=_optional_string(metadata.get('distribution')),
            ),
        )
    return tuple(chromas)


def _as_mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        msg = f'Expected a mapping for {context}, got {type(value).__name__}.'
        raise WikiError(msg)
    return value


def _parse_string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, Mapping):
        return tuple(str(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    return (str(value),)


def _parse_iso_date(value: Any) -> date | None:
    if not isinstance(value, str) or value == _NIL_SENTINEL:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _parse_int(value: Any) -> int | None:
    if value is None or value == _NIL_SENTINEL or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _optional_string(value: Any) -> str | None:
    if value is None or value == _NIL_SENTINEL:
        return None
    return str(value)


def _decode_wiki_title(value: str) -> str:
    return unquote(value).replace('_', ' ')
