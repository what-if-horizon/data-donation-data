"""
URL metadata scraper.

Architecture
------------
* ``scrape_pending`` — scrape all URLs in the ``urls`` table that have
                       ``status='pending'``.

URLs are added to the ``urls`` table automatically during ingest (any field
value starting with ``http://`` or ``https://`` is queued).

Concurrency model
-----------------
* A global ``asyncio.Semaphore`` caps the total number of simultaneous
  in-flight HTTP requests (default: 10).
* A per-domain ``asyncio.Lock`` serialises requests to the *same* domain and
  enforces a configurable random delay between consecutive hits to that domain.
* Requests to *different* domains are never delayed relative to each other —
  they run fully in parallel up to the concurrency cap.

Lock-ordering guarantee (no deadlocks)
---------------------------------------
Every coroutine always acquires locks in the same order:
    domain_lock  →  semaphore
A coroutine never holds the semaphore while waiting for a domain lock, so
circular waits are impossible.

The scraper uses ``httpx.AsyncClient`` for HTTP and ``extruct`` to extract
structured metadata from each page (Open Graph, JSON-LD, microdata,
Dublin Core, microformat, RDFa).

Database tables
---------------
urls
    One row per (participant, field, url).  The scraper reads from this
    table and updates ``status``, ``canonical_url``, and ``error``.

url_metadata
    One row per *canonical* URL (primary key). Populated by the scraper on
    a successful fetch; holds the scraped timestamp and a JSON blob of all
    extracted metadata.
"""

from __future__ import annotations

import asyncio
import json
import random
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Callable, Optional
from urllib.parse import urljoin, urlparse

import extruct
import httpx

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

USER_AGENT = "Twitterbot/1.0 (compatible; academic-research-metadata-scraper)"

DEFAULT_TIMEOUT = 15  # seconds per request
DEFAULT_DELAY_MIN = 0.5  # min seconds between requests to the *same* domain
DEFAULT_DELAY_MAX = 2.5  # max seconds between requests to the *same* domain
DEFAULT_CONCURRENCY = 10  # max simultaneous in-flight requests
MAX_RETRIES = 2  # transient-error retries per URL


# ---------------------------------------------------------------------------
# HTML / metadata helpers
# ---------------------------------------------------------------------------


def _canonical_url(html: str, base_url: str) -> Optional[str]:
    """
    Extract a canonical URL from page HTML.

    Tries ``<link rel="canonical">`` first, then ``og:url``.
    Returns ``None`` if neither is found.
    """
    try:
        from lxml import html as lxml_html  # type: ignore

        tree = lxml_html.fromstring(html.encode())

        links = tree.xpath('//link[@rel="canonical"]/@href')
        if links:
            return urljoin(base_url, links[0])

        og = tree.xpath('//meta[@property="og:url"]/@content')
        if og:
            return urljoin(base_url, og[0])
    except Exception:  # noqa: BLE001
        pass
    return None


def _extract_metadata(html: str, base_url: str) -> dict:
    """Run extruct on *html* and return all found structured metadata."""
    return extruct.extract(
        html,
        base_url=base_url,
        syntaxes=["opengraph", "json-ld", "microdata", "dublincore", "microformat", "rdfa"],
        uniform=True,
        errors="ignore",
    )


# ---------------------------------------------------------------------------
# Async scraping core
# ---------------------------------------------------------------------------


