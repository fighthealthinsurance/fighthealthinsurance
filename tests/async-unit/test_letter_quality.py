"""ml/letter_quality.py: the TypeSafe draft scorer.

The scorer decides the ORDER letters appear in for every user, so its failure
mode matters more than its happy path: any doubt must mean "no score", never a
wrong score and never a broken stream.
"""

import asyncio
from unittest.mock import patch

import pytest
from django.test import override_settings

import aiohttp

from fighthealthinsurance.ml import letter_quality as lq
from fighthealthinsurance.ml import typesafe

ENABLED = dict(TYPESAFE_API_KEY="test-key", TYPESAFE_LETTER_RANKING_ENABLED=True)


def _payload(**scores):
    answers = {q: {"score": scores.get(q, 2)} for q in lq.SCORE_QUESTIONS}
    return {"answers": answers, "usage": {"input_tokens": 1234}}


class TestEnabled:
    def test_off_without_a_key(self):
        with override_settings(TYPESAFE_API_KEY=None, TYPESAFE_LETTER_RANKING_ENABLED=True):
            assert lq.enabled() is False

    def test_off_without_the_flag(self):
        with override_settings(TYPESAFE_API_KEY="k", TYPESAFE_LETTER_RANKING_ENABLED=False):
            assert lq.enabled() is False

    def test_on_with_both(self):
        with override_settings(**ENABLED):
            assert lq.enabled() is True


class TestBuildDocument:
    def test_letter_follows_denial_under_labelled_headers(self):
        doc = lq.build_document("Denied: not medically necessary.", "Dear reviewer,")
        assert doc.startswith("THE DENIAL:\nDenied: not medically necessary.")
        assert doc.endswith("THE APPEAL LETTER:\nDear reviewer,")

    def test_denial_gives_way_before_the_letter(self):
        letter = "L" * 20_000
        denial = "D" * 20_000
        doc = lq.build_document(denial, letter)
        assert len(doc) <= lq.DOCUMENT_CHAR_CAP
        assert doc.endswith(letter), "the letter must survive intact"
        assert doc.count("D") < 20_000

    def test_denial_is_cut_from_the_end(self):
        denial = "REASON FIRST " + "x" * 30_000
        doc = lq.build_document(denial, "short letter")
        assert "REASON FIRST" in doc

    def test_missing_denial_is_tolerated(self):
        doc = lq.build_document(None, "letter")
        assert "THE DENIAL:\n\n\nTHE APPEAL LETTER:\nletter" == doc


class TestParseAnswers:
    def test_composite_is_the_mean_over_the_max(self):
        score = lq.parse_answers(_payload(cites_denial=2, medical_necessity=1, no_invented_facts=2, tone_and_form=1))
        assert score.quality == pytest.approx(6 / 8)
        assert score.grounding == 2
        assert score.input_tokens == 1234

    def test_grounding_is_the_invented_facts_answer_alone(self):
        score = lq.parse_answers(_payload(no_invented_facts=0))
        assert score.grounding == 0
        assert score.quality == pytest.approx(6 / 8)

    def test_out_of_range_score_is_rejected(self):
        with pytest.raises(lq.LetterScoringError):
            lq.parse_answers(_payload(cites_denial=2.01))

    def test_expected_levels_are_fractional_and_kept(self):
        # System One returns the probability-weighted expected level.
        score = lq.parse_answers(_payload(no_invented_facts=1.3, tone_and_form=0.5))
        assert score.grounding == pytest.approx(1.3)
        assert score.quality == pytest.approx((2 + 2 + 1.3 + 0.5) / 8)

    def test_missing_question_is_rejected(self):
        payload = _payload()
        del payload["answers"]["tone_and_form"]
        with pytest.raises(lq.LetterScoringError):
            lq.parse_answers(payload)

    def test_garbage_is_rejected_not_crashed(self):
        with pytest.raises(lq.LetterScoringError):
            lq.parse_answers({"answers": "nope"})

    def test_missing_usage_is_fine(self):
        payload = _payload()
        del payload["usage"]
        assert lq.parse_answers(payload).input_tokens == 0


