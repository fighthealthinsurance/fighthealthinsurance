"""A shared file's text is judged a plan document, a person's own papers, or
neither (plan_document_kind). Made-up text: no real plan or person."""

import pytest

from fighthealthinsurance.plan_document_kind import (
    OTHER,
    OTHER_MIN_WORDS,
    PERSONAL,
    PLAN,
    TITLE_CHARS,
    document_kind,
)
from fighthealthinsurance.plan_document_scrub import scrub
from tests.plan_text_samples import (
    CHINESE_TEXT,
    CONTRACT_TEXT,
    DENIAL_LETTER,
    EOB,
    FORMULARY_TEXT,
    ID_CARD,
    PERSONALISED_SUMMARY,
    PLAN_TEXT,
    PROVIDER_BILL,
    RATE_TABLE_TEXT,
    RECIPE,
    SPANISH_DENIAL,
    SPANISH_EOB,
    SPANISH_TEXT,
    SPD,
    TABLE_TEXT,
)

@pytest.mark.parametrize(
    "text",
    [EOB, DENIAL_LETTER, PROVIDER_BILL, ID_CARD, SPANISH_EOB, SPANISH_DENIAL],
    ids=["eob", "denial", "bill", "id_card", "spanish_eob", "spanish_denial"],
)
def test_a_persons_own_papers_are_personal(text):
    assert document_kind(text) == PERSONAL


@pytest.mark.parametrize(
    "text",
    [
        PLAN_TEXT,
        TABLE_TEXT,
        CONTRACT_TEXT,
        FORMULARY_TEXT,
        SPANISH_TEXT,
        RATE_TABLE_TEXT,
    ],
    ids=["summary", "table", "contract", "formulary", "spanish", "rates"],
)
def test_plan_text_is_a_plan(text):
    assert document_kind(text) == PLAN


def test_a_plan_summary_with_a_members_name_and_id_is_still_a_plan():
    assert document_kind(PERSONALISED_SUMMARY) == PLAN


def test_a_plan_description_that_talks_about_claims_and_denials_is_a_plan():
    assert document_kind(SPD) == PLAN


def test_an_eob_title_under_a_plan_title_with_a_members_details_is_personal():
    assert document_kind("Summary of Benefits\n" + EOB) == PERSONAL


def test_a_personal_title_far_down_a_plan_document_is_not_a_title():
    text = PLAN_TEXT + "\n" * TITLE_CHARS + "This is not a bill."
    assert document_kind(text) == PLAN


def test_one_detail_alone_does_not_make_a_plan_document_personal():
    assert document_kind("Member ID: W123456789\n" + PLAN_TEXT) == PLAN


def test_text_with_no_plan_words_is_other():
    assert document_kind(RECIPE) == OTHER


def test_short_text_with_no_plan_words_stays_a_plan():
    assert document_kind(" ".join(RECIPE.split()[: OTHER_MIN_WORDS // 2])) == PLAN


def test_a_plan_description_that_opens_with_a_letter_to_participants_is_a_plan():
    letter = (
        "Dear Participant,\n\nThis booklet describes the benefits of the "
        "Crumpler Manufacturing Employee Health Plan. Please keep it with "
        "your important papers.\n\n"
    )
    assert document_kind(letter + SPD) == PLAN


def test_a_plan_cover_letter_to_a_named_member_is_a_plan():
    letter = (
        "Dear Rebecca Lee Crumpler,\n\nEnclosed is your Summary Plan "
        "Description.\nMember ID: W123456789\n\n"
    )
    assert document_kind(letter + CONTRACT_TEXT + PLAN_TEXT) == PLAN


def test_a_named_greeting_alone_is_not_enough_to_be_personal():
    assert document_kind("Dear Rebecca Lee Crumpler,\n\n" + PLAN_TEXT) == PLAN


def test_text_in_another_alphabet_stays_a_plan():
    assert document_kind(CHINESE_TEXT * 20) == PLAN


@pytest.mark.parametrize(
    "text",
    [EOB, DENIAL_LETTER, PROVIDER_BILL, ID_CARD],
    ids=["eob", "denial", "bill", "id_card"],
)
def test_papers_already_cleaned_are_still_personal(text):
    assert document_kind(scrub(text)) == PERSONAL


def test_a_plan_already_cleaned_is_still_a_plan():
    assert document_kind(scrub(PERSONALISED_SUMMARY)) == PLAN


def test_a_plan_chapter_about_denials_with_no_ones_details_is_a_plan():
    chapter = (
        "Chapter 9. What to do if you have a problem or complaint. If we "
        "denied your request for a service, you can ask for an appeal. "
    )
    assert document_kind(chapter + CONTRACT_TEXT + PLAN_TEXT) == PLAN


def test_a_denial_letter_that_mentions_the_plan_documents_is_personal():
    letter = DENIAL_LETTER.replace(
        "Your plan\ndoes not cover",
        "Under your plan documents and our formulary, your plan\ndoes not cover",
    )
    assert "plan documents" in letter
    assert document_kind(letter) == PERSONAL
