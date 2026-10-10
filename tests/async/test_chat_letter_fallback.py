"""A failed chat turn asking for a letter should still deliver a letter.

When every chat model fails on a "please draft the letter" turn (the
handle_chat_message total-failure branch), the appeal-generation pipeline is
tried before giving up: an existing ProposedAppeal reserve is served straight
from the DB, and otherwise a bounded make_appeals run drafts one. Also covers
the generate_appeal_letter chat tool, which routes letter drafting to the
same pipeline instead of having the chat model write the letter inline.
"""

import asyncio
import itertools
from unittest.mock import AsyncMock, MagicMock, patch

from loguru import logger
from rest_framework.test import APITestCase

from fighthealthinsurance import common_view_logic
from fighthealthinsurance.chat.appeal_letter_generator import (
    DraftedLetter,
    draft_letter_for_chat,
    fill_letter_placeholders,
    find_reserve_letter,
    generate_letter_for_denial,
)
from fighthealthinsurance.chat.tools import AppealTool, GenerateAppealLetterTool
from fighthealthinsurance.chat_interface import ChatInterface
from fighthealthinsurance.client_gone import ClientGone
from fighthealthinsurance.context_utils import CONTEXT_LEVEL_TEMPLATE
from fighthealthinsurance.generate_appeal import GeneratedAppeal
from fighthealthinsurance.ml.model_identity import TEMPLATE_MODEL_NAME
from fighthealthinsurance.models import (
    Appeal,
    ChatTurn,
    Denial,
    OngoingChat,
    ProposedAppeal,
)

# Failure plumbing shared with the failed-turn persistence tests: routes model
# selection to a mock backend and makes the chat LLM pass fail.
from tests.chat_fixtures import (
    FrameRecorder as _FrameRecorder,
    RecordingChatModel,
    llm_call_fails as _llm_call_fails,
    make_professional_chat as _make_professional_chat,
)

# Long enough to pass is_real_appeal and the reserve length preference.
# Free of the phrases the wizard's placeholder fill rewrites (e.g. "Dear
# Insurance Company"), so a served reserve comes back verbatim.
RESERVE_LETTER = (
    "To the appeals reviewer: I am writing to formally appeal the denial of "
    "the MRI ordered for my chronic back pain. The imaging is medically "
    "necessary to evaluate ongoing symptoms that have not improved with "
    "conservative treatment. Please reverse the denial. Sincerely, Patient"
)

GENERATED_LETTER = (
    "Dear Acme Health, I appeal the denial of claim 123. The requested "
    "procedure is medically necessary for the diagnosis documented in the "
    "attached records, and the denial should be overturned. Sincerely, Me"
)


# A full model draft: model-written and past FHI_CHAT_LETTER_MIN_CHARS (350).
FULL_MODEL_LETTER = GENERATED_LETTER + " " + (
    "The treating physician documented failed conservative therapy, "
    "worsening symptoms, and a clinical need for this procedure. " * 3
)

EDITED_LETTER = (
    "My carefully hand-edited appeal letter about the MRI denial, "
    "which must not be replaced by boilerplate."
)


async def _link_letter_appeal(chat, user, **denial_fields):
    """Create a Denial with letter context and an Appeal linked to the chat."""
    fields = {
        "denial_text": "Your MRI claim was denied as not medically necessary.",
        "procedure": "MRI",
        "diagnosis": "chronic back pain",
        "hashed_email": Denial.get_hashed_email(user.email),
    }
    fields.update(denial_fields)
    denial = await Denial.objects.acreate(**fields)
    appeal = await Appeal.objects.acreate(
        chat=chat,
        for_denial=denial,
        hashed_email=fields["hashed_email"],
    )
    return appeal, denial


