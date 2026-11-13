"""The insurer, plan year and plan type our model reads with a plan's
sponsor, and the second look at a long document that named no sponsor.

Each fact in the model's answer is checked against the document on its own
and dropped alone when it fails: its quoted line must be in the text and,
with personal details taken out, still give it. The insurer's whole name
must be in its line; the year must be four digits in its line, from 2000 to
next year; the type must be one of PolicyDocument's plan categories, with a
word in its line that names that kind of plan.

The second look reads the passages around party words that the first
excerpt left out, the sponsor's own words first, within its own budget.
Nothing here asks a model.
"""

import json

import pytest
from django.utils import timezone

from fighthealthinsurance.models import PolicyDocument
from fighthealthinsurance.plan_documents_processing import clean
from fighthealthinsurance.plan_sponsor_reader import (
    _PLAN_TYPE_WORDS,
    ASK,
    MAX_EXCERPT_CHARS,
    SECOND_EXCERPT_CHARS,
    PlanFacts,
    checked_facts,
    excerpt,
    second_excerpt,
)
from tests.plan_text_samples import long_spd

INSURER_LINE = "Crumpler Health Mutual: Gold PPO"
YEAR_LINE = "Coverage Period: 01/01/2026 - 12/31/2026"
TYPE_LINE = "This group health plan is subject to ERISA."
PAGE_ONE = (
    "Summary of Benefits and Coverage: What this Plan Covers & What You Pay\n"
    f"{INSURER_LINE}    {YEAR_LINE}\n"
    f"{TYPE_LINE}\n"
    "Member Name: Rebecca Lee Crumpler    Date of Birth: 03/14/2001\n"
    "Group number 202612\n"
) + ("Covered services and what the plan pays. " * 20)


def facts(text=PAGE_ONE, **fields):
    # Checked against the text as stored, as the reading does.
    data = {"sponsor": None, "ein": None, "evidence": None} | fields
    return checked_facts(json.dumps(data), clean(text))


class TestTheInsurer:
    def test_an_insurer_its_quoted_line_names_is_taken(self):
        found = facts(insurer="Crumpler Health Mutual", insurer_evidence=INSURER_LINE)
        assert found.insurer_name == "Crumpler Health Mutual"

    def test_an_insurer_whose_line_is_not_in_the_document_is_dropped(self):
        found = facts(
            insurer="Crumpler Health Mutual",
            insurer_evidence="Insured by Crumpler Health Mutual",
        )
        assert found.insurer_name == ""

    def test_an_insurer_its_line_does_not_name_in_full_is_dropped(self):
        found = facts(
            insurer="Crumpler Health Mutual Insurance Company",
            insurer_evidence=INSURER_LINE,
        )
        assert found.insurer_name == ""

    def test_a_person_given_as_the_insurer_is_dropped(self):
        found = facts(
            insurer="Rebecca Lee Crumpler",
            insurer_evidence="Member Name: Rebecca Lee Crumpler",
        )
        assert found.insurer_name == ""

    def test_an_insurer_whose_quote_holds_an_email_is_still_taken(self):
        line = "Crumpler Health Mutual: Gold PPO  questions: rebecca@example.com"
        found = facts(PAGE_ONE + line, insurer="Crumpler Health Mutual", insurer_evidence=line)
        assert found.insurer_name == "Crumpler Health Mutual"

    def test_an_insurer_with_letters_its_line_does_not_have_is_dropped(self):
        found = facts(
            insurer="Crumpler Health Mutual 李", insurer_evidence=INSURER_LINE
        )
        assert found.insurer_name == ""

    def test_an_accented_insurer_its_line_names_is_taken(self):
        line = "Cooperativa Médica Crumpler: Plan Oro"
        found = facts(
            PAGE_ONE + line, insurer="Cooperativa Médica Crumpler", insurer_evidence=line
        )
        assert found.insurer_name == "Cooperativa Médica Crumpler"

    def test_a_person_quoted_without_their_label_is_dropped(self):
        found = facts(
            insurer="Rebecca Lee Crumpler", insurer_evidence="Rebecca Lee Crumpler"
        )
        assert found.insurer_name == ""

    def test_the_employer_given_as_its_own_insurer_is_dropped(self):
        found = facts(
            sponsor="Crumpler Health Mutual",
            insurer="Crumpler Health Mutual",
            insurer_evidence=INSURER_LINE,
        )
        assert found.insurer_name == ""


