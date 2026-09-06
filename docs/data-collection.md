# Data collection

Data collection is a separate, reproducible stage. The collectors write normalized records and compressed raw source responses to the same local SQLite database. The raw responses make it possible to improve a parser without downloading the source again.

## Setup

Run all commands from the repository root. Python 3.8–3.13 is supported.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

On Windows PowerShell, activate the environment with `.venv\Scripts\Activate.ps1` instead.

If `uv` is installed in the environment, the editable install can instead be created with:

```bash
uv pip install -e .
```

When running from WSL with the repository on `/mnt/c`, close Windows-native SQLite viewers and editor database
extensions before running a collector or classifier. SQLite cannot safely coordinate a writer across WSL and a
Windows process that keeps the database's journal files open. The application uses SQLite's rollback journal for
compatibility with this setup.

## Clean, complete rebuild

Run these steps sequentially from the repository root to build a new database. The reset command permanently removes
the current database and its SQLite journal sidecars; do not run it if you need to retain the current data.

```bash
rm -f \
  data/lol_skin_release_statistics.sqlite3 \
  data/lol_skin_release_statistics.sqlite3-journal \
  data/lol_skin_release_statistics.sqlite3-shm \
  data/lol_skin_release_statistics.sqlite3-wal

collect-lol-wiki \
  --database data/lol_skin_release_statistics.sqlite3 \
  --user-agent "lol-skin-release-statistics/0.1 (your-email@example.com)"
```

League of Graphs pages must be saved manually because this project intentionally has no automated downloader. Put
the saved `.htm` files under `data/leagueofgraphs_html_pages`, then import them:

```bash
import-leagueofgraphs-html \
  --input-dir data/leagueofgraphs_html_pages \
  --database data/lol_skin_release_statistics.sqlite3
```

Finally configure LiteLLM and classify every champion-patch:

```bash
export LITELLM_KEY="your-api-key"
export LITELLM_URL="https://litellm.example.com/v1"
export LITELLM_MODEL="your-model-name"

classify-lol-patches \
  --database data/lol_skin_release_statistics.sqlite3 \
  --all \
  --workers 6
```

The Wiki collector is the only source-data network fetch in this sequence; LiteLLM separately makes classification
requests. The offline League of Graphs importer and LiteLLM classifier write to the same database, so do not run
them concurrently.

## League Wiki collector

The installed `collect-lol-wiki` command collects:

- the canonical champion list and Wiki URLs;
- every skin and release date from `Module:SkinData/data`, including base skins and chromas;
- each champion's patch history, split into individual leaf changes;
- the US release date of every referenced patch and the effective date of labelled hotfixes.

Run the complete collection with an identifying user agent containing a working contact address:

```bash
collect-lol-wiki \
  --database data/lol_skin_release_statistics.sqlite3 \
  --user-agent "lol-skin-release-statistics/0.1 (your-email@example.com)"
```

The equivalent module invocation is:

```bash
python -m lol_skin_release_statistics.collect_wiki \
  --database data/lol_skin_release_statistics.sqlite3 \
  --user-agent "lol-skin-release-statistics/0.1 (your-email@example.com)"
```

The default delay is 0.75 seconds between live API requests. Increase it with `--delay`; do not set it to zero for a full collection.

### Staged and test runs

Use `--stage` to collect only part of the dataset. It can be repeated and accepts `champions`, `skins`, or `patches`:

```bash
collect-lol-wiki --stage champions --stage skins
collect-lol-wiki --stage patches --champion Ashe
collect-lol-wiki --stage patches --champion Ashe --champion Wukong
```

The champion stage is run automatically if a requested stage needs an empty champion registry. Values passed to `--champion` are exact canonical names from the `champions` table and only limit patch-history collection; skin metadata always comes from the Wiki's global module.

Reruns use raw API responses cached in `source_documents`. Use `--refresh` only when a new Wiki snapshot is wanted:

```bash
collect-lol-wiki --refresh
```

The import is transactional and replaces the normalized rows for each refreshed scope, so retries do not create duplicates. If a run is interrupted, rerun the same command; already downloaded documents will be read from SQLite.

### Output tables