async def _scrape_one(
    client: httpx.AsyncClient,
    url: str,
    *,
    semaphore: asyncio.Semaphore,
    domain_locks: defaultdict,
    domain_last_request: dict[str, float],
    delay_min: float,
    delay_max: float,
) -> tuple[str, str, dict]:
    """
    Fetch and extract metadata for a single URL.

    Acquires the domain lock first (serialising same-domain requests and
    enforcing the per-domain delay), then acquires the global semaphore
    immediately before the HTTP call.  This ordering guarantees
    deadlock-freedom: no coroutine ever holds the semaphore while waiting
    for a domain lock.

    Returns
    -------
    tuple
        ``(url, status, kwargs)`` ready for ``_update_after_scrape``.
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return url, "failed", {"error": f"Unsupported scheme: {parsed.scheme!r}"}

    domain = parsed.netloc

    async with domain_locks[domain]:
        last_time = domain_last_request.get(domain)
        if last_time is not None:
            elapsed = time.monotonic() - last_time
            delay = random.uniform(delay_min, delay_max)
            if elapsed < delay:
                await asyncio.sleep(delay - elapsed)

        async with semaphore:
            try:
                response: Optional[httpx.Response] = None
                for attempt in range(1, MAX_RETRIES + 1):
                    try:
                        response = await client.get(
                            url,
                            follow_redirects=True,
                            timeout=DEFAULT_TIMEOUT,
                        )
                        break
                    except (httpx.TimeoutException, httpx.TransportError):
                        if attempt == MAX_RETRIES:
                            raise
                        await asyncio.sleep(1.5 * attempt)

                assert response is not None
                final_url = str(response.url)

                if response.status_code >= 400:
                    raise httpx.HTTPStatusError(
                        f"HTTP {response.status_code}",
                        request=response.request,
                        response=response,
                    )

                html = response.text
                canonical = _canonical_url(html, final_url)
                metadata = _extract_metadata(html, final_url)

                return (
                    url,
                    "success",
                    {
                        "canonical_url": canonical or final_url,
                        "data": json.dumps(metadata, ensure_ascii=False),
                    },
                )

            except Exception as exc:  # noqa: BLE001
                return url, "failed", {"error": str(exc)[:2000]}

            finally:
                domain_last_request[domain] = time.monotonic()


async def _run_scrape(
    con: sqlite3.Connection,
    pending_urls: list[str],
    *,
    delay_min: float,
    delay_max: float,
    concurrency: int,
    on_progress: Optional[Callable],
) -> dict[str, int]:
    """
    Async driver: fan out all pending URLs as concurrent tasks.

    SQLite writes happen on the single asyncio thread (between ``await``
    points), so no additional locking is needed for DB access.
    """
    domain_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
    domain_last_request: dict[str, float] = {}
    semaphore = asyncio.Semaphore(concurrency)

    counts: dict[str, int] = {"success": 0, "failed": 0}
    finished = 0
    total = len(pending_urls)

    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
    }

    async with httpx.AsyncClient(headers=headers) as client:

        async def process(normalized_url: str) -> None:
            nonlocal finished
            _, status, kwargs = await _scrape_one(
                client,
                normalized_url,
                semaphore=semaphore,
                domain_locks=domain_locks,
                domain_last_request=domain_last_request,
                delay_min=delay_min,
                delay_max=delay_max,
            )
            # DB write — safe on the single asyncio thread.
            _update_after_scrape(con, normalized_url, status=status, **kwargs)
            counts[status] = counts.get(status, 0) + 1
            finished += 1
            if on_progress:
                on_progress(normalized_url, status, finished, total)

        await asyncio.gather(*[process(url) for url in pending_urls])

    return counts


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def scrape_pending(
    con: sqlite3.Connection,
    *,
    delay_min: float = DEFAULT_DELAY_MIN,
    delay_max: float = DEFAULT_DELAY_MAX,
    concurrency: int = DEFAULT_CONCURRENCY,
    limit: Optional[int] = None,
    on_progress: Optional[Callable] = None,
) -> dict[str, int]:
    """
    Scrape all URLs currently marked ``status='pending'`` in the ``urls`` table.

    Each distinct *normalised* URL is scraped only once, even if multiple
    raw URLs collapse to the same normalised form or the same URL appears
    for multiple participants.  After a successful scrape the resolved
    ``canonical_url`` and metadata are written to ``url_metadata``; all
    ``urls`` rows sharing that ``normalized_url`` are updated to
    ``status='success'``.  On failure, all matching rows are marked
    ``status='failed'``.

    Requests to different domains run **concurrently** (up to *concurrency*
    simultaneous connections).  Requests to the **same domain** are
    serialised with a random delay of ``[delay_min, delay_max]`` seconds.

    The pending list is shuffled before dispatch to spread domains across
    worker slots from the start.

    Parameters
    ----------
    con:
        Open database connection.
    delay_min / delay_max:
        Random delay range applied **only** between consecutive requests to
        the *same* domain.
    concurrency:
        Maximum number of simultaneous in-flight HTTP requests.
    limit:
        If set, stop after processing this many distinct URLs.
    on_progress:
        Optional callback ``fn(url, status, finished, total)`` called after
        each URL completes.

    Returns
    -------
    dict
        ``{"success": n, "failed": n}`` counts.
    """
    pending_urls: list[str] = [
        row["normalized_url"]
        for row in con.execute(
            "SELECT DISTINCT normalized_url FROM urls WHERE status = 'pending' AND normalized_url IS NOT NULL"
        ).fetchall()
    ]

    random.shuffle(pending_urls)

    if limit is not None:
        pending_urls = pending_urls[:limit]

    if not pending_urls:
        return {"success": 0, "failed": 0}

    return asyncio.run(
        _run_scrape(
            con,
            pending_urls,
            delay_min=delay_min,
            delay_max=delay_max,
            concurrency=concurrency,
            on_progress=on_progress,
        )
    )


def _update_after_scrape(
    con: sqlite3.Connection,
    normalized_url: str,
    *,
    status: str,
    canonical_url: Optional[str] = None,
    data: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    """
    Persist scrape results for *normalized_url*.

    All ``urls`` rows that share this ``normalized_url`` are updated
    together, so every participant whose raw URL collapsed to the same
    normalised form benefits from a single scrape.

    On success: upsert into ``url_metadata`` (keyed on ``canonical_url``)
    and mark all matching rows as ``'success'``.

    On failure: mark all matching rows as ``'failed'`` with the error.
    """
    now = datetime.now(timezone.utc).isoformat()

    if status == "success" and canonical_url:
        con.execute(
            """
            INSERT OR REPLACE INTO url_metadata (canonical_url, scraped_at, data)
            VALUES (?, ?, ?)
            """,
            (canonical_url, now, data),
        )
        con.execute(
            "UPDATE urls SET status = 'success', canonical_url = ? WHERE normalized_url = ?",
            (canonical_url, normalized_url),
        )
    else:
        con.execute(
            "UPDATE urls SET status = 'failed', error = ? WHERE normalized_url = ?",
            (error, normalized_url),
        )

    con.commit()
