"""
Donation data file ingestion.

File naming convention
----------------------
Key=value pairs are separated by underscores that **immediately precede a
known key name**, so values that contain underscores (e.g.
``source=google_chrome``) are handled correctly::

    assignment=359_task=862_participant=01a1f222ad90a288_source=YouTube_key=1763123719433.json

Known keys: assignment, task, participant, source, key.

Storage model
-------------
Each JSON file contains a **list of single-key objects**: ``[{table_name: [rows...]}, ...]``.
Each row is a flat dict of ``{field_name: value}`` pairs.

Data is stored in long format across four normalised tables:

* ``tables``       — one row per unique (source, table_name) pair.
* ``fields``       — one row per unique (table_id, field) pair; references ``table_id``.
* ``participants`` — one row per unique participant string.
* ``data``         — one non-null cell per (field_id, participant_id, row_index).
                     Null / sentinel values are **never stored**.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from tqdm import tqdm

# ---------------------------------------------------------------------------
# Filename parsing
# ---------------------------------------------------------------------------

#: The fixed set of key names that appear in donation data filenames.
DONATION_KEYS: tuple[str, ...] = ("assignment", "task", "participant", "source", "key")

# Split on "_" that is immediately followed by one of the known key names and "=".
# A lookahead is used so the "_" is consumed but the "key=" portion is kept.
_SPLIT_RE: re.Pattern = re.compile(r"_(?=(?:" + "|".join(DONATION_KEYS) + r")=)")


def parse_donation_filename(stem: str) -> dict[str, str]:
    """
    Parse a donation data filename stem into its key=value components.

    The stem is split only at underscores that immediately precede a known
    key name, so values containing underscores (e.g. ``source=google_chrome``)
    are preserved intact.

    Parameters
    ----------
    stem:
        Filename without extension, e.g.
        ``assignment=1_task=2_participant=abc_source=YouTube_key=999``.

    Returns
    -------
    dict
        Contains only the recognised keys found in *stem*.

    Examples
    --------
    >>> parse_donation_filename(
    ...     "assignment=359_task=862_participant=abc_source=google_chrome_key=99"
    ... )
    {'assignment': '359', 'task': '862', 'participant': 'abc',
     'source': 'google_chrome', 'key': '99'}
    """
    parts = _SPLIT_RE.split(stem)
    result: dict[str, str] = {}
    for part in parts:
        if "=" in part:
            k, v = part.split("=", 1)
            if k in DONATION_KEYS:
                result[k] = v
    return result


# ---------------------------------------------------------------------------
# Value helpers
# ---------------------------------------------------------------------------


#: String sentinels that various sources write instead of a proper JSON null.
_NULL_SENTINELS: frozenset[str] = frozenset({"null", "None"})


def _to_db_value(v: object) -> Optional[str]:
    """Convert any JSON value to a TEXT-compatible DB string, or ``None``.

    String sentinels such as ``"null"`` (common in TikTok and YouTube exports)
    and ``"None"`` (Python's ``None`` serialised as a string) are mapped to
    ``None`` so they are stored as SQL NULL rather than the literal text.
    """
    if v is None:
        return None
    if isinstance(v, str):
        if v in _NULL_SENTINELS:
            return None
        return v
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def _is_url(value: str) -> bool:
    """Return ``True`` if *value* looks like an HTTP(S) URL."""
    return value.startswith(("http://", "https://"))


# ---------------------------------------------------------------------------
# URL normalisation
# ---------------------------------------------------------------------------

#: Query parameters that are always stripped — they carry tracking / analytics
#: information and never affect the page content that will be scraped.
_TRACKING_PARAMS: frozenset[str] = frozenset(
    {
        # Google Analytics / UTM
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "utm_source_platform",
        "utm_creative_format",
        # Google Ads
        "gclid",
        "gclsrc",
        "dclid",
        # Facebook
        "fbclid",
        "fb_action_ids",
        "fb_action_types",
        "fb_source",
        "fb_ref",
        # Microsoft / Bing
        "msclkid",
        # Twitter / X
        "twclid",
        # Mailchimp
        "mc_cid",
        "mc_eid",
        # Miscellaneous
        "_ga",
        "_gl",
        "igshid",
        "si",
    }
)

#: For domains listed here, retain ONLY these query parameters.
#: For every other domain ALL query parameters are dropped.
#: This is intentionally aggressive: the trade-off is fewer unique URLs to
#: scrape at the cost of losing parameter-dependent content on unknown sites.
_KEEP_PARAMS: dict[str, frozenset[str]] = {
    "youtube.com": frozenset({"v", "list"}),  # video ID + playlist ID
    "youtu.be": frozenset(),  # video ID is in the path
}


def _base_domain(hostname: str) -> str:
    """Strip a leading ``www.`` prefix for domain-whitelist lookups."""
    return hostname.removeprefix("www.")


def normalize_url(url: str) -> str | None:
    """
    Return a normalised form of *url*, or ``None`` if the URL is unusable.

    Normalisation steps applied in order:

    1. Lowercase scheme and hostname.
    2. Remove default ports (80 for http, 443 for https).
    3. Strip the URL fragment (``#...``).
    4. For domains in ``_KEEP_PARAMS``, retain only the whitelisted query
       parameters (e.g. ``v=`` on youtube.com); all others are dropped.
    5. For every other domain, drop **all** query parameters.  This is
       intentional — most page content is path-based, and stripping
       parameters produces more duplicates, meaning fewer actual HTTP
       requests and less exposure of tracking data.
    6. Sort any retained parameters for a stable canonical form.

    Parameters
    ----------
    url:
        Raw URL string as extracted from donated data.

    Returns
    -------
    str or None
        Normalised URL string, or ``None`` if *url* is not a valid
        ``http``/``https`` URL.

    Examples
    --------
    >>> normalize_url("https://www.YouTube.com/watch?v=abc&utm_source=twitter#comments")
    'https://www.youtube.com/watch?v=abc'
    >>> normalize_url("https://example.com/article?ref=newsletter&id=42")
    'https://example.com/article'
    """
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return None

    if parsed.scheme not in ("http", "https"):
        return None

    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return None

    # Drop default ports.
    port = parsed.port
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        port = None
    netloc = f"{hostname}:{port}" if port else hostname

    # Filter query parameters.
    base = _base_domain(hostname)
    keep = _KEEP_PARAMS.get(base)  # None → domain not in whitelist
    if parsed.query and keep is not None:
        params = parse_qs(parsed.query, keep_blank_values=False)
        filtered = sorted((k, v[0]) for k, v in params.items() if k in keep)
        query = urlencode(filtered) if filtered else ""
    else:
        # Domain not whitelisted → drop everything.
        query = ""

    return urlunparse((scheme, netloc, parsed.path or "/", "", query, ""))


# ---------------------------------------------------------------------------
# Lookup-or-create helpers (with in-memory caches)
# ---------------------------------------------------------------------------


def _get_or_create_participant(
    con: sqlite3.Connection,
    cache: dict[str, int],
    participant: str,
) -> int:
    """Return ``participant_id`` for *participant*, inserting a new row if needed."""
    if participant not in cache:
        con.execute(
            "INSERT OR IGNORE INTO participants (participant) VALUES (?)",
            (participant,),
        )
        row = con.execute(
            "SELECT participant_id FROM participants WHERE participant = ?",
            (participant,),
        ).fetchone()
        cache[participant] = row["participant_id"]
    return cache[participant]


def _get_or_create_table(
    con: sqlite3.Connection,
    cache: dict[tuple[str, str], int],
    source: str,
    table_name: str,
) -> int:
    """Return ``table_id`` for *(source, table_name)*, inserting if needed."""
    key = (source, table_name)
    if key not in cache:
        con.execute(
            "INSERT OR IGNORE INTO tables (source, table_name) VALUES (?, ?)",
            (source, table_name),
        )
        row = con.execute(
            "SELECT table_id FROM tables WHERE source = ? AND table_name = ?",
            (source, table_name),
        ).fetchone()
        cache[key] = row["table_id"]
    return cache[key]


def _get_or_create_field(
    con: sqlite3.Connection,
    cache: dict[tuple[int, str], int],
    table_id: int,
    field: str,
) -> int:
    """Return ``field_id`` for *(table_id, field)*, inserting if needed."""
    key = (table_id, field)
    if key not in cache:
        con.execute(
            "INSERT OR IGNORE INTO fields (table_id, field) VALUES (?, ?)",
            (table_id, field),
        )
        row = con.execute(
            "SELECT field_id FROM fields WHERE table_id = ? AND field = ?",
            (table_id, field),
        ).fetchone()
        cache[key] = row["field_id"]
    return cache[key]


def _get_or_create_file(
    con: sqlite3.Connection,
    cache: dict[tuple, int],
    participant_id: int,
    assignment: str | None,
    task: str | None,
    file_key: str | None,
) -> int:
    """Return ``file_id`` for this file, inserting if needed."""
    key = (participant_id, file_key)
    if key not in cache:
        con.execute(
            "INSERT OR IGNORE INTO files (participant_id, assignment, task, file_key) VALUES (?, ?, ?, ?)",
            (participant_id, assignment, task, file_key),
        )
        row = con.execute(
            "SELECT file_id FROM files WHERE participant_id = ? AND file_key = ?",
            (participant_id, file_key),
        ).fetchone()
        cache[key] = row["file_id"]
    return cache[key]


def ingest_donation_directory(
    con: sqlite3.Connection,
    directory: Path,
    *,
    recursive: bool = False,
    batch_size: int = 500,
) -> dict[str, int]:
    """
    Scan *directory* for donation data JSON files and ingest them.

    Each ``.json`` file must follow the donation naming convention.  The
    JSON content must be a list of single-key objects::

        [{table_name: [row_dict, ...]}, ...]

    Each row dict maps column names to scalar values.  Null values (Python
    ``None``, ``"null"``, ``"None"``) are **silently skipped** and never
    stored.  URL values are queued in the ``urls`` table for scraping.

    Parameters
    ----------
    con:
        Open :class:`sqlite3.Connection` (core tables must already exist).
    directory:
        Path to the directory to scan.
    recursive:
        When ``True``, scan sub-directories recursively (``**/*.json``).
    batch_size:
        Number of data rows accumulated before each ``executemany`` flush.

    Returns
    -------
    dict
        ``{"files": n, "rows": n, "urls": n, "skipped": n}``.
    """
    if not directory.is_dir():
        raise ValueError(f"{directory} is not a directory")

    glob_pat = "**/*.json" if recursive else "*.json"
    files = sorted(directory.glob(glob_pat))

    if not files:
        return {"files": 0, "rows": 0, "urls": 0, "skipped": 0}

    participant_cache: dict[str, int] = {}
    table_cache: dict[tuple[str, str], int] = {}
    field_cache: dict[tuple[int, str], int] = {}
    file_cache: dict[tuple, int] = {}

    data_batch: list[tuple] = []
    url_batch: list[tuple] = []

    total_files = 0
    total_rows = 0
    total_urls = 0
    skipped = 0

    def _flush() -> None:
        nonlocal total_rows, total_urls
        if data_batch:
            con.executemany(
                "INSERT INTO data (file_id, field_id, row_index, value) VALUES (?, ?, ?, ?)",
                data_batch,
            )
            total_rows += len(data_batch)
            data_batch.clear()
        if url_batch:
            con.executemany(
                "INSERT INTO urls (participant_id, field_id, url, normalized_url) VALUES (?, ?, ?, ?)",
                url_batch,
            )
            total_urls += len(url_batch)
            url_batch.clear()

    for path in tqdm(files, desc="Ingesting", unit=" files"):
        meta = parse_donation_filename(path.stem)

        if "source" not in meta or "participant" not in meta:
            skipped += 1
            continue

        source = meta["source"].lower()

        try:
            with path.open(encoding="utf-8") as fh:
                content = json.load(fh)
        except Exception:  # noqa: BLE001
            skipped += 1
            continue

        # Each file is a list of {table_name: [rows...]} objects.
        if not isinstance(content, list):
            skipped += 1
            continue

        participant_id = _get_or_create_participant(con, participant_cache, meta["participant"])
        assignment = meta.get("assignment")
        task = meta.get("task")
        file_key = meta.get("key")
        file_id = _get_or_create_file(con, file_cache, participant_id, assignment, task, file_key)
        file_had_data = False

        for item in content:
            if not isinstance(item, dict):
                continue
            for table_name_raw, rows in item.items():
                table_name = unicodedata.normalize("NFC", table_name_raw)
                table_id = _get_or_create_table(con, table_cache, source, table_name)

                if not isinstance(rows, list):
                    rows = [rows] if isinstance(rows, dict) else []

                for row_index, row in enumerate(rows):
                    if not isinstance(row, dict):
                        continue
                    for field_name_raw, raw_value in row.items():
                        field_name = unicodedata.normalize("NFC", field_name_raw)
                        value = _to_db_value(raw_value)
                        if value is None:  # skip nulls entirely
                            continue

                        field_id = _get_or_create_field(con, field_cache, table_id, field_name)
                        data_batch.append((file_id, field_id, row_index, value))
                        file_had_data = True

                        if _is_url(value):
                            normalized = normalize_url(value)
                            if normalized:
                                url_batch.append((participant_id, field_id, value, normalized))

        if file_had_data:
            total_files += 1
        else:
            skipped += 1

        if len(data_batch) >= batch_size:
            _flush()

    _flush()
    con.commit()

    return {
        "files": total_files,
        "rows": total_rows,
        "urls": total_urls,
        "skipped": skipped,
    }
