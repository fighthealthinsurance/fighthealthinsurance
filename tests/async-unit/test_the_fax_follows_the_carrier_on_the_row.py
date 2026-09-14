"""The appeal fax follows the carrier the row holds, not the one just matched.

Every carrier column is protected separately, which is right on its own and
wrong together. If somebody corrects the insurer between two extraction runs,
their name survives while the plan and fax columns are still empty, so a retry
that matches the original carrier again fills those in from it. The row then
names one insurer and carries another's fax number, and choosing "fax my
appeal" sends the letter, with the diagnosis and history in it, to a company
that has nothing to do with the claim.
"""

import pytest

from fighthealthinsurance import common_view_logic
from fighthealthinsurance.models import Denial, InsuranceCompany, InsurancePlan

pytestmark = pytest.mark.asyncio


async def _carrier(name, fax):
    # regex is a RegexField with no default; a carrier row cannot be made
    # without one.
    return await InsuranceCompany.objects.acreate(
        name=name, regex=name, negative_regex="", appeal_fax_number=fax
    )


@pytest.mark.django_db(transaction=True)
async def test_the_fax_comes_from_the_carrier_the_row_names() -> None:
    theirs = await _carrier("Cigna", "555-0001")
    other = await _carrier("Aetna", "555-0002")
    denial = await Denial.objects.acreate(
        denial_text="a denial",
        hashed_email="x",
        insurance_company="Cigna",
        insurance_company_obj=theirs,
    )
    fax = await common_view_logic.DenialCreatorHelper._fax_for_the_carrier_on_the_row(
        denial.denial_id
    )
    assert fax == "555-0001", (
        f"the row names Cigna and the fax came back as {fax}, which is the "
        f"other carrier's number"
    )
    assert other.appeal_fax_number == "555-0002"


@pytest.mark.django_db(transaction=True)
async def test_a_carrier_with_no_fax_on_file_leaves_the_column_empty() -> None:
    """Empty asks the person for a number. Wrong does not."""
    theirs = await _carrier("Cigna", "")
    denial = await Denial.objects.acreate(
        denial_text="a denial",
        hashed_email="x",
        insurance_company="Cigna",
        insurance_company_obj=theirs,
    )
    fax = await common_view_logic.DenialCreatorHelper._fax_for_the_carrier_on_the_row(
        denial.denial_id
    )
    assert fax is None


@pytest.mark.django_db(transaction=True)
async def test_a_plan_belonging_to_another_carrier_is_not_used() -> None:
    """The same mismatch one level down."""
    theirs = await _carrier("Cigna", "555-0001")
    other = await _carrier("Aetna", "555-0002")
    other_plan = await InsurancePlan.objects.acreate(
        insurance_company=other,
        plan_name="Aetna PPO",
        regex="Aetna PPO",
        negative_regex="",
        appeal_fax_number="555-0003",
    )
    denial = await Denial.objects.acreate(
        denial_text="a denial",
        hashed_email="x",
        insurance_company="Cigna",
        insurance_company_obj=theirs,
        insurance_plan_obj=other_plan,
    )
    fax = await common_view_logic.DenialCreatorHelper._fax_for_the_carrier_on_the_row(
        denial.denial_id
    )
    assert fax == "555-0001", (
        f"the plan on the row belongs to Aetna while the row names Cigna, and "
        f"the fax came back as {fax}"
    )


@pytest.mark.django_db(transaction=True)
async def test_the_rows_own_plan_still_wins_over_its_carrier() -> None:
    theirs = await _carrier("Cigna", "555-0001")
    their_plan = await InsurancePlan.objects.acreate(
        insurance_company=theirs,
        plan_name="Cigna PPO",
        regex="Cigna PPO",
        negative_regex="",
        appeal_fax_number="555-0009",
    )
    denial = await Denial.objects.acreate(
        denial_text="a denial",
        hashed_email="x",
        insurance_company="Cigna",
        insurance_company_obj=theirs,
        insurance_plan_obj=their_plan,
    )
    fax = await common_view_logic.DenialCreatorHelper._fax_for_the_carrier_on_the_row(
        denial.denial_id
    )
    assert fax == "555-0009"
