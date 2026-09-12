"""The lifetime counters only ever go up, and move exactly when a draft is
generated, a person gets their first generated draft, or a fax is delivered."""

from unittest import mock

from django.db import DatabaseError
from django.test import TestCase

from fighthealthinsurance import fax_send_core, lifetime_counters
from fighthealthinsurance.models import (
    Denial,
    FaxesToSend,
    LifetimeCounters,
    ProposedAppeal,
)


def _counters():
    row = LifetimeCounters.objects.filter(pk=LifetimeCounters.SINGLETON_ID).first()
    if row is None:
        return (0, 0, 0, 0)
    return (
        row.appeals_generated,
        row.people_with_draft,
        row.faxes_delivered,
        row.faxes_sent,
    )


class DraftCounterTest(TestCase):
    def _denial(self, hashed="person-a"):
        return Denial.objects.create(semi_sekret="s", hashed_email=hashed)

    def test_generated_drafts_and_first_person_count(self):
        d1 = self._denial()
        ProposedAppeal.objects.create(for_denial=d1, appeal_text="first draft")
        self.assertEqual(_counters()[:3], (1, 1, 0))
        ProposedAppeal.objects.create(for_denial=d1, appeal_text="second draft")
        self.assertEqual(_counters()[:3], (2, 1, 0))  # same person, once
        d2 = self._denial()  # same person, another denial
        ProposedAppeal.objects.create(for_denial=d2, appeal_text="third draft")
        self.assertEqual(_counters()[:3], (3, 1, 0))
        ProposedAppeal.objects.create(
            for_denial=self._denial("person-b"), appeal_text="b draft"
        )
        self.assertEqual(_counters()[:3], (4, 2, 0))

    def test_speculative_precompute_counts_as_generated(self):
        ProposedAppeal.objects.create(
            for_denial=self._denial(), appeal_text="held back", speculative=True
        )
        self.assertEqual(_counters()[:3], (1, 1, 0))

    def test_chosen_copy_is_a_pick_not_a_generation(self):
        d = self._denial()
        ProposedAppeal.objects.create(for_denial=d, appeal_text="draft")
        ProposedAppeal.objects.create(for_denial=d, appeal_text="draft", chosen=True)
        self.assertEqual(_counters()[:3], (1, 1, 0))

    def test_blank_hash_is_a_draft_but_not_a_person(self):
        ProposedAppeal.objects.create(for_denial=self._denial(""), appeal_text="x")
        self.assertEqual(_counters()[:3], (1, 0, 0))

    def test_update_does_not_count_again(self):
        row = ProposedAppeal.objects.create(for_denial=self._denial(), appeal_text="x")
        row.appeal_text = "edited"
        row.save()
        ProposedAppeal.objects.filter(pk=row.pk).update(speculative=False)
        self.assertEqual(_counters()[:3], (1, 1, 0))

    def test_counter_failure_never_blocks_the_draft_save(self):
        with mock.patch.object(
            lifetime_counters, "_add", side_effect=DatabaseError("no table")
        ):
            row = ProposedAppeal.objects.create(
                for_denial=self._denial(), appeal_text="still saved"
            )
        self.assertTrue(ProposedAppeal.objects.filter(pk=row.pk).exists())
        self.assertEqual(_counters()[:3], (0, 0, 0))

    def test_chosen_only_history_does_not_hide_a_first_generation(self):
        """Someone who only ever submitted their own text (a chosen copy via
        the share flow) and later gets their first generated draft is a new
        person for the counter (review)."""
        d = self._denial()
        ProposedAppeal.objects.create(for_denial=d, appeal_text="own text", chosen=True)
        self.assertEqual(_counters()[:3], (0, 0, 0))
        ProposedAppeal.objects.create(
            for_denial=self._denial(), appeal_text="generated"
        )
        self.assertEqual(_counters()[:3], (1, 1, 0))

    def test_deleting_the_person_leaves_the_counters(self):
        d = self._denial()
        ProposedAppeal.objects.create(for_denial=d, appeal_text="x")
        d.delete()
        self.assertEqual(_counters()[:3], (1, 1, 0))

    def test_text_change_dropping_speculative_drafts_does_not_recount(self):
        """Replacing the denial text deletes the precompute's drafts; the
        next generation for that person must not count them again (review)."""
        d = self._denial()
        ProposedAppeal.objects.create(
            for_denial=d, appeal_text="reserve", speculative=True
        )
        self.assertEqual(_counters()[:3], (1, 1, 0))
        ProposedAppeal.objects.filter(for_denial=d, speculative=True).delete()
        ProposedAppeal.objects.create(for_denial=d, appeal_text="fresh draft")
        self.assertEqual(_counters()[:3], (2, 1, 0))

    def test_flag_survives_draft_churn_but_leaves_with_the_data(self):
        from fighthealthinsurance.helpers.data_helpers import RemoveDataHelper

        email = "back@example.com"
        hashed = Denial.get_hashed_email(email)
        d1 = self._denial(hashed)
        d2 = self._denial(hashed)
        ProposedAppeal.objects.create(for_denial=d1, appeal_text="first")
        flags = Denial.objects.filter(hashed_email=hashed).values_list(
            "person_counted", flat=True
        )
        self.assertEqual(list(flags), [True, True])
        ProposedAppeal.objects.filter(for_denial=d1).delete()  # churn
        ProposedAppeal.objects.create(for_denial=d2, appeal_text="again")
        self.assertEqual(_counters()[:2], (2, 1))
        RemoveDataHelper.remove_data_for_email(email)
        self.assertFalse(Denial.objects.filter(hashed_email=hashed).exists())
        self.assertEqual(_counters()[:2], (2, 1))  # the counter keeps them
        # A person who returns after deletion starts unflagged: counted again,
        # documented.
        ProposedAppeal.objects.create(
            for_denial=self._denial(hashed), appeal_text="new"
        )
        self.assertEqual(_counters()[:2], (3, 2))

    def test_stale_full_save_does_not_clear_the_flag(self):
        """An instance loaded before the flag flipped, then saved in full
        (edit paths do this), must not write the stale False back (review)."""
        d = self._denial()
        stale = Denial.objects.get(pk=d.pk)
        ProposedAppeal.objects.create(for_denial=d, appeal_text="first")
        self.assertTrue(Denial.objects.get(pk=d.pk).person_counted)
        stale.denial_text = "edited later"
        stale.save()
        fresh = Denial.objects.get(pk=d.pk)
        self.assertTrue(fresh.person_counted)
        self.assertEqual(fresh.denial_text, "edited later")
        ProposedAppeal.objects.create(for_denial=d, appeal_text="second")
        self.assertEqual(_counters()[:2], (2, 1))

    def test_receiver_after_deletion_creates_nothing(self):
        """A draft insert committed just before the person's deletion, with
        the receiver running after it: no denial rows remain to flag, so no
        identifier reappears and no person is counted (review)."""
        from django.db.models.signals import post_save

        d = self._denial("gone")
        draft = ProposedAppeal(for_denial=d, appeal_text="late")
        post_save.disconnect(
            sender=ProposedAppeal, dispatch_uid="lifetime_counters_draft_saved"
        )
        try:
            draft.save()
        finally:
            post_save.connect(
                lifetime_counters._on_draft_saved,
                sender=ProposedAppeal,
                dispatch_uid="lifetime_counters_draft_saved",
            )
        self.assertEqual(_counters()[:2], (0, 0))  # the receiver did not run yet
        Denial.objects.filter(pk=d.pk).delete()
        with mock.patch.object(lifetime_counters, "_add") as add:
            lifetime_counters._on_draft_saved(ProposedAppeal, draft, created=True)
        add.assert_called_once_with(appeals_generated=1, people_with_draft=0)
        self.assertFalse(Denial.objects.filter(hashed_email="gone").exists())


