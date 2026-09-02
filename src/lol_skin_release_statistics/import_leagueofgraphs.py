"""Import manually saved League of Graphs champion histories into SQLite."""

from __future__ import annotations

import argparse
import logging
import sqlite3
from pathlib import Path
from typing import Sequence

from tqdm import tqdm

from lol_skin_release_statistics.database import Database
from lol_skin_release_statistics.leagueofgraphs import (
    LeagueOfGraphsPage,
    LeagueOfGraphsParseError,
    parse_leagueofgraphs_page,
)

DEFAULT_DATABASE = Path('data/lol_skin_release_statistics.sqlite3')
DEFAULT_INPUT_DIRECTORY = Path('data/leagueofgraphs_html_pages')
LOGGER = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    """Build the offline importer command-line parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=DEFAULT_DATABASE, help='SQLite database to update.')
    parser.add_argument(
        '--input-dir',
        type=Path,
        default=DEFAULT_INPUT_DIRECTORY,
        help='Directory recursively searched for .htm files.',
    )
    parser.add_argument('--dry-run', action='store_true', help='Parse and summarize files without writing SQLite.')
    parser.add_argument('--verbose', action='store_true', help='Report every parsed champion page.')
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Import saved pages and return a process exit code."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s',
    )
    try:
        files = _html_files(args.input_dir)
        pages = _parse_pages(files, args.verbose)
        _validate_unique_pages(pages)
        if args.dry_run:
            _log_summary(pages, dry_run=True)
            return 0
        with Database(args.database) as database:
            database.replace_graph_pages(pages)
        _log_summary(pages, dry_run=False)
    except (KeyError, LeagueOfGraphsParseError, OSError, ValueError, sqlite3.Error):
        LOGGER.exception('League of Graphs HTML import failed')
        return 1
    return 0


def _html_files(input_directory: Path) -> list[Path]:
    if not input_directory.is_dir():
        msg = f'Input directory does not exist: {input_directory}'
        raise ValueError(msg)
    files = sorted(input_directory.rglob('*.htm'))
    if not files:
        msg = f'No .htm files found under {input_directory}'
        raise ValueError(msg)
    return files


def _parse_pages(files: Sequence[Path], verbose: bool) -> list[LeagueOfGraphsPage]:
    pages: list[LeagueOfGraphsPage] = []
    for path in tqdm(files, desc='Parsing HTML', unit='page'):
        page = parse_leagueofgraphs_page(path)
        pages.append(page)
        if verbose:
            LOGGER.info(
                'Parsed %s: slug=%s, points=%d per metric',
                page.champion_name,
                page.slug,
                len(page.series[0].points),
            )
    return pages


def _validate_unique_pages(pages: Sequence[LeagueOfGraphsPage]) -> None:
    game_ids: set[int] = set()
    slugs: set[str] = set()
    for page in pages:
        if page.game_id in game_ids:
            msg = f'Multiple HTML files contain Riot champion ID {page.game_id}.'
            raise ValueError(msg)
        if page.slug in slugs:
            msg = f'Multiple HTML files contain League of Graphs slug {page.slug!r}.'
            raise ValueError(msg)
        game_ids.add(page.game_id)
        slugs.add(page.slug)


def _log_summary(pages: Sequence[LeagueOfGraphsPage], dry_run: bool) -> None:
    series_count = sum(len(page.series) for page in pages)
    point_count = sum(len(series.points) for page in pages for series in page.series)
    action = 'Parsed' if dry_run else 'Imported'
    LOGGER.info('%s %d champion pages, %d series, and %d points', action, len(pages), series_count, point_count)


if __name__ == '__main__':
    raise SystemExit(main())