class ChatLetterFallbackTest(APITestCase):
    """The total-failure branch routes letter requests to the appeal pipeline."""

    async def _run_failing_turn(self, username, npi, message, link_appeal=True):
        user, chat = await _make_professional_chat(username, npi)
        appeal = denial = None
        if link_appeal:
            appeal, denial = await _link_letter_appeal(chat, user)
        recorder = _FrameRecorder()
        interface = ChatInterface(
            send_json_message_func=recorder,
            chat=chat,
            user=user,
        )
        with _llm_call_fails(lambda *a, **k: (None, None)):
            await interface.handle_chat_message(message)
        return chat, appeal, denial, recorder

    async def test_letter_request_rescued_by_letter_fallback(self):
        with patch(
            "fighthealthinsurance.chat_interface.draft_letter_for_chat",
            new=AsyncMock(return_value=DraftedLetter(GENERATED_LETTER, True)),
        ) as mock_draft:
            chat, appeal, _, recorder = await self._run_failing_turn(
                "letterfall1", "9999920001", "Please go ahead and draft a letter."
            )
        mock_draft.assert_awaited_once()
        self.assertTrue(mock_draft.await_args.kwargs["prefer_existing"])
        # The turn delivers the letter as an assistant reply, not an error.
        error_frames = [f for f in recorder.frames if "error" in f]
        self.assertEqual(error_frames, [])
        assistant_frames = [
            f for f in recorder.frames if f.get("role") == "assistant"
        ]
        self.assertEqual(len(assistant_frames), 1)
        self.assertIn(GENERATED_LETTER, assistant_frames[0]["content"])
        self.assertIn(f"/appeals/{appeal.id}", assistant_frames[0]["content"])

    async def test_letter_fallback_reply_is_persisted(self):
        with patch(
            "fighthealthinsurance.chat_interface.draft_letter_for_chat",
            new=AsyncMock(return_value=DraftedLetter(GENERATED_LETTER, True)),
        ):
            chat, _, _, _ = await self._run_failing_turn(
                "letterfall2", "9999920002", "Please go ahead and draft a letter."
            )
        fresh = await OngoingChat.objects.aget(id=chat.id)
        assistant_msgs = [
            m for m in (fresh.chat_history or []) if m.get("role") == "assistant"
        ]
        self.assertEqual(len(assistant_msgs), 1)
        self.assertIn(GENERATED_LETTER, assistant_msgs[0]["content"])
        # The user's message survived too (pre-persist).
        user_msgs = [m for m in fresh.chat_history if m.get("role") == "user"]
        self.assertEqual(len(user_msgs), 1)

    async def test_letter_fallback_serves_existing_reserve_without_models(self):
        """With a ProposedAppeal already in the DB the rescue needs zero
        model calls: the reserve is served and saved onto the appeal."""
        user, chat = await _make_professional_chat("letterfall3", "9999920003")
        appeal, denial = await _link_letter_appeal(chat, user, your_state="CA")
        await ProposedAppeal.objects.acreate(
            appeal_text=RESERVE_LETTER,
            for_denial=denial,
            speculative=True,
            # A held-back reserve is only served for the state it was
            # written for (servable_drafts).
            built_for_state="CA",
        )
        recorder = _FrameRecorder()
        interface = ChatInterface(
            send_json_message_func=recorder,
            chat=chat,
            user=user,
        )
        with _llm_call_fails(lambda *a, **k: (None, None)):
            await interface.handle_chat_message("Can you write the appeal letter?")
        assistant_frames = [
            f for f in recorder.frames if f.get("role") == "assistant"
        ]
        self.assertEqual(len(assistant_frames), 1)
        self.assertIn(RESERVE_LETTER, assistant_frames[0]["content"])
        fresh_appeal = await Appeal.objects.aget(id=appeal.id)
        self.assertEqual(fresh_appeal.appeal_text, RESERVE_LETTER)

    async def test_no_fallback_without_linked_appeal(self):
        with patch(
            "fighthealthinsurance.chat_interface.draft_letter_for_chat",
            new=AsyncMock(return_value=DraftedLetter(GENERATED_LETTER, True)),
        ) as mock_draft:
            chat, _, _, recorder = await self._run_failing_turn(
                "letterfall4",
                "9999920004",
                "Please go ahead and draft a letter.",
                link_appeal=False,
            )
        mock_draft.assert_not_awaited()
        error_frames = [f for f in recorder.frames if "error" in f]
        self.assertTrue(error_frames, f"expected an error frame: {recorder.frames}")
        assistant_frames = [
            f for f in recorder.frames if f.get("role") == "assistant"
        ]
        self.assertEqual(assistant_frames, [])

    async def test_non_letter_request_skips_fallback_and_errors(self):
        with patch(
            "fighthealthinsurance.chat_interface.draft_letter_for_chat",
            new=AsyncMock(return_value=DraftedLetter(GENERATED_LETTER, True)),
        ) as mock_draft:
            chat, _, _, recorder = await self._run_failing_turn(
                "letterfall5", "9999920005", "Why was my MRI claim denied?"
            )
        mock_draft.assert_not_awaited()
        error_frames = [f for f in recorder.frames if "error" in f]
        self.assertTrue(error_frames)

    async def test_failed_fallback_error_names_the_appeal_in_plain_text(self):
        """When even the letter fallback can't deliver, the error message
        points the user at the linked appeal's own page. The client shows
        error frames as plain text, so a markdown link would render raw."""
        with patch(
            "fighthealthinsurance.chat_interface.draft_letter_for_chat",
            new=AsyncMock(return_value=None),
        ):
            chat, appeal, _, recorder = await self._run_failing_turn(
                "letterfall6", "9999920006", "Please go ahead and draft a letter."
            )
        error_frames = [f for f in recorder.frames if "error" in f]
        self.assertIn(
            f"from the page for Appeal #{appeal.id}.", error_frames[0]["error"]
        )

    async def test_redo_request_does_not_ask_for_a_stored_draft(self):
        with patch(
            "fighthealthinsurance.chat_interface.draft_letter_for_chat",
            new=AsyncMock(return_value=DraftedLetter(GENERATED_LETTER, True)),
        ) as mock_draft:
            await self._run_failing_turn(
                "letterfall17",
                "9999920017",
                "Please redo the letter with the new diagnosis.",
            )
        self.assertFalse(mock_draft.await_args.kwargs["use_reserve"])

    async def test_redo_request_is_not_answered_with_a_draft_from_before(self):
        """The stored draft predates the change the person asked for, so
        with no new letter to give, the turn ends in the error frame."""
        user, chat = await _make_professional_chat("letterfall18", "9999920018")
        _, denial = await _link_letter_appeal(chat, user, your_state="CA")
        await ProposedAppeal.objects.acreate(
            appeal_text=RESERVE_LETTER,
            for_denial=denial,
            speculative=True,
            built_for_state="CA",
        )
        recorder = _FrameRecorder()
        interface = ChatInterface(
            send_json_message_func=recorder,
            chat=chat,
            user=user,
        )
        with patch(
            "fighthealthinsurance.chat.appeal_letter_generator."
            "generate_letter_for_denial",
            new=AsyncMock(return_value=None),
        ), _llm_call_fails(lambda *a, **k: (None, None)):
            await interface.handle_chat_message(
                "Please redo the letter with the new diagnosis."
            )
        assistant_frames = [
            f for f in recorder.frames if f.get("role") == "assistant"
        ]
        self.assertEqual(assistant_frames, [])

    async def test_redo_turn_keeps_stored_drafts_from_the_letter_tool(self):
        """The redo rule reaches the letter tool too, not just the fallback:
        if its generation fails, a draft from before the request must not
        stand in for the new letter."""
        user, chat = await _make_professional_chat("letterfall19", "9999920019")
        await _link_letter_appeal(chat, user)
        interface = ChatInterface(
            send_json_message_func=_FrameRecorder(), chat=chat, user=user
        )
        model = RecordingChatModel(
            always_reply='**generate_appeal_letter**{"procedure": "MRI"}'
        )
        with patch(
            "fighthealthinsurance.ml.ml_router.MLRouter."
            "get_chat_backends_with_fallback",
            return_value=([model], []),
        ), patch(
            "fighthealthinsurance.chat_interface.fire_and_forget_in_new_threadpool",
            new_callable=AsyncMock,
        ), patch(
            "fighthealthinsurance.chat.tools.generate_appeal_letter_tool."
            "draft_letter_for_chat",
            new=AsyncMock(return_value=DraftedLetter(GENERATED_LETTER, True)),
        ) as mock_draft:
            await interface.handle_chat_message(
                "Please redo the letter with the new diagnosis."
            )
        self.assertFalse(mock_draft.await_args.kwargs["use_reserve"])


    async def _interface_with_linked_appeal(self, username, npi):
        user, chat = await _make_professional_chat(username, npi)
        await _link_letter_appeal(chat, user)
        recorder = _FrameRecorder()
        interface = ChatInterface(
            send_json_message_func=recorder,
            chat=chat,
            user=user,
        )
        return interface, recorder

    @staticmethod
    def _tool_drafted_then_turn_failed(interface):
        """LLM-pass stand-in: the letter tool drafts (and saves) a letter,
        then the turn fails -- e.g. a later research pass runs it out of
        budget."""

        def run(*args, **kwargs):
            interface._turn_drafted_letter[0] = DraftedLetter(GENERATED_LETTER, True)
            return (None, None)

        return run

    async def test_fallback_delivers_the_letter_drafted_this_turn(self):
        """Not a second generation that would be saved over the first."""
        interface, recorder = await self._interface_with_linked_appeal(
            "letterfall7", "9999920007"
        )
        with patch(
            "fighthealthinsurance.chat_interface.draft_letter_for_chat",
            new=AsyncMock(return_value=DraftedLetter(RESERVE_LETTER, True)),
        ) as mock_draft:
            with _llm_call_fails(self._tool_drafted_then_turn_failed(interface)):
                await interface.handle_chat_message(
                    "Please go ahead and draft a letter."
                )
        mock_draft.assert_not_awaited()
        assistant_frames = [
            f for f in recorder.frames if f.get("role") == "assistant"
        ]
        self.assertIn(GENERATED_LETTER, assistant_frames[0]["content"])

    async def test_letter_drafted_this_turn_is_delivered_for_any_message(self):
        """A bare "yes" to the model's offer to draft doesn't read as a
        letter request, but the letter it led to is already on the appeal:
        deliver it rather than hide it behind an error."""
        interface, recorder = await self._interface_with_linked_appeal(
            "letterfall8", "9999920008"
        )
        with _llm_call_fails(self._tool_drafted_then_turn_failed(interface)):
            await interface.handle_chat_message("Yes, please.")
        assistant_frames = [
            f for f in recorder.frames if f.get("role") == "assistant"
        ]
        self.assertEqual(len(assistant_frames), 1)
        self.assertIn(GENERATED_LETTER, assistant_frames[0]["content"])

    async def test_previous_turns_letter_is_not_reused(self):
        interface, recorder = await self._interface_with_linked_appeal(
            "letterfall9", "9999920009"
        )
        interface._turn_drafted_letter[0] = DraftedLetter(GENERATED_LETTER, True)
        with patch(
            "fighthealthinsurance.chat_interface.draft_letter_for_chat",
            new=AsyncMock(return_value=DraftedLetter(RESERVE_LETTER, True)),
        ) as mock_draft:
            with _llm_call_fails(lambda *a, **k: (None, None)):
                await interface.handle_chat_message(
                    "Please go ahead and draft a letter."
                )
        mock_draft.assert_awaited_once()


    @staticmethod
    def _failure_event(capture):
        return next(
            call.kwargs
            for call in capture.call_args_list
            if call.args[0] == "chat_turn_total_failure"
        )

    async def test_failure_event_reports_no_attempt_without_linked_appeal(self):
        """No letter-capable appeal: the fallback never ran, and the event
        must not read like a fallback that ran and failed."""
        with patch(
            "fighthealthinsurance.chat_interface.capture_reliability_event"
        ) as capture:
            await self._run_failing_turn(
                "letterfall10",
                "9999920010",
                "Please go ahead and draft a letter.",
                link_appeal=False,
            )
        self.assertFalse(self._failure_event(capture)["letter_fallback_attempted"])

    async def test_failure_event_reports_a_failed_fallback_attempt(self):
        with patch(
            "fighthealthinsurance.chat_interface.draft_letter_for_chat",
            new=AsyncMock(return_value=None),
        ), patch(
            "fighthealthinsurance.chat_interface.capture_reliability_event"
        ) as capture:
            await self._run_failing_turn(
                "letterfall11", "9999920011", "Please go ahead and draft a letter."
            )
        self.assertTrue(self._failure_event(capture)["letter_fallback_attempted"])

    async def test_rescued_turn_is_counted_and_recorded_as_failed(self):
        """The chat models failed all the same: the rescued turn is counted,
        and its ChatTurn row written, as a failure -- the routing policy
        reads those rows for how each model's calls ended."""
        with patch(
            "fighthealthinsurance.chat_interface.draft_letter_for_chat",
            new=AsyncMock(return_value=DraftedLetter(GENERATED_LETTER, True)),
        ), patch("fighthealthinsurance.chat_interface.record_chat_turn") as counted:
            chat, _, _, _ = await self._run_failing_turn(
                "letterfall12", "9999920012", "Please go ahead and draft a letter."
            )
        rows = [t.outcome async for t in ChatTurn.objects.filter(chat_id=chat.id)]
        self.assertEqual(
            ([call.args[0] for call in counted.call_args_list], rows),
            (["failed"], ["failed"]),
        )

    async def _run_turn_the_client_leaves_at_the_fallback(
        self, username, npi, side_effect=lambda *a, **k: (None, None), env=None
    ):
        """A failing letter turn whose client is gone by the time the
        fallback's first status frame goes out. Returns (chat, counted
        outcomes, the draft mock)."""
        user, chat = await _make_professional_chat(username, npi)
        await _link_letter_appeal(chat, user)

        async def send(frame):
            if "appeal generator instead" in str(frame.get("status", "")):
                raise ClientGone()

        interface = ChatInterface(send_json_message_func=send, chat=chat, user=user)
        with patch.dict("os.environ", env or {}), patch(
            "fighthealthinsurance.chat_interface.draft_letter_for_chat",
            new=AsyncMock(return_value=DraftedLetter(GENERATED_LETTER, True)),
        ) as mock_draft, patch(
            "fighthealthinsurance.chat_interface.record_chat_turn"
        ) as counted, _llm_call_fails(
            side_effect
        ):
            await asyncio.wait_for(
                interface.handle_chat_message("Please go ahead and draft a letter."),
                timeout=20,
            )
        return chat, [call.args[0] for call in counted.call_args_list], mock_draft

    async def test_client_leaving_at_the_fallback_is_client_gone(self):
        """A departure, as when the client leaves before the fallback: not a
        failure to count or apologise for."""
        _, outcomes, _ = await self._run_turn_the_client_leaves_at_the_fallback(
            "letterfall13", "9999920013"
        )
        self.assertEqual(outcomes, ["client_gone"])

    async def test_no_letter_is_drafted_for_a_client_that_left(self):
        _, _, mock_draft = await self._run_turn_the_client_leaves_at_the_fallback(
            "letterfall14", "9999920014"
        )
        mock_draft.assert_not_awaited()

    async def test_timed_out_turn_left_at_the_fallback_is_counted_once(self):
        """Counted "timeout" before the fallback ran, it keeps that count and
        its row rather than being counted "client_gone" as well."""

        async def stalled(*args, **kwargs):
            await asyncio.sleep(30)
            return ("never seen", None)

        chat, outcomes, _ = await self._run_turn_the_client_leaves_at_the_fallback(
            "letterfall16",
            "9999920016",
            side_effect=stalled,
            env={"FHI_CHAT_TURN_BUDGET": "0.3", "FHI_CHAT_HEARTBEAT_SECONDS": "600"},
        )
        rows = [t.outcome async for t in ChatTurn.objects.filter(chat_id=chat.id)]
        self.assertEqual((outcomes, rows), (["timeout"], ["timeout"]))

    async def test_fallback_failure_logs_the_error_class_not_its_text(self):
        """Chat-path logs name an exception's class only: its text can carry
        the user's details. (The traceback, which deployed sinks keep without
        frame variables, still has it.)"""
        messages = []
        sink_id = logger.add(
            lambda msg: messages.append(msg.record["message"]), level="DEBUG"
        )
        try:
            with patch(
                "fighthealthinsurance.chat_interface.draft_letter_for_chat",
                new=AsyncMock(side_effect=RuntimeError("SENTINEL-letterfall-15")),
            ):
                await self._run_failing_turn(
                    "letterfall15", "9999920015", "Please go ahead and draft a letter."
                )
        finally:
            logger.remove(sink_id)
        self.assertEqual([m for m in messages if "SENTINEL-letterfall-15" in m], [])


