"""The serving registry: one row per distinct answer from a backend leg,
drafts pointing at the row current for their backend, and nothing on the
draft path ever waiting on the database."""

import datetime
import threading
import time
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from fighthealthinsurance.common_view_logic import mark_proposal_chosen
from fighthealthinsurance.ml import serving_registry as sr
from fighthealthinsurance.models import Denial, ProposedAppeal, ServingIdentity

BACKEND = "AlphaRemoteInternal(fhi-local @ 10.0.0.5:8000)"
TWO_LEGS = (
    "AlphaRemoteInternal(fhi-local @ 10.0.0.5:8000) "
    "+backup(/app/model @ 10.0.0.6:8000)"
)
CARD = {
    "endpoint": "10.0.0.5:8000",
    "model_id": "fhi-local",
    "weights": "/models/gemma-4-26b-a4b-it-awq",
    "parent": "",
    "max_model_len": 32768,
    "owned_by": "vllm",
}
BACKUP_CARD = {**CARD, "endpoint": "10.0.0.6:8000", "model_id": "/app/model"}


class _Backend:
    """A backend as the sweep leaves it: a card from this round per leg."""

    def __init__(self, label, card, backup=None, legs=1):
        self.label = label
        self.last_model_card = card
        self.last_backup_model_card = backup
        self.legs = legs
        self.token = None

    def backend_descriptor(self):
        return self.label

    def serving_legs(self):
        legs = [("primary", "http://10.0.0.5:8000/v1", "fhi-local")]
        if self.legs > 1:
            legs.append(("backup", "http://10.0.0.6:8000/v1", "/app/model"))
        return legs


class RegistryTestCase(TestCase):
    def setUp(self):
        sr.reset_serving_registry_cache()

    def tearDown(self):
        sr.reset_serving_registry_cache()

    def lookup(self, backend):
        return async_to_sync(sr.aserving_id_for)(backend)


class RecordTest(RegistryTestCase):
    def test_the_same_answer_is_one_row(self):
        first = sr.record(BACKEND, CARD)
        second = sr.record(BACKEND, dict(CARD))
        self.assertEqual(first, second)
        self.assertEqual(ServingIdentity.objects.count(), 1)

    def test_new_weights_behind_the_same_name_are_a_new_row(self):
        first = sr.record(BACKEND, CARD)
        second = sr.record(BACKEND, {**CARD, "weights": "/models/gemma-4-26b-q8"})
        self.assertNotEqual(first, second)

    def test_an_unchanged_answer_moves_last_seen_only_after_a_while(self):
        row_id = sr.record(BACKEND, CARD)
        recent = timezone.now() - datetime.timedelta(minutes=1)
        ServingIdentity.objects.filter(pk=row_id).update(last_seen=recent)
        sr.record(BACKEND, CARD)
        self.assertEqual(ServingIdentity.objects.get(pk=row_id).last_seen, recent)
        old = timezone.now() - datetime.timedelta(hours=1)
        ServingIdentity.objects.filter(pk=row_id).update(last_seen=old)
        sr.record(BACKEND, CARD)
        self.assertGreater(ServingIdentity.objects.get(pk=row_id).last_seen, old)

    def test_going_back_to_an_earlier_answer_makes_it_current_at_once(self):
        a = sr.record(BACKEND, CARD)
        b = sr.record(BACKEND, {**CARD, "weights": "/models/next"})
        a_again = sr.record(BACKEND, CARD)
        self.assertEqual(a, a_again)
        self.assertEqual(sr.load_current_rows()[BACKEND], a)


class RecordBackendsTest(RegistryTestCase):
    def test_a_single_leg_backend_attributes_its_row(self):
        sr.record_backends([_Backend(BACKEND, CARD)])
        row = ServingIdentity.objects.get()
        self.assertEqual(self.lookup(BACKEND), row.pk)

    def test_two_legs_serving_the_same_weights_attribute_the_primary(self):
        sr.record_backends([_Backend(TWO_LEGS, CARD, BACKUP_CARD, legs=2)])
        primary = ServingIdentity.objects.get(endpoint="10.0.0.5:8000")
        self.assertEqual(ServingIdentity.objects.count(), 2)
        self.assertEqual(self.lookup(TWO_LEGS), primary.pk)

    def test_two_legs_serving_different_weights_attribute_nothing(self):
        backup = {**BACKUP_CARD, "weights": "/models/older-build"}
        sr.record_backends([_Backend(TWO_LEGS, CARD, backup, legs=2)])
        self.assertEqual(ServingIdentity.objects.count(), 2)
        self.assertIsNone(self.lookup(TWO_LEGS))
        self.assertIsNone(sr.load_current_rows()[TWO_LEGS])

    def test_a_backup_leg_that_cannot_be_read_attributes_nothing(self):
        with patch(
            "fighthealthinsurance.ml.ml_models.fetch_model_card", return_value=None
        ) as fetch:
            sr.record_backends([_Backend(TWO_LEGS, CARD, None, legs=2)])
        fetch.assert_called_once()
        self.assertIsNone(self.lookup(TWO_LEGS))

    def test_a_backend_whose_check_failed_this_round_is_not_recorded(self):
        sr.record_backends([_Backend(BACKEND, None)])
        self.assertFalse(ServingIdentity.objects.exists())

    def test_a_failed_check_withdraws_the_attribution_at_once(self):
        backend = _Backend(BACKEND, CARD)
        sr.record_backends([backend])
        self.assertIsNotNone(self.lookup(BACKEND))
        backend.last_model_card = None
        sr.record_backends([backend])
        self.assertIsNone(self.lookup(BACKEND))

    def test_a_round_that_could_not_be_recorded_withdraws_the_attribution(self):
        sr.record_backends([_Backend(BACKEND, CARD)])
        with patch.object(sr, "record", side_effect=RuntimeError("db down")):
            sr.record_backends([_Backend(BACKEND, {**CARD, "weights": "/new"})])
        self.assertIsNone(self.lookup(BACKEND))

    def test_one_bad_backend_never_stops_the_others(self):
        bad = _Backend("Broken(x @ y)", {**CARD, "max_model_len": "not a number"})
        sr.record_backends([bad, _Backend(BACKEND, CARD), object()])
        self.assertEqual(
            list(ServingIdentity.objects.values_list("backend", flat=True)),
            [BACKEND],
        )


