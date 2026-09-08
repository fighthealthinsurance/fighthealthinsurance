"""ml/denial_triage.py: category, regulation, urgency and the appeal deadline.

The deadline path matters most: a wrong date shown confidently could cost
someone an appeal, so every step (candidates, the question, the parse, what a
reader may be told) is pinned here, and every failure means "no triage".
"""

import asyncio
import datetime
from unittest.mock import patch

import pytest
from django.test import override_settings

from fighthealthinsurance.ml import denial_triage as dt

ENABLED = dict(TYPESAFE_API_KEY="test-key", TYPESAFE_DENIAL_TRIAGE_ENABLED=True)
DENIAL_DATE = datetime.date(2026, 9, 2)
LETTER = (
    "Dear Member, your request for an MRI of the lumbar spine performed on "
    "August 14, 2026 was denied as not medically necessary. You have the right "
    "to appeal. Your written appeal must be received within 180 days of this "
    "notice. If our decision is upheld you may request an external review by "
    "10/15/2026. Records requests must be made within 30 days of receipt. "
    "Group plan administered for Totally Legit Co."
)


class TestDateCandidates:
    def test_document_order_with_anchored_window_resolved(self):
        found = dt.date_candidates(LETTER, DENIAL_DATE)
        assert [c.label for c in found] == ["2026-08-14", "180 days from notice", "2026-10-15", "30 days"]
        window = found[1]
        assert window.anchored and window.resolves_to == DENIAL_DATE + datetime.timedelta(days=180)
        assert found[3].resolves_to is None, "after receipt is not a date we hold"

    def test_window_stays_unresolved_without_a_denial_date(self):
        found = dt.date_candidates(LETTER, None)
        assert found[1].label == "180 days from notice" and found[1].resolves_to is None

    def test_business_days_are_never_resolved(self):
        found = dt.date_candidates("appeal within 30 business days of this notice", DENIAL_DATE)
        assert found[0].label == "30 business days" and found[0].resolves_to is None

    def test_a_window_counted_from_service_or_receipt_is_not_anchored(self):
        for tail in (
            "of the date of service",
            "after receipt",
            "following your discharge",
            "after receipt. Keep a copy of this notice for your records",
            "following the date of this letter's receipt",
            "of this letter\u2019s receipt",
            "of the date this notice was received",
            "after this notice has been received",
            "of this notice being received",
            "after this letter is delivered",
            "following this determination becoming final",
            "of this notice, starting on the date you receive it",
            "of this notice, which begins upon receipt",
            "of this notice, and the period begins when you receive it",
            "of this notice or the date it was delivered, whichever is later",
        ):
            found = dt.date_candidates(f"appeal within 60 days {tail}.", DENIAL_DATE)
            assert found[0].label == "60 days" and found[0].resolves_to is None, tail

    def test_only_an_immediately_following_anchor_counts(self):
        for tail in (
            "of this notice",
            ", from the date of this letter",
            "following this determination",
            "of this notice to preserve your rights",
            "of this notice, to preserve your rights",
            "of this notice, and late appeals are not accepted",
            "of the date on this notice; late appeals are not accepted",
        ):
            found = dt.date_candidates(f"appeal within 60 days {tail}.", DENIAL_DATE)
            assert found[0].label == "60 days from notice", tail
            assert found[0].resolves_to == DENIAL_DATE + datetime.timedelta(days=60)

    def test_the_appeal_bearing_mention_of_a_repeated_date_wins(self):
        text = "Service on 10/15/2026 was denied. Your appeal must be received by 10/15/2026."
        found = dt.date_candidates(text, None)
        assert [c.label for c in found] == ["2026-10-15"]
        assert "appeal" in found[0].snippet.lower()

    def test_adjacent_lines_get_their_own_snippets(self):
        text = "External review: by 12/01/2026\nInternal appeal: within 180 days of this notice"
        found = dt.date_candidates(text, DENIAL_DATE)
        assert found[0].snippet == "External review: by 12/01/2026"
        assert found[1].snippet == "Internal appeal: within 180 days of this notice"

    def test_is_anchored_window(self):
        assert dt.is_anchored_window("180 days from notice")
        assert not dt.is_anchored_window("180 days")
        assert not dt.is_anchored_window("30 business days")
        assert not dt.is_anchored_window(None)

    def test_snippet_is_the_containing_sentence(self):
        found = dt.date_candidates(LETTER, None)
        assert found[1].snippet == "Your written appeal must be received within 180 days of this notice."

    def test_two_digit_years_and_identifier_embedded_dates_are_rejected(self):
        assert dt.date_candidates("appeal by 10/15/26", None) == []
        assert dt.date_candidates("claim 12/01/2026-0091827 and ref 5/2026-01-04", None) == []

    def test_iso_dates_are_accepted(self):
        found = dt.date_candidates("appeal by 2026-10-15.", None)
        assert [c.label for c in found] == ["2026-10-15"]

    def test_cap_keeps_the_appeal_sentence_over_a_pile_of_service_dates(self):
        services = " ".join(f"Service on 1/{i}/2026." for i in range(1, 12))
        text = services + " Your appeal must be filed within 180 days of this notice."
        found = dt.date_candidates(text, DENIAL_DATE)
        assert len(found) == dt.MAX_DATE_CANDIDATES
        assert any(c.label == "180 days from notice" for c in found)
        assert [c.position for c in found] == sorted(c.position for c in found)

    def test_dedupes(self):
        found = dt.date_candidates("by 10/15/2026, again 10/15/2026, within 30 days, within 30 days", None)
        assert [c.label for c in found] == ["2026-10-15", "30 days"]

    def test_empty_text(self):
        assert dt.date_candidates(None, None) == []


