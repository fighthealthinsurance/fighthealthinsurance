"""
Unit tests for chat message preprocessing and variant scoring.

Covers:
- The normal/primary path stays lossless and is the only variant for short,
  clean messages.
- Long pastes produce compact, bounded alternatives (never the full text) and
  signal that the original should be stored for reference.
- Weird Unicode (combining marks, emoji ZWJ, bidi controls, zero-width,
  replacement chars, lone surrogates) produces lower-scored alternatives while
  preserving the original primary, and never crashes.
- Non-Latin / clinical text is preserved verbatim (never ASCII-fied).
- Scoring: a valid primary outranks alternatives; an alternative wins when the
  primary is empty/invalid.

Invisible / bidi characters are constructed via ``chr()`` so this source file
contains no hidden (or "Trojan Source") characters.
"""

from django.test import SimpleTestCase

from fighthealthinsurance.chat.llm_client import (
    build_llm_calls_for_variants,
    score_llm_response,
)
from fighthealthinsurance.chat.message_preprocessor import (
    DIRECT_CHAT_HARD_LIMIT_CHARS,
    DIRECT_CHAT_SOFT_LIMIT_CHARS,
    LONG_DOC_REFERENCE_DELTA,
    MessageVariant,
    has_suspicious_unicode,
    prepare_user_message_variants,
    safe_display_truncate,
    strip_control_chars,
)

# Named code points used in tests.
COMBINING_ACUTE = chr(0x0301)
ZWJ = chr(0x200D)
ZERO_WIDTH_SPACE = chr(0x200B)
RLO = chr(0x202E)  # RIGHT-TO-LEFT OVERRIDE
REPLACEMENT = chr(0xFFFD)
LONE_SURROGATE = chr(0xD800)

# A clean, valid response/context that trips none of the quality penalties.
_VALID_RESPONSE = (
    "You can request an internal appeal within 180 days and include "
    "supporting medical records."
)
_VALID_CONTEXT = "Conversation summary: discussed appeal timelines."


def _kinds(variants):
    return [v.kind for v in variants]


class _StubBackend:
    """Minimal RemoteModelLike stand-in that records the message it received."""

    def __init__(self, quality: int = 10):
        self._quality = quality
        self.received: list[str] = []

    def quality(self) -> int:
        return self._quality

    def get_max_context(self) -> int:
        return 100000

    def generate_chat_response(self, message, **kwargs):
        self.received.append(message)

        async def _coro():
            return ("ok", "ctx")

        return _coro()


class PreparePrimaryPathTest(SimpleTestCase):
    def test_short_normal_message_only_primary(self):
        msg = "Can you help me appeal my MRI denial?"
        variants = prepare_user_message_variants(msg, is_document=False)
        self.assertEqual(len(variants), 1)
        self.assertEqual(variants[0].kind, "primary_original")
        self.assertEqual(variants[0].score_delta, 0)
        self.assertIsNone(variants[0].display_text)
        # Primary text is byte-identical to the original (lossless).
        self.assertEqual(variants[0].text_for_llm, msg)

    def test_empty_message_returns_single_primary(self):
        variants = prepare_user_message_variants("", is_document=False)
        self.assertEqual(len(variants), 1)
        self.assertEqual(variants[0].kind, "primary_original")

    def test_document_upload_marker_not_re_routed(self):
        # An explicit upload arrives already-replaced with a short marker; even a
        # long is_document body must not trigger long-paste re-routing here.
        big = "denied because experimental. " * 4000
        variants = prepare_user_message_variants(
            big, is_document=True, document_name="upload.pdf"
        )
        self.assertEqual(_kinds(variants), ["primary_original"])