class TestScoreLetter:
    def test_disabled_returns_none_without_a_request(self):
        with override_settings(TYPESAFE_API_KEY=None), patch.object(lq, "_post") as post:
            assert asyncio.run(lq.score_letter("d", "letter")) is None
            post.assert_not_called()

    def test_empty_letter_is_not_sent(self):
        with override_settings(**ENABLED), patch.object(lq, "_post") as post:
            assert asyncio.run(lq.score_letter("d", "   ")) is None
            post.assert_not_called()

    def test_happy_path(self):
        async def fake_post(document, timeout):
            assert "THE APPEAL LETTER:\nDear reviewer" in document
            return _payload(cites_denial=1)

        with override_settings(**ENABLED), patch.object(lq, "_post", fake_post):
            score = asyncio.run(lq.score_letter("denial", "Dear reviewer"))
        assert score is not None
        assert score.quality == pytest.approx(7 / 8)

    @pytest.mark.parametrize(
        "failure",
        [
            lq.LetterScoringError("HTTP 429"),
            asyncio.TimeoutError(),
            ConnectionError("boom"),
        ],
    )
    def test_any_failure_means_no_score(self, failure):
        async def fake_post(document, timeout):
            raise failure

        with override_settings(**ENABLED), patch.object(lq, "_post", fake_post):
            assert asyncio.run(lq.score_letter("denial", "letter")) is None

    def test_malformed_answer_means_no_score(self):
        async def fake_post(document, timeout):
            return {"answers": {}}

        with override_settings(**ENABLED), patch.object(lq, "_post", fake_post):
            assert asyncio.run(lq.score_letter("denial", "letter")) is None

    def test_cancellation_propagates(self):
        async def fake_post(document, timeout):
            raise asyncio.CancelledError()

        with override_settings(**ENABLED), patch.object(lq, "_post", fake_post):
            with pytest.raises(asyncio.CancelledError):
                asyncio.run(lq.score_letter("denial", "letter"))

    def test_failure_log_never_carries_the_document(self):
        secret = "PATIENT NAME JANE DOE MRN 998877"
        seen = []

        async def fake_post(document, timeout):
            raise lq.LetterScoringError("HTTP 500")

        with override_settings(**ENABLED), patch.object(lq, "_post", fake_post):
            with patch.object(lq.logger, "warning", side_effect=lambda m, *a, **k: seen.append(str(m))):
                asyncio.run(lq.score_letter(secret, "letter mentioning " + secret))
        assert seen and all(secret not in m for m in seen)


class TestFailureSummary:
    """What the status page is allowed to know about a failure."""

    def test_the_http_status_when_the_api_answered(self):
        assert lq.failure_summary(typesafe.TypeSafeError("HTTP 402", status=402)) == "HTTP 402"

    @pytest.mark.parametrize("failure", [TimeoutError(), asyncio.TimeoutError()])
    def test_timeouts_read_as_timeout(self, failure):
        assert lq.failure_summary(failure) == "timeout"

    def test_anything_else_is_the_class_name_alone(self):
        error = aiohttp.ClientConnectionError("Cannot connect to host secret-host.example:443")
        summary = lq.failure_summary(error)
        assert summary == "ClientConnectionError"
        assert "secret-host" not in summary
        assert lq.failure_summary(lq.LetterScoringError("HTTP 500")) == "LetterScoringError"
        assert lq.failure_summary(typesafe.TypeSafeError("no status")) == "TypeSafeError"


class TestFailureHook:
    def test_a_failure_reaches_the_hook_as_a_summary_only(self):
        secret = "PATIENT NAME JANE DOE MRN 998877"
        seen = []

        async def fake_post(document, timeout):
            raise typesafe.TypeSafeError("HTTP 402", status=402)

        async def hook(summary):
            seen.append(summary)

        with override_settings(**ENABLED), patch.object(lq, "_post", fake_post):
            result = asyncio.run(lq.score_letter(secret, "letter " + secret, on_failure=hook))
        assert result is None
        assert seen == ["HTTP 402"]

    def test_a_broken_hook_cannot_break_scoring(self):
        async def fake_post(document, timeout):
            raise TimeoutError()

        async def hook(summary):
            raise RuntimeError("db down")

        with override_settings(**ENABLED), patch.object(lq, "_post", fake_post):
            assert asyncio.run(lq.score_letter("d", "letter", on_failure=hook)) is None

    def test_cancellation_inside_the_hook_still_propagates(self):
        async def fake_post(document, timeout):
            raise TimeoutError()

        async def hook(summary):
            raise asyncio.CancelledError()

        with override_settings(**ENABLED), patch.object(lq, "_post", fake_post):
            with pytest.raises(asyncio.CancelledError):
                asyncio.run(lq.score_letter("d", "letter", on_failure=hook))

    def test_success_does_not_call_the_hook(self):
        calls = []

        async def fake_post(document, timeout):
            return _payload()

        async def hook(summary):
            calls.append(summary)

        with override_settings(**ENABLED), patch.object(lq, "_post", fake_post):
            assert asyncio.run(lq.score_letter("d", "letter", on_failure=hook)) is not None
        assert calls == []


