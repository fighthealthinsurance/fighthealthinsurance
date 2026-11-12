"""A copy ticked for research whose save failed is made again later.

On Explain Policy and the appeal's plan documents step, a copy whose save
fails is held (SharedCopyRetry) pointing at the page's own row for the
file, and the sweep makes it again (shared_copy_retries.retry_due_copies)
through the same door as the first try. The page's own work never depends
on it. Every retry ends within RETRY_LIFETIME, a removal by email or the
origin's deletion takes it, and only reason codes and counts are logged.
"""

import datetime
import itertools
from unittest.mock import Mock, patch

from django.core.exceptions import ValidationError
from django.db import models
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from loguru import logger

from fighthealthinsurance import consent, shared_copy_retries, shared_plan_documents
from fighthealthinsurance.ml.ml_document_extraction import read_and_decrypt_file
from fighthealthinsurance.models import (
    Denial,
    InsuranceCompany,
    PlanDocuments,
    PolicyDocument,
    SharedCopyRetry,
    SharedPlanDocumentsDay,
)
from tests.sync.test_sharing_is_offered_where_people_already_upload import (
    EMAIL,
    PDF,
    SEMI_SEKRET,
    _UploadsTestCase,
    pdf,
)

Day = SharedPlanDocumentsDay
failing_save = patch.object(
    shared_plan_documents,
    "save_shared_documents",
    side_effect=RuntimeError("storage went away"),
)


def dropped(reason):
    row = Day.objects.filter(day=timezone.now().date(), name=reason).first()
    return row.count if row else 0


def due_now():
    SharedCopyRetry.objects.update(next_try_at=timezone.now())
    return shared_copy_retries.retry_due_copies()


class _ExplainPolicy(_UploadsTestCase):
    def upload(self, **extra):
        data = {
            "first_name": "Rebecca",
            "last_name": "Crumpler",
            "email": EMAIL,
            "document_type": "summary_of_benefits",
            "plan_category": "aca_marketplace",
            "tos_agreement": "on",
            "privacy_policy": "on",
            "policy_document": pdf(),
            "share_for_research": "on",
        }
        data.update(extra)
        return Client().post(reverse("understand_policy"), data)

    def fail_first_try(self):
        with failing_save, patch.object(shared_plan_documents.logger, "error"):
            self.upload()
        return PolicyDocument.objects.get(source=PolicyDocument.SOURCE_EXPLAIN_POLICY)

    def copies(self):
        return PolicyDocument.objects.filter(source=PolicyDocument.SOURCE_SHARED_EXPLAIN)


