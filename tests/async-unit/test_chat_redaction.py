"""chat/redaction.py: the identifiers held for a chat and the records linked
to it, for redacting chat text before an outside scorer sees it.

A chat linked to an appeal carries that appeal's patient and professionals
and everything letter scoring collects for the appeal's denial; a chat
linked to a prior authorization request carries the patient name, plan id,
member id and date of birth written on it. A record that cannot be read
raises, so a caller sends nothing.
"""

import datetime
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model

from fhi_users.models import PatientUser, ProfessionalUser, UserContactInfo
from fighthealthinsurance.chat.redaction import chat_redactions
from fighthealthinsurance.common_view_logic import scoring_redactions
from fighthealthinsurance.ml import letter_quality
from fighthealthinsurance.models import (
    Appeal,
    Denial,
    OngoingChat,
    PriorAuthRequest,
)

User = get_user_model()


def _professional(username, first, last, npi):
    return ProfessionalUser.objects.create(
        user=User.objects.create_user(
            username=username,
            email=f"{username}@clinic.example",
            first_name=first,
            last_name=last,
        ),
        active=True,
        npi_number=npi,
    )


def _patient(username, first, last):
    return PatientUser.objects.create(
        user=User.objects.create_user(
            username=username,
            email=f"{username}@example.org",
            first_name=first,
            last_name=last,
        ),
        display_name=f"{first} {last[0]}.",
    )


def _found(chat):
    return dict(chat_redactions(chat.id))


@pytest.mark.django_db
def test_a_linked_appeal_brings_everything_letter_scoring_takes_from_its_denial():
    doctor = _professional("drwren", "Corvina", "Thistlewood", "1234567893")
    patient = _patient("marisol", "Marisol", "Quenby")
    UserContactInfo.objects.create(
        user=patient.user, phone_number="212-555-0199", address1="14 Larch Way"
    )
    denial = Denial.objects.create(
        hashed_email="h",
        denial_text="denied",
        raw_email="marisol.q@example.org",
        claim_id="CLM-778812",
        plan_id="PLN-99-ALPHA",
        appeal_fax_number="415-555-0142",
        employer_name="Brightwater Mills",
        patient_user=patient,
        primary_professional=doctor,
    )
    chat = OngoingChat.objects.create(professional_user=doctor)
    Appeal.objects.create(
        hashed_email="h", for_denial=denial, chat=chat, patient_user=patient
    )

    found = _found(chat)

    # Every value letter scoring collects for the denial is here too.
    for value, _category in scoring_redactions(denial):
        assert value in found, value
    person = f"PATIENT#{patient.pk}"
    for value, category in {
        "CLM-778812": "CLAIM_ID",
        "PLN-99-ALPHA": "PLAN_ID",
        "marisol.q@example.org": "EMAIL",
        "415-555-0142": "PHONE",
        "Brightwater Mills": "EMPLOYER",
        "Marisol Quenby": person,
        "Marisol": person,
        "Quenby": person,
        "marisol": "USERNAME",
        "212-555-0199": "PHONE",
        "14 Larch Way": "ADDRESS",
        "Corvina Thistlewood": f"PROFESSIONAL#{doctor.pk}",
        "1234567893": "NPI",
    }.items():
        assert found.get(value) == category, value


@pytest.mark.django_db
def test_an_appeals_own_patient_and_professionals_are_included():
    doctor = _professional("drowl", "Bubo", "Nightjar", "1111111111")
    other = _professional("drlark", "Alauda", "Skylark", "2222222222")
    patient = _patient("tamsin", "Tamsin", "Oakhollow")
    chat = OngoingChat.objects.create(professional_user=doctor)
    Appeal.objects.create(
        hashed_email="h",
        chat=chat,
        patient_user=patient,
        creating_professional=doctor,
        primary_professional=other,
    )
    found = _found(chat)
    assert found["Tamsin Oakhollow"] == f"PATIENT#{patient.pk}"
    assert found["tamsin@example.org"] == "EMAIL"
    assert found["Alauda Skylark"] == f"PROFESSIONAL#{other.pk}"
    assert found["2222222222"] == "NPI"


@pytest.mark.django_db
def test_a_linked_prior_auth_brings_its_patient_plan_member_and_birth_date():
    doctor = _professional("drfinch", "Fringilla", "Coelebs", "3333333333")
    chat = OngoingChat.objects.create(professional_user=doctor)
    prior_auth = PriorAuthRequest.objects.create(
        chat=chat,
        creator_professional_user=doctor,
        diagnosis="d",
        treatment="t",
        insurance_company="i",
        patient_name="Odalys Pennywhistle",
        plan_id="GRP-4410",
        member_id="MBR-00917733",
        patient_dob=datetime.date(1984, 3, 7),
    )
    found = _found(chat)
    person = f"PATIENT#pa-{prior_auth.pk}"
    for value, category in {
        "Odalys Pennywhistle": person,
        "Odalys": person,
        "Pennywhistle": person,
        "GRP-4410": "PLAN_ID",
        "MBR-00917733": "MEMBER_ID",
        "1984-03-07": "DOB",
        "03/07/1984": "DOB",
        "3/7/1984": "DOB",
        "Fringilla Coelebs": f"PROFESSIONAL#{doctor.pk}",
    }.items():
        assert found.get(value) == category, value


@pytest.mark.django_db
def test_two_patients_in_one_chat_get_tokens_of_their_own():
    doctor = _professional("drheron", "Ardea", "Cinerea", "4444444444")
    first = _patient("ines", "Ines", "Marchbank")
    second = _patient("oren", "Oren", "Blackwood")
    chat = OngoingChat.objects.create(professional_user=doctor)
    for patient in (first, second):
        denial = Denial.objects.create(
            hashed_email="h", denial_text="denied", patient_user=patient
        )
        Appeal.objects.create(hashed_email="h", chat=chat, for_denial=denial)
    redacted = letter_quality.redact(
        "Ines Marchbank and Oren Blackwood", chat_redactions(chat.id)
    )
    assert redacted == "[PATIENT_1] and [PATIENT_2]"


@pytest.mark.django_db
def test_a_linked_record_that_cannot_be_read_raises():
    doctor = _professional("drkite", "Milvus", "Regalis", "5555555555")
    chat = OngoingChat.objects.create(professional_user=doctor)
    denial = Denial.objects.create(hashed_email="h", denial_text="denied")
    Appeal.objects.create(hashed_email="h", chat=chat, for_denial=denial)
    with patch(
        "fighthealthinsurance.common_view_logic.scoring_redactions",
        side_effect=RuntimeError("db away"),
    ):
        with pytest.raises(RuntimeError):
            chat_redactions(chat.id)


@pytest.mark.django_db
def test_a_deleted_chat_raises():
    chat = OngoingChat.objects.create()
    chat_id = chat.id
    chat.delete()
    with pytest.raises(OngoingChat.DoesNotExist):
        chat_redactions(chat_id)


@pytest.mark.django_db
def test_an_anonymous_unlinked_chat_has_none():
    assert chat_redactions(OngoingChat.objects.create().id) == []
