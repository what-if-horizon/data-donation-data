"""
Summary helpers for the donation data schema.

Public API
----------
list_tables(con)                       → list[str]   — all SQLite tables
list_sources(con)                      → list[str]   — distinct sources
summarise_all_sources(con)             → list[dict]  — source-level stats
summarise_source(con, src, ...)        → list[dict]  — per-table/field stats
participant_field_summary(con, ...)    → list[dict]  — participant × field counts
task_assignment_summary(con, ...)      → list[dict]  — participant counts per task/assignment
"""

from __future__ import annotations

import sqlite3
import unicodedata
from typing import Any


def list_tables(con: sqlite3.Connection) -> list[str]:
    """Return names of all SQLite tables (excluding internals)."""
    rows = con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    return [r[0] for r in rows]


def list_sources(con: sqlite3.Connection) -> list[str]:
    """Return distinct source names."""
    rows = con.execute("SELECT DISTINCT source FROM tables ORDER BY source").fetchall()
    return [r[0] for r in rows]


def summarise_all_sources(con: sqlite3.Connection) -> list[dict]:
    """
    Return source-level statistics.

    Each dict contains ``source``, ``n_tables``, ``n_fields``,
    ``n_participants``, ``n_rows``.

    Implementation notes
    --------------------
    The ``data`` table can be very large (100M+ rows).  We avoid a single
    monolithic join across all of it by splitting into two cheap queries:

    1. **Metadata query** – counts tables and fields from the tiny
       ``tables`` / ``fields`` tables (no ``data`` scan at all).
    2. **Stats query** – uses a CTE that first DISTINCT-deduplicates
       ``data`` down to its ``(field_id, file_id)`` pairs (a much smaller
       set, ≈ n_fields × n_files) before joining to ``files`` for
       participant counts.  The ``idx_data_cover`` index on
       ``data(field_id, file_id)`` means this step is a covering-index
       scan with no heap access.
    """
    # --- 1. metadata: n_tables and n_fields per source (no data scan) ---
    meta_rows = con.execute(
        """
        SELECT
            t.source,
            COUNT(DISTINCT t.table_id) AS n_tables,
            COUNT(DISTINCT f.field_id) AS n_fields
        FROM tables t
        JOIN fields f ON f.table_id = t.table_id
        GROUP BY t.source
        """
    ).fetchall()

    meta: dict[str, dict] = {}
    for row in meta_rows:
        meta[row["source"]] = {
            "source": row["source"],
            "n_tables": row["n_tables"],
            "n_fields": row["n_fields"],
            "n_participants": 0,
            "n_rows": 0,
        }

    # --- 2. stats: n_participants and n_rows per source ---
    # The CTE collapses 122M+ data rows to distinct (field_id, file_id)
    # pairs first (using idx_data_cover), then joins the tiny fields /
    # tables / files tables to resolve source and participant.
    stats_rows = con.execute(
        """
        WITH deduped AS (
            SELECT DISTINCT field_id, file_id
            FROM data
        )
        SELECT
            t.source,
            COUNT(DISTINCT fi.participant_id) AS n_participants
        FROM deduped d
        JOIN fields f  ON f.field_id  = d.field_id
        JOIN tables t  ON t.table_id  = f.table_id
        JOIN files  fi ON fi.file_id  = d.file_id
        GROUP BY t.source
        """
    ).fetchall()

    # n_rows is a simple COUNT(*) — reuse a separate lightweight query
    rows_rows = con.execute(
        """
        SELECT t.source, COUNT(*) AS n_rows
        FROM data d
        JOIN fields f ON f.field_id = d.field_id
        JOIN tables t ON t.table_id = f.table_id
        GROUP BY t.source
        """
    ).fetchall()

    for row in stats_rows:
        src = row["source"]
        if src in meta:
            meta[src]["n_participants"] = row["n_participants"]

    for row in rows_rows:
        src = row["source"]
        if src in meta:
            meta[src]["n_rows"] = row["n_rows"]

    return sorted(meta.values(), key=lambda r: r["source"])