class TestThePlanYear:
    @pytest.mark.parametrize("given", [2026, "2026", " 2026 "])
    def test_a_year_in_its_quoted_line_is_taken(self, given):
        assert facts(plan_year=given, plan_year_evidence=YEAR_LINE).plan_year == 2026

    def test_a_year_its_line_does_not_give_is_dropped(self):
        found = facts(plan_year=2025, plan_year_evidence=YEAR_LINE)
        assert found.plan_year is None

    def test_a_year_whose_line_is_not_in_the_document_is_dropped(self):
        found = facts(plan_year=2026, plan_year_evidence="Plan year 2026")
        assert found.plan_year is None

    def test_a_year_before_2000_is_dropped(self):
        line = "Coverage Period: 01/01/1999 - 12/31/1999"
        found = facts(PAGE_ONE + line, plan_year=1999, plan_year_evidence=line)
        assert found.plan_year is None

    def test_a_year_after_next_year_is_dropped(self):
        later = timezone.now().year + 2
        line = f"Coverage Period: 01/01/{later} - 12/31/{later}"
        found = facts(PAGE_ONE + line, plan_year=later, plan_year_evidence=line)
        assert found.plan_year is None

    def test_next_year_is_taken(self):
        next_year = timezone.now().year + 1
        line = f"Coverage Period: 01/01/{next_year} - 12/31/{next_year}"
        found = facts(PAGE_ONE + line, plan_year=next_year, plan_year_evidence=line)
        assert found.plan_year == next_year

    def test_a_year_inside_a_longer_number_is_dropped(self):
        found = facts(plan_year=2026, plan_year_evidence="Group number 202612")
        assert found.plan_year is None

    @pytest.mark.parametrize(
        "line",
        [
            "Coverage Period: 07/01/2025 - 06/30/2026",
            "Plan year: July 1, 2025 through June 30, 2026",
            "Coverage period 2025-07-01 to 2026-06-30",
        ],
    )
    @pytest.mark.parametrize("given, kept", [(2025, 2025), (2026, None)])
    def test_a_range_across_two_years_gives_the_year_it_starts(self, line, given, kept):
        found = facts(PAGE_ONE + line, plan_year=given, plan_year_evidence=line)
        assert found.plan_year == kept

    def test_a_year_beside_a_range_within_one_year_is_kept(self):
        line = "Plan Year 2026. Open enrollment 11/01/2025 - 12/15/2025"
        found = facts(PAGE_ONE + line, plan_year=2026, plan_year_evidence=line)
        assert found.plan_year == 2026

    def test_a_birth_year_is_dropped(self):
        found = facts(
            plan_year=2001,
            plan_year_evidence="Member Name: Rebecca Lee Crumpler    Date of Birth: 03/14/2001",
        )
        assert found.plan_year is None

    @pytest.mark.parametrize(
        "line",
        [
            "2026 Evidence of Coverage",
            "2026 Summary of Benefits",
            "Benefits Booklet 2026",
            "Open Enrollment 2026",
            "2026 Certificate of Coverage",
        ],
    )
    def test_a_year_in_the_documents_title_is_taken(self, line):
        found = facts(PAGE_ONE + line, plan_year=2026, plan_year_evidence=line)
        assert found.plan_year == 2026

    def test_a_year_from_a_line_that_is_not_the_plans_year_is_dropped(self):
        line = "Revision date: 2026"
        found = facts(PAGE_ONE + line, plan_year=2026, plan_year_evidence=line)
        assert found.plan_year is None

    def test_a_birth_date_quoted_without_its_label_is_dropped(self):
        found = facts(plan_year=2001, plan_year_evidence="03/14/2001")
        assert found.plan_year is None

    @pytest.mark.parametrize("given", [2026.0, True, "２０２６", "26", "2026-27"])
    def test_a_year_that_is_not_four_plain_digits_is_dropped(self, given):
        assert facts(plan_year=given, plan_year_evidence=YEAR_LINE).plan_year is None


