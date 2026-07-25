"""Tests for the speculative internal-only candidate-appeal precompute."""

import io
import os
import threading
from unittest.mock import AsyncMock, MagicMock, patch

from django.test import TestCase, TransactionTestCase, override_settings
from loguru import logger as loguru_logger

from fighthealthinsurance.utils import join_fire_and_forget_threads

from fighthealthinsurance.common_view_logic import DenialCreatorHelper
from fighthealthinsurance.generate_appeal import GeneratedAppeal
from fighthealthinsurance.base_actor_ref import ray_cluster_available
from fighthealthinsurance.ml.ml_speculative_appeals_helper import (
    SpeculativeAppealsHelper,
    dispatch_speculative_appeals,
)
from fighthealthinsurance.models import Denial, ProposedAppeal

_SUMMARIZE = (
    "fighthealthinsurance.ml.ml_appeal_context_helper."
    "MLAppealContextHelper.prewarm_candidate_denial_text_summary"
)
_MAKE_APPEALS = "fighthealthinsurance.common_view_logic.appealGenerator.make_appeals"
# Patched at the source module because create_or_update_denial imports it
# function-locally (from ... import dispatch_speculative_appeals).
_DISPATCH = (
    "fighthealthinsurance.ml.ml_speculative_appeals_helper."
    "dispatch_speculative_appeals"
)
_UPDATE_DENIAL = (
    "fighthealthinsurance.common_view_logic.DenialCreatorHelper._update_denial"
)
# Both imported function-locally inside dispatch_speculative_appeals, so patch
# them at their source modules.
_THREAD_FALLBACK = "fighthealthinsurance.utils.run_in_registered_daemon_thread"
_ACTOR_REF = (
    "fighthealthinsurance.speculative_appeals_actor_ref."
    "speculative_appeals_actor_ref.get"
)


