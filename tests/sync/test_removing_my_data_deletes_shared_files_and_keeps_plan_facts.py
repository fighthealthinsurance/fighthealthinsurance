"""Remove-my-data deletes the plan documents a person shared and keeps what
they told us about the plan.

A removal request deletes each file the person shared, everything read from
it (its cleaned text and any analysis), its fingerprint and the scrambled
email that linked it to them. Facts about the plan, such as its insurer,
year and premium, stay on the row with the record of the share, since they
describe the plan, not the person, and the row is marked removed so no
reading writes to it again. Who the plan covers and how often it is paid
stay only with the premium they describe. A row with no fact about the plan
is deleted whole. Other people's shares, shares given with no email, and the
person's own Explain Policy upload are handled as before.
"""

from decimal import Decimal
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.forms.models import model_to_dict
from django.test import TestCase
from django.utils import timezone

from fighthealthinsurance import shared_plan_documents
from fighthealthinsurance.helpers.data_helpers import RemoveDataHelper
from fighthealthinsurance.models import (
    Denial,
    InsuranceCompany,
    InsurancePlan,
    PolicyDocument,
    PolicyDocumentAnalysis,
)
from fighthealthinsurance.shared_plan_documents import save_shared_documents

EMAIL = "Rebecca.Crumpler@example.com"
OTHER_EMAIL = "mary.mahoney@example.com"
CONTENT = b"Summary of Benefits, Crumpler Mutual Gold PPO. Member: Rebecca Lee Crumpler"
CLEANED = "Summary of Benefits, Crumpler Mutual Gold PPO.\nOverall deductible: $1,500"
REMOVED = PolicyDocument.PROCESSING_REMOVED


class _RemovalTestCase(TestCase):
    def setUp(self):
        self.company = InsuranceCompany.objects.create(name="Crumpler Health Mutual")
        self.plan = InsurancePlan.objects.create(
            insurance_company=self.company, plan_name="Gold PPO", state="MA"
        )
        self.storage = PolicyDocument._meta.get_field("document_enc").storage

    def tearDown(self):
        # Rows roll back with the test; their stored files would not.
        for doc in PolicyDocument.objects.all():
            if doc.document_enc:
                doc.document_enc.delete(save=False)

    def all_facts(self):
        return {
            "plan_category": "employer_erisa",
            "plan_year": 2026,
            "state": "MA",
            "insurer_name": "Crumpler Health Mutual",
            "employer_name": "Lee Crumpler Clinic",
            "premium_amount": Decimal("250.00"),
            "premium_period": "monthly",
            "who_is_covered": "family",
            "insurance_company_obj": self.company,
            "insurance_plan_obj": self.plan,
        }

    def share(self, details=None, email=EMAIL, source=PolicyDocument.SOURCE_SHARED):
        """One shared file, read and cleaned, as its reading would leave it."""
        (doc,) = save_shared_documents(
            [SimpleUploadedFile("Rebecca Lee Crumpler SBC.txt", CONTENT)],
            source=source,
            details=self.all_facts() if details is None else details,
            hashed_email=Denial.get_hashed_email(email.lower()) if email else None,
            document_type="summary_of_benefits",
        )
        PolicyDocument.objects.filter(pk=doc.pk).update(
            processing_status=PolicyDocument.PROCESSING_READY,
            processed_at=timezone.now(),
            cleaned_text=CLEANED,
        )
        doc.refresh_from_db()
        return doc

    def remove(self, email=EMAIL):
        with (
            patch.object(self.storage, "delete", wraps=self.storage.delete) as deleted,
            self.captureOnCommitCallbacks(execute=True),
        ):
            RemoveDataHelper.remove_data_for_email(email)
        return deleted

    def after(self, doc):
        return PolicyDocument.objects.filter(pk=doc.pk).first()


class TheFilesAndWhatWasReadFromThemAreDeletedTest(_RemovalTestCase):
    def test_the_stored_file_is_deleted(self):
        doc = self.share()
        name = doc.document_enc.name
        deleted = self.remove()
        deleted.assert_any_call(name)
        self.assertFalse(self.after(doc).document_enc)

    def test_the_cleaned_text_is_deleted_and_the_row_marked_removed(self):
        doc = self.share()
        self.remove()
        kept = self.after(doc)
        self.assertEqual(kept.cleaned_text, "")
        self.assertEqual(kept.processing_status, REMOVED)

    def test_an_analysis_of_the_file_is_deleted(self):
        doc = self.share()
        PolicyDocumentAnalysis.objects.create(
            policy_document=doc, summary="Deductible $1,500 for Rebecca"
        )
        self.remove()
        self.assertFalse(PolicyDocumentAnalysis.objects.exists())

    def test_what_linked_the_file_to_the_person_is_deleted(self):
        doc = self.share()
        self.assertTrue(doc.hashed_email and doc.fingerprint and doc.filename)
        self.remove()
        kept = self.after(doc)
        self.assertIsNone(kept.hashed_email)
        self.assertEqual(kept.fingerprint, "")
        self.assertEqual(kept.filename, "")
        self.assertIsNone(kept.session_key)

    def test_the_kept_row_holds_nothing_from_the_file(self):
        doc = self.share()
        self.remove()
        for name, value in model_to_dict(self.after(doc)).items():
            for gone in ("Rebecca", "deductible", doc.fingerprint):
                with self.subTest(field=name, value=gone):
                    self.assertNotIn(gone, str(value))

    def test_every_way_of_sharing_is_covered(self):
        for source in PolicyDocument.SHARED_SOURCES:
            with self.subTest(source=source):
                doc = self.share(source=source)
                self.remove()
                kept = self.after(doc)
                self.assertFalse(kept.document_enc)
                self.assertIsNone(kept.hashed_email)


