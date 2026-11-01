"""Tests for ml/citation_verification.py: identifiers in model-written
citations are found, checked against their registries (faked here with
httpx.MockTransport behind the checker's untraced client), and the citations
dropped, trimmed or labelled."""

import asyncio
import json
import time
from typing import List
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import unquote

import httpx
import pytest

from fighthealthinsurance.ml import citation_verification as cv
from fighthealthinsurance.ml.ml_citations_helper import (
    MLCitationsHelper,
    _citation_guard_text,
    _may_check_citations_outside,
)
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


async def _yes():
    return True


async def _no():
    return False


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
            # eCFR's answer for a part that does not exist.
            return httpx.Response(404, json={})
        return httpx.Response(500)

    return handler


def _client(seen, **kw) -> cv.UntracedClient:
    return cv.UntracedClient(httpx.MockTransport(_registry(seen, **kw)))


def _client_for(handler) -> cv.UntracedClient:
    return cv.UntracedClient(httpx.MockTransport(handler))


def _key(kind, value, malformed=False):
    return (kind, value, malformed)


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


async def _check(citation, may_send=_yes, **kw):
    seen: List[httpx.Request] = []
    client_kw = {k: kw.pop(k) for k in ("delay", "fail", "ecfr") if k in kw}
    async with _client(seen, **client_kw) as client:
        statuses = await cv.check_identifiers(
            cv.find_identifiers(citation), client=client, may_send=may_send, **kw
        )
    return statuses, seen


async def _verify(citations, may_send=_yes, **kw):
    seen: List[httpx.Request] = []
    client_kw = {k: kw.pop(k) for k in ("delay", "fail", "ecfr") if k in kw}
    async with _client(seen, **client_kw) as client:
        out = await cv.verify_citations(
            citations, client=client, may_send=may_send, **kw
        )
    return out, seen


class TestFindIdentifiers:
    def test_finds_a_doi_and_drops_trailing_punctuation(self):
        ids = cv.find_identifiers(f"Polack et al. NEJM 2020. doi:{REAL_DOI}.")
        assert ids == [cv.Identifier("doi", REAL_DOI.lower())]

    def test_a_doi_of_the_wrong_form_is_not_sendable(self):
        ids = cv.find_identifiers("Some study. doi: 10.12/abc")
        assert [(i.kind, i.malformed) for i in ids] == [("doi", True)]

    def test_finds_pmids_in_text_and_pubmed_urls(self):
        ids = cv.find_identifiers(
            f"PMID: {REAL_PMID}; https://pubmed.ncbi.nlm.nih.gov/{FAKE_PMID}/"
        )
        assert {i.value for i in ids if i.kind == "pmid"} == {REAL_PMID, FAKE_PMID}

    def test_reads_cfr_part_and_section(self):
        ids = cv.find_identifiers("29 C.F.R. § 2560.503-1 and 45 CFR 147.136(b)")
        assert {i.value for i in ids if i.kind == "cfr"} == {
            "29/2560/2560.503-1",
            "45/147/147.136",
        }

    def test_a_cfr_title_past_50_is_not_sendable(self):
        ids = cv.find_identifiers("99 CFR 12.3")
        assert [(i.kind, i.malformed) for i in ids] == [("cfr", True)]

    def test_reads_us_code_sections(self):
        ids = cv.find_identifiers("42 U.S.C. § 300gg-19")
        assert ids == [cv.Identifier("usc", "42/300gg-19")]

    def test_plain_text_reference_has_no_identifiers(self):
        assert cv.find_identifiers("ACR Appropriateness Criteria: Low Back Pain") == []