class SpeculativeAppealsHelperTest(TestCase):
    def setUp(self):
        # use_external=True: the denial opted into external models; the
        # speculative precompute must still stay internal-only.
        self.denial = Denial.objects.create(
            hashed_email="hash",
            denial_text="I was denied an MRI for chronic back pain.",
            use_external=True,
        )

    def test_persists_speculative_rows_internal_only(self):
        with patch(_MAKE_APPEALS) as mock_make, patch(
            _SUMMARIZE, new_callable=AsyncMock, return_value=None
        ):
            mock_make.return_value = iter(
                [
                    GeneratedAppeal(
                        text="A sufficiently long speculative appeal letter here.",
                        model_name="fhi-internal",
                        context_level="full",
                    ),
                    GeneratedAppeal(
                        text="Another sufficiently long speculative appeal draft.",
                        model_name="fhi-internal",
                        context_level="tier1_shed",
                    ),
                ]
            )
            count = SpeculativeAppealsHelper.generate_for_denial_sync(
                self.denial.denial_id
            )

        self.assertEqual(count, 2)
        specs = ProposedAppeal.objects.filter(for_denial=self.denial, speculative=True)
        self.assertEqual(specs.count(), 2)
        for s in specs:
            self.assertTrue(s.speculative)
            # Always tagged speculative regardless of the internal tier used.
            self.assertEqual(s.context_level, "speculative")

        # make_appeals was called with use_external forced False in memory...
        called_denial = mock_make.call_args.args[0]
        self.assertFalse(called_denial.use_external)
        # ...but the persisted denial keeps its external opt-in.
        self.denial.refresh_from_db()
        self.assertTrue(self.denial.use_external)

    def test_idempotent_when_speculative_rows_exist(self):
        ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="pre-existing speculative draft",
            speculative=True,
            context_level="speculative",
        )
        with patch(_MAKE_APPEALS) as mock_make:
            count = SpeculativeAppealsHelper.generate_for_denial_sync(
                self.denial.denial_id
            )
        self.assertEqual(count, 0)
        mock_make.assert_not_called()

    def test_skips_when_no_denial_text(self):
        blank = Denial.objects.create(hashed_email="h2", denial_text="")
        with patch(_MAKE_APPEALS) as mock_make:
            count = SpeculativeAppealsHelper.generate_for_denial_sync(blank.denial_id)
        self.assertEqual(count, 0)
        mock_make.assert_not_called()

    def test_skips_runt_outputs(self):
        with patch(_MAKE_APPEALS) as mock_make, patch(
            _SUMMARIZE, new_callable=AsyncMock, return_value=None
        ):
            mock_make.return_value = iter(
                [
                    GeneratedAppeal(text="short", model_name="m"),  # runt
                    GeneratedAppeal(
                        text="A real, deliverable speculative appeal letter.",
                        model_name="m",
                    ),
                ]
            )
            count = SpeculativeAppealsHelper.generate_for_denial_sync(
                self.denial.denial_id
            )
        self.assertEqual(count, 1)

    def test_force_regenerates_past_a_promoted_reserve_row(self):
        """A promoted reserve row (speculative=False, still tagged speculative)
        survives invalidation on purpose, and the idempotency guard matches it --
        so without force a denial that ever served one reserve appeal could never
        build a reserve again. This is the real behavior the text-replacement
        test can only assert through a mock."""
        ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="A reserve appeal that was already served to the user.",
            speculative=False,
            context_level="speculative",
        )
        with patch(_MAKE_APPEALS) as mock_make, patch(
            _SUMMARIZE, new_callable=AsyncMock, return_value=None
        ):
            mock_make.return_value = iter(
                [
                    GeneratedAppeal(
                        text="A freshly generated reserve appeal letter.",
                        model_name="m",
                    )
                ]
            )
            count = SpeculativeAppealsHelper.generate_for_denial_sync(
                self.denial.denial_id, force=True
            )
        self.assertEqual(count, 1)

        # ...and without force the same call is correctly a no-op.
        with patch(_MAKE_APPEALS) as mock_make:
            self.assertEqual(
                SpeculativeAppealsHelper.generate_for_denial_sync(
                    self.denial.denial_id
                ),
                0,
            )
        mock_make.assert_not_called()

    def test_generation_failure_returns_zero_not_raises(self):
        # The prewarm is patched too: it runs before make_appeals, so mocking it
        # keeps the test off any real ML backend regardless of the setUp denial
        # text (today it is short enough to no-op below the summary threshold).
        with patch(_MAKE_APPEALS, side_effect=RuntimeError("boom")), patch(
            _SUMMARIZE, new_callable=AsyncMock, return_value=None
        ):
            count = SpeculativeAppealsHelper.generate_for_denial_sync(
                self.denial.denial_id
            )
        self.assertEqual(count, 0)

    def test_long_denial_summary_fed_to_make_appeals_as_override(self):
        # For an oversized denial the pre-warmed summary must be passed to
        # make_appeals as denial_text_override -- otherwise the full text
        # overflows the window and the speculative reserve is empty for exactly
        # the long denials this safety net matters most for.
        with patch(_MAKE_APPEALS) as mock_make, patch(
            _SUMMARIZE, new_callable=AsyncMock, return_value="A condensed summary."
        ):
            mock_make.return_value = iter(
                [
                    GeneratedAppeal(
                        text="A real speculative appeal letter goes here.",
                        model_name="m",
                    )
                ]
            )
            SpeculativeAppealsHelper.generate_for_denial_sync(self.denial.denial_id)
        self.assertEqual(
            mock_make.call_args.kwargs["denial_text_override"], "A condensed summary."
        )

    def test_generation_runs_off_the_thread_running_the_coroutine(self):
        """The whole point of bridging make_appeals is that it does not run on
        the event loop. It blocks for minutes (model calls, then a lazy iterator
        whose next() waits on the next model future), so a refactor that dropped
        the database_sync_to_async and awaited it inline would stall the actor's
        loop for every other precompute.

        The comparison is against the thread the COROUTINE BODY runs on, not the
        test's own thread: async_to_sync already runs the body on a fresh thread
        of its own, so "differs from the caller" would hold even with no bridge
        at all. The prewarm is awaited directly in the body, which makes it a
        faithful sample of that thread.
        """
        body_threads: list[int] = []
        generation_threads: list[int] = []

        async def _record_body_thread(*args, **kwargs):
            body_threads.append(threading.get_ident())
            return None

        def _record_generation_thread(*args, **kwargs):
            generation_threads.append(threading.get_ident())
            return iter(
                [
                    GeneratedAppeal(
                        text="A real speculative appeal letter goes here.",
                        model_name="m",
                    )
                ]
            )

        with patch(_MAKE_APPEALS, side_effect=_record_generation_thread), patch(
            _SUMMARIZE, side_effect=_record_body_thread
        ):
            count = SpeculativeAppealsHelper.generate_for_denial_sync(
                self.denial.denial_id
            )

        self.assertEqual(count, 1)
        self.assertEqual(len(body_threads), 1)
        self.assertEqual(len(generation_threads), 1)
        self.assertNotEqual(generation_threads[0], body_threads[0])

    def test_normal_denial_passes_no_override(self):
        # A normal-sized denial: prewarm returns None, so denial_text_override is
        # None and make_appeals uses the full raw denial text.
        with patch(_MAKE_APPEALS) as mock_make, patch(
            _SUMMARIZE, new_callable=AsyncMock, return_value=None
        ):
            mock_make.return_value = iter(
                [
                    GeneratedAppeal(
                        text="A real speculative appeal letter goes here.",
                        model_name="m",
                    )
                ]
            )
            SpeculativeAppealsHelper.generate_for_denial_sync(self.denial.denial_id)
        self.assertIsNone(mock_make.call_args.kwargs["denial_text_override"])


