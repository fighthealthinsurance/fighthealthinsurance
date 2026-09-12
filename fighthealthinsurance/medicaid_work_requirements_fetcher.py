"""Check each state's Medicaid agency site for public work-requirement mentions.

Refreshes the provenance columns in ``data/medicaid_resources.csv``
(``work_requirement_last_checked``, ``work_requirement_source_url``,
``work_requirement_mentioned``) by fetching the ``agency_website`` homepage
already on file for each state and looking for a handful of keyword phrases.

This is groundtruthing for a guidance surface, not a legal determination:
one page per state, a keyword search, no attempt to classify
approved/pending/stalled. That curated narrative (``work_requirement_waiver``
/ ``waiver_activity``) is intentionally left untouched here -- this fetcher
only ever adds/refreshes the three provenance columns above it. Every state
must implement a federal work requirement by
``medicaid_api.WORK_REQUIREMENT_UNIVERSAL_YEAR`` regardless of what a
homepage currently says, so "not mentioned" must never be read as "doesn't
apply" -- callers surfacing this data say so explicitly.

Invoked by:
  * ``ingest_medicaid_work_requirements`` management command
"""

from __future__ import annotations

import csv
import datetime
import urllib.robotparser
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

import aiohttp
from asgiref.sync import sync_to_async
from bs4 import BeautifulSoup
from loguru import logger

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CSV_PATH = DATA_DIR / "medicaid_resources.csv"

FETCH_TIMEOUT_SEC = 15
ROBOTS_TIMEOUT_SEC = 8
MAX_BYTES = 3 * 1024 * 1024  # 3 MB -- an agency homepage, not a document
DEFAULT_CONCURRENCY = 8

PROVENANCE_FIELDS = (
    "work_requirement_last_checked",
    "work_requirement_source_url",
    "work_requirement_mentioned",
)

# Kept short and literal on purpose -- this is a cheap public-page signal,
# not an attempt to parse eligibility rules out of prose.
_KEYWORDS = (
    "work requirement",
    "community engagement",
    "work and community engagement",
    "80 hours",
    "work and community engagement requirement",
)

_SNIPPET_MARGIN = 120


def _find_snippet(text: str) -> Optional[str]:
    lowered = text.lower()
    for keyword in _KEYWORDS:
        idx = lowered.find(keyword)
        if idx == -1:
            continue
        start = max(0, idx - _SNIPPET_MARGIN)
        end = min(len(text), idx + len(keyword) + _SNIPPET_MARGIN)
        return text[start:end].strip()
    return None


