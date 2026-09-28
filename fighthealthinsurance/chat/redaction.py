"""The identifiers held for a chat and the records linked to it, for
letter_quality.Redactor, so chat text sent to an outside scorer is redacted
the way the letter scorer redacts a denial.

``chat_redactions`` covers:

* the chat's accounts: the professional's names, email, login, NPI, fax and
  contact record; the signed-in person's names, email, login and contact
  record; the domain's address;
* each linked appeal: its patient and professionals, the same fields as
  above, its domain's address, and everything
  ``common_view_logic.scoring_redactions`` collects for the appeal's denial
  (its email, claim id, plan id, fax and employer, its patient and
  professionals, its domain's address);
* each linked prior authorization request: the patient name, plan id,
  member id and date of birth written on it, its professionals and its
  domain's address.

Each person gets a token of their own ("PATIENT#<id>"), so two patients in
one professional's chat stay distinguishable. Sync, because it walks
relations; call it off the event loop. All or nothing: a record that cannot
be read raises, and a caller that gets no list sends nothing. A deleted chat
raises too.
"""

import datetime
from typing import Any, List, Optional, Tuple

from django.core.exceptions import ObjectDoesNotExist

# The category scoring_redactions gives the denial's patient.
_DENIAL_PATIENT = "PATIENT#patient"


class _Found:
    def __init__(self) -> None:
        self.out: List[Tuple[str, str]] = []

    def add(self, value: Any, category: str) -> None:
        text = str(value).strip() if value is not None else ""
        if text and text.upper() != "UNKNOWN":
            self.out.append((text, category))

    def login(self, user: Any) -> None:
        # A domain-scoped login is stored as raw🐼domain_id; the raw login
        # is the spelling a person would write, so both go in.
        username = str(getattr(user, "username", "") or "")
        self.add(username, "USERNAME")
        if "🐼" in username:
            self.add(username.split("🐼", 1)[0], "USERNAME")

    def account(self, user: Any, person: str) -> None:
        self.add(user.first_name, person)
        self.add(user.last_name, person)
        self.add(user.email, "EMAIL")
        self.login(user)
        try:
            contact = user.usercontactinfo
        except ObjectDoesNotExist:
            return
        self.add(contact.phone_number, "PHONE")
        self.add(contact.address1, "ADDRESS")
        self.add(contact.address2, "ADDRESS")

    def patient(self, patient: Any) -> None:
        if patient is None:
            return
        person = f"PATIENT#{patient.pk}"
        self.add(patient.get_legal_name(), person)
        self.add(patient.get_display_name(), person)
        self.account(patient.user, person)

    def professional(self, professional: Any) -> None:
        if professional is None:
            return
        person = f"PROFESSIONAL#{professional.pk}"
        self.add(professional.get_full_name(), person)
        self.add(professional.display_name, person)
        self.add(professional.npi_number, "NPI")
        self.add(professional.fax_number, "PHONE")
        self.account(professional.user, person)

    def domain(self, domain: Any) -> None:
        if domain is not None:
            self.add(domain.get_address(), "ADDRESS")

    def written_name(self, name: Optional[str], person: str) -> None:
        # A name typed on a form: the whole name, and each part the way a
        # first or last name is kept for an account.
        self.add(name, person)
        for part in str(name or "").split():
            self.add(part, person)

    def date_of_birth(self, dob: Optional[datetime.date]) -> None:
        if dob is None:
            return
        for spelling in (
            dob.isoformat(),
            dob.strftime("%m/%d/%Y"),
            f"{dob.month}/{dob.day}/{dob.year}",
        ):
            self.add(spelling, "DOB")


def chat_redactions(chat_id: Any) -> List[Tuple[str, str]]:
    """The identifiers for this chat, as (value, category) pairs for
    letter_quality.Redactor. See the module docstring for what is covered.
    Anonymous, unlinked chats have none, and the Redactor's generic email
    and phone patterns still run."""
    from fighthealthinsurance.common_view_logic import scoring_redactions
    from fighthealthinsurance.models import Appeal, OngoingChat, PriorAuthRequest

    chat = OngoingChat.objects.select_related(
        "user", "professional_user__user", "domain"
    ).get(pk=chat_id)
    found = _Found()

    professional = chat.professional_user
    found.professional(professional)
    user = chat.user
    if user is not None and (professional is None or professional.user_id != user.pk):
        try:
            patient = user.patientuser
        except ObjectDoesNotExist:
            patient = None
        if patient is not None:
            found.patient(patient)
        else:
            found.account(user, f"PATIENT#user{user.pk}")
    found.domain(chat.domain)

    appeals = Appeal.objects.filter(chat_id=chat.pk).select_related(
        "patient_user__user",
        "creating_professional__user",
        "primary_professional__user",
        "domain",
        "for_denial__patient_user__user",
        "for_denial__creating_professional__user",
        "for_denial__primary_professional__user",
        "for_denial__domain",
    )
    for appeal in appeals:
        found.patient(appeal.patient_user)
        found.professional(appeal.creating_professional)
        found.professional(appeal.primary_professional)
        found.domain(appeal.domain)
        denial = appeal.for_denial
        if denial is None:
            continue
        # Letter scoring's own list for the denial, so a denial's
        # identifiers are redacted here exactly as they are for its
        # letters; only its patient's token is made per person.
        person = f"PATIENT#{denial.patient_user_id}"
        for value, category in scoring_redactions(denial):
            found.add(value, person if category == _DENIAL_PATIENT else category)

    prior_auths = PriorAuthRequest.objects.filter(chat_id=chat.pk).select_related(
        "creator_professional_user__user",
        "created_for_professional_user__user",
        "domain",
    )
    for prior_auth in prior_auths:
        found.written_name(prior_auth.patient_name, f"PATIENT#pa-{prior_auth.pk}")
        found.add(prior_auth.plan_id, "PLAN_ID")
        found.add(prior_auth.member_id, "MEMBER_ID")
        found.date_of_birth(prior_auth.patient_dob)
        found.professional(prior_auth.creator_professional_user)
        found.professional(prior_auth.created_for_professional_user)
        found.domain(prior_auth.domain)
    return found.out
