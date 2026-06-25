"""
Command-line interface for data-donation-data.

Entry point: ``ddd``

Commands
--------
ddd ingest <directory> [--recursive]
ddd tables
ddd sources
ddd summarise <table>
ddd summarise-source <source>
ddd field-summary [--output FILE]
ddd participant-summary [--output FILE]
ddd export [SOURCE...] [--output FILE] [--format csv|jsonl] [--all] [--assignment ID] [--task ID]
ddd export-aliases [--output FILE]
ddd export-summarise [SOURCE...] [--output FILE] [--all] [--no-alias]
ddd export-examples [SOURCE...] [--output FILE] [--n N] [--random] [--all] [--no-alias]  (one row per example value)
ddd export-data [SOURCE...] [--output-dir DIR] [--all] [--no-alias]
ddd remove [--assignment ID] [--task ID] [--participant ID] [--yes]
ddd create-config [--output FILE]
ddd scrape run [--limit N] [--delay-min S] [--delay-max S] [--concurrency N]
ddd scrape status
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table as RichTable

from data_donation_data.db import db_path, get_connection
from data_donation_data.export import DEFAULT_YAML_PATH, init_export_yaml
from data_donation_data.ingest import ingest_donation_directory
from data_donation_data.scraper import scrape_pending
from data_donation_data.summarise import (
    list_sources,
    list_tables,
    participant_field_summary,
    summarise_all_sources,
    summarise_source,
    task_assignment_summary,
)

console = Console()


# ---------------------------------------------------------------------------
# Alias-map helper
# ---------------------------------------------------------------------------


def _try_load_alias_map() -> dict:
    """
    Load alias mappings from ``ddd_export.yaml`` if it exists.
    Returns ``{}`` when the file is absent or invalid.
    """
    try:
        from data_donation_data.export import build_alias_map, load_export_yaml

        return build_alias_map(load_export_yaml(DEFAULT_YAML_PATH))
    except (FileNotFoundError, ValueError):
        return {}


def _try_load_export_config() -> dict:
    """
    Load the full export config from ``ddd_export.yaml`` if it exists.
    Returns ``{"tasks": {}, "assignments": {}, "sources": {}}`` when absent.
    """
    try:
        from data_donation_data.export import load_export_yaml

        return load_export_yaml(DEFAULT_YAML_PATH)
    except (FileNotFoundError, ValueError):
        return {"tasks": {}, "assignments": {}, "sources": {}}


def _try_load_exclusion_set() -> dict:
    """
    Load per-source exclusion sets from ``ddd_export.yaml`` if it exists.
    Returns ``{}`` when the file is absent or invalid.
    """
    try:
        from data_donation_data.export import build_exclusion_set, load_export_yaml

        return build_exclusion_set(load_export_yaml(DEFAULT_YAML_PATH))
    except (FileNotFoundError, ValueError):
        return {}


def _try_load_table_alias_map() -> dict:
    """
    Load table alias mappings from ``ddd_export.yaml`` if it exists.
    Returns ``{}`` when the file is absent or invalid.
    """
    try:
        from data_donation_data.export import build_table_alias_map, load_export_yaml

        return build_table_alias_map(load_export_yaml(DEFAULT_YAML_PATH))
    except (FileNotFoundError, ValueError):
        return {}


# ---------------------------------------------------------------------------
# Root group
# ---------------------------------------------------------------------------


@click.group()
@click.version_option(package_name="data-donation-data")
def cli() -> None:
    """data-donation-data: ingest, summarise, and scrape donated datasets."""


# ---------------------------------------------------------------------------
# ddd tables
# ---------------------------------------------------------------------------


@cli.command("tables")
def cmd_tables() -> None:
    """List all tables in the database."""
    with get_connection() as con:
        tables = list_tables(con)

    if not tables:
        console.print("[yellow]No tables found.[/yellow]")
        return

    t = RichTable(title=f"Tables in {db_path()}", show_lines=False)
    t.add_column("Table", style="cyan")
    for name in tables:
        t.add_row(name)
    console.print(t)


# ---------------------------------------------------------------------------
# ddd sources
# ---------------------------------------------------------------------------


@cli.command("sources")
def cmd_sources() -> None:
    """List all data sources that have been ingested."""
    with get_connection() as con:
        sources = list_sources(con)

    if not sources:
        console.print("[yellow]No sources found. Run `ddd ingest` first.[/yellow]")
        return

    t = RichTable(title="Ingested sources", show_lines=False)
    t.add_column("Source", style="cyan")
    for src in sources:
        t.add_row(src)
    console.print(t)


# ---------------------------------------------------------------------------
# ddd ingest
# ---------------------------------------------------------------------------


@cli.command("ingest")
@click.argument("directory", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option(
    "--recursive",
    "-r",
    is_flag=True,
    default=False,
    help="Also scan sub-directories for JSON files.",
)
def cmd_ingest(directory: Path, recursive: bool) -> None:
    """Ingest donation data JSON files from DIRECTORY.

    Each JSON file must follow the naming convention:

        assignment=<id>_task=<id>_participant=<id>_source=<name>_key=<id>.json

    Data is stored in long format in the ``fields``, ``participants``, and
    ``data`` tables.  URL values are automatically added to the ``urls``
    table for later scraping.  Files that do not match the naming convention
    are skipped.
    """
    with get_connection() as con:
        try:
            counts = ingest_donation_directory(con, directory, recursive=recursive)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc

    skipped_note = f" ([dim]{counts['skipped']} file(s) skipped[/dim])" if counts["skipped"] else ""
    url_note = f", [bold]{counts['urls']:,}[/bold] URL(s) queued" if counts["urls"] else ""
    console.print(
        f"[green]✓[/green] Ingested "
        f"[bold]{counts['files']}[/bold] file(s) / "
        f"[bold]{counts['rows']:,}[/bold] row(s)"
        f"{url_note}"
        f"{skipped_note}"
    )


# ---------------------------------------------------------------------------
# ddd summarise
# ---------------------------------------------------------------------------


@cli.command("summarise")
def cmd_summarise() -> None:
    """Print a source-level summary across all ingested sources.

    For a field-level breakdown of a specific source use `ddd summarise-source`.
    """
    with get_connection() as con:
        rows = summarise_all_sources(con)

    if not rows:
        console.print("[yellow]No data found. Run `ddd ingest` first.[/yellow]")
        return

    t = RichTable(title="Source summary", show_lines=True)
    t.add_column("Source", style="cyan", no_wrap=True)
    t.add_column("Tables", justify="right")
    t.add_column("Fields", justify="right")
    t.add_column("Participants", justify="right")
    t.add_column("Rows", justify="right")

    for row in rows:
        t.add_row(
            row["source"],
            f"{row['n_tables']:,}",
            f"{row['n_fields']:,}",
            f"{row['n_participants']:,}",
            f"{row['n_rows']:,}",
        )

    console.print(t)


# ---------------------------------------------------------------------------
# ddd summarise-source
# ---------------------------------------------------------------------------


@cli.command("summarise-source")
@click.argument("source")
@click.option("--all", "show_all", is_flag=True, default=False, help="Include excluded fields (ignore .excluded in config).")
@click.option("--no-alias", "no_alias", is_flag=True, default=False, help="Use original field names (ignore alias mappings).")
def cmd_summarise_source(source: str, show_all: bool, no_alias: bool) -> None:
    """Print a field-level summary for a donation SOURCE.

    SOURCE is a data source name as returned by `ddd sources`
    (e.g. youtube, google_chrome).  Field aliases defined in
    ddd_export.yaml are applied automatically when the file exists.
    """
    alias_map = _try_load_alias_map()
    exclusion_set = _try_load_exclusion_set()
    if show_all:
        exclusion_set = {}
    if no_alias:
        alias_map = {}
    table_alias_map = {} if no_alias else _try_load_table_alias_map()
    with get_connection() as con:
        try:
            table_rows = summarise_source(
                con,
                source,
                alias_map=alias_map,
                exclusion_set=exclusion_set,
                table_alias_map=table_alias_map,
            )
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc

    if not table_rows:
        console.print(f"[yellow]No data found for source '{source}'.[/yellow]")
        return

    n_tables = len(table_rows)
    n_fields = sum(len(t["fields"]) for t in table_rows)
    n_rows = sum(f["n_rows"] for t in table_rows for f in t["fields"])
    alias_note = " [dim](aliases applied)[/dim]" if alias_map.get(source) else ""
    console.print(
        f"\n[bold]Source:[/bold] [cyan]{source}[/cyan]  "
        f"[bold]Tables:[/bold] {n_tables}  "
        f"[bold]Fields:[/bold] {n_fields}  "
        f"[bold]Total rows:[/bold] {n_rows:,}"
        f"{alias_note}\n"
    )

    for tbl in table_rows:
        console.print(f"[bold yellow]{tbl['table']}[/bold yellow]")
        t = RichTable(show_lines=False, box=None, padding=(0, 1))
        t.add_column("Field", style="cyan", no_wrap=True)
        t.add_column("Participants", justify="right")
        t.add_column("Rows", justify="right")
        for f in tbl["fields"]:
            t.add_row(f["field"], f"{f['n_participants']:,}", f"{f['n_rows']:,}")
        console.print(t)
        console.print()


# ---------------------------------------------------------------------------
# ddd export-summarise
# ---------------------------------------------------------------------------


@cli.command("export-summarise")
@click.argument("sources", nargs=-1)
@click.option(
    "--output",
    "-o",
    default="ddd_summary.csv",
    show_default=True,
    type=click.Path(dir_okay=False, writable=True, path_type=Path),
    help="Output CSV file.",
)
@click.option("--all", "show_all", is_flag=True, default=False, help="Include excluded fields.")
@click.option("--no-alias", "no_alias", is_flag=True, default=False, help="Use original field and table names.")
@click.option(
    "--by-participant",
    is_flag=True,
    default=False,
    help="Add a participant column (one row per field × assignment × participant).",
)
def cmd_export_summarise(
    sources: tuple[str, ...],
    output: Path,
    show_all: bool,
    no_alias: bool,
    by_participant: bool,
) -> None:
    """Export a field-level summary for all sources to a single CSV.

    By default writes one row per (original_field × assignment) with
    both raw DB names and canonical aliases, so analysts can group and
    aggregate freely.

    With --by-participant, breaks down to one row per
    (original_field × assignment × participant); n_participants is
    replaced by a participant column.

    \b
    Default columns:
      source          — data source (facebook, youtube, …)
      original_table  — raw table name as stored in the DB
      table           — canonical alias (from .alias in ddd_export.yaml)
      original_field  — raw field name as stored in the DB
      field           — canonical alias (from ddd_export.yaml)
      assignment      — assignment ID
      assignment_name — human-readable name (from ddd_export.yaml)
      n_participants  — distinct participants with this field in this assignment
      n_rows          — total stored cell values

    \b
    Examples:
      ddd export-summarise                        # all sources → ddd_summary.csv
      ddd export-summarise facebook youtube       # specific sources
      ddd export-summarise -o my_summary.csv
      ddd export-summarise --by-participant       # one row per participant
      ddd export-summarise --no-alias             # raw names only
    """
    import unicodedata

    from data_donation_data.export import (
        build_alias_map,
        build_exclusion_set,
        build_table_alias_map,
    )

    config = _try_load_export_config()
    alias_map = {} if no_alias else build_alias_map(config)
    exclusion_set = {} if (show_all or no_alias) else build_exclusion_set(config)
    table_alias_map = {} if no_alias else build_table_alias_map(config)
    assignment_names: dict[str, str] = config.get("assignments", {})

    with get_connection() as con:
        all_sources = list_sources(con)

    if not all_sources:
        console.print("[yellow]No data found. Run `ddd ingest` first.[/yellow]")
        return

    requested = sorted(sources) if sources else sorted(all_sources)

    unknown = [s for s in requested if s not in all_sources]
    if unknown:
        raise click.ClickException(f"Unknown source(s): {', '.join(unknown)}. Available: {', '.join(all_sources)}")

    placeholders = ",".join("?" * len(requested))

    if by_participant:
        sql = f"""
            WITH per_file AS (
                SELECT d.field_id, d.file_id, COUNT(*) AS n_rows
                FROM data d
                GROUP BY d.field_id, d.file_id
            )
            SELECT
                t.source,
                t.table_name,
                f.field,
                fi.assignment,
                p.participant,
                SUM(pf.n_rows) AS n_rows
            FROM per_file pf
            JOIN fields       f  ON f.field_id       = pf.field_id
            JOIN tables       t  ON t.table_id       = f.table_id
            JOIN files        fi ON fi.file_id       = pf.file_id
            JOIN participants p  ON p.participant_id = fi.participant_id
            WHERE t.source IN ({placeholders})
            GROUP BY f.field_id, fi.assignment, fi.participant_id
            ORDER BY t.source, t.table_name, f.field, fi.assignment, p.participant
        """
        columns = [
            "source",
            "original_table",
            "table",
            "original_field",
            "field",
            "assignment",
            "assignment_name",
            "participant",
            "n_rows",
        ]
    else:
        sql = f"""
            WITH per_file AS (
                SELECT d.field_id, d.file_id, COUNT(*) AS n_rows
                FROM data d
                GROUP BY d.field_id, d.file_id
            )
            SELECT
                t.source,
                t.table_name,
                f.field,
                fi.assignment,
                COUNT(DISTINCT fi.participant_id) AS n_participants,
                SUM(pf.n_rows)                   AS n_rows
            FROM per_file pf
            JOIN fields f  ON f.field_id  = pf.field_id
            JOIN tables t  ON t.table_id  = f.table_id
            JOIN files  fi ON fi.file_id  = pf.file_id
            WHERE t.source IN ({placeholders})
            GROUP BY f.field_id, fi.assignment
            ORDER BY t.source, t.table_name, f.field, fi.assignment
        """
        columns = [
            "source",
            "original_table",
            "table",
            "original_field",
            "field",
            "assignment",
            "assignment_name",
            "n_participants",
            "n_rows",
        ]

    with get_connection() as con:
        db_rows = con.execute(sql, requested).fetchall()

    n_written = 0
    with output.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for row in db_rows:
            src: str = row["source"]
            tbl: str = row["table_name"]
            field: str = row["field"]
            field_nfc = unicodedata.normalize("NFC", field)
            assignment: str = row["assignment"] or ""

            # Exclusion filter uses original table name (YAML is keyed that way).
            if field_nfc in exclusion_set.get(src, {}).get(tbl, frozenset()):
                continue

            canonical_table = table_alias_map.get(src, {}).get(tbl, tbl)
            canonical_field = alias_map.get(src, {}).get(tbl, {}).get(field_nfc, field_nfc)

            out: dict = {
                "source": src,
                "original_table": tbl,
                "table": canonical_table,
                "original_field": field,
                "field": canonical_field,
                "assignment": assignment,
                "assignment_name": assignment_names.get(assignment, ""),
                "n_rows": row["n_rows"],
            }
            if by_participant:
                out["participant"] = row["participant"]
            else:
                out["n_participants"] = row["n_participants"]

            writer.writerow(out)
            n_written += 1

    console.print(f"[green]✓[/green] Wrote [bold]{n_written:,}[/bold] rows to [cyan]{output}[/cyan]")
    assignments_seen = sorted({row["assignment"] or "" for row in db_rows})
    console.print("   Assignments: " + ", ".join(f"{a} ({assignment_names.get(a, '?')})" for a in assignments_seen))


# ---------------------------------------------------------------------------
# ddd export-examples
# ---------------------------------------------------------------------------


@cli.command("export-examples")
@click.argument("sources", nargs=-1)
@click.option(
    "--output",
    "-o",
    default="ddd_examples.csv",
    show_default=True,
    type=click.Path(dir_okay=False, writable=True, path_type=Path),
    help="Output CSV file.",
)
@click.option(
    "--n",
    "n_examples",
    default=5,
    show_default=True,
    type=int,
    help="Number of example values to sample per field.",
)
@click.option("--all", "show_all", is_flag=True, default=False, help="Include excluded fields.")
@click.option("--no-alias", "no_alias", is_flag=True, default=False, help="Use original field and table names.")
@click.option(
    "--random",
    "use_random",
    is_flag=True,
    default=False,
    help="Sample values randomly using ORDER BY RANDOM() (slower; default: first N distinct values).",
)
@click.option(
    "--with-assignment",
    "with_assignment",
    is_flag=True,
    default=False,
    help="Add assignment and assignment_name columns to the output.",
)
def cmd_export_examples(
    sources: tuple[str, ...],
    output: Path,
    n_examples: int,
    show_all: bool,
    no_alias: bool,
    use_random: bool,
    with_assignment: bool,
) -> None:
    """Export a sample of field values to a CSV for quick inspection.

    Writes one row per example value (long format), so each sampled value
    gets its own row. Aliases are applied; original names are omitted.

    By default, the first N distinct values are used (fast, index-only).
    Use --random for a random sample; this may be significantly slower on
    large fields.

    \b
    Columns (default):
      source  — data source (facebook, youtube, …)
      table   — canonical table alias (from .alias in ddd_export.yaml)
      field   — canonical field alias (from ddd_export.yaml)
      example — a single sampled value

    \b
    Additional columns with --with-assignment:
      assignment      — assignment ID
      assignment_name — human-readable name (from ddd_export.yaml)

    \b
    Examples:
      ddd export-examples                        # all sources → ddd_examples.csv
      ddd export-examples facebook youtube       # specific sources
      ddd export-examples -o my_examples.csv
      ddd export-examples --n 10                 # 10 examples per field
      ddd export-examples --random               # truly random (slower)
      ddd export-examples --no-alias             # raw names only
      ddd export-examples --with-assignment      # include assignment columns
    """
    import unicodedata

    from data_donation_data.export import (
        build_alias_map,
        build_exclusion_set,
        build_table_alias_map,
    )

    config = _try_load_export_config()
    alias_map = {} if no_alias else build_alias_map(config)
    exclusion_set = {} if (show_all or no_alias) else build_exclusion_set(config)
    table_alias_map = {} if no_alias else build_table_alias_map(config)

    if use_random:
        console.print("[yellow]⚠[/yellow]  --random mode: sampling with ORDER BY RANDOM() — may be slow on large fields.")

    with get_connection() as con:
        all_sources_list = list_sources(con)

        if not all_sources_list:
            console.print("[yellow]No data found. Run `ddd ingest` first.[/yellow]")
            return

        requested = sorted(sources) if sources else sorted(all_sources_list)
        unknown = [s for s in requested if s not in all_sources_list]
        if unknown:
            raise click.ClickException(f"Unknown source(s): {', '.join(unknown)}. Available: {', '.join(all_sources_list)}")

        placeholders = ",".join("?" * len(requested))
        meta_rows = con.execute(
            f"""
            SELECT f.field_id, f.field, t.table_name, t.source
            FROM fields f
            JOIN tables t ON t.table_id = f.table_id
            WHERE t.source IN ({placeholders})
            ORDER BY t.source, t.table_name, f.field
            """,
            requested,
        ).fetchall()

        assignment_names: dict[str, str] = config.get("assignments", {})

        if with_assignment:
            # Collect distinct assignments for a field, then sample N rows per
            # assignment separately.  A single LIMIT over the full data table
            # would be biased toward whichever assignment has lower file_ids
            # (i.e. was ingested first).
            assignments_sql = """
                SELECT DISTINCT fi.assignment
                FROM data d
                JOIN files fi ON fi.file_id = d.file_id
                WHERE d.field_id = ?
            """
            if use_random:
                sample_sql = """
                    SELECT d.value, fi.assignment
                    FROM data d
                    JOIN files fi ON fi.file_id = d.file_id
                    WHERE d.field_id = ? AND fi.assignment = ? AND d.value IS NOT NULL
                    ORDER BY RANDOM()
                    LIMIT ?
                """
            else:
                sample_sql = """
                    SELECT d.value, fi.assignment
                    FROM data d
                    JOIN files fi ON fi.file_id = d.file_id
                    WHERE d.field_id = ? AND fi.assignment = ? AND d.value IS NOT NULL
                    LIMIT ?
                """
        elif use_random:
            sample_sql = """
                SELECT value FROM data
                WHERE field_id = ? AND value IS NOT NULL
                ORDER BY RANDOM()
                LIMIT ?
            """
        else:
            sample_sql = """
                SELECT DISTINCT value FROM data
                WHERE field_id = ? AND value IS NOT NULL
                LIMIT ?
            """

        columns = ["source", "table", "field"]
        if with_assignment:
            columns += ["assignment", "assignment_name"]
        columns += ["example"]
        n_written = 0

        with output.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=columns)
            writer.writeheader()

            for row in meta_rows:
                src: str = row["source"]
                tbl: str = row["table_name"]
                field: str = row["field"]
                field_nfc = unicodedata.normalize("NFC", field)
                field_id: int = row["field_id"]

                # Exclusion filter (keyed by original table name, as in YAML).
                if field_nfc in exclusion_set.get(src, {}).get(tbl, frozenset()):
                    continue

                canonical_table = table_alias_map.get(src, {}).get(tbl, tbl)
                canonical_field = alias_map.get(src, {}).get(tbl, {}).get(field_nfc, field_nfc)

                sample_rows: list = []
                if with_assignment:
                    field_assignments = [r["assignment"] for r in con.execute(assignments_sql, (field_id,)).fetchall()]
                    for asgn in field_assignments:
                        sample_rows.extend(con.execute(sample_sql, (field_id, asgn, n_examples)).fetchall())
                else:
                    sample_rows = con.execute(sample_sql, (field_id, n_examples)).fetchall()
                for sample in sample_rows:
                    out: dict = {
                        "source": src,
                        "table": canonical_table,
                        "field": canonical_field,
                    }
                    if with_assignment:
                        assignment = sample["assignment"] or ""
                        out["assignment"] = assignment
                        out["assignment_name"] = assignment_names.get(assignment, "")
                    out["example"] = sample["value"]
                    writer.writerow(out)
                    n_written += 1

    console.print(f"[green]✓[/green] Wrote [bold]{n_written:,}[/bold] rows to [cyan]{output}[/cyan]")


# ---------------------------------------------------------------------------
# ddd export-data
# ---------------------------------------------------------------------------


@cli.command("export-data")
@click.argument("sources", nargs=-1)
@click.option(
    "--output-dir",
    "-o",
    default="ddd_cleaned",
    show_default=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Root output directory.",
)
@click.option("--all", "show_all", is_flag=True, default=False, help="Include excluded fields.")
@click.option("--no-alias", "no_alias", is_flag=True, default=False, help="Use original field and table names.")
def cmd_export_data(
    sources: tuple[str, ...],
    output_dir: Path,
    show_all: bool,
    no_alias: bool,
) -> None:
    """Export cleaned data as per-participant wide-format CSV files.

    For every (source, table, participant, assignment) combination, writes one
    CSV file where each row is an original data row and each column is an
    aliased field name.

    \b
    Directory layout:
      ddd_cleaned/
        {source}/
          {table}/
            {assignment_name}_{participant}.csv

    Tables that share a .alias in ddd_export.yaml are merged into the same
    folder.  Field and table names use aliases from ddd_export.yaml.

    \b
    Examples:
      ddd export-data                   # all sources → ddd_cleaned/
      ddd export-data facebook youtube  # specific sources
      ddd export-data -o my_output      # custom output directory
      ddd export-data --all             # include excluded fields
    """
    import re
    import unicodedata
    from collections import defaultdict

    from data_donation_data.export import (
        build_alias_map,
        build_exclusion_set,
        build_table_alias_map,
    )

    config = _try_load_export_config()
    alias_map = {} if no_alias else build_alias_map(config)
    exclusion_set = {} if (show_all or no_alias) else build_exclusion_set(config)
    table_alias_map = {} if no_alias else build_table_alias_map(config)
    assignment_names: dict[str, str] = config.get("assignments", {})

    def _safe(s: str) -> str:
        """Sanitise *s* for use as a filename component."""
        return re.sub(r"[^\w\-]", "_", s).strip("_") or "unknown"

    with get_connection() as con:
        all_sources_list = list_sources(con)

        if not all_sources_list:
            console.print("[yellow]No data found. Run `ddd ingest` first.[/yellow]")
            return

        requested = sorted(sources) if sources else sorted(all_sources_list)
        unknown = [s for s in requested if s not in all_sources_list]
        if unknown:
            raise click.ClickException(f"Unknown source(s): {', '.join(unknown)}. Available: {', '.join(all_sources_list)}")

        n_files = 0

        for source in requested:
            console.print(f"[bold]{source}[/bold]")

            table_names = [
                r["table_name"]
                for r in con.execute(
                    "SELECT DISTINCT table_name FROM tables WHERE source = ? ORDER BY table_name",
                    (source,),
                ).fetchall()
            ]

            # Group original table names by their canonical alias.
            # Tables that share an alias are merged into one output directory.
            alias_to_tables: dict[str, list[str]] = defaultdict(list)
            for tbl in table_names:
                alias = table_alias_map.get(source, {}).get(tbl, tbl)
                alias_to_tables[alias].append(tbl)

            for tbl_alias, original_tables in sorted(alias_to_tables.items()):
                table_dir = output_dir / source / tbl_alias
                table_dir.mkdir(parents=True, exist_ok=True)

                phs = ",".join("?" * len(original_tables))

                # Build field_id → canonical column name (exclusions applied).
                field_rows = con.execute(
                    f"""
                    SELECT f.field_id, f.field, t.table_name
                    FROM fields f
                    JOIN tables t ON t.table_id = f.table_id
                    WHERE t.source = ? AND t.table_name IN ({phs})
                    ORDER BY f.field
                    """,
                    [source] + original_tables,
                ).fetchall()

                field_id_to_col: dict[int, str] = {}
                columns: list[str] = []
                seen: set[str] = set()

                for frow in field_rows:
                    field_nfc = unicodedata.normalize("NFC", frow["field"])
                    if field_nfc in exclusion_set.get(source, {}).get(frow["table_name"], frozenset()):
                        continue
                    col = alias_map.get(source, {}).get(frow["table_name"], {}).get(field_nfc, field_nfc)
                    field_id_to_col[frow["field_id"]] = col
                    if col not in seen:
                        seen.add(col)
                        columns.append(col)

                if not columns:
                    continue

                # Stream rows sorted so all rows for one (participant, assignment)
                # arrive together — lets us pivot and write without holding the
                # whole table in memory.
                data_cur = con.execute(
                    f"""
                    SELECT
                        p.participant,
                        fi.assignment,
                        fi.file_id,
                        d.row_index,
                        d.field_id,
                        d.value
                    FROM data d
                    JOIN files        fi ON fi.file_id      = d.file_id
                    JOIN fields       f  ON f.field_id       = d.field_id
                    JOIN tables       t  ON t.table_id       = f.table_id
                    JOIN participants p  ON p.participant_id = fi.participant_id
                    WHERE t.source = ? AND t.table_name IN ({phs})
                    ORDER BY p.participant, fi.assignment, fi.file_id, d.row_index
                    """,
                    [source] + original_tables,
                )

                # Pivot state for the current (participant, assignment) slice.
                cur_participant: str | None = None
                cur_assignment: str | None = None
                # (file_id, row_index) → {col: value}  — one dict per original row
                cur_rows: dict[tuple[int, int], dict[str, str]] = {}

                def _flush() -> None:
                    if cur_participant is None or not cur_rows:
                        return
                    asgn_str = str(cur_assignment) if cur_assignment is not None else ""
                    asgn_label = assignment_names.get(asgn_str, asgn_str)
                    fname = f"{_safe(asgn_label)}_{_safe(cur_participant)}.csv"
                    with (table_dir / fname).open("w", newline="", encoding="utf-8") as fh:
                        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
                        w.writeheader()
                        for row_dict in cur_rows.values():
                            w.writerow(row_dict)
                    nonlocal n_files
                    n_files += 1

                for dr in data_cur:
                    fid: int = dr["field_id"]
                    if fid not in field_id_to_col:
                        continue  # excluded field

                    participant: str = dr["participant"]
                    assignment: str | None = dr["assignment"]

                    if (participant, assignment) != (cur_participant, cur_assignment):
                        _flush()
                        cur_participant = participant
                        cur_assignment = assignment
                        cur_rows = {}

                    rkey = (int(dr["file_id"]), int(dr["row_index"]))
                    if rkey not in cur_rows:
                        cur_rows[rkey] = {}
                    cur_rows[rkey][field_id_to_col[fid]] = dr["value"]

                _flush()  # write the last participant

            console.print(f"  {source}: done")

    console.print(f"\n[green]✓[/green] Wrote [bold]{n_files:,}[/bold] files to [cyan]{output_dir}/[/cyan]")


# ---------------------------------------------------------------------------
# ddd participant-summary
# ---------------------------------------------------------------------------


@cli.command("participant-summary")
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    default=None,
    help="Write CSV to this file instead of printing a table.",
)
def cmd_participant_summary(output: Path | None) -> None:
    """Show participant counts per task and assignment.

    Displays the number of distinct participants for every (task, assignment)
    combination found in the database.  Human-readable task and assignment
    names are shown when defined in ``ddd_export.yaml``.

    With --output a CSV is written instead of the interactive table.
    """
    cfg = _try_load_export_config()
    task_names: dict[str, str] = cfg.get("tasks", {})
    assignment_names: dict[str, str] = cfg.get("assignments", {})

    with get_connection() as con:
        rows = task_assignment_summary(con, task_names=task_names, assignment_names=assignment_names)

    if not rows:
        console.print("[yellow]No data found. Run `ddd ingest` first.[/yellow]")
        return

    fieldnames = ["task", "task_name", "assignment", "assignment_name", "n_participants"]

    if output is not None:
        with output.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        console.print(f"[green]\u2713[/green] Wrote [bold]{len(rows):,}[/bold] rows to [cyan]{output}[/cyan]")
        return

    # Rich table output
    tbl = RichTable(show_header=True, header_style="bold cyan")
    tbl.add_column("Task", style="dim")
    tbl.add_column("Task name")
    tbl.add_column("Assignment", style="dim")
    tbl.add_column("Assignment name")
    tbl.add_column("Participants", justify="right", style="bold")

    for row in rows:
        tbl.add_row(
            row["task"] or "(none)",
            row["task_name"] or "",
            row["assignment"] or "(none)",
            row["assignment_name"] or "",
            str(row["n_participants"]),
        )

    console.print(tbl)


# ---------------------------------------------------------------------------
# ddd field-summary
# ---------------------------------------------------------------------------


@cli.command("field-summary")
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    default=None,
    help="Write CSV to this file instead of stdout.",
)
@click.option("--all", "show_all", is_flag=True, default=False, help="Include excluded fields (ignore .excluded in config).")
@click.option("--no-alias", "no_alias", is_flag=True, default=False, help="Use original field names (ignore alias mappings).")
def cmd_field_summary(output: Path | None, show_all: bool, no_alias: bool) -> None:
    """Export a long-format participant × field summary as CSV.

    For every combination of (source, participant, field) the CSV reports:

    \b
      source       — data source name
      participant  — participant identifier
      field        — field name (canonical; aliases applied if ddd_export.yaml exists)
      n_non_null   — number of non-missing values
      n_total      — total rows for this participant in this source
    """
    alias_map = _try_load_alias_map()
    exclusion_set = _try_load_exclusion_set()
    if show_all:
        exclusion_set = {}
    if no_alias:
        alias_map = {}
    with get_connection() as con:
        rows = participant_field_summary(con, alias_map=alias_map, exclusion_set=exclusion_set)

    if not rows:
        console.print("[yellow]No data found. Run `ddd ingest` first.[/yellow]")
        return

    fieldnames = ["source", "table", "participant", "field", "n_rows"]

    if output is None:
        writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    else:
        with output.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        console.print(f"[green]✓[/green] Wrote [bold]{len(rows):,}[/bold] rows to [cyan]{output}[/cyan]")


# ---------------------------------------------------------------------------
# ddd export
# ---------------------------------------------------------------------------


@cli.command("export")
@click.argument("sources", nargs=-1, required=False)
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    default="ddd_export_data.csv",
    show_default=True,
    help="Output file path.",
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["csv", "jsonl"]),
    default="csv",
    show_default=True,
    help="Output format.",
)
@click.option(
    "--all",
    "include_all",
    is_flag=True,
    default=False,
    help="Include fields marked as .excluded in ddd_export.yaml.",
)
@click.option(
    "--assignment",
    "-a",
    default=None,
    help="Filter rows to this assignment ID.",
)
@click.option(
    "--task",
    "-t",
    default=None,
    help="Filter rows to this task ID.",
)
def cmd_export(
    sources: tuple[str, ...],
    output: Path | None,
    fmt: str,
    include_all: bool,
    assignment: str | None,
    task: str | None,
) -> None:
    """Export ingested data to CSV or JSONL with aliases applied.

    Exports all sources by default. Pass one or more SOURCE names to
    limit the export to those sources only.

    Each row contains: source, participant, assignment, task,
    field (canonical alias), original_field, key, value.

    Fields listed under .excluded in ddd_export.yaml are skipped unless
    --all is given. Aliases from ddd_export.yaml are applied automatically
    when the config file exists.

    Examples:

    \b
      ddd export                          # all sources → ddd_export_data.csv
      ddd export facebook instagram       # specific sources
      ddd export --all                    # include excluded fields
      ddd export -o out.csv               # write to custom file
      ddd export --format jsonl -o out.jsonl
      ddd export -a 359                   # filter by assignment
      ddd export -t 940                   # filter by task
    """
    import json

    from data_donation_data.export import export_data

    config = _try_load_export_config()
    has_config = bool(config.get("sources"))

    src_list = list(sources) if sources else None

    with get_connection() as con:
        # Validate requested sources exist.
        if src_list:
            known = {row["source"] for row in con.execute("SELECT DISTINCT source FROM fields").fetchall()}
            bad = [s for s in src_list if s not in known]
            if bad:
                raise click.ClickException(f"Unknown source(s): {', '.join(bad)}. Known sources: {', '.join(sorted(known))}")

        columns, rows = export_data(
            con,
            src_list,
            include_all=include_all,
            config=config,
            assignment=assignment,
            task=task,
        )

    if not rows:
        click.echo("No rows matched the given filters.", err=True)
        return

    # ------------------------------------------------------------------
    # Write output
    # ------------------------------------------------------------------
    if not has_config:
        click.echo(
            "Note: no ddd_export.yaml found — run `ddd create-config` "
            "to generate one. Aliases and exclusions are not applied.",
            err=True,
        )
    elif include_all:
        click.echo(
            "--all: exporting all fields, including those marked .excluded.",
            err=True,
        )

    if fmt == "csv":
        with output.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
    else:  # jsonl
        with output.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    console.print(f"[green]✓[/green] Wrote [bold]{len(rows):,}[/bold] rows to [cyan]{output}[/cyan]")
    sources_in_output = sorted({r["source"] for r in rows if r["source"] is not None})
    console.print(f"   Sources: [cyan]{', '.join(sources_in_output)}[/cyan]")


# ---------------------------------------------------------------------------
# ddd export-aliases
# ---------------------------------------------------------------------------


@cli.command("export-aliases")
@click.option(
    "--output",
    "-o",
    default="ddd_aliases.csv",
    show_default=True,
    type=click.Path(dir_okay=False, writable=True, path_type=Path),
    help="Output CSV file.",
)
def cmd_export_aliases(output: Path) -> None:
    """Export a CSV mapping every field to its canonical alias.

    Writes one row per field in the database with columns:

    \b
      field_id       — internal field identifier
      source         — data source (e.g. facebook, youtube)
      table          — canonical table name (after .alias from ddd_export.yaml)
      original_field — raw field name as stored in the DB
      alias          — canonical field name (after field alias from ddd_export.yaml)

    This is useful for auditing which fields map to which aliases before
    running a full export, and for cross-referencing field_id values that
    appear in exported data.

    Run `ddd create-config` first to generate ddd_export.yaml.
    """
    import unicodedata

    from data_donation_data.export import build_alias_map, build_table_alias_map, load_export_yaml

    # Load config (gracefully absent).
    try:
        config = load_export_yaml(DEFAULT_YAML_PATH)
    except (FileNotFoundError, ValueError):
        config = {"tasks": {}, "assignments": {}, "sources": {}}
        console.print(
            "[yellow]Warning:[/yellow] ddd_export.yaml not found — "
            "aliases will not be applied. Run `ddd create-config` first.",
            err=True,
        )

    alias_map = build_alias_map(config)
    table_alias_map = build_table_alias_map(config)

    with get_connection() as con:
        rows = con.execute(
            """
            SELECT f.field_id, t.source, t.table_name, f.field
            FROM fields f
            JOIN tables t ON t.table_id = f.table_id
            ORDER BY t.source, t.table_name, f.field
            """
        ).fetchall()

    with output.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["field_id", "source", "table", "original_field", "alias"],
        )
        writer.writeheader()
        for row in rows:
            src: str = row["source"]
            tbl: str = row["table_name"]
            field: str = row["field"]
            field_nfc = unicodedata.normalize("NFC", field)

            canonical_table = table_alias_map.get(src, {}).get(tbl, tbl)
            canonical_field = alias_map.get(src, {}).get(tbl, {}).get(field_nfc, field_nfc)

            writer.writerow(
                {
                    "field_id": row["field_id"],
                    "source": src,
                    "table": canonical_table,
                    "original_field": field,
                    "alias": canonical_field,
                }
            )

    console.print(f"[green]✓[/green] Wrote [bold]{len(rows):,}[/bold] field aliases to [cyan]{output}[/cyan]")


# ---------------------------------------------------------------------------
# ddd remove
# ---------------------------------------------------------------------------


@cli.command("remove")
@click.option("--assignment", "-a", default=None, help="Remove data with this assignment ID.")
@click.option("--task", "-t", default=None, help="Remove data with this task ID.")
@click.option("--participant", "-p", default=None, help="Remove data for this participant.")
@click.option("--yes", "-y", is_flag=True, default=False, help="Skip confirmation prompt.")
def cmd_remove(
    assignment: str | None,
    task: str | None,
    participant: str | None,
    yes: bool,
) -> None:
    """Remove ingested data by assignment, task, and/or participant.

    At least one filter must be supplied.  Filters are combined with AND,
    so only rows matching ALL supplied values are removed.

    \b
    Examples:
      ddd remove --assignment 1
      ddd remove --task 10
      ddd remove --assignment 1 --task 10
      ddd remove --participant abc123

    After deletion, orphaned participants, their queued URLs, and any
    fields with no remaining data are automatically cleaned up.
    Ingested-file records matching the filters are also removed so the
    files can be re-ingested if needed.
    """
    if not any([assignment, task, participant]):
        raise click.ClickException("Provide at least one of --assignment, --task, --participant.")

    # Build a parameterised WHERE clause against the files+participants join.
    conditions: list[str] = []
    params: list[str] = []
    if assignment is not None:
        conditions.append("fi.assignment = ?")
        params.append(assignment)
    if task is not None:
        conditions.append("fi.task = ?")
        params.append(task)
    if participant is not None:
        conditions.append("p.participant = ?")
        params.append(participant)

    where = " AND ".join(conditions)
    join = "FROM data d JOIN files fi ON fi.file_id = d.file_id JOIN participants p ON p.participant_id = fi.participant_id"

    with get_connection() as con:
        n_rows = con.execute(f"SELECT COUNT(*) {join} WHERE {where}", params).fetchone()[0]

        if n_rows == 0:
            console.print("[yellow]No matching data found.[/yellow]")
            return

        # Gather preview details.
        affected = con.execute(
            f"""
            SELECT fi.assignment, fi.task, p.participant,
                   COUNT(*) AS n_rows
            {join}
            WHERE {where}
            GROUP BY fi.assignment, fi.task, p.participant
            ORDER BY fi.assignment, fi.task, p.participant
            """,
            params,
        ).fetchall()

        tbl = RichTable(show_header=True, header_style="bold yellow")
        tbl.add_column("Assignment")
        tbl.add_column("Task")
        tbl.add_column("Participant")
        tbl.add_column("Rows", justify="right")
        for row in affected:
            tbl.add_row(
                row["assignment"] or "(none)",
                row["task"] or "(none)",
                row["participant"] or "(none)",
                f"{row['n_rows']:,}",
            )
        console.print(tbl)
        console.print(
            f"[bold yellow]This will permanently delete "
            f"{n_rows:,} row(s) across "
            f"{len({r['participant'] for r in affected})} participant(s).[/bold yellow]"
        )

        if not yes:
            click.confirm("Continue?", abort=True)

        # Remember which participants are affected before deleting.
        affected_pids: list[int] = [
            r[0] for r in con.execute(f"SELECT DISTINCT fi.participant_id {join} WHERE {where}", params).fetchall()
        ]

        # Delete matching data rows.
        con.execute(
            f"DELETE FROM data WHERE data_id IN (SELECT d.data_id {join} WHERE {where})",
            params,
        )

        # Remove ingested_file records so affected files can be re-ingested.
        # Each filter becomes a LIKE pattern against the filename stem.
        file_conditions: list[str] = []
        file_params: list[str] = []
        if assignment is not None:
            # assignment is always the first key in the stem.
            file_conditions.append("file_stem LIKE ?")
            file_params.append(f"assignment={assignment}_%")
        if task is not None:
            file_conditions.append("file_stem LIKE ?")
            file_params.append(f"%_task={task}_%")
        if participant is not None:
            file_conditions.append("file_stem LIKE ?")
            file_params.append(f"%_participant={participant}_%")
        n_files = con.execute(
            f"DELETE FROM ingested_files WHERE {' AND '.join(file_conditions)}",
            file_params,
        ).rowcount

        # Clean up orphaned files, then participants with no files left.
        con.execute("DELETE FROM files WHERE file_id NOT IN (SELECT DISTINCT file_id FROM data)")
        if affected_pids:
            ph = ",".join("?" * len(affected_pids))
            orphaned_pids: list[int] = [
                r[0]
                for r in con.execute(
                    f"SELECT participant_id FROM participants "
                    f"WHERE participant_id IN ({ph}) "
                    f"AND participant_id NOT IN (SELECT DISTINCT participant_id FROM files)",
                    affected_pids,
                ).fetchall()
            ]
            if orphaned_pids:
                oph = ",".join("?" * len(orphaned_pids))
                con.execute(f"DELETE FROM urls         WHERE participant_id IN ({oph})", orphaned_pids)
                con.execute(f"DELETE FROM participants WHERE participant_id IN ({oph})", orphaned_pids)

        # Clean up fields and tables that now have no data at all.
        n_fields = con.execute("DELETE FROM fields WHERE field_id NOT IN (SELECT DISTINCT field_id FROM data)").rowcount
        con.execute("DELETE FROM tables WHERE table_id NOT IN (SELECT DISTINCT table_id FROM fields)")

        con.commit()

    parts = [f"[bold]{n_rows:,}[/bold] data row(s)"]
    if n_files:
        parts.append(f"[bold]{n_files}[/bold] file record(s)")
    if orphaned_pids:
        parts.append(f"[bold]{len(orphaned_pids)}[/bold] orphaned participant(s)")
    if n_fields:
        parts.append(f"[bold]{n_fields}[/bold] unused field(s)")
    console.print("[green]✓[/green] Removed " + ", ".join(parts) + ".")


# ---------------------------------------------------------------------------


@cli.command("create-config")
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    default=None,
    help=f"Path for the YAML file. Defaults to {DEFAULT_YAML_PATH}.",
)
def cmd_create_config(output: Path | None) -> None:
    """Create or update the ddd_export.yaml configuration file.

    Writes a YAML file listing every (source, field) found in the database.
    Open the file in a text editor to customise it — all settings are
    preserved when you re-run this command after ingesting more data.

    Field settings (one block per source):

    \b
      include — true/false, whether to include the field in exports
      aliases — alternative field names treated as this one in summaries

    Optional top-level name mappings (used by `ddd participant-summary`):

    \b
      tasks:
        "10": YouTube Watch History
        "20": Chrome Browsing History

    \b
      assignments:
        "1": Pilot Round
        "2": Main Study
    """
    yaml_path = output or DEFAULT_YAML_PATH

    with get_connection() as con:
        sources = list_sources(con)
        if not sources:
            raise click.ClickException("No data found. Run `ddd ingest` before initialising the export config.")
        _, n_added, n_total = init_export_yaml(con, yaml_path)

    if n_added:
        console.print(
            f"[green]✓[/green] [bold]{n_added}[/bold] new field(s) added — "
            f"[bold]{n_total}[/bold] total in [cyan]{yaml_path}[/cyan]"
        )
    else:
        console.print(f"[green]✓[/green] Up to date — [bold]{n_total}[/bold] field(s) in [cyan]{yaml_path}[/cyan]")


# ---------------------------------------------------------------------------
# ddd scrape
# ---------------------------------------------------------------------------


@cli.group("scrape")
def scrape_group() -> None:
    """Commands for URL scraping."""


@scrape_group.command("run")
@click.option("--limit", "-n", default=None, type=int, help="Stop after N URLs.")
@click.option(
    "--delay-min",
    default=0.5,
    show_default=True,
    type=float,
    help="Seconds to wait between requests to the same domain.",
)
@click.option(
    "--delay-max",
    default=None,
    type=float,
    help="Upper bound for the per-domain delay (defaults to --delay-min, i.e. a fixed wait).",
)
@click.option(
    "--concurrency",
    "-j",
    default=10,
    show_default=True,
    type=int,
    help="Maximum number of simultaneous in-flight requests.",
)
def cmd_scrape_run(
    limit: int | None,
    delay_min: float,
    delay_max: float | None,
    concurrency: int,
) -> None:
    """Scrape all pending URLs concurrently.

    URLs are queued automatically during `ddd ingest` (any field value
    starting with http:// or https:// is added to the urls table).

    Requests to different domains run in parallel (up to --concurrency).
    Requests to the same domain are serialised with a fixed per-domain delay
    (or a random delay in [--delay-min, --delay-max] if --delay-max is set).
    """
    delay_max = delay_max if delay_max is not None else delay_min
    from rich.progress import MofNCompleteColumn, Progress, SpinnerColumn, TextColumn
    from rich.table import Column

    with get_connection() as con:
        pending_count: int = con.execute("SELECT COUNT(DISTINCT url) FROM urls WHERE status = 'pending'").fetchone()[0]

    if pending_count == 0:
        console.print("[yellow]No pending URLs to scrape.[/yellow]")
        return

    effective_total = min(pending_count, limit) if limit else pending_count
    console.print(f"Scraping [bold]{effective_total:,}[/bold] pending URL(s) (of {pending_count:,} distinct)…")

    with Progress(
        SpinnerColumn(),
        MofNCompleteColumn(),
        TextColumn("[progress.description]{task.description}"),
        TextColumn(
            "{task.fields[url]}",
            table_column=Column(no_wrap=True, overflow="crop", ratio=1),
        ),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task(f"[green]{0:5.1f}% failed[/green]", total=effective_total, url="")

        _counts = {"failed": 0}

        def _on_progress(url: str, status: str, idx: int, total: int) -> None:
            if status != "success":
                _counts["failed"] += 1
            pct = _counts["failed"] / idx * 100 if idx else 0.0
            pct_colour = "red" if _counts["failed"] else "green"
            url_colour = "white" if status == "success" else "red"
            progress.update(
                task,
                advance=1,
                description=f"[{pct_colour}]{pct:5.1f}% failed[/{pct_colour}]",
                url=f"[{url_colour}]{url}[/{url_colour}]",
            )

        with get_connection() as con:
            counts = scrape_pending(
                con,
                delay_min=delay_min,
                delay_max=delay_max,
                concurrency=concurrency,
                limit=limit,
                on_progress=_on_progress,
            )

    console.print(f"\n[green]✓ {counts['success']:,} success[/green]  [red]✗ {counts['failed']:,} failed[/red]")


@scrape_group.command("status")
def cmd_scrape_status() -> None:
    """Show scraping progress across all queued URLs."""
    with get_connection() as con:
        try:
            rows = con.execute(
                """
                SELECT status,
                       COUNT(url)                  AS n_raw,
                       COUNT(DISTINCT normalized_url) AS n_normalized
                  FROM urls
                 GROUP BY status
                 ORDER BY status
                """
            ).fetchall()
        except Exception as exc:  # noqa: BLE001
            raise click.ClickException(str(exc)) from exc

    if not rows:
        console.print("[yellow]No URLs queued yet. Run `ddd ingest` first.[/yellow]")
        return

    t = RichTable(title="URL scraping status", show_lines=False)
    t.add_column("Status", style="cyan")
    t.add_column("Raw URLs", justify="right")
    t.add_column("Normalized (unique)", justify="right")
    total_raw = sum(r["n_raw"] for r in rows)
    total_norm = sum(r["n_normalized"] for r in rows)
    for r in rows:
        colour = {"success": "green", "failed": "red", "pending": "yellow"}.get(r["status"], "white")
        pct = r["n_normalized"] / total_norm * 100 if total_norm else 0
        t.add_row(
            f"[{colour}]{r['status']}[/{colour}]",
            f"{r['n_raw']:,}",
            f"{r['n_normalized']:,} ({pct:.1f}%)",
        )
    t.add_section()
    t.add_row("[bold]Total[/bold]", f"[bold]{total_raw:,}[/bold]", f"[bold]{total_norm:,}[/bold]")
    console.print(t)
    if total_raw > total_norm:
        console.print(
            f"[dim]{total_raw - total_norm:,} raw URL(s) collapsed via normalisation "
            f"({total_norm:,} actual HTTP requests needed)[/dim]"
        )
