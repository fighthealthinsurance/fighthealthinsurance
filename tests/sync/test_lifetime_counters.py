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
    UCRAreaKind,
    UCRGeographicArea,
    UCRLookup,
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

    def test_later_denial_inherits_the_flag_so_deleting_the_first_is_safe(self):
        """A denial created after the person was counted must carry the
        flag: otherwise deleting the older denial by hand (admin) leaves the
        person present but unflagged, and their next draft counts them
        again (review)."""
        a = self._denial()
        ProposedAppeal.objects.create(for_denial=a, appeal_text="first")
        self.assertEqual(_counters()[:2], (1, 1))
        b = self._denial()  # same person, created after the count
        self.assertTrue(Denial.objects.get(pk=b.pk).person_counted)
        Denial.objects.filter(pk=a.pk).delete()
        ProposedAppeal.objects.create(for_denial=b, appeal_text="later")
        self.assertEqual(_counters()[:2], (2, 1))

    def test_unflagged_sibling_is_flagged_on_the_next_draft(self):
        """The race the row lock serializes can still leave one row
        unflagged if the two commits interleave the other way; the next
        draft repairs it without counting the person again."""
        a = self._denial()
        b = self._denial()
        Denial.objects.filter(pk=a.pk).update(person_counted=True)  # as if raced
        ProposedAppeal.objects.create(for_denial=b, appeal_text="draft")
        self.assertEqual(_counters()[:2], (1, 0))
        self.assertTrue(Denial.objects.get(pk=b.pk).person_counted)
        Denial.objects.filter(pk=a.pk).delete()
        ProposedAppeal.objects.create(for_denial=b, appeal_text="again")
        self.assertEqual(_counters()[:2], (2, 0))

    def test_row_moving_to_another_hash_adopts_that_persons_state(self):
        """The hash is the person. A flagged row re-keyed to an uncounted
        person must not suppress that person's first count, and an
        unflagged row re-keyed into a counted person must be flagged, or
        deleting that person's older denial lets them count again (review)."""
        a = self._denial()
        ProposedAppeal.objects.create(for_denial=a, appeal_text="first")
        self.assertEqual(_counters()[:2], (1, 1))
        a = Denial.objects.get(pk=a.pk)
        a.hashed_email = "person-q"  # the edit form can change the email
        a.save()
        self.assertFalse(Denial.objects.get(pk=a.pk).person_counted)
        ProposedAppeal.objects.create(for_denial=a, appeal_text="q first")
        self.assertEqual(_counters()[:2], (2, 2))
        stray = self._denial("person-r")  # unflagged, no draft yet
        stray = Denial.objects.get(pk=stray.pk)
        stray.hashed_email = "person-q"
        stray.save()
        self.assertTrue(Denial.objects.get(pk=stray.pk).person_counted)
        Denial.objects.filter(pk=a.pk).delete()
        ProposedAppeal.objects.create(for_denial=stray, appeal_text="q again")
        self.assertEqual(_counters()[:2], (3, 2))
        stray.hashed_email = ""
        stray.save(update_fields=["hashed_email"])
        self.assertFalse(Denial.objects.get(pk=stray.pk).person_counted)

    def test_second_stale_rekey_keeps_the_counted_flag(self):
        """Two instances hold the same denial under A. The first moves it
        to B and B is counted. The second then saves its own A->B edit:
        the row already belongs to B, so that is not a move and B's flag
        must survive, or B's next draft counts B again (review)."""
        first = self._denial()
        second = Denial.objects.get(pk=first.pk)
        first.hashed_email = "person-b"
        first.save()
        ProposedAppeal.objects.create(for_denial=first, appeal_text="b first")
        self.assertEqual(_counters()[:2], (1, 1))
        second.hashed_email = "person-b"
        second.save()
        self.assertTrue(Denial.objects.get(pk=first.pk).person_counted)
        ProposedAppeal.objects.create(for_denial=first, appeal_text="b again")
        self.assertEqual(_counters()[:2], (2, 1))

    def test_partial_save_does_not_move_the_baseline(self):
        """Assigning a new hash and saving other fields first must not make
        the later hash save look like a no-op (review)."""
        a = self._denial()
        ProposedAppeal.objects.create(for_denial=a, appeal_text="first")
        a = Denial.objects.get(pk=a.pk)
        a.hashed_email = "person-b"
        a.denial_text = "edited"
        a.save(update_fields=["denial_text"])
        row = Denial.objects.get(pk=a.pk)
        self.assertEqual((row.hashed_email, row.person_counted), ("person-a", True))
        a.save(update_fields=["hashed_email"])
        row = Denial.objects.get(pk=a.pk)
        self.assertEqual((row.hashed_email, row.person_counted), ("person-b", False))
        ProposedAppeal.objects.create(for_denial=a, appeal_text="b first")
        self.assertEqual(_counters()[:2], (2, 2))

    def test_refreshed_instance_does_not_undo_a_move(self):
        original = self._denial()
        mover = Denial.objects.get(pk=original.pk)
        mover.hashed_email = "person-b"
        mover.save()
        ProposedAppeal.objects.create(for_denial=mover, appeal_text="b first")
        original.refresh_from_db()
        self.assertEqual(original.hashed_email, "person-b")
        original.denial_text = "edited later"
        original.save()  # persisted hash == instance hash: not a move
        self.assertTrue(Denial.objects.get(pk=original.pk).person_counted)
        ProposedAppeal.objects.create(for_denial=original, appeal_text="b again")
        self.assertEqual(_counters()[:2], (2, 1))

    def test_draft_counts_the_denials_persisted_person_not_a_cached_one(self):
        """The speculative precompute holds a denial instance; if another
        request changed its email meanwhile, the draft belongs to the new
        person and must count them, not the old hash (review)."""
        cached = self._denial()
        mover = Denial.objects.get(pk=cached.pk)
        mover.hashed_email = "person-b"
        mover.save()
        ProposedAppeal.objects.create(for_denial=cached, appeal_text="from cache")
        self.assertEqual(_counters()[:2], (1, 1))
        self.assertTrue(Denial.objects.get(pk=cached.pk).person_counted)
        ProposedAppeal.objects.create(for_denial=mover, appeal_text="b again")
        self.assertEqual(_counters()[:2], (2, 1))

    def _stale_then_real(self, stale):
        """_persisted_hash that lies once, the way a read can be overtaken
        by another request's re-key before this writer takes the lock."""
        real = lifetime_counters._persisted_hash
        reads = []

        def read(denial_id):
            reads.append(denial_id)
            return stale if len(reads) == 1 else real(denial_id)

        return reads, read

    def test_rekey_between_read_and_lock_goes_round_to_the_new_person(self):
        x = self._denial()
        sibling = self._denial()  # stays with person-a, gets locked first
        Denial.objects.filter(pk=x.pk).update(hashed_email="person-b")
        reads, read = self._stale_then_real("person-a")
        with mock.patch.object(lifetime_counters, "_persisted_hash", new=read):
            self.assertTrue(lifetime_counters._mark_person(x.pk))
        self.assertEqual(len(reads), 2)  # read again after the mismatch
        self.assertTrue(Denial.objects.get(pk=x.pk).person_counted)
        # person-a was locked but never flagged: the denial had left them
        self.assertFalse(Denial.objects.get(pk=sibling.pk).person_counted)

    def test_retry_never_waits_for_the_new_persons_lock(self):
        """The retry holds the previous person's locks, so it must take the
        new person's lock with the try variant and give up if it is busy;
        waiting there deadlocks against a re-key going the other way."""
        x = self._denial()
        Denial.objects.filter(pk=x.pk).update(hashed_email="person-b")
        reads, read = self._stale_then_real("person-a")
        blocking = []
        real_lock = lifetime_counters.person_lock

        def lock(hashed, *, blocking_flag=None, **kwargs):
            blocking.append(kwargs.get("blocking", True))
            return real_lock(hashed, **kwargs)

        with mock.patch.object(
            lifetime_counters, "_persisted_hash", new=read
        ), mock.patch.object(lifetime_counters, "person_lock", new=lock):
            self.assertTrue(lifetime_counters._mark_person(x.pk))
        self.assertEqual(blocking, [True, False])  # first blocks, retry does not

    def test_busy_lock_after_a_rekey_counts_the_draft_but_not_the_person(self):
        x = self._denial()
        Denial.objects.filter(pk=x.pk).update(hashed_email="person-b")
        reads, read = self._stale_then_real("person-a")
        with mock.patch.object(
            lifetime_counters, "_persisted_hash", new=read
        ), mock.patch.object(
            lifetime_counters, "person_lock", side_effect=[True, False]
        ):
            ProposedAppeal.objects.create(for_denial=x, appeal_text="draft")
        # The draft still counts; only the person is given up on, logged.
        self.assertEqual(_counters()[:2], (1, 0))
        self.assertFalse(Denial.objects.get(pk=x.pk).person_counted)

    def test_deferred_load_still_detects_a_hash_change(self):
        a = self._denial()
        ProposedAppeal.objects.create(for_denial=a, appeal_text="first")
        thin = Denial.objects.only("denial_text").get(pk=a.pk)
        thin.hashed_email = "person-q"
        thin.save()
        self.assertFalse(Denial.objects.get(pk=a.pk).person_counted)

    def test_every_flag_writer_takes_the_person_lock(self):
        def locked(lock):
            return [call.args[0] for call in lock.call_args_list]

        with mock.patch.object(
            lifetime_counters, "person_lock", return_value=True
        ) as lock:
            d = self._denial()
            self.assertEqual(locked(lock), ["person-a"])
            lock.reset_mock()
            self._denial("")  # no person, nothing to serialize
            lock.assert_not_called()
            ProposedAppeal.objects.create(for_denial=d, appeal_text="draft")
            self.assertEqual(locked(lock), ["person-a"])
            self.assertEqual(lock.call_args.kwargs, {"blocking": True})
            lock.reset_mock()
            d = Denial.objects.get(pk=d.pk)
            d.denial_text = "edited"
            d.save()  # a full save writes the hash: same lock, flag untouched
            self.assertEqual(locked(lock), ["person-a"])
            lock.reset_mock()
            d.save(update_fields=["denial_text"])  # hash not written: no lock
            lock.assert_not_called()
            d.hashed_email = "person-q"
            d.save()
            self.assertEqual(locked(lock), ["person-q"])

    def test_person_lock_is_a_postgres_advisory_lock(self):
        key = lifetime_counters._advisory_key("person-a")
        self.assertEqual(key, lifetime_counters._advisory_key("person-a"))
        self.assertNotEqual(key, lifetime_counters._advisory_key("person-b"))
        self.assertTrue(-(2**63) <= key < 2**63)
        conn = mock.MagicMock(vendor="postgresql")
        cursor = conn.cursor.return_value.__enter__.return_value
        with mock.patch.object(lifetime_counters, "connection", conn):
            lifetime_counters.person_lock("person-a")
        cursor.execute.assert_called_once_with(
            "SELECT pg_advisory_xact_lock(%s)", [key]
        )
        with mock.patch.object(lifetime_counters, "connection", conn):
            lifetime_counters.person_lock("")
        cursor.execute.assert_called_once()  # blank hash: no lock

    def test_creation_still_enforces_the_ucr_owner_guard(self):
        owner = self._denial()
        area = UCRGeographicArea.objects.create(kind=UCRAreaKind.ZIP3, code="941")
        lookup = UCRLookup.objects.create(
            denial=owner, procedure_code="99213", matched_area=area, rates_snapshot=[]
        )
        with self.assertRaises(ValueError):
            Denial(
                semi_sekret="s", hashed_email="person-a", latest_ucr_lookup=lookup
            ).save()

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
    def test_stale_full_save_keeps_the_markers(self, *_):
        """resend/precheck/remote_send_fax load the row, then save() it in
        full. If that save interleaves with a finalize on another worker
        the stale False markers must not be written back (review)."""
        fax = self._fax()
        stale = FaxesToSend.objects.get(pk=fax.pk)  # loaded before finalize
        self._finalize(fax, True)
        self.assertEqual(_counters()[2:], (1, 1))
        stale.destination = "(555) 555-0102"
        stale.sent = False  # what SendFaxHelper.resend writes
        stale.save()
        fresh = FaxesToSend.objects.get(pk=fax.pk)
        self.assertEqual(fresh.destination, "(555) 555-0102")
        self.assertFalse(fresh.sent)
        self.assertTrue(fresh.attempt_counted and fresh.delivery_counted)
        self._finalize(fresh, True)
        self.assertEqual(_counters()[2:], (1, 1))

    def test_markers_are_not_on_the_admin_form(self):
        from django.contrib import admin
        from django.test import RequestFactory

        from django.contrib.auth import get_user_model

        request = RequestFactory().get("/")
        request.user = get_user_model().objects.create_superuser(
            "staff", "s@example.com", "pw"
        )
        fax_form = admin.site._registry[FaxesToSend].get_form(request)
        for name in FaxesToSend.LIFETIME_MARKERS:
            self.assertNotIn(name, fax_form.base_fields)
        denial_form = admin.site._registry[Denial].get_form(request)
        self.assertNotIn("person_counted", denial_form.base_fields)

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


