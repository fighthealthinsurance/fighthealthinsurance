"""Tests for the subscriber cleanup flow: the hygiene analysis, the
``cleanup_subscribers`` management command, and the staff review page."""

from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.urls import reverse

from fighthealthinsurance import subscriber_hygiene as hygiene
from fighthealthinsurance.models import MailingListSubscriber

User = get_user_model()


def make_subscriber(email, **kwargs):
    return MailingListSubscriber.objects.create(email=email, **kwargs)


class AnalyzeRecordTest(TestCase):
    """Single-row checks."""

    @staticmethod
    def _codes(email="person@example.org", **kwargs):
        record = hygiene.ContactRecord(pk=1, email=email, **kwargs)
        return hygiene.analyze_record(record).codes

    def test_ordinary_subscriber_is_not_flagged(self):
        self.assertEqual(
            self._codes(
                email="jane@hospital.org", name="Jane Doe", comments="From appeal flow"
            ),
            [],
        )

    def test_reserved_domain_is_unsendable(self):
        self.assertIn(hygiene.UNSENDABLE, self._codes(email="someone@example.com"))

    def test_malformed_address_is_flagged_once_as_unsendable(self):
        codes = self._codes(email="not-an-email")
        self.assertIn(hygiene.UNSENDABLE, codes)
        # is_blocked_email already rejects it; don't double-report it as invalid.
        self.assertNotIn(hygiene.INVALID_EMAIL, codes)

    def test_address_with_spaces_is_invalid(self):
        self.assertIn(
            hygiene.INVALID_EMAIL, self._codes(email="two words@realdomain.org")
        )

    def test_url_in_name_is_form_spam(self):
        codes = self._codes(name="Buy now http://spam.example")
        self.assertIn(hygiene.LINK_IN_FIELDS, codes)

    def test_url_in_comments_is_form_spam(self):
        self.assertIn(
            hygiene.LINK_IN_FIELDS, self._codes(comments="check www.spam.example")
        )

    def test_newline_in_name_is_header_injection(self):
        self.assertIn(
            hygiene.HEADER_INJECTION, self._codes(name="Jane\nBcc: victim@example.org")
        )

    def test_mail_header_in_comments_is_header_injection(self):
        self.assertIn(
            hygiene.HEADER_INJECTION, self._codes(comments="Content-Type: text/html")
        )

    def test_zero_width_character_in_name_is_suspicious_unicode(self):
        self.assertIn(hygiene.SUSPICIOUS_UNICODE, self._codes(name="Ja​ne"))

    def test_control_character_in_address_is_flagged(self):
        self.assertIn(hygiene.CONTROL_IN_EMAIL, self._codes(email="jane​@hospital.org"))

    def test_spam_tld_is_review_only(self):
        codes = self._codes(email="someone@vendor.ru")
        self.assertEqual(codes, [hygiene.SPAM_TLD])
        self.assertFalse(hygiene.REASONS[hygiene.SPAM_TLD].auto_cleanable)

    def test_known_internal_address_is_flagged_as_test(self):
        self.assertIn(hygiene.INTERNAL_TEST, self._codes(email="test@test.com"))

    def test_role_account_is_flagged(self):
        self.assertIn(hygiene.ROLE_ADDRESS, self._codes(email="noreply@hospital.org"))

    def test_overlong_name_is_flagged(self):
        self.assertIn(hygiene.OVERLONG_FIELD, self._codes(name="x" * 500))


class MailboxKeyTest(TestCase):
    def test_gmail_dots_and_tags_collapse(self):
        self.assertEqual(hygiene.mailbox_key("Ja.ne+news@GMAIL.com"), "jane@gmail.com")

    def test_dots_are_significant_off_gmail(self):
        self.assertEqual(
            hygiene.mailbox_key("ja.ne@hospital.org"), "ja.ne@hospital.org"
        )


