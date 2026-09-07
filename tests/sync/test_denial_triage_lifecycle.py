"""The triage's life after it is written: a window resolves when the denial
date arrives, and the whole thing is cleared when the letter is replaced."""

import datetime

from django.test import TestCase

from fighthealthinsurance.common_view_logic import (
    DenialCreatorHelper,
    FindNextStepsHelper,
)
from fighthealthinsurance.ml import denial_triage as dt
from fighthealthinsurance.models import Denial

TEXT = "Denied. Appeal within 180 days of this notice."


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
