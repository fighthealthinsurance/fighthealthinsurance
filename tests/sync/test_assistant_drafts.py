"""assistant_drafts.py: the chat path's bookkeeping, letters and sweep."""

import asyncio
import json
import os
import random
import re
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiohttp
import yaml
from asgiref.sync import async_to_sync
from django.core.management import call_command
from django.test import (
    SimpleTestCase,
    TestCase,
    TransactionTestCase,
    override_settings,
)
from django.utils import timezone

from fighthealthinsurance import appeal_journey_core
from fighthealthinsurance import assistant_drafts as drafts
from fighthealthinsurance import generation_lease
from fighthealthinsurance.common_view_logic import AppealsBackendHelper
from fighthealthinsurance.denial_context import load_qa, merge_qa
from fighthealthinsurance.generate_appeal import GeneratedAppeal
from fighthealthinsurance.helpers.data_helpers import RemoveDataHelper
from fighthealthinsurance.letter_placeholders import (
    PATTERNS_FILE,
    find_placeholders_as_written,
)
from fighthealthinsurance.ml import ml_models, spend
from fighthealthinsurance.models import (
    AssistantDraft,
    Denial,
    ProposedAppeal,
    SpendCounter,
)

ALL_ON = dict(
    MCP_DRAFT_IN_CHAT_ENABLED=True,
    MCP_SERVER_ENABLED=True,
    MCP_PREPARE_APPEAL_ENABLED=True,
    TEMPORAL_ENABLED=True,
    TEMPORAL_APPEAL_JOURNEY_ENABLED=True,
    TEMPORAL_PAYLOAD_KEY="test-key",
)

LETTER = (
    "Dear {insurance_company}, I am writing to appeal the denial of my MRI. "
    "The scan is medically necessary for my [Diagnosis] and the treatment "
    "plan my doctor set out. Please reverse the decision. Sincerely, "
    "[Your Name]"
)