class TestParsingRealWorldShapes:
    @pytest.mark.parametrize(
        "citation, expected",
        [
            (
                "DOI: https://doi.org/10.1056/NEJMoa2001017",
                cv.Identifier("doi", "10.1056/nejmoa2001017"),
            ),
            (
                "**DOI:** 10.1056/NEJMoa2001017",
                cv.Identifier("doi", "10.1056/nejmoa2001017"),
            ),
            (
                "DOI: [10.1056/NEJMoa2001017](https://doi.org/10.1056/NEJMoa2001017)",
                cv.Identifier("doi", "10.1056/nejmoa2001017"),
            ),
            (
                "“10.1056/NEJMoa2001017”",
                cv.Identifier("doi", "10.1056/nejmoa2001017"),
            ),
            (
                "https://doi.org/10.1056/NEJMoa2001017?utm_source=chatgpt.com",
                cv.Identifier("doi", "10.1056/nejmoa2001017"),
            ),
            (
                "https://doi.org/10.1056/NEJMoa2001017#abstract",
                cv.Identifier("doi", "10.1056/nejmoa2001017"),
            ),
            ("**PMID:** 32109013", cv.Identifier("pmid", "32109013")),
            (
                "https://pubmed.ncbi.nlm.nih.gov/32109013.",
                cv.Identifier("pmid", "32109013"),
            ),
            ("**29 CFR 2560.503-1**", cv.Identifier("cfr", "29/2560/2560.503-1")),
            ("42 U.S.C. § 300gg–19", cv.Identifier("usc", "42/300gg-19")),
            (
                "45 CFR 147.136(b)(1)–(3)",
                cv.Identifier("cfr", "45/147/147.136"),
            ),
            (
                "Lancet. 10.1016/S0140-6736(20)30183-5.",
                cv.Identifier("doi", "10.1016/s0140-6736(20)30183-5"),
            ),
        ],
    )
    def test_reads_the_identifier(self, citation, expected):
        assert cv.find_identifiers(citation) == [expected]

    def test_doi_not_available_is_not_an_identifier(self):
        assert cv.find_identifiers("DOI: N/A") == []

    def test_prose_about_dois_is_not_an_identifier(self):
        assert cv.find_identifiers("include the DOI number in the appeal") == []

    @pytest.mark.parametrize(
        "citation", ["42 CFR 438.400–438.424", "45 CFR 147.136-147.140"]
    )
    def test_a_section_range_is_never_looked_up(self, citation):
        (ident,) = cv.find_identifiers(citation)
        assert ident.kind == "cfr" and ident.malformed

    def test_a_doi_cut_off_at_a_tag_is_never_sent(self):
        (ident,) = cv.find_identifiers("doi: 10.1056/NEJMoa20<br>01017")
        assert ident.malformed

    @pytest.mark.parametrize("char", ["#x", "+", "&", "%2F", ",x", "*x", "?x"])
    def test_odd_characters_in_a_doi_make_it_unsendable(self, char):
        idents = cv.find_identifiers(f"see 10.1234/abc{char}def")
        assert idents and all(i.kind == "doi" for i in idents)
        assert not any(cv._sendable(i) for i in idents)

    def test_a_markdown_link_yields_the_one_valid_doi_twice(self):
        citation = f"[{REAL_DOI}](https://doi.org/{REAL_DOI})"
        assert cv.find_identifiers(citation) == [cv.Identifier("doi", REAL_DOI.lower())]
        assert len(cv.find_occurrences(citation)) == 2

    @pytest.mark.parametrize(
        "citation, kind",
        [
            ("https://pubmed.ncbi.nlm.nih.gov/33301246oops", "pmid"),
            ("45 CFR 147.136oops", "cfr"),
            ("42 USC 300gg-???", "usc"),
        ],
    )
    def test_a_valid_prefix_with_junk_stuck_on_is_not_sendable(self, citation, kind):
        assert [(i.kind, i.malformed) for i in cv.find_identifiers(citation)] == [
            (kind, True)
        ]

    @pytest.mark.parametrize(
        "citation",
        [
            "doi:10.1234/patient=Rebecca_Crumpler&diagnosis=HIV",
            "see 10.1234/../../../x",
            "10.1234/" + "a" * 101,
        ],
    )
    def test_shapes_that_are_never_sent(self, citation):
        idents = cv.find_identifiers(citation)
        assert idents and not any(cv._sendable(i) for i in idents)

    def test_a_long_run_of_spaces_parses_fast(self):
        started = time.monotonic()
        cv.find_occurrences("PMID" + " " * 100_000 + "1")
        cv.find_occurrences("45" + " " * 100_000 + "CFR 147")
        assert time.monotonic() - started < 1.0