class ScanSubscribersTest(TestCase):
    """Cross-row checks and queryset handling."""

    def test_case_variant_duplicate_flags_the_older_row_only(self):
        older = make_subscriber("Jane@Hospital.org")
        newer = make_subscriber("jane@hospital.org")

        result = hygiene.scan_subscribers()
        flagged = {f.record.pk: f for f in result.findings}

        self.assertIn(older.id, flagged)
        self.assertNotIn(newer.id, flagged)
        self.assertIn(hygiene.DUPLICATE, flagged[older.id].codes)
        self.assertIn(f"#{newer.id}", flagged[older.id].details[hygiene.DUPLICATE])

    def test_duplicate_is_auto_cleanable(self):
        make_subscriber("jane@hospital.org")
        make_subscriber("JANE@hospital.org")
        result = hygiene.scan_subscribers()
        self.assertEqual(len(result.auto_cleanable_findings), 1)

    def test_alias_duplicates_are_review_only(self):
        make_subscriber("jane@gmail.com")
        make_subscriber("ja.ne@gmail.com")

        result = hygiene.scan_subscribers()

        self.assertEqual(result.flagged_count, 2)
        for finding in result.findings:
            self.assertEqual(finding.codes, [hygiene.ALIAS_DUPLICATE])
            self.assertFalse(finding.auto_cleanable)

    def test_reviewed_rows_are_excluded_from_the_queue(self):
        subscriber = make_subscriber("someone@example.com")
        self.assertEqual(hygiene.scan_subscribers().flagged_count, 1)

        hygiene.mark_subscribers_reviewed([subscriber.id], actor="staffer")

        self.assertEqual(hygiene.scan_subscribers().flagged_count, 0)
        self.assertEqual(
            hygiene.scan_subscribers(include_reviewed=True).flagged_count, 1
        )

    def test_reviewed_row_still_counts_as_the_duplicate_keeper(self):
        """A reviewed row must not vanish before the duplicate pass, or an older
        unreviewed copy would look unique and survive."""
        older = make_subscriber("jane@hospital.org")
        newer = make_subscriber("Jane@Hospital.org")
        hygiene.mark_subscribers_reviewed([newer.id])

        result = hygiene.scan_subscribers()

        self.assertEqual([f.record.pk for f in result.findings], [older.id])
        self.assertIn(hygiene.DUPLICATE, result.findings[0].codes)

    def test_counts_by_code_only_reports_present_reasons(self):
        make_subscriber("someone@example.com")
        counts = hygiene.scan_subscribers().counts_by_code()
        self.assertEqual(counts, {hygiene.UNSENDABLE: 1})

    def test_clean_list_produces_no_findings(self):
        make_subscriber("jane@hospital.org", name="Jane Doe")
        make_subscriber("bob@clinic.org", name="Bob Roberts")
        self.assertEqual(hygiene.scan_subscribers().findings, [])


class DeleteSubscribersTest(TestCase):
    def test_delete_removes_only_the_given_ids(self):
        doomed = make_subscriber("someone@example.com")
        kept = make_subscriber("jane@hospital.org")

        deleted = hygiene.delete_subscribers([doomed.id], actor="tester")

        self.assertEqual(deleted, 1)
        self.assertFalse(MailingListSubscriber.objects.filter(id=doomed.id).exists())
        self.assertTrue(MailingListSubscriber.objects.filter(id=kept.id).exists())

    def test_delete_with_no_ids_is_a_no_op(self):
        make_subscriber("jane@hospital.org")
        self.assertEqual(hygiene.delete_subscribers([]), 0)
        self.assertEqual(MailingListSubscriber.objects.count(), 1)


class CleanupSubscribersCommandTest(TestCase):
    def setUp(self):
        self.junk = make_subscriber("someone@example.com")
        self.spammy = make_subscriber(
            "seo@vendor.org", name="Cheap pills http://spam.example"
        )
        self.review_only = make_subscriber("someone@vendor.ru")
        self.good = make_subscriber("jane@hospital.org", name="Jane Doe")

    def _run(self, *args):
        out = StringIO()
        call_command("cleanup_subscribers", *args, stdout=out)
        return out.getvalue()

    def test_report_does_not_delete_anything(self):
        output = self._run()
        self.assertEqual(MailingListSubscriber.objects.count(), 4)
        self.assertIn("--apply", output)
        self.assertIn(hygiene.UNSENDABLE, output)

    def test_report_masks_email_addresses(self):
        output = self._run()
        self.assertNotIn("someone@example.com", output)

    def test_apply_deletes_auto_cleanable_rows_only(self):
        self._run("--apply")

        remaining = set(MailingListSubscriber.objects.values_list("id", flat=True))
        self.assertEqual(remaining, {self.review_only.id, self.good.id})

    def test_apply_with_explicit_reason_only_deletes_that_reason(self):
        self._run("--apply", "--reasons", hygiene.LINK_IN_FIELDS)

        remaining = set(MailingListSubscriber.objects.values_list("id", flat=True))
        self.assertEqual(remaining, {self.junk.id, self.review_only.id, self.good.id})

    def test_apply_refuses_review_only_reasons_without_force(self):
        with self.assertRaises(CommandError):
            self._run("--apply", "--reasons", hygiene.SPAM_TLD)
        self.assertEqual(MailingListSubscriber.objects.count(), 4)

    def test_force_allows_deleting_on_a_review_only_reason(self):
        self._run("--apply", "--force", "--reasons", hygiene.SPAM_TLD)
        self.assertFalse(
            MailingListSubscriber.objects.filter(id=self.review_only.id).exists()
        )

    def test_unknown_reason_code_is_rejected(self):
        with self.assertRaises(CommandError):
            self._run("--reasons", "not_a_real_reason")

    def test_all_sources_reports_other_tables(self):
        output = self._run("--all-sources")
        self.assertIn("ChatLeads", output)
        self.assertIn("DemoRequests", output)
        self.assertIn("InterestedProfessional", output)


