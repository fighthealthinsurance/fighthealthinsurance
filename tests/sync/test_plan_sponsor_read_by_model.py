"""Our own model reads the company behind a shared plan document.

The fixed patterns misread wording they weren't written for ("plan sponsored
by Google, any amount paid ..." once came out as a company named "ed by
Google, any amount paid ..."). So the reading asks our internal model first
and checks what it says against the document: the line it quotes and the
name must be in the text, the name must be a company and not an insurer, and
an EIN must be the document's. A company it finds counts at once; staff can
confirm or fix it in the admin, and nothing waits for them. When no model
gives a usable answer, the fixed patterns read it as before.
"""

import json
import uuid
from contextlib import contextmanager
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.utils import timezone

from fighthealthinsurance.admin import PolicyDocumentAdmin
from fighthealthinsurance.models import InsuranceCompany, PlanSponsor, PolicyDocument
from fighthealthinsurance.plan_documents_processing import afind_company, alink_company
from fighthealthinsurance.plan_sponsor_reader import (
    MAX_EXCERPT_CHARS,
    aread_sponsor,
    excerpt,
)
from fighthealthinsurance.plan_sponsors import identify_sponsor

BOOKLET = (
    "Your Benefit Booklet\nAnthem Blue Cross PPO\n"
    + ("Covered services and what the plan pays. " * 40)
    + "\nThis plan is sponsored by Google LLC for its eligible employees.\n"
    "Employer Identification Number: 77-0493581\n"
    "If you are enrolled in a plan sponsored by Google, any amount paid during "
    "the same calendar year toward your calendar year deductible counts.\n"
    + ("Deductibles, copays and coinsurance for each service. " * 40)
)


def answer(sponsor, evidence, ein=None):
    return json.dumps({"sponsor": sponsor, "ein": ein, "evidence": evidence})


@contextmanager
def model_answers(*answers):
    """Our internal models, faked: each answer in turn, as the real helper
    tries one model after another until one passes the check."""

    async def fake(system_prompts, prompt, validator=None, **kwargs):
        for a in answers:
            if validator is None or validator(a):
                return a
        return None

    with patch("fighthealthinsurance.ml.ml_inference.infer_with_fallback", fake):
        yield


def read(text):
    return async_to_sync(aread_sponsor)(text)


class TheModelsAnswerIsCheckedTest(TestCase):
    def test_a_sponsor_quoted_from_the_document_is_taken(self):
        with model_answers(
            answer("Google LLC", "This plan is sponsored by Google LLC for its eligible employees.", "77-0493581")
        ):
            sponsor, answered = read(BOOKLET)
        self.assertEqual((sponsor.name, sponsor.ein, sponsor.source, answered), ("Google LLC", "77-0493581", "model", True))

    def test_the_quoted_line_is_kept_as_evidence(self):
        with model_answers(answer("Google LLC", "This plan is sponsored by Google LLC for its eligible employees.")):
            sponsor, _ = read(BOOKLET)
        self.assertIn("sponsored by Google LLC", sponsor.evidence)

    def test_a_line_that_is_not_in_the_document_is_refused(self):
        with model_answers(answer("Google LLC", "Google LLC is the plan sponsor and administrator.")):
            sponsor, answered = read(BOOKLET)
        self.assertEqual((sponsor, answered), (None, False))

    def test_a_name_that_is_not_in_the_document_is_refused(self):
        with model_answers(answer("Alphabet Inc.", "This plan is sponsored by Google LLC for its eligible employees.")):
            sponsor, _ = read(BOOKLET)
        self.assertIsNone(sponsor)

    def test_an_insurer_given_as_the_sponsor_is_refused(self):
        InsuranceCompany.objects.create(name="Anthem Blue Cross")
        with model_answers(answer("Anthem Blue Cross", "Anthem Blue Cross PPO")):
            sponsor, _ = read(BOOKLET)
        self.assertIsNone(sponsor)

    def test_an_ein_the_document_does_not_give_is_dropped_and_the_company_kept(self):
        with model_answers(
            answer("Google LLC", "This plan is sponsored by Google LLC for its eligible employees.", "12-3456789")
        ):
            sponsor, _ = read(BOOKLET)
        self.assertEqual((sponsor.name, sponsor.ein), ("Google LLC", ""))

    def test_the_plans_own_name_is_cut_to_the_company(self):
        text = "Crumpler Cooperative Welfare Benefit Plan\nSummary Plan Description\n" + ("Covered services. " * 30)
        with model_answers(answer("Crumpler Cooperative Welfare Benefit Plan", "Crumpler Cooperative Welfare Benefit Plan")):
            sponsor, _ = read(text)
        self.assertEqual(sponsor.name, "Crumpler Cooperative")

    def test_a_document_the_model_says_names_no_sponsor_has_none(self):
        with model_answers(json.dumps({"sponsor": None, "ein": None, "evidence": None})):
            sponsor, answered = read(BOOKLET)
        self.assertEqual((sponsor, answered), (None, True))

    def test_a_refused_answer_lets_the_next_model_answer(self):
        with model_answers(
            answer("Alphabet Inc.", "made up"),
            answer("Google LLC", "This plan is sponsored by Google LLC for its eligible employees."),
        ):
            sponsor, _ = read(BOOKLET)
        self.assertEqual(sponsor.name, "Google LLC")

    def test_an_answer_that_is_not_json_is_refused(self):
        with model_answers("The sponsor is Google LLC."):
            sponsor, answered = read(BOOKLET)
        self.assertEqual((sponsor, answered), (None, False))


