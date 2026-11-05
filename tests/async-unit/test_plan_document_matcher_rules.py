"""The rules of plan_document_matcher, run on plain values.

``score_candidate``, ``select`` and the helpers around them are pure: no
ORM, no model call. So the worked examples here build ``Candidate`` and
``DenialFacts`` directly and check the arithmetic the module's docstring
describes (hard excludes, points per signal, the identifying-signal rule,
the margin). Everything that needs rows -- ``match_documents``,
``arefresh_match``, the renderer -- lives in
tests/sync/test_plan_document_matcher.py.

The letters are invented.
"""

import re

import pytest

from fighthealthinsurance.document_grep import whole_phrase_pattern
from fighthealthinsurance.models import PublicPlanDocument
from fighthealthinsurance.plan_document_bundle import NEGATIVE_REGEX_PROBE
from fighthealthinsurance.plan_document_matcher import (
    IDENTIFYING_SIGNALS,
    MARGIN,
    MAX_SELECTED,
    MIN_SCORE,
    Candidate,
    DenialFacts,
    Scored,
    companions,
    conflicts,
    ranked,
    score_candidate,
    select,
    tie_break_answer_valid,
)

# The flags RegexField compiles match_regex / negative_regex with.
REGEX_FLAGS = re.IGNORECASE | re.UNICODE | re.M

EOC = PublicPlanDocument.DOCUMENT_TYPE_EVIDENCE_OF_COVERAGE
SBC = PublicPlanDocument.DOCUMENT_TYPE_SUMMARY_OF_BENEFITS
OTHER = PublicPlanDocument.DOCUMENT_TYPE_OTHER
MEDICAL = PublicPlanDocument.COVERAGE_KIND_MEDICAL
DENTAL = PublicPlanDocument.COVERAGE_KIND_DENTAL

# The worked example's negative regex (the Kaiser NorCal document in the
# bundle docs): other regions, the Medicare product, and Medi-Cal written
# as two words or hyphenated, never "medical".
EXAMPLE_NEGATIVE_REGEX = (
    r"\bsouthern\s+california\b|\blos\s+angeles\b|\bsenior\s+advantage\b"
    r"|\bmedi[-\s]cal\b"
)

LETTER = (
    "Kaiser Permanente Northern California denied an MRI as not medically " "necessary."
)


def regex(pattern: str) -> "re.Pattern[str]":
    return re.compile(pattern, REGEX_FLAGS)


def candidate(**overrides) -> Candidate:
    """A NorCal 2025 EOC keyed on one keyword, with fields overridden."""
    fields = dict(
        id=1,
        title="Kaiser Permanente Northern California HMO Evidence of Coverage",
        citation_name=(
            "Kaiser Permanente Northern California HMO Evidence of Coverage (2025)"
        ),
        document_type=EOC,
        coverage_kind=MEDICAL,
        line_of_business="commercial",
        state="CA",
        region="Northern California",
        employer_name="",
        plan_year=2025,
        plan_id_prefixes=(),
        match_keywords=("northern california",),
        match_regex=None,
        negative_regex=None,
        insurance_plan_id=None,
        plan_name="",
        source_url="",
    )
    fields.update(overrides)
    return Candidate(**fields)


def facts(**overrides) -> DenialFacts:
    """A 2025 California medical denial whose letter names the region."""
    fields = dict(
        company_id=7,
        plan_id=None,
        state="CA",
        lob=None,
        lob_is_hard=False,
        dental=False,
        employer_name="",
        plan_id_text="",
        year=2025,
        blob=LETTER,
        letter_hash="0123456789abcdef",
        procedure="",
        diagnosis="",
    )
    fields.update(overrides)
    return DenialFacts(**fields)


def scored(c: Candidate, score: float, identifying: int = 1) -> Scored:
    """A ``Scored`` with the score and identifying count given: all
    ``select`` reads."""
    return Scored(
        candidate=c,
        score=float(score),
        signals={"identifying": identifying},
        identifying=identifying,
    )


def signals_of(c: Candidate, f: DenialFacts) -> dict:
    result = score_candidate(c, f)
    assert result is not None, "a hard exclude fired where a score was expected"
    return result.signals