class AnExplainPolicyCopyIsMadeAgainTest(_ExplainPolicy):
    def test_a_copy_whose_save_fails_waits_pointing_at_the_pages_own_file(self):
        own = self.fail_first_try()
        retry = SharedCopyRetry.objects.get()
        self.assertEqual(
            (retry.origin_policy_document_id, retry.consent_version, retry.attempts),
            (own.pk, consent.SHARING_VERSION, 0),
        )

    def test_waiting_is_not_counted_as_dropped(self):
        self.fail_first_try()
        self.assertEqual(dropped(Day.DROPPED_NOT_SAVED), 0)

    def test_the_retry_makes_the_copy_from_the_pages_own_file(self):
        self.fail_first_try()
        self.assertEqual(due_now()[shared_copy_retries.SAVED], 1)
        self.assertEqual(read_and_decrypt_file(self.copies().get().document_enc), PDF)
        self.assertFalse(SharedCopyRetry.objects.exists())

    def test_the_copy_keeps_the_version_its_box_was_ticked_under(self):
        self.fail_first_try()
        later = consent.SHARING_VERSION + datetime.timedelta(days=30)
        with patch.object(consent, "SHARING_VERSION", later):
            due_now()
        self.assertNotEqual(self.copies().get().shared_consent_version, later)

    def test_a_retry_that_is_not_due_waits(self):
        self.fail_first_try()
        shared_copy_retries.retry_due_copies()
        self.assertFalse(self.copies().exists())

    def test_a_full_budget_waits_for_the_next_day_and_spends_no_attempt(self):
        self.fail_first_try()
        with patch.object(
            shared_plan_documents,
            "save_shared_documents",
            side_effect=shared_plan_documents.OverDailyBudget(),
        ):
            due_now()
        retry = SharedCopyRetry.objects.get()
        tomorrow = timezone.now().date() + datetime.timedelta(days=1)
        self.assertEqual(
            (retry.attempts, retry.next_try_at.date()), (0, tomorrow)
        )

    def test_another_failure_waits_and_counts_an_attempt(self):
        self.fail_first_try()
        with failing_save:
            due_now()
        retry = SharedCopyRetry.objects.get()
        self.assertEqual((retry.attempts, retry.last_reason), (1, "RuntimeError"))
        self.assertGreater(retry.next_try_at, timezone.now())

    def test_after_its_last_attempt_it_is_dropped(self):
        self.fail_first_try()
        SharedCopyRetry.objects.update(
            attempts=shared_copy_retries.MAX_COPY_ATTEMPTS - 1
        )
        with failing_save:
            due_now()
        self.assertFalse(SharedCopyRetry.objects.exists())
        self.assertEqual(dropped(Day.DROPPED_NOT_SAVED), 1)

    def test_past_its_lifetime_it_is_dropped_whatever_the_budget(self):
        self.fail_first_try()
        SharedCopyRetry.objects.update(
            created_at=timezone.now()
            - shared_copy_retries.RETRY_LIFETIME
            - datetime.timedelta(minutes=1),
            next_try_at=timezone.now() + datetime.timedelta(days=1),
        )
        shared_copy_retries.retry_due_copies()
        self.assertFalse(SharedCopyRetry.objects.exists())
        self.assertEqual(dropped(Day.DROPPED_NOT_SAVED), 1)

    def test_a_copy_that_reaches_its_lifetime_during_the_run_is_not_saved(self):
        self.fail_first_try()
        from fighthealthinsurance.ml import ml_document_extraction

        real = ml_document_extraction.read_and_decrypt_file

        def expired_while_reading(field, max_bytes=None):
            SharedCopyRetry.objects.update(
                created_at=timezone.now() - shared_copy_retries.RETRY_LIFETIME
            )
            return real(field, max_bytes=max_bytes)

        with patch.object(
            ml_document_extraction, "read_and_decrypt_file", expired_while_reading
        ):
            outcome = due_now()
        self.assertEqual(outcome[shared_copy_retries.DROPPED], 1)
        self.assertFalse(self.copies().exists())
        self.assertFalse(SharedCopyRetry.objects.exists())
        self.assertEqual(dropped(Day.DROPPED_NOT_SAVED), 1)

    def test_a_run_starts_no_copy_past_its_time_bound(self):
        own = self.fail_first_try()
        SharedCopyRetry.objects.create(
            source=PolicyDocument.SOURCE_SHARED_EXPLAIN,
            origin_policy_document=own,
            consent_version=consent.SHARING_VERSION,
            next_try_at=timezone.now(),
        )
        clock = Mock()
        clock.monotonic.side_effect = itertools.chain(
            [0, 0], itertools.repeat(shared_copy_retries.RUN_SECONDS)
        )
        with patch.object(shared_copy_retries, "time", clock):
            outcome = due_now()
        self.assertEqual(outcome[shared_copy_retries.SAVED], 1)
        left = SharedCopyRetry.objects.get()
        self.assertEqual(left.attempts, 0)
        self.assertLessEqual(left.next_try_at, timezone.now())

    def test_a_page_copy_with_no_file_any_more_drops_it(self):
        own = self.fail_first_try()
        own.document_enc.delete(save=False)
        PolicyDocument.objects.filter(pk=own.pk).update(document_enc="")
        due_now()
        self.assertFalse(SharedCopyRetry.objects.exists())
        self.assertEqual(dropped(Day.DROPPED_ORIGIN_GONE), 1)

    def test_a_copy_refused_by_the_closer_look_is_dropped(self):
        self.fail_first_try()
        with patch(
            "fighthealthinsurance.chat_forms.validate_shared_document",
            side_effect=ValidationError("not a plan document"),
        ):
            due_now()
        self.assertFalse(SharedCopyRetry.objects.exists())
        self.assertEqual(dropped(Day.DROPPED_REFUSED), 1)

    def test_every_expired_copy_goes_whatever_the_runs_limit(self):
        own = self.fail_first_try()
        old = timezone.now() - shared_copy_retries.RETRY_LIFETIME
        for _ in range(shared_copy_retries.MAX_RETRIES_PER_RUN + 2):
            SharedCopyRetry.objects.create(
                source=PolicyDocument.SOURCE_SHARED_EXPLAIN,
                origin_policy_document=own,
                consent_version=consent.SHARING_VERSION,
                next_try_at=timezone.now() + datetime.timedelta(days=1),
            )
        SharedCopyRetry.objects.update(created_at=old - datetime.timedelta(minutes=1))
        shared_copy_retries.retry_due_copies()
        self.assertFalse(SharedCopyRetry.objects.exists())

    def test_a_removal_during_the_retry_stops_the_copy(self):
        self.fail_first_try()
        from fighthealthinsurance.ml import ml_document_extraction

        real = ml_document_extraction.read_and_decrypt_file

        def removed_while_reading(field, max_bytes=None):
            content = real(field, max_bytes=max_bytes)
            shared_plan_documents.remove_shared_documents(
                Denial.get_hashed_email(EMAIL)
            )
            return content

        with patch.object(
            ml_document_extraction, "read_and_decrypt_file", removed_while_reading
        ):
            outcome = due_now()
        self.assertEqual(outcome[shared_copy_retries.GONE], 1)
        self.assertFalse(self.copies().exists())

    def test_a_copy_and_its_retrys_end_commit_together(self):
        self.fail_first_try()
        with patch.object(
            SharedCopyRetry, "delete", side_effect=RuntimeError("db went away")
        ), patch.object(shared_copy_retries.logger, "warning"):
            due_now()
        self.assertFalse(self.copies().exists())
        self.assertEqual(SharedCopyRetry.objects.get().attempts, 1)

    def test_a_page_copy_marked_spam_is_never_made_again(self):
        own = self.fail_first_try()
        PolicyDocument.objects.filter(pk=own.pk).update(
            processing_status=PolicyDocument.PROCESSING_SPAM
        )
        due_now()
        self.assertFalse(self.copies().exists())
        self.assertFalse(SharedCopyRetry.objects.exists())

    def test_a_file_whose_copy_staff_marked_spam_is_not_made_again(self):
        self.fail_first_try()
        self.upload()
        self.copies().update(processing_status=PolicyDocument.PROCESSING_SPAM)
        due_now()
        self.assertEqual(self.copies().count(), 1)
        self.assertEqual(dropped(Day.DROPPED_REFUSED), 1)

    def test_a_retry_refused_for_no_bucket_copy_is_counted(self):
        from django.test import override_settings

        self.fail_first_try()
        with override_settings(SHARED_FILES_NEED_BUCKET_COPY=True), patch(
            "fighthealthinsurance.storage_health.bucket_backends", return_value=[object()]
        ), patch(
            "fighthealthinsurance.storage_health.has_bucket_copy", return_value=False
        ), patch.object(shared_plan_documents.logger, "error"), patch.object(
            shared_plan_documents.logger, "warning"
        ):
            due_now()
        self.assertEqual(dropped(Day.NO_BUCKET_OPT_IN), 1)
        self.assertEqual(SharedCopyRetry.objects.get().attempts, 1)
        self.assertFalse(self.copies().exists())

    def test_a_saved_copys_reading_starts_once_it_is_committed(self):
        from fighthealthinsurance import temporal_client

        self.fail_first_try()
        with patch.object(
            temporal_client, "dispatch_shared_plan_documents"
        ) as dispatch, self.captureOnCommitCallbacks(execute=True):
            due_now()
            dispatch.assert_not_called()
        dispatch.assert_called_once_with(str(self.copies().get().share_batch))

    def test_a_saved_copy_waits_for_the_sweep_if_its_start_is_lost(self):
        self.fail_first_try()
        due_now()
        self.assertEqual(
            self.copies().get().processing_status, PolicyDocument.PROCESSING_RECEIVED
        )

    def test_staff_can_count_the_copies_waiting(self):
        self.fail_first_try()
        self.assertEqual(shared_copy_retries.copies_waiting(), 1)

    def test_nothing_about_the_file_or_person_is_logged(self):
        lines: list = []
        sink = logger.add(lambda message: lines.append(str(message)), level="DEBUG")
        try:
            self.fail_first_try()
            with failing_save:
                due_now()
            due_now()
        finally:
            logger.remove(sink)
        for line in lines:
            self.assertNotIn("Crumpler", line)
            self.assertNotIn(EMAIL, line)