class TestResolveWindow:
    def test_only_anchored_calendar_windows_resolve(self):
        assert dt.resolve_window("180 days from notice", DENIAL_DATE) == datetime.date(2027, 3, 1)
        assert dt.resolve_window("180 days", DENIAL_DATE) is None
        assert dt.resolve_window("30 business days", DENIAL_DATE) is None
        assert dt.resolve_window("180 days from notice", None) is None
        assert dt.resolve_window(None, DENIAL_DATE) is None


class TestQuestions:
    def test_deadline_question_only_when_there_are_candidates(self):
        assert "deadline" not in dt.build_questions([])
        questions = dt.build_questions(dt.date_candidates(LETTER, None))
        options = questions["deadline"]["criteria"]
        assert dt.NONE_LABEL in options
        assert options["180 days from notice"].startswith("180 days from notice: Your written appeal")
        assert "INTERNAL" in questions["deadline"]["instructions"]
        assert "more than one deadline" in options[dt.NONE_LABEL]

    def test_types_match_the_api(self):
        questions = dt.build_questions([])
        assert questions["category"]["type"] == "choice"
        assert questions["regulation"]["type"] == "choice"
        assert questions["pre_service"]["type"] == "noul"
        assert questions["urgent"]["type"] == "noul"
        assert set(questions["category"]["criteria"]) == set(dt.CATEGORIES)


def _payload(deadline="180 days from notice", deadline_conf=0.9, category="medical_necessity"):
    answers = {
        "category": {"type": "choice", "choice": category, "confidence": 0.85},
        "regulation": {"type": "choice", "choice": "employer_plan", "confidence": 0.6},
        "pre_service": {"type": "noul", "noul": 0.92},
        "urgent": {"type": "noul", "noul": 0.05},
    }
    if deadline is not None:
        answers["deadline"] = {"type": "choice", "choice": deadline, "confidence": deadline_conf}
    return {"answers": answers, "usage": {"input_tokens": 700}}


class TestParse:
    def setup_method(self):
        self.candidates = dt.date_candidates(LETTER, DENIAL_DATE)

    def test_happy_path_picks_the_internal_window_not_the_external_review_date(self):
        result = dt.parse(_payload(), self.candidates)
        assert result.category == "medical_necessity"
        assert result.regulation == "employer_plan"
        assert result.pre_service == pytest.approx(0.92)
        assert result.deadline_label == "180 days from notice"
        assert result.deadline == datetime.date(2027, 3, 1)
        assert result.deadline_confidence == pytest.approx(0.9)
        assert result.input_tokens == 700

    def test_none_of_these(self):
        result = dt.parse(_payload(deadline=dt.NONE_LABEL), self.candidates)
        assert result.deadline is None and result.deadline_label is None

    def test_an_unanchored_pick_keeps_the_label_but_no_date(self):
        result = dt.parse(_payload(deadline="30 days"), self.candidates)
        assert result.deadline_label == "30 days" and result.deadline is None

    def test_unknown_category_is_rejected(self):
        with pytest.raises(dt.TriageError):
            dt.parse(_payload(category="vibes"), self.candidates)

    def test_deadline_not_among_candidates_is_rejected(self):
        with pytest.raises(dt.TriageError):
            dt.parse(_payload(deadline="2031-01-01"), self.candidates)

    def test_probability_out_of_range_is_rejected(self):
        payload = _payload()
        payload["answers"]["urgent"]["noul"] = 1.4
        with pytest.raises(dt.TriageError):
            dt.parse(payload, self.candidates)

    def test_missing_answer_is_rejected(self):
        payload = _payload()
        del payload["answers"]["regulation"]
        with pytest.raises(dt.TriageError):
            dt.parse(payload, self.candidates)


