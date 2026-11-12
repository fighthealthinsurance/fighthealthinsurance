"""A shared file's text is judged a plan document, a person's own papers, or
neither (plan_document_kind). Made-up text: no real plan or person."""

import time

import pytest

from fighthealthinsurance.plan_document_kind import (
    BY_DETAILS,
    BY_EOB,
    DETAIL_CHARS,
    OTHER,
    OTHER_MIN_WORDS,
    PERSONAL,
    PLAN,
    TITLE_CHARS,
    document_kind,
    judge,
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


def test_a_plan_description_with_a_members_id_on_it_stays_a_plan():
    assert document_kind("Member ID: W123456789\n" + SPD) == PLAN


def test_a_contents_line_naming_eobs_is_not_a_heading():
    contents = (
        "Summary Plan Description\nContents\n"
        "Explanation of Benefits ........................ 45\n"
        "Member ID: W123456789\n"
    )
    assert document_kind(contents + CONTRACT_TEXT + PLAN_TEXT) == PLAN


def test_a_spanish_bill_with_each_value_under_its_label_is_personal():
    bill = (
        "Factura médica\nNombre del paciente:\nRebecca Lee Crumpler\n"
        "Número de cuenta:\n55012-77\nFecha de nacimiento:\n02/08/1931\n"
        "Total a pagar: $60\n"
    )
    assert document_kind(bill) == PERSONAL


@pytest.mark.parametrize(
    "text",
    [
        "Member name:\n[removed]\nMember ID:\n[removed]\nAmount due $60\n",
        "Cardholder Name: [removed]\nCardholder ID: [removed]\nRx BIN 610014\n",
        "Name: [removed]\nID\n[removed]\nGroup 1234567\n",
    ],
    ids=["under_labels", "cardholder", "bare_labels"],
)
def test_cleaned_papers_with_their_details_removed_are_personal(text):
    assert document_kind(text) == PERSONAL


@pytest.mark.parametrize(
    "label",
    [
        "Fecha de nacimiento",
        "Nombre del paciente",
        "Número de cuenta",
        "Estimada",
        "Member",
        "Member name",
        "Date of birth",
        "Social security",
        "Explanation of",
        "has been",
    ],
)
def test_a_long_run_of_spaces_after_a_label_is_read_quickly(label):
    started = time.monotonic()
    document_kind(label + " " * 99_000 + "!")
    assert time.monotonic() - started < 2


SAMPLE_CARD = (
    "Sample ID card\nMember name: Jane Q. Sample\nMember ID: ABC123456789\n"
    "Date of birth: 01/01/1980\n"
)
UNLISTED_TITLE = "Crumpler Cooperative Wellness Guide\n"


def test_a_guide_with_a_sample_id_card_far_inside_stays_a_plan():
    body = CONTRACT_TEXT * (DETAIL_CHARS // len(CONTRACT_TEXT) + 1)
    assert document_kind(UNLISTED_TITLE + body + SAMPLE_CARD) == PLAN


def test_the_same_details_on_the_first_page_are_personal():
    assert document_kind(UNLISTED_TITLE + SAMPLE_CARD + CONTRACT_TEXT) == PERSONAL


@pytest.mark.parametrize(
    "line",
    [
        "Explanation of Benefits    45",
        "\tExplanation of Benefits\t45",
        "Explanation of Benefits (EOB)  45",
        "Explanation of Benefits ........ 45",
    ],
    ids=["spaces", "tabs", "abbreviation", "dot_leaders"],
)
def test_a_contents_line_is_not_a_heading(line):
    text = UNLISTED_TITLE + "Contents\n" + line + "\n" + CONTRACT_TEXT
    assert document_kind(text) == PLAN


@pytest.mark.parametrize(
    "heading",
    [
        "Your Explanation of Benefits",
        "Explanation of Benefits (EOB)",
        "Su explicación de beneficios",
    ],
)
def test_an_eob_heading_with_a_detail_is_personal(heading):
    statement = (
        f"Crumpler Mutual Health\n{heading}\nStatement date: 04/02/2026\n"
        "Member ID: W123456789\nClaim total $412.00\n"
    )
    assert judge(statement) == (PERSONAL, BY_EOB)


@pytest.mark.parametrize(
    "title",
    [
        "Certificate of Insurance",
        "Outline of Coverage",
        "Plan Document",
        "Group Insurance Policy",
        "Group Policy",
        "Evidence of Benefits",
        "Benefits Guide",
        "Plan Brochure",
        "Certificado de seguro",
        "Guía de beneficios",
        "Documento del plan",
    ],
)
def test_a_plan_title_keeps_a_plan_with_a_members_details_a_plan(title):
    details = "Member name: Rebecca Lee Crumpler\nMember ID: W123456789\n"
    assert document_kind(f"{title}\n{details}{CONTRACT_TEXT}") == PLAN


def test_without_a_plan_title_the_same_details_are_personal():
    details = "Member name: Rebecca Lee Crumpler\nMember ID: W123456789\n"
    assert judge(f"{UNLISTED_TITLE}{details}{CONTRACT_TEXT}") == (PERSONAL, BY_DETAILS)


@pytest.mark.parametrize("label", ["Group Policy:", "Group Policy", "Group Policy #"])
def test_a_group_policy_number_on_an_id_card_is_not_a_plan_title(label):
    assert document_kind(ID_CARD.replace("Group:", label)) == PERSONAL


def test_a_letter_naming_its_plan_documents_has_no_plan_title():
    assert document_kind("Please see your plan documents.\n" + ID_CARD) == PERSONAL
