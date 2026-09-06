"""Classify aggregate champion-patch notes through a LiteLLM proxy."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from functools import lru_cache
from multiprocessing import get_context
from pathlib import Path
from typing import Sequence

from tqdm import tqdm

from lol_skin_release_statistics.classification import (
    ClassificationCandidate,
    ClassificationError,
    ClassificationResult,
    LiteLLMClient,
    classification_input_json,
)
from lol_skin_release_statistics.database import Database

DEFAULT_DATABASE = Path('data/lol_skin_release_statistics.sqlite3')
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class _WorkerConfig:
    """Pickle-safe LiteLLM settings shared by worker processes."""

    api_key: str
    base_url: str
    model: str
    delay: float
    timeout: float
    retries: int
    verify: bool


def build_parser() -> argparse.ArgumentParser:
    """Build the classifier command-line parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=DEFAULT_DATABASE, help='SQLite database to update.')
    parser.add_argument(
        '--champion',
        action='append',
        help='Classify only this exact canonical champion name; repeat for multiple champions.',
    )
    parser.add_argument('--patch', action='append', help='Classify only this patch ID; repeat for multiple patches.')
    volume = parser.add_mutually_exclusive_group()
    volume.add_argument('--limit', type=_positive_int, help='Maximum number of champion-patches to classify in this run.')
    volume.add_argument(
        '--all',
        action='store_true',
        dest='classify_all',
        help='Explicitly classify every remaining entry in the selected scope.',
    )
    parser.add_argument('--force', action='store_true', help='Replace classifications already stored for the scope.')
    parser.add_argument('--delay', type=float, default=0.0, help='Minimum seconds between LiteLLM requests.')
    parser.add_argument('--timeout', type=float, default=120.0, help='Request timeout in seconds.')
    parser.add_argument('--retries', type=int, default=3, help='Retries after transient or invalid responses.')
    parser.add_argument(
        '--workers',
        type=_positive_int,
        default=12,
        help='Number of parallel classification processes (default: 12).',
    )
    parser.add_argument(
        '--no-verify-ssl',
        action='store_true',
        help='Disable TLS certificate and hostname verification for the LiteLLM endpoint.',
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show candidate inputs without calling LiteLLM or changing classifications.',
    )
    parser.add_argument('--verbose', action='store_true', help='Log every classified champion-patch.')
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run classification and return a process exit code."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s',
    )
    try:
        _validate_runtime_arguments(args)
        effective_limit = args.limit if args.limit is not None else (5 if args.dry_run else None)
        with Database(args.database) as database:
            candidates = database.classification_candidates(
                champion_names=args.champion,
                patch_ids=args.patch,
                limit=effective_limit,
                force=args.force,
            )
            if args.dry_run:
                _log_dry_run(candidates)
                return 0
            if args.no_verify_ssl:
                LOGGER.warning('TLS certificate and hostname verification are disabled for the LiteLLM endpoint.')
            worker_config = _WorkerConfig(
                api_key=_environment_value('LITELLM_KEY'),
                base_url=_environment_value('LITELLM_URL'),
                model=_environment_value('LITELLM_MODEL'),
                delay=args.delay,
                timeout=args.timeout,
                retries=args.retries,
                verify=not args.no_verify_ssl,
            )
            _classify_candidates(database, worker_config, candidates, args.workers, args.verbose)
            counts = database.classification_counts()
            LOGGER.info('Stored classifications: %s', json.dumps(counts, sort_keys=True))
    except (ClassificationError, KeyError, OSError, ValueError, sqlite3.Error):
        LOGGER.exception('Patch classification failed')
        return 1
    except KeyboardInterrupt:
        LOGGER.warning('Classification interrupted; completed entries were saved and the run can be resumed.')
        return 130
    return 0


def _classify_candidates(
    database: Database,
    worker_config: _WorkerConfig,
    candidates: Sequence[ClassificationCandidate],
    workers: int,
    verbose: bool,
) -> None:
    total = len(candidates)
    if total == 0:
        LOGGER.info('No champion-patches require classification.')
        return
    LOGGER.info('Classifying %d champion-patches with %d worker processes', total, workers)
    tasks = ((candidate, worker_config) for candidate in candidates)
    with get_context('spawn').Pool(processes=workers) as pool:
        results = pool.imap_unordered(_classify_candidate, tasks, chunksize=1)
        for candidate, result in tqdm(results, total=total, desc='Classifying', unit='champion-patch'):
            database.save_patch_classification(candidate.champion_id, candidate.patch_id, result)
            if verbose:
                LOGGER.info(
                    'Classified champion=%s patch=%s label=%s confidence=%s',
                    candidate.champion_name,
                    candidate.patch_id,
                    result.label,
                    result.confidence,
                )


def _classify_candidate(
    task: tuple[ClassificationCandidate, _WorkerConfig],
) -> tuple[ClassificationCandidate, ClassificationResult]:
    """Classify one task inside a worker process."""
    candidate, worker_config = task
    result = _worker_client(worker_config).classify(candidate)
    return candidate, result


@lru_cache(maxsize=1)
def _worker_client(config: _WorkerConfig) -> LiteLLMClient:
    """Create one reusable client per worker process."""
    return LiteLLMClient(
        api_key=config.api_key,
        base_url=config.base_url,
        model=config.model,
        delay=config.delay,
        timeout=config.timeout,
        retries=config.retries,
        verify=config.verify,
    )


def _log_dry_run(candidates: Sequence[ClassificationCandidate]) -> None:
    LOGGER.info('Dry run: %d candidate champion-patches', len(candidates))
    for candidate in candidates:
        LOGGER.info(
            'champion=%s patch=%s input=%s',
            candidate.champion_name,
            candidate.patch_id,
            classification_input_json(candidate),
        )


def _environment_value(name: str, required: bool = True) -> str:
    value = os.environ.get(name, '').strip()
    if required and not value:
        msg = f'Required environment variable {name} is not set.'
        raise ValueError(msg)
    return value


def _validate_runtime_arguments(args: argparse.Namespace) -> None:
    if not args.dry_run and args.limit is None and not args.classify_all:
        msg = 'Specify --limit N for a bounded run or --all to classify every remaining entry.'
        raise ValueError(msg)
    if args.delay < 0:
        msg = '--delay cannot be negative.'
        raise ValueError(msg)
    if args.timeout <= 0:
        msg = '--timeout must be positive.'
        raise ValueError(msg)
    if args.retries < 0:
        msg = '--retries cannot be negative.'
        raise ValueError(msg)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        msg = 'value must be greater than zero'
        raise argparse.ArgumentTypeError(msg)
    return parsed


if __name__ == '__main__':
    raise SystemExit(main())
