"""assistant_drafts.py: the chat path's bookkeeping, letters and sweep."""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from fighthealthinsurance import assistant_drafts as drafts
from fighthealthinsurance.denial_context import load_qa
from fighthealthinsurance.helpers.data_helpers import RemoveDataHelper
from fighthealthinsurance.ml import spend
from fighthealthinsurance.models import AssistantDraft, Denial, ProposedAppeal, SpendCounter

ALL_ON = dict(
    MCP_DRAFT_IN_CHAT_ENABLED=True,
    MCP_SERVER_ENABLED=True,
    MCP_PREPARE_APPEAL_ENABLED=True,
    TEMPORAL_ENABLED=True,
    TEMPORAL_APPEAL_JOURNEY_ENABLED=True,
)

LETTER = (
    "Dear {insurance_company}, I am writing to appeal the denial of my MRI. "
    "The scan is medically necessary for my [Diagnosis] and the treatment "
    "plan my doctor set out. Please reverse the decision. Sincerely, "
    "[Your Name]"
)


def a_denial(**fields):
    base = dict(
        hashed_email=Denial.get_hashed_email("person@example.com"),
        denial_text="The MRI was denied as not medically necessary.",
        insurance_company="Example Health",
        channel="assistant",
    )
    base.update(fields)
    return Denial.objects.create(**base)


class FlagTest(TestCase):
    def test_on_only_with_every_flag(self):
        with override_settings(**ALL_ON):
            self.assertTrue(drafts.draft_in_chat_enabled())
        for name in ALL_ON:
            with override_settings(**{**ALL_ON, name: False}):
                self.assertFalse(drafts.draft_in_chat_enabled(), name)