class FactsAboutThePlanAreKeptTest(_RemovalTestCase):
    def test_the_plan_facts_and_the_record_of_the_share_stay(self):
        doc = self.share()
        kept_fields = (
            shared_plan_documents.PLAN_FACT_FIELDS
            + shared_plan_documents.SHARE_RECORD_FIELDS
        )
        before = {name: getattr(doc, name) for name in kept_fields}
        self.remove()
        kept = self.after(doc)
        for name in kept_fields:
            with self.subTest(field=name):
                self.assertEqual(getattr(kept, name), before[name])

    def test_the_kind_of_plan_alone_keeps_the_row(self):
        doc = self.share({"plan_category": "medicaid_chip"})
        self.remove()
        self.assertEqual(self.after(doc).plan_category, "medicaid_chip")

    def test_a_premium_of_zero_alone_keeps_the_row(self):
        doc = self.share({"premium_amount": Decimal("0")})
        self.remove()
        self.assertEqual(self.after(doc).premium_amount, Decimal("0"))

    def test_who_it_covers_and_how_often_go_without_an_amount(self):
        doc = self.share(
            {
                "insurer_name": "Crumpler Health Mutual",
                "premium_period": "monthly",
                "who_is_covered": "family",
            }
        )
        self.remove()
        kept = self.after(doc)
        self.assertEqual(kept.insurer_name, "Crumpler Health Mutual")
        self.assertEqual((kept.premium_period, kept.who_is_covered), ("", ""))


class ARowWithNothingAboutThePlanIsDeletedTest(_RemovalTestCase):
    def test_a_share_with_no_plan_facts_is_deleted_with_its_file(self):
        doc = self.share({})
        name = doc.document_enc.name
        deleted = self.remove()
        self.assertIsNone(self.after(doc))
        deleted.assert_any_call(name)

    def test_who_it_covers_alone_is_not_a_plan_fact(self):
        doc = self.share({"who_is_covered": "family", "premium_period": "monthly"})
        self.remove()
        self.assertIsNone(self.after(doc))


class OtherRowsAreUntouchedTest(_RemovalTestCase):
    def snapshot(self, doc):
        doc = self.after(doc)
        return model_to_dict(doc) | {"file": doc.document_enc.name}

    def test_other_peoples_shares_are_untouched(self):
        mine = self.share()
        theirs = self.share(email=OTHER_EMAIL)
        before = self.snapshot(theirs)
        deleted = self.remove()
        self.assertEqual(self.snapshot(theirs), before)
        self.assertNotIn(theirs.document_enc.name, str(deleted.mock_calls))
        self.assertIsNone(self.after(mine).hashed_email)

    def test_a_share_with_no_email_is_untouched(self):
        anonymous = self.share(email=None)
        self.share()
        before = self.snapshot(anonymous)
        self.remove()
        self.assertEqual(self.snapshot(anonymous), before)

    def test_the_persons_own_explain_policy_upload_is_still_deleted_whole(self):
        own = PolicyDocument.objects.create(
            source=PolicyDocument.SOURCE_EXPLAIN_POLICY,
            plan_category="aca_marketplace",
            hashed_email=Denial.get_hashed_email(EMAIL.lower()),
            document_enc=SimpleUploadedFile("policy.txt", CONTENT),
        )
        name = own.document_enc.name
        deleted = self.remove()
        self.assertIsNone(self.after(own))
        deleted.assert_any_call(name)


class EveryFieldIsDecidedTest(TestCase):
    def test_each_field_is_kept_or_cleared_on_removal(self):
        """A new field on PolicyDocument needs a decision here: kept as a
        plan fact, kept as the record of the share, or cleared."""
        decided = (
            set(shared_plan_documents.PLAN_FACT_FIELDS)
            | set(shared_plan_documents.SHARE_RECORD_FIELDS)
            | set(shared_plan_documents.REMOVED_VALUES)
        )
        fields = {field.name for field in PolicyDocument._meta.concrete_fields}
        self.assertEqual(decided, fields)