@pytest.mark.asyncio
class TestCheckIdentifiers:
    async def test_registry_answers_set_found_and_missing(self):
        statuses, _seen = await _check(
            f"doi:{REAL_DOI} doi:{FAKE_DOI} PMID {REAL_PMID} PMID {FAKE_PMID} "
            "45 CFR 147.136 45 CFR 147.999"
        )
        assert statuses[_key("doi", REAL_DOI.lower())] == cv.FOUND
        assert statuses[_key("doi", FAKE_DOI.lower())] == cv.MISSING
        assert statuses[_key("pmid", REAL_PMID)] == cv.FOUND
        assert statuses[_key("pmid", FAKE_PMID)] == cv.MISSING
        assert statuses[_key("cfr", "45/147/147.136")] == cv.FOUND
        assert statuses[_key("cfr", "45/147/147.999")] == cv.MISSING

    async def test_all_pmids_go_in_one_esummary_call(self):
        _statuses, seen = await _check(f"PMID {REAL_PMID}, PMID {FAKE_PMID}")
        assert sum("esummary" in str(r.url) for r in seen) == 1

    async def test_a_registry_error_leaves_the_identifier_unchecked(self):
        statuses, _seen = await _check(f"doi:{REAL_DOI} PMID {REAL_PMID}", fail=True)
        assert set(statuses.values()) == {cv.UNCHECKED}

    async def test_a_slow_registry_is_cut_off_at_the_budget(self):
        started = time.monotonic()
        statuses, _seen = await _check(f"doi:{REAL_DOI}", delay=5.0, budget_seconds=0.2)
        assert statuses[_key("doi", REAL_DOI.lower())] == cv.UNCHECKED
        assert time.monotonic() - started < 2.0

    async def test_a_genuine_section_is_found(self):
        statuses, _seen = await _check("45 CFR 147.136")
        assert statuses == {_key("cfr", "45/147/147.136"): cv.FOUND}

    async def test_a_genuine_part_is_found(self):
        statuses, _seen = await _check("45 CFR Part 147")
        assert statuses == {_key("cfr", "45/147/"): cv.FOUND}

    async def test_a_section_the_part_does_not_list_is_missing(self):
        statuses, _seen = await _check("45 CFR 147.999")
        assert statuses == {_key("cfr", "45/147/147.999"): cv.MISSING}

    async def test_ecfrs_empty_object_404_is_missing(self):
        statuses, _seen = await _check("45 CFR 999.1")
        assert statuses == {_key("cfr", "45/999/999.1"): cv.MISSING}

    @pytest.mark.parametrize(
        "response",
        [
            httpx.Response(404, json=[]),
            httpx.Response(404, json={"message": "no route"}),
            httpx.Response(404, json={"status": 404, "error": "Not Found"}),
            httpx.Response(200, json={"error": "something else"}),
            httpx.Response(
                200,
                json={
                    "identifier": "45",
                    "type": "title",
                    "children": [{"identifier": "147", "type": "part"}],
                },
            ),
        ],
    )
    async def test_other_ecfr_answers_are_unchecked(self, response):
        async def handler(request):
            return response

        async with _client_for(handler) as c:
            statuses = await cv.check_identifiers(
                cv.find_identifiers("45 CFR 147.136"), client=c, may_send=_yes
            )
        assert statuses == {_key("cfr", "45/147/147.136"): cv.UNCHECKED}

    async def test_a_bot_check_redirect_is_unchecked(self):
        statuses, seen = await _check("45 CFR 147.136", ecfr="redirect")
        assert statuses == {_key("cfr", "45/147/147.136"): cv.UNCHECKED}
        assert len(seen) == 1

    async def test_a_challenge_page_is_unchecked(self):
        statuses, _seen = await _check("45 CFR 147.999", ecfr="challenge")
        assert statuses == {_key("cfr", "45/147/147.999"): cv.UNCHECKED}

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

        async with _client_for(handler) as c:
            statuses = await cv.check_identifiers(
                cv.find_identifiers(f"doi:{FAKE_DOI}"), client=c, may_send=_yes
            )
        assert statuses == {_key("doi", FAKE_DOI.lower()): cv.UNCHECKED}

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
            httpx.Response(
                200,
                json={
                    "result": {
                        "uids": [REAL_PMID],
                        REAL_PMID: {
                            "uid": REAL_PMID,
                            "error": "backend server not found",
                        },
                    }
                },
            ),
            httpx.Response(
                200,
                json={"result": {"uids": [REAL_PMID], REAL_PMID: {"uid": REAL_PMID}}},
            ),
        ],
    )
    async def test_an_unexpected_ncbi_answer_is_unchecked(self, response):
        async def handler(request):
            return response

        async with _client_for(handler) as c:
            statuses = await cv.check_identifiers(
                cv.find_identifiers(f"PMID {REAL_PMID}"), client=c, may_send=_yes
            )
        assert statuses == {_key("pmid", REAL_PMID): cv.UNCHECKED}

    async def test_us_code_is_never_looked_up(self):
        statuses, seen = await _check("42 U.S.C. § 300gg-19")
        assert statuses == {_key("usc", "42/300gg-19"): cv.FORM_ONLY}
        assert seen == []

    async def test_a_malformed_occurrence_keeps_its_own_verdict(self):
        statuses, _seen = await _check(f"doi:{REAL_DOI} and doi: 10.12/abc")
        assert statuses[_key("doi", REAL_DOI.lower())] == cv.FOUND
        assert statuses[_key("doi", "10.12/abc", True)] == cv.UNCHECKED


