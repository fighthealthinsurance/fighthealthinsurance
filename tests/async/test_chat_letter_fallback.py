"""A failed chat turn asking for a letter should still deliver a letter.

When every chat model fails on a "please draft the letter" turn (the
handle_chat_message total-failure branch), the appeal-generation pipeline is
tried before giving up: an existing ProposedAppeal reserve is served straight
from the DB, and otherwise a bounded make_appeals run drafts one. Also covers
the generate_appeal_letter chat tool, which routes letter drafting to the
same pipeline instead of having the chat model write the letter inline.
"""

from unittest.mock import AsyncMock, patch

from rest_framework.test import APITestCase

from fighthealthinsurance import common_view_logic
from fighthealthinsurance.chat.appeal_letter_generator import (
    DraftedLetter,
    draft_letter_for_chat,
    find_reserve_letter,
    generate_letter_for_denial,
)
from fighthealthinsurance.chat.tools import AppealTool, GenerateAppealLetterTool
from fighthealthinsurance.chat_interface import ChatInterface
from fighthealthinsurance.generate_appeal import GeneratedAppeal
from fighthealthinsurance.models import (
    Appeal,
    Denial,
    OngoingChat,
    ProposedAppeal,
)

# Failure plumbing shared with the failed-turn persistence tests: routes model
# selection to a mock backend and makes the chat LLM pass fail.
from tests.chat_fixtures import (
    FrameRecorder as _FrameRecorder,
    llm_call_fails as _llm_call_fails,
    make_professional_chat as _make_professional_chat,
)

# Long enough to pass is_real_appeal and the reserve length preference.
RESERVE_LETTER = (
    "Dear Insurance Company, I am writing to formally appeal the denial of "
    "the MRI ordered for my chronic back pain. The imaging is medically "
    "necessary to evaluate ongoing symptoms that have not improved with "
    "conservative treatment. Please reverse the denial. Sincerely, Patient"
)

GENERATED_LETTER = (
    "Dear Acme Health, I appeal the denial of claim 123. The requested "
    "procedure is medically necessary for the diagnosis documented in the "
    "attached records, and the denial should be overturned. Sincerely, Me"
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
        appeal, denial = await _link_letter_appeal(chat, user)
        await ProposedAppeal.objects.acreate(
            appeal_text=RESERVE_LETTER, for_denial=denial, speculative=True
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

    async def test_failed_fallback_error_links_to_appeal_page(self):
        """When even the letter fallback can't deliver, the error message
        points the user at the linked appeal's own generation page."""
        with patch(
            "fighthealthinsurance.chat_interface.draft_letter_for_chat",
            new=AsyncMock(return_value=None),
        ):
            chat, appeal, _, recorder = await self._run_failing_turn(
                "letterfall6", "9999920006", "Please go ahead and draft a letter."
            )
        error_frames = [f for f in recorder.frames if "error" in f]
        self.assertEqual(len(error_frames), 1)
        self.assertIn(f"/appeals/{appeal.id}", error_frames[0]["error"])


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


class LetterSelectionPolicyTest(APITestCase):
    """generate_letter_for_denial picks a letter, not just any model output."""

    def _denial(self):
        return Denial(
            denial_text="Denied as not medically necessary.",
            procedure="MRI",
            diagnosis="back pain",
        )

    async def test_long_model_letter_wins_immediately(self):
        long_letter = GeneratedAppeal(text="word " * 120, model_name="fhi-model")
        never_reached = GeneratedAppeal(text="word " * 400, model_name="other")
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
        static_letter = GeneratedAppeal(text="word " * 300, model_name=None)
        with patch.object(
            common_view_logic.appealGenerator,
            "make_appeals",
            return_value=iter([one_liner, static_letter]),
        ):
            item = await generate_letter_for_denial(self._denial())
        self.assertIsNotNone(item)
        self.assertIsNone(item.model_name)
        self.assertEqual(item.text, static_letter.text)

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


class FindReserveLetterTest(APITestCase):
    """Reserve lookup prefers live rows and skips runts."""

    async def test_prefers_live_over_speculative_and_skips_runts(self):
        denial = await Denial.objects.acreate(
            denial_text="denied", hashed_email=Denial.get_hashed_email("a@b.com")
        )
        await ProposedAppeal.objects.acreate(
            appeal_text=RESERVE_LETTER + " speculative extra text here",
            for_denial=denial,
            speculative=True,
        )
        await ProposedAppeal.objects.acreate(
            appeal_text=RESERVE_LETTER, for_denial=denial, speculative=False
        )
        await ProposedAppeal.objects.acreate(
            appeal_text="runt", for_denial=denial, speculative=False
        )
        found = await find_reserve_letter(denial)
        # The live row wins even though the speculative one is longer.
        self.assertEqual(found, RESERVE_LETTER)

    async def test_junk_rows_cannot_evict_reserve_from_the_window(self):
        """A pile of runt live drafts (a degraded-model period) must not fill
        the bounded lookup window and hide the one deliverable reserve --
        the runt filter runs in SQL before the slice."""
        denial = await Denial.objects.acreate(
            denial_text="denied", hashed_email=Denial.get_hashed_email("e@f.com")
        )
        for _ in range(12):
            await ProposedAppeal.objects.acreate(
                appeal_text="junk", for_denial=denial, speculative=False
            )
        await ProposedAppeal.objects.acreate(
            appeal_text=RESERVE_LETTER, for_denial=denial, speculative=True
        )
        self.assertEqual(await find_reserve_letter(denial), RESERVE_LETTER)

    async def test_returns_none_when_no_deliverable_rows(self):
        denial = await Denial.objects.acreate(
            denial_text="denied", hashed_email=Denial.get_hashed_email("c@d.com")
        )
        await ProposedAppeal.objects.acreate(
            appeal_text="runt", for_denial=denial, speculative=False
        )
        self.assertIsNone(await find_reserve_letter(denial))


class DraftLetterForChatTest(APITestCase):
    """draft_letter_for_chat persistence behavior."""

    async def test_generated_letter_persisted_with_provenance(self):
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
        row = await ProposedAppeal.objects.aget(for_denial=denial)
        self.assertEqual(row.model_name, "fhi-model")
        self.assertEqual(row.context_level, "full")
        self.assertFalse(row.speculative)

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

    async def test_failed_appeal_save_reported_not_hidden(self):
        """A letter whose appeal save failed is still delivered, but flagged
        so callers don't claim it was saved to the appeal."""
        user, chat = await _make_professional_chat("draftpersist3", "9999940003")
        appeal, denial = await _link_letter_appeal(chat, user)
        await ProposedAppeal.objects.acreate(
            appeal_text=RESERVE_LETTER, for_denial=denial
        )
        with patch.object(
            Appeal, "asave", new=AsyncMock(side_effect=RuntimeError("db down"))
        ):
            drafted = await draft_letter_for_chat(
                appeal=appeal, denial=denial, use_external=False, prefer_existing=True
            )
        self.assertEqual(drafted, DraftedLetter(RESERVE_LETTER, False))


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
