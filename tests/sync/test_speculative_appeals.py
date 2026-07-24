"""Tests for the speculative internal-only candidate-appeal precompute."""

from unittest.mock import AsyncMock, patch

from django.test import TestCase

from fighthealthinsurance.generate_appeal import GeneratedAppeal
from fighthealthinsurance.ml.ml_speculative_appeals_helper import (
    SpeculativeAppealsHelper,
)
from fighthealthinsurance.models import Denial, ProposedAppeal

_SUMMARIZE = (
    "fighthealthinsurance.ml.ml_appeal_context_helper."
    "MLAppealContextHelper.maybe_summarize_denial_text"
)
_MAKE_APPEALS = "fighthealthinsurance.common_view_logic.appealGenerator.make_appeals"


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