class DenialTextReplacedMidGenerationTest(TransactionTestCase):
    """The mid-generation text-replacement guard, which is the highest-stakes
    behavior in the helper: the reconciliation promotes reserve rows oldest
    first, so a row written about a letter the user has since replaced would be
    actively PREFERRED and handed back as their appeal.

    TransactionTestCase, not TestCase. The replacement is performed from inside
    the mocked make_appeals, which now runs on the _generate_drafts worker
    thread with its own connection. Under TestCase's wrapping write transaction
    that UPDATE raises "database table is locked", the blanket except swallows
    it, and the test passes with 0 rows for entirely the wrong reason -- green
    even if the guard were deleted.
    """

    def setUp(self):
        self.denial = Denial.objects.create(
            hashed_email="hash-replaced",
            denial_text="Original letter: denied an MRI of the lumbar spine.",
        )

    def test_discards_reserve_when_denial_text_changed_mid_generation(self):
        denial_id = self.denial.denial_id

        def _replace_text_then_yield(*args, **kwargs):
            # Simulate the user replacing the letter while make_appeals runs.
            Denial.objects.filter(denial_id=denial_id).update(
                denial_text="A completely different denial letter."
            )
            return iter(
                [
                    GeneratedAppeal(
                        text="An appeal about the ORIGINAL, now-replaced letter.",
                        model_name="m",
                    )
                ]
            )

        sink = io.StringIO()
        handler_id = loguru_logger.add(sink, level="INFO")
        try:
            with patch(_MAKE_APPEALS, side_effect=_replace_text_then_yield), patch(
                _SUMMARIZE, new_callable=AsyncMock, return_value=None
            ):
                count = SpeculativeAppealsHelper.generate_for_denial_sync(denial_id)
        finally:
            loguru_logger.remove(handler_id)
        logged = sink.getvalue()

        self.assertEqual(count, 0)
        self.assertFalse(
            ProposedAppeal.objects.filter(for_denial=self.denial).exists(),
            "a reserve about the replaced letter must not be persisted",
        )
        # The 0 has to come from the guard, not from the run blowing up on its
        # way there -- otherwise this asserts nothing about the guard at all.
        self.assertIn("text was replaced while precomputing", logged)
        self.assertNotIn("generation failed for denial", logged)