@pytest.mark.asyncio
class TestWhatIsSent:
    async def test_leak_and_traversal_dois_make_no_request(self, checks_on):
        out, seen = await _verify(
            [
                "Smith. doi:10.1234/patient=Rebecca_Crumpler&diagnosis=HIV",
                "Jones. 10.1234/../../../x",
            ]
        )
        assert seen == []
        assert all(c.startswith(cv.UNVERIFIED_LABEL) for c in out)

    async def test_a_doi_is_sent_as_one_encoded_path_segment(self):
        _statuses, seen = await _check(f"doi:{REAL_DOI}")
        (request,) = seen
        assert request.url.raw_path.decode().endswith("10.1056%2Fnejmoa2034577")

    async def test_only_identifiers_reach_the_registries(self):
        _statuses, seen = await _check(
            f"Jane Doe's knee MRI follow-up. doi:{REAL_DOI} PMID {REAL_PMID}"
        )
        sent = " ".join(str(r.url) + r.content.decode() for r in seen).lower()
        assert seen
        for word in ("jane", "doe", "knee", "mri", "follow"):
            assert word not in sent

    async def test_an_ambient_ncbi_key_is_not_sent(self):
        with patch.dict("os.environ", {"NCBI_API_KEY": "ambient-key"}):
            _statuses, seen = await _check(f"PMID {REAL_PMID}")
        assert "ambient-key" not in str(seen[0].url)
        assert "api_key" not in seen[0].url.params

    async def test_requests_bypass_asyncclient_send_and_carry_no_trace_headers(
        self,
    ):
        """AsyncClient.send is what the error tracker's httpx integration
        wraps; the checker must not go through it."""
        with patch.object(
            httpx.AsyncClient, "send", side_effect=AssertionError("traced path")
        ):
            statuses, seen = await _check(f"doi:{REAL_DOI}")
        assert statuses[_key("doi", REAL_DOI.lower())] == cv.FOUND
        headers = {k.lower() for k in seen[0].headers.keys()}
        assert "sentry-trace" not in headers and "baggage" not in headers

    async def test_a_doi_overlapping_the_persons_text_is_not_sent(self):
        statuses, seen = await _check(
            "doi:10.1234/crumpler2020",
            guard_text="Patient Rebecca CRUMPLER was denied an MRI.",
        )
        assert seen == []
        assert statuses == {_key("doi", "10.1234/crumpler2020"): cv.UNCHECKED}

    async def test_a_doi_with_no_overlap_is_sent(self):
        _statuses, seen = await _check(
            f"doi:{REAL_DOI}", guard_text="Denied a knee MRI in March."
        )
        assert len(seen) == 1


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
        with patch.object(cv, "cache", recording):
            await _check(f"doi:{REAL_DOI} doi:{FAKE_DOI}")
            await _check("doi:10.1000/timesout", fail=True)
        assert sorted(v for _k, v in recording.sets) == [cv.FOUND, cv.MISSING]

    async def test_a_refusal_is_not_cached(self):
        recording = _RecordingCache()

        async def handler(request):
            return httpx.Response(429, json={"responseCode": 100})

        with patch.object(cv, "cache", recording):
            async with _client_for(handler) as c:
                statuses = await cv.check_identifiers(
                    cv.find_identifiers(f"doi:{FAKE_DOI}"), client=c, may_send=_yes
                )
        assert statuses == {_key("doi", FAKE_DOI.lower()): cv.UNCHECKED}
        assert recording.sets == []

    async def test_slow_cache_reads_count_against_the_one_deadline(self):
        recording = _RecordingCache(get_delay=5.0)
        started = time.monotonic()
        with patch.object(cv, "cache", recording):
            statuses, seen = await _check(f"doi:{REAL_DOI}", budget_seconds=0.2)
        assert time.monotonic() - started < 2.0
        assert statuses[_key("doi", REAL_DOI.lower())] == cv.UNCHECKED
        assert seen == []

    async def test_no_cache_reads_past_max_lookups(self):
        recording = _RecordingCache()
        dois = " ".join(f"doi:10.1000/item{n}" for n in range(cv.MAX_LOOKUPS + 10))
        with patch.object(cv, "cache", recording):
            _statuses, seen = await _check(dois, fail=True)
        assert len(recording.gets) == cv.MAX_LOOKUPS
        assert len(seen) == cv.MAX_LOOKUPS


