"""A company lookup that did not finish is tried again by the hourly sweep.

A lookup that raises, or one no model answered where the fixed patterns
found no company, is counted on the row (company_lookup_failures) apart
from a document that names no company. The sweep looks again
(plan_documents_processing.aretry_company_lookups), a few rows a run,
fewest failed first, until MAX_COMPANY_LOOKUPS have not finished; a company
staff set is left alone.
"""

import itertools
from io import StringIO
from unittest.mock import AsyncMock, Mock, patch

from asgiref.sync import async_to_sync
from django.core.management import call_command

from fighthealthinsurance import plan_documents_processing as processing
from fighthealthinsurance.activities import plan_documents_sweep as sweep_activities
from fighthealthinsurance.models import PolicyDocument
from fighthealthinsurance.plan_sponsor_reader import PlanReading
from fighthealthinsurance.plan_sponsors import (
    SOURCE_MODEL,
    SOURCE_PERSON,
    Sponsor,
    link_sponsor,
)
from tests.plan_text_samples import PLAN_TEXT
from tests.sync.test_shared_plan_documents_are_read_and_cleaned import _ReadingTest

READY = PolicyDocument.PROCESSING_READY
UNANSWERED = PlanReading(None, False)
FOUND = PlanReading(Sponsor("Crumpler Cooperative", ""), True)


def finding(reading):
    return patch.object(
        processing, "afind_company", new=AsyncMock(return_value=reading)
    )


class _LookupTest(_ReadingTest):
    def unfinished(self, count=1):
        """Shared rows read as ready whose company lookup did not finish."""
        docs = []
        with finding(UNANSWERED):
            for n in range(count):
                _, doc = self.read(f"plan{n}.txt", f"{PLAN_TEXT}\nPage {n}".encode())
                docs.append(doc)
        return docs

    def retry(self, **kwargs):
        return async_to_sync(processing.aretry_company_lookups)(**kwargs)


class AnUnfinishedLookupIsCountedTest(_LookupTest):
    def test_one_no_model_answered_is_counted_and_the_reading_stands(self):
        (doc,) = self.unfinished()
        self.assertEqual((doc.processing_status, doc.company_lookup_failures), (READY, 1))

    def test_one_the_fixed_patterns_settled_is_not_counted(self):
        settled = PlanReading(Sponsor("Crumpler Cooperative", ""), False)
        with finding(settled):
            _, doc = self.read("plan.txt", PLAN_TEXT.encode())
        self.assertEqual(doc.company_lookup_failures, 0)
        self.assertIsNotNone(doc.plan_sponsor_id)

    def test_link_plan_sponsors_counts_one_no_model_answered(self):
        with finding(PlanReading(None, True)):
            self.read("plan.txt", PLAN_TEXT.encode())
        out, err = StringIO(), StringIO()
        with patch(
            "fighthealthinsurance.management.commands.link_plan_sponsors.afind_company",
            new=AsyncMock(return_value=UNANSWERED),
        ):
            call_command("link_plan_sponsors", "--apply", stdout=out, stderr=err)
        self.assertIn("1 failed this time", out.getvalue())
        self.assertIn("did not finish: no model answered", err.getvalue())
        self.assertEqual(PolicyDocument.objects.get().company_lookup_failures, 1)