class GenerateAppealLetterToolTest(APITestCase):
    """The generate_appeal_letter tool: appeal setup + pipeline handoff."""

    async def _make_chat(self, username, npi):
        _, chat = await _make_professional_chat(username, npi)
        return chat

    async def test_tool_drafts_letter_and_links_appeal(self):
        chat = await self._make_chat("lettertool1", "9999930001")
        status = AsyncMock()
        tool = GenerateAppealLetterTool(status, AsyncMock())
        response_text = (
            "On it!\n"
            '**generate_appeal_letter**{"procedure": "MRI", '
            '"diagnosis": "chronic back pain", '
            '"medical_reason": "failed six weeks of conservative therapy"}'
        )
        with patch(
            "fighthealthinsurance.chat.tools.generate_appeal_letter_tool."
            "draft_letter_for_chat",
            new=AsyncMock(return_value=DraftedLetter(GENERATED_LETTER, True)),
        ) as mock_draft:
            response, context, handled = await tool.handle(
                response_text, "", chat=chat
            )
        self.assertTrue(handled)
        mock_draft.assert_awaited_once()
        self.assertNotIn("generate_appeal_letter", response)
        self.assertIn(GENERATED_LETTER, response)
        self.assertIn("/appeals/", response)
        # The surrounding prose the model wrote survives.
        self.assertIn("On it!", response)
        appeal = await Appeal.objects.select_related("for_denial").aget(chat=chat)
        self.assertEqual(appeal.for_denial.procedure, "MRI")
        self.assertEqual(appeal.for_denial.diagnosis, "chronic back pain")
        # The letter-context key is folded into qa_context, not dropped.
        self.assertIn(
            "failed six weeks of conservative therapy",
            appeal.for_denial.qa_context or "",
        )

    async def test_tool_asks_for_details_when_no_letter_context(self):
        chat = await self._make_chat("lettertool2", "9999930002")
        tool = GenerateAppealLetterTool(AsyncMock(), AsyncMock())
        with patch(
            "fighthealthinsurance.chat.tools.generate_appeal_letter_tool."
            "draft_letter_for_chat",
            new=AsyncMock(return_value=DraftedLetter(GENERATED_LETTER, True)),
        ) as mock_draft:
            response, _, handled = await tool.handle(
                "**generate_appeal_letter**{}", "", chat=chat
            )
        self.assertTrue(handled)
        mock_draft.assert_not_awaited()
        self.assertNotIn("generate_appeal_letter", response)
        self.assertIn("procedure or service", response)

    async def test_tool_failure_keeps_appeal_link(self):
        chat = await self._make_chat("lettertool3", "9999930003")
        tool = GenerateAppealLetterTool(AsyncMock(), AsyncMock())
        with patch(
            "fighthealthinsurance.chat.tools.generate_appeal_letter_tool."
            "draft_letter_for_chat",
            new=AsyncMock(return_value=None),
        ):
            response, _, handled = await tool.handle(
                '**generate_appeal_letter**{"procedure": "MRI"}', "", chat=chat
            )
        self.assertTrue(handled)
        self.assertNotIn("generate_appeal_letter", response)
        self.assertIn("/appeals/", response)
        appeal = await Appeal.objects.select_related("for_denial").aget(chat=chat)
        self.assertEqual(appeal.for_denial.procedure, "MRI")

    async def test_invalid_json_strips_tool_syntax(self):
        chat = await self._make_chat("lettertool4", "9999930004")
        error = AsyncMock()
        tool = GenerateAppealLetterTool(AsyncMock(), error)
        response, _, handled = await tool.handle(
            "Here you go.\n**generate_appeal_letter**{not valid json}",
            "",
            chat=chat,
        )
        self.assertTrue(handled)
        self.assertNotIn("generate_appeal_letter", response)
        error.assert_awaited()


    async def test_second_letter_call_in_a_turn_is_not_drafted(self):
        """An earlier pass of the turn (before a recursive research pass)
        already drafted the letter: a repeat call is stripped with a notice
        instead of drafting a second letter over the first."""
        chat = await self._make_chat("lettertool5", "9999930005")
        tool = GenerateAppealLetterTool(
            AsyncMock(),
            AsyncMock(),
            drafted_this_turn=[DraftedLetter(GENERATED_LETTER, True)],
        )
        with patch(
            "fighthealthinsurance.chat.tools.generate_appeal_letter_tool."
            "draft_letter_for_chat",
            new=AsyncMock(return_value=DraftedLetter(RESERVE_LETTER, True)),
        ) as mock_draft:
            response, _, _ = await tool.handle(
                "More detail from the research.\n"
                '**generate_appeal_letter**{"procedure": "MRI"}',
                "",
                chat=chat,
            )
        mock_draft.assert_not_awaited()
        self.assertNotIn("generate_appeal_letter", response)
        self.assertIn("drafted one letter", response)

    async def test_drafted_letter_is_recorded_for_the_turn(self):
        chat = await self._make_chat("lettertool6", "9999930006")
        drafted_this_turn = [None]
        tool = GenerateAppealLetterTool(
            AsyncMock(), AsyncMock(), drafted_this_turn=drafted_this_turn
        )
        with patch(
            "fighthealthinsurance.chat.tools.generate_appeal_letter_tool."
            "draft_letter_for_chat",
            new=AsyncMock(return_value=DraftedLetter(GENERATED_LETTER, True)),
        ):
            await tool.handle(
                '**generate_appeal_letter**{"procedure": "MRI"}', "", chat=chat
            )
        self.assertEqual(drafted_this_turn, [DraftedLetter(GENERATED_LETTER, True)])

    async def test_tool_without_chat_strips_the_call(self):
        """A declined call must not render its raw payload."""
        tool = GenerateAppealLetterTool(AsyncMock(), AsyncMock())
        response, _, _ = await tool.handle(
            'Sure.\n**generate_appeal_letter**{"procedure": "MRI"}', "", chat=None
        )
        self.assertNotIn("generate_appeal_letter", response)


    async def test_numeric_procedure_still_reaches_the_letter_pipeline(self):
        """A CPT code sent as a JSON number must not crash the context gate,
        which calls str methods on the in-memory denial."""
        chat = await self._make_chat("lettertool7", "9999930007")
        tool = GenerateAppealLetterTool(AsyncMock(), AsyncMock())
        with patch(
            "fighthealthinsurance.chat.tools.generate_appeal_letter_tool."
            "draft_letter_for_chat",
            new=AsyncMock(return_value=DraftedLetter(GENERATED_LETTER, True)),
        ) as mock_draft:
            await tool.handle(
                '**generate_appeal_letter**{"procedure": 72148}', "", chat=chat
            )
        self.assertEqual(mock_draft.await_args.kwargs["denial"].procedure, "72148")

    async def test_null_for_a_required_field_does_not_block_the_letter(self):
        """The prompt asks for whichever fields the model knows: a null
        denial_text must not fail the denial save and lose the letter."""
        chat = await self._make_chat("lettertool8", "9999939008")
        tool = GenerateAppealLetterTool(AsyncMock(), AsyncMock())
        with patch(
            "fighthealthinsurance.chat.tools.generate_appeal_letter_tool."
            "draft_letter_for_chat",
            new=AsyncMock(return_value=DraftedLetter(GENERATED_LETTER, True)),
        ) as mock_draft:
            await tool.handle(
                '**generate_appeal_letter**{"procedure": "MRI", "denial_text": null}',
                "",
                chat=chat,
            )
        mock_draft.assert_awaited_once()

    async def test_drafting_error_reaches_the_user_without_its_text(self):
        """The exception text can quote a database row, PHI included."""
        chat = await self._make_chat("lettertool9", "9999939009")
        error = AsyncMock()
        tool = GenerateAppealLetterTool(AsyncMock(), error)
        with patch(
            "fighthealthinsurance.chat.tools.generate_appeal_letter_tool."
            "draft_letter_for_chat",
            new=AsyncMock(side_effect=RuntimeError("Failing row contains (PHI)")),
        ):
            await tool.handle(
                '**generate_appeal_letter**{"procedure": "MRI"}', "", chat=chat
            )
        self.assertNotIn("Failing row", error.await_args.args[0])

    async def test_letter_survives_a_client_that_left_while_drafting(self):
        """The letter is drafted and saved by the time the status frames
        find the client gone. They must not swap it for a failure message,
        or the reconnect replay shows that instead of the saved letter."""
        chat = await self._make_chat("lettertool10", "9999939010")
        client_gone = False

        async def status(message):
            if client_gone:
                raise ClientGone()

        async def draft(**kwargs):
            nonlocal client_gone
            client_gone = True
            return DraftedLetter(GENERATED_LETTER, True)

        tool = GenerateAppealLetterTool(status, AsyncMock(side_effect=ClientGone()))
        with patch(
            "fighthealthinsurance.chat.tools.generate_appeal_letter_tool."
            "draft_letter_for_chat",
            new=draft,
        ):
            response, _, _ = await tool.handle(
                '**generate_appeal_letter**{"procedure": "MRI"}', "", chat=chat
            )
        self.assertIn(GENERATED_LETTER, response)

    async def test_call_abandoned_before_drafting_is_answered_about_the_letter(self):
        """A reply that was only the call reads as a letter problem, not
        AppealTool's "problem saving those appeal details"."""
        chat = await self._make_chat("lettertool11", "9999939011")
        tool = GenerateAppealLetterTool(
            AsyncMock(side_effect=ClientGone()), AsyncMock(side_effect=ClientGone())
        )
        response, _, _ = await tool.handle(
            '**generate_appeal_letter**{"procedure": "MRI"}', "", chat=chat
        )
        self.assertIn("problem drafting the letter", response)

    async def test_letter_for_a_sent_appeal_says_it_went_out(self):
        """Not "already has a saved letter ... copy this draft if you prefer
        it": that letter was sent, and the reply has to say so."""
        chat = await self._make_chat("lettertool12", "9999939012")
        tool = GenerateAppealLetterTool(AsyncMock(), AsyncMock())
        sent = DraftedLetter(
            GENERATED_LETTER,
            saved_to_appeal=False,
            preserved_existing=True,
            appeal_sent=True,
        )
        with patch(
            "fighthealthinsurance.chat.tools.generate_appeal_letter_tool."
            "draft_letter_for_chat",
            new=AsyncMock(return_value=sent),
        ):
            response, _, _ = await tool.handle(
                '**generate_appeal_letter**{"procedure": "MRI"}', "", chat=chat
            )
        self.assertIn("has already been sent", response)

    async def test_tool_passes_its_stored_draft_rule_to_the_drafter(self):
        chat = await self._make_chat("lettertool13", "9999939013")
        tool = GenerateAppealLetterTool(AsyncMock(), AsyncMock(), use_reserve=False)
        with patch(
            "fighthealthinsurance.chat.tools.generate_appeal_letter_tool."
            "draft_letter_for_chat",
            new=AsyncMock(return_value=DraftedLetter(GENERATED_LETTER, True)),
        ) as mock_draft:
            await tool.handle(
                '**generate_appeal_letter**{"procedure": "MRI"}', "", chat=chat
            )
        self.assertFalse(mock_draft.await_args.kwargs["use_reserve"])