class TestWholePhrasePattern:
    def test_a_stem_does_not_find_a_longer_word(self):
        assert whole_phrase_pattern("target").search("targeted therapy") is None

    def test_a_place_does_not_find_a_longer_name(self):
        assert (
            whole_phrase_pattern("san jose").search("Seen at San Joseph Hospital")
            is None
        )

    def test_a_phrase_is_found_as_whole_words_in_any_case(self):
        assert whole_phrase_pattern("san jose").search(
            "Seen at the San Jose Medical Center."
        )

    def test_a_line_break_inside_the_phrase_still_matches(self):
        assert whole_phrase_pattern("northern california").search(
            "the Northern\nCalifornia Region"
        )

    def test_the_phrase_is_not_found_inside_a_longer_word(self):
        assert whole_phrase_pattern("age").search("coverage") is None

    def test_regex_syntax_in_a_phrase_is_literal(self):
        assert whole_phrase_pattern("a.b").search("a b or axb") is None
        assert whole_phrase_pattern("a.b").search("plan a.b here")


class TestExampleNegativeRegex:
    def test_it_does_not_match_ordinary_denial_letter_words(self):
        pattern = regex(EXAMPLE_NEGATIVE_REGEX)
        assert pattern.search(NEGATIVE_REGEX_PROBE) is None
        assert (
            pattern.search("San Francisco Medical Center: not medically necessary")
            is None
        )

    @pytest.mark.parametrize(
        "text",
        [
            "Southern California Region",
            "Los Angeles Medical Center",
            "a Senior Advantage member",
            "a Medi-Cal managed care plan",
            "Medi Cal",
        ],
    )
    def test_it_matches_what_it_is_written_for(self, text):
        assert regex(EXAMPLE_NEGATIVE_REGEX).search(text)


class TestScoreCandidateHardExcludes:
    def test_another_state_is_excluded(self):
        assert score_candidate(candidate(state="CA"), facts(state="NY")) is None

    def test_a_national_document_or_an_unknown_state_is_not_excluded(self):
        assert score_candidate(candidate(state=""), facts(state="NY")) is not None
        assert score_candidate(candidate(state="CA"), facts(state="")) is not None

    def test_a_confirmed_line_of_business_excludes_another_specific_one(self):
        medicare = candidate(line_of_business="medicare_advantage")
        assert (
            score_candidate(medicare, facts(lob="commercial", lob_is_hard=True)) is None
        )

    def test_an_inferred_line_of_business_only_costs_points(self):
        medicare = candidate(line_of_business="medicare_advantage")
        result = score_candidate(medicare, facts(lob="commercial", lob_is_hard=False))
        assert result is not None
        assert result.signals["line_of_business"] == -10
        assert result.score == 39 - 10

    def test_a_document_for_all_lines_is_never_excluded_by_line_of_business(self):
        result = score_candidate(
            candidate(line_of_business="all"), facts(lob="medicaid", lob_is_hard=True)
        )
        assert result is not None
        assert "line_of_business" not in result.signals

    def test_a_dental_document_is_excluded_from_a_medical_claim(self):
        assert (
            score_candidate(candidate(coverage_kind=DENTAL), facts(dental=False))
            is None
        )

    def test_a_medical_document_is_excluded_from_a_dental_claim(self):
        assert (
            score_candidate(candidate(coverage_kind=MEDICAL), facts(dental=True))
            is None
        )

    def test_a_dental_document_scores_for_a_dental_claim(self):
        result = score_candidate(candidate(coverage_kind=DENTAL), facts(dental=True))
        assert result is not None
        assert result.signals["coverage_kind"] == 3

    def test_a_negative_regex_hit_on_the_letter_excludes(self):
        c = candidate(negative_regex=regex(EXAMPLE_NEGATIVE_REGEX))
        letter = "Kaiser Permanente Los Angeles Medical Center denied an MRI."
        assert score_candidate(c, facts(blob=letter)) is None

    def test_a_negative_regex_that_misses_leaves_the_document_in(self):
        # LETTER says "medically", which the example regex is written not
        # to match.
        c = candidate(negative_regex=regex(EXAMPLE_NEGATIVE_REGEX))
        assert score_candidate(c, facts()) is not None

    def test_another_plan_of_the_same_carrier_is_excluded(self):
        assert score_candidate(candidate(insurance_plan_id=2), facts(plan_id=1)) is None

    def test_a_plan_unknown_on_either_side_does_not_exclude(self):
        assert (
            score_candidate(candidate(insurance_plan_id=None), facts(plan_id=1))
            is not None
        )
        assert (
            score_candidate(candidate(insurance_plan_id=2), facts(plan_id=None))
            is not None
        )


