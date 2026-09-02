"""Parser for manually saved League of Graphs champion statistics pages."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Mapping, Sequence
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag

if TYPE_CHECKING:
    from pathlib import Path

_CHAMPION_CLASS = re.compile(r'^champion-(?P<game_id>\d+)-\d+$')
_GRAPH_DATA = re.compile(r'\bdata\s*:\s*(?P<data>\[\s*\[.*?\]\s*\])\s*,\s*lines\s*:', re.DOTALL)
_POINT_FIELD_COUNT = 2
_METRIC_BOXES = {
    'popularity': 'popularityHistoryBox',
    'win_rate': 'winrateHistoryBox',
    'ban_rate': 'banrateHistoryBox',
}


class LeagueOfGraphsParseError(ValueError):
    """Raised when a saved page does not contain the expected statistics."""


@dataclass(frozen=True)
class GraphPoint:
    """One timestamped percentage observation."""

    observed_at_ms: int
    value: float


@dataclass(frozen=True)
class GraphSeries:
    """One historical metric extracted from a saved page."""

    metric: str
    points: tuple[GraphPoint, ...]


@dataclass(frozen=True)
class LeagueOfGraphsPage:
    """Champion identity, provenance, and histories from one saved page."""

    champion_name: str
    game_id: int
    slug: str
    source_url: str
    source_file: str
    file_modified_at: str
    content_hash: str
    series: tuple[GraphSeries, ...]


def parse_leagueofgraphs_page(path: Path) -> LeagueOfGraphsPage:
    """Parse one browser-saved champion statistics HTML file."""
    raw = path.read_bytes()
    soup = BeautifulSoup(raw, 'html.parser')
    page_status = _page_status(soup)
    champion_name, game_id = _champion_identity(soup)
    slug = _champion_slug(page_status)
    source_url = _source_url(soup, slug)
    series = tuple(_parse_series(soup, metric, box_class) for metric, box_class in _METRIC_BOXES.items())
    modified_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).replace(microsecond=0).isoformat()
    return LeagueOfGraphsPage(
        champion_name=champion_name,
        game_id=game_id,
        slug=slug,
        source_url=source_url,
        source_file=path.as_posix(),
        file_modified_at=modified_at,
        content_hash=sha256(raw).hexdigest(),
        series=series,
    )


def _page_status(soup: BeautifulSoup) -> Mapping[str, Any]:
    for script in soup.find_all('script'):
        text = script.string or script.get_text()
        if '"basePageStatus"' not in text:
            continue
        try:
            value = json.loads(text.strip())
        except json.JSONDecodeError:
            continue
        if isinstance(value, Mapping) and isinstance(value.get('basePageStatus'), Mapping):
            return value['basePageStatus']
    msg = 'League of Graphs page configuration was not found.'
    raise LeagueOfGraphsParseError(msg)


def _champion_identity(soup: BeautifulSoup) -> tuple[str, int]:
    image = soup.select_one('#championsFilter .filterHeader img')
    if not isinstance(image, Tag):
        msg = 'Selected champion information was not found.'
        raise LeagueOfGraphsParseError(msg)
    champion_name = str(image.get('alt', '')).strip()
    classes = image.get('class', ())
    game_id = None
    if isinstance(classes, Sequence) and not isinstance(classes, str):
        for class_name in classes:
            match = _CHAMPION_CLASS.fullmatch(str(class_name))
            if match:
                game_id = int(match.group('game_id'))
                break
    if not champion_name or game_id is None:
        msg = 'Selected champion name or Riot ID was not found.'
        raise LeagueOfGraphsParseError(msg)
    return champion_name, game_id


def _champion_slug(page_status: Mapping[str, Any]) -> str:
    value = page_status.get('champions')
    if not isinstance(value, str) or not value.strip() or value == 'all':
        msg = 'Selected League of Graphs champion slug was not found.'
        raise LeagueOfGraphsParseError(msg)
    return value.strip()


def _source_url(soup: BeautifulSoup, slug: str) -> str:
    canonical = soup.find('link', rel='canonical')
    href = canonical.get('href') if isinstance(canonical, Tag) else None
    if isinstance(href, str):
        parsed = urlsplit(href.strip())
        if parsed.scheme in {'http', 'https'} and parsed.netloc:
            return href.strip()
    return f'https://www.leagueofgraphs.com/champions/stats/{slug}'


def _parse_series(soup: BeautifulSoup, metric: str, box_class: str) -> GraphSeries:
    box = soup.select_one(f'.{box_class}')
    if not isinstance(box, Tag):
        msg = f'Missing {metric} history box.'
        raise LeagueOfGraphsParseError(msg)
    for script in box.find_all('script'):
        match = _GRAPH_DATA.search(script.string or script.get_text())
        if match:
            return GraphSeries(metric=metric, points=_parse_points(match.group('data'), metric))
    msg = f'Missing {metric} history data.'
    raise LeagueOfGraphsParseError(msg)


def _parse_points(raw: str, metric: str) -> tuple[GraphPoint, ...]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        msg = f'Invalid {metric} history data.'
        raise LeagueOfGraphsParseError(msg) from error
    if not isinstance(value, list) or not value:
        msg = f'Empty {metric} history data.'
        raise LeagueOfGraphsParseError(msg)
    points: list[GraphPoint] = []
    for item in value:
        if not isinstance(item, list) or len(item) != _POINT_FIELD_COUNT:
            msg = f'Invalid point in {metric} history.'
            raise LeagueOfGraphsParseError(msg)
        timestamp, observation = item
        if isinstance(timestamp, bool) or not isinstance(timestamp, int):
            msg = f'Invalid timestamp in {metric} history.'
            raise LeagueOfGraphsParseError(msg)
        if isinstance(observation, bool) or not isinstance(observation, (int, float)):
            msg = f'Invalid value in {metric} history.'
            raise LeagueOfGraphsParseError(msg)
        points.append(GraphPoint(timestamp, float(observation)))
    timestamps = [point.observed_at_ms for point in points]
    if timestamps != sorted(set(timestamps)):
        msg = f'Timestamps are duplicated or out of order in {metric} history.'
        raise LeagueOfGraphsParseError(msg)
    return tuple(points)