class LetterSelectionPolicyTest(APITestCase):
    """generate_letter_for_denial picks a letter, not just any model output."""

    def _denial(self):
        return Denial(
            denial_text="Denied as not medically necessary.",
            procedure="MRI",
            diagnosis="back pain",
        )

    async def test_long_model_letter_wins_immediately(self):
        long_letter = GeneratedAppeal(
            text="word " * 120, model_name="fhi-model", infer_type="full"
        )
        never_reached = GeneratedAppeal(
            text="word " * 400, model_name="other", infer_type="full"
        )
        with patch.object(
            common_view_logic.appealGenerator,
            "make_appeals",
            return_value=iter([long_letter, never_reached]),
        ):
            item = await generate_letter_for_denial(self._denial())
        self.assertIsNotNone(item)
        self.assertEqual(item.model_name, "fhi-model")

    async def test_short_model_output_loses_to_longer_static_template(self):
        """A medically-necessary one-liner must not beat a full static
        template letter just because it carries a model name."""
        one_liner = GeneratedAppeal(
            text="The MRI is medically necessary for diagnosis.",
            model_name="fhi-model",
        )
        static_letter = GeneratedAppeal(
            text="word " * 300,
            model_name=TEMPLATE_MODEL_NAME,
            context_level=CONTEXT_LEVEL_TEMPLATE,
        )
        with patch.object(
            common_view_logic.appealGenerator,
            "make_appeals",
            return_value=iter([one_liner, static_letter]),
        ):
            item = await generate_letter_for_denial(self._denial())
        self.assertEqual(item.text, static_letter.text)

    async def test_template_does_not_end_the_wait_for_a_model_letter(self):
        """make_appeals names its templates with a pseudo-model, so a model
        name alone must not count as a model's letter."""
        template = GeneratedAppeal(
            text="word " * 300,
            model_name=TEMPLATE_MODEL_NAME,
            context_level=CONTEXT_LEVEL_TEMPLATE,
        )
        model_letter = GeneratedAppeal(
            text="word " * 120, model_name="fhi-model", infer_type="full"
        )
        with patch.object(
            common_view_logic.appealGenerator,
            "make_appeals",
            return_value=iter([template, model_letter]),
        ):
            item = await generate_letter_for_denial(self._denial())
        self.assertEqual(item.model_name, "fhi-model")

    class _Template:
        """A specialized template whose static letter is RESERVE_LETTER."""

        name = "test"

        @classmethod
        def static_appeal(cls):
            return RESERVE_LETTER

    async def test_too_short_a_deadline_serves_a_template_without_models(self):
        """Inside make_appeals' give-up margin a model answer can't be used,
        so no model is asked."""
        with patch(
            "fighthealthinsurance.generate_appeal.detect_specialized_templates",
            return_value=[self._Template],
        ), patch.object(
            common_view_logic.appealGenerator, "make_appeals"
        ) as make_appeals:
            item = await generate_letter_for_denial(
                self._denial(), deadline_seconds=10
            )
        self.assertEqual((make_appeals.called, item.text), (False, RESERVE_LETTER))

    async def test_a_summary_that_leaves_too_little_time_serves_a_template(self):
        """The floor applies to the time left after the denial summary,
        which may take up to a third of the deadline."""
        clock = MagicMock()
        # Drafting starts at 0s; the summary returns at 20s of a 30s deadline.
        clock.monotonic.side_effect = itertools.chain([0.0], itertools.repeat(20.0))
        with patch(
            "fighthealthinsurance.generate_appeal.detect_specialized_templates",
            return_value=[self._Template],
        ), patch(
            "fighthealthinsurance.ml.ml_appeal_context_helper."
            "MLAppealContextHelper.maybe_summarize_denial_text",
            new=AsyncMock(return_value=None),
        ), patch(
            "fighthealthinsurance.chat.appeal_letter_generator.time", clock
        ), patch.object(
            common_view_logic.appealGenerator, "make_appeals"
        ) as make_appeals:
            item = await generate_letter_for_denial(
                self._denial(), deadline_seconds=30
            )
        self.assertEqual((make_appeals.called, item.text), (False, RESERVE_LETTER))

    async def test_runt_only_output_returns_none(self):
        runt = GeneratedAppeal(text="no", model_name="fhi-model")
        with patch.object(
            common_view_logic.appealGenerator,
            "make_appeals",
            return_value=iter([runt]),
        ):
            item = await generate_letter_for_denial(self._denial())
        self.assertIsNone(item)

    async def test_generation_failure_returns_none(self):
        with patch.object(
            common_view_logic.appealGenerator,
            "make_appeals",
            side_effect=RuntimeError("boom"),
        ):
            item = await generate_letter_for_denial(self._denial())
        self.assertIsNone(item)

    async def _consent_seen_by_the_summarizer(self, stored, chat_consent):
        denial = self._denial()
        denial.use_external = stored
        seen = []

        async def record_consent(summarized_denial):
            seen.append(summarized_denial.use_external)
            return None

        with patch(
            "fighthealthinsurance.ml.ml_appeal_context_helper."
            "MLAppealContextHelper.maybe_summarize_denial_text",
            new=AsyncMock(side_effect=record_consent),
        ), patch.object(
            common_view_logic.appealGenerator,
            "make_appeals",
            return_value=iter([]),
        ):
            await generate_letter_for_denial(denial, use_external=chat_consent)
        return seen

    async def test_stored_opt_out_is_not_widened_by_the_chat(self):
        """A denial whose owner declined outside models stays declined when
        the chat's toggle is left at its default (on)."""
        self.assertEqual(
            await self._consent_seen_by_the_summarizer(False, True), [False]
        )

    async def test_summarizer_sees_chat_consent_not_the_stored_opt_in(self):
        """This chat's use_external applies before ANY model call: the
        denial summarizer routes by denial.use_external too, and the stored
        flag may say yes where this user said no."""
        denial = self._denial()
        denial.use_external = True
        seen = []

        async def record_consent(summarized_denial):
            seen.append(summarized_denial.use_external)
            return None

        with patch(
            "fighthealthinsurance.ml.ml_appeal_context_helper."
            "MLAppealContextHelper.maybe_summarize_denial_text",
            new=AsyncMock(side_effect=record_consent),
        ), patch.object(
            common_view_logic.appealGenerator,
            "make_appeals",
            return_value=iter([]),
        ):
            await generate_letter_for_denial(denial, use_external=False)
        self.assertEqual(seen, [False])