class LookupTest(RegistryTestCase):
    def test_a_lookup_never_queries_the_database(self):
        sr.record_backends([_Backend(BACKEND, CARD)])
        sr.reset_serving_registry_cache()
        with CaptureQueriesContext(connection) as queries:
            self.assertIsNone(self.lookup(BACKEND))
            self.assertIsNone(self.lookup("Unknown(x @ y)"))
        self.assertEqual(len(queries), 0)

    def test_the_loader_supplies_rows_this_process_did_not_record(self):
        sr.record_backends([_Backend(BACKEND, CARD)])
        row_id = ServingIdentity.objects.get().pk
        sr.reset_serving_registry_cache()
        with sr._lock:
            sr._loaded.update(sr.load_current_rows())
        self.assertEqual(self.lookup(BACKEND), row_id)

    def test_the_loader_ignores_legs_not_seen_for_hours(self):
        old = sr.record(TWO_LEGS, {**BACKUP_CARD, "weights": "/models/gone"})
        ServingIdentity.objects.filter(pk=old).update(
            last_seen=timezone.now() - datetime.timedelta(days=1)
        )
        primary = sr.record(TWO_LEGS, CARD)
        self.assertEqual(sr.load_current_rows()[TWO_LEGS], primary)

    def test_no_backend_means_no_row(self):
        self.assertIsNone(self.lookup(""))

    def test_no_attribution_from_the_sweep_never_ages_into_the_loaders(self):
        # The sweep saw the check fail, then stopped refreshing this backend
        # (a skipped round, a stopped timer). The loader may still hold an
        # older row; the sweep's None must win however old it is.
        row_id = sr.record(BACKEND, CARD)
        long_ago = time.monotonic() - sr.SWEEP_TRUST_SECONDS - 1
        with sr._lock:
            sr._from_sweep[BACKEND] = (None, long_ago)
            sr._loaded[BACKEND] = row_id
        self.assertIsNone(self.lookup(BACKEND))

    def test_an_attribution_from_the_sweep_gives_way_to_the_loader_in_time(self):
        sr.record_backends([_Backend(BACKEND, CARD)])
        with sr._lock:
            row_id, at = sr._from_sweep[BACKEND]
            sr._from_sweep[BACKEND] = (row_id, at - sr.SWEEP_TRUST_SECONDS - 1)
            sr._loaded[BACKEND] = None
        self.assertIsNone(self.lookup(BACKEND))


class RecordInTheBackgroundTest(RegistryTestCase):
    def test_tests_and_other_no_background_configs_record_nothing(self):
        with patch.object(sr, "record_backends") as record:
            sr.record_backends_async([_Backend(BACKEND, CARD)])
        record.assert_not_called()

    @override_settings(ML_HEALTH_BACKGROUND_SWEEP=True)
    def test_a_stuck_recording_never_holds_up_the_caller(self):
        release, started = threading.Event(), threading.Event()

        def stuck(_backends):
            started.set()
            release.wait(5)

        with patch.object(sr, "record_backends", stuck):
            began = time.monotonic()
            sr.record_backends_async([_Backend(BACKEND, CARD)])
            self.assertLess(time.monotonic() - began, 1.0)
            self.assertTrue(started.wait(5))
            # A round still recording means the next one is skipped, not queued.
            sr.record_backends_async([_Backend(BACKEND, CARD)])
            release.set()
        for _ in range(50):
            if sr._recording.acquire(blocking=False):
                sr._recording.release()
                break
            time.sleep(0.05)
        else:
            self.fail("the recording thread never finished")


class PickedLetterServingTest(RegistryTestCase):
    def test_a_pick_points_at_the_same_row_as_its_draft(self):
        denial = Denial.objects.create(hashed_email="hash", denial_text="denied")
        row_id = sr.record(BACKEND, CARD)
        ProposedAppeal.objects.create(
            for_denial=denial,
            appeal_text="the letter",
            model_name="fhi-local",
            serving_id=row_id,
        )
        pick = mark_proposal_chosen(denial, "the letter")
        self.assertEqual(pick.serving_id, row_id)
