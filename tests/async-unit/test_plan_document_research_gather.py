"""The appeal-time plan-document refresh in the research gather.

``AppealsBackendHelper._generate_appeals_body`` gathers the research
contexts by position: seven fixed slots, then NICE only when a key is
configured, then (with ``PLAN_DOCUMENT_MATCHING_ENABLED``) the
``plan_document_matcher.arefresh_match`` refresh. The NICE result is read as
``results[7]``, so the refresh has to sit after that conditional slot, or a
NICE answer would be lost and, with NICE off, the refresh's bool would be
read in its place. These tests drive ``generate_appeals`` the way
``test_common_view_logic.py`` does (a Django TestCase, ``async_to_sync``,
every lookup answered in memory) and read what reached ``make_appeals``.
"""

import contextlib
import json
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.test import TestCase, override_settings

from fighthealthinsurance import common_view_logic
from fighthealthinsurance.common_view_logic import AppealsBackendHelper
from fighthealthinsurance.generate_appeal import GeneratedAppeal
from fighthealthinsurance.models import Denial

EMAIL = "someone@example.com"
RAG = "Guideline: imaging is standard of care after conservative care."
NICE = "NICE guidance: imaging is recommended after six weeks."
REFRESH = "fighthealthinsurance.plan_document_matcher.arefresh_match"
# The research substeps every first attempt runs, flag or no flag.
ALWAYS = frozenset(
    {
        "pubmed",
        "citations",
        "guidelines",
        "imr_decisions",
        "pa_requirements",
        "uspstf",
        "clinical_trials",
    }
)


class ResearchGatherPlanDocumentsTests(TestCase):
    def setUp(self):
        self.denial = Denial.objects.create(
            semi_sekret="sekret",
            hashed_email=Denial.get_hashed_email(EMAIL),
            denial_text="Your MRI was denied as not medically necessary.",
            procedure="MRI",
            diagnosis="back pain",
        )

    @contextlib.contextmanager
    def _research_stubbed(self, nice_key=""):
        """Every research lookup answered in memory; yields the appealGenerator
        stand-in, whose make_appeals call carries the gathered contexts.
        ``nice_key`` empty is NICE off (the slot is not gathered)."""
        with contextlib.ExitStack() as stack:
            generator = stack.enter_context(
                patch("fighthealthinsurance.common_view_logic.appealGenerator")
            )
            generator.make_appeals.return_value = iter(
                [
                    GeneratedAppeal(
                        text="Dear insurer, "
                        + "this claim is medically necessary. " * 20,
                        model_name="fhi-internal",
                        context_level="full",
                    )
                ]
            )
            stack.enter_context(
                patch(
                    "fighthealthinsurance.common_view_logic.get_rag_context_for_denial",
                    new=AsyncMock(return_value=RAG),
                )
            )
            stack.enter_context(
                patch(
                    "fighthealthinsurance.common_view_logic."
                    "MLCitationsHelper.generate_citations_for_denial",
                    new=AsyncMock(return_value=None),
                )
            )
            stack.enter_context(
                patch.object(
                    common_view_logic.IMRDecisionRetriever,
                    "get_context_for_denial",
                    new=AsyncMock(return_value=""),
                )
            )
            pmt = stack.enter_context(
                patch("fighthealthinsurance.common_view_logic.AppealsBackendHelper.pmt")
            )
            pmt.find_context_for_denial = AsyncMock(return_value=None)
            stack.enter_context(
                patch.object(AppealsBackendHelper.nice, "api_key", nice_key)
            )
            stack.enter_context(
                patch.object(
                    AppealsBackendHelper.nice,
                    "find_context_for_denial",
                    new=AsyncMock(return_value=NICE),
                )
            )
            yield generator

    def _research_substeps(self):
        """Run the flow; ``substep -> state`` of its research status frames."""

        async def run():
            states = {}
            async for chunk in AppealsBackendHelper.generate_appeals(
                {
                    "denial_id": self.denial.denial_id,
                    "email": EMAIL,
                    "semi_sekret": "sekret",
                }
            ):
                if not chunk or not chunk.strip():
                    continue
                try:
                    frame = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                if (
                    frame.get("type") == "status"
                    and frame.get("phase") == "research"
                    and frame.get("substep")
                ):
                    states[frame["substep"]] = frame.get("state")
            return states

        return async_to_sync(run)()

    @staticmethod
    def _done(states):
        return {substep for substep, state in states.items() if state == "done"}

    def test_with_nice_off_the_refresh_runs_and_the_other_contexts_survive(self):
        refresh = AsyncMock(return_value=True)
        with override_settings(PLAN_DOCUMENT_MATCHING_ENABLED=True), patch(
            REFRESH, new=refresh
        ), self._research_stubbed(nice_key="") as generator:
            states = self._research_substeps()

        refresh.assert_awaited_once()
        self.assertEqual(refresh.await_args.args[0].denial_id, self.denial.denial_id)
        self.assertEqual(states.get("plan_documents"), "done")
        self.assertNotIn("nice", states)
        self.assertTrue(ALWAYS <= self._done(states), states)
        kwargs = generator.make_appeals.call_args.kwargs
        self.assertEqual(kwargs["rag_context"], RAG)
        # With NICE off, results[7] is the refresh's True: not NICE guidance,
        # so the persisted (empty) NICE context is what reaches make_appeals.
        self.assertIsNone(kwargs["nice_context"])

    def test_with_nice_on_the_refresh_comes_after_the_nice_slot(self):
        refresh = AsyncMock(return_value=True)
        with override_settings(PLAN_DOCUMENT_MATCHING_ENABLED=True), patch(
            REFRESH, new=refresh
        ), self._research_stubbed(nice_key="nice-key") as generator:
            states = self._research_substeps()

        refresh.assert_awaited_once()
        self.assertEqual(states.get("nice"), "done")
        self.assertEqual(states.get("plan_documents"), "done")
        kwargs = generator.make_appeals.call_args.kwargs
        # NICE still occupies slot 7; the refresh did not displace it.
        self.assertEqual(kwargs["nice_context"], NICE)
        self.assertEqual(kwargs["rag_context"], RAG)

    def test_with_the_flag_off_the_refresh_never_runs(self):
        refresh = AsyncMock(return_value=True)
        with override_settings(PLAN_DOCUMENT_MATCHING_ENABLED=False), patch(
            REFRESH, new=refresh
        ), self._research_stubbed(nice_key="") as generator:
            states = self._research_substeps()

        refresh.assert_not_awaited()
        self.assertNotIn("plan_documents", states)
        self.assertTrue(ALWAYS <= self._done(states), states)
        kwargs = generator.make_appeals.call_args.kwargs
        self.assertEqual(kwargs["rag_context"], RAG)
        self.assertIsNone(kwargs["nice_context"])

    def test_a_refresh_that_raises_costs_its_substep_and_nothing_else(self):
        refresh = AsyncMock(side_effect=RuntimeError("corpus unavailable"))
        with override_settings(PLAN_DOCUMENT_MATCHING_ENABLED=True), patch(
            REFRESH, new=refresh
        ), self._research_stubbed(nice_key="") as generator:
            states = self._research_substeps()

        refresh.assert_awaited_once()
        self.assertEqual(states.get("plan_documents"), "error")
        self.assertTrue(ALWAYS <= self._done(states), states)
        kwargs = generator.make_appeals.call_args.kwargs
        self.assertEqual(kwargs["rag_context"], RAG)
        self.assertIsNone(kwargs["nice_context"])
