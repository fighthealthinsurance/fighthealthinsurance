"""The triage's life after it is written: a window resolves when the denial
date arrives, and the whole thing is cleared when the letter is replaced."""

import datetime
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.test import TestCase, override_settings

from fighthealthinsurance.common_view_logic import (
    DenialCreatorHelper,
    FindNextStepsHelper,
)
from fighthealthinsurance.ml import denial_triage as dt
from fighthealthinsurance.models import Denial

TEXT = "Denied. Appeal within 180 days of this notice."


def _is_triage_refresh(kwargs) -> bool:
    """The review step refreshes the triage columns by name; its other
    refreshes (after generating questions) name other fields or none."""
    return "appeal_deadline_label" in (kwargs.get("fields") or [])


class TriageLifecycleTest(TestCase):
    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def _triaged(self, **overrides):
        values = dict(
            hashed_email=Denial.get_hashed_email("life@example.com"),
            denial_text=TEXT,
            semi_sekret="sekret",
            triage_category="medical_necessity",
            triage_category_confidence=0.9,
            appeal_deadline=None,
            appeal_deadline_label="180 days from notice",
            appeal_deadline_confidence=0.9,
            triage_source=dt.SOURCE,
            triage_text_hash=dt.text_hash(TEXT),
        )
        values.update(overrides)
        return Denial.objects.create(**values)

    def test_the_window_resolves_once_the_denial_date_is_confirmed(self):
        denial = self._triaged()
        FindNextStepsHelper.find_next_steps(
            denial_id=denial.denial_id,
            email="life@example.com",
            semi_sekret="sekret",
            procedure="MRI",
            diagnosis="back pain",
            insurance_company=None,
            plan_id=None,
            claim_id=None,
            denial_type=None,
            denial_date=datetime.date(2026, 9, 2),
        )
        denial.refresh_from_db()
        self.assertEqual(denial.appeal_deadline, datetime.date(2027, 3, 1))

    def test_a_corrected_denial_date_moves_the_deadline_with_it(self):
        denial = self._triaged(appeal_deadline=datetime.date(2027, 3, 1))
        FindNextStepsHelper.find_next_steps(
            denial_id=denial.denial_id,
            email="life@example.com",
            semi_sekret="sekret",
            procedure="MRI",
            diagnosis="back pain",
            insurance_company=None,
            plan_id=None,
            claim_id=None,
            denial_type=None,
            denial_date=datetime.date(2026, 9, 12),
        )
        denial.refresh_from_db()
        self.assertEqual(denial.appeal_deadline, datetime.date(2027, 3, 11))

    def test_a_triage_landing_during_the_date_correction_anchors_to_the_new_date(self):
        """The race (review): the review step holds the corrected date in
        memory, its refresh finds no triage yet, and THEN the triage lands.
        Run the real triage hook from inside that refresh; the deadline it
        writes must be anchored to the corrected date, not the old one."""
        text = "Denied as not medically necessary. Appeal within 180 days of this notice."
        denial = Denial.objects.create(
            hashed_email=Denial.get_hashed_email("life@example.com"),
            denial_text=text,
            semi_sekret="sekret",
            denial_date=datetime.date(2026, 9, 2),
        )
        result = dt.parse(
            {
                "answers": {
                    "category": {"choice": "medical_necessity", "confidence": 0.9},
                    "regulation": {"choice": "unknown", "confidence": 0.4},
                    "pre_service": {"noul": 0.1},
                    "urgent": {"noul": 0.02},
                    "deadline": {"choice": "180 days from notice", "confidence": 0.95},
                }
            },
            dt.date_candidates(text, datetime.date(2026, 9, 2)),
        )
        real_refresh = Denial.refresh_from_db
        landed = []

        def refresh_then_triage(instance, *args, **kwargs):
            real_refresh(instance, *args, **kwargs)
            if landed or not _is_triage_refresh(kwargs):
                return
            landed.append(True)
            with override_settings(
                TYPESAFE_API_KEY="test-key", TYPESAFE_DENIAL_TRIAGE_ENABLED=True
            ), patch.object(dt, "triage", new=AsyncMock(return_value=result)):
                async_to_sync(DenialCreatorHelper.extract_set_triage)(instance.denial_id)

        with patch.object(Denial, "refresh_from_db", refresh_then_triage):
            FindNextStepsHelper.find_next_steps(
                denial_id=denial.denial_id,
                email="life@example.com",
                semi_sekret="sekret",
                procedure="MRI",
                diagnosis="back pain",
                insurance_company=None,
                plan_id=None,
                claim_id=None,
                denial_type=None,
                denial_date=datetime.date(2026, 9, 12),
            )
        self.assertTrue(landed, "the triage never ran inside the refresh")
        denial.refresh_from_db()
        self.assertEqual(denial.denial_date, datetime.date(2026, 9, 12))
        self.assertEqual(denial.appeal_deadline_label, "180 days from notice")
        self.assertEqual(denial.appeal_deadline, datetime.date(2027, 3, 11))

    def test_overlapping_submissions_cannot_restore_an_old_date_under_a_new_deadline(self):
        """Two review submissions overlap: A confirms September 12, B corrects
        to September 22 while A is still running, the triage lands after both
        refreshes, and A finishes last. The row must hold B's date with the
        deadline anchored to it; A's stale copy of the date must not win
        (review)."""
        text = "Denied as not medically necessary. Appeal within 180 days of this notice."
        denial = Denial.objects.create(
            hashed_email=Denial.get_hashed_email("life@example.com"),
            denial_text=text,
            semi_sekret="sekret",
            denial_date=datetime.date(2026, 9, 12),
        )
        result = dt.parse(
            {
                "answers": {
                    "category": {"choice": "medical_necessity", "confidence": 0.9},
                    "regulation": {"choice": "unknown", "confidence": 0.4},
                    "pre_service": {"noul": 0.1},
                    "urgent": {"noul": 0.02},
                    "deadline": {"choice": "180 days from notice", "confidence": 0.95},
                }
            },
            dt.date_candidates(text, datetime.date(2026, 9, 12)),
        )

        def submit(date):
            FindNextStepsHelper.find_next_steps(
                denial_id=denial.denial_id,
                email="life@example.com",
                semi_sekret="sekret",
                procedure="MRI",
                diagnosis="back pain",
                insurance_company=None,
                plan_id=None,
                claim_id=None,
                denial_type=None,
                denial_date=date,
            )

        real_refresh = Denial.refresh_from_db
        refreshes = []

        def refresh_then(instance, *args, **kwargs):
            real_refresh(instance, *args, **kwargs)
            if not _is_triage_refresh(kwargs):
                return
            refreshes.append(instance.denial_date)
            if len(refreshes) == 1:
                # Inside A's refresh: B runs start to finish.
                submit(datetime.date(2026, 9, 22))
            elif len(refreshes) == 2:
                # Inside B's refresh: the triage lands.
                with override_settings(
                    TYPESAFE_API_KEY="test-key", TYPESAFE_DENIAL_TRIAGE_ENABLED=True
                ), patch.object(dt, "triage", new=AsyncMock(return_value=result)):
                    async_to_sync(DenialCreatorHelper.extract_set_triage)(
                        instance.denial_id
                    )

        with patch.object(Denial, "refresh_from_db", refresh_then):
            submit(datetime.date(2026, 9, 12))
        self.assertEqual(len(refreshes), 2)
        denial.refresh_from_db()
        self.assertEqual(denial.denial_date, datetime.date(2026, 9, 22))
        self.assertEqual(denial.appeal_deadline, datetime.date(2027, 3, 21))

    def test_a_stale_triage_is_not_resolved_against_a_new_date(self):
        denial = self._triaged(triage_text_hash=dt.text_hash("some other letter"))
        FindNextStepsHelper.find_next_steps(
            denial_id=denial.denial_id,
            email="life@example.com",
            semi_sekret="sekret",
            procedure="MRI",
            diagnosis="back pain",
            insurance_company=None,
            plan_id=None,
            claim_id=None,
            denial_type=None,
            denial_date=datetime.date(2026, 9, 2),
        )
        denial.refresh_from_db()
        self.assertIsNone(denial.appeal_deadline)

    def test_admin_columns_are_blank_for_a_stale_triage(self):
        from fighthealthinsurance.admin import DenialAdmin

        current = self._triaged(appeal_deadline=datetime.date(2027, 3, 1))
        stale = self._triaged(
            appeal_deadline=datetime.date(2027, 3, 1),
            triage_text_hash=dt.text_hash("letter A"),
            hashed_email="other",
        )
        admin = DenialAdmin(Denial, None)
        self.assertEqual(admin.triage_category_current(current), "medical_necessity")
        self.assertEqual(
            admin.appeal_deadline_current(current),
            "2027-03-01 (180 days from notice, conf 0.90)",
        )
        self.assertEqual(admin.triage_category_current(stale), "")
        self.assertEqual(admin.appeal_deadline_current(stale), "")

    def test_the_change_form_never_shows_raw_triage_fields_and_the_summary_is_gated(self):
        from django.contrib.auth import get_user_model
        from django.test import RequestFactory

        from fighthealthinsurance.admin import DenialAdmin

        current = self._triaged(appeal_deadline=datetime.date(2027, 3, 1))
        stale = self._triaged(
            appeal_deadline=datetime.date(2027, 3, 1),
            triage_text_hash=dt.text_hash("letter A"),
            hashed_email="other",
        )
        from django.contrib import admin as django_admin

        admin = django_admin.site._registry[Denial]
        self.assertIsInstance(admin, DenialAdmin)
        request = RequestFactory().get("/")
        request.user = get_user_model().objects.create_superuser("root", "r@example.com", "pw")
        form_fields = set(admin.get_form(request, current)().fields)
        for column in dt.TRIAGE_COLUMNS:
            self.assertNotIn(column, form_fields, column)
        self.assertIn("triage_summary", admin.get_fields(request, current))
        self.assertIn("category=medical_necessity", admin.triage_summary(current))
        self.assertIn("deadline=2027-03-01", admin.triage_summary(current))
        self.assertEqual(admin.triage_summary(stale), "")

    def test_editing_the_denial_date_in_the_admin_moves_an_anchored_deadline(self):
        from django.contrib import admin as django_admin
        from django.contrib.auth import get_user_model
        from django.test import RequestFactory

        denial = self._triaged(
            appeal_deadline=datetime.date(2027, 3, 1), denial_date=datetime.date(2026, 9, 2)
        )
        admin = django_admin.site._registry[Denial]
        request = RequestFactory().post("/")
        request.user = get_user_model().objects.create_superuser("root2", "r2@example.com", "pw")
        denial.denial_date = datetime.date(2026, 9, 12)
        admin.save_model(request, denial, form=None, change=True)
        denial.refresh_from_db()
        self.assertEqual(denial.appeal_deadline, datetime.date(2027, 3, 11))

    def test_an_unanchored_window_never_resolves(self):
        denial = self._triaged(appeal_deadline_label="30 days")
        FindNextStepsHelper.find_next_steps(
            denial_id=denial.denial_id,
            email="life@example.com",
            semi_sekret="sekret",
            procedure="MRI",
            diagnosis="back pain",
            insurance_company=None,
            plan_id=None,
            claim_id=None,
            denial_type=None,
            denial_date=datetime.date(2026, 9, 2),
        )
        denial.refresh_from_db()
        self.assertIsNone(denial.appeal_deadline)

    def test_replacing_the_letter_clears_every_triage_column(self):
        denial = self._triaged(appeal_deadline=datetime.date(2027, 3, 1))
        DenialCreatorHelper._invalidate_denial_text_artifacts(denial)
        denial.refresh_from_db()
        for column in dt.TRIAGE_COLUMNS:
            self.assertIsNone(getattr(denial, column), column)
