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
    Create the five core tables and their indexes (idempotent).

    Schema
    ------
    fields
        One row per unique (source, field) pair. ``field_id`` is
        auto-generated and used as a FK in ``data`` and ``urls``.

    participants
        One row per unique participant identifier. ``participant_id`` is
        auto-generated and used as a FK in ``data`` and ``urls``.

    data
        Long-format observation store: one row per (field, participant)
        value. Also records file-level provenance (assignment, task, key).

    urls
        One row per (participant, field, url) triple found during ingest.
        ``url`` is the raw value as found in the data; ``normalized_url``
        is the cleaned form used for deduplication and actual HTTP requests
        (tracking parameters stripped, query strings dropped for most domains).
        Tracks per-URL scraping status and the resolved canonical URL.

    url_metadata
        One row per canonical URL, populated by the scraper on success.
        ``canonical_url`` is the primary key.
    """
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS fields (
            field_id  INTEGER PRIMARY KEY AUTOINCREMENT,
            source    TEXT    NOT NULL,
            field     TEXT    NOT NULL,
            UNIQUE(source, field)
        );

        CREATE TABLE IF NOT EXISTS participants (
            participant_id INTEGER PRIMARY KEY AUTOINCREMENT,
            participant    TEXT   NOT NULL UNIQUE
        );

        CREATE TABLE IF NOT EXISTS data (
            data_id        INTEGER PRIMARY KEY AUTOINCREMENT,
            field_id       INTEGER NOT NULL REFERENCES fields(field_id),
            participant_id INTEGER NOT NULL REFERENCES participants(participant_id),
            assignment     TEXT,
            task           TEXT,
            key            TEXT,
            value          TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_data_field
            ON data(field_id);
        CREATE INDEX IF NOT EXISTS idx_data_participant
            ON data(participant_id);
        CREATE INDEX IF NOT EXISTS idx_data_field_participant
            ON data(field_id, participant_id);

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

        CREATE INDEX IF NOT EXISTS idx_urls_url
            ON urls(url);
        CREATE INDEX IF NOT EXISTS idx_urls_normalized
            ON urls(normalized_url);
        CREATE INDEX IF NOT EXISTS idx_urls_status
            ON urls(status);

        CREATE TABLE IF NOT EXISTS url_metadata (
            canonical_url TEXT PRIMARY KEY,
            scraped_at    TEXT,
            data          TEXT,
            error         TEXT
        );
        """
    )
