"""
Central SQLite connection helper.

Usage
-----
    from data_donation_data.db import get_connection

    with get_connection() as con:
        con.execute("SELECT 1")

The database path is resolved (in priority order) from:
1. The ``DDD_DB`` environment variable.
2. ``./data.db`` in the current working directory.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path


def db_path() -> Path:
    """Return the resolved path to the SQLite database file."""
    env = os.environ.get("DDD_DB")
    if env:
        return Path(env)
    return Path.cwd() / "data.db"


@contextmanager
def get_connection(path: Path | None = None):
    """
    Yield a :class:`sqlite3.Connection` with sensible defaults.

    * ``row_factory = sqlite3.Row`` — rows are accessible by column name.
    * WAL mode for concurrent read/write.
    * Foreign keys enforced.
    * Core tables are created on first use (idempotent).
    """
    resolved = path or db_path()
    con = sqlite3.connect(resolved)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    try:
        ensure_core_tables(con)
        yield con
    finally:
        con.close()


def ensure_core_tables(con: sqlite3.Connection) -> None:
    """
    Create the core tables and their indexes (idempotent).

    Schema
    ------
    tables
        One row per unique (source, table_name) pair found in donated files.
        The top-level keys of each JSON file item are table names.

    fields
        One row per unique (table_id, field) pair — i.e. one column within
        one table of one source.

    participants
        One row per unique participant identifier.

    data
        Long-format store: one row per non-null cell value.
        (participant × table × row_index × field → value)
        Null values are never stored.

    ingested_files
        Tracks successfully ingested file stems for deduplication.

    urls / url_metadata
        URL scraping queue and results.
    """
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS tables (
            table_id   INTEGER PRIMARY KEY AUTOINCREMENT,
            source     TEXT    NOT NULL,
            table_name TEXT    NOT NULL,
            UNIQUE(source, table_name)
        );

        CREATE TABLE IF NOT EXISTS fields (
            field_id INTEGER PRIMARY KEY AUTOINCREMENT,
            table_id INTEGER NOT NULL REFERENCES tables(table_id),
            field    TEXT    NOT NULL,
            UNIQUE(table_id, field)
        );

        CREATE TABLE IF NOT EXISTS participants (
            participant_id INTEGER PRIMARY KEY AUTOINCREMENT,
            participant    TEXT   NOT NULL UNIQUE
        );

        CREATE TABLE IF NOT EXISTS files (
            file_id        INTEGER PRIMARY KEY AUTOINCREMENT,
            participant_id INTEGER NOT NULL REFERENCES participants(participant_id),
            assignment     TEXT,
            task           TEXT,
            file_key       TEXT,
            UNIQUE(participant_id, file_key)
        );

        CREATE INDEX IF NOT EXISTS idx_files_participant
            ON files(participant_id);

        CREATE TABLE IF NOT EXISTS data (
            data_id   INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id   INTEGER NOT NULL REFERENCES files(file_id),
            field_id  INTEGER NOT NULL REFERENCES fields(field_id),
            row_index INTEGER NOT NULL DEFAULT 0,
            value     TEXT    NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_data_field
            ON data(field_id);
        CREATE INDEX IF NOT EXISTS idx_data_file
            ON data(file_id);
        CREATE INDEX IF NOT EXISTS idx_data_cover
            ON data(field_id, file_id);

        CREATE TABLE IF NOT EXISTS urls (
            url_id         INTEGER PRIMARY KEY AUTOINCREMENT,
            participant_id INTEGER NOT NULL REFERENCES participants(participant_id),
            field_id       INTEGER NOT NULL REFERENCES fields(field_id),
            url            TEXT    NOT NULL,
            normalized_url TEXT,
            canonical_url  TEXT,
            status         TEXT    NOT NULL DEFAULT 'pending'
                               CHECK(status IN ('pending', 'success', 'failed')),
            error          TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_urls_status
            ON urls(status);

        CREATE TABLE IF NOT EXISTS ingested_files (
            file_stem   TEXT PRIMARY KEY,
            ingested_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        );

        CREATE TABLE IF NOT EXISTS url_metadata (
            canonical_url TEXT PRIMARY KEY,
            scraped_at    TEXT,
            data          TEXT,
            error         TEXT
        );
        """
    )


def list_sources(con: sqlite3.Connection) -> list[str]:
    """Return distinct source names from the tables table."""
    return [r[0] for r in con.execute("SELECT DISTINCT source FROM tables ORDER BY source").fetchall()]


def list_tables_for_source(con: sqlite3.Connection, source: str) -> list[str]:
    """Return table names for a given source, alphabetically."""
    return [
        r[0] for r in con.execute("SELECT table_name FROM tables WHERE source = ? ORDER BY table_name", (source,)).fetchall()
    ]