@pytest.mark.asyncio
class TestConsent:
    async def test_no_request_without_a_consent_check(self, checks_on):
        citation = f"Polack FP. doi:{REAL_DOI}"
        out, seen = await _verify([citation], may_send=None)
        assert seen == []
        assert out == [cv.UNVERIFIED_LABEL + citation]

    async def test_no_request_when_consent_says_no(self, checks_on):
        citation = f"Polack FP. doi:{REAL_DOI}"
        out, seen = await _verify([citation], may_send=_no)
        assert seen == []
        assert out == [cv.UNVERIFIED_LABEL + citation]

    async def test_no_request_when_the_consent_check_fails(self, checks_on):
        async def broken():
            raise RuntimeError("db down")

        _out, seen = await _verify([f"doi:{REAL_DOI}"], may_send=broken)
        assert seen == []

    async def test_consent_is_asked_again_before_later_requests(self):
        answers = [True, False]
        asked = []

        async def changing():
            asked.append(1)
            return answers[min(len(asked) - 1, 1)]

        async def handler(request):
            await asyncio.sleep(0.05)
            return httpx.Response(404, json={"responseCode": 100})

        dois = " ".join(f"doi:10.1000/item{n}" for n in range(8))
        with (
            patch.object(cv, "MAX_CONCURRENT_LOOKUPS", 1),
            patch.object(cv, "CONSENT_RECHECK_SECONDS", 0.0),
        ):
            async with _client_for(handler) as c:
                statuses = await cv.check_identifiers(
                    cv.find_identifiers(dois), client=c, may_send=changing
                )
        assert len(asked) >= 2
        assert list(statuses.values()).count(cv.MISSING) < 8


