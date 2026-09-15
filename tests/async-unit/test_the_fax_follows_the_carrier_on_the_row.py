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


# The same rule, through the writers rather than the helper. The four tests
# above prove the helper; these prove each writer obeys it.

from unittest.mock import AsyncMock, patch

from fighthealthinsurance.common_view_logic import DenialCreatorHelper

# Loading a carrier or plan as a model instance compiles both of its regex
# columns, and an empty negative_regex is stored as NULL, which the field
# refuses. The writers below load instances; the helper tests above do not.
NEVER = "zzz-never-matches-zzz"


async def _carrier_row(name, fax):
    return await InsuranceCompany.objects.acreate(
        name=name, regex=name, negative_regex=NEVER, appeal_fax_number=fax
    )


async def _plan_of(carrier, name, fax, regex=None):
    return await InsurancePlan.objects.acreate(
        insurance_company=carrier,
        plan_name=name,
        regex=regex if regex is not None else name,
        negative_regex=NEVER,
        appeal_fax_number=fax,
    )


@pytest.mark.django_db(transaction=True)
async def test_the_fax_extractor_does_not_take_another_carriers_plan_fax() -> None:
    """The row names Cigna and, from an earlier mismatch, holds an Aetna plan.
    The letter has no fax in it. The fallback used to take the plan's."""
    theirs = await _carrier_row("Cigna", "")
    other = await _carrier_row("Aetna", "555-0002")
    other_plan = await _plan_of(other, "Aetna PPO", "555-0003")
    denial = await Denial.objects.acreate(
        denial_text="a denial with no fax number in it",
        hashed_email="x",
        insurance_company="Cigna",
        insurance_company_obj=theirs,
        insurance_plan_obj=other_plan,
    )
    with patch(
        "fighthealthinsurance.common_view_logic.appealGenerator.get_fax_number",
        new=AsyncMock(return_value=None),
    ), patch.object(
        DenialCreatorHelper, "get_plan_documents_text", new=AsyncMock(return_value="")
    ):
        outcome = await DenialCreatorHelper.extract_set_fax_number(denial.denial_id)

    stored = (
        await Denial.objects.filter(denial_id=denial.denial_id)
        .values_list("appeal_fax_number", flat=True)
        .afirst()
    )
    assert not stored, f"the fax column holds {stored}, another carrier's number"
    assert outcome in (None, "")


@pytest.mark.django_db(transaction=True)
async def test_the_regex_plan_matcher_does_not_store_another_carriers_plan() -> None:
    theirs = await _carrier_row("Cigna", "555-0001")
    other = await _carrier_row("Aetna", "555-0002")
    await _plan_of(other, "Aetna PPO", "555-0003", regex="Aetna PPO")
    denial = await Denial.objects.acreate(
        denial_text="Your Aetna PPO claim was denied.",
        hashed_email="x",
        insurance_company="Cigna",
        insurance_company_obj=theirs,
    )

    matched = await DenialCreatorHelper.match_insurance_plan_from_regex(
        denial.denial_id
    )

    row = (
        await Denial.objects.filter(denial_id=denial.denial_id)
        .values("insurance_plan_obj_id", "insurance_company_obj_id")
        .afirst()
    )
    assert matched is None
    assert row["insurance_plan_obj_id"] is None
    assert row["insurance_company_obj_id"] == theirs.id


@pytest.mark.django_db(transaction=True)
async def test_a_match_that_contradicts_the_insurer_the_person_named_is_not_stored() -> (
    None
):
    """The person typed Cigna in the box and picked nothing structured. A
    retry reading the letter matches Aetna. Their words outrank the letter."""
    await _carrier_row("Cigna", "555-0001")
    await _carrier_row("Aetna", "555-0002")
    denial = await Denial.objects.acreate(
        denial_text="Aetna has denied your claim.",
        hashed_email="x",
        insurance_company="Cigna",
    )
    with patch(
        "fighthealthinsurance.common_view_logic.appealGenerator.get_insurance_company",
        new=AsyncMock(return_value="Aetna"),
    ):
        await DenialCreatorHelper.extract_set_insurance_company(denial.denial_id)

    row = (
        await Denial.objects.filter(denial_id=denial.denial_id)
        .values("insurance_company", "insurance_company_obj_id", "appeal_fax_number")
        .afirst()
    )
    assert row["insurance_company"] == "Cigna"
    assert row["insurance_company_obj_id"] is None
    assert not row["appeal_fax_number"], "Aetna's fax landed on a Cigna case"


@pytest.mark.django_db(transaction=True)
async def test_the_regex_plan_matcher_honours_the_insurer_the_person_named() -> None:
    """No structured carrier yet, Cigna typed in the box, and the letter
    matches an Aetna plan. The box wins: nothing structured is stored."""
    await _carrier_row("Cigna", "555-0001")
    other = await _carrier_row("Aetna", "555-0002")
    await _plan_of(other, "Aetna PPO", "555-0003", regex="Aetna PPO")
    denial = await Denial.objects.acreate(
        denial_text="Your Aetna PPO claim was denied.",
        hashed_email="x",
        insurance_company="Cigna",
    )

    matched = await DenialCreatorHelper.match_insurance_plan_from_regex(
        denial.denial_id
    )

    row = (
        await Denial.objects.filter(denial_id=denial.denial_id)
        .values(
            "insurance_plan_obj_id", "insurance_company_obj_id", "insurance_company"
        )
        .afirst()
    )
    assert matched is None
    assert row["insurance_plan_obj_id"] is None
    assert row["insurance_company_obj_id"] is None
    assert row["insurance_company"] == "Cigna"