class SubscriberCleanupViewTest(TestCase):
    def setUp(self):
        self.url = reverse("subscriber_cleanup")
        User.objects.create_user(username="staff", password="pw123", is_staff=True)
        self.junk = make_subscriber("someone@example.com")
        self.review_only = make_subscriber("someone@vendor.ru")
        self.good = make_subscriber("jane@hospital.org", name="Jane Doe")

    def login(self):
        self.assertTrue(self.client.login(username="staff", password="pw123"))

    def test_non_staff_are_redirected(self):
        User.objects.create_user(username="plain", password="pw123", is_staff=False)
        self.client.login(username="plain", password="pw123")
        response = self.client.get(self.url)
        self.assertIn(response.status_code, (302, 403))

    def test_page_lists_flagged_rows_only(self):
        self.login()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "someone@example.com")
        self.assertContains(response, "someone@vendor.ru")
        self.assertNotContains(response, "jane@hospital.org")

    def test_delete_selected_removes_the_checked_rows(self):
        self.login()
        response = self.client.post(
            self.url,
            {"action": "delete_selected", "subscriber_id": [str(self.junk.id)]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(MailingListSubscriber.objects.filter(id=self.junk.id).exists())
        self.assertTrue(MailingListSubscriber.objects.filter(id=self.good.id).exists())

    def test_keep_selected_marks_reviewed_and_clears_the_queue(self):
        self.login()
        self.client.post(
            self.url,
            {"action": "keep_selected", "subscriber_id": [str(self.review_only.id)]},
        )
        self.review_only.refresh_from_db()
        self.assertIsNotNone(self.review_only.cleanup_reviewed_at)
        self.assertEqual(self.review_only.cleanup_reviewed_by, "staff")
        self.assertNotIn(
            self.review_only.id,
            [f.record.pk for f in hygiene.scan_subscribers().findings],
        )

    def test_bulk_delete_requires_confirmation(self):
        self.login()
        response = self.client.post(self.url, {"action": "delete_auto"})
        self.assertContains(response, "confirmation box")
        self.assertTrue(MailingListSubscriber.objects.filter(id=self.junk.id).exists())

    def test_bulk_delete_spares_review_only_rows(self):
        self.login()
        self.client.post(self.url, {"action": "delete_auto", "confirm": "yes"})

        remaining = set(MailingListSubscriber.objects.values_list("id", flat=True))
        self.assertEqual(remaining, {self.review_only.id, self.good.id})

    def test_delete_with_nothing_selected_reports_an_error(self):
        self.login()
        response = self.client.post(self.url, {"action": "delete_selected"})
        self.assertContains(response, "No subscribers were selected.")
        self.assertEqual(MailingListSubscriber.objects.count(), 3)

    def test_include_leads_scans_the_other_tables(self):
        self.login()
        response = self.client.get(self.url, {"include_leads": "1"})
        self.assertContains(response, "ChatLeads")


class UnsubscribeDuplicateTest(TestCase):
    """Unsubscribing has to clear every row for the address, not just the one
    holding the token, or duplicates keep the person subscribed."""

    def test_unsubscribe_removes_case_variant_duplicates(self):
        first = make_subscriber("jane@hospital.org")
        make_subscriber("Jane@Hospital.org")

        response = self.client.get(
            reverse("unsubscribe", kwargs={"token": first.unsubscribe_token})
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            MailingListSubscriber.objects.filter(
                email__iexact="jane@hospital.org"
            ).count(),
            0,
        )
