# Rendering the Quarto analysis

The main analysis article is [`src/analysis/skin_release_findings.qmd`](../src/analysis/skin_release_findings.qmd).
Run the commands below from the repository root so that the article can locate the database and its analysis source.

## Prerequisites

Rendering requires:

- [Quarto](https://quarto.org/docs/get-started/);
- R;
- the `DBI`, `RSQLite`, and `data.table` R packages;
- a populated `data/lol_skin_release_statistics.sqlite3` database; and
- the current `tmp.R` scratch analysis in the repository root.

Confirm that Quarto and R are available:

```bash
quarto --version
Rscript --version
```

Install the required R packages once if they are not already available:

```r
install.packages(c("DBI", "RSQLite", "data.table"))
```

The article sources `tmp.R` in a hidden setup chunk, then selects and formats its results. Consequently, changes to
the calculations in `tmp.R` are reflected the next time the article is rendered. Keep the visible parameter block in
the article synchronized with the corresponding values in `tmp.R`.

## Render HTML

```bash
quarto render src/analysis/skin_release_findings.qmd --to html
```

The output is written to `src/analysis/skin_release_findings.html`.

## Render PDF

PDF rendering also requires a TeX distribution. Quarto can install its recommended lightweight distribution,
TinyTeX:

```bash
quarto install tinytex
```

Then render the article with:

```bash
quarto render src/analysis/skin_release_findings.qmd --to pdf
```

The output is written to `src/analysis/skin_release_findings.pdf`.

## Preview while editing

Use preview mode to render the HTML article and refresh it when the source changes:

```bash
quarto preview src/analysis/skin_release_findings.qmd --to html
```

Stop the preview server with `Ctrl+C`.

## Code visibility

The article hides its R implementation so that the rendered document focuses on the findings. Only the analysis
parameters and SQL source queries are displayed. Tables, statistical results, and figures are regenerated during each
render.

## Troubleshooting

### SQLite database not found

Run the render command from the repository root and confirm that
`data/lol_skin_release_statistics.sqlite3` exists. Instructions for rebuilding the database are in
[`data-collection.md`](data-collection.md).

### Required R package is missing

Run the `install.packages(...)` command above in R, then render again with the same R installation.

### PDF engine is missing

If HTML works but PDF rendering reports that no LaTeX engine is available, run:

```bash
quarto install tinytex
```

Restart the shell if the installer updates the executable search path.

### Scratch analysis is missing

The current article depends on the repository-root `tmp.R` file. Restore or recreate that file before rendering. The
article deliberately fails early rather than producing a report from incomplete calculations.