class AWaitingCopyGoesWithItsPersonTest(_ExplainPolicy):
    def test_a_removal_by_email_deletes_it(self):
        self.fail_first_try()
        shared_plan_documents.remove_shared_documents(Denial.get_hashed_email(EMAIL))
        self.assertFalse(SharedCopyRetry.objects.exists())

    def test_deleting_the_pages_own_row_takes_it(self):
        own = self.fail_first_try()
        own.delete()
        self.assertFalse(SharedCopyRetry.objects.exists())

    def test_every_field_is_accounted_for(self):
        """A new field on SharedCopyRetry needs a decision: how a removal
        and the origin's deletion reach it. Today: the origins cascade, the
        email is the removal's key, and the rest is about the retry only."""
        fields = {field.name for field in SharedCopyRetry._meta.concrete_fields}
        self.assertEqual(
            fields,
            {
                "id",
                "created_at",
                "source",
                "origin_policy_document",
                "origin_plan_document",
                "hashed_email",
                "consent_version",
                "attempts",
                "next_try_at",
                "last_reason",
            },
        )

    def test_both_origins_cascade(self):
        for name in ("origin_policy_document", "origin_plan_document"):
            with self.subTest(name):
                field = SharedCopyRetry._meta.get_field(name)
                self.assertIs(field.remote_field.on_delete, models.CASCADE)


