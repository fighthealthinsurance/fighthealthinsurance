"""DenialCreatorHelper.extract_set_triage: the gate, the write, idempotence."""

import datetime
from unittest.mock import AsyncMock, patch

import pytest
from asgiref.sync import async_to_sync
from django.test import override_settings

from fighthealthinsurance.common_view_logic import DenialCreatorHelper
from fighthealthinsurance.ml import denial_triage as dt
from fighthealthinsurance.models import Denial

ENABLED = dict(TYPESAFE_API_KEY="test-key", TYPESAFE_DENIAL_TRIAGE_ENABLED=True)
TEXT = "Denied as not medically necessary. Appeal within 180 days of this notice."


def _denial(use_external=True, **fields):
    return Denial.objects.create(
        hashed_email="h", denial_text=TEXT, use_external=use_external,
        denial_date=datetime.date(2026, 9, 2), **fields,
    )


def _result():
    return dt.parse(
        {
            "answers": {
                "category": {"choice": "medical_necessity", "confidence": 0.9},
                "regulation": {"choice": "unknown", "confidence": 0.4},
                "pre_service": {"noul": 0.1},
                "urgent": {"noul": 0.02},
                "deadline": {"choice": "180 days from notice", "confidence": 0.95},
            }
        },
        dt.date_candidates(TEXT, datetime.date(2026, 9, 2)),
    )


@pytest.mark.django_db
def test_triage_is_written_to_the_denial_with_its_text_hash():
    denial = _denial()
    with override_settings(**ENABLED), patch.object(
        dt, "triage", new=AsyncMock(return_value=_result())
    ) as triage:
        async_to_sync(DenialCreatorHelper.extract_set_triage)(denial.denial_id)
        triage.assert_awaited_once_with(TEXT, datetime.date(2026, 9, 2))
    denial.refresh_from_db()
    assert denial.triage_category == "medical_necessity"
    assert denial.appeal_deadline == datetime.date(2027, 3, 1)
    assert denial.appeal_deadline_label == "180 days from notice"
    assert denial.appeal_deadline_confidence == pytest.approx(0.95)
    assert denial.triage_text_hash == dt.text_hash(TEXT)
    assert denial.triaged_at is not None
    assert dt.is_current(denial)


@pytest.mark.django_db
def test_a_result_for_a_replaced_letter_updates_nothing():
    denial = _denial()

    async def replace_then_answer(text, date):
        await Denial.objects.filter(pk=denial.pk).aupdate(denial_text="a different letter")
        return _result()

    with override_settings(**ENABLED), patch.object(dt, "triage", side_effect=replace_then_answer):
        async_to_sync(DenialCreatorHelper.extract_set_triage)(denial.denial_id)
    denial.refresh_from_db()
    assert denial.triage_category is None and denial.triaged_at is None


@pytest.mark.django_db
def test_a_result_after_consent_was_withdrawn_updates_nothing():
    denial = _denial()

    async def withdraw_then_answer(text, date):
        await Denial.objects.filter(pk=denial.pk).aupdate(use_external=False)
        return _result()

    with override_settings(**ENABLED), patch.object(dt, "triage", side_effect=withdraw_then_answer):
        async_to_sync(DenialCreatorHelper.extract_set_triage)(denial.denial_id)
    denial.refresh_from_db()
    assert denial.triaged_at is None


@pytest.mark.django_db
def test_a_date_confirmed_during_the_call_resolves_the_window_at_write_time():
    denial = Denial.objects.create(hashed_email="h", denial_text=TEXT, use_external=True, denial_date=None)

    async def confirm_then_answer(text, date):
        assert date is None
        await Denial.objects.filter(pk=denial.pk).aupdate(denial_date=datetime.date(2026, 9, 2))
        return dt.parse(
            {
                "answers": {
                    "category": {"choice": "medical_necessity", "confidence": 0.9},
                    "regulation": {"choice": "unknown", "confidence": 0.4},
                    "pre_service": {"noul": 0.1},
                    "urgent": {"noul": 0.02},
                    "deadline": {"choice": "180 days from notice", "confidence": 0.95},
                }
            },
            dt.date_candidates(TEXT, None),
        )

    with override_settings(**ENABLED), patch.object(dt, "triage", side_effect=confirm_then_answer):
        async_to_sync(DenialCreatorHelper.extract_set_triage)(denial.denial_id)
    denial.refresh_from_db()
    assert denial.appeal_deadline == datetime.date(2027, 3, 1)