class AModelThatTakesTooLongTest(TestCase):
    def test_the_fixed_patterns_read_it_instead(self):
        import asyncio

        async def slow(*args, **kwargs):
            await asyncio.sleep(5)

        text = "Plan Sponsor: Crumpler Cooperative, LLC\nThis plan is governed by ERISA.\n" + ("Covered. " * 30)
        with patch("fighthealthinsurance.ml.ml_inference.infer_with_fallback", slow), patch(
            "fighthealthinsurance.plan_sponsor_reader.TOTAL_TIMEOUT_SECONDS", 0.1
        ):
            sponsor = async_to_sync(afind_company)(text)
        self.assertEqual(sponsor.source, "rules")


class TheExcerptTest(TestCase):
    def test_a_long_document_is_cut_to_the_budget(self):
        self.assertLessEqual(len(excerpt(BOOKLET * 10)), MAX_EXCERPT_CHARS + 200)

    def test_the_passage_naming_the_sponsor_is_in_it(self):
        self.assertIn("sponsored by Google LLC", excerpt(BOOKLET * 10))


class TheFixedPatternsTest(TestCase):
    def test_plan_sponsored_by_in_a_sentence_is_not_a_sponsor_label(self):
        text = (
            "Summary Plan Description\nIf you are enrolled in a plan sponsored by Google, "
            "any amount paid during the same calendar year toward your calendar\n" + ("Covered. " * 30)
        )
        self.assertIsNone(identify_sponsor(text))


def shared_row(text):
    return PolicyDocument.objects.create(
        source=PolicyDocument.SOURCE_SHARED,
        share_batch=uuid.uuid4(),
        shared_at=timezone.now(),
        fingerprint=uuid.uuid4().hex,
        processing_status=PolicyDocument.PROCESSING_READY,
        cleaned_text=text,
    )


class TheReadingUsesTheModelFirstTest(TestCase):
    def test_the_models_company_is_linked_with_its_line(self):
        doc = shared_row(BOOKLET)
        with model_answers(answer("Google LLC", "This plan is sponsored by Google LLC for its eligible employees.")):
            async_to_sync(alink_company)(str(doc.pk), BOOKLET)
        doc.refresh_from_db()
        self.assertEqual(
            (doc.plan_sponsor.name, doc.plan_sponsor_source, doc.plan_sponsor_checked_at),
            ("Google LLC", "model", None),
        )

    def test_with_no_model_the_fixed_patterns_read_it(self):
        text = "Plan Sponsor: Crumpler Cooperative, LLC\nThis plan is governed by ERISA.\n" + ("Covered. " * 30)
        with model_answers():
            sponsor = async_to_sync(afind_company)(text)
        self.assertEqual((sponsor.name, sponsor.source), ("Crumpler Cooperative, LLC", "rules"))

    def test_when_the_model_says_none_the_patterns_are_not_asked(self):
        text = "Plan Sponsor: Crumpler Cooperative, LLC\nThis plan is governed by ERISA.\n" + ("Covered. " * 30)
        with model_answers(json.dumps({"sponsor": None, "ein": None, "evidence": None})):
            self.assertIsNone(async_to_sync(afind_company)(text))


class StaffCanCheckWithoutHoldingItUpTest(TestCase):
    def setUp(self):
        self.staff = get_user_model().objects.create_superuser("staff", "s@example.com", "x")
        self.admin = PolicyDocumentAdmin(PolicyDocument, AdminSite())
        self.request = RequestFactory().post("/")
        self.request.user = self.staff
        self.admin.message_user = lambda *a, **k: None
        self.company = PlanSponsor.objects.create(name="Google LLC", normalized_name="google")

    def test_confirming_records_who_checked_it(self):
        doc = shared_row(BOOKLET)
        PolicyDocument.objects.filter(pk=doc.pk).update(plan_sponsor=self.company, plan_sponsor_source="model")
        self.admin.confirm_company(self.request, PolicyDocument.objects.filter(pk=doc.pk))
        doc.refresh_from_db()
        self.assertEqual(doc.plan_sponsor_checked_by, self.staff)

    def test_a_company_staff_pick_is_theirs_and_checked(self):
        doc = shared_row(BOOKLET)

        class Form:
            changed_data = ["plan_sponsor"]

        doc.plan_sponsor = self.company
        self.admin.save_model(self.request, doc, Form(), change=True)
        doc.refresh_from_db()
        self.assertEqual((doc.plan_sponsor_source, doc.plan_sponsor_checked_by), ("staff", self.staff))
