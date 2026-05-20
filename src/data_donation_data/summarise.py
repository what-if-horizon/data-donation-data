"""
Summary helpers for the donation data schema.

Public API
----------
list_tables(con)               → list[str]      — all SQLite tables
list_sources(con)              → list[str]      — distinct sources in the fields table
summarise_all_sources(con)     → list[dict]     — source-level rollup stats
summarise_table(con, table)    → TableSummary   — raw per-column stats for any table
summarise_source(con, src)     → list[dict]     — per-field stats for a donation source
participant_field_summary(con) → list[dict]     — long-format source×participant×field counts
"""

from __future__ import annotations

import sqlite3
import unicodedata
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Generic table summary (works on any SQLite table)
# ---------------------------------------------------------------------------


@dataclass
class ColumnSummary:
    name: str
    total: int
    non_null: int
    null_count: int
    null_pct: float
    distinct: int
    samples: list[str] = field(default_factory=list)


@dataclass
class TableSummary:
    table: str
    row_count: int
    columns: list[ColumnSummary] = field(default_factory=list)


def list_tables(con: sqlite3.Connection) -> list[str]:
    """Return names of all tables in the database (excluding sqlite internals)."""
    rows = con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    return [r[0] for r in rows]


def summarise_table(con: sqlite3.Connection, table: str) -> TableSummary:
    """
    Return a :class:`TableSummary` with per-column statistics for *table*.

    Raises
    ------
    ValueError
        If *table* does not exist.
    """
    available = list_tables(con)
    if table not in available:
        raise ValueError(f"Table '{table}' not found. Available: {', '.join(available) or '(none)'}")

    row_count: int = con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    col_names = [row[1] for row in con.execute(f'PRAGMA table_info("{table}")').fetchall()]

    summaries: list[ColumnSummary] = []
    for col in col_names:
        non_null: int = con.execute(f'SELECT COUNT("{col}") FROM "{table}"').fetchone()[0]
        null_count = row_count - non_null
        null_pct = (null_count / row_count * 100) if row_count else 0.0
        distinct: int = con.execute(f'SELECT COUNT(DISTINCT "{col}") FROM "{table}"').fetchone()[0]
        sample_rows = con.execute(f'SELECT DISTINCT "{col}" FROM "{table}" WHERE "{col}" IS NOT NULL LIMIT 5').fetchall()
        summaries.append(
            ColumnSummary(
                name=col,
                total=row_count,
                non_null=non_null,
                null_count=null_count,
                null_pct=round(null_pct, 2),
                distinct=distinct,
                samples=[str(r[0]) for r in sample_rows],
            )
        )

    return TableSummary(table=table, row_count=row_count, columns=summaries)


# ---------------------------------------------------------------------------
# Donation-data helpers
# ---------------------------------------------------------------------------


def summarise_all_sources(con: sqlite3.Connection) -> list[dict]:
    """
    Return source-level statistics across all donation sources.

    Each dict contains:

    * ``source``         — source name
    * ``n_fields``       — number of distinct fields for this source
    * ``n_participants`` — number of distinct participants with data for this source
    * ``n_total``        — total observations across all participants and fields
    * ``n_non_null``     — observations with a non-NULL value
    * ``pct_non_null``   — percentage non-null (rounded to 1 dp)
    """
    rows = con.execute(
        """
        SELECT
            f.source,
            COUNT(DISTINCT f.field_id)                   AS n_fields,
            COUNT(DISTINCT d.participant_id)              AS n_participants,
            COUNT(*)                                      AS n_total,
            COUNT(d.value)                               AS n_non_null,
            ROUND(100.0 * COUNT(d.value) / COUNT(*), 1)  AS pct_non_null
        FROM fields f
        JOIN data d ON d.field_id = f.field_id
        GROUP BY f.source
        ORDER BY f.source
        """
    ).fetchall()
    return [dict(row) for row in rows]


def list_sources(con: sqlite3.Connection) -> list[str]:
    """Return the distinct source names present in the ``fields`` table."""
    rows = con.execute("SELECT DISTINCT source FROM fields ORDER BY source").fetchall()
    return [r[0] for r in rows]