class DeletionTotalsTest(TestCase):
    """RemoveDataHelper's own bookkeeping, which is not a lifetime counter:
    it counts what deletion took, and must never block a deletion."""

    EMAIL = "person@example.com"

    def _denial(self):
        return Denial.objects.create(
            semi_sekret="s", hashed_email=Denial.get_hashed_email(self.EMAIL)
        )

    def _totals(self):
        from fighthealthinsurance.models import DataRemovalTotals

        row = DataRemovalTotals.objects.filter(
            pk=DataRemovalTotals.SINGLETON_ID
        ).first()
        return (row.requests, row.denials) if row else (0, 0)

    def test_totals_count_the_denials_the_delete_actually_removed(self):
        """A denial created after a pre-delete count would be deleted and
        never counted; the number comes from the delete itself (review)."""
        from fighthealthinsurance.helpers.data_helpers import RemoveDataHelper

        self._denial()
        late = None

        real_delete = RemoveDataHelper._delete_rows.__func__

        def delete_rows(cls, email, hashed_email):
            nonlocal late
            late = Denial.objects.create(  # lands after any pre-count
                semi_sekret="s", hashed_email=hashed_email
            )
            return real_delete(cls, email, hashed_email)

        with mock.patch.object(
            RemoveDataHelper, "_delete_rows", classmethod(delete_rows)
        ):
            RemoveDataHelper.remove_data_for_email(self.EMAIL)
        self.assertIsNotNone(late)
        self.assertEqual(self._totals(), (1, 2))
        self.assertFalse(Denial.objects.exists())

    def test_deletion_locks_the_person_before_touching_their_rows(self):
        """The cascade locks denial rows one at a time; taking the person's
        lock first is what keeps it from deadlocking a count (review)."""
        from fighthealthinsurance.helpers.data_helpers import RemoveDataHelper

        d = self._denial()
        order = []
        with mock.patch.object(
            lifetime_counters,
            "person_lock",
            side_effect=lambda h, **kw: order.append(("lock", h)) or True,
        ), mock.patch.object(
            RemoveDataHelper,
            "_delete_rows",
            classmethod(
                lambda cls, email, hashed: order.append(("delete", hashed)) or 0
            ),
        ):
            RemoveDataHelper.remove_data_for_email(self.EMAIL)
        self.assertEqual(order, [("lock", d.hashed_email), ("delete", d.hashed_email)])

    def test_bookkeeping_failure_never_blocks_a_deletion(self):
        from fighthealthinsurance.helpers.data_helpers import RemoveDataHelper
        from fighthealthinsurance.models import DataRemovalTotals

        self._denial()
        with mock.patch.object(
            DataRemovalTotals.objects,
            "get_or_create",
            side_effect=DatabaseError("no table"),
        ):
            RemoveDataHelper.remove_data_for_email(self.EMAIL)
        self.assertFalse(Denial.objects.exists())


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
        # The seed marked the present fax as it counted it, so a resend of
        # that fax after the migration counts nothing again.
        seeded = FaxesToSend.objects.get(hashed_email="p")
        self.assertTrue(seeded.attempt_counted and seeded.delivery_counted)
        with mock.patch(
            "fighthealthinsurance.fax_send_core.send_fax_status_notification"
        ), mock.patch("fighthealthinsurance.fax_send_core.EmailMultiAlternatives"):
            fax_send_core.finalize_fax(seeded, True, False)
        self.assertEqual(_counters(), (2, 1, 1, 1))