class FaxCounterTest(TestCase):
    def _fax(self):
        return FaxesToSend.objects.create(
            hashed_email="h",
            paid=True,
            email="a@b.com",
            appeal_text="x",
            name="Test",
            destination="(555) 555-0100",
        )

    def _finalize(self, fax, success):
        fax_send_core.finalize_fax(fax, success, False)
        fax.refresh_from_db()
        return fax

    @mock.patch("fighthealthinsurance.fax_send_core.send_fax_status_notification")
    @mock.patch("fighthealthinsurance.fax_send_core.EmailMultiAlternatives")
    @mock.patch("fighthealthinsurance.helpers.fax_helpers._dispatch_or_ray_fax")
    def test_real_resend_after_failure_does_not_count_sent_twice(self, *_):
        """SendFaxHelper.resend resets sent=False on the row; the lifetime
        "sent" must still count that fax once (review)."""
        from fighthealthinsurance.helpers.fax_helpers import SendFaxHelper

        fax = self._finalize(self._fax(), False)
        self.assertEqual(_counters()[2:], (0, 1))  # delivered, sent
        SendFaxHelper.resend("(555) 555-0101", str(fax.uuid), fax.hashed_email)
        fax.refresh_from_db()
        self.assertFalse(fax.sent)  # the row's latest-attempt flag was reset
        self.assertTrue(fax.attempt_counted)  # the once-only marker was not
        self._finalize(fax, True)
        self.assertEqual(_counters()[2:], (1, 1))

    @mock.patch("fighthealthinsurance.fax_send_core.send_fax_status_notification")
    @mock.patch("fighthealthinsurance.fax_send_core.EmailMultiAlternatives")
    def test_failure_after_delivery_cannot_count_delivery_again(self, *_):
        fax = self._finalize(self._fax(), True)
        self.assertEqual(_counters()[2:], (1, 1))
        fax = self._finalize(fax, False)  # e.g. resent to another number, failed
        self.assertFalse(fax.fax_success)  # latest outcome
        self.assertTrue(fax.delivery_counted)  # counted once, for life
        self._finalize(fax, True)
        self.assertEqual(_counters()[2:], (1, 1))

    @mock.patch("fighthealthinsurance.fax_send_core.send_fax_status_notification")
    @mock.patch("fighthealthinsurance.fax_send_core.EmailMultiAlternatives")
    def test_delivered_fax_counts_once_and_failed_send_does_not(self, *_):
        fax = self._fax()
        fax_send_core.finalize_fax(fax, True, False)
        self.assertEqual(_counters()[2:], (1, 1))  # delivered, sent
        # finalize runs under an unlimited retry policy: a retry on the same
        # delivered fax must not count it again (review).
        fax_send_core.finalize_fax(fax, True, False)
        self.assertEqual(_counters()[2:], (1, 1))
        failed = self._fax()
        fax_send_core.finalize_fax(failed, False, False)
        self.assertEqual(_counters()[2:], (1, 2))  # sent, not delivered
        # A later resend of that failed fax that succeeds: delivered once,
        # not sent again (the attempt transition already happened).
        fax_send_core.finalize_fax(failed, True, False)
        self.assertEqual(_counters()[2:], (2, 2))

    @mock.patch("fighthealthinsurance.fax_send_core.send_fax_status_notification")
    @mock.patch("fighthealthinsurance.fax_send_core.EmailMultiAlternatives")
    def test_gone_row_does_not_count(self, *_):
        fax = self._fax()
        FaxesToSend.objects.filter(pk=fax.pk).delete()
        fax_send_core.finalize_fax(fax, True, False)
        self.assertEqual(_counters()[2:], (0, 0))

    @mock.patch("fighthealthinsurance.fax_send_core.send_fax_status_notification")
    @mock.patch("fighthealthinsurance.fax_send_core.EmailMultiAlternatives")
    def test_counter_failure_never_blocks_finalize(self, *_):
        fax = self._fax()
        with mock.patch.object(
            lifetime_counters, "_add", side_effect=DatabaseError("no table")
        ):
            fax_send_core.finalize_fax(fax, True, False)
        fax.refresh_from_db()
        self.assertTrue(fax.sent and fax.fax_success)