class TestTriage:
    def test_off_by_default(self):
        with patch.object(dt, "_post") as post:
            assert asyncio.run(dt.triage(LETTER, None)) is None
            post.assert_not_called()

    def test_happy_path_sends_only_the_letter(self):
        seen = {}

        async def fake_post(document, questions, timeout):
            seen["document"] = document
            seen["questions"] = questions
            return _payload()

        with override_settings(**ENABLED), patch.object(dt, "_post", fake_post):
            result = asyncio.run(dt.triage(LETTER, DENIAL_DATE))
        assert result is not None and result.deadline == datetime.date(2027, 3, 1)
        assert seen["document"] == LETTER
        assert "deadline" in seen["questions"]

    def test_failure_means_no_triage_and_no_text_in_the_log(self):
        secret = "MEMBER JANE DOE 998877"
        seen = []

        async def fake_post(document, questions, timeout):
            raise dt.typesafe.TypeSafeError("HTTP 500")

        with override_settings(**ENABLED), patch.object(dt, "_post", fake_post):
            with patch.object(dt.logger, "warning", side_effect=lambda m, *a, **k: seen.append(str(m))):
                assert asyncio.run(dt.triage(LETTER + " " + secret, None)) is None
        assert seen and all(secret not in m for m in seen)


class _DenialLike:
    def __init__(self, deadline, confidence, text="letter", stale=False, source=dt.SOURCE):
        self.appeal_deadline = deadline
        self.appeal_deadline_confidence = confidence
        self.denial_text = text
        self.triage_text_hash = dt.text_hash("something else" if stale else text)
        self.triage_source = source


class TestWhatAReaderMayBeTold:
    TODAY = datetime.date(2026, 9, 7)

    def test_confident_current_future_deadline(self):
        denial = _DenialLike(datetime.date(2026, 10, 15), 0.9)
        assert dt.deadline_to_show(denial, self.TODAY) == datetime.date(2026, 10, 15)
        assert dt.deadline_sentence(denial, self.TODAY) == (
            "Your denial letter appears to say appeals are due by October 15, 2026. "
            "Check the letter to be sure."
        )

    @pytest.mark.parametrize(
        "denial",
        [
            _DenialLike(datetime.date(2026, 10, 15), 0.5),  # not confident enough
            _DenialLike(datetime.date(2026, 9, 7), 0.99),  # today is not "ahead"
            _DenialLike(datetime.date(2026, 9, 1), 0.99),  # already past
            _DenialLike(None, 0.99),  # never resolved
            _DenialLike(datetime.date(2026, 10, 15), None),
            _DenialLike(datetime.date(2026, 10, 15), 0.99, stale=True),  # letter replaced since
            _DenialLike(datetime.date(2026, 10, 15), 0.99, source="typesafe/speed_latest/rubric-0"),  # older rubric
        ],
    )
    def test_silence_otherwise(self, denial):
        assert dt.deadline_to_show(denial, self.TODAY) is None
        assert dt.deadline_sentence(denial, self.TODAY) == ""


class TestRowValues:
    def test_every_column_is_named_and_the_text_hash_travels(self):
        result = dt.parse(_payload(), dt.date_candidates(LETTER, DENIAL_DATE))
        now = datetime.datetime(2026, 9, 7, 12, 0, tzinfo=datetime.timezone.utc)
        values = dt.row_values(result, now, LETTER)
        assert values["appeal_deadline"] == datetime.date(2027, 3, 1)
        assert values["triage_source"] == dt.SOURCE
        assert dt.source_for({"model": "speed_20260901"}) == f"typesafe/speed_20260901/rubric-{dt.RUBRIC_VERSION}"
        assert dt.same_rubric(dt.SOURCE) and not dt.same_rubric("typesafe/x/rubric-0") and not dt.same_rubric("manual/rubric-1")
        assert values["triage_text_hash"] == dt.text_hash(LETTER)
        assert values["triaged_at"] == now
        assert set(values) == set(dt.TRIAGE_COLUMNS)
        assert set(dt.cleared_values()) == set(dt.TRIAGE_COLUMNS)
        assert all(v is None for v in dt.cleared_values().values())