class TestThePlanType:
    def test_a_type_its_quoted_line_names_is_taken(self):
        found = facts(plan_type="employer_erisa", plan_type_evidence=TYPE_LINE)
        assert found.plan_category == "employer_erisa"

    @pytest.mark.parametrize("given", ["ppo", "unknown", "Employer Plan (ERISA)", 3])
    def test_a_type_that_is_not_one_of_our_plan_categories_is_dropped(self, given):
        found = facts(plan_type=given, plan_type_evidence=TYPE_LINE)
        assert found.plan_category == ""

    def test_a_type_its_line_does_not_name_is_dropped(self):
        found = facts(plan_type="medicaid_chip", plan_type_evidence=TYPE_LINE)
        assert found.plan_category == ""

    def test_a_type_whose_line_is_not_in_the_document_is_dropped(self):
        found = facts(
            plan_type="employer_erisa", plan_type_evidence="This plan is an ERISA plan."
        )
        assert found.plan_category == ""

    @pytest.mark.parametrize(
        "kind, kept",
        [("employer_erisa", ""), ("employer_non_erisa", "employer_non_erisa")],
    )
    def test_a_plan_not_subject_to_erisa_is_not_an_erisa_plan(self, kind, kept):
        line = "This plan is a governmental plan and is not subject to ERISA."
        found = facts(PAGE_ONE + line, plan_type=kind, plan_type_evidence=line)
        assert found.plan_category == kept

    @pytest.mark.parametrize(
        "kind, line",
        [
            ("medicare_traditional", "This is a Medicare Advantage plan."),
            ("aca_marketplace", "This individual plan is sold off-exchange."),
            ("individual_off_exchange", "This individual plan is sold on the Marketplace."),
            ("employer_erisa", "This is not an ERISA plan."),
            ("medicare_advantage", "This is not a Medicare Advantage plan."),
            ("medicare_traditional", "If you have Medicare, this plan pays secondary."),
            ("medicare_traditional", "This plan is secondary to Medicare."),
            ("medicare_traditional", "This plan is not Original Medicare."),
            ("medicare_traditional", "This is not traditional Medicare."),
        ],
    )
    def test_a_line_that_rules_its_type_out_drops_it(self, kind, line):
        found = facts(PAGE_ONE + line, plan_type=kind, plan_type_evidence=line)
        assert found.plan_category == ""

    @pytest.mark.parametrize(
        "kind, line",
        [
            ("medicare_advantage", "This is a Medicare Advantage plan."),
            ("individual_off_exchange", "This individual plan is sold off-exchange."),
            ("aca_marketplace", "This plan is sold on the Health Insurance Marketplace."),
            ("employer_non_erisa", "This is not an ERISA plan."),
            ("medicare_traditional", "Original Medicare Part A and Part B coverage."),
            ("medicare_traditional", "Original Medicare pays primary for covered services."),
        ],
    )
    def test_a_line_that_names_its_type_keeps_it(self, kind, line):
        found = facts(PAGE_ONE + line, plan_type=kind, plan_type_evidence=line)
        assert found.plan_category == kind

    def test_every_plan_category_but_unknown_has_the_words_that_name_it(self):
        categories = {key for key, _ in PolicyDocument.PLAN_CATEGORY_CHOICES}
        assert set(_PLAN_TYPE_WORDS) == categories - {"unknown"}

    def test_the_question_offers_every_plan_type(self):
        assert all(kind in ASK for kind in _PLAN_TYPE_WORDS)


class TestEachFactStandsAlone:
    def test_one_fact_that_fails_leaves_the_others(self):
        found = facts(
            insurer="Crumpler Health Mutual",
            insurer_evidence=INSURER_LINE,
            plan_year=2019,
            plan_year_evidence=YEAR_LINE,
            plan_type="employer_erisa",
            plan_type_evidence=TYPE_LINE,
        )
        assert found == PlanFacts("Crumpler Health Mutual", None, "employer_erisa")

    def test_an_answer_that_is_not_json_has_no_facts(self):
        assert checked_facts("The insurer is Crumpler Health Mutual.", PAGE_ONE) == (
            PlanFacts()
        )

    def test_an_answer_with_no_facts_has_none(self):
        assert facts() == PlanFacts()


class TestTheSecondLook:
    def test_it_reads_the_plan_information_the_first_excerpt_left_out(self):
        text = long_spd(generic_blocks=5)
        assert "Plan Sponsor: Crumpler Cooperative" not in excerpt(text)
        assert "Plan Sponsor: Crumpler Cooperative" in second_excerpt(text)

    def test_the_sponsors_own_words_come_before_other_party_words(self):
        # In document order the general passages before it would fill the
        # budget first.
        text = long_spd(generic_blocks=40)
        assert "Plan Sponsor: Crumpler Cooperative" in second_excerpt(text)

    def test_it_reads_nothing_the_first_excerpt_read(self):
        assert "Summary Plan Description" not in second_excerpt(long_spd())

    def test_it_stays_within_its_budget(self):
        more = second_excerpt(long_spd(generic_blocks=40))
        assert 0 < len(more) <= SECOND_EXCERPT_CHARS + 50

    def test_a_document_the_first_excerpt_reads_whole_has_none(self):
        text = PAGE_ONE * 5
        assert len(text) <= MAX_EXCERPT_CHARS
        assert second_excerpt(text) == ""

    def test_a_long_document_with_no_party_words_left_out_has_none(self):
        text = "Plan Sponsor: Crumpler Cooperative\n" + (
            "Covered services and what it pays. " * 1000
        )
        assert second_excerpt(text) == ""