class TestFrames:
    def test_score_frame_is_keyed_by_row_id(self):
        score = lq.parse_answers(_payload(medical_necessity=0))
        frame = lq.score_frame("42", score)
        assert frame == {
            "type": "score",
            "id": "42",
            "quality_score": 0.75,
            "grounding_score": 2,
            "scorer": lq.SCORER,
        }

    def test_with_score_fields_only_when_scored_under_the_current_rubric_and_enabled(self):
        class Row:
            quality_score = None
            grounding_score = None
            quality_scorer = None

        with override_settings(**ENABLED):
            assert lq.with_score_fields({"id": "1", "content": "x"}, Row()) == {"id": "1", "content": "x"}
            Row.quality_score, Row.grounding_score, Row.quality_scorer = 0.5, 1, "typesafe/x/rubric-0"
            assert lq.with_score_fields({"id": "1", "content": "x"}, Row()) == {"id": "1", "content": "x"}
            Row.quality_scorer = lq.SCORER
            assert lq.with_score_fields({"id": "1", "content": "x"}, Row()) == {
                "id": "1",
                "content": "x",
                "quality_score": 0.5,
                "scorer": lq.SCORER,
                "grounding_score": 1.0,
            }
        # The flag is the kill switch for scores already in the database too.
        with override_settings(TYPESAFE_LETTER_RANKING_ENABLED=False):
            assert lq.with_score_fields({"id": "1", "content": "x"}, Row()) == {"id": "1", "content": "x"}


    def test_with_score_fields_ignores_non_numeric_rows(self):
        from unittest.mock import MagicMock

        frame = lq.with_score_fields({"id": "1", "content": "x"}, MagicMock())
        assert frame == {"id": "1", "content": "x"}