class TheSweepLooksAgainTest(_LookupTest):
    def test_a_lookup_that_finishes_links_the_company_and_clears_the_count(self):
        (doc,) = self.unfinished()
        with finding(FOUND):
            self.assertEqual(self.retry(), 1)
        doc.refresh_from_db()
        self.assertIsNotNone(doc.plan_sponsor_id)
        self.assertEqual(doc.company_lookup_failures, 0)

    def test_it_reads_the_rows_cleaned_text(self):
        (doc,) = self.unfinished()
        with finding(FOUND) as found:
            self.retry()
        found.assert_awaited_once_with(doc.cleaned_text, doc.cleaned_text)

    def test_one_that_does_not_finish_again_counts_another_try(self):
        (doc,) = self.unfinished()
        with finding(UNANSWERED):
            self.assertEqual(self.retry(), 0)
        doc.refresh_from_db()
        self.assertEqual(doc.company_lookup_failures, 2)

    def test_after_its_last_try_a_row_is_left_for_staff(self):
        (doc,) = self.unfinished()
        PolicyDocument.objects.filter(pk=doc.pk).update(
            company_lookup_failures=processing.MAX_COMPANY_LOOKUPS
        )
        with finding(FOUND) as found:
            self.assertEqual(self.retry(), 0)
        found.assert_not_awaited()

    def test_a_company_staff_set_is_not_looked_up_again(self):
        (doc,) = self.unfinished()
        PolicyDocument.objects.filter(pk=doc.pk).update(plan_sponsor_source="staff")
        with finding(FOUND) as found:
            self.retry()
        found.assert_not_awaited()

    def test_a_row_with_no_text_is_not_looked_at_and_holds_none_back(self):
        textless, other = self.unfinished(2)
        PolicyDocument.objects.filter(pk=textless.pk).update(cleaned_text="")
        PolicyDocument.objects.filter(pk=other.pk).update(company_lookup_failures=2)
        with finding(FOUND) as found:
            self.assertEqual(self.retry(limit=1), 1)
        found.assert_awaited_once_with(other.cleaned_text, other.cleaned_text)

    def test_a_row_no_longer_ready_is_not_looked_up_again(self):
        (doc,) = self.unfinished()
        PolicyDocument.objects.filter(pk=doc.pk).update(
            processing_status=PolicyDocument.PROCESSING_REMOVED
        )
        with finding(FOUND) as found:
            self.retry()
        found.assert_not_awaited()

    def test_a_run_looks_at_no_more_than_its_limit(self):
        self.unfinished(3)
        with finding(FOUND) as found:
            self.assertEqual(self.retry(limit=2), 2)
        self.assertEqual(found.await_count, 2)

    def test_the_fewest_failed_go_first(self):
        tried_more, tried_less = self.unfinished(2)
        PolicyDocument.objects.filter(pk=tried_more.pk).update(
            company_lookup_failures=3
        )
        with finding(FOUND):
            self.retry(limit=1)
        tried_less.refresh_from_db()
        self.assertIsNotNone(tried_less.plan_sponsor_id)

    def test_a_run_stops_at_the_first_lookup_that_does_not_finish(self):
        self.unfinished(2)
        with finding(UNANSWERED) as found:
            self.retry()
        self.assertEqual(found.await_count, 1)
        self.assertEqual(
            sorted(PolicyDocument.objects.values_list("company_lookup_failures", flat=True)),
            [1, 2],
        )

    def test_a_run_starts_no_lookup_past_its_time_bound(self):
        self.unfinished(2)
        clock = Mock()
        clock.monotonic.side_effect = itertools.chain(
            [0, 0], itertools.repeat(processing.LOOKUP_RUN_SECONDS)
        )
        with finding(FOUND) as found, patch.object(processing, "time", clock):
            self.assertEqual(self.retry(), 1)
        self.assertEqual(found.await_count, 1)

    def test_the_sweeps_activity_returns_how_many_finished(self):
        self.unfinished()
        with finding(FOUND):
            self.assertEqual(
                async_to_sync(sweep_activities.retry_company_lookups)(), 1
            )


class ALinkLandsOnlyOnARowStillAPlanTest(_LookupTest):
    def link_after_marking(self, status):
        (doc,) = self.unfinished()
        PolicyDocument.objects.filter(pk=doc.pk).update(processing_status=status)
        link_sponsor(doc.pk, FOUND.sponsor)
        doc.refresh_from_db()
        return doc.plan_sponsor_id

    def test_a_row_marked_potential_spam_meanwhile_gets_no_company(self):
        self.assertIsNone(self.link_after_marking(PolicyDocument.PROCESSING_SPAM))

    def test_a_row_read_as_not_a_plan_meanwhile_gets_no_company(self):
        self.assertIsNone(
            self.link_after_marking(PolicyDocument.PROCESSING_NOT_A_PLAN)
        )

    def test_a_ready_row_still_gets_its_company(self):
        self.assertIsNotNone(self.link_after_marking(READY))


class LinkPlanSponsorsTreatsATypedCompanyLikeTheSweepTest(_LookupTest):
    def typed_then_command(self, company_source):
        (doc,) = self.unfinished()
        typed = Sponsor("Rebecca Lee Crumpler Clinic", "", source=SOURCE_PERSON)
        link_sponsor(doc.pk, typed)
        PolicyDocument.objects.filter(pk=doc.pk).update(
            plan_sponsor_source=company_source
        )
        read = Sponsor("Crumpler Cooperative", "", source=SOURCE_MODEL)
        with patch(
            "fighthealthinsurance.management.commands.link_plan_sponsors.afind_company",
            new=AsyncMock(return_value=PlanReading(read, True)),
        ):
            call_command(
                "link_plan_sponsors", "--apply", stdout=StringIO(), stderr=StringIO()
            )
        doc.refresh_from_db()
        return doc

    def test_a_reading_replaces_the_company_the_person_typed(self):
        doc = self.typed_then_command(SOURCE_PERSON)
        self.assertEqual(doc.plan_sponsor.name, "Crumpler Cooperative")
        self.assertEqual(doc.plan_sponsor_source, SOURCE_MODEL)
        self.assertEqual(doc.company_lookup_failures, 0)

    def test_a_company_staff_set_stays(self):
        doc = self.typed_then_command("staff")
        self.assertEqual(doc.plan_sponsor.name, "Rebecca Lee Crumpler Clinic")
        self.assertEqual(doc.plan_sponsor_source, "staff")
