"""Command-line collector for League of Legends Wiki data."""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Mapping, Sequence

from lol_skin_release_statistics.database import Database
from lol_skin_release_statistics.wiki import (
    ApiResponse,
    WikiClient,
    WikiError,
    parse_champion_ids,
    parse_champions,
    parse_patch_history,
    parse_patch_release_date,
    parse_redirect_title,
    parse_skins,
    wiki_page_url,
)

DEFAULT_DATABASE = Path('data/lol_skin_release_statistics.sqlite3')
DEFAULT_USER_AGENT = 'lol-skin-release-statistics/0.1 (research collector; contact: repository owner)'
LOGGER = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=DEFAULT_DATABASE, help='SQLite output/cache path.')
    parser.add_argument(
        '--stage',
        action='append',
        choices=('champions', 'skins', 'patches'),
        help='Collection stage to run; repeat as needed. The default runs every stage.',
    )
    parser.add_argument(
        '--champion',
        action='append',
        help='Limit patch-history collection to this exact champion name; repeat for multiple champions.',
    )
    parser.add_argument('--delay', type=float, default=0.75, help='Minimum seconds between live API requests.')
    parser.add_argument('--refresh', action='store_true', help='Ignore cached raw responses and fetch again.')
    parser.add_argument('--user-agent', default=DEFAULT_USER_AGENT, help='Identifying HTTP User-Agent value.')
    parser.add_argument('--verbose', action='store_true', help='Enable detailed progress logs.')
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run selected Wiki collection stages and return a process exit code."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s',
    )
    stages = tuple(dict.fromkeys(args.stage or ('champions', 'skins', 'patches')))
    try:
        with Database(args.database) as database:
            client = WikiClient(args.user_agent, args.delay)
            if 'champions' in stages or not database.champion_names():
                collect_champions(client, database, args.refresh)
            if 'skins' in stages:
                collect_skins(client, database, args.refresh)
            if 'patches' in stages:
                collect_patches(client, database, args.refresh, args.champion)
            LOGGER.info('Collection complete: %s', json.dumps(database.counts(), sort_keys=True))
    except (KeyError, OSError, ValueError, WikiError, sqlite3.Error):
        LOGGER.exception('Wiki collection failed')
        return 1
    return 0


def collect_champions(client: WikiClient, database: Database, refresh: bool) -> None:
    """Discover and persist the Wiki champion registry."""
    LOGGER.info('Collecting canonical champion list')
    payload = _get_page(client, database, 'champion_list', 'List_of_champions', ('text',), refresh)
    skin_payload = _get_page(client, database, 'skin_data', 'Module:SkinData/data', ('wikitext',), refresh)
    champion_ids = parse_champion_ids(_parse_property(skin_payload, 'wikitext'))
    champions = parse_champions(_parse_property(payload, 'text'), champion_ids)
    database.replace_champions(champions)
    LOGGER.info('Stored %d champions', len(champions))


def collect_skins(client: WikiClient, database: Database, refresh: bool) -> None:
    """Collect the global skin and chroma metadata table."""
    LOGGER.info('Collecting Module:SkinData/data')
    payload = _get_page(client, database, 'skin_data', 'Module:SkinData/data', ('wikitext',), refresh)
    skins = parse_skins(_parse_property(payload, 'wikitext'))
    database.replace_skins(skins)
    LOGGER.info('Stored %d skins', len(skins))


def collect_patches(
    client: WikiClient,
    database: Database,
    refresh: bool,
    selected_champions: Sequence[str] | None,
) -> None:
    """Collect champion patch histories and referenced patch release dates."""
    champions = list(selected_champions or database.champion_names())
    collected_patch_ids: set[str] = set()
    for index, champion_name in enumerate(champions, start=1):
        data_name = database.champion_data_name(champion_name)
        history_title = f'{data_name}/Patch history'
        LOGGER.info('Collecting patch history %d/%d: %s', index, len(champions), champion_name)
        payload = _get_page(client, database, 'patch_history', history_title, ('text', 'wikitext'), refresh)
        events = parse_patch_history(_parse_property(payload, 'text'))
        database.replace_patch_history(champion_name, events, wiki_page_url(history_title))
        collected_patch_ids.update(event.patch_id for event in events)

    patch_pages = database.patch_pages(refresh=refresh)
    if selected_champions is not None:
        patch_pages = [page for page in patch_pages if page[0] in collected_patch_ids]
    for index, (patch_id, wiki_title) in enumerate(patch_pages, start=1):
        LOGGER.info('Collecting patch date %d/%d: %s', index, len(patch_pages), patch_id)
        payload = _get_page(client, database, 'patch_page', wiki_title, ('text', 'wikitext'), refresh)
        release_date = parse_patch_release_date(_parse_property(payload, 'text'), wiki_title)
        redirect_title = parse_redirect_title(_parse_property(payload, 'wikitext'))
        if release_date is None and redirect_title is not None:
            redirect_payload = _get_page(
                client,
                database,
                'patch_page',
                redirect_title,
                ('text', 'wikitext'),
                refresh,
            )
            release_date = parse_patch_release_date(_parse_property(redirect_payload, 'text'), redirect_title)
        if release_date is None:
            LOGGER.warning('No release date found for %s (%s)', patch_id, wiki_title)
        database.set_patch_release_date(patch_id, release_date)
    database.update_event_effective_dates()


def _get_page(
    client: WikiClient,
    database: Database,
    source_type: str,
    title: str,
    properties: Sequence[str],
    refresh: bool,
) -> Mapping[str, Any]:
    if not refresh:
        cached = database.load_source(source_type, title)
        if cached is not None:
            LOGGER.debug('Using cached source: %s', title)
            return cached
    response = client.parse_page(title, properties)
    _save_response(database, source_type, title, response)
    return response.payload


def _save_response(database: Database, source_type: str, title: str, response: ApiResponse) -> None:
    database.save_source(
        source_type,
        title,
        response.url,
        response.retrieved_at,
        response.content_hash,
        response.raw,
    )


def _parse_property(payload: Mapping[str, Any], property_name: str) -> str:
    parsed = payload.get('parse')
    if not isinstance(parsed, Mapping):
        msg = 'MediaWiki response does not contain a parse object.'
        raise WikiError(msg)
    value = parsed.get(property_name)
    if not isinstance(value, str):
        msg = f'MediaWiki response does not contain parse.{property_name} text.'
        raise WikiError(msg)
    return value


if __name__ == '__main__':
    raise SystemExit(main())