class FindReserveLetterTest(APITestCase):
    """Reserve lookup serves exactly what the wizard would, best first."""

    async def _denial(self, email, state="CA"):
        return await Denial.objects.acreate(
            denial_text="denied",
            hashed_email=Denial.get_hashed_email(email),
            your_state=state,
        )

    async def test_prefers_live_over_speculative_and_skips_runts(self):
        denial = await self._denial("a@b.com")
        await ProposedAppeal.objects.acreate(
            appeal_text=RESERVE_LETTER + " speculative extra text here",
            for_denial=denial,
            speculative=True,
            built_for_state="CA",
        )
        await ProposedAppeal.objects.acreate(
            appeal_text=RESERVE_LETTER, for_denial=denial, speculative=False
        )
        await ProposedAppeal.objects.acreate(
            appeal_text="runt", for_denial=denial, speculative=False
        )
        found = await find_reserve_letter(denial)
        # The live row wins even though the (servable) reserve is longer.
        self.assertEqual(found, RESERVE_LETTER)

    async def test_junk_rows_cannot_evict_reserve_from_the_window(self):
        """A pile of runt live drafts (a degraded-model period) must not fill
        the bounded lookup window and hide the one deliverable reserve --
        the runt filter runs in SQL before the slice."""
        denial = await self._denial("e@f.com")
        for i in range(12):
            # Distinct texts: drafts are unique per (denial, fingerprint).
            await ProposedAppeal.objects.acreate(
                appeal_text=f"junk {i}", for_denial=denial, speculative=False
            )
        await ProposedAppeal.objects.acreate(
            appeal_text=RESERVE_LETTER,
            for_denial=denial,
            speculative=True,
            built_for_state="CA",
        )
        self.assertEqual(await find_reserve_letter(denial), RESERVE_LETTER)

    async def test_reserve_written_for_another_state_is_not_served(self):
        """A held-back reserve argues under the state it was written for;
        once the case names another state it must not be served."""
        denial = await self._denial("g@h.com", state="CA")
        await ProposedAppeal.objects.acreate(
            appeal_text=RESERVE_LETTER,
            for_denial=denial,
            speculative=True,
            built_for_state="NY",
        )
        self.assertIsNone(await find_reserve_letter(denial))

    async def test_unstamped_legacy_reserve_is_not_served(self):
        """A reserve from before the state stamp is unknown, and unknown is
        treated as another state."""
        denial = await self._denial("i@j.com")
        await ProposedAppeal.objects.acreate(
            appeal_text=RESERVE_LETTER, for_denial=denial, speculative=True
        )
        self.assertIsNone(await find_reserve_letter(denial))

    async def test_chosen_row_is_not_served(self):
        """Chosen rows are copies of the user's own pick (possibly text they
        wrote) -- never presented back as a draft the generator produced."""
        denial = await self._denial("k@l.com")
        await ProposedAppeal.objects.acreate(
            appeal_text=RESERVE_LETTER, for_denial=denial, chosen=True
        )
        self.assertIsNone(await find_reserve_letter(denial))

    async def test_higher_quality_draft_beats_longer_unscored_one(self):
        """Within a group the draft main would rank first wins
        (letter_quality.sort_key), not merely the longest."""
        denial = await self._denial("m@n.com")
        await ProposedAppeal.objects.acreate(
            appeal_text=RESERVE_LETTER + " plus a much longer unscored tail",
            for_denial=denial,
        )
        await ProposedAppeal.objects.acreate(
            appeal_text=RESERVE_LETTER,
            for_denial=denial,
            quality_score=0.9,
            grounding_score=2.0,
        )
        self.assertEqual(await find_reserve_letter(denial), RESERVE_LETTER)

    async def test_returns_none_when_no_deliverable_rows(self):
        denial = await self._denial("c@d.com")
        await ProposedAppeal.objects.acreate(
            appeal_text="runt", for_denial=denial, speculative=False
        )
        self.assertIsNone(await find_reserve_letter(denial))