def summarise_source(
    con: sqlite3.Connection,
    source: str,
    alias_map: dict[str, dict[str, dict[str, str]]] | None = None,
    exclusion_set: dict[str, dict[str, frozenset[str]]] | None = None,
    table_alias_map: dict[str, dict[str, str]] | None = None,
) -> list[dict]:
    """
    Return per-table, per-field statistics for *source*.

    Returns a list of table dicts, each containing:

    * ``table``  — table name (canonical after .alias; merged if multiple tables share one)
    * ``fields`` — list of field dicts:

        * ``field``         — field name (canonical after alias)
        * ``n_participants`` — distinct participants with this field
        * ``n_rows``         — total non-null cell values stored

    Parameters
    ----------
    alias_map:
        ``{source: {table_name: {original_field: canonical_field}}}``
    exclusion_set:
        ``{source: {table_name: frozenset(excluded_fields)}}``

    Raises
    ------
    ValueError
        If *source* is not found.
    """
    available = list_sources(con)
    if source not in available:
        raise ValueError(f"Source '{source}' not found. Available: {', '.join(available) or '(none)'}")

    # --- 1. metadata: field names and table names (no data scan) ---
    meta_rows = con.execute(
        """
        SELECT f.field_id, f.field, t.table_name
        FROM fields f
        JOIN tables t ON t.table_id = f.table_id
        WHERE t.source = ?
        ORDER BY t.table_name, f.field
        """,
        (source,),
    ).fetchall()

    # --- 2. n_participants and n_rows per field ---
    # per_file groups by (field_id, file_id) — matching idx_data_cover's sort
    # order, so the CTE streams without a temp sort.  The outer aggregation
    # then works on ≈111K pairs rather than 122M+ raw rows.
    stats_rows = con.execute(
        """
        WITH src_fields AS (
            SELECT f.field_id
            FROM fields f
            JOIN tables t ON t.table_id = f.table_id
            WHERE t.source = ?
        ),
        per_file AS (
            SELECT d.field_id, d.file_id, COUNT(*) AS n_rows
            FROM data d
            WHERE d.field_id IN (SELECT field_id FROM src_fields)
            GROUP BY d.field_id, d.file_id
        )
        SELECT
            pf.field_id,
            SUM(pf.n_rows)                   AS n_rows,
            COUNT(DISTINCT fi.participant_id) AS n_participants
        FROM per_file pf
        JOIN files fi ON fi.file_id = pf.file_id
        GROUP BY pf.field_id
        """,
        (source,),
    ).fetchall()

    stats: dict[int, sqlite3.Row] = {row["field_id"]: row for row in stats_rows}

    src_aliases = (alias_map or {}).get(source, {})
    src_exclusions = (exclusion_set or {}).get(source, {})

    # Group by table, apply alias + exclusion.
    tables_out: dict[str, dict[str, dict[str, Any]]] = {}

    src_table_aliases: dict[str, str] = table_alias_map.get(source, {}) if table_alias_map else {}

    for row in meta_rows:
        field_id = row["field_id"]
        tbl: str = row["table_name"]
        canonical_table: str = src_table_aliases.get(tbl, tbl)
        orig_nfc = unicodedata.normalize("NFC", row["field"])

        # Skip excluded fields (keyed by original table name).
        if orig_nfc in src_exclusions.get(tbl, frozenset()):
            continue

        # Field alias lookup uses original table name (YAML is keyed that way).
        canonical_field = src_aliases.get(tbl, {}).get(orig_nfc, orig_nfc)

        if canonical_table not in tables_out:
            tables_out[canonical_table] = {}

        field_stats = stats.get(field_id)
        n_participants = field_stats["n_participants"] if field_stats else 0
        n_rows = field_stats["n_rows"] if field_stats else 0

        if canonical_field in tables_out[canonical_table]:
            tables_out[canonical_table][canonical_field]["n_participants"] += n_participants
            tables_out[canonical_table][canonical_field]["n_rows"] += n_rows
        else:
            tables_out[canonical_table][canonical_field] = {
                "field": canonical_field,
                "n_participants": n_participants,
                "n_rows": n_rows,
            }

    return [
        {
            "table": tbl,
            "fields": sorted(fields.values(), key=lambda f: f["field"]),
        }
        for tbl, fields in sorted(tables_out.items())
    ]


def task_assignment_summary(
    con: sqlite3.Connection,
    task_names: dict[str, str] | None = None,
    assignment_names: dict[str, str] | None = None,
) -> list[dict]:
    """
    Return participant counts grouped by (task, assignment).

    Each dict: ``task``, ``task_name``, ``assignment``, ``assignment_name``,
    ``n_participants``.
    """
    rows = con.execute(
        """
        SELECT
            task,
            assignment,
            COUNT(DISTINCT participant_id) AS n_participants
        FROM files
        GROUP BY task, assignment
        ORDER BY task, assignment
        """
    ).fetchall()

    task_names = task_names or {}
    assignment_names = assignment_names or {}

    result = []
    for row in rows:
        task_id = row["task"] or ""
        assignment_id = row["assignment"] or ""
        result.append(
            {
                "task": task_id,
                "task_name": task_names.get(str(task_id), ""),
                "assignment": assignment_id,
                "assignment_name": assignment_names.get(str(assignment_id), ""),
                "n_participants": row["n_participants"],
            }
        )
    return result


def participant_field_summary(
    con: sqlite3.Connection,
    alias_map: dict[str, dict[str, dict[str, str]]] | None = None,
    exclusion_set: dict[str, dict[str, frozenset[str]]] | None = None,
) -> list[dict]:
    """
    Long-format summary: one row per (source, table, participant, field).

    Each dict: ``source``, ``table``, ``participant``, ``field``, ``n_rows``.
    """
    rows = con.execute(
        """
        SELECT
            t.source,
            t.table_name,
            p.participant,
            f.field,
            COUNT(*) AS n_rows
        FROM data d
        JOIN files        fi ON fi.file_id       = d.file_id
        JOIN fields       f  ON d.field_id        = f.field_id
        JOIN tables       t  ON f.table_id         = t.table_id
        JOIN participants p  ON fi.participant_id   = p.participant_id
        GROUP BY f.field_id, fi.participant_id
        ORDER BY t.source, t.table_name, p.participant, f.field
        """
    ).fetchall()

    aggregated: dict[tuple, dict] = {}
    for row in rows:
        src = row["source"]
        tbl = row["table_name"]
        orig_nfc = unicodedata.normalize("NFC", row["field"])

        # Skip excluded.
        if orig_nfc in (exclusion_set or {}).get(src, {}).get(tbl, frozenset()):
            continue

        canonical = (alias_map or {}).get(src, {}).get(tbl, {}).get(orig_nfc, orig_nfc)
        key = (src, tbl, row["participant"] or "", canonical)

        if key in aggregated:
            aggregated[key]["n_rows"] += row["n_rows"]
        else:
            aggregated[key] = {
                "source": src,
                "table": tbl,
                "participant": row["participant"],
                "field": canonical,
                "n_rows": row["n_rows"],
            }

    return sorted(
        aggregated.values(),
        key=lambda r: (r["source"], r["table"], r["participant"] or "", r["field"]),
    )
