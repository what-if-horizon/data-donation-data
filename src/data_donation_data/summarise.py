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


def summarise_source(con: sqlite3.Connection, source: str) -> list[dict]:
    """
    Return per-field statistics for a donation *source*.

    Each dict contains:

    * ``field``          — field name
    * ``n_total``        — total observations across all participants
    * ``n_non_null``     — observations with a non-NULL value
    * ``pct_non_null``   — percentage non-null (rounded to 1 dp)
    * ``n_participants`` — number of distinct participants with this field

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
            ROUND(100.0 * COUNT(d.value) / COUNT(*), 1) AS pct_non_null,
            COUNT(DISTINCT d.participant_id)             AS n_participants
        FROM fields f
        JOIN data d ON d.field_id = f.field_id
        WHERE f.source = ?
        GROUP BY f.field_id
        ORDER BY f.field
        """,
        (source,),
    ).fetchall()

    return [dict(row) for row in rows]


def participant_field_summary(con: sqlite3.Connection) -> list[dict]:
    """
    Compute a long-format summary across all donation sources.

    For every (source, participant, field) combination returns:

    * ``source``      — data source name
    * ``participant`` — participant identifier
    * ``field``       — field name
    * ``n_non_null``  — number of non-NULL values (SQL COUNT ignores NULLs)
    * ``n_total``     — total rows for this participant × source

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

    return [
        {
            "source": row["source"],
            "participant": row["participant"],
            "field": row["field"],
            "n_non_null": row["n_non_null"],
            "n_total": row["n_total"],
        }
        for row in rows
    ]
