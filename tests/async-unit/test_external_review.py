import datetime

from django.test import TestCase
from django.urls import reverse

from fighthealthinsurance.external_review import (
    CONFIDENCE_NOT,
    CONFIDENCE_POSSIBLE,
    CONFIDENCE_UNKNOWN,
    detect_external_review_eligibility,
    generate_external_review_packet,
    get_state_config,
    schedule_external_review_followups,
)
from fighthealthinsurance.models import Denial, FollowUpSched


class ExternalReviewTests(TestCase):
    def setUp(self):
        self.denial = Denial.objects.create(
            hashed_email=Denial.get_hashed_email("a@b.com"),
            denial_text="denied",
            state="CA",
            denial_type_text="medical necessity",
        )

    def test_urgent_case_has_expedited_copy(self):
        packet = generate_external_review_packet(
            self.denial,
            {
                "urgent": True,
                "plan_type": "ACA marketplace",
                "appeal_denial_date": datetime.date.today(),
            },
        )
        self.assertIn("expedited", " ".join(packet["warnings"]).lower())

    def test_urgent_false_string_does_not_enable_urgent(self):
        packet = generate_external_review_packet(
            self.denial,
            {
                "urgent": "false",
                "plan_type": "ACA marketplace",
                "appeal_denial_date": datetime.date.today(),
            },
        )
        self.assertNotIn(
            "Urgent medical risk", " ".join(packet["eligibility"]["rationale"])
        )

    def test_missing_state_falls_back(self):
        cfg = get_state_config(None)
        self.assertEqual(cfg["state"], "FEDERAL")

    def test_new_seed_state_configured(self):
        cfg = get_state_config("WA")
        self.assertEqual(cfg["state"], "WA")
        self.assertIn("Washington Office of the", cfg["regulator_name"])

    def test_reminders_are_scheduled(self):
        schedule_external_review_followups(
            self.denial,
            "a@b.com",
            datetime.date.today() + datetime.timedelta(days=90),
            today=datetime.date(2026, 5, 2),
        )
        self.assertEqual(FollowUpSched.objects.filter(denial_id=self.denial).count(), 3)

    def test_bad_email_skips_reminders(self):
        schedule_external_review_followups(
            self.denial,
            "not-an-email",
            datetime.date.today() + datetime.timedelta(days=90),
            today=datetime.date(2026, 5, 2),
        )
        self.assertEqual(FollowUpSched.objects.filter(denial_id=self.denial).count(), 0)

    def test_plan_type_uncertainty(self):
        result = detect_external_review_eligibility(
            state="CA",
            plan_type="",
            denial_type="",
            denial_date=None,
            appeal_denial_date=None,
            urgent=False,
        )
        self.assertEqual(result.confidence, CONFIDENCE_UNKNOWN)

    def test_medicare_advantage_warning(self):
        result = detect_external_review_eligibility(
            state="CA",
            plan_type="Medicare Advantage",
            denial_type="medical necessity",
            denial_date=None,
            appeal_denial_date=datetime.date.today(),
            urgent=False,
        )
        self.assertEqual(result.confidence, CONFIDENCE_UNKNOWN)
        self.assertIn("Medicare Advantage", " ".join(result.rationale))

    def test_erisa_routes_with_warning(self):
        result = detect_external_review_eligibility(
            state="CA",
            plan_type="Employer ERISA self-funded",
            denial_type="medical necessity",
            denial_date=None,
            appeal_denial_date=datetime.date.today(),
            urgent=False,
        )
        self.assertEqual(result.confidence, CONFIDENCE_POSSIBLE)
        self.assertEqual(result.routing, "erisa_federal_path")

    def test_limited_benefit_not_eligible(self):
        result = detect_external_review_eligibility(
            state="CA",
            plan_type="short-term",
            denial_type="medical necessity",
            denial_date=None,
            appeal_denial_date=datetime.date.today(),
            urgent=False,
        )
        self.assertEqual(result.confidence, CONFIDENCE_NOT)

    def test_internal_appeal_denied_triggers_external_review_cta(self):
        response = self.client.post(
            reverse("external_review_wizard"),
            {
                "denial_id": self.denial.denial_id,
                "email": "a@b.com",
                "semi_sekret": self.denial.semi_sekret,
                "appeal_denial_date": str(datetime.date.today()),
                "plan_type": "ACA marketplace",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("wizard", response.json())

    def test_wizard_requires_secret(self):
        response = self.client.post(
            reverse("external_review_wizard"),
            {
                "denial_id": self.denial.denial_id,
                "email": "a@b.com",
            },
        )
        self.assertEqual(response.status_code, 400)
