# STATUS

This is just a quick first Claude build. The goal is to have a simple python module to manage the What If Data Donation data CLI scripts.

# data-donation-data

A Python package for ingesting, querying, and web-scraping donated dataset files.
Data is stored in a local SQLite database and managed through the `ddd` command-line tool.

## Features

- **Ingest**: Scan a directory of donation data JSON files and load them into
  source-specific SQLite tables, parsing structured metadata from the filenames.
- **Summarise**: Inspect any table column-by-column, or export a long-format
  participant × field coverage report across all donation tables.
- **Extract**: Export a selection of columns from any table to CSV or JSONL.
- **Scrape**: Queue URLs from any table column and scrape structured metadata
  (Open Graph, JSON-LD, microdata, Dublin Core, RDFa, microformat) into a
  dedicated `url_metadata` table, with parallel cross-domain requests and
  polite per-domain rate limiting.

## Installation

```sh
pip install -e .
```

Requires Python 3.11+.

## Database location

By default the database is written to `data.db` in the current working directory.
Set the `DDD_DB` environment variable to use a different path:

```sh
export DDD_DB=/path/to/my.db
```

---

## Commands

### `ddd ingest <directory>`

Scan a directory for donation data JSON files and load them into the database.

Files must follow this naming convention:

```
assignment=<id>_task=<id>_participant=<id>_source=<name>_key=<id>.json
```

The filename is split on `_<key>=` boundaries (not plain underscores), so
source names that contain underscores (e.g. `source=google_chrome`) are handled
correctly.

Each source gets its own table named `ddd_<source>` (e.g. `ddd_youtube`,
`ddd_google_chrome`). Every table has columns for the four metadata fields
(`assignment`, `task`, `participant`, `key`) followed by one column per JSON
field found in the file bodies. New columns are added automatically as new
fields are encountered across files.

```sh
# Ingest all JSON files in a directory
ddd ingest path/to/data/

# Also scan sub-directories
ddd ingest path/to/data/ --recursive
```

Files that do not match the naming convention (e.g. missing a `source=` part)
are silently skipped and reported in the summary.

---

### `ddd tables`

List all tables in the database.

```sh
ddd tables
```

---

### `ddd summarise <table>`

Print a column-level summary of any table: row count, non-null count, null
percentage, distinct value count, and a few sample values.

```sh
ddd summarise ddd_youtube
```

---

### `ddd field-summary`

Export a long-format CSV covering all `ddd_*` donation tables. For every
combination of source × participant × field it reports the total number of
rows for that participant and the number of non-missing values for that field.

```sh
# Print to stdout
ddd field-summary

# Write to a file
ddd field-summary --output summary.csv
```

Output columns:

| Column | Description |
|---|---|
| `source` | Data source (table name without the `ddd_` prefix) |
| `participant` | Participant identifier |
| `field` | Column / field name |
| `n_non_null` | Number of non-missing values for this participant + field |
| `n_total` | Total rows for this participant in this source |

Metadata columns (`assignment`, `task`, `key`) are excluded from the field list.

---

### `ddd extract <table> <col> [<col> ...]`

Export selected columns from any table to CSV or JSONL.

```sh
# CSV to stdout
ddd extract ddd_youtube participant url title

# JSONL to a file
ddd extract ddd_youtube participant url --format jsonl --output urls.jsonl

# With a WHERE filter
ddd extract ddd_youtube participant url --where "participant = 'alice'"
```

---

### `ddd scrape queue <table> <url_column>`

Read unique URLs from a column in any table and add them to the `url_metadata`
table as `pending`. Already-queued URLs are skipped by default.

```sh
ddd scrape queue ddd_youtube url

# Re-queue URLs even if already present
ddd scrape queue ddd_youtube url --allow-duplicates
```

---

### `ddd scrape run`

Scrape all pending URLs. Requests to **different domains run in parallel**;
requests to the **same domain are serialised** with a random delay between
them to avoid IP bans.

```sh
ddd scrape run

# Limit to 100 URLs, 20 concurrent connections, 1–3 s per-domain delay
ddd scrape run --limit 100 --concurrency 20 --delay-min 1.0 --delay-max 3.0
```

Options:

| Option | Default | Description |
|---|---|---|
| `--limit / -n` | — | Stop after N URLs |
| `--concurrency / -j` | `10` | Max simultaneous in-flight requests |
| `--delay-min` | `0.5` | Min seconds between requests to the same domain |
| `--delay-max` | `2.5` | Max seconds between requests to the same domain |

Scraped metadata (Open Graph, JSON-LD, microdata, Dublin Core, RDFa,
microformat) is stored as a JSON string in the `data` column of `url_metadata`.

---

### `ddd scrape status`

Show a breakdown of `pending` / `success` / `failed` counts in `url_metadata`.

```sh
ddd scrape status
```

---

## The `url_metadata` table

| Column | Type | Description |
|---|---|---|
| `url` | TEXT | Original queued URL (unique) |
| `canonical_url` | TEXT | Canonical URL found on the page |
| `status` | TEXT | `pending`, `success`, or `failed` |
| `scraped_at` | TEXT | ISO-8601 timestamp of the scrape attempt |
| `data` | TEXT | JSON string of all extracted structured metadata |
| `error` | TEXT | Error message if status is `failed` |

---

## Project layout

```
src/data_donation_data/
├── __init__.py      — package version
├── db.py            — SQLite connection helper (WAL mode, DDD_DB env var)
├── ingest.py        — filename parsing + donation directory ingestion
├── summarise.py     — column summaries + participant × field report
├── extract.py       — CSV / JSONL export
├── scraper.py       — async URL scraper (httpx + extruct)
└── cli.py           — Click-based `ddd` entry point
```