class DraftLetterForChatTest(APITestCase):
    """draft_letter_for_chat persistence behavior."""

    async def test_generated_letter_saved_to_appeal_without_a_draft_row(self):
        """The letter lands on the appeal, but no ProposedAppeal row: live
        drafts belong to the denial's generation lease, which chat does not
        hold."""
        user, chat = await _make_professional_chat("draftpersist1", "9999940001")
        appeal, denial = await _link_letter_appeal(chat, user)
        generated = GeneratedAppeal(
            text=GENERATED_LETTER, model_name="fhi-model", context_level="full"
        )
        with patch(
            "fighthealthinsurance.chat.appeal_letter_generator."
            "generate_letter_for_denial",
            new=AsyncMock(return_value=generated),
        ):
            drafted = await draft_letter_for_chat(
                appeal=appeal, denial=denial, use_external=False
            )
        self.assertEqual(drafted, DraftedLetter(GENERATED_LETTER, True))
        fresh_appeal = await Appeal.objects.aget(id=appeal.id)
        self.assertEqual(fresh_appeal.appeal_text, GENERATED_LETTER)
        self.assertEqual(
            await ProposedAppeal.objects.filter(for_denial=denial).acount(), 0
        )

    async def test_reserve_reuse_creates_no_duplicate_row(self):
        user, chat = await _make_professional_chat("draftpersist2", "9999940002")
        appeal, denial = await _link_letter_appeal(chat, user)
        await ProposedAppeal.objects.acreate(
            appeal_text=RESERVE_LETTER, for_denial=denial
        )
        drafted = await draft_letter_for_chat(
            appeal=appeal, denial=denial, use_external=False, prefer_existing=True
        )
        self.assertEqual(drafted.text, RESERVE_LETTER)
        self.assertEqual(
            await ProposedAppeal.objects.filter(for_denial=denial).acount(), 1
        )

    async def test_reserve_does_not_overwrite_existing_letter(self):
        """A reserve draft must never clobber a real (possibly user-edited)
        letter already on the appeal; it is delivered without saving."""
        user, chat = await _make_professional_chat("draftpersist4", "9999940004")
        appeal, denial = await _link_letter_appeal(chat, user)
        edited = (
            "My carefully hand-edited appeal letter about the MRI denial, "
            "which must not be replaced by a stale precomputed draft."
        )
        appeal.appeal_text = edited
        await appeal.asave()
        await ProposedAppeal.objects.acreate(
            appeal_text=RESERVE_LETTER, for_denial=denial
        )
        drafted = await draft_letter_for_chat(
            appeal=appeal, denial=denial, use_external=False, prefer_existing=True
        )
        self.assertEqual(drafted, DraftedLetter(RESERVE_LETTER, False, True))
        fresh = await Appeal.objects.aget(id=appeal.id)
        self.assertEqual(fresh.appeal_text, edited)

    async def test_served_letter_gets_the_wizards_placeholder_fill(self):
        user, chat = await _make_professional_chat("draftpersist5", "9999940005")
        appeal, denial = await _link_letter_appeal(
            chat, user, insurance_company="Acme Health"
        )
        await ProposedAppeal.objects.acreate(
            appeal_text="Dear Insurance Company, " + RESERVE_LETTER,
            for_denial=denial,
        )
        drafted = await draft_letter_for_chat(
            appeal=appeal, denial=denial, use_external=False, prefer_existing=True
        )
        self.assertEqual(drafted.text, "Dear Acme Health, " + RESERVE_LETTER)

    async def test_failed_appeal_save_reported_not_hidden(self):
        """A letter whose appeal save failed is still delivered, but flagged
        so callers don't claim it was saved to the appeal."""
        user, chat = await _make_professional_chat("draftpersist3", "9999940003")
        appeal, denial = await _link_letter_appeal(chat, user)
        await ProposedAppeal.objects.acreate(
            appeal_text=RESERVE_LETTER, for_denial=denial
        )
        with patch(
            "django.db.models.query.QuerySet.aupdate",
            new=AsyncMock(side_effect=RuntimeError("db down")),
        ):
            drafted = await draft_letter_for_chat(
                appeal=appeal, denial=denial, use_external=False, prefer_existing=True
            )
        self.assertEqual(drafted, DraftedLetter(RESERVE_LETTER, False))

    async def test_request_for_a_new_letter_is_not_answered_by_a_reserve(self):
        """A draft made before whatever the person wants changed is no
        answer to a redo; with generation failing there is no letter."""
        user, chat = await _make_professional_chat("draftpersist6", "9999940006")
        appeal, denial = await _link_letter_appeal(chat, user)
        await ProposedAppeal.objects.acreate(
            appeal_text=RESERVE_LETTER, for_denial=denial
        )
        with patch(
            "fighthealthinsurance.chat.appeal_letter_generator."
            "generate_letter_for_denial",
            new=AsyncMock(return_value=None),
        ):
            drafted = await draft_letter_for_chat(
                appeal=appeal,
                denial=denial,
                use_external=False,
                prefer_existing=True,
                use_reserve=False,
            )
        self.assertIsNone(drafted)


class PairedToolCallsTest(APITestCase):
    """Two anchored tool calls in one reply must not swallow each other.

    The greedy anchored patterns run under DOTALL, so before precise payload
    extraction (parse_anchored_json_payload) the first handler's capture ran
    through the second call's closing brace -- json failed and BOTH calls
    were stripped. Handlers run in chat_interface order: AppealTool first,
    then GenerateAppealLetterTool.
    """

    async def _run_both_tools(self, chat, response_text):
        appeal_tool = AppealTool(AsyncMock(), AsyncMock())
        response_text, context, _ = await appeal_tool.handle(
            response_text, "", chat=chat
        )
        letter_tool = GenerateAppealLetterTool(AsyncMock(), AsyncMock())
        with patch(
            "fighthealthinsurance.chat.tools.generate_appeal_letter_tool."
            "draft_letter_for_chat",
            new=AsyncMock(return_value=DraftedLetter(GENERATED_LETTER, True)),
        ) as mock_draft:
            response_text, context, _ = await letter_tool.handle(
                response_text, context, chat=chat
            )
        return response_text, mock_draft

    async def test_appeal_then_letter_calls_both_run(self):
        _, chat = await _make_professional_chat("paired1", "9999950001")
        response = (
            "Setting up your appeal first.\n"
            '**create_or_update_appeal**{"procedure": "MRI", '
            '"diagnosis": "chronic back pain"}\n'
            "Now drafting the letter.\n"
            '**generate_appeal_letter**{"insurance_company": "Acme Health"}'
        )
        result, mock_draft = await self._run_both_tools(chat, response)
        mock_draft.assert_awaited_once()
        self.assertNotIn("create_or_update_appeal", result)
        self.assertNotIn("generate_appeal_letter", result)
        # The prose between the calls survives both replacements.
        self.assertIn("Now drafting the letter.", result)
        self.assertIn(GENERATED_LETTER, result)
        appeal = await Appeal.objects.select_related("for_denial").aget(chat=chat)
        self.assertEqual(appeal.for_denial.procedure, "MRI")
        self.assertEqual(appeal.for_denial.insurance_company, "Acme Health")

    async def test_letter_then_appeal_calls_both_run(self):
        _, chat = await _make_professional_chat("paired2", "9999950002")
        response = (
            '**generate_appeal_letter**{"procedure": "MRI"}\n'
            "Also recording the diagnosis.\n"
            '**create_or_update_appeal**{"diagnosis": "chronic back pain"}'
        )
        result, mock_draft = await self._run_both_tools(chat, response)
        mock_draft.assert_awaited_once()
        self.assertNotIn("create_or_update_appeal", result)
        self.assertNotIn("generate_appeal_letter", result)
        self.assertIn("Also recording the diagnosis.", result)
        appeal = await Appeal.objects.select_related("for_denial").aget(chat=chat)
        self.assertEqual(appeal.for_denial.procedure, "MRI")
        self.assertEqual(appeal.for_denial.diagnosis, "chronic back pain")