def summarise_source(
    con: sqlite3.Connection,
    source: str,
    alias_map: dict[str, dict[str, str]] | None = None,
    exclusion_set: dict[str, frozenset[str]] | None = None,
) -> list[dict]:
    """
    Return per-field statistics for a donation *source*.

    Each dict contains:

    * ``field``          — field name (canonical, after alias mapping)
    * ``n_total``        — total observations across all participants
    * ``n_non_null``     — observations with a non-NULL value
    * ``pct_non_null``   — percentage non-null (rounded to 1 dp)
    * ``n_participants`` — number of distinct participants with this field
                           (may be an overcount when aliases are merged)

    Parameters
    ----------
    alias_map:
        Optional ``{source: {alias → canonical}}`` mapping produced by
        ``export.build_alias_map``.  When supplied, field names that appear
        as aliases are renamed to their canonical name and their statistics
        are summed together.
    exclusion_set:
        Optional ``{source: frozenset(field_names)}`` mapping of fields to
        exclude from the summary (by original field name, before aliasing).
        Produced by ``export.build_exclusion_set``.

    Raises
    ------
    ValueError
        If *source* is not found in the ``fields`` table.
    """
    available = list_sources(con)
    if source not in available:
        raise ValueError(f"Source '{source}' not found. Available: {', '.join(available) or '(none)'}")

    rows = con.execute(
        """
        SELECT
            f.field,
            COUNT(*)                                    AS n_total,
            COUNT(d.value)                              AS n_non_null,
            COUNT(DISTINCT d.participant_id)             AS n_participants
        FROM fields f
        JOIN data d ON d.field_id = f.field_id
        WHERE f.source = ?
        GROUP BY f.field_id
        ORDER BY f.field
        """,
        (source,),
    ).fetchall()

    # Filter excluded fields (by original field name, before aliasing).
    # Normalise to NFC so YAML-sourced names match DB fields regardless of
    # how the original JSON files encoded their accented characters.
    excluded_fields = frozenset(unicodedata.normalize("NFC", f) for f in (exclusion_set or {}).get(source, frozenset()))
    if excluded_fields:
        rows = [r for r in rows if unicodedata.normalize("NFC", r["field"]) not in excluded_fields]

    # Apply alias mapping and aggregate rows with the same canonical name.
    source_aliases = (alias_map or {}).get(source, {})
    aggregated: dict[str, dict] = {}
    for row in rows:
        nfc_field = unicodedata.normalize("NFC", row["field"])
        canonical = source_aliases.get(nfc_field, nfc_field)
        if canonical in aggregated:
            aggregated[canonical]["n_total"] += row["n_total"]
            aggregated[canonical]["n_non_null"] += row["n_non_null"]
            # n_participants may overcount when aliases are merged (a participant
            # could appear in both the aliased and canonical field).
            aggregated[canonical]["n_participants"] += row["n_participants"]
        else:
            aggregated[canonical] = {
                "field": canonical,
                "n_total": row["n_total"],
                "n_non_null": row["n_non_null"],
                "n_participants": row["n_participants"],
            }

    # Recalculate pct_non_null after aggregation.
    result = []
    for entry in aggregated.values():
        entry["pct_non_null"] = round(100.0 * entry["n_non_null"] / entry["n_total"], 1) if entry["n_total"] > 0 else 0.0
        result.append(entry)

    return sorted(result, key=lambda r: r["field"])


def task_assignment_summary(
    con: sqlite3.Connection,
    task_names: dict[str, str] | None = None,
    assignment_names: dict[str, str] | None = None,
) -> list[dict]:
    """
    Return participant counts grouped by (task, assignment).

    Each dict contains:

    * ``task``            — task identifier as stored in the data
    * ``task_name``       — human-readable task name (from *task_names*) or ``""``
    * ``assignment``      — assignment identifier as stored in the data
    * ``assignment_name`` — human-readable assignment name (from *assignment_names*) or ``""``
    * ``n_participants``  — number of distinct participants with data in this (task, assignment)

    Parameters
    ----------
    task_names:
        Optional ``{task_id: name}`` mapping (from ``load_export_yaml``).
    assignment_names:
        Optional ``{assignment_id: name}`` mapping (from ``load_export_yaml``).

    Sorted by task → assignment.
    """
    rows = con.execute(
        """
        SELECT
            task,
            assignment,
            COUNT(DISTINCT participant_id) AS n_participants
        FROM data
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
    alias_map: dict[str, dict[str, str]] | None = None,
    exclusion_set: dict[str, frozenset[str]] | None = None,
) -> list[dict]:
    """
    Compute a long-format summary across all donation sources.

    For every (source, participant, field) combination returns:

    * ``source``      — data source name
    * ``participant`` — participant identifier
    * ``field``       — field name (canonical, after alias mapping)
    * ``n_non_null``  — number of non-NULL values (SQL COUNT ignores NULLs)
    * ``n_total``     — total rows for this participant × field

    Parameters
    ----------
    alias_map:
        Optional ``{source: {alias → canonical}}`` mapping produced by
        ``export.build_alias_map``.  When supplied, rows whose field name
        is an alias are renamed to the canonical name and their counts are
        summed with any existing row for that canonical name.
    exclusion_set:
        Optional ``{source: frozenset(field_names)}`` mapping of fields to
        exclude from the summary (by original field name, before aliasing).
        Produced by ``export.build_exclusion_set``.

    Sorted by source → participant → field.
    """
    rows = con.execute(
        """
        SELECT
            f.source,
            p.participant,
            f.field,
            COUNT(d.value) AS n_non_null,
            COUNT(*)       AS n_total
        FROM data d
        JOIN fields      f ON d.field_id       = f.field_id
        JOIN participants p ON d.participant_id = p.participant_id
        GROUP BY d.field_id, d.participant_id
        ORDER BY f.source, p.participant, f.field
        """
    ).fetchall()

    # Filter excluded fields per source (NFC-normalised for accent safety).
    if exclusion_set:
        nfc_exclusion = {
            src: frozenset(unicodedata.normalize("NFC", f) for f in fields) for src, fields in exclusion_set.items()
        }
        rows = [r for r in rows if unicodedata.normalize("NFC", r["field"]) not in nfc_exclusion.get(r["source"], frozenset())]

    # Apply alias mapping and aggregate rows that collapse to the same key.
    aggregated: dict[tuple, dict] = {}
    for row in rows:
        source = row["source"]
        source_aliases = (alias_map or {}).get(source, {})
        nfc_field = unicodedata.normalize("NFC", row["field"])
        canonical = source_aliases.get(nfc_field, nfc_field)
        key = (source, row["participant"] or "", canonical)

        if key in aggregated:
            aggregated[key]["n_non_null"] += row["n_non_null"]
            aggregated[key]["n_total"] += row["n_total"]
        else:
            aggregated[key] = {
                "source": source,
                "participant": row["participant"],
                "field": canonical,
                "n_non_null": row["n_non_null"],
                "n_total": row["n_total"],
            }

    return sorted(
        aggregated.values(),
        key=lambda r: (r["source"], r["participant"] or "", r["field"]),
    )