@pytest.mark.django_db(transaction=True)
async def test_a_text_correction_after_the_match_leaves_the_fax_empty() -> None:
    """The structured carrier says Aetna from an earlier match; the person
    then corrects the insurer box to Cigna. Aetna's fax is not theirs."""
    other = await _carrier("Aetna", "555-0002")
    denial = await Denial.objects.acreate(
        denial_text="a denial",
        hashed_email="x",
        insurance_company="Cigna",
        insurance_company_obj=other,
    )

    fax = await DenialCreatorHelper._fax_for_the_carrier_on_the_row(denial.denial_id)

    assert fax is None


@pytest.mark.django_db(transaction=True)
async def test_a_plan_of_another_carrier_supplies_no_fax_when_no_carrier_is_stored() -> (
    None
):
    """Typed Cigna, no structured carrier, an Aetna plan left on the row from
    an earlier match. The plan's carrier is checked against the typed name."""
    other = await _carrier_row("Aetna", "555-0002")
    other_plan = await _plan_of(other, "Aetna PPO", "555-0003")
    denial = await Denial.objects.acreate(
        denial_text="a denial",
        hashed_email="x",
        insurance_company="Cigna",
        insurance_plan_obj=other_plan,
    )

    fax = await DenialCreatorHelper._fax_for_the_carrier_on_the_row(denial.denial_id)

    assert fax is None


@pytest.mark.django_db(transaction=True)
async def test_an_unreachable_reader_does_not_stop_the_carrier_regex() -> None:
    """The model could not be asked, but a configured carrier's regex
    matches the letter: the carrier is stored and the step is found."""
    from fighthealthinsurance.generate_appeal import ExtractionUnavailable

    aetna = await _carrier_row("Aetna", "555-0002")
    denial = await Denial.objects.acreate(
        denial_text="Aetna has denied your claim.", hashed_email="x"
    )
    with patch(
        "fighthealthinsurance.common_view_logic.appealGenerator.get_insurance_company",
        new=AsyncMock(side_effect=ExtractionUnavailable("down")),
    ):
        outcome = await DenialCreatorHelper.extract_set_insurance_company(
            denial.denial_id
        )

    row = (
        await Denial.objects.filter(denial_id=denial.denial_id)
        .values("insurance_company_obj_id")
        .afirst()
    )
    assert outcome == "found", outcome
    assert row["insurance_company_obj_id"] == aetna.id


@pytest.mark.django_db(transaction=True)
async def test_an_unreachable_reader_with_no_regex_match_is_failed_not_absent() -> None:
    from fighthealthinsurance.generate_appeal import ExtractionUnavailable

    denial = await Denial.objects.acreate(
        denial_text="a denial naming nobody", hashed_email="x"
    )
    with patch(
        "fighthealthinsurance.common_view_logic.appealGenerator.get_insurance_company",
        new=AsyncMock(side_effect=ExtractionUnavailable("down")),
    ):
        outcome = await DenialCreatorHelper.extract_set_insurance_company(
            denial.denial_id
        )

    assert outcome == "failed", outcome


@pytest.mark.django_db(transaction=True)
async def test_a_fax_read_for_one_carrier_is_not_written_after_a_correction() -> None:
    """The read chose Aetna's number; the person corrected the insurer to
    Cigna before the write. The write carries the carrier it was read for,
    and finds the row no longer holds it."""
    aetna = await _carrier_row("Aetna", "555-0002")
    cigna = await _carrier_row("Cigna", "555-0001")
    denial = await Denial.objects.acreate(
        denial_text="a denial", hashed_email="x", insurance_company_obj=aetna
    )
    fax, justified_by = await DenialCreatorHelper._fax_and_its_justification(
        denial.denial_id
    )
    assert fax == "555-0002"
    await Denial.objects.filter(denial_id=denial.denial_id).aupdate(
        insurance_company_obj=cigna, insurance_company="Cigna"
    )

    written = await DenialCreatorHelper._write_fax_if_the_carrier_still_holds(
        denial.denial_id, fax, justified_by
    )

    stored = (
        await Denial.objects.filter(denial_id=denial.denial_id)
        .values_list("appeal_fax_number", flat=True)
        .afirst()
    )
    assert written is False
    assert not stored, f"Aetna's number landed on a Cigna case: {stored}"


@pytest.mark.django_db(transaction=True)
async def test_a_plan_checked_against_one_carrier_is_not_stored_after_a_correction() -> (
    None
):
    """The check read Aetna; the person corrected the insurer to Cigna before
    the write. The write carries the carrier it was checked against."""
    aetna = await _carrier_row("Aetna", "555-0002")
    cigna = await _carrier_row("Cigna", "555-0001")
    aetna_plan = await _plan_of(aetna, "Aetna PPO", "555-0003")
    denial = await Denial.objects.acreate(
        denial_text="a denial", hashed_email="x", insurance_company_obj=aetna
    )
    checked_against = (aetna.id, None)
    await Denial.objects.filter(denial_id=denial.denial_id).aupdate(
        insurance_company_obj=cigna, insurance_company="Cigna"
    )

    stored = await DenialCreatorHelper._write_plan_if_the_carrier_still_holds(
        denial.denial_id, aetna_plan, *checked_against
    )

    row = (
        await Denial.objects.filter(denial_id=denial.denial_id)
        .values("insurance_plan_obj_id", "insurance_company_obj_id")
        .afirst()
    )
    assert stored is False
    assert row["insurance_plan_obj_id"] is None
    assert row["insurance_company_obj_id"] == cigna.id