class DraftRowTest(TestCase):
    def test_the_id_finds_the_row_and_is_never_stored(self):
        new = drafts.create_draft(a_denial(), procedure="MRI", condition="back pain")
        self.assertTrue(drafts.is_draft_id(new.draft_id))
        self.assertNotIn(new.draft_id, new.draft.draft_id_digest)
        self.assertEqual(drafts.find_draft(new.draft_id).pk, new.draft.pk)
        self.assertIsNone(drafts.find_draft(new.draft.draft_id_digest))
        self.assertIsNone(drafts.find_draft("not-an-id"))
        self.assertEqual((new.draft.procedure, new.draft.condition), ("MRI", "back pain"))

    def test_an_unagreed_draft_lives_two_hours_and_an_agreed_one_a_day(self):
        new = drafts.create_draft(a_denial())
        self.assertAlmostEqual(
            new.draft.expires_at, timezone.now() + timedelta(hours=2), delta=timedelta(minutes=1)
        )
        drafts.mark_agreed(new.draft)
        new.draft.refresh_from_db()
        self.assertEqual(new.draft.status, drafts.READING)
        self.assertAlmostEqual(
            new.draft.expires_at, timezone.now() + timedelta(hours=24), delta=timedelta(minutes=1)
        )

    def test_an_expired_draft_is_not_found_and_is_swept(self):
        new = drafts.create_draft(a_denial())
        AssistantDraft.objects.filter(pk=new.draft.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        self.assertIsNone(drafts.find_draft(new.draft_id))
        self.assertEqual(drafts.sweep_expired(), 1)
        self.assertFalse(AssistantDraft.objects.filter(pk=new.draft.pk).exists())

    def test_the_draft_goes_with_its_denial(self):
        denial = a_denial()
        drafts.create_draft(denial)
        RemoveDataHelper.remove_data_for_email("person@example.com")
        self.assertFalse(AssistantDraft.objects.filter(denial_id=denial.denial_id).exists())


class QuestionsTest(TestCase):
    def test_questions_are_short_url_free_typed_and_each_once(self):
        rows = [
            ("Is the MRI for an injury?", "yes"),
            ["What did your doctor say about the scan?", ""],
            ("See https://example.com for details?", ""),
            ("denial date", ""),
            ("Is the MRI for an injury?", "no"),
            ("x" * 400, ""),
            "",
            None,
        ]
        cleaned = drafts.clean_questions(rows)
        self.assertEqual([q["kind"] for q in cleaned], ["yes_no", "text", "text"])
        self.assertEqual(len(cleaned[2]["label"]), 300)
        self.assertEqual(len({q["name"] for q in cleaned}), 3)
        for q in cleaned:
            self.assertNotIn("http", q["label"])
            self.assertNotIn("One way to answer", q["label"])

    def test_answers_are_filed_under_the_question_and_only_for_asked_ones(self):
        denial = a_denial()
        new = drafts.create_draft(denial)
        new.draft.questions = drafts.clean_questions(
            [("Is the MRI for an injury?", ""), ("What did your doctor say?", "")]
        )
        new.draft.save()
        yes_no, text = new.draft.questions
        filed = drafts.file_answers(
            new.draft,
            [
                {"name": yes_no["name"], "value": "Yes"},
                {"name": text["name"], "value": "  skip "},
            ],
        )
        self.assertEqual(filed, 1)
        denial.refresh_from_db()
        self.assertEqual(load_qa(denial), {"Is the MRI for an injury?": "Yes"})
        with self.assertRaises(drafts.UnknownQuestion) as caught:
            drafts.file_answers(new.draft, [{"name": "q_made_up", "value": "x"}])
        self.assertIn("q_made_up", str(caught.exception))
        with self.assertRaises(ValueError):
            drafts.file_answers(new.draft, [{"name": yes_no["name"], "value": "maybe"}])
        with self.assertRaises(ValueError):
            drafts.file_answers(new.draft, [{"name": text["name"], "value": 3}])
        denial.refresh_from_db()
        self.assertEqual(load_qa(denial), {"Is the MRI for an injury?": "Yes"})

    def test_a_long_answer_is_cut_and_a_reserved_key_is_never_asked(self):
        denial = a_denial()
        new = drafts.create_draft(denial)
        new.draft.questions = drafts.clean_questions([("What happened?", ""), ("in_network", "")])
        new.draft.save()
        self.assertEqual(len(new.draft.questions), 1)
        drafts.file_answers(new.draft, [{"name": new.draft.questions[0]["name"], "value": "a" * 2000}])
        denial.refresh_from_db()
        self.assertEqual(len(load_qa(denial)["What happened?"]), 1000)


class LettersTest(TestCase):
    def _row(self, denial, text=LETTER, **fields):
        return ProposedAppeal.objects.create(for_denial=denial, appeal_text=text, **fields)

    def test_three_distinct_real_letters_newest_first_with_values_filled_in(self):
        denial = a_denial()
        for i in range(5):
            self._row(denial, LETTER + f" Letter {i}.")
        self._row(denial, "Too short.")
        self._row(denial, LETTER + " chosen", chosen=True)
        self._row(denial, LETTER + " reserve", speculative=True)
        letters = drafts.collect_letters(denial)
        self.assertEqual(len(letters), 3)
        self.assertTrue(letters[0]["text"].endswith("Letter 4."))
        self.assertIn("Example Health", letters[0]["text"])
        self.assertNotIn("{insurance_company}", letters[0]["text"])
        self.assertIn("[Your Name]", letters[0]["placeholders"])
        self.assertEqual(len({l["text"] for l in letters}), 3)

    def test_a_long_letter_is_cut_and_says_so(self):
        denial = a_denial()
        self._row(denial, LETTER + " more. " * 2000)
        [letter] = drafts.collect_letters(denial)
        self.assertEqual(len(letter["text"]), drafts.LETTER_MAX_CHARS)
        self.assertTrue(letter["cut_short"])

    def test_status_from_what_landed(self):
        denial = a_denial()
        self.assertEqual(drafts.letters_status(denial, finished=False), drafts.DRAFTING)
        self.assertEqual(drafts.letters_status(denial, finished=True), drafts.STOPPED)
        self._row(denial)
        self.assertEqual(drafts.letters_status(denial, finished=False), drafts.DRAFTING)
        self.assertEqual(drafts.letters_status(denial, finished=True), drafts.READY)
        for i in range(2):
            self._row(denial, LETTER + f" {i}")
        self.assertEqual(drafts.letters_status(denial, finished=False), drafts.READY)


@override_settings(FHI_SPEND_BACKGROUND=False)
class WorkerSpendTest(TestCase):
    def test_a_deepinfra_fallback_for_an_assistant_denial_lands_in_its_own_counter(self):
        """What the appeal worker does for an assistant denial: mark the
        channel from the Denial, then a DeepInfra answer is counted under
        deepinfra:assistant, not deepinfra:other."""
        from fighthealthinsurance.ml import ml_models

        denial = a_denial()
        backend = ml_models.DeepInfra.__new__(ml_models.DeepInfra)
        usage = {"prompt_tokens": 1000, "completion_tokens": 200}
        with patch.object(spend, "deepinfra_cost_micro", return_value=1234), spend.channel_scope():
            spend.set_channel_of(denial)
            self.assertEqual(spend.current_use(), spend.ASSISTANT)
            backend._record_spend("meta-llama/some-model", {"usage": usage})
        self.assertEqual(spend.current_use(), spend.OTHER)
        # What the ledger's worker thread does between requests.
        spend._ledger._write_pending()
        row = SpendCounter.objects.get(name="deepinfra:assistant")
        self.assertEqual(row.amount, 1234)
        self.assertFalse(SpendCounter.objects.filter(name="deepinfra:other").exists())