class TestScoreCandidateSignals:
    def test_the_same_plan_scores_forty(self):
        assert (
            signals_of(candidate(insurance_plan_id=3), facts(plan_id=3))["same_plan"]
            == 40
        )

    def test_a_plan_id_prefix_matches_case_insensitively(self):
        s = signals_of(
            candidate(plan_id_prefixes=("GRP",)), facts(plan_id_text="grp-12345")
        )
        assert s["plan_id_prefix"] == 30

    def test_a_plan_id_with_another_prefix_does_not_match(self):
        s = signals_of(
            candidate(plan_id_prefixes=("GRP",)), facts(plan_id_text="HMO-12345")
        )
        assert "plan_id_prefix" not in s

    def test_an_empty_plan_id_never_matches_a_prefix(self):
        # Not even an empty prefix, which every string starts with.
        s = signals_of(candidate(plan_id_prefixes=("GRP", "")), facts(plan_id_text=""))
        assert "plan_id_prefix" not in s

    def test_the_employer_is_matched_in_the_typed_employer(self):
        s = signals_of(
            candidate(employer_name="Golden Gate Transit"),
            facts(employer_name="Golden Gate Transit District"),
        )
        assert s["employer"] == 30

    def test_a_multi_word_employer_is_also_looked_for_in_the_letter(self):
        s = signals_of(
            candidate(employer_name="Golden Gate Transit"),
            facts(blob=LETTER + " Group: Golden Gate Transit."),
        )
        assert s["employer"] == 30

    def test_a_short_one_word_employer_is_not_looked_for_in_the_letter(self):
        s = signals_of(
            candidate(employer_name="Acme"), facts(blob=LETTER + " Group: Acme.")
        )
        assert "employer" not in s

    def test_a_document_without_an_employer_gives_no_employer_signal(self):
        s = signals_of(
            candidate(employer_name=""), facts(employer_name="Golden Gate Transit")
        )
        assert "employer" not in s

    def test_a_denial_without_an_employer_gives_no_employer_signal(self):
        s = signals_of(
            candidate(employer_name="Golden Gate Transit"), facts(employer_name="")
        )
        assert "employer" not in s

    def test_the_employer_is_matched_as_whole_words(self):
        s = signals_of(
            candidate(employer_name="Golden Gate Transit"),
            facts(employer_name="Golden Gate Transitions LLC"),
        )
        assert "employer" not in s

    def test_the_match_regex_scores_twenty_five(self):
        s = signals_of(
            candidate(match_regex=regex(r"\bdeductible\s+hmo\b")),
            facts(blob="Your Deductible HMO plan denied an MRI."),
        )
        assert s["match_regex"] == 25

    def test_a_match_regex_that_misses_scores_nothing(self):
        s = signals_of(candidate(match_regex=regex(r"\bdeductible\s+hmo\b")), facts())
        assert "match_regex" not in s

    def test_each_keyword_hit_scores_ten_and_the_count_is_kept(self):
        c = candidate(
            match_keywords=("northern california", "san francisco", "sacramento")
        )
        s = signals_of(
            c,
            facts(blob="Kaiser Permanente San Francisco, Northern California Region."),
        )
        assert s["keyword"] == 20
        assert s["keyword_hits"] == 2

    def test_keyword_points_are_capped_at_thirty(self):
        c = candidate(
            match_keywords=(
                "northern california",
                "san francisco",
                "sacramento",
                "san jose",
            )
        )
        s = signals_of(
            c,
            facts(blob="Northern California: San Francisco, Sacramento and San Jose."),
        )
        assert s["keyword"] == 30
        assert s["keyword_hits"] == 4

    def test_keywords_are_matched_as_whole_words(self):
        s = signals_of(
            candidate(match_keywords=("san jose",)),
            facts(blob="Treated at San Joseph Hospital."),
        )
        assert "keyword" not in s

    def test_the_state_scores_ten_only_when_both_sides_know_it(self):
        assert signals_of(candidate(state="CA"), facts(state="CA"))["state"] == 10
        assert "state" not in signals_of(candidate(state="CA"), facts(state=""))
        assert "state" not in signals_of(candidate(state=""), facts(state="CA"))

    def test_the_same_line_of_business_scores_ten(self):
        s = signals_of(
            candidate(line_of_business="medicare_advantage"),
            facts(lob="medicare_advantage", lob_is_hard=True),
        )
        assert s["line_of_business"] == 10

    def test_an_unknown_line_of_business_scores_nothing(self):
        s = signals_of(
            candidate(line_of_business="medicare_advantage"), facts(lob=None)
        )
        assert "line_of_business" not in s

    def test_the_plan_year_scores_ten_when_equal_and_five_when_adjacent(self):
        assert (
            signals_of(candidate(plan_year=2025), facts(year=2025))["plan_year"] == 10
        )
        assert signals_of(candidate(plan_year=2024), facts(year=2025))["plan_year"] == 5
        assert "plan_year" not in signals_of(
            candidate(plan_year=2023), facts(year=2025)
        )
        assert "plan_year" not in signals_of(
            candidate(plan_year=2025), facts(year=None)
        )

    def test_document_type_points_rank_an_eoc_over_its_sbc(self):
        assert signals_of(candidate(document_type=EOC), facts())["document_type"] == 6
        assert signals_of(candidate(document_type=SBC), facts())["document_type"] == 2
        assert "document_type" not in signals_of(
            candidate(document_type=OTHER), facts()
        )

    def test_the_coverage_kind_adds_three_once_the_document_survives(self):
        assert signals_of(candidate(), facts())["coverage_kind"] == 3

    def test_identifying_counts_only_the_identifying_signals(self):
        c = candidate(employer_name="Golden Gate Transit")
        result = score_candidate(c, facts(employer_name="Golden Gate Transit"))
        assert result is not None
        assert result.identifying == 2 == result.signals["identifying"]
        # These agree with the document but do not identify it.
        assert {"state", "plan_year", "document_type", "coverage_kind"} <= set(
            result.signals
        )

    def test_identifying_signals_are_the_documented_five(self):
        assert IDENTIFYING_SIGNALS == (
            "same_plan",
            "plan_id_prefix",
            "employer",
            "match_regex",
            "keyword",
        )

    def test_the_score_sums_the_points_and_not_the_hit_count(self):
        result = score_candidate(candidate(), facts())
        assert result is not None
        assert result.score == 10 + 10 + 10 + 6 + 3
        assert result.signals["keyword_hits"] == 1