class MedicaidWorkRequirementFetcher:
    """Async context manager driving the per-state homepage check."""

    def __init__(
        self,
        session: Optional[aiohttp.ClientSession] = None,
        timeout_sec: int = FETCH_TIMEOUT_SEC,
        max_bytes: int = MAX_BYTES,
        concurrency: int = DEFAULT_CONCURRENCY,
        csv_path: Path = CSV_PATH,
    ) -> None:
        self._session = session
        self._owns_session = session is None
        self._timeout = aiohttp.ClientTimeout(total=timeout_sec)
        self._max_bytes = max_bytes
        self._csv_path = csv_path
        import asyncio

        self._semaphore = asyncio.Semaphore(concurrency)
        self._robots_cache: Dict[str, bool] = {}

    async def __aenter__(self) -> "MedicaidWorkRequirementFetcher":
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def check_all(
        self,
        states: Optional[List[str]] = None,
        dry_run: bool = False,
    ) -> Dict[str, int]:
        """Check every state's agency homepage; refresh the CSV in place.

        ``states`` (optional) restricts the run to matching ``state`` column
        values (case-insensitive exact match), for ``--state`` scoping.
        """
        import asyncio

        rows = self._read_rows()
        stats = {"checked": 0, "mentioned": 0, "failed": 0, "skipped": 0}
        wanted = {s.lower() for s in states} if states else None

        async def _one(row: Dict[str, str]) -> None:
            state_name = (row.get("state") or "").strip()
            if wanted is not None and state_name.lower() not in wanted:
                return
            url = (row.get("agency_website") or "").strip()
            if not url:
                stats["skipped"] += 1
                return
            async with self._semaphore:
                try:
                    mentioned, checked_url = await self._check_state(url)
                except Exception as e:
                    logger.warning(
                        f"Medicaid work-requirement check failed for "
                        f"{state_name} ({url}): {e}"
                    )
                    stats["failed"] += 1
                    return
            stats["checked"] += 1
            if mentioned:
                stats["mentioned"] += 1
            row["work_requirement_last_checked"] = datetime.date.today().isoformat()
            row["work_requirement_source_url"] = checked_url
            row["work_requirement_mentioned"] = "yes" if mentioned else "no"

        await asyncio.gather(*(_one(row) for row in rows))

        if not dry_run:
            self._write_rows(rows)

        logger.info(
            f"Medicaid work-requirement check complete: {stats['checked']} "
            f"checked, {stats['mentioned']} mentioned it, {stats['failed']} "
            f"failed, {stats['skipped']} skipped (no agency_website)"
        )
        return stats

    async def _check_state(self, url: str) -> "tuple[bool, str]":
        """Fetch ``url``; return ``(keyword_found, url_actually_checked)``."""
        if not await self._robots_allow(url):
            raise PermissionError(f"robots.txt disallows fetching {url}")
        html = await self._get_text(url)
        text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
        return (_find_snippet(text) is not None, url)

    async def _get_text(self, url: str) -> str:
        if self._session is None:
            raise RuntimeError(
                "MedicaidWorkRequirementFetcher must be used as "
                "'async with MedicaidWorkRequirementFetcher()'"
            )
        headers = {
            "User-Agent": "fighthealthinsurance-medicaid-work-req-check/1.0 "
            "(guidance-only informational check; "
            "+https://www.fighthealthinsurance.com)",
            "Accept": "text/html,application/xhtml+xml,*/*",
        }
        async with self._session.get(
            url, headers=headers, allow_redirects=True
        ) as resp:
            resp.raise_for_status()
            data = await resp.content.read(self._max_bytes + 1)
            if len(data) > self._max_bytes:
                data = data[: self._max_bytes]
            encoding = resp.charset or "utf-8"
            return data.decode(encoding, errors="replace")

    async def _robots_allow(self, url: str) -> bool:
        """Best-effort robots.txt check, cached per host for this run.

        A robots.txt fetch failure (no file, timeout, non-200) is treated as
        "allowed" -- most state agency sites have none, and this is a single
        homepage fetch, not a crawl.
        """
        parsed = urlparse(url)
        host = f"{parsed.scheme}://{parsed.netloc}"
        if host in self._robots_cache:
            return self._robots_cache[host]

        allowed = True
        try:
            robots_url = f"{host}/robots.txt"
            if self._session is None:
                raise RuntimeError("no session")
            async with self._session.get(
                robots_url,
                timeout=aiohttp.ClientTimeout(total=ROBOTS_TIMEOUT_SEC),
            ) as resp:
                if resp.status == 200:
                    body = await resp.text(errors="replace")

                    def _parse() -> bool:
                        parser = urllib.robotparser.RobotFileParser()
                        parser.parse(body.splitlines())
                        return parser.can_fetch(
                            "fighthealthinsurance-medicaid-work-req-check", url
                        )

                    # Plain asgiref sync_to_async ON PURPOSE: pure text
                    # parsing, no ORM access.
                    allowed = await sync_to_async(_parse, thread_sensitive=False)()
        except Exception:
            allowed = True

        self._robots_cache[host] = allowed
        return allowed

    def _read_rows(self) -> List[Dict[str, str]]:
        with open(self._csv_path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def _write_rows(self, rows: List[Dict[str, str]]) -> None:
        if not rows:
            return
        fieldnames = list(rows[0].keys())
        for field in PROVENANCE_FIELDS:
            if field not in fieldnames:
                fieldnames.append(field)
        with open(self._csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow({field: row.get(field, "") for field in fieldnames})