class PrepareLongMessageTest(SimpleTestCase):
    def setUp(self):
        # ~57k chars, well above the hard limit.
        self.big = (
            "This claim was denied because the procedure is considered "
            "experimental. "
        ) * 800
        self.assertGreater(len(self.big), DIRECT_CHAT_HARD_LIMIT_CHARS)

    def test_long_message_produces_compact_bounded_variants(self):
        variants = prepare_user_message_variants(self.big, is_document=False)
        # No primary_original: raw is too large to send/store verbatim.
        self.assertNotIn("primary_original", _kinds(variants))
        self.assertIn("long_message_document_reference", _kinds(variants))
        # Every variant the LLM would see is bounded.
        for v in variants:
            self.assertLessEqual(len(v.text_for_llm), DIRECT_CHAT_HARD_LIMIT_CHARS)

    def test_long_message_full_text_never_in_any_variant(self):
        variants = prepare_user_message_variants(self.big, is_document=False)
        for v in variants:
            self.assertNotIn(self.big, v.text_for_llm)

    def test_long_message_reference_signals_storage_and_compact_marker(self):
        variants = prepare_user_message_variants(self.big, is_document=False)
        ref = next(v for v in variants if v.kind == "long_message_document_reference")
        self.assertTrue(ref.metadata.get("store_full_text"))
        self.assertEqual(ref.metadata.get("char_count"), len(self.big))
        self.assertTrue(
            ref.metadata.get("document_name", "").startswith("pasted_message_")
        )
        # Display marker is short and references the stored doc.
        self.assertIsNotNone(ref.display_text)
        self.assertLess(len(ref.display_text), 300)

    def test_long_message_provided_document_name_is_used(self):
        variants = prepare_user_message_variants(
            self.big, is_document=False, document_name="denial_letter.txt"
        )
        ref = next(v for v in variants if v.kind == "long_message_document_reference")
        self.assertEqual(ref.metadata.get("document_name"), "denial_letter.txt")

    def test_just_under_soft_limit_stays_primary(self):
        msg = "a" * (DIRECT_CHAT_SOFT_LIMIT_CHARS - 1)
        variants = prepare_user_message_variants(msg, is_document=False)
        self.assertEqual(_kinds(variants), ["primary_original"])

    def test_long_unbroken_string_does_not_crash(self):
        variants = prepare_user_message_variants("A" * 50000, is_document=False)
        self.assertTrue(variants)
        for v in variants:
            self.assertLessEqual(len(v.text_for_llm), DIRECT_CHAT_HARD_LIMIT_CHARS)


class PrepareUnicodeTest(SimpleTestCase):
    def test_excessive_combining_marks_offer_alternatives_keep_primary(self):
        zalgo = "e" + (COMBINING_ACUTE * 50)
        variants = prepare_user_message_variants(zalgo, is_document=False)
        self.assertEqual(variants[0].text_for_llm, zalgo)  # preserved
        self.assertEqual(
            _kinds(variants), ["primary_original", "unicode_control_stripped"]
        )

    def test_few_combining_marks_is_not_flagged(self):
        # A normal accented word should not be treated as suspicious.
        self.assertFalse(has_suspicious_unicode("café résumé naïve"))
        variants = prepare_user_message_variants("café résumé", is_document=False)
        self.assertEqual(_kinds(variants), ["primary_original"])

    def test_emoji_zwj_sequence_preserves_primary(self):
        family = (
            "\U0001f468" + ZWJ + "\U0001f469" + ZWJ + "\U0001f467"
            " \U0001f44d\U0001f3fe"
        )
        variants = prepare_user_message_variants(family, is_document=False)
        self.assertEqual(variants[0].kind, "primary_original")
        self.assertEqual(variants[0].text_for_llm, family)
        self.assertIn("unicode_control_stripped", _kinds(variants))

    def test_bidi_control_stripped_in_alternative_only(self):
        rlo = "balance " + RLO + "0001$"
        variants = prepare_user_message_variants(rlo, is_document=False)
        self.assertEqual(variants[0].text_for_llm, rlo)  # primary preserved
        stripped = next(v for v in variants if v.kind == "unicode_control_stripped")
        self.assertNotIn(RLO, stripped.text_for_llm)

    def test_zero_width_and_replacement_chars_flagged(self):
        weird = "foo" + ZERO_WIDTH_SPACE + "bar" + REPLACEMENT + " baz"
        self.assertTrue(has_suspicious_unicode(weird))
        variants = prepare_user_message_variants(weird, is_document=False)
        self.assertEqual(variants[0].kind, "primary_original")
        self.assertGreater(len(variants), 1)

    def test_non_latin_clinical_text_preserved_verbatim(self):
        for text in (
            "هل يمكنك مساعدتي في استئناف رفض التأمين الطبي؟",
            "请帮我对这次保险拒赔提出申诉",
            "Patient prescribed Lévothyroxine 50µg",
        ):
            variants = prepare_user_message_variants(text, is_document=False)
            # Primary must be present and byte-identical (never ASCII-fied).
            self.assertEqual(variants[0].kind, "primary_original")
            self.assertEqual(variants[0].text_for_llm, text)

    def test_lone_surrogate_is_made_encodable(self):
        text = "patient name " + LONE_SURROGATE + " here"
        variants = prepare_user_message_variants(text, is_document=False)
        self.assertTrue(variants)
        for v in variants:
            # Must not raise UnicodeEncodeError (JSON/WebSocket/DB safety).
            v.text_for_llm.encode("utf-8")
            if v.display_text is not None:
                v.display_text.encode("utf-8")


