"""Tests for ml/citation_verification.py: identifiers in model-written
citations are found, checked against their registries (faked here with
httpx.MockTransport), and the citations dropped, trimmed or labelled."""

import asyncio
import json
from typing import List
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from fighthealthinsurance.ml import citation_verification as cv
from fighthealthinsurance.ml.ml_citations_helper import MLCitationsHelper
from fighthealthinsurance.models import Denial

REAL_DOI = "10.1056/NEJMoa2034577"
FAKE_DOI = "10.1056/NOTREAL999999"
REAL_PMID = "33301246"
FAKE_PMID = "99999999"


def _registry(seen: List[httpx.Request], delay: float = 0.0, fail: bool = False):
    """A fake doi.org / NCBI / eCFR that knows REAL_DOI, REAL_PMID and
    45 CFR 147.136, and records every request."""

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if delay:
            await asyncio.sleep(delay)
        if fail:
            raise httpx.ConnectTimeout("registry down", request=request)
        url = str(request.url)
        if "doi.org/api/handles/" in url:
            doi = url.split("/api/handles/", 1)[1]
            if doi.lower() == REAL_DOI.lower():
                return httpx.Response(200, json={"responseCode": 1})
            return httpx.Response(404, json={"responseCode": 100})
        if "esummary.fcgi" in url:
            ids = request.url.params["id"].split(",")
            result: dict = {"uids": ids}
            for pmid in ids:
                if pmid == REAL_PMID:
                    result[pmid] = {"uid": pmid}
                else:
                    result[pmid] = {"uid": pmid, "error": "cannot get document summary"}
            return httpx.Response(200, json={"result": result})
        if "ecfr.gov" in url:
            if "title-45" in url and request.url.params.get("section") == "147.136":
                return httpx.Response(302, headers={"location": "https://x"})
            return httpx.Response(404)
        return httpx.Response(500)

    return handler


def _client(seen, **kw) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(_registry(seen, **kw)))


@pytest.fixture
def checks_on():
    with patch.object(cv, "checks_enabled", return_value=True):
        yield


class TestFindIdentifiers:
    def test_finds_a_doi_and_drops_trailing_punctuation(self):
        ids = cv.find_identifiers(f"Polack et al. NEJM 2020. doi:{REAL_DOI}.")
        dois = [i for i in ids if i.kind == "doi" and not i.malformed]
        assert [i.value for i in dois] == [REAL_DOI.lower()]

    def test_marks_a_doi_of_the_wrong_form_malformed(self):
        ids = cv.find_identifiers("Some study. doi: 10.12/abc")
        assert any(i.kind == "doi" and i.malformed for i in ids)

    def test_finds_pmids_in_text_and_pubmed_urls(self):
        ids = cv.find_identifiers(
            f"PMID: {REAL_PMID}; https://pubmed.ncbi.nlm.nih.gov/{FAKE_PMID}/"
        )
        assert {i.value for i in ids if i.kind == "pmid"} == {REAL_PMID, FAKE_PMID}

    def test_marks_a_pmid_with_letters_malformed(self):
        ids = cv.find_identifiers("PMID: 12AB34")
        assert [(i.kind, i.malformed) for i in ids] == [("pmid", True)]

    def test_reads_cfr_part_and_section(self):
        ids = cv.find_identifiers("29 C.F.R. § 2560.503-1 and 45 CFR 147.136(b)")
        assert {i.value for i in ids if i.kind == "cfr"} == {
            "29/2560/2560.503-1",
            "45/147/147.136",
        }

    def test_marks_a_cfr_title_past_50_malformed(self):
        ids = cv.find_identifiers("99 CFR 12.3")
        assert [(i.kind, i.malformed) for i in ids] == [("cfr", True)]

    def test_reads_us_code_sections(self):
        ids = cv.find_identifiers("42 U.S.C. § 300gg-19")
        assert [(i.kind, i.value, i.malformed) for i in ids] == [
            ("usc", "42/300gg", False)
        ]

    def test_plain_text_reference_has_no_identifiers(self):
        assert cv.find_identifiers("ACR Appropriateness Criteria: Low Back Pain") == []


