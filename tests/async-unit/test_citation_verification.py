"""Tests for ml/citation_verification.py: identifiers in model-written
citations are found, checked against their registries (faked here with
httpx.MockTransport), and the citations dropped, trimmed or labelled."""

import asyncio
import json
from typing import List
from urllib.parse import unquote
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


# eCFR's structure of title 45 for part 147, cut down to what the check reads.
TITLE_45_PART_147 = {
    "identifier": "45",
    "type": "title",
    "children": [
        {
            "identifier": "A",
            "type": "subtitle",
            "children": [
                {
                    "identifier": "147",
                    "type": "part",
                    "children": [
                        {"identifier": "147.104", "type": "section"},
                        {"identifier": "147.136", "type": "section"},
                    ],
                }
            ],
        }
    ],
}

BOT_CHECK_PAGE = "<html><body>Please confirm you are a human</body></html>"


def _registry(
    seen: List[httpx.Request],
    delay: float = 0.0,
    fail: bool = False,
    ecfr: str = "real",
):
    """A fake doi.org / NCBI / eCFR that knows REAL_DOI, REAL_PMID and
    45 CFR 147.136, and records every request. ``ecfr`` swaps eCFR's real
    answers for a bot-check redirect ("redirect") or a 200 challenge page
    ("challenge")."""

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if delay:
            await asyncio.sleep(delay)
        if fail:
            raise httpx.ConnectTimeout("registry down", request=request)
        url = str(request.url)
        if "doi.org/api/handles/" in url:
            doi = unquote(url.split("/api/handles/", 1)[1])
            if doi.lower() == REAL_DOI.lower():
                return httpx.Response(200, json={"responseCode": 1, "handle": doi})
            return httpx.Response(404, json={"responseCode": 100, "handle": doi})
        if "esummary.fcgi" in url:
            ids = request.url.params["id"].split(",")
            result: dict = {"uids": ids}
            for pmid in ids:
                if pmid == REAL_PMID:
                    result[pmid] = {
                        "uid": pmid,
                        "title": "Safety and Efficacy of the BNT162b2 Vaccine.",
                        "source": "N Engl J Med",
                    }
                else:
                    result[pmid] = {"uid": pmid, "error": "cannot get document summary"}
            return httpx.Response(200, json={"result": result})
        if "ecfr.gov" in url:
            if ecfr == "redirect":
                return httpx.Response(
                    302, headers={"location": "https://www.ecfr.gov/bot-check"}
                )
            if ecfr == "challenge":
                return httpx.Response(
                    200, text=BOT_CHECK_PAGE, headers={"content-type": "text/html"}
                )
            if "title-45.json" in url and request.url.params.get("part") == "147":
                return httpx.Response(200, json=TITLE_45_PART_147)
            return httpx.Response(404, json={})
        return httpx.Response(500)

    return handler


def _client(seen, **kw) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(_registry(seen, **kw)))