class TestRedact:
    IDS = [
        ("Jane Q. Doe", "PATIENT"),
        ("Doe", "PATIENT"),
        ("TLH-2026-0091827", "CLAIM_ID"),
        ("Al", "PATIENT"),  # two letters is a real surname; whole-word keeps it safe
        ("UNKNOWN", "PLAN_ID"),  # the sentinel, never a value
    ]

    def test_longest_identifier_wins_and_each_value_gets_its_own_token(self):
        text = "Re: JANE Q. DOE (claim TLH-2026-0091827). The Doe family. Doesn't apply. Alaska. Al too."
        out = lq.redact(text, self.IDS)
        assert out == (
            "Re: [PATIENT_1] (claim [CLAIM_ID_1]). The [PATIENT_2] family. "
            "Doesn't apply. Alaska. [PATIENT_3] too."
        )

    def test_one_word_names_match_capitalised_and_all_caps_only(self):
        out = lq.redact(
            "Will will call. PATIENT: WILL. Dr. WILL SMITH will not. Li and LI, not li.",
            [("Will", "PATIENT"), ("Will Smith", "PROFESSIONAL"), ("Li", "PATIENT")],
        )
        assert out == (
            "[PATIENT_1] will call. PATIENT: [PATIENT_1]. Dr. [PROFESSIONAL_1] will not. "
            "[PATIENT_2] and [PATIENT_2], not li."
        )

    def test_emails_are_taken_before_names_so_an_address_stays_whole(self):
        out = lq.redact("write May@clinic.example or May", [("May", "PATIENT")])
        assert out == "write [EMAIL_1] or [PATIENT_1]"

    def test_tokens_are_never_re_redacted(self):
        out = lq.redact("Alice Smith, claim PATIENT_1", [("Alice Smith", "PATIENT"), ("PATIENT_1", "CLAIM_ID")])
        assert out == "[PATIENT_1], claim [CLAIM_ID_1]"

    def test_fax_and_phone_share_one_namespace_keyed_by_digits(self):
        doc = lq.build_document("Fax (415) 555-0100.", "Fax 415-555-0100.", [("(415) 555-0100", "FAX")])
        assert doc.count("[PHONE_1]") == 2 and "[FAX" not in doc

    def test_unicode_adjacent_and_punycode_emails(self):
        out = lq.redact("josé@example.com,a@exämple.com and x@y.co, p@example.xn--p1ai.", [])
        assert out == "[EMAIL_1],[EMAIL_2] and [EMAIL_3], [EMAIL_4]."

    def test_local_parts_with_rfc_atext_characters_are_whole(self):
        out = lq.redact("o'connor@example.com and first+tag@example.org or a!b#c@example.net", [])
        assert out == "[EMAIL_1] and [EMAIL_2] or [EMAIL_3]"

    def test_over_bound_input_is_not_scored_at_all(self):
        huge = "x" * (lq.RAW_CHAR_BOUND + 1)
        with pytest.raises(lq.LetterScoringError):
            lq.build_document("", huge)

        async def fake_post(document, timeout):
            raise AssertionError("must not be called")

        with override_settings(**ENABLED), patch.object(lq, "_post", fake_post):
            assert asyncio.run(lq.score_letter("d", huge)) is None

    def test_a_longer_known_identifier_beats_a_generic_match_inside_it(self):
        out = lq.redact("plan ABC-415-555-0100-Z, call 415-555-0100", [("ABC-415-555-0100-Z", "PLAN_ID")])
        assert out == "plan [PLAN_ID_1], call [PHONE_1]"

    def test_a_lowercase_stored_name_never_matches_ordinary_prose(self):
        out = lq.redact("the plan will cover Will; MAY and may", [("will", "PATIENT"), ("may", "PATIENT")])
        assert out == "the plan will cover [PATIENT_1]; [PATIENT_2] and may"

    def test_partially_overlapping_identifiers_become_one_span_so_nothing_leaks(self):
        out = lq.redact(
            "Re: Alice Smith Jones, MD", [("Alice Smith", "PATIENT"), ("Smith Jones", "PROFESSIONAL")]
        )
        assert out == "Re: [PATIENT_1], MD"

    def test_an_identifier_straddling_the_sent_boundary_never_leaks_a_fragment(self):
        plan = "PLAN-" + "7" * 60
        # Straddles the cap: the value is redacted BEFORE the cut, so what
        # falls off the end is a whole token (or nothing), never "PLAN-777".
        letter = "x" * (lq.DOCUMENT_CHAR_CAP - 40) + " " + plan + " tail"
        doc = lq.build_document("", letter, [(plan, "PLAN_ID")])
        assert "7777" not in doc and "PLAN-" not in doc and "[PLAN" not in doc.rsplit("]", 1)[-1]
        # Just inside the cap: the whole token is kept.
        letter = "x" * (lq.DOCUMENT_CHAR_CAP - 200) + " " + plan + " tail"
        doc = lq.build_document("", letter, [(plan, "PLAN_ID")])
        assert "[PLAN_ID_1]" in doc and "7777" not in doc

    def test_every_alias_of_one_person_shares_a_token(self):
        ids = [("Sam Smith", "PROFESSIONAL#7"), ("Sam Smith MD", "PROFESSIONAL#7"), ("Smith", "PROFESSIONAL#7"), ("Ann Lee", "PROFESSIONAL#9")]
        doc = lq.build_document("Denied by Sam Smith.", "Dr. Smith, Sam Smith MD, and Ann Lee agree.", ids)
        assert doc.count("[PROFESSIONAL_1]") == 3 and doc.count("[PROFESSIONAL_2]") == 1

    def test_raw_private_use_characters_are_just_text(self):
        weird = "before \ue0000\ue001 after"
        assert lq.redact(weird, []) == weird

    def test_truncation_never_splits_a_token(self):
        assert lq._cut("hello [PATIENT_1] world", 12) == "hello "
        assert lq._cut("x [A_1] y", 7) == "x [A_1]"
        assert lq._cut("short", 99) == "short"
        doc = lq.build_document("d" * 30_000, "[PATIENT_1]" * 3_000)
        assert "[PAT\n" not in doc and not doc.endswith("[PAT")

    def test_the_email_scan_is_linear_on_a_pathological_input(self):
        import time

        text = "a" * 48_000 + "@" + "b" * 48_000  # no final dot, no email
        started = time.perf_counter()
        out = lq.redact(text, [])
        assert time.perf_counter() - started < 0.5
        assert out == text
        assert lq._email_spans("x jane.doe@example.org, y") == [(2, 22)]
        assert lq._email_spans("bad@nodot or @lone or a@b.c") == []

    def test_generic_email_and_phone_patterns_number_distinct_values(self):
        out = lq.redact(
            "write to jane.doe@example.org or call (415) 555-0100, again 415-555-0100, NPI 1234567890.", []
        )
        assert out == "write to [EMAIL_1] or call [PHONE_1], again [PHONE_1], NPI [PHONE_2]."

    def test_a_shared_redactor_keeps_tokens_stable_across_denial_and_draft(self):
        doc = lq.build_document("Call 415-555-0100.", "Call 415-555-0100 or 212-555-0199.")
        assert doc.count("[PHONE_1]") == 2 and doc.count("[PHONE_2]") == 1

    def test_dates_and_amounts_survive(self):
        text = "denied on 10/15/2026 for $2,340.00; appeal within 180 days"
        assert lq.redact(text, []) == text

    def test_unknown_sentinel_is_never_redacted(self):
        assert lq.redact("status UNKNOWN", [("UNKNOWN", "[X]")]) == "status UNKNOWN"

    def test_document_is_built_from_redacted_parts(self):
        doc = lq.build_document(
            "Member Jane Q. Doe was denied.", "Dear reviewer, Jane Q. Doe needs the MRI.",
            [("Jane Q. Doe", "PATIENT")],
        )
        assert "Jane" not in doc
        assert doc.count("[PATIENT_1]") == 2

    def test_score_letter_sends_the_redacted_document(self):
        seen = []

        async def fake_post(document, timeout):
            seen.append(document)
            return _payload()

        with override_settings(**ENABLED), patch.object(lq, "_post", fake_post):
            asyncio.run(lq.score_letter("denial for Jane Q. Doe", "letter for Jane Q. Doe, fax 415-555-0100",
                                        identifiers=[("Jane Q. Doe", "PATIENT")]))
        assert seen and "Jane" not in seen[0] and "555-0100" not in seen[0]


