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
ddd extract <table> <col>... [--output FILE] [--format csv|jsonl] [--where CLAUSE]
ddd export init [--output FILE]
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
    summarise_source,
    summarise_table,
)

console = Console()


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
@click.argument("table")
def cmd_summarise(table: str) -> None:
    """Print a column-level summary of TABLE.

    TABLE is a raw SQLite table name (e.g. data, fields, participants,
    urls, url_metadata).  For a field-level summary of a donation source
    use `ddd summarise-source`.
    """
    with get_connection() as con:
        try:
            summary = summarise_table(con, table)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc

    console.print(f"\n[bold]Table:[/bold] [cyan]{summary.table}[/cyan]  [bold]Rows:[/bold] {summary.row_count:,}\n")

    t = RichTable(show_lines=True)
    t.add_column("Column", style="cyan", no_wrap=True)
    t.add_column("Non-null", justify="right")
    t.add_column("Null", justify="right")
    t.add_column("Null %", justify="right")
    t.add_column("Distinct", justify="right")
    t.add_column("Samples", style="dim")

    for col in summary.columns:
        t.add_row(
            col.name,
            f"{col.non_null:,}",
            f"{col.null_count:,}",
            f"{col.null_pct:.1f}%",
            f"{col.distinct:,}",
            " | ".join(col.samples[:3]),
        )

    console.print(t)


# ---------------------------------------------------------------------------
# ddd summarise-source
# ---------------------------------------------------------------------------


@cli.command("summarise-source")
@click.argument("source")
def cmd_summarise_source(source: str) -> None:
    """Print a field-level summary for a donation SOURCE.

    SOURCE is a data source name as returned by `ddd sources`
    (e.g. youtube, google_chrome).
    """
    with get_connection() as con:
        try:
            rows = summarise_source(con, source)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc

    if not rows:
        console.print(f"[yellow]No data found for source '{source}'.[/yellow]")
        return

    total_rows = sum(r["n_total"] for r in rows)
    console.print(
        f"\n[bold]Source:[/bold] [cyan]{source}[/cyan]  "
        f"[bold]Fields:[/bold] {len(rows)}  "
        f"[bold]Total observations:[/bold] {total_rows:,}\n"
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
def cmd_field_summary(output: Path | None) -> None:
    """Export a long-format participant × field summary as CSV.

    For every combination of (source, participant, field) the CSV reports:

    \b
      source       — data source name
      participant  — participant identifier
      field        — field name
      n_non_null   — number of non-missing values
      n_total      — total rows for this participant in this source
    """
    with get_connection() as con:
        rows = participant_field_summary(con)

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
# ddd export
# ---------------------------------------------------------------------------


@cli.group("export")
def export_group() -> None:
    """Commands for configuring and running data exports."""


@export_group.command("init")
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    default=None,
    help=f"Path for the YAML file. Defaults to {DEFAULT_YAML_PATH}.",
)
def cmd_export_init(output: Path | None) -> None:
    """Create or update ddd_export.yaml with all available fields.

    Every (source, field) combination found in the database is listed with
    a true/false toggle.  Existing toggles are preserved; newly discovered
    fields are added with true (included) by default.

    Edit the file manually to toggle fields on or off, then re-run this
    command after ingesting more data to pick up any new fields.
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
    help="Minimum seconds between requests to the same domain.",
)
@click.option(
    "--delay-max",
    default=2.5,
    show_default=True,
    type=float,
    help="Maximum seconds between requests to the same domain.",
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
    delay_max: float,
    concurrency: int,
) -> None:
    """Scrape all pending URLs concurrently.

    URLs are queued automatically during `ddd ingest` (any field value
    starting with http:// or https:// is added to the urls table).

    Requests to different domains run in parallel (up to --concurrency).
    Requests to the same domain are serialised with a per-domain random delay.
    """
    from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn

    with get_connection() as con:
        pending_count: int = con.execute("SELECT COUNT(DISTINCT url) FROM urls WHERE status = 'pending'").fetchone()[0]

    if pending_count == 0:
        console.print("[yellow]No pending URLs to scrape.[/yellow]")
        return

    effective_total = min(pending_count, limit) if limit else pending_count
    console.print(f"Scraping [bold]{effective_total:,}[/bold] pending URL(s) (of {pending_count:,} distinct)…")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Scraping…", total=effective_total)

        def _on_progress(url: str, status: str, idx: int, total: int) -> None:
            colour = "green" if status == "success" else "red"
            short_url = url[:60] + "…" if len(url) > 60 else url
            progress.update(
                task,
                advance=1,
                description=f"[{colour}]{status}[/{colour}] {short_url}",
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
