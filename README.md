# data-donation-data

A Python package for ingesting, querying, and web-scraping donated dataset files.
Data is stored in a local SQLite database and managed through the `ddd` command-line tool.

## Features

- **Ingest**: Scan a directory of donation data JSON files and load them into a
  normalised long-format database, parsing structured metadata from the filenames.
  URL values are automatically queued for scraping.
- **Summarise**: Inspect any table column-by-column, or explore a donation source
  field-by-field. Export a long-format participant × field coverage report.
- **Extract**: Export a selection of columns from any table to CSV or JSONL.
- **Export**: Generate a `ddd_export.yaml` config file to toggle which fields
  to include in exports.
- **Scrape**: Scrape structured metadata (Open Graph, JSON-LD, microdata,
  Dublin Core, RDFa, microformat) from all queued URLs, with parallel
  cross-domain requests and polite per-domain rate limiting.

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

## Database schema

Data is stored in a normalised long-format schema across five tables:

| Table | Description |
|---|---|
| `fields` | One row per unique `(source, field)` pair; auto-generates `field_id` |
| `participants` | One row per unique participant string; auto-generates `participant_id` |
| `data` | One row per observation: `(field_id, participant_id, assignment, task, key, value)` |
| `urls` | One row per `(participant, field, url)` triple found during ingest; tracks scraping status |
| `url_metadata` | One row per canonical URL, populated by the scraper |

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

Data is stored in long format across the `fields`, `participants`, and `data`
tables. Any field value that starts with `http://` or `https://` is automatically
added to the `urls` table with `status='pending'` for later scraping — no
separate queue step is needed.

Files that do not match the naming convention are silently skipped and
reported in the summary.

```sh
# Ingest all JSON files in a directory
ddd ingest path/to/data/

# Also scan sub-directories
ddd ingest path/to/data/ --recursive
```

---

### `ddd tables`

List all tables in the database.

```sh
ddd tables
```

---

### `ddd sources`

List all data sources that have been ingested (distinct values from the `source`
column of the `fields` table).

```sh
ddd sources
```

---

### `ddd summarise`

Print a source-level summary across all ingested sources, showing the number of
fields, participants, total observations, and non-null percentage per source.
For a field-level breakdown of a specific source use `ddd summarise-source`.

```sh
ddd summarise
```

Output columns:

| Column | Description |
|---|---|
| `Source` | Data source name |
| `Fields` | Number of distinct fields for this source |
| `Participants` | Number of distinct participants with data for this source |
| `Observations` | Total observations across all participants and fields |
| `Non-null` | Observations with a non-null value |
| `Non-null %` | Percentage of non-null observations |

---

### `ddd summarise-source <source>`

Print a field-level summary for a specific donation source: total observations,
non-null count, non-null percentage, and number of distinct participants per field.

`<source>` is a source name as shown by `ddd sources` (e.g. `youtube`,
`google_chrome`).

```sh
ddd summarise-source youtube
```

---

### `ddd field-summary`

Export a long-format CSV covering all ingested sources. For every combination of
source × participant × field it reports the total number of rows and the number
of non-missing values.

```sh
# Print to stdout
ddd field-summary

# Write to a file
ddd field-summary --output summary.csv
```

Output columns:

| Column | Description |
|---|---|
| `source` | Data source name |
| `participant` | Participant identifier |
| `field` | Field name |
| `n_non_null` | Number of non-missing values for this participant + field |
| `n_total` | Total rows for this participant in this source |

---

### `ddd extract <table> <col> [<col> ...]`

Export selected columns from any table to CSV or JSONL.

```sh
# CSV to stdout
ddd extract data participant_id value

# JSONL to a file
ddd extract data participant_id value --format jsonl --output out.jsonl

# With a WHERE filter
ddd extract data value --where "field_id = 42"
```

---

### `ddd export init`

Create or update a `ddd_export.yaml` file listing every `(source, field)`
combination with a boolean toggle. Re-run after ingesting more data to pick
up any new fields without losing existing toggle settings.

```sh
ddd export init

# Write to a custom path
ddd export init --output my_export.yaml
```

Edit the generated YAML manually to set fields to `true` (include) or
`false` (exclude).

---

### `ddd scrape run`

Scrape all pending URLs. URLs are queued automatically during `ddd ingest` —
any field value starting with `http://` or `https://` is added to the `urls`
table. Requests to **different domains run in parallel**; requests to the
**same domain are serialised** with a random delay between them to avoid
IP bans.

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

Show a breakdown of `pending` / `success` / `failed` counts in the `urls`
table, with both raw URL counts and deduplicated normalised URL counts.

```sh
ddd scrape status
```

---

## The `urls` table

| Column | Type | Description |
|---|---|---|
| `url_id` | INTEGER | Primary key |
| `participant_id` | INTEGER | FK → `participants` |
| `field_id` | INTEGER | FK → `fields` |
| `url` | TEXT | Raw URL as found in the donated data |
| `normalized_url` | TEXT | Cleaned URL (tracking params stripped, used for deduplication) |
| `canonical_url` | TEXT | Canonical URL resolved during scraping |
| `status` | TEXT | `pending`, `success`, or `failed` |
| `error` | TEXT | Error message if status is `failed` |

## The `url_metadata` table

| Column | Type | Description |
|---|---|---|
| `canonical_url` | TEXT | Canonical URL (primary key) |
| `scraped_at` | TEXT | ISO-8601 timestamp of the scrape attempt |
| `data` | TEXT | JSON string of all extracted structured metadata |
| `error` | TEXT | Error message if scraping failed |

---

## Project layout

```
src/data_donation_data/
├── __init__.py      — package version
├── db.py            — SQLite connection helper + schema (WAL mode, DDD_DB env var)
├── ingest.py        — filename parsing + donation directory ingestion
├── summarise.py     — column summaries + participant × field report
├── extract.py       — CSV / JSONL export
├── export.py        — ddd_export.yaml config management
├── scraper.py       — async URL scraper (httpx + extruct)
└── cli.py           — Click-based `ddd` entry point
```
