# League of Legends skin release statistics

Do champions receive favourable balance treatment when they get a new skin? This project collects League of Legends skin releases, patch histories, and player-performance histories to examine that question.

It contains a reproducible data-collection pipeline, a populated SQLite database, and the rendered analysis. The current dataset snapshot is from 6 September 2026.

> [!IMPORTANT]
> ## Explore the database
>
> The ready-to-query [SQLite database](data/lol_skin_release_statistics.sqlite3) is the most useful artifact in this repository. It is a 44 MB snapshot containing 173 champions, 2,130 skin records (1,957 non-base skins), 466 patches, 34,047 patch-note entries, 11,855 champion-patch classifications, and 140,054 win-, pick-, and ban-rate observations.
>
> It includes normalized tables as well as cached League Wiki source documents, so it can be used as a starting point for different analyses without rerunning the collectors. See the [data model](docs/db_data_model.md) for its tables and relationships.

## What is included

- League Wiki collector for champion identities, skin metadata and release dates, patch histories, patch dates, and hotfix dates.
- Offline importer for manually saved League of Graphs pages, containing historical win rate, pick rate, and ban rate.
- Champion-patch classifier, which assigns each aggregate patch change a `buff`, `nerf`, `change`, or `rework` label.
- SQLite database and source-data provenance.
- Quarto/R analysis source, rendered HTML, and rendered PDF.

League of Graphs pages are deliberately imported from manually saved HTML. The project does not download them automatically because the site's Terms of Use prohibit automated extraction without permission.

## Read or run the project

- [Data collection guide](docs/data-collection.md) — setup, complete rebuild, source constraints, importer, and patch classification.
- [Analysis rendering guide](docs/quarto.md) — prerequisites and commands to render the Quarto report as HTML or PDF.
- [Database data model](docs/db_data_model.md) — tables, fields, and entity-relationship diagram.

## Analysis

Read the full report: [PDF](src/analysis/skin_release_findings.pdf) · [Quarto source](src/analysis/skin_release_findings.qmd)

The analysis considers 1,377 eligible skin releases since 2014, excluding releases close to a champion launch or major rework. Its main results are:

- Champions near a skin release appear in patch notes more often: the estimated odds are 24% higher.
- There is no convincing evidence that those patch-note appearances are more likely to be buffs than nerfs, or that win rate rises after a release.
- Pick and ban rates do increase after a release, consistent with increased player attention.

In short, the data supports “champions get more attention when they receive a skin,” but does not establish “Riot makes champions stronger to sell skins.” This is an observational analysis, so it shows association rather than causation.
