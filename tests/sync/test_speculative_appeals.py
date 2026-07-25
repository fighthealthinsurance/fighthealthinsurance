"""Tests for the speculative internal-only candidate-appeal precompute."""

from unittest.mock import AsyncMock, MagicMock, patch

from django.test import TestCase

from fighthealthinsurance.common_view_logic import DenialCreatorHelper
from fighthealthinsurance.generate_appeal import GeneratedAppeal
from fighthealthinsurance.ml.ml_speculative_appeals_helper import (
    SpeculativeAppealsHelper,
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
        specs = ProposedAppeal.objects.filter(
            for_denial=self.denial, speculative=True
        )
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
            count = SpeculativeAppealsHelper.generate_for_denial_sync(
                blank.denial_id
            )
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

    def test_generation_failure_returns_zero_not_raises(self):
        with patch(_MAKE_APPEALS, side_effect=RuntimeError("boom")):
            count = SpeculativeAppealsHelper.generate_for_denial_sync(
                self.denial.denial_id
            )
        self.assertEqual(count, 0)


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

    def test_does_not_dispatch_on_update_of_existing_denial(self):
        existing = Denial.objects.create(
            hashed_email=Denial.get_hashed_email("dispatch-update@example.com"),
            denial_text="An existing denial for a denied physical therapy course.",
        )
        with patch(_UPDATE_DENIAL, return_value=MagicMock()), patch(
            _DISPATCH
        ) as mock_dispatch:
            DenialCreatorHelper.create_or_update_denial(
                email="dispatch-update@example.com",
                denial_text="Updated denial text with more detail on the denial.",
                zip="94103",
                denial=existing,
            )
        mock_dispatch.assert_not_called()