| Table | Contents |
| --- | --- |
| `source_documents` | Gzip-compressed API JSON, URL, retrieval timestamp, and SHA-256 hash |
| `champions` | Canonical names, Riot IDs, public Wiki titles/URLs, and internal Wiki data keys |
| `champion_source_ids` | Source-specific champion identifiers and URLs, including display/data-name aliases |
| `graph_series` | League of Graphs metric series and local HTML provenance linked to a champion |
| `graph_points` | Unix-millisecond timestamps and percentage values belonging to a graph series |
| `skins` | Skin IDs/names, release dates, availability, price, sets, and SkinData/Cosmetics source URLs |
| `skin_chromas` | Chroma/form metadata linked to its skin (a Wiki ID can intentionally be shared by multiple forms) |
| `patches` | Patch identifiers, Wiki titles/URLs, and release dates |
| `patch_events` | Champion-specific patch/hotfix headings and effective dates |
| `patch_entries` | Individual changes with their ability/stat context and source order |
| `patch_classifications` | Shareable label, worded confidence, and timestamp for each champion-patch |

See the [database entity-relationship diagram](db_data_model.md) for all implemented fields and relationships.

Dates are stored as ISO `YYYY-MM-DD` text. A skin's original Wiki value is also retained in `release_date_raw`, because canceled and not-yet-released skins can use `N/A`; their normalized date is `NULL`. The collector likewise warns and leaves a patch date as `NULL` when an old Wiki page does not expose one reliably. Unlinked `Unknown Patch` sections are retained as entries but are not treated as downloadable patch pages.

Useful validation queries include:

```bash
sqlite3 data/lol_skin_release_statistics.sqlite3 \
  "SELECT COUNT(*) AS champions FROM champions; SELECT COUNT(*) AS skins FROM skins;"

sqlite3 -header -column data/lol_skin_release_statistics.sqlite3 \
  "SELECT c.name, s.display_name, s.release_date
   FROM skins AS s JOIN champions AS c ON c.id = s.champion_id
   WHERE c.name = 'Ashe' ORDER BY s.release_date;"
```

## Champion-patch classification

The `classify-lol-patches` command sends all normalized `patch_events` and `patch_entries` for one champion and patch ID per request to an OpenAI-compatible LiteLLM proxy. It classifies their overall effect as `buff`, `nerf`, `change`, or `rework` and stores the validated response in `patch_classifications`.

Configure the endpoint using environment variables; credentials are never written to the database:

```bash
export LITELLM_KEY="your-api-key"
export LITELLM_URL="https://litellm.example.com/v1"
export LITELLM_MODEL="your-model-name"
```

`LITELLM_URL` may be either the proxy base URL or the complete `/chat/completions` URL. The classifier appends `/chat/completions` when necessary.

First inspect a few inputs without making model calls:

```bash
classify-lol-patches --dry-run --limit 3
```

Then run a small paid/requested sample and review the results before starting the complete dataset:

```bash
classify-lol-patches --champion Ashe --limit 10

sqlite3 -header -column data/lol_skin_release_statistics.sqlite3 \
  "SELECT c.name, cl.patch_id, cl.label, cl.confidence
   FROM patch_classifications AS cl
   JOIN champions AS c ON c.id = cl.champion_id
   ORDER BY cl.id DESC LIMIT 10;"
```

TLS certificate and hostname verification are enabled by default. For a trusted internal LiteLLM endpoint whose
certificate cannot be validated, disable verification explicitly for that run:

```bash
classify-lol-patches --champion Ashe --limit 10 --no-verify-ssl
```

Do not use `--no-verify-ssl` for endpoints reached over an untrusted network.

Run all remaining entries with:

```bash
classify-lol-patches --all
```

Classification uses 12 parallel worker processes by default. Each worker performs LiteLLM requests independently,
while the parent process remains the only SQLite writer and commits each completed result. Adjust concurrency to the
capacity and rate limits of the endpoint:

```bash
classify-lol-patches --all --workers 6
```

The explicit `--all` flag prevents accidentally starting a large batch of model requests. A `tqdm` progress bar reports completed champion-patches, elapsed time, throughput, and estimated time remaining.

Useful controls are:

- `--champion NAME` and `--patch ID`, both repeatable, to restrict a run;
- `--limit N` for a bounded sample, or `--all` for every remaining entry in the selected scope;
- `--workers N` to set the number of parallel processes (default: 12);
- `--delay SECONDS`, `--timeout SECONDS`, and `--retries N` for endpoint behavior;
- `--no-verify-ssl` to explicitly disable certificate and hostname verification;
- `--force` to replace existing results in the selected scope;
- `--verbose` to report every classified champion-patch.

Each successful response is committed immediately by the parent process. If the process is interrupted, rerunning the command skips champion-patches that already have a classification. Use `--force` when intentionally replacing them, including when changing models or prompts. Responses are locally validated before insertion, and transient HTTP errors, rate limits, malformed JSON, and invalid labels are retried. `--delay` applies independently within each worker, so reduce `--workers` as well when enforcing a low aggregate request rate.

Only the label, worded confidence, and classification timestamp are stored. Confidence is one of `VERY_UNCERTAIN`, `UNCERTAIN`, `AMBIGUOUS`, `CERTAIN`, or `VERY_CERTAIN`. The LiteLLM URL, model name, API key, prompt, request metadata, token usage, and raw model response are not written to SQLite, keeping the database suitable for sharing.

Reimporting an unchanged cached Wiki history preserves its classifications. If a refreshed Wiki history has actually changed, champion-patch classifications for that champion are removed and must be generated again; this prevents labels from remaining attached to stale text.

The model receives every patch heading and leaf entry for the champion-patch, including hotfix headings and effective dates. The prompt defines `rework` conservatively so large ordinary balance patches and initial champion releases are not mislabeled solely because they contain many changes. Classification quality should still be evaluated on a manually reviewed sample before downstream analysis.

## League of Graphs HTML import

There is intentionally no automated League of Graphs downloader. Its current Terms of Use prohibit automated queries without express written permission. The offline importer parses manually saved HTML files without making requests to League of Graphs. Keep the original files under a local data directory; they retain the selected filters even though the normalized database intentionally does not.

Place browser-saved champion statistics pages under `data/leagueofgraphs_html_pages`. Only the `.htm` files are
required; the companion `_files` directories contain rendering assets and are ignored. The importer searches the
input directory recursively.

Validate every saved page without opening or changing SQLite:

```bash
import-leagueofgraphs-html --dry-run
```

After the patch classifier has stopped and DBeaver is disconnected, import all pages with:

```bash
import-leagueofgraphs-html
```

Use a different location when necessary:

```bash
import-leagueofgraphs-html \
  --input-dir data/leagueofgraphs_html_pages \
  --database data/lol_skin_release_statistics.sqlite3
```

For every page, the importer extracts the inline popularity, win-rate, and ban-rate histories. Values are stored as
percentage points with their original Unix timestamps in milliseconds. The champion is matched by Riot's
numeric champion ID, so display-name and source-slug differences do not affect the mapping. If a page's Riot ID is
absent from the database, refresh the Wiki collection before importing. The League of Graphs slug and canonical
page URL are upserted into `champion_source_ids` with source `leagueofgraphs`.

The three metrics are stored independently. A source page can occasionally omit one observation from one metric;
this does not prevent the other observations from being imported.

Role, rank, region, and queue filters are intentionally not persisted. `graph_series` retains the canonical page URL,
relative source filename, file modification time, and SHA-256 hash. All supplied pages are committed together, and
reimporting a page replaces its three series without creating duplicate observations. Duplicate pages for the same
champion are rejected because filter variants cannot be distinguished in this data model.

Inspect imported points with:

```sql
SELECT
    champion.name AS champion,
    source.source_id AS leagueofgraphs_slug,
    series.metric,
    datetime(point.observed_at_ms / 1000, 'unixepoch') AS observed_at_utc,
    point.value AS percentage
FROM graph_points AS point
JOIN graph_series AS series ON series.id = point.graph_series_id
JOIN champions AS champion ON champion.id = series.champion_id
JOIN champion_source_ids AS source
    ON source.champion_id = champion.id
   AND source.source = 'leagueofgraphs'
ORDER BY champion.name, series.metric, point.observed_at_ms;
```

## Code quality

Run Ruff from the repository root:

```bash
ruff check .
ruff format --check .
```
