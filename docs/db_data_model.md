# Database entity-relationship diagram

This diagram represents the tables currently created by the Python collectors, offline importer, and classifier.

```mermaid
erDiagram
    SOURCE_DOCUMENTS {
        int id PK
        string source_type UK
        string source_key UK
        string source_url
        string retrieved_at
        string content_hash
        string content_encoding
        blob content
    }

    CHAMPIONS {
        int id PK
        int game_id UK
        string name UK
        string wiki_title UK
        string wiki_data_name UK
        string wiki_url UK
    }

    CHAMPION_SOURCE_IDS {
        int id PK
        int champion_id FK
        string source UK
        string source_id UK
        string source_url
    }

    GRAPH_SERIES {
        int id PK
        int champion_id FK
        string metric UK
        string source_url
        string source_file
        string file_modified_at
        string content_hash
    }

    GRAPH_POINTS {
        int id PK
        int graph_series_id FK
        int observed_at_ms UK
        float value
    }

    SKINS {
        int id PK
        int champion_id FK
        int wiki_skin_id UK
        string internal_name UK
        string display_name
        boolean is_base
        string release_date
        string release_date_raw
        string retired_date
        string availability
        int cost
        boolean loot_eligible
        string sets_json
        string source_url
        string cosmetics_url
    }

    SKIN_CHROMAS {
        int id PK
        int skin_id FK
        string name UK
        int wiki_chroma_id
        string availability
        string source
        string distribution
    }

    PATCHES {
        string patch_id PK
        string wiki_title
        string wiki_url
        string release_date
    }

    PATCH_EVENTS {
        int id PK
        int champion_id FK
        string patch_id FK
        string heading
        string effective_date
        int source_order UK
        string source_url
    }

    PATCH_ENTRIES {
        int id PK
        int patch_event_id FK
        string context
        string change_text
        int source_order UK
    }

    CLASSIFICATIONS {
        int id PK
        int patch_entry_id FK, UK
        string label
        string confidence
        string classified_at
    }

    CHAMPIONS ||--o{ CHAMPION_SOURCE_IDS : "has source identities"
    CHAMPIONS ||--o{ GRAPH_SERIES : "has histories"
    GRAPH_SERIES ||--o{ GRAPH_POINTS : "contains"
    CHAMPIONS ||--o{ SKINS : "has"
    SKINS ||--o{ SKIN_CHROMAS : "has"
    CHAMPIONS ||--o{ PATCH_EVENTS : "has history"
    PATCHES ||--o{ PATCH_EVENTS : "groups"
    PATCH_EVENTS ||--o{ PATCH_ENTRIES : "contains"
    PATCH_ENTRIES ||--o| CLASSIFICATIONS : "may have"
```

`source_documents` is deliberately standalone: its `source_type` and `source_key` identify the cached raw API document without coupling normalized rows to a particular snapshot. Composite uniqueness constraints are shown as `UK` on their participating fields.

## Rendering locally

GitHub renders the Mermaid block directly. To produce standalone images with Mermaid CLI:

```bash
npx -p @mermaid-js/mermaid-cli@11.15.0 mmdc \
  -i docs/db_data_model.md \
  -o docs/db_data_model.svg \
  -b white

npx -p @mermaid-js/mermaid-cli@11.15.0 mmdc \
  -i docs/db_data_model.md \
  -o docs/db_data_model.png \
  -w 4000 \
  -H 3000 \
  -b white
```

If Chromium is not already available to Puppeteer, install the version requested by the installed Mermaid CLI package before rendering.

On a minimal Linux or WSL installation, Chromium also needs at least one system font. If Mermaid reports
`svg element not in render tree` for an ER diagram and `fc-list` returns no fonts, install Fontconfig and a font family,
then rebuild the font cache:

```bash
sudo apt update
sudo apt install fontconfig fonts-dejavu-core
fc-cache -f
```

The error occurs because Mermaid measures ER-diagram labels as SVG text; without a usable font, Chromium reports a
zero-sized text box. Rerun the same `mmdc` command after the fonts are installed.