class MultiCallAndErrorStripTest(APITestCase):
    """Same-tool duplicates execute per call; error strips stay span-bounded."""

    async def test_two_appeal_calls_both_apply(self):
        """Each call of the same anchored tool gets its own pass -- neither
        renders as raw syntax nor silently loses its field updates."""
        _, chat = await _make_professional_chat("multicall1", "9999960001")
        response = (
            '**create_or_update_appeal**{"procedure": "MRI"}\n'
            "Also recording:\n"
            '**create_or_update_appeal**{"diagnosis": "chronic back pain"}'
        )
        tool = AppealTool(AsyncMock(), AsyncMock())
        result, _, handled = await tool.handle(response, "", chat=chat)
        self.assertTrue(handled)
        self.assertNotIn("create_or_update_appeal", result)
        self.assertIn("Also recording:", result)
        appeal = await Appeal.objects.select_related("for_denial").aget(chat=chat)
        self.assertEqual(appeal.for_denial.procedure, "MRI")
        self.assertEqual(appeal.for_denial.diagnosis, "chronic back pain")

    async def test_duplicate_letter_calls_draft_once_and_strip_stragglers(self):
        """One letter per turn: the second call is stripped, not re-drafted."""
        _, chat = await _make_professional_chat("multicall2", "9999960002")
        response = (
            '**generate_appeal_letter**{"procedure": "MRI"}\n'
            "And again for luck:\n"
            '**generate_appeal_letter**{"procedure": "MRI scan"}'
        )
        tool = GenerateAppealLetterTool(AsyncMock(), AsyncMock())
        with patch(
            "fighthealthinsurance.chat.tools.generate_appeal_letter_tool."
            "draft_letter_for_chat",
            new=AsyncMock(return_value=DraftedLetter(GENERATED_LETTER, True)),
        ) as mock_draft:
            result, _, handled = await tool.handle(response, "", chat=chat)
        self.assertTrue(handled)
        mock_draft.assert_awaited_once()
        self.assertNotIn("generate_appeal_letter", result)
        self.assertEqual(result.count(GENERATED_LETTER), 1)
        # The dropped duplicate is acknowledged rather than vanishing.
        self.assertIn("drafted one letter", result)

    async def test_failed_draft_strips_a_duplicate_call_without_claiming_a_letter(
        self,
    ):
        """No letter, no "I drafted one letter" notice: the reply already
        says why there is none."""
        _, chat = await _make_professional_chat("multicall5", "9999969005")
        response = (
            '**generate_appeal_letter**{"procedure": "MRI"}\n'
            '**generate_appeal_letter**{"procedure": "MRI scan"}'
        )
        tool = GenerateAppealLetterTool(AsyncMock(), AsyncMock())
        with patch(
            "fighthealthinsurance.chat.tools.generate_appeal_letter_tool."
            "draft_letter_for_chat",
            new=AsyncMock(return_value=None),
        ):
            result, _, _ = await tool.handle(response, "", chat=chat)
        self.assertNotIn("drafted one letter", result)

    async def test_appeal_update_lands_on_the_call_not_a_quote_of_it(self):
        """A copy of the call quoted in the prose keeps its text. Replacing
        the first copy instead left the real call in place to run again."""
        _, chat = await _make_professional_chat("multicall6", "9999969006")
        quoted = "I'll send `**create_or_update_appeal**{\"procedure\": \"MRI\"}` now:"
        response = quoted + '\n**create_or_update_appeal**{"procedure": "MRI"}'
        tool = AppealTool(AsyncMock(), AsyncMock())
        result, _, _ = await tool.handle(response, "", chat=chat)
        self.assertTrue(result.startswith(quoted + "\nI've created/updated"))

    async def test_calls_past_the_cap_are_stripped_not_leaked(self):
        """A reply with more calls than max_calls_per_reply executes the
        first three and strips the rest -- raw tool syntax never renders."""
        _, chat = await _make_professional_chat("multicall4", "9999960004")
        response = (
            '**create_or_update_appeal**{"procedure": "MRI"}\n'
            '**create_or_update_appeal**{"diagnosis": "chronic back pain"}\n'
            '**create_or_update_appeal**{"insurance_company": "Acme Health"}\n'
            '**create_or_update_appeal**{"employer_name": "Overflow Corp"}'
        )
        tool = AppealTool(AsyncMock(), AsyncMock())
        result, _, handled = await tool.handle(response, "", chat=chat)
        self.assertTrue(handled)
        self.assertNotIn("create_or_update_appeal", result)
        self.assertNotIn("Overflow Corp", result)
        appeal = await Appeal.objects.select_related("for_denial").aget(chat=chat)
        # First three applied; the over-cap call was stripped, not executed.
        self.assertEqual(appeal.for_denial.procedure, "MRI")
        self.assertEqual(appeal.for_denial.diagnosis, "chronic back pain")
        self.assertEqual(appeal.for_denial.insurance_company, "Acme Health")
        self.assertIsNone(appeal.for_denial.employer_name)
        # The drop is stated, not silent: this text is what the user reads
        # AND what the model sees as history, so an unannounced drop would
        # leave both believing the update landed.
        self.assertIn("hasn't been saved", result)

    async def test_appeal_tool_error_leaves_letter_call_intact(self):
        """When AppealTool's execute blows up, the on-error strip must not
        swallow the prose or the pending generate_appeal_letter call (the
        old greedy re.sub deleted everything between the two calls)."""
        _, chat = await _make_professional_chat("multicall3", "9999960003")
        response = (
            '**create_or_update_appeal**{"procedure": "MRI"}\n'
            "Now drafting the letter.\n"
            '**generate_appeal_letter**{"procedure": "MRI"}'
        )
        tool = AppealTool(AsyncMock(), AsyncMock())
        with patch.object(
            AppealTool,
            "_get_or_create_appeal",
            new=AsyncMock(side_effect=RuntimeError("db down")),
        ):
            result, _, handled = await tool.handle(response, "", chat=chat)
        self.assertTrue(handled)
        self.assertNotIn("create_or_update_appeal", result)
        self.assertIn("Now drafting the letter.", result)
        self.assertIn("generate_appeal_letter", result)


    async def test_declined_call_sends_its_error_once(self):
        """A call execute() declines (no chat) is not retried up to the cap,
        repeating the same error to the user each time."""
        error = AsyncMock()
        tool = AppealTool(AsyncMock(), error)
        await tool.handle(
            'Noted.\n**create_or_update_appeal**{"procedure": "MRI"}', "", chat=None
        )
        self.assertEqual(error.await_count, 1)

    async def test_declined_call_is_stripped_without_a_cap_notice(self):
        """It was declined, not dropped for exceeding the cap."""
        tool = AppealTool(AsyncMock(), AsyncMock())
        result, _, _ = await tool.handle(
            'Noted.\n**create_or_update_appeal**{"procedure": "MRI"}', "", chat=None
        )
        self.assertNotIn("create_or_update_appeal", result)
        self.assertNotIn("hasn't been saved", result)


