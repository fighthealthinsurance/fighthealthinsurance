"""plan_document_scrub: personal details out of shared plan documents, plan facts kept.

The text of a shared file is cleaned before it is stored. What names, reaches
or identifies a person is replaced with [removed]; what makes the document
useful for research (group numbers, plan names, the employer and insurer,
dollar amounts, the plan's own toll-free lines, coverage dates) is kept.
"""

import time

import pytest

from fighthealthinsurance.plan_document_scrub import REMOVED, removal_spans, scrub


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Member ID: W123456789", f"Member ID: {REMOVED}"),
        ("MEMBER ID # XYZ 98765432", f"MEMBER ID # {REMOVED}"),
        ("Subscriber ID W1234-5678", f"Subscriber ID {REMOVED}"),
        ("Subscriber number: 00012345", f"Subscriber number: {REMOVED}"),
        ("Claim #: 2026-ABC-99812", f"Claim #: {REMOVED}"),
        ("ID: W123456789\nRxBIN 610014", f"ID: {REMOVED}\nRxBIN 610014"),
    ],
)
def test_identifiers_after_their_labels_are_removed(text, expected):
    assert scrub(text) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Medicare Number: 1EG4-TE5-MK73", f"Medicare Number: {REMOVED}"),
        ("MBI: 1EG4-TE5-MK73", f"MBI: {REMOVED}"),
        ("Medicare ID 1EG4TE5MK73", f"Medicare ID {REMOVED}"),
        ("Medicaid ID #: 123456789012", f"Medicaid ID #: {REMOVED}"),
        ("Cardholder ID: W123456789", f"Cardholder ID: {REMOVED}"),
        ("Cardholder Name: Rebecca Lee Crumpler", f"Cardholder Name: {REMOVED}"),
        ("Beneficiary: Rebecca Lee Crumpler", f"Beneficiary: {REMOVED}"),
        ("Recipient ID: 12345678901", f"Recipient ID: {REMOVED}"),
        ("Recipient: Mary Eliza Mahoney", f"Recipient: {REMOVED}"),
        ("ID Number: W123456789", f"ID Number: {REMOVED}"),
        ("Identification Number: 123456789", f"Identification Number: {REMOVED}"),
        ("ID# W123 456 789", f"ID# {REMOVED}"),
        (
            "Group 0012345   ID# W123 456 789",
            f"Group 0012345   ID# {REMOVED}",
        ),
        ("Patient Account Number: 000123456", f"Patient Account Number: {REMOVED}"),
        ("Patient Account #: A123456", f"Patient Account #: {REMOVED}"),
    ],
)
def test_medicare_medicaid_card_and_account_numbers_are_removed(text, expected):
    assert scrub(text) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Policy Number: XYZ 123 456", f"Policy Number: {REMOVED}"),
        ("Policy number: 5551234567", f"Policy number: {REMOVED}"),
        ("Policy #: 0012345", f"Policy #: {REMOVED}"),
        ("Policy ID 00123456789", f"Policy ID {REMOVED}"),
        ("Policy: W123456789", f"Policy: {REMOVED}"),
        ("Claim Number: 2026ABC123", f"Claim Number: {REMOVED}"),
    ],
)
def test_policy_and_claim_numbers_are_removed(text, expected):
    # On a plan bought alone, a policy number can point to one person.
    assert scrub(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Plan ID Number: 12345MA0010001",
        "Group ID #: 0012345",
        "RxBIN: 610014 RxPCN: MEDDPRIME RxGRP: RX1234",
        "Payer ID: 12345",
        "ERISA plan number: 501",
        "Policy year: 2026",
        "Policy period: 01/01/2026 - 12/31/2026",
    ],
)
def test_plan_numbers_next_to_the_new_labels_are_kept(text):
    assert scrub(text) == text


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Patient: Rebecca Lee Crumpler", f"Patient: {REMOVED}"),
        ("Member name: Elizabeth Blackwell", f"Member name: {REMOVED}"),
        ("PATIENT NAME: MARY SEACOLE", f"PATIENT NAME: {REMOVED}"),
        ("Policyholder Name Susan La Flesche", f"Policyholder Name {REMOVED}"),
        ("Name: Rebecca Lee Crumpler\n", f"Name: {REMOVED}\n"),
        ("Dear Elizabeth Blackwell,\nThank you", f"Dear {REMOVED},\nThank you"),
    ],
)
def test_names_after_their_labels_are_removed(text, expected):
    assert scrub(text) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("MEMBER: CRUMPLER, REBECCA L", f"MEMBER: {REMOVED}"),
        ("Subscriber: Crumpler, Rebecca L", f"Subscriber: {REMOVED}"),
        ("PATIENT NAME: BLACKWELL, ELIZABETH", f"PATIENT NAME: {REMOVED}"),
        (
            "Member name: La Flesche, Susan\nGroup Number: 1234567890",
            f"Member name: {REMOVED}\nGroup Number: 1234567890",
        ),
    ],
)
def test_a_surname_first_name_after_a_label_is_removed_whole(text, expected):
    assert scrub(text) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Member ID:\nW123456789", f"Member ID:\n{REMOVED}"),
        ("Subscriber ID\n  XYZ123456789", f"Subscriber ID\n  {REMOVED}"),
        ("Member Name:\nRebecca Lee Crumpler", f"Member Name:\n{REMOVED}"),
        ("Patient:\nCRUMPLER, REBECCA", f"Patient:\n{REMOVED}"),
        ("Date of Birth\n02/08/1831", f"Date of Birth\n{REMOVED}"),
        ("DOB:\nFebruary 8, 1831", f"DOB:\n{REMOVED}"),
        ("SSN:\n123456789", f"SSN:\n{REMOVED}"),
        ("Home Address:\n12 Beacon Street", f"Home Address:\n{REMOVED}"),
    ],
)
def test_a_value_on_the_line_after_its_label_is_removed(text, expected):
    # Text pulled from PDF forms and tables often puts the value below.
    assert scrub(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Patient Name\nDate of Service: 03/04/2026",
        "Member ID\nGroup Number: 1234567890",
        "Member Name:\nPlan: Gold PPO",
        "Member ID\n\nOverall deductible: $1,500",
    ],
)
def test_a_label_on_the_line_below_is_not_taken_for_a_value(text):
    assert scrub(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "Dear Member,",
        "Dear Employee,",
        "Dear Plan Participant,",
        "Dear Participant:",
        "Dear Subscriber,",
        "Dear Customer,",
        "Dear Medicare Beneficiary,",
        "Dear Parent or Guardian,",
    ],
)
def test_a_greeting_to_everyone_is_kept(text):
    assert scrub(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "NPI: 1234567890",
        "Provider NPI #: 1234567890",
        "Your doctor, Mary Eliza Mahoney MD, NPI 1234567890",
    ],
)
def test_a_providers_npi_is_kept(text):
    assert scrub(text) == text