class TestScorerIdentity:
    def test_scorer_names_model_and_rubric_version(self):
        assert lq.SCORER == f"typesafe/{lq.MODEL}/rubric-{lq.RUBRIC_VERSION}"

    def test_the_answering_model_is_recorded_when_typesafe_names_it(self):
        assert lq.scorer_for({"model": "speed_20260401"}) == f"typesafe/speed_20260401/rubric-{lq.RUBRIC_VERSION}"
        assert lq.scorer_for({}) == lq.SCORER
        assert lq.parse_answers({**_payload(), "model": "speed_20260401"}).scorer.startswith("typesafe/speed_20260401/")

    def test_same_rubric_is_what_decides_reuse_and_the_whole_string_must_be_ours(self):
        assert lq.same_rubric(f"typesafe/anything/rubric-{lq.RUBRIC_VERSION}")
        assert not lq.same_rubric(f"typesafe/{lq.MODEL}/rubric-{lq.RUBRIC_VERSION + 1}")
        assert not lq.same_rubric(f"manual-import/rubric-{lq.RUBRIC_VERSION}")
        assert not lq.same_rubric(f"typesafe/x/y/rubric-{lq.RUBRIC_VERSION}")
        assert not lq.same_rubric(None)

    def test_an_odd_model_name_in_the_answer_cannot_forge_provenance(self):
        assert lq.scorer_for({"model": "weird value!"}) == lq.SCORER
        assert lq.scorer_for({"model": "x" * 200}) == lq.SCORER

    def test_needs_scoring(self):
        class Row:
            appeal_text = "a real draft"
            quality_score = None
            quality_scorer = None

        assert lq.needs_scoring(Row())
        Row.quality_score, Row.quality_scorer = 0.5, lq.SCORER
        assert not lq.needs_scoring(Row())
        Row.quality_scorer = "typesafe/x/rubric-0"
        assert lq.needs_scoring(Row())
        Row.appeal_text = "  "
        assert not lq.needs_scoring(Row())


class TestSortKey:
    def test_unscored_sorts_last_then_ungrounded_then_quality(self):
        keys = sorted(
            [lq.sort_key(None, None), lq.sort_key(0.9, 0), lq.sort_key(0.4, 2), lq.sort_key(0.8, 1)],
            reverse=True,
        )
        assert keys == [(2, 0.8), (2, 0.4), (1, 0.9), (0, 0.0)]