class LetterDeadlineClampTest(APITestCase):
    """The letter tool's deadline is clamped to the turn budget's remainder."""

    def _interface(self):
        from unittest.mock import MagicMock

        return ChatInterface(
            send_json_message_func=AsyncMock(), chat=MagicMock(), user=None
        )

    def test_none_outside_a_budgeted_turn(self):
        self.assertIsNone(self._interface()._remaining_letter_deadline())

    def test_clamped_to_remaining_budget(self):
        import os
        import time as time_mod

        interface = self._interface()
        interface._turn_deadline = time_mod.monotonic() + 40.0
        # Pin the env default so an FHI_CHAT_LETTER_DEADLINE set in the
        # environment can't change what the clamp is compared against.
        with patch.dict(os.environ, {"FHI_CHAT_LETTER_DEADLINE": "75"}):
            deadline = interface._remaining_letter_deadline()
        # ~40s left minus the 15s margin, well under the 75s default.
        self.assertLess(deadline, 30.0)
        self.assertGreater(deadline, 20.0)

    def test_floored_when_budget_nearly_exhausted(self):
        import time as time_mod

        interface = self._interface()
        interface._turn_deadline = time_mod.monotonic() + 1.0
        self.assertEqual(interface._remaining_letter_deadline(), 10.0)

    def test_capped_at_env_default_when_budget_is_ample(self):
        import os
        import time as time_mod

        interface = self._interface()
        interface._turn_deadline = time_mod.monotonic() + 10_000.0
        with patch.dict(os.environ, {"FHI_CHAT_LETTER_DEADLINE": "75"}):
            self.assertEqual(interface._remaining_letter_deadline(), 75.0)


class FillLetterPlaceholdersTest(APITestCase):
    """Chat letters get the wizard's own placeholder substitution."""

    async def test_fills_known_fields_and_keeps_unknown_placeholders(self):
        denial = await Denial.objects.acreate(
            denial_text="denied",
            hashed_email=Denial.get_hashed_email("sub1@x.com"),
            insurance_company="Acme Health",
            claim_id="CLM-123",
            procedure="UNKNOWN",
        )
        result = await fill_letter_placeholders(
            "Dear Sir/Madam, re [Claim # Placeholder] for {procedure} "
            "({diagnosis}).",
            denial,
        )
        # Spellings the old four-field subset never handled.
        self.assertIn("Dear Acme Health", result)
        self.assertIn("CLM-123", result)
        # The extractor's UNKNOWN marker and a missing value keep the blank.
        self.assertIn("{procedure}", result)
        self.assertIn("{diagnosis}", result)

    async def test_claim_id_matching_insurance_company_keeps_placeholder(self):
        denial = await Denial.objects.acreate(
            denial_text="denied",
            hashed_email=Denial.get_hashed_email("sub2@x.com"),
            insurance_company="Acme Health",
            claim_id="Acme Health",  # a known extractor failure mode
        )
        result = await fill_letter_placeholders("re claim {claim_id}", denial)
        self.assertIn("{claim_id}", result)


class OverwritePolicyTest(APITestCase):
    """Only a full model-written draft may replace a letter on the appeal."""

    async def _appeal_with_edited_letter(self, username, npi):
        user, chat = await _make_professional_chat(username, npi)
        appeal, denial = await _link_letter_appeal(chat, user)
        appeal.appeal_text = EDITED_LETTER
        await appeal.asave()
        return appeal, denial

    async def _draft_with(self, appeal, denial, generated):
        with patch(
            "fighthealthinsurance.chat.appeal_letter_generator."
            "generate_letter_for_denial",
            new=AsyncMock(return_value=generated),
        ):
            return await draft_letter_for_chat(
                appeal=appeal, denial=denial, use_external=False
            )

    async def test_zero_model_template_does_not_replace_edited_letter(self):
        """make_appeals names templates with a pseudo-model; that name must
        not pass them off as a model's letter."""
        appeal, denial = await self._appeal_with_edited_letter(
            "overwrite1", "9999970001"
        )
        template = GeneratedAppeal(
            text=FULL_MODEL_LETTER,
            model_name=TEMPLATE_MODEL_NAME,
            context_level=CONTEXT_LEVEL_TEMPLATE,
        )
        drafted = await self._draft_with(appeal, denial, template)
        self.assertTrue(drafted.preserved_existing)
        fresh = await Appeal.objects.aget(id=appeal.id)
        self.assertEqual(fresh.appeal_text, EDITED_LETTER)

    async def test_short_model_fragment_does_not_replace_edited_letter(self):
        appeal, denial = await self._appeal_with_edited_letter(
            "overwrite2", "9999970002"
        )
        fragment = GeneratedAppeal(
            text=GENERATED_LETTER, model_name="fhi-model", infer_type="full"
        )
        drafted = await self._draft_with(appeal, denial, fragment)
        self.assertTrue(drafted.preserved_existing)
        fresh = await Appeal.objects.aget(id=appeal.id)
        self.assertEqual(fresh.appeal_text, EDITED_LETTER)

    async def test_medically_necessary_paragraph_does_not_replace_letter(self):
        """A model's reason paragraph, however long, is not a letter."""
        appeal, denial = await self._appeal_with_edited_letter(
            "overwrite4", "9999970004"
        )
        reason = GeneratedAppeal(
            text=FULL_MODEL_LETTER,
            model_name="fhi-model",
            infer_type="medically_necessary",
        )
        await self._draft_with(appeal, denial, reason)
        fresh = await Appeal.objects.aget(id=appeal.id)
        self.assertEqual(fresh.appeal_text, EDITED_LETTER)

    async def test_full_draft_never_replaces_a_sent_appeals_letter(self):
        """A sent appeal's text records what went out."""
        appeal, denial = await self._appeal_with_edited_letter(
            "overwrite5", "9999970005"
        )
        await Appeal.objects.filter(id=appeal.id).aupdate(sent=True)
        full = GeneratedAppeal(
            text=FULL_MODEL_LETTER, model_name="fhi-model", infer_type="full"
        )
        await self._draft_with(appeal, denial, full)
        fresh = await Appeal.objects.aget(id=appeal.id)
        self.assertEqual(fresh.appeal_text, EDITED_LETTER)

    async def test_letter_saved_while_drafting_is_kept_from_a_fragment(self):
        """The overwrite check reads the appeal as it is when the draft is
        done, not as it was when drafting began (empty, here)."""
        user, chat = await _make_professional_chat("overwrite6", "9999970006")
        appeal, denial = await _link_letter_appeal(chat, user)

        async def person_saves_a_letter_meanwhile(*args, **kwargs):
            await Appeal.objects.filter(id=appeal.id).aupdate(
                appeal_text=EDITED_LETTER
            )
            return GeneratedAppeal(
                text=GENERATED_LETTER, model_name="fhi-model", infer_type="full"
            )

        with patch(
            "fighthealthinsurance.chat.appeal_letter_generator."
            "generate_letter_for_denial",
            new=AsyncMock(side_effect=person_saves_a_letter_meanwhile),
        ):
            await draft_letter_for_chat(
                appeal=appeal, denial=denial, use_external=False
            )
        fresh = await Appeal.objects.aget(id=appeal.id)
        self.assertEqual(fresh.appeal_text, EDITED_LETTER)

    async def test_full_model_redraft_replaces_existing_letter(self):
        """An explicit redraft that produced a real letter does replace it."""
        appeal, denial = await self._appeal_with_edited_letter(
            "overwrite3", "9999970003"
        )
        full = GeneratedAppeal(
            text=FULL_MODEL_LETTER, model_name="fhi-model", infer_type="full"
        )
        drafted = await self._draft_with(appeal, denial, full)
        self.assertTrue(drafted.saved_to_appeal)
        fresh = await Appeal.objects.aget(id=appeal.id)
        self.assertEqual(fresh.appeal_text, FULL_MODEL_LETTER)
