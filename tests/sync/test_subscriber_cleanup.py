"""Tests for the subscriber cleanup flow: the hygiene analysis, the
``cleanup_subscribers`` management command, and the staff review page."""

from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.models import QuerySet
from django.test import TestCase
from django.urls import reverse

from fighthealthinsurance import subscriber_hygiene as hygiene
from fighthealthinsurance.models import InterestedProfessional, MailingListSubscriber

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

    def test_url_in_comments_is_review_only(self):
        """No signup form writes free text into comments, so a link there came
        from an admin edit -- a signal for a human, never a bulk delete."""
        codes = self._codes(
            email="jane@hospital.org", comments="check www.spam.example"
        )
        self.assertEqual(codes, [hygiene.LINK_IN_COMMENTS])
        self.assertFalse(hygiene.REASONS[hygiene.LINK_IN_COMMENTS].auto_cleanable)

    def test_newline_in_name_is_header_injection(self):
        self.assertIn(
            hygiene.HEADER_INJECTION, self._codes(name="Jane\nBcc: victim@example.org")
        )

    def test_mail_header_in_comments_is_review_only(self):
        """Comments are written by our code or staff, never a signup form, so a
        header-looking line there is a signal for a human, not bulk junk."""
        codes = self._codes(
            email="jane@hospital.org", comments="Content-Type: text/html"
        )
        self.assertEqual(codes, [hygiene.HEADER_IN_COMMENTS])
        self.assertFalse(hygiene.REASONS[hygiene.HEADER_IN_COMMENTS].auto_cleanable)

    def test_bare_newline_in_name_is_header_injection(self):
        self.assertIn(hygiene.HEADER_INJECTION, self._codes(name="Jane\nDoe"))

    def test_multiline_comments_are_not_header_injection(self):
        """Lead tables keep multi-line messages in comments; a line break there
        is ordinary text, not an injection attempt."""
        self.assertNotIn(
            hygiene.HEADER_INJECTION,
            self._codes(comments="Hi there,\nWe handle lots of denials."),
        )

    def test_cc_in_prose_is_not_header_injection(self):
        self.assertNotIn(
            hygiene.HEADER_INJECTION,
            self._codes(comments="Please cc: my office manager on replies"),
        )

    def test_zero_width_character_in_name_is_suspicious_unicode(self):
        self.assertIn(hygiene.SUSPICIOUS_UNICODE, self._codes(name="Ja\u200bne"))

    def test_control_character_in_address_is_flagged(self):
        self.assertIn(
            hygiene.CONTROL_IN_EMAIL, self._codes(email="jane\u200b@hospital.org")
        )

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

    def test_duplicate_is_review_only(self):
        """Deleting a duplicate retires the unsubscribe link already mailed from
        it, so duplicates are never part of a bulk delete."""
        make_subscriber("jane@hospital.org")
        make_subscriber("JANE@hospital.org")
        result = hygiene.scan_subscribers()
        self.assertEqual(result.auto_cleanable_findings, [])

    def test_junk_newer_copy_is_not_chosen_as_the_duplicate_keeper(self):
        """When a bot reuses a real address, the person's own older row is the
        one to keep; the newer spam row is the junk."""
        real = make_subscriber("jane@hospital.org", name="Jane Doe")
        spam = make_subscriber("Jane@Hospital.org", name="http://spam.example")

        flagged = {f.record.pk: f for f in hygiene.scan_subscribers().findings}

        self.assertNotIn(real.id, flagged)
        self.assertIn(hygiene.DUPLICATE, flagged[spam.id].codes)

    def test_alias_duplicates_are_review_only(self):
        make_subscriber("jane@gmail.com")
        make_subscriber("ja.ne@gmail.com")

        result = hygiene.scan_subscribers()

        self.assertEqual(result.flagged_count, 2)
        for finding in result.findings:
            self.assertEqual(finding.codes, [hygiene.ALIAS_DUPLICATE])
            self.assertFalse(finding.auto_cleanable)

    def test_junk_copy_of_a_kept_address_is_held_from_bulk_cleanup(self):
        """The junk copy may be the one whose unsubscribe link went out, so
        deleting it while the person's row stays would leave them subscribed
        with a dead link."""
        spam = make_subscriber("jane@hospital.org", name="http://spam.example")
        make_subscriber("Jane@Hospital.org", name="Jane Doe")

        flagged = {f.record.pk: f for f in hygiene.scan_subscribers().findings}

        self.assertTrue(flagged[spam.id].bulk_hold)
        self.assertFalse(flagged[spam.id].auto_cleanable)

    def test_all_junk_copies_of_an_address_stay_bulk_cleanable(self):
        """With no real row left, deleting every copy stops the mail."""
        make_subscriber("bot@spam.org", name="http://spam.example")
        make_subscriber("Bot@spam.org", name="www.spam.example")

        result = hygiene.scan_subscribers()

        self.assertEqual(len(result.auto_cleanable_findings), 2)

    def test_alias_detail_is_bounded_for_large_groups(self):
        """A bot minting many +tag variants must not make every row carry the
        id of every other row."""
        for i in range(20):
            make_subscriber(f"jane+{i}@gmail.com")

        details = [
            f.details[hygiene.ALIAS_DUPLICATE]
            for f in hygiene.scan_subscribers().findings
        ]

        self.assertTrue(all(d.count("#") <= 5 for d in details))
        self.assertTrue(all(d.endswith("and 14 more") for d in details))

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

    def test_unreviewed_only_spares_a_row_kept_since_the_scan(self):
        """Simulates staff pressing Keep between a scan and the delete."""
        subscriber = make_subscriber("someone@example.com")
        ids = [f.record.pk for f in hygiene.scan_subscribers().findings]
        hygiene.mark_subscribers_reviewed([subscriber.id])

        deleted = hygiene.delete_subscribers(ids, unreviewed_only=True)

        self.assertEqual(deleted, 0)
        self.assertTrue(MailingListSubscriber.objects.filter(id=subscriber.id).exists())

    def test_unreviewed_only_takes_a_row_lock(self):
        """SQLite takes no row locks, so a two-connection race can't be tested
        here; assert the lock is requested instead."""
        subscriber = make_subscriber("someone@example.com")
        real = QuerySet.select_for_update
        calls = []

        def spy(qs, *args, **kwargs):
            calls.append(qs)
            return real(qs, *args, **kwargs)

        with patch.object(QuerySet, "select_for_update", spy):
            hygiene.delete_subscribers([subscriber.id], unreviewed_only=True)

        self.assertTrue(calls)

    def test_keep_does_not_overwrite_an_existing_review(self):
        subscriber = make_subscriber("someone@example.com")
        hygiene.mark_subscribers_reviewed([subscriber.id], actor="first")
        subscriber.refresh_from_db()
        first_reviewed_at = subscriber.cleanup_reviewed_at

        hygiene.mark_subscribers_reviewed([subscriber.id], actor="second")

        subscriber.refresh_from_db()
        self.assertEqual(
            (subscriber.cleanup_reviewed_by, subscriber.cleanup_reviewed_at),
            ("first", first_reviewed_at),
        )

    def test_whole_addresses_only_spares_a_partial_address(self):
        """Deleting one copy while another stays would kill the deleted copy's
        unsubscribe link and keep mailing the person."""
        junk = make_subscriber("jane@hospital.org", name="http://spam.example")
        make_subscriber("Jane@Hospital.org", name="Jane Doe")

        deleted = hygiene.delete_subscribers([junk.id], whole_addresses_only=True)

        self.assertEqual(deleted, 0)

    def test_whole_addresses_only_deletes_every_copy_when_all_selected(self):
        first = make_subscriber("bot@spam.org", name="http://spam.example")
        second = make_subscriber("BOT@spam.org", name="www.spam.example")

        deleted = hygiene.delete_subscribers(
            [first.id, second.id], whole_addresses_only=True
        )

        self.assertEqual(deleted, 2)

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

    def test_apply_spares_a_held_junk_copy_unless_forced(self):
        held = make_subscriber("Jane@Hospital.org", name="http://spam.example")

        self._run("--apply")
        self.assertTrue(MailingListSubscriber.objects.filter(id=held.id).exists())

        self._run("--apply", "--force")
        self.assertFalse(MailingListSubscriber.objects.filter(id=held.id).exists())

    def test_narrowed_reasons_never_delete_part_of_an_address(self):
        """Two junk copies of one address with different codes: --reasons for
        one code alone must not delete just that copy."""
        link = make_subscriber("bot@spam.org", name="http://spam.example")
        make_subscriber("Bot@spam.org", name="Bob\nBcc: victim@hospital.org")

        self._run("--apply", "--reasons", hygiene.LINK_IN_FIELDS)
        self.assertTrue(MailingListSubscriber.objects.filter(id=link.id).exists())

        self._run("--apply", "--force", "--reasons", hygiene.LINK_IN_FIELDS)
        self.assertFalse(MailingListSubscriber.objects.filter(id=link.id).exists())

    def test_negative_limit_is_rejected(self):
        with self.assertRaises(CommandError):
            self._run("--limit", "-1")

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
        self.client.post(
            self.url,
            {"action": "delete_selected", "subscriber_id": [str(self.junk.id)]},
        )
        self.assertFalse(MailingListSubscriber.objects.filter(id=self.junk.id).exists())

    def test_delete_selected_redirects_so_refresh_does_not_resubmit(self):
        self.login()
        response = self.client.post(
            self.url,
            {"action": "delete_selected", "subscriber_id": [str(self.junk.id)]},
            follow=True,
        )
        self.assertRedirects(response, self.url)
        self.assertContains(response, "Deleted 1 subscriber(s).")

    def test_selected_actions_ignore_rows_outside_the_queue(self):
        """A stale tab or crafted request naming a clean subscriber must not
        delete it."""
        self.login()
        self.client.post(
            self.url,
            {"action": "delete_selected", "subscriber_id": [str(self.good.id)]},
        )
        self.assertTrue(MailingListSubscriber.objects.filter(id=self.good.id).exists())

    def test_keep_selected_ignores_rows_outside_the_queue(self):
        self.login()
        self.client.post(
            self.url,
            {"action": "keep_selected", "subscriber_id": [str(self.good.id)]},
        )
        self.good.refresh_from_db()
        self.assertIsNone(self.good.cleanup_reviewed_at)

    def test_keep_only_touches_the_rows_ticked(self):
        """Keep must not sweep up other flagged rows (nothing is pre-checked)."""
        self.login()
        self.client.post(
            self.url,
            {"action": "keep_selected", "subscriber_id": [str(self.review_only.id)]},
        )
        self.junk.refresh_from_db()
        self.assertIsNone(self.junk.cleanup_reviewed_at)

    def test_no_row_is_pre_checked(self):
        self.login()
        response = self.client.get(self.url)
        self.assertNotRegex(response.content.decode(), r"<input[^>]*\bchecked\b")

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
        self.assertContains(response, "No subscribers in the queue were selected.")
        self.assertEqual(MailingListSubscriber.objects.count(), 3)

    def test_delete_selected_spares_a_held_copy(self):
        held = make_subscriber("Jane@Hospital.org", name="http://spam.example")
        self.login()
        self.client.post(
            self.url, {"action": "delete_selected", "subscriber_id": [str(held.id)]}
        )
        self.assertTrue(MailingListSubscriber.objects.filter(id=held.id).exists())

    def test_delete_selected_removes_an_address_when_every_copy_is_ticked(self):
        first = make_subscriber("bot@spam.org", name="http://spam.example")
        second = make_subscriber("Bot@spam.org", name="www.spam.example")
        self.login()
        self.client.post(
            self.url,
            {
                "action": "delete_selected",
                "subscriber_id": [str(first.id), str(second.id)],
            },
        )
        self.assertFalse(
            MailingListSubscriber.objects.filter(email__iexact="bot@spam.org").exists()
        )

    def test_bulk_delete_spares_a_held_junk_copy(self):
        held = make_subscriber("Jane@Hospital.org", name="http://spam.example")
        self.login()
        self.client.post(self.url, {"action": "delete_auto", "confirm": "yes"})
        self.assertTrue(MailingListSubscriber.objects.filter(id=held.id).exists())

    def test_lead_scan_failure_does_not_break_the_page(self):
        self.login()
        with patch.object(
            hygiene, "scan_other_contact_tables", side_effect=RuntimeError("boom")
        ):
            response = self.client.get(self.url, {"include_leads": "1"})
        self.assertContains(response, "someone@example.com")

    def test_page_past_the_end_is_clamped_to_the_last_page(self):
        self.login()
        response = self.client.get(self.url, {"page": "99"})
        self.assertEqual(response.context["page_number"], 1)

    def test_later_page_shows_the_rows_after_the_first_page(self):
        self.login()
        page_size = 100  # SubscriberCleanupView.page_size
        for i in range(page_size + 5):
            make_subscriber(f"bot{i}@example.com")
        response = self.client.get(self.url, {"page": "2"})

        self.assertEqual(response.context["page_number"], 2)
        all_flagged = sorted(
            hygiene.scan_subscribers().findings,
            key=lambda f: (not f.auto_cleanable, f.record.pk),
        )
        self.assertEqual(
            [row["id"] for row in response.context["rows"]],
            [f.record.pk for f in all_flagged[page_size:]],
        )

    def test_include_leads_scans_the_other_tables(self):
        self.login()
        response = self.client.get(self.url, {"include_leads": "1"})
        self.assertContains(response, "ChatLeads")


class OtherContactTablesTest(TestCase):
    """The read-only lead-table scans."""

    @staticmethod
    def _pro_findings():
        results = {r.source: r for r in hygiene.scan_other_contact_tables()}
        return {f.record.pk: f for f in results["InterestedProfessional"].findings}

    def test_professional_matching_intake_spam_rules_is_flagged(self):
        pro = InterestedProfessional.objects.create(
            email="sales@agency.org",
            name="Sam Seller",
            comments="We offer SEO services to get you on the first page of Google.",
        )
        self.assertIn(hygiene.PRO_SPAM_RULES, self._pro_findings()[pro.pk].codes)

    def test_genuine_professional_is_not_flagged(self):
        pro = InterestedProfessional.objects.create(
            email="billing@clinic.org",
            name="Pat Biller",
            comments="We see a lot of prior auth denials.\nCan you help?",
        )
        self.assertNotIn(pro.pk, self._pro_findings())

    def test_lead_scans_never_delete_rows(self):
        InterestedProfessional.objects.create(
            email="sales@agency.org", name="http://spam.example"
        )
        hygiene.scan_other_contact_tables()
        self.assertEqual(InterestedProfessional.objects.count(), 1)


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