@pytest.mark.asyncio
class TestCheckIdentifiers:
    async def test_registry_answers_set_found_and_missing(self):
        seen: List[httpx.Request] = []
        ids = cv.find_identifiers(
            f"doi:{REAL_DOI} doi:{FAKE_DOI} PMID {REAL_PMID} PMID {FAKE_PMID} "
            "45 CFR 147.136 45 CFR 147.999"
        )
        async with _client(seen) as client:
            statuses = await cv.check_identifiers(ids, client=client)
        assert statuses[("doi", REAL_DOI.lower())] == cv.FOUND
        assert statuses[("doi", FAKE_DOI.lower())] == cv.MISSING
        assert statuses[("pmid", REAL_PMID)] == cv.FOUND
        assert statuses[("pmid", FAKE_PMID)] == cv.MISSING
        assert statuses[("cfr", "45/147/147.136")] == cv.FOUND
        assert statuses[("cfr", "45/147/147.999")] == cv.MISSING

    async def test_all_pmids_go_in_one_esummary_call(self):
        seen: List[httpx.Request] = []
        ids = cv.find_identifiers(f"PMID {REAL_PMID}, PMID {FAKE_PMID}")
        async with _client(seen) as client:
            await cv.check_identifiers(ids, client=client)
        assert sum("esummary" in str(r.url) for r in seen) == 1

    async def test_a_registry_error_leaves_the_identifier_unchecked(self):
        seen: List[httpx.Request] = []
        ids = cv.find_identifiers(f"doi:{REAL_DOI} PMID {REAL_PMID}")
        async with _client(seen, fail=True) as client:
            statuses = await cv.check_identifiers(ids, client=client)
        assert set(statuses.values()) == {cv.UNCHECKED}

    async def test_a_slow_registry_is_cut_off_at_the_budget(self):
        seen: List[httpx.Request] = []
        ids = cv.find_identifiers(f"doi:{REAL_DOI}")
        async with _client(seen, delay=5.0) as client:
            loop = asyncio.get_running_loop()
            started = loop.time()
            statuses = await cv.check_identifiers(
                ids, budget_seconds=0.2, client=client
            )
            elapsed = loop.time() - started
        assert statuses[("doi", REAL_DOI.lower())] == cv.UNCHECKED
        assert elapsed < 2.0

    async def test_us_code_is_checked_for_form_only(self):
        seen: List[httpx.Request] = []
        ids = cv.find_identifiers("42 U.S.C. § 300gg-19")
        async with _client(seen) as client:
            statuses = await cv.check_identifiers(ids, client=client)
        assert statuses == {("usc", "42/300gg"): cv.FORM_ONLY}
        assert seen == []

    async def test_only_identifiers_reach_the_registries(self):
        seen: List[httpx.Request] = []
        citation = f"Jane Doe's knee MRI follow-up. doi:{REAL_DOI} PMID {REAL_PMID}"
        async with _client(seen) as client:
            await cv.check_identifiers(cv.find_identifiers(citation), client=client)
        sent = " ".join(str(r.url) + r.content.decode() for r in seen).lower()
        assert seen
        for word in ("jane", "doe", "knee", "mri", "follow"):
            assert word not in sent


@pytest.mark.asyncio
class TestVerifyCitations:
    async def _verify(self, citations, **kw):
        seen: List[httpx.Request] = []
        async with _client(seen, **kw) as client:
            return await cv.verify_citations(citations, client=client)

    async def test_off_by_default_in_tests_and_returns_citations_unchanged(self):
        citations = [f"Made up. doi:{FAKE_DOI}"]
        assert await cv.verify_citations(citations) == citations

    async def test_drops_a_short_citation_with_a_missing_doi(self, checks_on):
        good = f"Polack FP. NEJM 2020. doi:{REAL_DOI}"
        bad = f"Invented T. J Fake Med 2021. doi:{FAKE_DOI}"
        assert await self._verify([good, bad]) == [good]

    async def test_drops_a_short_citation_with_a_malformed_pmid(self, checks_on):
        assert await self._verify(["Some trial. PMID: 12AB34"]) == []

    async def test_cuts_a_missing_identifier_out_of_a_long_answer(self, checks_on):
        answer = (
            "Here is the evidence for the treatment. " * 15
            + f"See doi:{FAKE_DOI} and doi:{REAL_DOI}."
        )
        (out,) = await self._verify([answer])
        assert FAKE_DOI not in out
        assert REAL_DOI in out
        assert cv.REMOVED_REFERENCE in out

    async def test_labels_a_citation_it_could_not_check(self, checks_on):
        citation = f"Polack FP. NEJM 2020. doi:{REAL_DOI}"
        assert await self._verify([citation], fail=True) == [
            cv.UNVERIFIED_LABEL + citation
        ]

    async def test_keeps_found_and_identifier_free_citations_in_order(self, checks_on):
        citations = [
            "ACR Appropriateness Criteria: Low Back Pain",
            f"Polack FP. NEJM 2020. PMID {REAL_PMID}",
            "45 CFR 147.136, internal claims and appeals",
            "42 U.S.C. § 300gg-19",
        ]
        assert await self._verify(citations) == citations

    async def test_returns_citations_unchanged_on_an_unexpected_error(self, checks_on):
        citations = [f"Polack FP. doi:{REAL_DOI}"]
        with patch.object(cv, "check_identifiers", side_effect=RuntimeError("boom")):
            assert await cv.verify_citations(citations) == citations

    async def test_stored_shape_stays_a_list_of_strings(self, checks_on):
        out = await self._verify(
            [f"doi:{REAL_DOI}", f"doi:{FAKE_DOI}", "plain"], fail=False
        )
        assert isinstance(out, list) and all(isinstance(c, str) for c in out)
        json.dumps(out)


@pytest.mark.asyncio
class TestCitationsForDenialAreChecked:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.denial = MagicMock(spec=Denial)
        self.denial.pk = None
        self.denial.denial_id = 4242
        self.denial.procedure = "MRI"
        self.denial.diagnosis = "Headache"
        self.denial.denial_text = ""
        self.denial.health_history = ""
        self.denial.ml_citation_context = None

    async def test_model_citations_are_checked_and_cms_ones_added_after(self):
        verify = AsyncMock(return_value=["checked model citation"])
        with (
            patch.object(
                MLCitationsHelper,
                "generate_generic_citations",
                new_callable=AsyncMock,
                return_value=["model citation", "bad model citation"],
            ),
            patch.object(
                MLCitationsHelper,
                "generate_cms_coverage_citations",
                new_callable=AsyncMock,
                return_value=["CMS NCD 220.2"],
            ),
            patch(
                "fighthealthinsurance.ml.ml_citations_helper.verify_citations", verify
            ),
        ):
            result = await MLCitationsHelper._generate_citations_for_denial(
                denial=self.denial, timeout=30
            )
        assert result == ["checked model citation", "CMS NCD 220.2"]
        assert verify.await_args.args[0] == ["model citation", "bad model citation"]
        # A tenth of a 30 second run.
        assert verify.await_args.kwargs["budget_seconds"] == 3.0