def test_a_name_stops_before_the_next_label():
    assert scrub("Patient: Rebecca Crumpler DOB: 02/08/1831") == (
        f"Patient: {REMOVED} DOB: {REMOVED}"
    )


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Date of birth: 02/08/1831", f"Date of birth: {REMOVED}"),
        ("DOB 2-8-1831", f"DOB {REMOVED}"),
        ("D.O.B.: February 8, 1831", f"D.O.B.: {REMOVED}"),
        ("Birthdate 8 Feb 1831", f"Birthdate {REMOVED}"),
    ],
)
def test_dates_of_birth_are_removed(text, expected):
    assert scrub(text) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("SSN: 123-45-6789", f"SSN: {REMOVED}"),
        ("Social Security number XXX-XX-6789", f"Social Security number {REMOVED}"),
        ("on file as 987-65-4321.", f"on file as {REMOVED}."),
    ],
)
def test_social_security_numbers_are_removed(text, expected):
    assert scrub(text) == expected


def test_an_address_after_its_label_is_removed():
    text = "Address: 12 Beacon Street, Boston MA 02108\nPlan year 2026"
    assert scrub(text) == f"Address: {REMOVED}\nPlan year 2026"


@pytest.mark.parametrize(
    "text, expected",
    [
        (
            "Mailing Address: 12 Beacon Street, Boston MA 02108",
            f"Mailing Address: {REMOVED}",
        ),
        ("Mailing address: PO Box 77, Boston MA 02108", f"Mailing address: {REMOVED}"),
        ("Address 12 Beacon Street, Boston MA 02108", f"Address {REMOVED}"),
        ("Home Address 12 Beacon Street", f"Home Address {REMOVED}"),
        ("Home Address\n12 Beacon Street", f"Home Address\n{REMOVED}"),
        (
            "Your address on file: 12 Beacon Street, Boston MA",
            f"Your address on file: {REMOVED}",
        ),
    ],
)
def test_a_members_address_is_removed_with_or_without_a_colon(text, expected):
    assert scrub(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Send appeals to: Crumpler Mutual Health, Attn: Appeals, PO Box 9, "
        "Boston MA 02108",
        "Mail to: Crumpler Mutual Health Appeals, PO Box 9, Boston MA 02108",
        "Plan address: PO Box 9, Boston MA 02108",
        "Plan Sponsor: Lee Crumpler Clinic, 100 Main Street, Boston, MA 02108",
        "Address changes must be made in writing.",
    ],
)
def test_the_plans_own_mailing_lines_are_kept(text):
    assert scrub(text) == text