# What main listed before placeholders_in: every bracket that starts with a
# capital, citations and all, with {{...}}, {...} and $name.
MAIN_PLACEHOLDER = re.compile(
    r"\{\{?[A-Za-z_][A-Za-z0-9_ ]*\}\}?|\[[A-Z][^\]\n]{2,40}\]|\$[a-z][a-z_]{2,}\b"
)
# Fill-ins main listed, each one the fax form refuses to send.
FILL_INS_MAIN_LISTED = (
    # Refused as a whole.
    "[Your Name]",
    "[Patient Name]",
    "[Patient's Name]",
    "[Patient’s Name]",
    "[Member ID]",
    "[Date of Service]",
    "[Insurance Company Name]",
    "[Provider's NPI Number]",
    "[Phone Number]",
    "[Signature]",
    "[Today's Date]",
    "[CPT Code]",
    "[INSERT DATE]",
    "[CLAIM_NUMBER]",
    "[Address Line 1]",
    "[City, State ZIP]",
    "[Date: MM/DD/YYYY]",
    "[Date of Birth: MM/DD/YYYY]",
    "[Phone: (555) 555-5555]",
    "[Insert date of denial, e.g. 01/02/2026]",
    "[Your Name, Ph.D.]",
    # Refused for a blank inside it.
    "[DOB: MM/DD/YYYY]",
    "[Member ID: XXXXXX]",
    "[Policy Number: XXXXXXXX]",
    "[Group Number: XXXXXX]",
    "[Claim Number: XXX-XX-XXXX]",
    "[Claim #: ________]",
    "[NPI: XXXXXXXXXX]",
    "[ICD-10: XXX.X]",
    "[Amount Billed: $X,XXX.XX]",
    "[Your {{FIRST_NAME}}]",
    "[Patient: {{FIRST_NAME}} {{LAST_NAME}}]",
    "[Subscriber ID: {{SCSID}}]",
)
# Brackets main listed that the fax form lets through: citations and the
# notes that go with a quotation.
CITATIONS_MAIN_LISTED = (
    "[CMS NCD 220.2]",
    "[Smith et al. 2020]",
    "[Id.]",
    "[PMID: 12345678]",
    "[Exhibit 1]",
    "[Section 2.1]",
    "[Emphasis added]",
    "[Internal citations omitted]",
    "[Sic]",
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


def asking(denial, rows):
    """A draft waiting for answers to these generated_questions rows."""
    denial.generated_questions = rows
    denial.save(update_fields=["generated_questions"])
    new = drafts.create_draft(denial)
    new.draft.questions = drafts.clean_questions(rows)
    new.draft.status = drafts.QUESTIONS
    new.draft.save()
    return new.draft


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
        self.assertEqual(len(new.draft.draft_id_digest), 64)
        self.assertEqual(drafts.find_draft(new.draft_id).pk, new.draft.pk)
        self.assertIsNone(drafts.find_draft(new.draft.draft_id_digest))
        self.assertIsNone(drafts.find_draft("not-an-id"))
        self.assertEqual((new.draft.procedure, new.draft.condition), ("MRI", "back pain"))

    def test_the_assistants_procedure_and_condition_are_bounded(self):
        new = drafts.create_draft(a_denial(), procedure="p" * 500, condition="c" * 500)
        self.assertEqual(len(new.draft.procedure), drafts.FIELD_MAX_CHARS)
        self.assertEqual(len(new.draft.condition), drafts.FIELD_MAX_CHARS)

    def test_an_unagreed_draft_lives_two_hours_and_an_agreed_one_a_day(self):
        new = drafts.create_draft(a_denial())
        self.assertEqual(new.draft.status, drafts.WAITING)
        self.assertAlmostEqual(
            new.draft.expires_at, timezone.now() + timedelta(hours=2), delta=timedelta(minutes=1)
        )
        drafts.mark_agreed(new.draft)
        new.draft.refresh_from_db()
        self.assertEqual(new.draft.status, drafts.READING)
        self.assertAlmostEqual(
            new.draft.expires_at, timezone.now() + timedelta(hours=24), delta=timedelta(minutes=1)
        )

    def test_an_unknown_status_is_refused(self):
        new = drafts.create_draft(a_denial())
        with self.assertRaises(ValueError):
            drafts.set_status(new.draft, "made-up")

    def test_the_draft_goes_with_its_denial(self):
        denial = a_denial()
        drafts.create_draft(denial)
        RemoveDataHelper.remove_data_for_email("person@example.com")
        self.assertFalse(AssistantDraft.objects.filter(denial_id=denial.denial_id).exists())


class SweepTest(TestCase):
    def _expired(self):
        new = drafts.create_draft(a_denial())
        AssistantDraft.objects.filter(pk=new.draft.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        return new

    def test_an_expired_draft_is_not_found_and_is_swept(self):
        new = self._expired()
        live = drafts.create_draft(a_denial())
        self.assertIsNone(drafts.find_draft(new.draft_id))
        self.assertEqual(drafts.sweep_expired(), 1)
        self.assertFalse(AssistantDraft.objects.filter(pk=new.draft.pk).exists())
        self.assertTrue(AssistantDraft.objects.filter(pk=live.draft.pk).exists())

    def test_the_command_sweeps_and_counts_under_a_fixed_stage(self):
        self._expired()
        before = drafts.DRAFTS.labels("swept")._value.get()
        call_command("sweep_assistant_drafts")
        self.assertFalse(AssistantDraft.objects.exists())
        self.assertEqual(drafts.DRAFTS.labels("swept")._value.get(), before + 1)


class QuestionsTest(TestCase):
    def test_questions_are_typed_url_free_reserved_free_and_each_once(self):
        rows = [
            ("Is the MRI for an injury?", "yes"),
            ["What did your doctor say about the scan?", "One way to answer"],
            ("Was it inpatient or outpatient?", ""),
            ("Which setting was it (inpatient/outpatient/home)?", ""),
            ("See https://example.com for details?", ""),
            ("denial date", ""),
            ("Is the MRI for an injury?", "no"),
            ("x" * 400, ""),
            "",
            None,
        ]
        cleaned = drafts.clean_questions(rows)
        self.assertEqual(
            [q["kind"] for q in cleaned], ["yes_no", "text", "text", "choice", "text"]
        )
        self.assertEqual(cleaned[3]["choices"], ["inpatient", "outpatient", "home"])
        self.assertEqual(len(cleaned[4]["label"]), drafts.QUESTION_MAX_CHARS)
        self.assertEqual(len({q["name"] for q in cleaned}), 5)
        for q in cleaned:
            self.assertEqual(set(q), {"name", "kind", "label", "choices"})
            self.assertNotIn("http", q["label"])
            self.assertNotIn("One way to answer", str(q))

    def test_answers_are_filed_under_the_question_the_way_the_site_files_them(self):
        denial = a_denial()
        draft = asking(
            denial,
            [
                ("Is the MRI for an injury?", ""),
                ("What did your doctor say?", ""),
                ("Which setting was it (inpatient/outpatient)?", ""),
            ],
        )
        yes_no, text, choice = draft.questions
        filed = drafts.file_answers(
            draft,
            [
                {"name": yes_no["name"], "value": "yes"},
                {"name": text["name"], "value": "  skip "},
                {"name": choice["name"], "value": "OUTPATIENT"},
            ],
        )
        self.assertEqual(filed, 2)
        denial.refresh_from_db()
        self.assertEqual(
            load_qa(denial),
            {
                "Is the MRI for an injury?": "Yes",
                "Which setting was it (inpatient/outpatient)?": "outpatient",
            },
        )
        draft.refresh_from_db()
        self.assertIsNotNone(draft.answers_at)

    def test_an_answer_to_a_question_never_asked_is_refused_by_name(self):
        denial = a_denial()
        draft = asking(denial, [("Is the MRI for an injury?", "")])
        name = draft.questions[0]["name"]
        with self.assertRaises(drafts.UnknownQuestion) as caught:
            drafts.file_answers(
                draft,
                [{"name": name, "value": "yes"}, {"name": "q_made_up", "value": "x"}],
            )
        self.assertIn("q_made_up", str(caught.exception))
        denial.refresh_from_db()
        self.assertEqual(load_qa(denial), {})

    def test_a_reserved_key_can_never_be_answered(self):
        denial = a_denial()
        draft = asking(denial, [("What happened?", "")])
        reserved = drafts.question_field_name("in_network")
        draft.questions = draft.questions + [
            {"name": reserved, "kind": "text", "label": "in_network", "choices": []}
        ]
        draft.save()
        denial.generated_questions = [("What happened?", ""), ("in_network", "")]
        denial.save(update_fields=["generated_questions"])
        with self.assertRaises(drafts.UnknownQuestion):
            drafts.file_answers(draft, [{"name": reserved, "value": "true"}])
        denial.refresh_from_db()
        self.assertNotIn("in_network", load_qa(denial))

    def test_answers_that_do_not_fit_the_question_are_refused(self):
        draft = asking(
            a_denial(),
            [("Is the MRI for an injury?", ""), ("Which one (left/right)?", "")],
        )
        yes_no, choice = draft.questions
        for answer in (
            {"name": yes_no["name"], "value": "maybe"},
            {"name": choice["name"], "value": "middle"},
            {"name": yes_no["name"], "value": 3},
        ):
            with self.assertRaises(ValueError):
                drafts.file_answers(draft, [answer])

    def test_answers_are_taken_only_while_the_draft_waits_for_them(self):
        draft = asking(a_denial(), [("What happened?", "")])
        drafts.set_status(draft, drafts.DRAFTING)
        with self.assertRaises(ValueError):
            drafts.file_answers(draft, [{"name": draft.questions[0]["name"], "value": "x"}])

    def test_a_stale_copy_cannot_file_after_drafting_starts(self):
        draft = asking(a_denial(), [("What happened?", "")])
        AssistantDraft.objects.filter(pk=draft.pk).update(status=drafts.DRAFTING)
        with self.assertRaises(ValueError):
            drafts.file_answers(draft, [{"name": draft.questions[0]["name"], "value": "x"}])

    def test_an_expired_draft_takes_no_answers(self):
        draft = asking(a_denial(), [("What happened?", "")])
        AssistantDraft.objects.filter(pk=draft.pk).update(expires_at=timezone.now())
        with self.assertRaises(ValueError):
            drafts.file_answers(draft, [{"name": draft.questions[0]["name"], "value": "x"}])

    def test_answers_merge_with_answers_filed_since_the_copy_was_loaded(self):
        denial = a_denial()
        draft = asking(denial, [("What happened?", ""), ("When?", "")])
        fresh = Denial.objects.get(pk=denial.pk)
        merge_qa(fresh, {"When?": "May"}, source="test")
        fresh.save(update_fields=["qa_context"])
        drafts.file_answers(draft, [{"name": draft.questions[0]["name"], "value": "x"}])
        qa = load_qa(Denial.objects.get(pk=denial.pk))
        self.assertEqual((qa["What happened?"], qa["When?"]), ("x", "May"))

    def test_a_long_answer_is_cut(self):
        denial = a_denial()
        draft = asking(denial, [("What happened?", "")])
        drafts.file_answers(draft, [{"name": draft.questions[0]["name"], "value": "a" * 2000}])
        denial.refresh_from_db()
        self.assertEqual(len(load_qa(denial)["What happened?"]), drafts.ANSWER_MAX_CHARS)


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

    def test_the_same_text_twice_is_one_letter(self):
        denial = a_denial()
        self._row(denial, LETTER)
        # A legacy twin: the backfill left duplicates with no fingerprint.
        twin = self._row(denial, LETTER + " twin")
        ProposedAppeal.objects.filter(pk=twin.pk).update(
            appeal_text=LETTER.upper(), text_fingerprint=None
        )
        self.assertEqual(len(drafts.collect_letters(denial)), 1)

    def test_dollar_placeholders_are_listed(self):
        denial = a_denial()
        self._row(denial, LETTER + " Signed, $your_name_here. It cost $500.")
        [letter] = drafts.collect_letters(denial)
        self.assertIn("$your_name_here", letter["placeholders"])
        self.assertNotIn("$500", letter["placeholders"])

    def test_citations_in_a_letter_are_not_listed_as_placeholders(self):
        denial = a_denial()
        self._row(
            denial,
            LETTER + " Coverage follows [CMS NCD 220.2], [42 CFR 438.210] and "
            "the evidence [1], [Smith et al. 2020].",
        )
        [letter] = drafts.collect_letters(denial)
        cited = {"[CMS NCD 220.2]", "[42 CFR 438.210]", "[1]", "[Smith et al. 2020]"}
        self.assertEqual(cited & set(letter["placeholders"]), set())

    def test_every_line_of_a_sender_block_is_listed(self):
        denial = a_denial()
        self._row(
            denial, "[Your Name]\n[Address Line 1]\n[City, State ZIP]\n\n" + LETTER
        )
        [letter] = drafts.collect_letters(denial)
        for line in ("[Your Name]", "[Address Line 1]", "[City, State ZIP]"):
            with self.subTest(line=line):
                self.assertIn(line, letter["placeholders"])

    def test_a_letter_cut_short_lists_only_what_its_text_has(self):
        denial = a_denial()
        self._row(
            denial, LETTER + " Born [DOB: MM/DD/YYYY], [Your {{FIRST_NAME}}]." * 300
        )
        [letter] = drafts.collect_letters(denial)
        self.assertTrue(letter["cut_short"])
        self.assertEqual(
            [found for found in letter["placeholders"] if found not in letter["text"]],
            [],
        )
        self.assertIn("[DOB: MM/DD/YYYY]", letter["placeholders"])
        self.assertIn("[Your {{FIRST_NAME}}]", letter["placeholders"])

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

    def test_the_site_taking_the_generation_lease_means_on_site(self):
        denial = a_denial()
        self.assertFalse(drafts.site_took_generation(denial))
        lease = generation_lease.acquire(denial, generation_lease.new_holder("journey"))
        generation_lease.release(denial, lease.epoch)
        self.assertFalse(drafts.site_took_generation(denial))
        generation_lease.acquire(
            denial, generation_lease.new_holder("interactive"), steal=True
        )
        self.assertTrue(drafts.site_took_generation(denial))


class PlaceholdersTest(SimpleTestCase):
    """What a letter lists for the assistant to fill in: fill-in prompts,
    never citations."""

    def test_fill_in_prompts_are_placeholders(self):
        for prompt in (
            "[DATE]",
            "[Your Phone Number]",
            "[Patient Name]",
            "[Member ID]",
            "[Patient's Name]",
            "[Patient’s Name]",
            "[City, State ZIP]",
            # A digit, colon, semicolon or full stop in a prompt that starts
            # with a fill-in word: the fax form stops these too.
            "[Address Line 1]",
            "[Address Line 2]",
            "[Insert date of denial, e.g. 01/02/2026]",
            "[Date: MM/DD/YYYY]",
            "[Date of Service: MM/DD/YYYY]",
            "[Phone: (555) 555-5555]",
            "[Insert Specific Reason; e.g. step therapy]",
            "[Your Name, Ph.D.]",
            # In lower case, and a choice of pronouns.
            "[doctor name]",
            "[his/her]",
            "{{FIRST_NAME}}",
            "{diagnosis}",
            "$your_name_here",
        ):
            with self.subTest(prompt=prompt):
                self.assertEqual(
                    drafts.placeholders_in(f"Signed {prompt} today."), [prompt]
                )

    def test_citations_are_not_placeholders(self):
        for citation in (
            "[CMS NCD 220.2]",
            "[42 CFR 438.210]",
            "[1]",
            "[Smith et al. 2020]",
            "[29 C.F.R. 2560.503-1]",
            "[Id.]",
            # The notes that go with a quotation.
            "[Emphasis added]",
            "[Internal citations omitted]",
            "[Sic]",
        ):
            with self.subTest(citation=citation):
                self.assertEqual(
                    drafts.placeholders_in(f"As required {citation}, reverse it."),
                    [],
                )

    def test_a_links_text_is_not_a_placeholder(self):
        for letter in (
            "See the [Coverage Policy](https://example.com/policy).",
            "See the [Coverage Policy][1].\n\n[1]: https://example.com/policy",
        ):
            with self.subTest(letter=letter):
                self.assertEqual(drafts.placeholders_in(letter), [])

    def test_the_brackets_listed_are_the_ones_the_fax_form_stops(self):
        # The fax form refuses a letter with any of these left in, so the
        # assistant is asked to fill in each, and nothing it lets through.
        letter = (
            "[Your Name]\n[Address Line 1]\n[City, State ZIP]\n"
            "Re: [patient's name], denied on [insert date of denial, e.g. "
            "01/02/2026] under [CMS NCD 220.2] [1]. [Emphasis added] "
            "[his/her] doctor says so [Id.]."
        )
        listed = drafts.placeholders_in(letter)
        self.assertEqual(
            listed,
            [
                "[Address Line 1]",
                "[City, State ZIP]",
                "[Your Name]",
                "[his/her]",
                "[insert date of denial, e.g. 01/02/2026]",
                "[patient's name]",
            ],
        )
        self.assertEqual(listed, sorted(find_placeholders_as_written(letter)))

    def test_each_fill_in_is_listed_once_sorted(self):
        self.assertEqual(
            drafts.placeholders_in("[Your Name], [DATE], [Your Name] per [1]."),
            ["[DATE]", "[Your Name]"],
        )

    def test_a_bracket_stopped_for_a_blank_inside_it_is_listed_whole(self):
        # The fax form stops [DOB: MM/DD/YYYY] for its MM/DD/YYYY, so the
        # bracket is what the assistant fills in.
        for prompt in (
            "[DOB: MM/DD/YYYY]",
            "[Member ID: XXXXXX]",
            "[Claim #: ________]",
            "[ICD-10: XXX.X]",
            "[Patient: {{FIRST_NAME}} {{LAST_NAME}}]",
        ):
            with self.subTest(prompt=prompt):
                self.assertEqual(
                    drafts.placeholders_in(f"Signed {prompt} today."), [prompt]
                )

    def test_a_bracket_with_a_blank_inside_is_listed_as_the_letter_has_it(self):
        # Not as the fax form reads it once {{FIRST_NAME}} is taken out,
        # "[Your               ]", which is in no letter. {{FIRST_NAME}} on
        # its own is listed too.
        self.assertEqual(
            drafts.placeholders_in(
                "Dear {{FIRST_NAME}}, signed [Your {{FIRST_NAME}}] and "
                "[Your [sic] Name]."
            ),
            ["[Your [sic] Name]", "[Your {{FIRST_NAME}}]", "{{FIRST_NAME}}"],
        )

    def test_every_fill_in_main_listed_is_still_listed(self):
        letter = "\n".join(
            f"Line {i}: {bracket}."
            for i, bracket in enumerate(FILL_INS_MAIN_LISTED + CITATIONS_MAIN_LISTED)
        )
        main_listed = set(MAIN_PLACEHOLDER.findall(letter))
        self.assertEqual(main_listed, set(FILL_INS_MAIN_LISTED + CITATIONS_MAIN_LISTED))
        # Main's list less its citations, and nothing else.
        self.assertEqual(
            set(drafts.placeholders_in(letter)),
            main_listed - set(CITATIONS_MAIN_LISTED),
        )

    def test_every_fill_in_main_listed_in_the_apps_own_templates_is_listed(self):
        fixtures = Path(drafts.__file__).with_name("fixtures")
        templates = [
            row["fields"]
            for name in ("initial.yaml", "followup.yaml")
            for row in yaml.safe_load((fixtures / name).read_text(encoding="utf-8"))
            if row["model"] == "fighthealthinsurance.appealtemplates"
        ]
        self.assertTrue(templates)
        for template in templates:
            letter = template["appeal_text"]
            with self.subTest(template=template["name"]):
                self.assertEqual(
                    set(MAIN_PLACEHOLDER.findall(letter))
                    - set(drafts.placeholders_in(letter)),
                    set(),
                )

    def test_every_listed_placeholder_is_in_the_letter(self):
        """Whatever the letter, each fill-in listed is in it exactly as
        listed: never one put together from a blank the fax form has taken
        out. Letters made at random from fill-ins, citations, links, stray
        brackets and every example in letter_placeholders.json, and cut
        anywhere, as a long letter is."""
        spec = json.loads(PATTERNS_FILE.read_text(encoding="utf-8"))
        pieces = (
            list(FILL_INS_MAIN_LISTED)
            + list(CITATIONS_MAIN_LISTED)
            + [
                example
                for section in ("ignore", "placeholders")
                for entry in spec[section]
                for example in entry["examples"]
            ]
            + spec["reference_links"]["examples"]
            + [
                "{{FIRST_NAME}}",
                "{diagnosis}",
                "$your_name_here",
                "[Your [sic] Name]",
                "[Your [Member ID]",
                "[Coverage Policy](https://example.com/policy)",
                "[Policy dated MM/DD/YYYY](https://example.com/policy)",
                "[Coverage Policy][1]",
                "\n[1]: https://example.com/policy\n",
                "XXXXXX",
                "MM/DD/YYYY",
                "________",
                "[",
                "]",
                "{{",
                "}}",
                "[Your ",
                "Name]",
                "Dear Example Health,",
                "\n",
            ]
        )
        rng = random.Random(20261006)
        letters = list(pieces)
        for _ in range(500):
            letter = "".join(
                rng.choice(pieces) + rng.choice(("", " ", ", ", "\n"))
                for _ in range(rng.randint(1, 8))
            )
            letters.append(letter)
            letters.append(letter[: rng.randint(0, len(letter))])
        wrong = []
        for letter in letters:
            listed = drafts.placeholders_in(letter)
            if listed != sorted(set(listed)) or any(
                found not in letter for found in listed
            ):
                wrong.append((letter, listed))
        self.assertEqual(wrong, [])


class GenerationAnswersTest(TestCase):
    def setUp(self):
        # After the conftest fixture that turns Temporal off.
        self.enterContext(override_settings(**ALL_ON))

    def test_no_answers_with_the_chat_path_off(self):
        denial = a_denial()
        draft = drafts.create_draft(denial).draft
        draft.answers_at = timezone.now()
        draft.save()
        with override_settings(MCP_DRAFT_IN_CHAT_ENABLED=False):
            self.assertIsNone(drafts.answers_for_generation(denial))

    def test_no_answers_until_the_assistant_files_some(self):
        denial = a_denial()
        merge_qa(denial, {"medical_reason": "pain"}, source="test")
        denial.save()
        draft = drafts.create_draft(denial).draft
        self.assertIsNone(drafts.answers_for_generation(denial))
        draft.answers_at = timezone.now()
        draft.save()
        self.assertEqual(drafts.answers_for_generation(denial), {"medical_reason": "pain"})

    def test_answers_ride_as_a_questionnaire_and_never_as_a_control_key(self):
        denial = a_denial()
        seen = {}

        def capture(parameters):
            seen.update(parameters)
            return iter(())

        with patch.object(AppealsBackendHelper, "generate_appeals", capture):
            AppealsBackendHelper.generate_appeals_for_denial(
                denial,
                answers={
                    "What happened?": "a fall",
                    "semi_sekret": "forged",
                    "_internal_hashed_email": "forged",
                    "_background": False,
                    "professional_to_finish": "True",
                },
            )
        self.assertEqual(seen["What happened?"], "a fall")
        self.assertTrue(seen["questionnaire"])
        self.assertEqual(seen["semi_sekret"], denial.semi_sekret)
        self.assertEqual(seen["_internal_hashed_email"], denial.hashed_email)
        self.assertTrue(seen["_background"])
        self.assertNotIn("professional_to_finish", seen)

    def test_without_answers_it_is_no_questionnaire(self):
        denial = a_denial()
        seen = {}

        def capture(parameters):
            seen.update(parameters)
            return iter(())

        with patch.object(AppealsBackendHelper, "generate_appeals", capture):
            AppealsBackendHelper.generate_appeals_for_denial(denial)
        self.assertNotIn("questionnaire", seen)


def _drafts(texts):
    return [
        GeneratedAppeal(text=t, model_name="fhi-internal", context_level="full")
        for t in texts
    ]


class _FakeResponse:
    status = 200

    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body

    def raise_for_status(self):
        return None


class _FakePost:
    def __init__(self, body):
        self._body = body
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        return self

    async def __aenter__(self):
        return _FakeResponse(self._body)

    async def __aexit__(self, *exc):
        return False


@override_settings(FHI_SPEND_BACKGROUND=False)
class WorkerGenerationTest(TransactionTestCase):
    """The worker's own path (appeal_journey_core) for an assistant denial,
    with only the model layer stubbed, as tests/async-unit does."""

    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(**ALL_ON))
        for target in (
            "fighthealthinsurance.common_view_logic.get_rag_context_for_denial",
            "fighthealthinsurance.common_view_logic.MLCitationsHelper.generate_citations_for_denial",
        ):
            patcher = patch(target, new_callable=AsyncMock, return_value=None)
            patcher.start()
            self.addCleanup(patcher.stop)
        pmt = patch("fighthealthinsurance.common_view_logic.AppealsBackendHelper.pmt")
        pmt.start().find_context_for_denial = AsyncMock(return_value=None)
        self.addCleanup(pmt.stop)
        spend._ledger.reset_for_tests()
        self.addCleanup(spend._ledger.reset_for_tests)

    def _assistant_denial(self):
        return Denial.objects.create(
            denial_text="Coverage for the requested MRI was denied as not medically necessary.",
            semi_sekret="sekret",
            hashed_email=Denial.get_hashed_email("worker@example.com"),
            gen_attempts=3,
            channel="assistant",
        )

    def test_a_deepinfra_fallback_for_an_assistant_denial_lands_in_its_own_counter(self):
        denial = self._assistant_denial()
        reply = (
            "Dear Reviewer, I appeal the denial of my MRI: my physician documented "
            "months of conservative treatment and the imaging is medically necessary."
        )
        post = _FakePost(
            {
                "choices": [{"message": {"content": reply}}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 200, "estimated_cost": 0.001234},
            }
        )
        with patch.dict(os.environ, {"DEEPINFRA_API": "test-key"}):
            backend = ml_models.DeepInfra(model="google/gemma-4-26B-A4B-it")
        uses = []

        def make_appeals(*args, **kwargs):
            uses.append(spend.current_use())
            answer = asyncio.run(backend._infer(system_prompts=["Write an appeal."], prompt="MRI"))
            texts = [answer[0]] if answer and answer[0] else []
            return iter(_drafts(texts))

        with patch.object(aiohttp.ClientSession, "post", post), patch(
            "fighthealthinsurance.common_view_logic.appealGenerator"
        ) as generator:
            generator.make_appeals.side_effect = make_appeals
            try:
                appeal_journey_core.generate_and_store_appeals(denial)
            except appeal_journey_core.JourneyIncomplete:
                pass
        self.assertEqual(uses, [spend.ASSISTANT])
        self.assertGreaterEqual(post.calls, 1)
        spend._ledger.flush_sync_for_tests()
        self.assertEqual(SpendCounter.objects.get(name="deepinfra:assistant").amount, 1234)
        self.assertFalse(SpendCounter.objects.filter(name="deepinfra:other").exists())
        self.assertEqual(spend.current_use(), spend.OTHER)

    def test_background_generation_sends_the_filed_answers(self):
        denial = self._assistant_denial()
        merge_qa(denial, {"What happened?": "a fall"}, source="test")
        denial.save()
        draft = drafts.create_draft(denial).draft
        draft.answers_at = timezone.now()
        draft.save()
        seen = []
        real = AppealsBackendHelper.generate_appeals_for_denial

        def spy(denial, **kwargs):
            seen.append(kwargs.get("answers"))
            return real(denial, **kwargs)

        with patch.object(
            AppealsBackendHelper, "generate_appeals_for_denial", side_effect=spy
        ), patch("fighthealthinsurance.common_view_logic.appealGenerator") as generator:
            generator.make_appeals.return_value = iter(())
            try:
                async_to_sync(appeal_journey_core.agenerate_and_store_appeals)(denial)
            except appeal_journey_core.JourneyIncomplete:
                pass
        self.assertEqual(seen, [{"What happened?": "a fall"}])


class ActivityErrorsTest(TestCase):
    def test_an_unexpected_error_reaches_history_without_its_text(self):
        from temporalio.exceptions import ApplicationError

        from fighthealthinsurance.activities.assistant_appeal import _sanitized

        with self.assertRaises(ApplicationError) as caught:
            with _sanitized("reading", "u"):
                raise RuntimeError("Dear Example Health, my MRI")
        self.assertNotIn("MRI", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