class TestWorkedArithmetic:
    def test_one_keyword_state_year_eoc_and_medical_make_thirty_nine_and_select(self):
        result = score_candidate(candidate(), facts())
        assert result is not None
        assert result.score == 39
        assert result.score >= MIN_SCORE
        assert result.identifying == 1
        selected, rivals = select([result])
        assert [s.candidate.id for s in selected] == [1]
        assert rivals == []

    def test_without_the_state_and_the_year_it_is_nineteen_and_does_not_select(self):
        result = score_candidate(candidate(), facts(state="", year=None))
        assert result is not None
        assert result.score == 19
        assert result.identifying == 1
        assert select([result]) == ([], [])


class TestSelect:
    def test_an_eoc_and_the_sbc_of_the_same_plan_ride_together(self):
        eoc = scored(candidate(id=1, document_type=EOC), 49)
        sbc = scored(candidate(id=2, document_type=SBC), 45)
        selected, rivals = select([sbc, eoc])
        assert [s.candidate.id for s in selected] == [1, 2]
        assert rivals == []

    def test_a_conflicting_candidate_within_the_margin_is_a_rival_and_nothing_is_selected(
        self,
    ):
        norcal = scored(candidate(id=1, region="Northern California"), 49)
        socal = scored(candidate(id=2, region="Southern California"), 49 - MARGIN + 1)
        selected, rivals = select([norcal, socal])
        assert selected == []
        assert [s.candidate.id for s in rivals] == [2]

    def test_a_conflicting_candidate_beyond_the_margin_is_skipped(self):
        norcal = scored(candidate(id=1, region="Northern California"), 60)
        socal = scored(candidate(id=2, region="Southern California"), 60 - MARGIN)
        selected, rivals = select([norcal, socal])
        assert [s.candidate.id for s in selected] == [1]
        assert rivals == []

    def test_a_rival_holds_back_the_companions_too(self):
        eoc = scored(candidate(id=1), 49)
        sbc = scored(candidate(id=2, document_type=SBC), 45)
        last_year = scored(candidate(id=3, plan_year=2024), 40)
        selected, rivals = select([eoc, sbc, last_year])
        assert selected == []
        assert [s.candidate.id for s in rivals] == [3]

    def test_at_most_max_selected_documents_are_taken(self):
        pool = [
            scored(candidate(id=i, document_type=OTHER), 50 - i) for i in range(1, 6)
        ]
        selected, rivals = select(pool)
        assert MAX_SELECTED == 3
        assert [s.candidate.id for s in selected] == [1, 2, 3]
        assert rivals == []

    def test_nothing_below_min_score_is_selected(self):
        assert select([scored(candidate(), MIN_SCORE - 1)]) == ([], [])
        selected, _ = select([scored(candidate(), MIN_SCORE)])
        assert len(selected) == 1

    def test_nothing_without_an_identifying_signal_is_selected(self):
        assert select([scored(candidate(), 50, identifying=0)]) == ([], [])

    def test_an_ineligible_candidate_is_never_a_rival(self):
        top = scored(candidate(id=1), 49)
        weak = scored(candidate(id=2, region="Southern California"), MIN_SCORE - 1)
        selected, rivals = select([top, weak])
        assert [s.candidate.id for s in selected] == [1]
        assert rivals == []

    def test_nothing_to_select_from(self):
        assert select([]) == ([], [])