class DispatchGuardTest(TestCase):
    """dispatch_speculative_appeals must never start a local Ray cluster, and
    must be fully disabled in the Test* configs."""

    def test_ray_cluster_available_requires_init_or_address(self):
        # ray.is_initialized is patched explicitly: other tests in the session
        # may have already initialized Ray, and this asserts the predicate's
        # logic, not ambient session state.
        with patch("ray.is_initialized", return_value=False):
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("RAY_ADDRESS", None)
                # Nothing to attach to -> dispatching would boot a local cluster.
                self.assertFalse(ray_cluster_available())
                os.environ["RAY_ADDRESS"] = "ray://cluster:10001"
                # A configured cluster -> auto-init CONNECTS instead.
                self.assertTrue(ray_cluster_available())
        # Already attached: available regardless of RAY_ADDRESS.
        with patch("ray.is_initialized", return_value=True):
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("RAY_ADDRESS", None)
                self.assertTrue(ray_cluster_available())

    @override_settings(SPECULATIVE_APPEALS_PRECOMPUTE=False)
    def test_disabled_by_setting_dispatches_nothing(self):
        with patch(_THREAD_FALLBACK) as mock_thread, patch(_ACTOR_REF) as mock_actor:
            dispatch_speculative_appeals(1234)
        mock_thread.assert_not_called()
        mock_actor.prefetch_for_denial.remote.assert_not_called()

    @override_settings(SPECULATIVE_APPEALS_PRECOMPUTE=True)
    def test_without_a_ray_cluster_uses_thread_fallback_not_the_actor(self):
        # The regression this guards: touching the actor ref with no cluster
        # configured makes Ray auto-init a brand-new LOCAL cluster (plus a
        # detached actor booting Django) on every denial creation.
        with patch("ray.is_initialized", return_value=False), patch.dict(
            os.environ, {}, clear=False
        ):
            os.environ.pop("RAY_ADDRESS", None)
            with patch(_THREAD_FALLBACK) as mock_thread, patch(
                _ACTOR_REF
            ) as mock_actor:
                dispatch_speculative_appeals(1234)
        mock_actor.prefetch_for_denial.remote.assert_not_called()
        mock_thread.assert_called_once()
        self.assertEqual(mock_thread.call_args.args[1], 1234)


class DispatchOnDenialCreateTest(TestCase):
    """The speculative precompute must be dispatched (fire-and-forget) the
    instant a NEW denial's text arrives, and NOT on subsequent updates."""

    def test_dispatches_precompute_on_new_denial_creation(self):
        with patch(_UPDATE_DENIAL, return_value=MagicMock()), patch(
            _DISPATCH
        ) as mock_dispatch:
            DenialCreatorHelper.create_or_update_denial(
                email="dispatch-create@example.com",
                denial_text="I was denied a knee MRI for a suspected meniscus tear.",
                zip="94103",
            )
        mock_dispatch.assert_called_once()
        # Dispatched with the freshly-created denial's id.
        (denial_id_arg,) = mock_dispatch.call_args.args
        self.assertTrue(Denial.objects.filter(denial_id=denial_id_arg).exists())

    def test_does_not_dispatch_on_update_that_keeps_the_same_text(self):
        text = "An existing denial for a denied physical therapy course."
        existing = Denial.objects.create(
            hashed_email=Denial.get_hashed_email("dispatch-update@example.com"),
            denial_text=text,
        )
        with patch(_UPDATE_DENIAL, return_value=MagicMock()), patch(
            _DISPATCH
        ) as mock_dispatch:
            DenialCreatorHelper.create_or_update_denial(
                email="dispatch-update@example.com",
                denial_text=text,  # unchanged letter -> nothing to recompute
                zip="94103",
                denial=existing,
            )
        mock_dispatch.assert_not_called()

    def test_replacing_denial_text_invalidates_artifacts_and_redispatches(self):
        """A changed denial letter makes every text-derived artifact wrong: the
        held-back reserve would be an appeal about a DIFFERENT denial, and a
        cached summary would misdescribe the claim. Both must be dropped and a
        fresh precompute dispatched."""
        existing = Denial.objects.create(
            hashed_email=Denial.get_hashed_email("dispatch-changed@example.com"),
            denial_text="Original letter: denied an MRI of the lumbar spine.",
            denial_text_summary="summary of the ORIGINAL letter",
            candidate_denial_text_summary="candidate summary of the ORIGINAL letter",
        )
        held_back = ProposedAppeal.objects.create(
            for_denial=existing,
            appeal_text="Reserve appeal written about the ORIGINAL letter.",
            speculative=True,
            context_level="speculative",
        )
        # Already delivered to the user (and possibly chosen): must survive.
        promoted = ProposedAppeal.objects.create(
            for_denial=existing,
            appeal_text="A speculative appeal that was already served.",
            speculative=False,
            context_level="speculative",
        )

        with patch(_UPDATE_DENIAL, return_value=MagicMock()), patch(
            _DISPATCH
        ) as mock_dispatch:
            DenialCreatorHelper.create_or_update_denial(
                email="dispatch-changed@example.com",
                denial_text="Replacement letter: denied a shoulder arthroscopy.",
                zip="94103",
                denial=existing,
            )

        # force=True is essential here: the idempotency guard also matches the
        # PROMOTED row below, which invalidation deliberately keeps, so without
        # it this denial could never rebuild a reserve for its new letter.
        mock_dispatch.assert_called_once_with(existing.denial_id, force=True)
        # The stale reserve is gone; the already-served row is untouched.
        self.assertFalse(
            ProposedAppeal.objects.filter(pk=held_back.pk).exists(),
            "held-back reserve derived from the old letter must be deleted",
        )
        self.assertTrue(ProposedAppeal.objects.filter(pk=promoted.pk).exists())
        # Both cached summaries are cleared so nothing summarizing the old
        # letter can be substituted into a later prompt.
        existing.refresh_from_db()
        self.assertIsNone(existing.denial_text_summary)
        self.assertIsNone(existing.candidate_denial_text_summary)


