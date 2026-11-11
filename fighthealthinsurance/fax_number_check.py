"""Is this a number that takes faxes?

People often type their insurer's member-services phone number into the fax
box: it is the number printed most prominently on a denial letter. A fax to
a voice line rings a phone menu and fails, and then support goes looking
for a fax problem that is really a wrong number. :func:`lookup_non_fax_number`
recognises those numbers from:

* ``KnownNonFaxNumber`` rows (staff-curated, fixture ``known_non_fax_numbers``),
* the ``appeal_phone_number`` of every ``InsuranceCompany`` / ``InsurancePlan``,
  unless that same number is also on file as somebody's appeals fax.
"""

import re
from dataclasses import dataclass
from typing import Optional

VOICE = "voice"
DISCONNECTED = "disconnected"


@dataclass(frozen=True)
class NonFaxNumber:
    """What we know about a number that does not take faxes."""

    number: str
    kind: str
    # Whose line it is, e.g. "Aetna member services"; "" when unknown.
    label: str

    @property
    def description(self) -> str:
        """The number as a person would read it in a sentence."""
        if self.kind == DISCONNECTED:
            return "a number that is out of service"
        if self.label:
            return f"the {self.label} phone line, not a fax line"
        return "an insurance company's voice phone line, not a fax line"


def normalize_phone_digits(number: Optional[str]) -> Optional[str]:
    """The 10 digits of a US number, without punctuation or a leading 1.

    None for anything that isn't 10 digits once those are dropped, so an
    international or partial number never matches a stored one."""
    if not number:
        return None
    digits = re.sub(r"\D", "", number)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return None
    return digits


def _numbers_on_file(field: str) -> list[tuple[str, str]]:
    """(number, carrier name) for every carrier and plan with ``field`` set.

    Numbers are stored as typed ("800-788-0710", "(800) 788 0710"), so they
    are compared as digits in Python; there are a few hundred rows."""
    from fighthealthinsurance.models import InsuranceCompany, InsurancePlan

    company = InsuranceCompany.objects.exclude(**{field: ""}).values_list(field, "name")
    plan = InsurancePlan.objects.exclude(**{field: ""}).values_list(
        field, "insurance_company__name"
    )
    return list(company) + list(plan)


def lookup_non_fax_number(number: Optional[str]) -> Optional[NonFaxNumber]:
    """The known voice line / dead number ``number`` is, or None."""
    from fighthealthinsurance.models import KnownNonFaxNumber

    digits = normalize_phone_digits(number)
    if digits is None:
        return None
    known = KnownNonFaxNumber.objects.filter(number=digits).first()
    if known is not None:
        return NonFaxNumber(number=digits, kind=known.kind, label=known.label)
    owners = [
        name
        for phone, name in _numbers_on_file("appeal_phone_number")
        if normalize_phone_digits(phone) == digits
    ]
    if not owners:
        return None
    # A carrier that lists one number as both its phone and its fax (or two
    # carriers that disagree) may well take faxes there: say nothing.
    if any(
        normalize_phone_digits(fax) == digits
        for fax, _name in _numbers_on_file("appeal_fax_number")
    ):
        return None
    return NonFaxNumber(number=digits, kind=VOICE, label=f"{owners[0]} member services")


async def alookup_non_fax_number(number: Optional[str]) -> Optional[NonFaxNumber]:
    """Async :func:`lookup_non_fax_number`. It reads the ORM, so it is
    bridged with channels' database_sync_to_async (CLAUDE.md)."""
    from channels.db import database_sync_to_async

    result: Optional[NonFaxNumber] = await database_sync_to_async(
        lookup_non_fax_number
    )(number)
    return result