@pytest.mark.asyncio
class TestCuttingAndLabelling:
    async def test_drops_a_short_citation_with_a_missing_doi(self, checks_on):
        good = f"Polack FP. NEJM 2020. doi:{REAL_DOI}"
        bad = f"Invented T. J Fake Med 2021. doi:{FAKE_DOI}"
        out, _seen = await _verify([good, bad])
        assert out == [good]

    async def test_labels_a_short_citation_with_an_unsendable_pmid(self, checks_on):
        out, seen = await _verify(["Some trial. PMID: 12AB34"])
        assert out == [cv.UNVERIFIED_LABEL + "Some trial. PMID: 12AB34"]
        assert seen == []

    async def test_cuts_a_missing_identifier_out_of_a_long_answer(self, checks_on):
        answer = (
            "Here is the evidence for the treatment. " * 15
            + f"See doi:{FAKE_DOI} and doi:{REAL_DOI}."
        )
        (out,), _seen = await _verify([answer])
        assert FAKE_DOI not in out
        assert REAL_DOI in out
        assert cv.REMOVED_REFERENCE in out

    async def test_a_missing_doi_does_not_cut_into_a_longer_valid_one(self, checks_on):
        answer = (
            "Evidence for the treatment. " * 20
            + "First doi:10.1056/NEJM then doi:"
            + REAL_DOI
            + "."
        )
        (out,), _seen = await _verify([answer])
        assert REAL_DOI in out
        assert "10.1056/NEJM then" not in out
        assert out.count(cv.REMOVED_REFERENCE) == 1

    async def test_every_form_of_a_missing_pmid_is_cut(self, checks_on):
        answer = (
            "Evidence for the treatment. " * 20
            + f"PMID: {FAKE_PMID}, also https://pubmed.ncbi.nlm.nih.gov/{FAKE_PMID}/ "
            + f"and PMID {REAL_PMID}."
        )
        (out,), _seen = await _verify([answer])
        assert FAKE_PMID not in out
        assert REAL_PMID in out
        assert out.count(cv.REMOVED_REFERENCE) == 2

    async def test_a_long_answer_with_missing_and_unchecked_is_labelled(
        self, checks_on
    ):
        async def handler(request):
            if "notreal" in str(request.url).lower():
                return httpx.Response(404, json={"responseCode": 100})
            raise httpx.ConnectTimeout("slow", request=request)

        answer = (
            "Evidence for the treatment. " * 20
            + f"See doi:{FAKE_DOI} and doi:10.1000/slow1."
        )
        async with _client_for(handler) as c:
            (out,) = await cv.verify_citations([answer], client=c, may_send=_yes)
        assert out.startswith(cv.UNVERIFIED_LABEL)
        assert FAKE_DOI not in out
        assert "10.1000/slow1" in out

    async def test_labels_a_citation_it_could_not_check(self, checks_on):
        citation = f"Polack FP. NEJM 2020. doi:{REAL_DOI}"
        out, _seen = await _verify([citation], fail=True)
        assert out == [cv.UNVERIFIED_LABEL + citation]

    async def test_keeps_found_and_identifier_free_citations_in_order(self, checks_on):
        citations = [
            "ACR Appropriateness Criteria: Low Back Pain",
            f"Polack FP. NEJM 2020. PMID {REAL_PMID}",
            "45 CFR 147.136, internal claims and appeals",
            "42 U.S.C. § 300gg-19",
        ]
        out, _seen = await _verify(citations)
        assert out == citations

    async def test_labels_references_on_an_unexpected_error(self, checks_on):
        citations = [f"Polack FP. doi:{REAL_DOI}", "ACR Appropriateness Criteria"]
        with patch.object(cv, "check_identifiers", side_effect=RuntimeError("boom")):
            assert await cv.verify_citations(citations, may_send=_yes) == [
                cv.UNVERIFIED_LABEL + citations[0],
                citations[1],
            ]

    async def test_an_overlong_citation_is_labelled(self, checks_on):
        citation = "Evidence. " * 3000 + f"doi:{REAL_DOI}"
        out, _seen = await _verify([citation])
        assert out == [cv.UNVERIFIED_LABEL + citation]

    async def test_off_by_default_in_tests_and_returns_citations_unchanged(self):
        citations = [f"Made up. doi:{FAKE_DOI}"]
        assert await cv.verify_citations(citations, may_send=_yes) == citations

    async def test_stored_shape_stays_a_list_of_strings(self, checks_on):
        out, _seen = await _verify([f"doi:{REAL_DOI}", f"doi:{FAKE_DOI}", "plain"])
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

    async def test_guard_text_holds_the_persons_fields(self):
        denial = MagicMock(spec=Denial)
        denial.denial_text = "Denied."
        denial.health_history = "History."
        denial.raw_email = "a@example.com"
        denial.employer_name = "Acme"
        denial.claim_id = "C-123"
        text = _citation_guard_text(denial)
        for part in ("Denied.", "History.", "a@example.com", "Acme", "C-123"):
            assert part in text

    async def test_an_in_memory_opt_out_is_respected_without_a_read(self):
        denial = MagicMock(spec=Denial)
        denial.use_external = False
        denial.denial_id = 1
        with patch.object(Denial.objects, "filter", side_effect=AssertionError):
            assert await _may_check_citations_outside(denial) is False


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
            if "notreal" in str(request.url).lower():
                return httpx.Response(404, json={"responseCode": 100})
            raise httpx.ConnectTimeout("slow", request=request)

        async def generic(*args, **kwargs):
            if during_generation is not None:
                await during_generation(denial)
            return list(model_citations)

        with (
            patch.object(cv, "checks_enabled", return_value=True),
            patch.object(cv, "_new_client", lambda: _client_for(handler)),
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


class TestUntracedClientProxy:
    def test_uses_the_environment_proxy_like_an_httpx_client(self):
        with patch.dict(
            "os.environ",
            {"https_proxy": "http://proxy.internal:3128", "no_proxy": "skip.example"},
            clear=True,
        ):
            client = cv.UntracedClient()
            via_proxy = client._transport_for("https://doi.org/api/handles/x")
            direct = client._transport_for("https://skip.example/x")
        assert via_proxy is not direct
        assert client._transports.keys() == {"http://proxy.internal:3128", None}


class TestRoundTwoEdges:
    def test_a_doi_running_into_the_scan_cap_is_not_sendable(self):
        filler = "x" * (cv.MAX_SCAN_CHARS - 12)
        citation = filler + " 10.1056/NEJMoa2034577 more text"
        (ident,) = cv.find_identifiers(citation)
        assert ident.kind == "doi" and ident.malformed

    @pytest.mark.parametrize(
        "tail",
        [
            " PMID: 33301246 more",
            " https://pubmed.ncbi.nlm.nih.gov/33301246/ more",
            " 45 CFR 147.136 more",
        ],
    )
    def test_other_forms_running_into_the_scan_cap_are_not_sendable(self, tail):
        cut = len(tail.split(" more")[0]) - 3
        citation = "x" * (cv.MAX_SCAN_CHARS - cut) + tail
        idents = cv.find_identifiers(citation)
        assert idents and all(i.malformed for i in idents)

    def test_a_scan_cap_inside_plain_text_leaves_earlier_identifiers_alone(self):
        citation = f"doi:{REAL_DOI} " + "x" * cv.MAX_SCAN_CHARS
        assert cv.find_identifiers(citation) == [cv.Identifier("doi", REAL_DOI.lower())]

    @pytest.mark.parametrize(
        "citation", ["DOI: https://doi.org/10.12/bad", "DOI: 10.1056/"]
    )
    def test_a_doi_mention_nothing_was_read_from_is_flagged(self, citation):
        idents = cv.find_identifiers(citation)
        assert idents and all(i.kind == "doi" and i.malformed for i in idents)

    def test_a_doi_mention_covered_by_a_read_doi_adds_nothing(self):
        assert cv.find_identifiers(f"DOI: https://doi.org/{REAL_DOI}") == [
            cv.Identifier("doi", REAL_DOI.lower())
        ]

    @pytest.mark.parametrize(
        "citation",
        [
            "42 CFR §§ 438.400–424",
            "42 CFR 438.400-424",
            "45 CFR 147.136 through 147.140",
            "45 CFR 147.136 to 147.140",
        ],
    )
    def test_abbreviated_and_worded_ranges_are_never_looked_up(self, citation):
        (ident,) = cv.find_identifiers(citation)
        assert ident.kind == "cfr" and ident.malformed

    @pytest.mark.parametrize(
        "citation, value",
        [
            ("29 CFR 2560.503-1", "29/2560/2560.503-1"),
            ("29 CFR 2590.715-2719A", "29/2590/2590.715-2719A"),
        ],
    )
    def test_hyphenated_section_numbers_stay_sections(self, citation, value):
        assert cv.find_identifiers(citation) == [cv.Identifier("cfr", value)]


@pytest.mark.asyncio
class TestGuardBounds:
    async def test_large_guard_text_is_fast(self):
        guard = ("lorem ipsum dolor sit amet " * 7000)[: cv.MAX_GUARD_CHARS]
        started = time.monotonic()
        statuses, seen = await _check(f"doi:{REAL_DOI}", guard_text=guard)
        assert time.monotonic() - started < 2.0
        assert len(seen) == 1

    async def test_guard_text_past_the_cap_blocks_every_doi(self):
        statuses, seen = await _check(
            f"doi:{REAL_DOI} PMID {REAL_PMID}",
            guard_text="z" * (cv.MAX_GUARD_CHARS + 1),
        )
        assert statuses[_key("doi", REAL_DOI.lower())] == cv.UNCHECKED
        assert statuses[_key("pmid", REAL_PMID)] == cv.FOUND
        assert [r for r in seen if "doi.org" in str(r.url)] == []

    async def test_guard_rejected_dois_count_toward_max_lookups(self):
        held = " ".join(f"doi:10.1000/held{n}" for n in range(cv.MAX_LOOKUPS))
        statuses, seen = await _check(held + f" doi:{REAL_DOI}", guard_text="held" * 3)
        assert seen == []
        assert statuses[_key("doi", REAL_DOI.lower())] == cv.UNCHECKED


class TestProxyLikeHttpx:
    def _proxy_for(self, env, url):
        with patch.dict("os.environ", env, clear=True):
            client = cv.UntracedClient()
            transport = client._transport_for(url)
        return {id(t): k for k, t in client._transports.items()}[id(transport)]

    def test_a_scheme_less_proxy_still_works(self):
        assert (
            self._proxy_for(
                {"HTTPS_PROXY": "proxy.example:3128"}, "https://doi.org/api/x"
            )
            == "http://proxy.example:3128"
        )

    def test_no_proxy_with_a_scheme_still_matches(self):
        assert (
            self._proxy_for(
                {
                    "HTTPS_PROXY": "http://proxy.example:3128",
                    "NO_PROXY": "https://doi.org",
                },
                "https://doi.org/api/x",
            )
            is None
        )

    def test_no_proxy_star_bypasses_everything(self):
        assert (
            self._proxy_for(
                {"HTTPS_PROXY": "http://proxy.example:3128", "NO_PROXY": "*"},
                "https://doi.org/api/x",
            )
            is None
        )
