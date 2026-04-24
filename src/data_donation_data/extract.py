"""
Data extraction helpers.

Exports selected columns from any table to CSV or JSONL.
"""

from __future__ import annotations

import csv
import json
import sqlite3
import sys
from pathlib import Path
from typing import TextIO


def _validate_columns(con: sqlite3.Connection, table: str, columns: list[str]) -> None:
    info = con.execute(f'PRAGMA table_info("{table}")').fetchall()
    available = {row[1] for row in info}
    missing = [c for c in columns if c not in available]
    if missing:
        raise ValueError(f"Column(s) not found in '{table}': {', '.join(missing)}. Available: {', '.join(sorted(available))}")


def _iter_rows(
    con: sqlite3.Connection,
    table: str,
    columns: list[str],
    where: str | None,
) -> list[sqlite3.Row]:
    col_list = ", ".join(f'"{c}"' for c in columns)
    sql = f'SELECT {col_list} FROM "{table}"'
    if where:
        sql += f" WHERE {where}"
    return con.execute(sql).fetchall()


def extract_to_csv(
    con: sqlite3.Connection,
    table: str,
    columns: list[str],
    output: Path | TextIO | None = None,
    where: str | None = None,
) -> int:
    """
    Write *columns* from *table* to CSV.

    Parameters
    ----------
    con:        Open database connection.
    table:      Source table name.
    columns:    Column names to include.
    output:     File path, open file object, or ``None`` for stdout.
    where:      Optional SQL WHERE clause (without the ``WHERE`` keyword).

    Returns
    -------
    int
        Number of rows written.
    """
    _validate_columns(con, table, columns)
    rows = _iter_rows(con, table, columns, where)

    if output is None:
        fh: TextIO = sys.stdout
        close = False
    elif isinstance(output, Path):
        fh = output.open("w", newline="", encoding="utf-8")
        close = True
    else:
        fh = output
        close = False

    try:
        writer = csv.writer(fh)
        writer.writerow(columns)
        for row in rows:
            writer.writerow(list(row))
    finally:
        if close:
            fh.close()

    return len(rows)


def extract_to_jsonl(
    con: sqlite3.Connection,
    table: str,
    columns: list[str],
    output: Path | TextIO | None = None,
    where: str | None = None,
) -> int:
    """
    Write *columns* from *table* to newline-delimited JSON.

    Parameters / Returns mirror :func:`extract_to_csv`.
    """
    _validate_columns(con, table, columns)
    rows = _iter_rows(con, table, columns, where)

    if output is None:
        fh: TextIO = sys.stdout
        close = False
    elif isinstance(output, Path):
        fh = output.open("w", encoding="utf-8")
        close = True
    else:
        fh = output
        close = False

    try:
        for row in rows:
            fh.write(json.dumps(dict(zip(columns, row)), ensure_ascii=False) + "\n")
    finally:
        if close:
            fh.close()

    return len(rows)