class SpeculativePrecomputeEndToEndTest(TransactionTestCase):
    """The whole chain, with the precompute ENABLED and no Ray cluster:

        create_or_update_denial -> dispatch -> real daemon thread
        -> generate_for_denial_sync -> persisted reserve rows

    Every other test either bypasses the settings gate (calling the helper
    directly) or mocks the dispatch, so nothing joined those pieces up. That
    matters because the gate is off in all three Test* configs, which means a
    break anywhere in this chain would otherwise ship silently.

    TransactionTestCase (not TestCase) because the work runs on a background
    thread with its own DB connection: inside TestCase's wrapping transaction
    that thread cannot see the denial the test just created. Only the ML backend
    is mocked, per the repo's testing guidance.
    """

    @override_settings(SPECULATIVE_APPEALS_PRECOMPUTE=True)
    def test_creating_a_denial_precomputes_a_reserve(self):
        email = "precompute-e2e@example.com"
        with patch("ray.is_initialized", return_value=False), patch.dict(
            os.environ, {}, clear=False
        ):
            # No cluster configured -> the thread fallback, which is the path
            # this test exists to exercise.
            os.environ.pop("RAY_ADDRESS", None)
            with patch(_UPDATE_DENIAL, return_value=MagicMock()), patch(
                _MAKE_APPEALS
            ) as mock_make, patch(
                _SUMMARIZE, new_callable=AsyncMock, return_value=None
            ):
                mock_make.return_value = iter(
                    [
                        GeneratedAppeal(
                            text="A precomputed reserve appeal letter for the denial.",
                            model_name="fhi-internal",
                        )
                    ]
                )
                DenialCreatorHelper.create_or_update_denial(
                    email=email,
                    denial_text="I was denied a knee MRI for a suspected tear.",
                    zip="94103",
                )
                # Drain inside the patches: the thread must not outlive them and
                # reach a real ML backend.
                join_fire_and_forget_threads(timeout=30.0)

        denial = Denial.objects.get(hashed_email=Denial.get_hashed_email(email))
        reserve = ProposedAppeal.objects.filter(for_denial=denial, speculative=True)
        self.assertEqual(reserve.count(), 1)
        self.assertEqual(reserve.first().context_level, "speculative")
        # The precompute must stay internal-only even end-to-end.
        self.assertFalse(mock_make.call_args.args[0].use_external)

    @override_settings(SPECULATIVE_APPEALS_PRECOMPUTE=True)
    def test_precompute_failure_does_not_break_denial_creation(self):
        """The precompute is best-effort: a backend blowing up must never take
        the user's denial submission down with it."""
        email = "precompute-boom@example.com"
        with patch("ray.is_initialized", return_value=False), patch.dict(
            os.environ, {}, clear=False
        ):
            os.environ.pop("RAY_ADDRESS", None)
            with patch(_UPDATE_DENIAL, return_value=MagicMock()), patch(
                _MAKE_APPEALS, side_effect=RuntimeError("backend down")
            ), patch(_SUMMARIZE, new_callable=AsyncMock, return_value=None):
                DenialCreatorHelper.create_or_update_denial(
                    email=email,
                    denial_text="I was denied physical therapy after surgery.",
                    zip="94103",
                )
                join_fire_and_forget_threads(timeout=30.0)

        # The denial still exists; there is simply no reserve.
        denial = Denial.objects.get(hashed_email=Denial.get_hashed_email(email))
        self.assertEqual(
            ProposedAppeal.objects.filter(for_denial=denial, speculative=True).count(),
            0,
        )
