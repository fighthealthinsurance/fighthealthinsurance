"""Tests for ``MedicaidWorkRequirementFetcher``'s keyword check + CSV refresh."""

import asyncio
import csv
from pathlib import Path

import pytest

from fighthealthinsurance.medicaid_work_requirements_fetcher import (
    MedicaidWorkRequirementFetcher,
    _find_snippet,
)


class _FakeResponse:
    def __init__(self, status: int = 200, text: str = "", charset: str = "utf-8"):
        self.status = status
        self._text = text
        self.charset = charset

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    @property
    def content(self):
        return self

    async def read(self, n: int = -1) -> bytes:
        return self._text.encode(self.charset)

    async def text(self, errors: str = "replace") -> str:
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """Maps exact URLs to canned ``_FakeResponse``s; unmapped URLs 404."""

    def __init__(self, responses: dict):
        self._responses = responses

    def get(self, url, **kwargs):
        return self._responses.get(url, _FakeResponse(status=404))


def _no_robots(url: str) -> dict:
    """A robots.txt 404 for ``url``'s host -- treated as "allowed"."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    return {f"{parsed.scheme}://{parsed.netloc}/robots.txt": _FakeResponse(status=404)}


def test_find_snippet_matches_keyword():
    text = "Welcome to the agency. Our work requirement rules changed this year."
    snippet = _find_snippet(text)
    assert snippet is not None
    assert "work requirement" in snippet.lower()


def test_find_snippet_no_match_returns_none():
    assert _find_snippet("Welcome to the agency homepage.") is None


def test_check_state_keyword_found_when_robots_txt_missing():
    url = "https://example-state.gov/medicaid"
    responses = {
        url: _FakeResponse(
            text="<html><body>Our work requirement policy is here.</body></html>"
        ),
        **_no_robots(url),
    }
    fetcher = MedicaidWorkRequirementFetcher(session=_FakeSession(responses))

    mentioned, checked_url = asyncio.run(fetcher._check_state(url))
    assert mentioned is True
    assert checked_url == url


def test_check_state_keyword_not_found():
    url = "https://example-state.gov/medicaid"
    responses = {
        url: _FakeResponse(text="<html><body>Apply for benefits here.</body></html>"),
        **_no_robots(url),
    }
    fetcher = MedicaidWorkRequirementFetcher(session=_FakeSession(responses))

    mentioned, _ = asyncio.run(fetcher._check_state(url))
    assert mentioned is False


def test_check_state_robots_disallow_raises():
    url = "https://example-state.gov/medicaid"
    robots_body = "User-agent: *\nDisallow: /\n"
    responses = {
        url: _FakeResponse(text="work requirement content"),
        "https://example-state.gov/robots.txt": _FakeResponse(text=robots_body),
    }
    fetcher = MedicaidWorkRequirementFetcher(session=_FakeSession(responses))

    with pytest.raises(PermissionError):
        asyncio.run(fetcher._check_state(url))


class TestCheckAllCsvRefresh:
    """``check_all`` refreshes only the provenance columns, in place."""

    FIELDNAMES = [
        "state",
        "agency",
        "agency_website",
        "work_requirement_waiver",
        "waiver_activity",
    ]

    def _write_csv(self, path: Path, rows: list) -> None:
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=self.FIELDNAMES)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)

    def _read_csv(self, path: Path) -> list:
        with open(path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def test_updates_provenance_columns_and_preserves_curated_data(self, tmp_path):
        csv_path = tmp_path / "medicaid_resources.csv"
        self._write_csv(
            csv_path,
            [
                {
                    "state": "Testlandia",
                    "agency": "Testlandia Medicaid Agency",
                    "agency_website": "https://example-state.gov/medicaid",
                    "work_requirement_waiver": "pending",
                    "waiver_activity": "Curated narrative, must not be touched.",
                }
            ],
        )
        url = "https://example-state.gov/medicaid"
        responses = {
            url: _FakeResponse(text="Our community engagement requirement applies."),
            **_no_robots(url),
        }
        fetcher = MedicaidWorkRequirementFetcher(
            session=_FakeSession(responses), csv_path=csv_path
        )

        stats = asyncio.run(fetcher.check_all())
        assert stats == {"checked": 1, "mentioned": 1, "failed": 0, "skipped": 0}

        rows = self._read_csv(csv_path)
        assert len(rows) == 1
        row = rows[0]
        assert row["work_requirement_waiver"] == "pending"
        assert row["waiver_activity"] == "Curated narrative, must not be touched."
        assert row["work_requirement_mentioned"] == "yes"
        assert row["work_requirement_source_url"] == url
        assert row["work_requirement_last_checked"]

    def test_skips_state_with_no_agency_website(self, tmp_path):
        csv_path = tmp_path / "medicaid_resources.csv"
        self._write_csv(
            csv_path,
            [
                {
                    "state": "Noweb",
                    "agency": "",
                    "agency_website": "",
                    "work_requirement_waiver": "",
                    "waiver_activity": "",
                }
            ],
        )
        fetcher = MedicaidWorkRequirementFetcher(
            session=_FakeSession({}), csv_path=csv_path
        )

        stats = asyncio.run(fetcher.check_all())
        assert stats == {"checked": 0, "mentioned": 0, "failed": 0, "skipped": 1}

    def test_failed_fetch_leaves_row_unchanged(self, tmp_path):
        csv_path = tmp_path / "medicaid_resources.csv"
        self._write_csv(
            csv_path,
            [
                {
                    "state": "Downlandia",
                    "agency": "",
                    "agency_website": "https://down-state.gov/medicaid",
                    "work_requirement_waiver": "N/A",
                    "waiver_activity": "N/A",
                }
            ],
        )
        # No canned response -> _FakeSession 404s -> raise_for_status raises.
        fetcher = MedicaidWorkRequirementFetcher(
            session=_FakeSession(_no_robots("https://down-state.gov/medicaid")),
            csv_path=csv_path,
        )

        stats = asyncio.run(fetcher.check_all())
        assert stats["failed"] == 1
        rows = self._read_csv(csv_path)
        assert rows[0].get("work_requirement_last_checked", "") == ""

    def test_state_scoping_only_updates_requested_state(self, tmp_path):
        csv_path = tmp_path / "medicaid_resources.csv"
        self._write_csv(
            csv_path,
            [
                {
                    "state": "Alandia",
                    "agency": "",
                    "agency_website": "https://a-state.gov",
                    "work_requirement_waiver": "",
                    "waiver_activity": "",
                },
                {
                    "state": "Blandia",
                    "agency": "",
                    "agency_website": "https://b-state.gov",
                    "work_requirement_waiver": "",
                    "waiver_activity": "",
                },
            ],
        )
        responses = {
            "https://a-state.gov": _FakeResponse(text="no mention here"),
            **_no_robots("https://a-state.gov"),
            "https://b-state.gov": _FakeResponse(text="no mention here"),
            **_no_robots("https://b-state.gov"),
        }
        fetcher = MedicaidWorkRequirementFetcher(
            session=_FakeSession(responses), csv_path=csv_path
        )

        stats = asyncio.run(fetcher.check_all(states=["Alandia"]))
        assert stats["checked"] == 1

        rows = {r["state"]: r for r in self._read_csv(csv_path)}
        assert rows["Alandia"]["work_requirement_mentioned"] == "no"
        assert rows["Blandia"].get("work_requirement_mentioned", "") == ""

    def test_dry_run_does_not_write_csv(self, tmp_path):
        csv_path = tmp_path / "medicaid_resources.csv"
        rows_in = [
            {
                "state": "Drylandia",
                "agency": "",
                "agency_website": "https://dry-state.gov",
                "work_requirement_waiver": "",
                "waiver_activity": "",
            }
        ]
        self._write_csv(csv_path, rows_in)
        before = csv_path.read_text()

        responses = {
            "https://dry-state.gov": _FakeResponse(text="work requirement mentioned"),
            **_no_robots("https://dry-state.gov"),
        }
        fetcher = MedicaidWorkRequirementFetcher(
            session=_FakeSession(responses), csv_path=csv_path
        )
        stats = asyncio.run(fetcher.check_all(dry_run=True))
        assert stats["mentioned"] == 1
        assert csv_path.read_text() == before
