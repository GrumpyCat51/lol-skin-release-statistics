"""SQLite persistence for the normalized League data."""

from __future__ import annotations

import gzip
import json
import sqlite3
from contextlib import contextmanager
from datetime import date
from typing import TYPE_CHECKING, Iterator, Mapping, Sequence
from urllib.parse import quote

from typing_extensions import Self

from lol_skin_release_statistics.classification import (
    ClassificationCandidate,
    ClassificationResult,
    PatchClassificationEntry,
    PatchClassificationEvent,
)
from lol_skin_release_statistics.wiki import Champion, PatchEvent, Skin, parse_hotfix_date, wiki_page_url

if TYPE_CHECKING:
    from pathlib import Path

    from lol_skin_release_statistics.leagueofgraphs import LeagueOfGraphsPage

SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = DELETE;

CREATE TABLE IF NOT EXISTS source_documents (
    id INTEGER PRIMARY KEY,
    source_type TEXT NOT NULL,
    source_key TEXT NOT NULL,
    source_url TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    content_encoding TEXT NOT NULL DEFAULT 'gzip',
    content BLOB NOT NULL,
    UNIQUE (source_type, source_key)
);

CREATE TABLE IF NOT EXISTS champions (
    id INTEGER PRIMARY KEY,
    game_id INTEGER NOT NULL UNIQUE,
    name TEXT NOT NULL UNIQUE,
    wiki_title TEXT NOT NULL UNIQUE,
    wiki_data_name TEXT NOT NULL UNIQUE,
    wiki_url TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS champion_source_ids (
    id INTEGER PRIMARY KEY,
    champion_id INTEGER NOT NULL REFERENCES champions(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_url TEXT NOT NULL,
    UNIQUE (source, source_id),
    UNIQUE (champion_id, source)
);

CREATE TABLE IF NOT EXISTS graph_series (
    id INTEGER PRIMARY KEY,
    champion_id INTEGER NOT NULL REFERENCES champions(id) ON DELETE CASCADE,
    metric TEXT NOT NULL CHECK (metric IN ('popularity', 'win_rate', 'ban_rate')),
    source_url TEXT NOT NULL,
    source_file TEXT NOT NULL,
    file_modified_at TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    UNIQUE (champion_id, metric)
);

CREATE TABLE IF NOT EXISTS graph_points (
    id INTEGER PRIMARY KEY,
    graph_series_id INTEGER NOT NULL REFERENCES graph_series(id) ON DELETE CASCADE,
    observed_at_ms INTEGER NOT NULL,
    value REAL NOT NULL,
    UNIQUE (graph_series_id, observed_at_ms)
);

CREATE TABLE IF NOT EXISTS skins (
    id INTEGER PRIMARY KEY,
    champion_id INTEGER NOT NULL REFERENCES champions(id) ON DELETE CASCADE,
    wiki_skin_id INTEGER,
    internal_name TEXT NOT NULL,
    display_name TEXT NOT NULL,
    is_base INTEGER NOT NULL CHECK (is_base IN (0, 1)),
    release_date TEXT,
    release_date_raw TEXT NOT NULL,
    retired_date TEXT,
    availability TEXT,
    cost INTEGER,
    loot_eligible INTEGER CHECK (loot_eligible IN (0, 1) OR loot_eligible IS NULL),
    sets_json TEXT NOT NULL,
    source_url TEXT NOT NULL,
    cosmetics_url TEXT NOT NULL,
    UNIQUE (champion_id, internal_name),
    UNIQUE (champion_id, wiki_skin_id)
);

CREATE TABLE IF NOT EXISTS skin_chromas (
    id INTEGER PRIMARY KEY,
    skin_id INTEGER NOT NULL REFERENCES skins(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    wiki_chroma_id INTEGER,
    availability TEXT,
    source TEXT,
    distribution TEXT,
    UNIQUE (skin_id, name)
);

CREATE TABLE IF NOT EXISTS patches (
    patch_id TEXT PRIMARY KEY,
    wiki_title TEXT,
    wiki_url TEXT,
    release_date TEXT
);

CREATE TABLE IF NOT EXISTS patch_events (
    id INTEGER PRIMARY KEY,
    champion_id INTEGER NOT NULL REFERENCES champions(id) ON DELETE CASCADE,
    patch_id TEXT NOT NULL REFERENCES patches(patch_id),
    heading TEXT NOT NULL,
    effective_date TEXT,
    source_order INTEGER NOT NULL,
    source_url TEXT,
    UNIQUE (champion_id, source_order)
);

CREATE TABLE IF NOT EXISTS patch_entries (
    id INTEGER PRIMARY KEY,
    patch_event_id INTEGER NOT NULL REFERENCES patch_events(id) ON DELETE CASCADE,
    context TEXT NOT NULL,
    change_text TEXT NOT NULL,
    source_order INTEGER NOT NULL,
    UNIQUE (patch_event_id, source_order)
);

CREATE TABLE IF NOT EXISTS patch_classifications (
    id INTEGER PRIMARY KEY,
    champion_id INTEGER NOT NULL REFERENCES champions(id) ON DELETE CASCADE,
    patch_id TEXT NOT NULL REFERENCES patches(patch_id),
    label TEXT NOT NULL CHECK (label IN ('buff', 'nerf', 'change', 'rework')),
    confidence TEXT NOT NULL CHECK (
        confidence IN ('VERY_UNCERTAIN', 'UNCERTAIN', 'AMBIGUOUS', 'CERTAIN', 'VERY_CERTAIN')
    ),
    classified_at TEXT NOT NULL,
    UNIQUE (champion_id, patch_id)
);

CREATE INDEX IF NOT EXISTS patch_events_patch_id_idx ON patch_events(patch_id);
CREATE INDEX IF NOT EXISTS skins_release_date_idx ON skins(release_date);
CREATE INDEX IF NOT EXISTS skin_chromas_wiki_id_idx ON skin_chromas(skin_id, wiki_chroma_id);
CREATE INDEX IF NOT EXISTS patch_classifications_label_idx ON patch_classifications(label);
CREATE INDEX IF NOT EXISTS graph_points_observed_at_idx ON graph_points(observed_at_ms);
"""


class Database:
    """Repository for the local SQLite collection database."""

    def __init__(self, path: Path) -> None:
        """Open a database and create its schema when necessary."""
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('PRAGMA foreign_keys = ON')
        self.connection.executescript(SCHEMA)
        self._migrate_legacy_classifications()

    def close(self) -> None:
        """Close the SQLite connection."""
        self.connection.close()

    def __enter__(self) -> Self:
        """Return this repository as a context manager."""
        return self

    def __exit__(self, *_args: object) -> None:
        """Close the repository when leaving its context."""
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Commit a unit of work or roll it back on error."""
        try:
            yield
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def save_source(
        self,
        source_type: str,
        source_key: str,
        source_url: str,
        retrieved_at: str,
        content_hash: str,
        raw: bytes,
    ) -> None:
        """Store a compressed raw API response and its provenance."""
        content = gzip.compress(raw, compresslevel=9)
        self.connection.execute(
            """
            INSERT INTO source_documents (
                source_type, source_key, source_url, retrieved_at, content_hash, content_encoding, content
            ) VALUES (?, ?, ?, ?, ?, 'gzip', ?)
            ON CONFLICT (source_type, source_key) DO UPDATE SET
                source_url = excluded.source_url,
                retrieved_at = excluded.retrieved_at,
                content_hash = excluded.content_hash,
                content_encoding = excluded.content_encoding,
                content = excluded.content
            """,
            (source_type, source_key, source_url, retrieved_at, content_hash, content),
        )
        self.connection.commit()

    def load_source(self, source_type: str, source_key: str) -> Mapping[str, object] | None:
        """Load and decode a cached API response, if present."""
        row = self.connection.execute(
            'SELECT content, content_encoding FROM source_documents WHERE source_type = ? AND source_key = ?',
            (source_type, source_key),
        ).fetchone()
        if row is None:
            return None
        raw = gzip.decompress(row['content']) if row['content_encoding'] == 'gzip' else row['content']
        value = json.loads(raw)
        if not isinstance(value, Mapping):
            msg = f'Cached source {source_type}/{source_key} is not a JSON object.'
            raise TypeError(msg)
        return value

    def replace_champions(self, champions: Sequence[Champion]) -> None:
        """Upsert the canonical Wiki champion registry."""
        with self.transaction():
            for champion in champions:
                self.connection.execute(
                    """
                    INSERT INTO champions (game_id, name, wiki_title, wiki_data_name, wiki_url)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT (game_id) DO UPDATE SET
                        name = excluded.name,
                        wiki_title = excluded.wiki_title,
                        wiki_data_name = excluded.wiki_data_name,
                        wiki_url = excluded.wiki_url
                    """,
                    (
                        champion.game_id,
                        champion.name,
                        champion.wiki_title,
                        champion.wiki_data_name,
                        champion.wiki_url,
                    ),
                )
                champion_id = self._champion_id(champion.name)
                self.connection.executemany(
                    """
                    INSERT INTO champion_source_ids (champion_id, source, source_id, source_url)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT (champion_id, source) DO UPDATE SET
                        source_id = excluded.source_id,
                        source_url = excluded.source_url
                    """,
                    (
                        (champion_id, 'league_wiki', champion.wiki_title, champion.wiki_url),
                        (
                            champion_id,
                            'league_wiki_data',
                            champion.wiki_data_name,
                            f'https://wiki.leagueoflegends.com/en-us/{quote(champion.wiki_data_name)}',
                        ),
                    ),
                )

    def champion_names(self) -> list[str]:
        """Return champion names in Wiki numeric-ID order."""
        rows = self.connection.execute('SELECT name FROM champions ORDER BY game_id').fetchall()
        return [str(row['name']) for row in rows]

    def champion_data_name(self, name: str) -> str:
        """Resolve a canonical champion name to its Wiki template/subpage key."""
        row = self.connection.execute('SELECT wiki_data_name FROM champions WHERE name = ?', (name,)).fetchone()
        if row is None:
            msg = f'Unknown champion: {name}'
            raise KeyError(msg)
        return str(row['wiki_data_name'])

    def replace_graph_pages(self, pages: Sequence[LeagueOfGraphsPage]) -> None:
        """Replace all supplied champion histories in one transaction."""
        seen_game_ids: set[int] = set()
        with self.transaction():
            for page in pages:
                if page.game_id in seen_game_ids:
                    msg = f'Multiple HTML files contain Riot champion ID {page.game_id}.'
                    raise ValueError(msg)
                seen_game_ids.add(page.game_id)
                champion_id = self._champion_id_for_graph_page(page)
                self.connection.execute(
                    """
                    INSERT INTO champion_source_ids (champion_id, source, source_id, source_url)
                    VALUES (?, 'leagueofgraphs', ?, ?)
                    ON CONFLICT (champion_id, source) DO UPDATE SET
                        source_id = excluded.source_id,
                        source_url = excluded.source_url
                    """,
                    (champion_id, page.slug, page.source_url),
                )
                for series in page.series:
                    self.connection.execute(
                        """
                        INSERT INTO graph_series (
                            champion_id, metric, source_url, source_file, file_modified_at, content_hash
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT (champion_id, metric) DO UPDATE SET
                            source_url = excluded.source_url,
                            source_file = excluded.source_file,
                            file_modified_at = excluded.file_modified_at,
                            content_hash = excluded.content_hash
                        """,
                        (
                            champion_id,
                            series.metric,
                            page.source_url,
                            page.source_file,
                            page.file_modified_at,
                            page.content_hash,
                        ),
                    )
                    series_id = self._graph_series_id(champion_id, series.metric)
                    self.connection.execute('DELETE FROM graph_points WHERE graph_series_id = ?', (series_id,))
                    self.connection.executemany(
                        """
                        INSERT INTO graph_points (graph_series_id, observed_at_ms, value)
                        VALUES (?, ?, ?)
                        """,
                        ((series_id, point.observed_at_ms, point.value) for point in series.points),
                    )

    def replace_skins(self, skins: Sequence[Skin]) -> None:
        """Replace all skin and chroma rows with one SkinData snapshot."""
        champion_ids = self._champion_ids()
        champion_data_names = self._champion_data_names()
        missing = sorted({skin.champion_name for skin in skins} - champion_ids.keys())
        if missing:
            msg = f'SkinData contains champions absent from the champion registry: {missing}'
            raise ValueError(msg)
        with self.transaction():
            self.connection.execute('DELETE FROM skins')
            for skin in skins:
                cursor = self.connection.execute(
                    """
                    INSERT INTO skins (
                        champion_id, wiki_skin_id, internal_name, display_name, is_base, release_date,
                        release_date_raw, retired_date, availability, cost, loot_eligible, sets_json,
                        source_url, cosmetics_url
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        champion_ids[skin.champion_name],
                        skin.skin_id,
                        skin.internal_name,
                        skin.display_name,
                        int(skin.is_base),
                        skin.release_date.isoformat() if skin.release_date else None,
                        skin.release_date_raw,
                        skin.retired_date.isoformat() if skin.retired_date else None,
                        skin.availability,
                        skin.cost,
                        int(skin.loot_eligible) if skin.loot_eligible is not None else None,
                        json.dumps(skin.sets, ensure_ascii=False),
                        wiki_page_url('Module:SkinData/data'),
                        wiki_page_url(f'{champion_data_names[skin.champion_name]}/Cosmetics'),
                    ),
                )
                skin_db_id = int(cursor.lastrowid)
                for chroma in skin.chromas:
                    self.connection.execute(
                        """
                        INSERT INTO skin_chromas (
                            skin_id, name, wiki_chroma_id, availability, source, distribution
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            skin_db_id,
                            chroma.name,
                            chroma.chroma_id,
                            chroma.availability,
                            chroma.source,
                            chroma.distribution,
                        ),
                    )

    def replace_patch_history(
        self,
        champion_name: str,
        events: Sequence[PatchEvent],
        history_url: str,
    ) -> None:
        """Replace one champion's parsed patch history."""
        champion_id = self._champion_id(champion_name)
        if self._patch_history_matches(champion_id, events):
            return
        with self.transaction():
            # A champion history refresh can revise any patch's notes. Existing aggregate labels
            # are no longer auditable against those notes, so they must be regenerated.
            self.connection.execute('DELETE FROM patch_classifications WHERE champion_id = ?', (champion_id,))
            self.connection.execute('DELETE FROM patch_events WHERE champion_id = ?', (champion_id,))
            for event in events:
                self.connection.execute(
                    """
                    INSERT INTO patches (patch_id, wiki_title, wiki_url)
                    VALUES (?, ?, ?)
                    ON CONFLICT (patch_id) DO UPDATE SET
                        wiki_title = excluded.wiki_title,
                        wiki_url = excluded.wiki_url
                    """,
                    (event.patch_id, event.wiki_title, event.wiki_url),
                )
                cursor = self.connection.execute(
                    """
                    INSERT INTO patch_events (
                        champion_id, patch_id, heading, source_order, source_url
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        champion_id,
                        event.patch_id,
                        event.heading,
                        event.source_order,
                        event.wiki_url or history_url,
                    ),
                )
                event_id = int(cursor.lastrowid)
                self.connection.executemany(
                    """
                    INSERT INTO patch_entries (patch_event_id, context, change_text, source_order)
                    VALUES (?, ?, ?, ?)
                    """,
                    ((event_id, entry.context, entry.change_text, entry.source_order) for entry in event.entries),
                )

    def patch_pages(self, refresh: bool) -> list[tuple[str, str]]:
        """Return referenced patch pages, optionally including dates already known."""
        conditions = ['wiki_title IS NOT NULL']
        if not refresh:
            conditions.append('release_date IS NULL')
        where_clause = f'WHERE {" AND ".join(conditions)}'
        rows = self.connection.execute(
            f'SELECT patch_id, wiki_title FROM patches {where_clause} ORDER BY patch_id',  # noqa: S608
        ).fetchall()
        return [(str(row['patch_id']), str(row['wiki_title'])) for row in rows]

    def set_patch_release_date(self, patch_id: str, release_date: date | None) -> None:
        """Update a patch release date, including an explicit unknown value."""
        value = release_date.isoformat() if release_date else None
        self.connection.execute('UPDATE patches SET release_date = ? WHERE patch_id = ?', (value, patch_id))
        self.connection.commit()

    def update_event_effective_dates(self) -> None:
        """Set patch-event dates from patch releases and any hotfix headings."""
        rows = self.connection.execute(
            """
            SELECT patch_events.id, patch_events.heading, patches.release_date
            FROM patch_events
            JOIN patches USING (patch_id)
            """,
        ).fetchall()
        with self.transaction():
            for row in rows:
                release = date.fromisoformat(row['release_date']) if row['release_date'] else None
                effective = parse_hotfix_date(str(row['heading']), release)
                self.connection.execute(
                    'UPDATE patch_events SET effective_date = ? WHERE id = ?',
                    (effective.isoformat() if effective else None, row['id']),
                )

    def classification_candidates(
        self,
        champion_names: Sequence[str] | None = None,
        patch_ids: Sequence[str] | None = None,
        limit: int | None = None,
        force: bool = False,
    ) -> list[ClassificationCandidate]:
        """Return filtered champion-patch notes that still require classification."""
        conditions: list[str] = []
        parameters: list[object] = []
        if not force:
            conditions.append(
                """
                NOT EXISTS (
                    SELECT 1 FROM patch_classifications classification
                    WHERE classification.champion_id = patch_events.champion_id
                      AND classification.patch_id = patch_events.patch_id
                )
                """,
            )
        if champion_names:
            placeholders = ','.join('?' for _name in champion_names)
            conditions.append(f'champions.name IN ({placeholders})')
            parameters.extend(champion_names)
        if patch_ids:
            placeholders = ','.join('?' for _patch_id in patch_ids)
            conditions.append(f'patches.patch_id IN ({placeholders})')
            parameters.extend(patch_ids)
        where_clause = f'WHERE {" AND ".join(conditions)}' if conditions else ''
        query = f"""
            SELECT
                champions.id AS champion_id,
                champions.name AS champion_name,
                patches.patch_id,
                patch_events.id AS event_id,
                patch_events.heading AS patch_heading,
                patches.release_date AS patch_release_date,
                patch_events.effective_date,
                patch_entries.context,
                patch_entries.change_text
            FROM patch_entries
            JOIN patch_events ON patch_events.id = patch_entries.patch_event_id
            JOIN patches ON patches.patch_id = patch_events.patch_id
            JOIN champions ON champions.id = patch_events.champion_id
            {where_clause}
            ORDER BY champions.name, patches.patch_id, patch_events.source_order, patch_entries.source_order
            """  # noqa: S608 - fragments are internally generated; values remain bound parameters.
        rows = self.connection.execute(query, parameters).fetchall()
        candidates = self._patch_classification_candidates(rows)
        return candidates[:limit] if limit is not None else candidates

    def save_patch_classification(
        self,
        champion_id: int,
        patch_id: str,
        result: ClassificationResult,
    ) -> None:
        """Insert or replace one champion-patch classification and commit it immediately."""
        self.connection.execute(
            """
            INSERT INTO patch_classifications (
                champion_id, patch_id, label, confidence, classified_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (champion_id, patch_id) DO UPDATE SET
                label = excluded.label,
                confidence = excluded.confidence,
                classified_at = excluded.classified_at
            """,
            (
                champion_id,
                patch_id,
                result.label,
                result.confidence,
                result.classified_at,
            ),
        )
        self.connection.commit()

    def classification_counts(self) -> dict[str, int]:
        """Count all stored champion-patch classification labels."""
        rows = self.connection.execute(
            """
            SELECT label, COUNT(*) AS count
            FROM patch_classifications
            GROUP BY label
            """,
        ).fetchall()
        return {str(row['label']): int(row['count']) for row in rows}

    def counts(self) -> dict[str, int]:
        """Return high-level row counts for collection reporting."""
        tables = (
            'champions',
            'graph_series',
            'graph_points',
            'skins',
            'skin_chromas',
            'patches',
            'patch_events',
            'patch_entries',
            'patch_classifications',
        )
        return {
            table: int(self.connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0])  # noqa: S608
            for table in tables
        }

    def _champion_id(self, name: str) -> int:
        row = self.connection.execute('SELECT id FROM champions WHERE name = ?', (name,)).fetchone()
        if row is None:
            msg = f'Unknown champion: {name}'
            raise KeyError(msg)
        return int(row['id'])

    def _champion_id_for_graph_page(self, page: LeagueOfGraphsPage) -> int:
        row = self.connection.execute('SELECT id FROM champions WHERE game_id = ?', (page.game_id,)).fetchone()
        if row is None:
            msg = (
                f'Unknown Riot champion ID in {page.source_file}: '
                f'{page.champion_name} ({page.game_id}); refresh the Wiki data before importing'
            )
            raise KeyError(msg)
        return int(row['id'])

    def _graph_series_id(self, champion_id: int, metric: str) -> int:
        row = self.connection.execute(
            'SELECT id FROM graph_series WHERE champion_id = ? AND metric = ?',
            (champion_id, metric),
        ).fetchone()
        if row is None:
            msg = f'Graph series was not stored: champion={champion_id}, metric={metric}'
            raise sqlite3.DatabaseError(msg)
        return int(row['id'])

    def _champion_ids(self) -> dict[str, int]:
        rows = self.connection.execute('SELECT id, name FROM champions').fetchall()
        return {str(row['name']): int(row['id']) for row in rows}

    def _champion_data_names(self) -> dict[str, str]:
        rows = self.connection.execute('SELECT name, wiki_data_name FROM champions').fetchall()
        return {str(row['name']): str(row['wiki_data_name']) for row in rows}

    def _patch_classification_candidates(self, rows: Sequence[sqlite3.Row]) -> list[ClassificationCandidate]:
        """Group ordered patch-entry rows into one model request per champion-patch."""
        grouped: dict[tuple[int, str], dict[str, object]] = {}
        for row in rows:
            key = (int(row['champion_id']), str(row['patch_id']))
            candidate = grouped.setdefault(
                key,
                {
                    'champion_name': str(row['champion_name']),
                    'patch_release_date': _optional_row_string(row['patch_release_date']),
                    'events': [],
                },
            )
            events = candidate['events']
            assert isinstance(events, list)
            heading = str(row['patch_heading'])
            effective_date = _optional_row_string(row['effective_date'])
            event_id = int(row['event_id'])
            if not events or events[-1]['id'] != event_id:
                events.append({'id': event_id, 'heading': heading, 'effective_date': effective_date, 'entries': []})
            entries = events[-1]['entries']
            assert isinstance(entries, list)
            entries.append(
                PatchClassificationEntry(context=str(row['context']), change_text=str(row['change_text'])),
            )

        candidates: list[ClassificationCandidate] = []
        for (champion_id, patch_id), candidate in grouped.items():
            event_data = candidate['events']
            assert isinstance(event_data, list)
            events = tuple(
                PatchClassificationEvent(
                    heading=str(event['heading']),
                    effective_date=event['effective_date'],
                    entries=tuple(event['entries']),
                )
                for event in event_data
            )
            candidates.append(
                ClassificationCandidate(
                    champion_id=champion_id,
                    champion_name=str(candidate['champion_name']),
                    patch_id=patch_id,
                    patch_release_date=candidate['patch_release_date'],
                    events=events,
                ),
            )
        return candidates

    def _migrate_legacy_classifications(self) -> None:
        """Normalize the old entry-level table's confidence values when it exists."""
        columns = self.connection.execute('PRAGMA table_info(classifications)').fetchall()
        column_names = tuple(str(row['name']) for row in columns)
        if not column_names:
            return
        expected = ('id', 'patch_entry_id', 'label', 'confidence', 'classified_at')
        if column_names == expected:
            return
        required = {'id', 'patch_entry_id', 'label', 'confidence', 'classified_at'}
        if not required.issubset(column_names):
            msg = f'Unsupported classifications schema: {column_names}'
            raise sqlite3.DatabaseError(msg)
        with self.transaction():
            self.connection.execute('ALTER TABLE classifications RENAME TO classifications_legacy')
            self.connection.execute(
                """
                CREATE TABLE classifications (
                    id INTEGER PRIMARY KEY,
                    patch_entry_id INTEGER NOT NULL UNIQUE REFERENCES patch_entries(id) ON DELETE CASCADE,
                    label TEXT NOT NULL CHECK (label IN ('buff', 'nerf', 'change', 'rework')),
                    confidence TEXT NOT NULL CHECK (
                        confidence IN ('VERY_UNCERTAIN', 'UNCERTAIN', 'AMBIGUOUS', 'CERTAIN', 'VERY_CERTAIN')
                    ),
                    classified_at TEXT NOT NULL
                )
                """,
            )
            self.connection.execute(
                """
                INSERT INTO classifications (id, patch_entry_id, label, confidence, classified_at)
                SELECT
                    id,
                    patch_entry_id,
                    label,
                    CASE
                        WHEN confidence IN (
                            'VERY_UNCERTAIN', 'UNCERTAIN', 'AMBIGUOUS', 'CERTAIN', 'VERY_CERTAIN'
                        ) THEN confidence
                        WHEN confidence < 0.2 THEN 'VERY_UNCERTAIN'
                        WHEN confidence < 0.4 THEN 'UNCERTAIN'
                        WHEN confidence < 0.6 THEN 'AMBIGUOUS'
                        WHEN confidence < 0.8 THEN 'CERTAIN'
                        ELSE 'VERY_CERTAIN'
                    END,
                    classified_at
                FROM classifications_legacy
                """,
            )
            self.connection.execute('DROP TABLE classifications_legacy')
            self.connection.execute('CREATE INDEX classifications_label_idx ON classifications(label)')

    def _patch_history_matches(self, champion_id: int, events: Sequence[PatchEvent]) -> bool:
        rows = self.connection.execute(
            """
            SELECT
                patch_events.source_order AS event_order,
                patch_events.patch_id,
                patch_events.heading,
                patch_entries.source_order AS entry_order,
                patch_entries.context,
                patch_entries.change_text
            FROM patch_events
            LEFT JOIN patch_entries ON patch_entries.patch_event_id = patch_events.id
            WHERE patch_events.champion_id = ?
            ORDER BY patch_events.source_order, patch_entries.source_order
            """,
            (champion_id,),
        ).fetchall()
        existing = [
            (
                int(row['event_order']),
                str(row['patch_id']),
                str(row['heading']),
                int(row['entry_order']) if row['entry_order'] is not None else -1,
                str(row['context']) if row['context'] is not None else '',
                str(row['change_text']) if row['change_text'] is not None else '',
            )
            for row in rows
        ]
        expected: list[tuple[int, str, str, int, str, str]] = []
        for event in events:
            if event.entries:
                expected.extend(
                    (
                        event.source_order,
                        event.patch_id,
                        event.heading,
                        entry.source_order,
                        entry.context,
                        entry.change_text,
                    )
                    for entry in event.entries
                )
            else:
                expected.append((event.source_order, event.patch_id, event.heading, -1, '', ''))
        return existing == expected


def _optional_row_string(value: object) -> str | None:
    return str(value) if value is not None else None