@pytest.mark.django_db
def test_a_date_corrected_during_the_call_wins_over_the_date_the_call_started_with():
    denial = _denial()  # denial_date 2026-09-02

    async def correct_then_answer(text, date):
        assert date == datetime.date(2026, 9, 2)
        await Denial.objects.filter(pk=denial.pk).aupdate(denial_date=datetime.date(2026, 9, 12))
        return _result()  # resolved March 1 against the OLD date

    with override_settings(**ENABLED), patch.object(dt, "triage", side_effect=correct_then_answer):
        async_to_sync(DenialCreatorHelper.extract_set_triage)(denial.denial_id)
    denial.refresh_from_db()
    assert denial.appeal_deadline == datetime.date(2027, 3, 11)


@pytest.mark.django_db
def test_exhausted_extraction_attempts_still_retry_triage():
    denial = _denial(extract_attempts=3)
    with override_settings(**ENABLED), patch.object(
        DenialCreatorHelper, "extract_set_triage", new=AsyncMock()
    ) as triage, patch.object(DenialCreatorHelper, "extract_set_regulator", new=AsyncMock()):
        async def drain():
            async for _ in DenialCreatorHelper.extract_entity(denial.denial_id):
                pass

        async_to_sync(drain)()
        triage.assert_awaited_once_with(denial.denial_id)


@pytest.mark.django_db
def test_a_reconnect_after_extraction_finished_retries_triage():
    denial = _denial(procedure="MRI", diagnosis="back pain")
    with override_settings(**ENABLED), patch.object(
        DenialCreatorHelper, "extract_set_triage", new=AsyncMock()
    ) as triage, patch.object(DenialCreatorHelper, "extract_set_regulator", new=AsyncMock()):
        async def drain():
            async for _ in DenialCreatorHelper.extract_entity(denial.denial_id):
                pass

        async_to_sync(drain)()
        triage.assert_awaited_once_with(denial.denial_id)


@pytest.mark.django_db
def test_an_older_rubric_is_redone_even_for_the_same_text():
    denial = _denial(
        triage_text_hash=dt.text_hash(TEXT), triage_category="other",
        triage_source="typesafe/speed_latest/rubric-0",
    )
    with override_settings(**ENABLED), patch.object(dt, "triage", new=AsyncMock(return_value=_result())) as triage:
        async_to_sync(DenialCreatorHelper.extract_set_triage)(denial.denial_id)
        triage.assert_awaited_once()
    denial.refresh_from_db()
    assert denial.triage_source == dt.SOURCE


@pytest.mark.django_db
def test_already_triaged_for_this_text_is_not_asked_again():
    denial = _denial(triage_text_hash=dt.text_hash(TEXT), triage_category="other", triage_source=dt.SOURCE)
    with override_settings(**ENABLED), patch.object(dt, "triage", new=AsyncMock()) as triage:
        async_to_sync(DenialCreatorHelper.extract_set_triage)(denial.denial_id)
        triage.assert_not_awaited()


@pytest.mark.django_db
def test_a_stale_triage_from_another_text_is_redone():
    denial = _denial(triage_text_hash=dt.text_hash("an older letter"), triage_category="other")
    with override_settings(**ENABLED), patch.object(dt, "triage", new=AsyncMock(return_value=_result())) as triage:
        async_to_sync(DenialCreatorHelper.extract_set_triage)(denial.denial_id)
        triage.assert_awaited_once()
    denial.refresh_from_db()
    assert denial.triage_category == "medical_necessity"


@pytest.mark.django_db
def test_off_by_default_never_reads_the_denial():
    denial = _denial()
    with patch.object(dt, "triage", new=AsyncMock()) as triage:
        async_to_sync(DenialCreatorHelper.extract_set_triage)(denial.denial_id)
        triage.assert_not_awaited()


@pytest.mark.django_db
def test_no_external_consent_means_no_triage():
    denial = _denial(use_external=False)
    with override_settings(**ENABLED), patch.object(dt, "triage", new=AsyncMock()) as triage:
        async_to_sync(DenialCreatorHelper.extract_set_triage)(denial.denial_id)
        triage.assert_not_awaited()
    denial.refresh_from_db()
    assert denial.triaged_at is None


@pytest.mark.django_db
def test_no_result_leaves_the_denial_untouched():
    denial = _denial()
    with override_settings(**ENABLED), patch.object(dt, "triage", new=AsyncMock(return_value=None)):
        async_to_sync(DenialCreatorHelper.extract_set_triage)(denial.denial_id)
    denial.refresh_from_db()
    assert denial.triage_category is None and denial.triaged_at is None
