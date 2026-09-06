# League of Legends skin release statistics

This project will build a local, reproducible dataset for analysing relationships between champion skins, champion performance, and balance changes over time.

The first stage is data collection and normalization. Analysis and visualisation should be built only after the source data can be reproduced reliably.

## Stage 1 scope

Collect the following data for every League of Legends champion:

1. Historical performance measurements from the charts on [League of Graphs](https://www.leagueofgraphs.com/champions/stats), especially:

   - win rate;
   - popularity/pick rate;
   - ban rate;
   - the timestamp associated with every point.

2. Every skin and its release date from the champion's Wiki [Cosmetics page](https://wiki.leagueoflegends.com/en-us/Ashe/Cosmetics).
3. The champion's complete [League of Legends Wiki](https://wiki.leagueoflegends.com/en-us/Ashe/Patch_history) patch history.
4. The release/effective date of every referenced patch and hotfix.
5. One classification of each champion's overall change in a patch as a `buff`, `nerf`, `change`, or `rework`.

## Proposed pipeline

```text
source champion lists
        │
        ▼
canonical champion registry + source-specific aliases/URLs
        │
        ├── League of Graphs charts ──► timestamped performance points ─┐
        ├── Wiki SkinData ────────────► skins + release dates ──────────┤
        └── Wiki patch histories ─────► patch entries + patch dates     │
                                                    │                   │
                                                    ▼                   │
                         champion-patch LiteLLM classification ────┤
                                                                        ▼
                                                                 local SQLite DB
```

### 1. Discover champions from the sources

Do not construct URLs by lower-casing champion names. Discover and persist the links published by each source instead:

- Wiki: [List of champions](https://wiki.leagueoflegends.com/en-us/List_of_champions)
- League of Graphs: [Win rate by experience](https://www.leagueofgraphs.com/champions/winrates-by-xp) or the champion selector on a stats page

This handles source-specific identifiers and renames. For example, the Wiki uses `Wukong`, while League of Graphs uses the `monkeyking` slug. Other names contain spaces, apostrophes, ampersands, abbreviations, or shortened slugs.

The database should keep a stable internal champion ID alongside the display name and every source-specific identifier. Newly released or renamed champions can then be added without changing historical rows.

### 2. Extract performance histories

League of Graphs' champion stats pages render the historical charts from JavaScript arrays of `[Unix timestamp in milliseconds, value]` pairs. Separate arrays exist for popularity, win rate, and ban rate. The data is therefore more precise than reading pixels or chart tooltips.

Each series must retain the champion identity and source provenance, including the source URL and saved HTML file.
Role, rank bracket, region, queue, and game-mode filters are intentionally not normalized or stored. The timestamps
are observations rather than patch identifiers, so they should be stored as-is and joined to the patch calendar later.

#### Access constraint

[League of Graphs' Terms of Use](https://www.leagueofgraphs.com/terms-of-use) prohibit automated queries and scraping without express written permission. Direct non-browser requests are also currently protected by a Cloudflare challenge. Although archived HTML confirms that the chart arrays are technically extractable, an automated collector should **not** be implemented or run until either:

- League of Graphs grants written permission; or
- a licensed/authorized replacement source is selected.

If permission is obtained, the extractor should use conservative request rates, caching, an identifiable user agent, retries with backoff, and raw-response hashes. It should not attempt to bypass CAPTCHAs or other access controls.

A Selenium-controlled browser is a technically suitable implementation after permission is obtained: it can load the normal champion stats page and read the embedded JavaScript arrays from the DOM. It should use a single browser/session, a single worker, long jittered delays, persistent caching, and resumable checkpoints. Slow browser automation is still an automated query under the current Terms, however, so human-like timing alone does not remove the permission requirement.

### 3. Extract skins and release dates

The rendered `<Champion>/Cosmetics` pages contain one card per base skin with its internal name, display name, price, release date, availability group, and Wiki file link for the splash image. Chromas are nested under their base skin and should not be counted as independent skin releases.

The cleaner source is the Wiki's [`Module:SkinData/data`](https://wiki.leagueoflegends.com/en-us/Module:SkinData/data), which generates those pages. It provides all champions in one structured Lua data table, including:

- champion and skin IDs;
- internal and formatted skin names;
- ISO-formatted release dates;
- availability, price, loot eligibility, and retirement dates;
- skin sets and optional feature flags;
- chroma IDs and availability metadata;
- optional lore, artists, voice actors, and related metadata.

Fetch and archive this module through the MediaWiki API, then parse it in Python. The rendered Cosmetics page can be used as a validation/fallback source and to resolve its published splash-art file link. Include the `Original` skin but mark it explicitly as the champion's base skin so analyses can include or exclude it deliberately.

The initial dataset needs skin metadata and the source Cosmetics-page URL. If local image files are useful later, resolve and download the canonical Wiki file separately and store its content hash and local path; the images themselves are not required for release-date analysis.

### 4. Extract patch histories and dates

The Wiki is backed by MediaWiki and exposes parsed HTML and wikitext through its API. The intended inputs are:

- `List_of_champions` for canonical Wiki champion pages;
- `<Champion>/Patch_history` for the expanded per-champion history;
- each referenced patch page, such as `V13.12`, for its `Release Date (US)` value.

A patch history is already structured as patch heading → ability/stat context → individual change entries. Store each leaf change separately while retaining its parent context and source order. Hotfix labels such as `V12.5 - March 9th Hotfix` need their own effective date in addition to the base patch release date.

Raw source text should be retained. The parser can then be improved without refetching the source, and every normalized entry remains auditable.

### 5. Classify champion-patch balance changes

Send all normalized patch notes for one champion and patch—not a whole champion history—to the configured model through LiteLLM and request a strict structured response with one label:

- `buff`: increases the champion's power or usability;
- `nerf`: decreases the champion's power or usability;
- `change`: neutral, mixed, mechanical, cosmetic, or bug-fix-only change;
- `rework`: belongs to an explicit, broad champion gameplay overhaul or relaunch that substantially replaces mechanics across the kit.

Use `rework` conservatively. A large ordinary balance patch, initial champion release, visual update, or smaller mid-scope adjustment is not automatically a rework. The classifier receives every patch heading, hotfix, ability/stat context, and individual change for the champion-patch, then judges their aggregate effect. Material buffs and nerfs with no clear net direction are classified as `change`.

Keep classification output separate from the extracted fact. The shared database only needs the label, worded confidence, and classification time. LiteLLM connection details, model identifiers, prompts, request metadata, and raw responses remain process-only and are not persisted.

Classification runs through 12 worker processes by default and displays a `tqdm` progress bar. Workers perform only
the LiteLLM requests; the parent process serializes SQLite writes so interrupted runs remain safely resumable.

## Planned SQLite model

The normalized database will live locally, for example at `data/lol_skin_release_statistics.sqlite3`.

| Table | Purpose |
| --- | --- |
| `champions` | Stable champion identity and canonical display name |
| `champion_source_ids` | Wiki titles/URLs, League of Graphs slugs/URLs, and aliases |
| `source_documents` | Retrieval metadata, content hash, and raw source payload |
| `graph_series` | Champion metric plus the saved HTML file, canonical source URL, modification time, and content hash |
| `graph_points` | Timestamp/value observations belonging to a graph series |
| `patches` | Patch identifier, release date, and source URL |
| `patch_events` | Champion-specific patch/hotfix headings, effective dates, and source order |
| `patch_entries` | Champion, patch, context, individual change text, and source order |
| `patch_classifications` | Shareable classification label, worded confidence, and timestamp for each champion-patch |
| `skins` | Skin ID/name, base-skin flag, release/retirement dates, availability, price, and source URLs |
| `skin_chromas` | Chroma IDs and availability, linked to their base skin |

Important constraints should prevent duplicate source identifiers, duplicate points within a series, and duplicate patch entries from repeated imports. Imports should be transactional and idempotent.

The implemented tables and relationships are shown in the [database ERD](docs/db_data_model.md).

## Extraction approach

The extraction code will be written in Python. A small HTTP client plus an HTML parser is sufficient for the Wiki; SQLite support is included in Python's standard library. Network and parser dependencies should be kept minimal and pinned once implementation begins.

The collectors should:

- identify themselves with a descriptive user agent;
- respect source terms and access controls;
- rate-limit and cache requests;
- retry transient failures with exponential backoff;
- validate expected page structure before writing rows;
- record retrieval times, source URLs, and content hashes;
- preserve raw data and normalized data separately;
- upsert deterministically so reruns do not create duplicates.

## Investigation status

- [x] Confirmed that the Wiki champion list can provide canonical champion URLs.
- [x] Confirmed that expanded Wiki patch histories and patch release dates are available through the MediaWiki API.
- [x] Confirmed that Wiki skin metadata and ISO release dates are available from `Module:SkinData/data`.
- [x] Confirmed that rendered Cosmetics pages provide skin cards and splash-art file links.
- [x] Confirmed that League of Graphs chart values are embedded as timestamp/value arrays rather than only rendered pixels.
- [x] Confirmed the `Wukong` ↔ `monkeyking` naming inconsistency and the need for source-specific mappings.
- [ ] Obtain League of Graphs scraping permission or choose an authorized historical-statistics source.
- [x] Implement the Python Wiki collector and SQLite schema (see [data collection instructions](docs/data-collection.md)).
- [x] Implement the resumable LiteLLM classification step (see [data collection instructions](docs/data-collection.md)).
- [x] Implement the offline importer for manually saved League of Graphs HTML pages.
- [ ] Evaluate classification quality and refine the prompt using a reviewed sample.
- [ ] Build downstream skin-release analyses.