def test_emails_and_local_phone_numbers_are_removed():
    text = "Write to rebecca.crumpler@example.com or call (617) 555-0142."
    assert scrub(text) == f"Write to {REMOVED} or call {REMOVED}."


@pytest.mark.parametrize(
    "number",
    [
        "1-800-555-0100",
        "(833) 555-0100",
        "844.555.0100",
        "855-555-0100",
        "866 555 0100",
        "+1 877 555 0100",
        "888-555-0100",
    ],
)
def test_toll_free_numbers_are_kept(number):
    text = f"Member services: {number}"
    assert scrub(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "Group Number: 1234567890",
        "Group #: 0042-77",
        "Plan name: Crumpler Gold PPO",
        "Plan ID 12345MA0010001",
        "Employer: Lee Crumpler Clinic",
        "Insurer: Crumpler Mutual Health",
        "Deductible $1,500 per person, $3,000 per family",
        "Coverage Period: 01/01/2026 - 12/31/2026",
        "Show your Member ID card at the pharmacy.",
        "The patient pays 20% coinsurance.",
        "Patient: you pay $30 per visit.",
        "Dear Member,\nWelcome to your plan.",
    ],
)
def test_plan_facts_are_kept(text):
    assert scrub(text) == text


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Email: rebecca.crumpler@plan.example.com", f"Email: {REMOVED}"),
        (
            "Home address: 12 Plan Street, Boston MA 02108",
            f"Home address: {REMOVED}",
        ),
    ],
)
def test_a_detail_that_runs_past_a_plan_fact_is_removed(text, expected):
    assert scrub(text) == expected


def test_a_summary_of_benefits_keeps_its_facts_and_loses_the_person():
    text = (
        "Crumpler Mutual Health: Gold PPO\n"
        "Coverage Period: 01/01/2026 - 12/31/2026\n"
        "Group Number: 1234567890  Employer: Lee Crumpler Clinic\n"
        "Member name: Rebecca Lee Crumpler  Member ID: W123456789\n"
        "Questions: call 1-800-555-0100 or visit crumplermutual.example\n"
        "Overall deductible: $1,500\n"
    )
    cleaned = scrub(text)
    for kept in (
        "Gold PPO",
        "01/01/2026 - 12/31/2026",
        "1234567890",
        "Lee Crumpler Clinic",
        "1-800-555-0100",
        "$1,500",
    ):
        assert kept in cleaned
    for gone in ("Rebecca Lee Crumpler", "W123456789"):
        assert gone not in cleaned


def test_nothing_found_leaves_the_text_as_it_was():
    text = "Out-of-pocket limit: $6,000 for one person."
    assert removal_spans(text) == []
    assert scrub(text) == text


def test_empty_text_stays_empty():
    assert scrub("") == ""


def test_a_long_document_is_cleaned_quickly():
    # About two million characters, the most a reading keeps.
    line = "Member ID: W123456789 Group Number 1234567890 call 617-555-0142 "
    text = line * (2_000_000 // len(line))
    started = time.monotonic()
    cleaned = scrub(text)
    assert time.monotonic() - started < 10
    assert "W123456789" not in cleaned and "617-555-0142" not in cleaned
    assert cleaned.count("1234567890") == text.count("1234567890")


LONG_RUN_LABELS = [
    "Plan",
    "Plan ID",
    "Group number",
    "Member ID",
    "Member name",
    "Member:",
    "DOB",
    "SSN",
    "Home address:",
    "Name:",
    "Medicare Number",
    "MBI",
    "ID#",
    "ID Number",
    "Patient Account",
    "Policy",
    "Cardholder Name",
    "ID# W123",
    "Mailing address",
    "Address",
    "Your address on file",
    "NPI",
    "Dear",
]


@pytest.mark.parametrize("label", LONG_RUN_LABELS)
@pytest.mark.parametrize(
    "gap",
    [" " * 20_000, " " * 20_000 + "\n" + " " * 20_000, "\t" * 20_000],
    ids=["spaces", "spaces-line-spaces", "tabs"],
)
def test_a_long_run_of_spaces_after_a_label_is_cleaned_quickly(label, gap):
    # Text pulled from a PDF table can hold long runs of spaces.
    text = f"{label}{gap}!"
    started = time.monotonic()
    cleaned = scrub(text)
    assert time.monotonic() - started < 1
    # The run itself is never taken for a value.
    assert gap in cleaned


def test_a_long_run_of_spaces_inside_a_surname_first_name_is_cleaned_quickly():
    text = "Member: Crumpler," + " " * 20_000 + "!"
    started = time.monotonic()
    scrub(text)
    assert time.monotonic() - started < 1