class SeedTest(TestCase):
    def test_seed_from_present_rows_once(self):
        from django.apps import apps

        d = Denial.objects.create(semi_sekret="s", hashed_email="p")
        # Rows that predate the counters: neither the add nor the marker ran.
        with mock.patch.object(lifetime_counters, "_add"), mock.patch.object(
            lifetime_counters, "_mark_person", return_value=False
        ):
            ProposedAppeal.objects.create(for_denial=d, appeal_text="a")
            ProposedAppeal.objects.create(for_denial=d, appeal_text="b", chosen=True)
            ProposedAppeal.objects.create(
                for_denial=Denial.objects.create(semi_sekret="s", hashed_email=""),
                appeal_text="c",
            )
        FaxesToSend.objects.create(
            hashed_email="p",
            paid=True,
            email="a@b.com",
            appeal_text="x",
            name="T",
            sent=True,
            fax_success=True,
        )
        # The migration seeds the row when the test database is built from
        # empty (all zeros); clear it so this test exercises the seed itself.
        LifetimeCounters.objects.all().delete()
        Denial.objects.update(person_counted=False)
        lifetime_counters.seed_from_present_rows(apps, None)
        # chosen copy excluded, blank hash no person; one fax present, sent
        # and delivered.
        self.assertEqual(_counters(), (2, 1, 1, 1))
        flagged = Denial.objects.filter(person_counted=True).values_list(
            "hashed_email", flat=True
        )
        self.assertEqual(list(flagged), ["p"])
        lifetime_counters.seed_from_present_rows(apps, None)  # idempotent
        self.assertEqual(_counters(), (2, 1, 1, 1))
