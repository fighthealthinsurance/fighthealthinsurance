"""Eligibility turns run the checker whether or not the model asked for it.

The system prompt tells the model to answer "do I qualify for Medicaid?"
with a ``medicaid_eligibility`` tool call and nothing else. These tests drive
whole turns through ChatInterface against a backend that does NOT do that --
it answers from memory -- and pin what the turn does about it: the request
note it appends to the model's message, the call it injects when the reply
carries none, the state it reads off the person's words, and the cases
where it must leave the reply alone.
"""

from unittest.mock import AsyncMock, patch

from rest_framework.test import APITestCase

from fighthealthinsurance.chat.eligibility_intent import (
    ELIGIBILITY_INTERVIEW_NOTE,
    ELIGIBILITY_REQUEST_NOTE,
)
from fighthealthinsurance.chat_interface import ChatInterface
from fighthealthinsurance.models import OngoingChat
from tests.chat_fixtures import (
    FrameRecorder,
    RecordingChatModel,
    discard_background_task,
    make_professional_chat,
)

# Opens the follow-up message the eligibility tool hands the model.
CHECKER_MARKER = "EXPERIMENTAL eligibility checker"
# What the checker asks first when it was given no state.
STATE_QUESTION = "state"

MEMORY_ANSWER = "You probably make too much to qualify, sorry about that."
CHECKER_WRITE_UP = "Which state are you in? (This experimental check can be wrong.)"


class MemoryAnsweringModel(RecordingChatModel):
    """A backend that answers eligibility questions from memory -- no tool
    call -- and writes up whatever the checker's follow-up hands it."""

    def __init__(self, user_turn_reply=MEMORY_ANSWER, follow_up_reply=CHECKER_WRITE_UP):
        super().__init__()
        self._user_turn_reply = user_turn_reply
        self._follow_up_reply = follow_up_reply

    async def generate_chat_response(self, current_message_for_llm, **kwargs):
        """The follow-up reply for the checker's message, else a fresh
        memory answer (numbered so later turns aren't repeats)."""
        await super().generate_chat_response(current_message_for_llm, **kwargs)
        if CHECKER_MARKER in (current_message_for_llm or ""):
            return (self._follow_up_reply, "Summary: eligibility check underway.")
        return (
            f"{self._user_turn_reply} (reply {len(self.calls)})",
            "Summary: answered from memory.",
        )


def _messages(model: RecordingChatModel) -> list[str]:
    """Every message the model was asked to reply to, in order."""
    return [call["message"] or "" for call in model.calls]


def _follow_ups(model: RecordingChatModel) -> list[str]:
    """The messages that carried the checker's follow-up."""
    return [m for m in _messages(model) if CHECKER_MARKER in m]


def _assistant_replies(recorder: FrameRecorder) -> list[str]:
    """The assistant replies the recorder saw go to the client."""
    return [f["content"] for f in recorder.frames if f.get("role") == "assistant"]