@pytest.fixture(autouse=True)
def _scoped_eutils_params():
    """The esummary call sends pubmed_tools._EUTILS_PARAMS, which picks up
    NCBI_API_KEY from the environment at import. Replace it for every test
    here so no ambient key is read or sent."""
    with patch.dict(
        "fighthealthinsurance.pubmed_tools._EUTILS_PARAMS",
        {"tool": "test-tool", "email": "test@example.com"},
        clear=True,
    ):
        yield


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
            ("usc", "42/300gg-19", False)
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

    async def _cfr(self, citation, **kw):
        seen: List[httpx.Request] = []
        async with _client(seen, **kw) as client:
            return await cv.check_identifiers(
                cv.find_identifiers(citation), client=client
            )

    async def test_a_genuine_section_is_found(self):
        statuses = await self._cfr("45 CFR 147.136")
        assert statuses == {("cfr", "45/147/147.136"): cv.FOUND}

    async def test_a_genuine_part_is_found(self):
        statuses = await self._cfr("45 CFR Part 147")
        assert statuses == {("cfr", "45/147/"): cv.FOUND}

    async def test_a_section_the_part_does_not_list_is_missing(self):
        statuses = await self._cfr("45 CFR 147.999")
        assert statuses == {("cfr", "45/147/147.999"): cv.MISSING}

    async def test_a_genuine_404_is_missing(self):
        statuses = await self._cfr("45 CFR 999.1")
        assert statuses == {("cfr", "45/999/999.1"): cv.MISSING}

    async def test_a_bot_check_redirect_is_unchecked(self):
        statuses = await self._cfr("45 CFR 147.136", ecfr="redirect")
        assert statuses == {("cfr", "45/147/147.136"): cv.UNCHECKED}

    async def test_a_challenge_page_is_unchecked(self):
        statuses = await self._cfr("45 CFR 147.999", ecfr="challenge")
        assert statuses == {("cfr", "45/147/147.999"): cv.UNCHECKED}

    async def test_ecfr_json_of_another_shape_is_unchecked(self):
        async def handler(request):
            return httpx.Response(200, json={"error": "something else"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            statuses = await cv.check_identifiers(
                cv.find_identifiers("45 CFR 147.136"), client=c
            )
        assert statuses == {("cfr", "45/147/147.136"): cv.UNCHECKED}

    @pytest.mark.parametrize(
        "response",
        [
            httpx.Response(302, headers={"location": "https://doi.org/login"}),
            httpx.Response(
                200, text=BOT_CHECK_PAGE, headers={"content-type": "text/html"}
            ),
            httpx.Response(200, json={"responseCode": 1, "handle": "10.9999/other"}),
            httpx.Response(200, json={"responseCode": 100}),
            httpx.Response(503, json={"responseCode": 100}),
            httpx.Response(403, json={"responseCode": 100}),
            httpx.Response(429, json={"responseCode": 100}),
        ],
    )
    async def test_an_unexpected_doi_answer_is_unchecked(self, response):
        async def handler(request):
            return response

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            statuses = await cv.check_identifiers(
                cv.find_identifiers(f"doi:{FAKE_DOI}"), client=c
            )
        assert statuses == {("doi", FAKE_DOI.lower()): cv.UNCHECKED}

    @pytest.mark.parametrize(
        "response",
        [
            httpx.Response(302, headers={"location": "https://www.ncbi.nlm.nih.gov/"}),
            httpx.Response(
                200, text=BOT_CHECK_PAGE, headers={"content-type": "text/html"}
            ),
            httpx.Response(200, json={"esearchresult": {}}),
            httpx.Response(429, json={"result": {"uids": []}}),
            httpx.Response(503, json={"result": {"uids": []}}),
            # An error that does not say the ID is unknown says nothing.
            httpx.Response(
                200,
                json={
                    "result": {
                        "uids": [REAL_PMID],
                        REAL_PMID: {"uid": REAL_PMID, "error": "backend timeout"},
                    }
                },
            ),
            # A record with neither title nor source is not a summary.
            httpx.Response(
                200,
                json={"result": {"uids": [REAL_PMID], REAL_PMID: {"uid": REAL_PMID}}},
            ),
        ],
    )
    async def test_an_unexpected_ncbi_answer_is_unchecked(self, response):
        async def handler(request):
            return response

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            statuses = await cv.check_identifiers(
                cv.find_identifiers(f"PMID {REAL_PMID}"), client=c
            )
        assert statuses == {("pmid", REAL_PMID): cv.UNCHECKED}

    async def test_redirects_are_never_followed(self):
        seen: List[httpx.Request] = []
        async with _client(seen, ecfr="redirect") as client:
            await cv.check_identifiers(
                cv.find_identifiers("45 CFR 147.136"), client=client
            )
        assert len(seen) == 1

    async def test_us_code_is_checked_for_form_only(self):
        seen: List[httpx.Request] = []
        ids = cv.find_identifiers("42 U.S.C. § 300gg-19")
        async with _client(seen) as client:
            statuses = await cv.check_identifiers(ids, client=client)
        assert statuses == {("usc", "42/300gg-19"): cv.FORM_ONLY}
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

    async def test_labels_references_on_an_unexpected_error(self, checks_on):
        citations = [f"Polack FP. doi:{REAL_DOI}", "ACR Appropriateness Criteria"]
        with patch.object(cv, "check_identifiers", side_effect=RuntimeError("boom")):
            assert await cv.verify_citations(citations) == [
                cv.UNVERIFIED_LABEL + citations[0],
                citations[1],
            ]

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
        assert callable(verify.await_args.kwargs["may_send"])


class TestIdentifiersAreBoundedBeforeAnyRequest:
    def test_a_doi_carrying_words_is_malformed(self):
        ids = cv.find_identifiers("doi:10.1234/patient=Rebecca_Crumpler&diagnosis=HIV")
        assert [(i.kind, i.malformed) for i in ids] == [("doi", True)]

    def test_a_doi_with_path_traversal_is_malformed(self):
        ids = cv.find_identifiers("see 10.1234/../../../x")
        assert [(i.kind, i.malformed) for i in ids] == [("doi", True)]

    def test_a_doi_past_the_length_limit_is_malformed(self):
        ids = cv.find_identifiers("10.1234/" + "a" * 101)
        assert [(i.kind, i.malformed) for i in ids] == [("doi", True)]

    def test_a_percent_encoded_doi_is_malformed(self):
        ids = cv.find_identifiers("10.1234/abc%2F..%2Fx")
        assert [(i.kind, i.malformed) for i in ids] == [("doi", True)]

    def test_a_markdown_link_yields_the_one_valid_doi(self):
        citation = f"[{REAL_DOI}](https://doi.org/{REAL_DOI})"
        assert cv.find_identifiers(citation) == [cv.Identifier("doi", REAL_DOI.lower())]
        assert len(cv.find_occurrences(citation)) == 2

    def test_balanced_parentheses_stay_in_a_doi(self):
        ids = cv.find_identifiers("Lancet. 10.1016/S0140-6736(20)30183-5.")
        assert [i.value for i in ids] == ["10.1016/s0140-6736(20)30183-5"]

    @pytest.mark.parametrize(
        "citation, kind",
        [
            ("https://pubmed.ncbi.nlm.nih.gov/33301246oops", "pmid"),
            ("45 CFR 147.136oops", "cfr"),
            ("42 USC 300gg-???", "usc"),
        ],
    )
    def test_a_valid_prefix_with_junk_stuck_on_is_malformed(self, citation, kind):
        assert [(i.kind, i.malformed) for i in cv.find_identifiers(citation)] == [
            (kind, True)
        ]

    def test_subsections_after_a_section_are_fine(self):
        ids = cv.find_identifiers("45 CFR 147.136(b)(1)(ii).")
        assert ids == [cv.Identifier("cfr", "45/147/147.136")]

    def test_prose_about_dois_is_not_an_identifier(self):
        assert cv.find_identifiers("include the DOI number in the appeal") == []


@pytest.mark.asyncio
class TestNothingMalformedIsSent:
    async def test_leak_and_traversal_dois_make_no_request(self, checks_on):
        seen: List[httpx.Request] = []
        citations = [
            "Smith. doi:10.1234/patient=Rebecca_Crumpler&diagnosis=HIV",
            "Jones. 10.1234/../../../x",
        ]
        async with _client(seen) as client:
            out = await cv.verify_citations(citations, client=client)
        assert out == []
        assert seen == []

    async def test_a_doi_is_sent_as_one_encoded_path_segment(self):
        seen: List[httpx.Request] = []
        async with _client(seen) as client:
            await cv.check_identifiers(
                cv.find_identifiers(f"doi:{REAL_DOI}"), client=client
            )
        (request,) = seen
        assert request.url.raw_path.decode().endswith("10.1056%2Fnejmoa2034577")

    async def test_an_ambient_ncbi_key_is_not_sent(self):
        seen: List[httpx.Request] = []
        with patch.dict("os.environ", {"NCBI_API_KEY": "ambient-key"}):
            async with _client(seen) as client:
                await cv.check_identifiers(
                    cv.find_identifiers(f"PMID {REAL_PMID}"), client=client
                )
        assert "ambient-key" not in str(seen[0].url)
        assert "api_key" not in seen[0].url.params


class _RecordingCache:
    def __init__(self, get_delay: float = 0.0):
        self.get_delay = get_delay
        self.gets: List[str] = []
        self.sets: List[tuple] = []

    async def aget(self, key, default=None):
        self.gets.append(key)
        if self.get_delay:
            await asyncio.sleep(self.get_delay)
        return default

    async def aset(self, key, value, timeout=None):
        self.sets.append((key, value))


@pytest.mark.asyncio
class TestCacheAndDeadline:
    async def test_only_found_and_missing_are_cached(self):
        recording = _RecordingCache()
        seen: List[httpx.Request] = []
        with patch.object(cv, "cache", recording):
            async with _client(seen) as client:
                await cv.check_identifiers(
                    cv.find_identifiers(f"doi:{REAL_DOI} doi:{FAKE_DOI}"),
                    client=client,
                )
            async with _client(seen, fail=True) as client:
                await cv.check_identifiers(
                    cv.find_identifiers("doi:10.1000/timesout"), client=client
                )
        assert sorted(v for _k, v in recording.sets) == [cv.FOUND, cv.MISSING]

    async def test_a_refusal_is_not_cached(self):
        recording = _RecordingCache()

        async def handler(request):
            return httpx.Response(429, json={"responseCode": 100})

        with patch.object(cv, "cache", recording):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
                statuses = await cv.check_identifiers(
                    cv.find_identifiers(f"doi:{FAKE_DOI}"), client=c
                )
        assert statuses == {("doi", FAKE_DOI.lower()): cv.UNCHECKED}
        assert recording.sets == []

    async def test_slow_cache_reads_count_against_the_one_deadline(self):
        recording = _RecordingCache(get_delay=5.0)
        seen: List[httpx.Request] = []
        with patch.object(cv, "cache", recording):
            async with _client(seen) as client:
                loop = asyncio.get_running_loop()
                started = loop.time()
                statuses = await cv.check_identifiers(
                    cv.find_identifiers(f"doi:{REAL_DOI}"),
                    budget_seconds=0.2,
                    client=client,
                )
                elapsed = loop.time() - started
        assert elapsed < 2.0
        assert statuses[("doi", REAL_DOI.lower())] == cv.UNCHECKED
        assert seen == []

    async def test_no_cache_reads_past_max_lookups(self):
        recording = _RecordingCache()
        dois = " ".join(f"doi:10.1000/item{n}" for n in range(cv.MAX_LOOKUPS + 10))
        seen: List[httpx.Request] = []
        with patch.object(cv, "cache", recording):
            async with _client(seen, fail=True) as client:
                await cv.check_identifiers(cv.find_identifiers(dois), client=client)
        assert len(recording.gets) == cv.MAX_LOOKUPS
        assert len(seen) == cv.MAX_LOOKUPS


@pytest.mark.asyncio
class TestConsentHook:
    async def test_no_request_when_consent_says_no(self, checks_on):
        seen: List[httpx.Request] = []
        citation = f"Polack FP. doi:{REAL_DOI}"

        async def no():
            return False

        async with _client(seen) as client:
            out = await cv.verify_citations([citation], client=client, may_send=no)
        assert seen == []
        assert out == [cv.UNVERIFIED_LABEL + citation]

    async def test_no_request_when_the_consent_check_fails(self, checks_on):
        seen: List[httpx.Request] = []

        async def broken():
            raise RuntimeError("db down")

        async with _client(seen) as client:
            await cv.verify_citations(
                [f"doi:{REAL_DOI}"], client=client, may_send=broken
            )
        assert seen == []

    async def test_malformed_identifiers_still_go_without_consent(self, checks_on):
        async def no():
            return False

        assert (
            await cv.verify_citations(["Some trial. PMID: 12AB34"], may_send=no) == []
        )


@pytest.mark.asyncio
class TestCuttingAndLabelling:
    async def _verify(self, citations, **kw):
        seen: List[httpx.Request] = []
        async with _client(seen, **kw) as client:
            return await cv.verify_citations(citations, client=client)

    async def test_a_missing_doi_does_not_cut_into_a_longer_valid_one(self, checks_on):
        answer = (
            "Evidence for the treatment. " * 20
            + "First doi:10.1056/NEJM then doi:"
            + REAL_DOI
            + "."
        )
        (out,) = await self._verify([answer])
        assert REAL_DOI in out
        assert "10.1056/NEJM then" not in out
        assert out.count(cv.REMOVED_REFERENCE) == 1

    async def test_every_form_of_a_missing_pmid_is_cut(self, checks_on):
        answer = (
            "Evidence for the treatment. " * 20
            + f"PMID: {FAKE_PMID}, also https://pubmed.ncbi.nlm.nih.gov/{FAKE_PMID}/ "
            + f"and PMID {REAL_PMID}."
        )
        (out,) = await self._verify([answer])
        assert FAKE_PMID not in out
        assert REAL_PMID in out
        assert out.count(cv.REMOVED_REFERENCE) == 2

    async def test_a_long_answer_with_missing_and_unchecked_is_labelled(
        self, checks_on
    ):
        async def handler(request):
            url = str(request.url)
            if "notreal" in url.lower():
                return httpx.Response(404, json={"responseCode": 100})
            raise httpx.ConnectTimeout("slow", request=request)

        answer = (
            "Evidence for the treatment. " * 20
            + f"See doi:{FAKE_DOI} and doi:10.1000/slow1."
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            (out,) = await cv.verify_citations([answer], client=c)
        assert out.startswith(cv.UNVERIFIED_LABEL)
        assert FAKE_DOI not in out
        assert "10.1000/slow1" in out


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
class TestRealVerifierThroughCitationGeneration:
    """The real verifier, consent gate and all, run through
    _generate_citations_for_denial with the registries faked."""

    async def _run(self, use_external, model_citations, during_generation=None):
        denial = await Denial.objects.acreate(
            denial_text="",
            procedure="mri brain",
            diagnosis="headache",
            use_external=use_external,
        )
        seen: List[httpx.Request] = []

        async def handler(request):
            seen.append(request)
            url = str(request.url)
            if "notreal" in url.lower():
                return httpx.Response(404, json={"responseCode": 100})
            raise httpx.ConnectTimeout("slow", request=request)

        async def generic(*args, **kwargs):
            if during_generation is not None:
                await during_generation(denial)
            return list(model_citations)

        with (
            patch.object(cv, "checks_enabled", return_value=True),
            patch.object(
                cv,
                "_new_client",
                lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            ),
            patch.object(MLCitationsHelper, "generate_generic_citations", generic),
            patch.object(
                MLCitationsHelper,
                "generate_cms_coverage_citations",
                new_callable=AsyncMock,
                return_value=["CMS NCD 220.2"],
            ),
        ):
            result = await MLCitationsHelper._generate_citations_for_denial(
                denial=denial, timeout=30
            )
        return result, seen

    async def test_a_mixed_long_answer_is_trimmed_and_labelled(self):
        answer = (
            "Evidence for the treatment. " * 20
            + f"See doi:{FAKE_DOI} and doi:10.1000/slow1."
        )
        result, seen = await self._run(True, [answer])
        assert len(seen) == 2
        out, cms = result
        assert out.startswith(cv.UNVERIFIED_LABEL)
        assert FAKE_DOI not in out and "10.1000/slow1" in out
        assert cms == "CMS NCD 220.2"

    async def test_an_opted_out_person_means_no_requests(self):
        result, seen = await self._run(False, [f"Made up. doi:{FAKE_DOI}"])
        assert seen == []
        assert result == [
            cv.UNVERIFIED_LABEL + f"Made up. doi:{FAKE_DOI}",
            "CMS NCD 220.2",
        ]

    async def test_withdrawing_consent_mid_generation_means_no_requests(self):
        async def withdraw(denial):
            await Denial.objects.filter(denial_id=denial.denial_id).aupdate(
                health_history_consent=False
            )

        _result, seen = await self._run(
            True, [f"Made up. doi:{FAKE_DOI}"], during_generation=withdraw
        )
        assert seen == []
