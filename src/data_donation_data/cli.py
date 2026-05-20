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
ddd extract <table> <col>... [--output FILE] [--format csv|jsonl] [--where CLAUSE]
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
from data_donation_data.extract import extract_to_csv, extract_to_jsonl
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


def _try_load_alias_map() -> dict[str, dict[str, str]]:
    """
    Load alias mappings from ``ddd_export.yaml`` if it exists.

    Returns an empty dict (no aliases) when the file is absent or invalid,
    so callers never have to handle a missing config file specially.
    """
    try:
        from data_donation_data.export import build_alias_map, load_export_yaml

        return build_alias_map(load_export_yaml(DEFAULT_YAML_PATH))
    except (FileNotFoundError, ValueError):
        return {}


def _try_load_export_config() -> dict:
    """
    Load the full export config from ``ddd_export.yaml`` if it exists.

    Returns ``{"tasks": {}, "assignments": {}, "sources": {}}`` when the
    file is absent or invalid.
    """
    try:
        from data_donation_data.export import load_export_yaml

        return load_export_yaml(DEFAULT_YAML_PATH)
    except (FileNotFoundError, ValueError):
        return {"tasks": {}, "assignments": {}, "sources": {}}


def _try_load_exclusion_set() -> dict[str, frozenset[str]]:
    """
    Load per-source exclusion sets from ``ddd_export.yaml`` if it exists.
    Returns an empty dict when the file is absent or invalid.
    """
    try:
        from data_donation_data.export import build_exclusion_set, load_export_yaml

        return build_exclusion_set(load_export_yaml(DEFAULT_YAML_PATH))
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
    t.add_column("Fields", justify="right")
    t.add_column("Participants", justify="right")
    t.add_column("Observations", justify="right")
    t.add_column("Non-null", justify="right")
    t.add_column("Non-null %", justify="right")

    for row in rows:
        t.add_row(
            row["source"],
            f"{row['n_fields']:,}",
            f"{row['n_participants']:,}",
            f"{row['n_total']:,}",
            f"{row['n_non_null']:,}",
            f"{row['pct_non_null']:.1f}%",
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
    with get_connection() as con:
        try:
            rows = summarise_source(con, source, alias_map=alias_map, exclusion_set=exclusion_set)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc

    if not rows:
        console.print(f"[yellow]No data found for source '{source}'.[/yellow]")
        return

    total_rows = sum(r["n_total"] for r in rows)
    alias_note = " [dim](aliases applied)[/dim]" if alias_map.get(source) else ""
    console.print(
        f"\n[bold]Source:[/bold] [cyan]{source}[/cyan]  "
        f"[bold]Fields:[/bold] {len(rows)}  "
        f"[bold]Total observations:[/bold] {total_rows:,}"
        f"{alias_note}\n"
    )

    t = RichTable(show_lines=True)
    t.add_column("Field", style="cyan", no_wrap=True)
    t.add_column("Observations", justify="right")
    t.add_column("Non-null", justify="right")
    t.add_column("Non-null %", justify="right")
    t.add_column("Participants", justify="right")

    for row in rows:
        t.add_row(
            row["field"],
            f"{row['n_total']:,}",
            f"{row['n_non_null']:,}",
            f"{row['pct_non_null']:.1f}%",
            f"{row['n_participants']:,}",
        )

    console.print(t)


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

    fieldnames = ["source", "participant", "field", "n_non_null", "n_total"]

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
# ddd extract
# ---------------------------------------------------------------------------


@cli.command("extract")
@click.argument("table")
@click.argument("columns", nargs=-1, required=True)
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    default=None,
    help="Output file path. Defaults to stdout.",
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
    "--where",
    default=None,
    help="Optional SQL WHERE clause (without the WHERE keyword).",
)
def cmd_extract(
    table: str,
    columns: tuple[str, ...],
    output: Path | None,
    fmt: str,
    where: str | None,
) -> None:
    """Extract COLUMNS from TABLE to CSV or JSONL."""
    col_list = list(columns)
    with get_connection() as con:
        try:
            if fmt == "csv":
                n = extract_to_csv(con, table, col_list, output=output, where=where)
            else:
                n = extract_to_jsonl(con, table, col_list, output=output, where=where)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc

    if output:
        console.print(f"[green]✓[/green] Wrote [bold]{n:,}[/bold] rows to [cyan]{output}[/cyan]")


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

    # Build a parameterised WHERE clause that works with the data+participants join.
    conditions: list[str] = []
    params: list[str] = []
    if assignment is not None:
        conditions.append("d.assignment = ?")
        params.append(assignment)
    if task is not None:
        conditions.append("d.task = ?")
        params.append(task)
    if participant is not None:
        conditions.append("p.participant = ?")
        params.append(participant)

    where = " AND ".join(conditions)
    join = "FROM data d JOIN participants p ON d.participant_id = p.participant_id"

    with get_connection() as con:
        n_rows = con.execute(f"SELECT COUNT(*) {join} WHERE {where}", params).fetchone()[0]

        if n_rows == 0:
            console.print("[yellow]No matching data found.[/yellow]")
            return

        # Gather preview details.
        affected = con.execute(
            f"""
            SELECT d.assignment, d.task, p.participant,
                   COUNT(*) AS n_rows
            {join}
            WHERE {where}
            GROUP BY d.assignment, d.task, p.participant
            ORDER BY d.assignment, d.task, p.participant
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
            r[0] for r in con.execute(f"SELECT DISTINCT p.participant_id {join} WHERE {where}", params).fetchall()
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

        # Clean up participants that now have no data at all.
        orphaned_pids: list[int] = []
        if affected_pids:
            ph = ",".join("?" * len(affected_pids))
            orphaned_pids: list[int] = [
                r[0]
                for r in con.execute(
                    f"SELECT participant_id FROM participants "
                    f"WHERE participant_id IN ({ph}) "
                    f"AND participant_id NOT IN (SELECT DISTINCT participant_id FROM data)",
                    affected_pids,
                ).fetchall()
            ]
            if orphaned_pids:
                oph = ",".join("?" * len(orphaned_pids))
                con.execute(f"DELETE FROM urls         WHERE participant_id IN ({oph})", orphaned_pids)
                con.execute(f"DELETE FROM participants WHERE participant_id IN ({oph})", orphaned_pids)

        # Clean up fields that now have no data at all.
        n_fields = con.execute("DELETE FROM fields WHERE field_id NOT IN (SELECT DISTINCT field_id FROM data)").rowcount

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
