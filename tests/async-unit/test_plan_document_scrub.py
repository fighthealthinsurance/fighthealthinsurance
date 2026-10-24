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
        "Policy number: 5551234567",
        "Plan name: Crumpler Gold PPO",
        "Plan ID 12345MA0010001",
        "Employer: Lee Crumpler Clinic",
        "Insurer: Crumpler Mutual Health",
        "Deductible $1,500 per person, $3,000 per family",
        "Coverage Period: 01/01/2026 - 12/31/2026",
        "Mailing address: PO Box 9, Crumpler Mutual Health",
        "Show your Member ID card at the pharmacy.",
        "The patient pays 20% coinsurance.",
        "Patient: you pay $30 per visit.",
        "Dear Member,\nWelcome to your plan.",
    ],
)
def test_plan_facts_are_kept(text):
    assert scrub(text) == text


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


@pytest.mark.parametrize(
    "label", ["Plan", "Group number", "Member ID", "Member name", "DOB", "SSN"]
)
def test_a_long_run_of_spaces_after_a_label_is_cleaned_quickly(label):
    # Text pulled from a PDF table can hold long runs of spaces.
    text = f"{label}{' ' * 20_000}!"
    started = time.monotonic()
    assert scrub(text) == text
    assert time.monotonic() - started < 1