class EligibilityInjectionTest(APITestCase):
    def setUp(self):
        super().setUp()
        self.model = MemoryAnsweringModel()
        self.enterContext(
            patch(
                "fighthealthinsurance.ml.ml_router.MLRouter.get_chat_backends",
                side_effect=lambda *args, **kwargs: [self.model],
            )
        )
        self.enterContext(
            patch(
                "fighthealthinsurance.ml.ml_router.MLRouter.get_chat_backends_with_fallback",
                side_effect=lambda *args, **kwargs: ([self.model], []),
            )
        )
        self.enterContext(
            patch(
                "fighthealthinsurance.chat_interface.fire_and_forget_in_new_threadpool",
                new=AsyncMock(side_effect=discard_background_task),
            )
        )

    async def _turn(self, interface: ChatInterface, message: str) -> list[str]:
        """Run one turn; the assistant replies it sent the client."""
        before = len(self.recorder.frames)
        await interface.handle_chat_message(message)
        return [
            f["content"]
            for f in self.recorder.frames[before:]
            if f.get("role") == "assistant"
        ]

    async def _interface(self, username: str, npi: str, history=None):
        user, chat = await make_professional_chat(username, npi)
        if history:
            chat.chat_history = history
            await chat.asave(update_fields=["chat_history"])
        self.recorder = FrameRecorder()
        return ChatInterface(send_json_message_func=self.recorder, chat=chat, user=user)

    async def test_request_without_tool_call_runs_the_checker(self):
        interface = await self._interface("eliginject1", "9999960001")
        replies = await self._turn(interface, "Am I eligible for Medicaid?")

        # The checker's follow-up wrote the reply, not the model's memory.
        self.assertEqual(len(replies), 1)
        self.assertIn("Which state", replies[0])
        self.assertNotIn("make too much", replies[0])
        self.assertEqual(len(_follow_ups(self.model)), 1)
        # Given no state, the checker asked for it first.
        self.assertIn(STATE_QUESTION, _follow_ups(self.model)[0].lower())

    async def test_the_models_message_carries_the_request_note(self):
        interface = await self._interface("eliginject2", "9999960002")
        await self._turn(interface, "Am I eligible for Medicaid?")

        user_turn_messages = [m for m in _messages(self.model) if CHECKER_MARKER not in m]
        self.assertTrue(user_turn_messages)
        for message in user_turn_messages:
            self.assertIn(ELIGIBILITY_REQUEST_NOTE.strip(), message)

    async def test_injected_call_carries_the_state_from_the_message(self):
        interface = await self._interface("eliginject3", "9999960003")
        await self._turn(interface, "Do I qualify for Medi-Cal?")

        (follow_up,) = _follow_ups(self.model)
        # The checker echoes what it recorded; the state came from "Medi-Cal".
        self.assertIn("state: ca", follow_up)

    async def test_injected_call_takes_the_state_from_an_earlier_message(self):
        history = [
            {"role": "user", "content": "Hi, I'm in Texas and just lost my job."},
            {"role": "assistant", "content": "I'm sorry to hear that. How can I help?"},
        ]
        interface = await self._interface("eliginject4", "9999960004", history)
        await self._turn(interface, "Can I get Medicaid?")

        (follow_up,) = _follow_ups(self.model)
        self.assertIn("state: tx", follow_up)

    async def test_a_message_naming_two_states_gets_no_state_from_history(self):
        history = [
            {"role": "user", "content": "I live in Texas and just lost my job."},
            {"role": "assistant", "content": "I'm sorry to hear that. How can I help?"},
        ]
        interface = await self._interface("eliginject15", "9999960015", history)
        await self._turn(
            interface, "I moved from Texas to California. Am I eligible for Medicaid?"
        )

        (follow_up,) = _follow_ups(self.model)
        # Neither state was sent -- not Texas from the earlier message, which
        # they just said they left -- so the checker asks which one applies.
        self.assertNotIn("state: tx", follow_up)
        self.assertNotIn("state: ca", follow_up)
        self.assertIn(STATE_QUESTION, follow_up.lower())

    async def test_the_models_own_tool_call_is_used_not_replaced(self):
        self.model = MemoryAnsweringModel(
            user_turn_reply='**medicaid_eligibility {"state": "tx", "age": 30}**'
        )
        interface = await self._interface("eliginject5", "9999960005")
        replies = await self._turn(interface, "Am I eligible for Medicaid?")

        (follow_up,) = _follow_ups(self.model)
        # The age the model sent is what the checker recorded: its call ran.
        self.assertIn("age: 30", follow_up)
        self.assertIn("state: tx", follow_up)
        self.assertIn("Which state", replies[0])

    async def test_a_general_question_is_left_to_the_model(self):
        interface = await self._interface("eliginject6", "9999960006")
        replies = await self._turn(interface, "What is Medicaid?")

        self.assertEqual(_follow_ups(self.model), [])
        self.assertIn("make too much", replies[0])
        for message in _messages(self.model):
            self.assertNotIn("[Note from the system", message)

    async def test_the_next_turn_carries_the_interview_note(self):
        interface = await self._interface("eliginject7", "9999960007")
        await self._turn(interface, "Am I eligible for Medicaid?")
        await self._turn(interface, "California")

        latest = _messages(self.model)[-1]
        self.assertNotIn(CHECKER_MARKER, latest)
        self.assertIn(ELIGIBILITY_INTERVIEW_NOTE.strip(), latest)
        self.assertNotIn(ELIGIBILITY_REQUEST_NOTE.strip(), latest)

    async def test_a_repeated_request_does_not_restart_the_interview(self):
        interface = await self._interface("eliginject8", "9999960008")
        await self._turn(interface, "Am I eligible for Medicaid?")
        replies = await self._turn(interface, "So am I eligible for Medicaid or not?")

        # The checker ran on the first turn; the second request is answered
        # by the model (which the note told to call the tool) and not by a
        # fresh injected call that would throw the interview's answers away.
        self.assertEqual(len(_follow_ups(self.model)), 1)
        self.assertIn("make too much", replies[0])

    async def test_a_request_asked_in_an_earlier_session_is_not_reinjected(self):
        history = [
            {"role": "user", "content": "Am I eligible for Medicaid?"},
            {"role": "assistant", "content": "Which state are you in?"},
            {"role": "user", "content": "California"},
            {"role": "assistant", "content": "Thanks. How old are you?"},
        ]
        interface = await self._interface("eliginject9", "9999960009", history)
        replies = await self._turn(interface, "Sorry, do I qualify for Medicaid?")

        self.assertEqual(_follow_ups(self.model), [])
        self.assertIn("make too much", replies[0])

    async def test_the_models_reply_returns_when_the_checker_pass_fails(self):
        self.model = MemoryAnsweringModel(follow_up_reply=None)
        interface = await self._interface("eliginject10", "9999960010")
        replies = await self._turn(interface, "Am I eligible for Medicaid?")

        self.assertTrue(_follow_ups(self.model))
        self.assertEqual(len(replies), 1)
        self.assertIn("make too much", replies[0])
        self.assertNotIn("medicaid_eligibility", replies[0])

    async def test_an_action_flow_reply_is_never_replaced(self):
        user, chat = await make_professional_chat("eliginject11", "9999960011")
        interface = ChatInterface(send_json_message_func=FrameRecorder(), chat=chat, user=user)
        call = interface._eligibility_call_to_inject(
            "Saving that now.\n**create_or_update_appeal**{\"procedure\": \"MRI\"}",
            "Am I eligible for Medicaid?",
            chat,
        )
        self.assertIsNone(call)

    async def test_no_note_once_the_checker_has_a_verdict(self):
        user, chat = await make_professional_chat("eliginject12", "9999960012")
        interface = ChatInterface(send_json_message_func=FrameRecorder(), chat=chat, user=user)
        interface._eligibility_computed[0] = True
        self.assertEqual(
            interface._eligibility_note_for_turn("Am I eligible for Medicaid?", False),
            "",
        )
        self.assertIsNone(
            interface._eligibility_call_to_inject(
                MEMORY_ANSWER, "Am I eligible for Medicaid?", chat
            )
        )

    async def test_an_upload_gets_no_note(self):
        user, chat = await make_professional_chat("eliginject13", "9999960013")
        interface = ChatInterface(send_json_message_func=FrameRecorder(), chat=chat, user=user)
        self.assertEqual(
            interface._eligibility_note_for_turn("Am I eligible for Medicaid?", True),
            "",
        )

    async def test_the_checker_reply_is_what_the_chat_remembers(self):
        interface = await self._interface("eliginject14", "9999960014")
        await self._turn(interface, "Am I eligible for Medicaid?")

        fresh = await OngoingChat.objects.aget(id=interface.chat.id)
        assistant = [m for m in fresh.chat_history if m.get("role") == "assistant"]
        self.assertEqual(len(assistant), 1)
        self.assertIn("Which state", assistant[0]["content"])
        self.assertNotIn("medicaid_eligibility", assistant[0]["content"])