class TestRanked:
    def test_score_then_identifying_then_document_type_then_id(self):
        a = scored(candidate(id=4, document_type=SBC), 40, identifying=1)
        b = scored(candidate(id=3, document_type=EOC), 40, identifying=1)
        c = scored(candidate(id=2, document_type=SBC), 40, identifying=2)
        d = scored(candidate(id=1), 45, identifying=0)
        assert [s.candidate.id for s in ranked([a, b, c, d])] == [1, 2, 3, 4]


class TestConflicts:
    @pytest.mark.parametrize(
        "left, right",
        [
            (dict(insurance_plan_id=1), dict(insurance_plan_id=2)),
            (dict(state="CA"), dict(state="NY")),
            (dict(region="Northern California"), dict(region="Southern California")),
            (dict(plan_year=2025), dict(plan_year=2024)),
            (
                dict(employer_name="Golden Gate Transit"),
                dict(employer_name="City of Oakland"),
            ),
            (
                dict(line_of_business="commercial"),
                dict(line_of_business="medicare_advantage"),
            ),
        ],
    )
    def test_two_documents_that_cannot_both_be_the_members_conflict(self, left, right):
        assert conflicts(candidate(**left), candidate(**right))

    @pytest.mark.parametrize(
        "left, right",
        [
            (dict(region="Northern California"), dict(region="northern california")),
            (dict(region="Northern California"), dict(region="")),
            (dict(plan_year=2025), dict(plan_year=None)),
            (dict(line_of_business="commercial"), dict(line_of_business="all")),
            (dict(insurance_plan_id=1), dict(insurance_plan_id=None)),
            (dict(employer_name="Golden Gate Transit"), dict(employer_name="")),
        ],
    )
    def test_a_blank_or_equal_field_never_conflicts(self, left, right):
        assert not conflicts(candidate(**left), candidate(**right))


class TestCompanions:
    def test_the_pick_leads_and_the_documents_that_do_not_conflict_follow(self):
        hmo = scored(candidate(id=1, plan_year=2024), 44)
        eoc = scored(candidate(id=2), 39)
        sbc = scored(candidate(id=3, document_type=SBC), 35)
        assert [s.candidate.id for s in companions(eoc, [hmo, eoc, sbc])] == [2, 3]
        assert [s.candidate.id for s in companions(hmo, [hmo, eoc, sbc])] == [1]

    def test_companions_stop_at_max_selected(self):
        pool = [
            scored(candidate(id=i, document_type=OTHER), 50 - i) for i in range(1, 6)
        ]
        chosen = companions(pool[4], pool)
        assert len(chosen) == MAX_SELECTED
        assert chosen[0] is pool[4]


class TestTieBreakAnswerValid:
    @pytest.mark.parametrize(
        "answer, count, valid",
        [
            ("1", 2, True),
            ("0", 2, True),
            ("2", 2, True),
            (" 2\n", 2, True),
            ("7", 2, False),
            ("3", 2, False),
            ("The first one", 2, False),
            ("1.", 2, False),
            ("", 2, False),
            ("-1", 2, False),
        ],
    )
    def test_only_a_number_from_zero_to_the_count_is_valid(self, answer, count, valid):
        assert tie_break_answer_valid(answer, count) is valid