class AnAppealCopyIsMadeAgainTest(_UploadsTestCase):
    def setUp(self):
        company = InsuranceCompany.objects.create(name="Crumpler Health Mutual")
        self.denial = Denial.objects.create(
            denial_text="Your claim has been denied.",
            hashed_email=Denial.get_hashed_email(EMAIL),
            semi_sekret=SEMI_SEKRET,
            your_state="MA",
            employer_name="Lee Crumpler Clinic",
            insurance_company="Crumpler Health Mutual",
            insurance_company_obj=company,
        )

    def post(self):
        return Client().post(
            reverse("dvc"),
            {
                "denial_id": self.denial.denial_id,
                "email": EMAIL,
                "semi_sekret": SEMI_SEKRET,
                "plan_documents": [pdf()],
                "share_for_research": "on",
            },
        )

    def fail_first_try(self):
        with failing_save, patch.object(shared_plan_documents.logger, "error"):
            return self.post()

    def test_it_points_at_the_appeals_own_file_and_the_appeal_goes_on(self):
        response = self.fail_first_try()
        self.assertTemplateUsed(response, "entity_extract.html")
        own = PlanDocuments.objects.get(denial=self.denial)
        self.assertEqual(SharedCopyRetry.objects.get().origin_plan_document_id, own.pk)

    def test_a_save_that_closes_the_upload_still_holds_the_copy(self):
        def closes_then_fails(files, **kwargs):
            for uploaded in files:
                uploaded.close()
            raise RuntimeError("storage went away")

        with patch.object(
            shared_plan_documents, "save_shared_documents", side_effect=closes_then_fails
        ), patch.object(shared_plan_documents.logger, "error"):
            self.post()
        own = PlanDocuments.objects.get(denial=self.denial)
        self.assertEqual(SharedCopyRetry.objects.get().origin_plan_document_id, own.pk)

    def test_a_failing_hold_never_changes_the_appeal(self):
        with failing_save, patch.object(
            shared_plan_documents.logger, "error"
        ), patch.object(
            SharedCopyRetry.objects, "create", side_effect=RuntimeError("db went away")
        ), patch.object(shared_copy_retries.logger, "warning"):
            response = self.post()
        self.assertTemplateUsed(response, "entity_extract.html")
        self.assertEqual(PlanDocuments.objects.filter(denial=self.denial).count(), 1)
        self.assertEqual(dropped(Day.DROPPED_NOT_SAVED), 1)

    def test_the_retry_makes_the_copy_with_what_the_appeal_knows(self):
        self.fail_first_try()
        due_now()
        copy = PolicyDocument.objects.get(source=PolicyDocument.SOURCE_SHARED_APPEAL)
        self.assertEqual(copy.state, "MA")
        self.assertEqual(read_and_decrypt_file(copy.document_enc), PDF)

    def test_a_copy_already_shared_from_the_appeal_is_not_made_twice(self):
        self.fail_first_try()
        self.post()
        outcome = due_now()
        self.assertEqual(outcome[shared_copy_retries.ALREADY_SHARED], 1)
        self.assertEqual(
            PolicyDocument.objects.filter(
                source=PolicyDocument.SOURCE_SHARED_APPEAL
            ).count(),
            1,
        )

    def test_deleting_the_appeal_takes_it(self):
        self.fail_first_try()
        self.denial.delete()
        self.assertFalse(SharedCopyRetry.objects.exists())