class UnicodeHelperTest(SimpleTestCase):
    def test_safe_display_truncate_respects_limit_and_appends_ellipsis(self):
        out = safe_display_truncate("x" * 100, 10)
        self.assertLessEqual(len(out), 10)
        self.assertTrue(out.endswith("…"))

    def test_safe_display_truncate_short_text_unchanged(self):
        self.assertEqual(safe_display_truncate("short", 100), "short")

    def test_strip_control_chars_keeps_tabs_and_newlines(self):
        self.assertEqual(strip_control_chars("a\tb\nc"), "a\tb\nc")
        self.assertEqual(strip_control_chars("a" + ZERO_WIDTH_SPACE + "b"), "ab")


class VariantScoringTest(SimpleTestCase):
    """Primary stays highest when valid; alternatives win when primary fails."""

    def test_primary_outranks_alternative_when_both_valid(self):
        base = 20
        primary = score_llm_response(
            (_VALID_RESPONSE, _VALID_CONTEXT),
            base,
            is_primary_call=True,
            chat_history=[],
            current_message="how long do I have to appeal",
        )
        alternative = score_llm_response(
            (_VALID_RESPONSE, _VALID_CONTEXT),
            base + LONG_DOC_REFERENCE_DELTA,
            is_primary_call=False,
            chat_history=[],
            current_message="how long do I have to appeal",
        )
        self.assertGreater(primary, alternative)

    def test_alternative_wins_when_primary_empty(self):
        base = 20
        primary_none = score_llm_response(
            (None, None),
            base,
            is_primary_call=True,
            chat_history=[],
            current_message="x",
        )
        primary_blank = score_llm_response(
            ("", ""),
            base,
            is_primary_call=True,
            chat_history=[],
            current_message="x",
        )
        alternative = score_llm_response(
            (_VALID_RESPONSE, _VALID_CONTEXT),
            base + LONG_DOC_REFERENCE_DELTA,
            is_primary_call=False,
            chat_history=[],
            current_message="x",
        )
        self.assertGreater(alternative, primary_none)
        self.assertGreater(alternative, primary_blank)


class BuildCallsForVariantsTest(SimpleTestCase):
    def test_deltas_applied_and_only_primary_in_primary_calls(self):
        backend = _StubBackend(quality=10)
        variants = [
            MessageVariant("primary_original", "hello", 0),
            MessageVariant("unicode_control_stripped", "hello-stripped", -30),
        ]
        calls, scores, primary_calls = build_llm_calls_for_variants(
            model_backends=[backend],
            variants=variants,
            previous_context_summary=None,
            history=[],
            is_professional=True,
            is_logged_in=True,
            full_history=None,
        )
        try:
            self.assertEqual(len(calls), 2)
            base = (backend.quality() ** 2) // 5  # 20
            self.assertCountEqual([scores[c] for c in calls], [base + 0, base - 30])
            # Only the primary_original variant's call is "primary".
            self.assertEqual(len(primary_calls), 1)
            self.assertEqual(scores[primary_calls[0]], base)
            # Primary text fanned out first, then the alternative.
            self.assertEqual(backend.received, ["hello", "hello-stripped"])
        finally:
            for c in calls:
                c.close()
