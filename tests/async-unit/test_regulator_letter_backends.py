"""Regulator letters ask up to three of our models, then one outside model
chosen for the letter's own use, not chat's.

The outside model used to come from the chat fan-out's roster only, so a
spent chat budget or a down roster left the letter with none while a
healthy hosted model sat unused, and a letter request spent chat's hourly
"every outside chat model is down" warning.

With an outside model that can answer, our models marked down are left out
(as for chat) rather than failed open onto ahead of it, which held the
letter for each dead model's share of the budget and logged the router's
fail-open ERROR.
"""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from django.test import SimpleTestCase, override_settings
from loguru import logger

from fighthealthinsurance.escalation_addresses import EscalationRecipient
from fighthealthinsurance.generate_regulator_letter import (
    _letter_backends,
    generate_regulator_letter,
)
from fighthealthinsurance.ml import ml_router as ml_router_module
from fighthealthinsurance.ml import spend
from fighthealthinsurance.ml.ml_models import RemoteModelLike
from fighthealthinsurance.ml.ml_router import MLRouter

_LETTER = "Dear Commissioner, " * 10
ROSTER = ["mistral", "glm"]
HOSTED = "google/gemma-4-26B-A4B-it"


def _model(name, *, external, provider=None, quality=80, reply=None):
    model = MagicMock(spec=RemoteModelLike)
    model.name = name
    model.external = external
    model.context_only = False
    model.quality.return_value = quality
    model.is_available.return_value = True
    model.health_checked_live = True
    model.supports_general_instructions.return_value = True
    model.SPEND_PROVIDER = provider
    # Judged for the call's own use, as RemoteModel._spend_allows is.
    model._spend_allows = MagicMock(
        side_effect=lambda: provider is None
        or spend.allows(provider, spend.current_use())
    )
    model.generate_prior_auth_response = AsyncMock(return_value=reply)
    return model


@override_settings(FHI_CHAT_OUTSIDE_MODELS=ROSTER)
class LetterOutsideModelTest(SimpleTestCase):
    def setUp(self):
        spend._ledger.reset_for_tests()
        self.addCleanup(spend._ledger.reset_for_tests)
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("FORCE_MODEL", None)

        self.router = MLRouter()
        self.ours = _model("fhi-ours", external=False, quality=200)
        self.router.internal_models_by_cost = [self.ours]
        self.router.models_by_name["fhi-ours"] = [self.ours]
        self.roster = [
            _model(name, external=True, provider=spend.DEEPINFRA, reply=_LETTER)
            for name in ROSTER
        ]
        self.router.chat_outside_models_by_name.update({m.name: m for m in self.roster})
        self.hosted = _model(
            HOSTED, external=True, provider=spend.DEEPINFRA, reply=_LETTER
        )
        self.router.external_models_by_cost = [self.hosted]
        self.router.all_models_by_cost = [self.ours, self.hosted]
        self.router.models_by_name[HOSTED] = [self.hosted]

        router = patch(
            "fighthealthinsurance.generate_regulator_letter.ml_router", self.router
        )
        router.start()
        self.addCleanup(router.stop)
        # Chat's hourly roster-down warning; a letter must never send it.
        warning = patch.object(ml_router_module, "_warn_roster_down")
        self.roster_warning = warning.start()
        self.addCleanup(warning.stop)
        # The fail-open ERROR goes out once per pool every ten minutes per
        # process; start unlogged, so a test finding none proves something.
        logged = patch.dict(ml_router_module._fail_open_logged_at, clear=True)
        logged.start()
        self.addCleanup(logged.stop)

    def roster_down(self):
        for model in self.roster:
            model.is_available.return_value = False

    def failing_open_errors(self, use_external):
        """The router's fail-open ERRORs logged while choosing a letter's
        models."""
        errors: list[str] = []
        sink = logger.add(
            lambda message: errors.append(message.record["message"]), level="ERROR"
        )
        try:
            _letter_backends(use_external)
        finally:
            logger.remove(sink)
        return [m for m in errors if "failing open" in m]

    def test_a_healthy_roster_model_is_the_outside_choice(self):
        self.assertEqual(_letter_backends(True), [self.ours, self.roster[0]])

    def test_a_spent_chat_budget_still_leaves_a_hosted_model(self):
        spend.pause(spend.DEEPINFRA, spend.CHAT)
        self.assertEqual(_letter_backends(True), [self.ours, self.hosted])

    def test_a_down_roster_leaves_a_hosted_model(self):
        self.roster_down()
        self.assertEqual(_letter_backends(True), [self.ours, self.hosted])

    def test_a_down_roster_never_fails_open_when_ours_are_down_too(self):
        """Nor do ours: the hosted model can answer, so it is asked alone
        rather than after our dead ones."""
        self.roster_down()
        self.ours.is_available.return_value = False
        self.assertEqual(_letter_backends(True), [self.hosted])

    def test_ours_marked_down_log_no_failing_open_error_beside_a_hosted_model(
        self,
    ):
        self.roster_down()
        self.ours.is_available.return_value = False
        self.assertEqual(self.failing_open_errors(True), [])

    def test_ours_marked_down_are_left_out_beside_a_healthy_roster_model(self):
        self.ours.is_available.return_value = False
        self.assertEqual(_letter_backends(True), [self.roster[0]])

    def test_ours_fail_open_when_no_outside_model_can_answer(self):
        self.roster_down()
        self.ours.is_available.return_value = False
        spend.pause(spend.DEEPINFRA, spend.OTHER)
        self.assertEqual(_letter_backends(True), [self.ours])

    def test_ours_fail_open_without_use_external(self):
        self.ours.is_available.return_value = False
        self.assertEqual(_letter_backends(False), [self.ours])

    def test_ours_failing_open_without_use_external_still_logs_the_error(self):
        """They are the letter's only answerers, so the outage is still
        reported (and failing_open_errors does see the ERROR)."""
        self.ours.is_available.return_value = False
        self.assertNotEqual(self.failing_open_errors(False), [])

    def test_a_letter_never_sends_chats_roster_down_warning(self):
        self.roster_down()
        _letter_backends(True)
        self.roster_warning.assert_not_called()

    def test_no_outside_model_without_use_external(self):
        self.assertEqual(_letter_backends(False), [self.ours])

    def test_no_outside_model_when_none_can_answer_for_this_use(self):
        self.roster_down()
        spend.pause(spend.DEEPINFRA, spend.OTHER)
        self.assertEqual(_letter_backends(True), [self.ours])

    def test_the_hosted_model_writes_the_letter_when_ours_fail(self):
        spend.pause(spend.DEEPINFRA, spend.CHAT)
        denial = SimpleNamespace(
            insurance_company="Aetna",
            procedure="MRI",
            diagnosis="back pain",
            claim_id="C-1",
            plan_id="",
            qa_context="",
            denial_text="Your MRI was denied as not medically necessary.",
            professional_to_finish=False,
        )
        recipient = EscalationRecipient(
            recipient_type="doi", name="Some Regulator", extra={}
        )
        text = asyncio.run(
            generate_regulator_letter(denial, recipient, use_external=True)
        )
        self.assertEqual(text, _LETTER)
