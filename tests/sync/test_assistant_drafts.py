"""assistant_drafts.py: the chat path's bookkeeping, letters and sweep."""

import asyncio
import json
import os
import random
import re
import time
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
    find_placeholder_spans,
    find_unfilled_placeholders,
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


# What main listed before placeholders_in, kept here as main had it: every
# bracket that starts with a capital, citations and all, with {{...}}, {...}
# and $name. placeholders_in lists this, less CITATIONS_MAIN_LISTED.
MAIN_PLACEHOLDER = re.compile(
    r"\{\{?[A-Za-z_][A-Za-z0-9_ ]*\}\}?|\[[A-Z][^\]\n]{2,40}\]|\$[a-z][a-z_]{2,}\b"
)
# Fill-ins main listed, each still listed.
FILL_INS_MAIN_LISTED = (
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
    # A digit, full stop, colon or semicolon, with no fill-in word first.
    "[Dr. Name]",
    "[ICD-10 Code]",
    "[Policy No.]",
    "[Claim Number: ]",
    "[Member ID:]",
    "[Physician Name, M.D.]",
    "[Diagnosis; Procedure]",
    # A blank inside it.
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
    # A year or a number with a point in an example value, which no
    # citation rule takes.
    "[Month of denial, e.g. March 2026]",
    "[Insert year, e.g. 2026]",
    "[Plan Year 2026]",
    "[A1c: 7.2%]",
    "[BMI 32.5]",
    "[Weight: 150.5 lbs]",
    "[Amount Billed: $1,234.56]",
    "[Phone: 555.555.5555]",
    "[ICD-10 Code: E11.9]",
    "[Section number]",
    # A {{...}} inside makes a bracket a fill-in, whatever else it holds.
    "[Smith et al. {{YEAR}}]",
    # A citation word or a year in a fill-in doesn't make it a citation: only
    # a bracket that is a citation as a whole is one (found in eval letters).
    "[USC Specialist's Name]",
    "[Month, 2018]",
    "[Late 2018]",
    "[Month Day, 2026]",
    "[January XX, 2026]",
    "[DD Month 2026]",
    "[Your Name, 2026]",
    "[Insert Date, 2026]",
    "[Signature Date 2026]",
    "[Current dose, e.g. 0.125 mg]",
    "[Dose: 0.125 mg]",
    "[Vivelle Dot 0.025 mg patch]",
    "[Reference Number]",
    "[XX-XX-2026]",
    "[MEMBER_ID_2026]",
    "[Id. at PAGE_NUMBER]",
    "[NPI, e.g. 2026]",
)
# Brackets main listed that no citation rule takes, so they stay listed:
# exhibits and attachments (see placeholders_in), a PubMed id and a link's
# text.
OTHER_BRACKETS_MAIN_LISTED = (
    "[Exhibit A]",
    "[Exhibit 1]",
    "[Attachment B]",
    "[Appendix C]",
    "[PMID: 12345678]",
    "[Coverage Policy]",
)
# Brackets main listed that are citations or a quotation's notes, which are
# no longer listed. A reference number or list ([1], [3, 4], [2-5]) never
# starts with a capital, so main never listed one (NEVER_LISTED).
CITATIONS_MAIN_LISTED = (
    # A regulation or statute.
    "[CMS NCD 220.2]",
    "[See 42 CFR 438.210]",
    "[See 29 C.F.R. 2560.503-1]",
    "[Title 42 U.S.C. 300gg-19]",
    "[Cf. 29 USC 1133]",
    "[Medicare LCD L33822]",
    "[Pub. L. 111-148]",
    "[ERISA § 503]",
    "[See 42 C.F.R. §§ 438.210, 438.404]",
    "[See 42 CFR 438.210 and 438.211]",
    "[Section 438.210]",
    "[Section 2.1]",
    "[Sec. 4.3]",
    "[Section 1557]",
    "[ERISA Section 503]",
    "[ACA Section 2719]",
    "[SSA Section 1862(a)(1)(A)]",
    # A reference marker with a capital.
    "[Reference 1]",
    "[References 2, 3]",
    "[References 6-10]",
    "[References 1–4]",
    "[Ref. 1]",
    "[Refs. 2-4]",
    # Authors and a year.
    "[Smith et al.]",
    "[Smith et al. 2020]",
    "[Smith 2020]",
    "[Smith 2020a]",
    "[Smith and Jones, 2019]",
    "[Smith & Jones 2019b]",
    "[American Diabetes Association, 2023]",
    "[American College of Physicians, 2023]",
    "[O'Brien (2018)]",
    "[Narang et al., 2017]",
    "[May et al., 2020]",
    "[García et al., 2020]",
    "[Smith et al., 2020; Jones et al., 2021]",
    "[Ng 2019; O'Brien (2018)]",
    "[Smith et al., 2020, p. 3]",
    "[WATER Trial (2017)]",
    # A quotation's notes.
    "[Emphasis added]",
    "[Emphasis ours]",
    "[Emphasis in original]",
    "[Emphasis omitted]",
    "[Emphasis supplied]",
    "[Emphasis mine]",
    "[Emphasis in the original]",
    "[EMPHASIS ADDED]",
    "[Internal citations omitted]",
    "[Internal quotation marks omitted]",
    "[Internal footnotes omitted]",
    "[Footnotes omitted]",
    "[Footnotes and citations omitted]",
    "[Citations omitted]",
    "[Alterations in original]",
    "[Brackets in original]",
    "[Cleaned up]",
    "[Sic]",
    "[Emphasis added; citations omitted]",
    "[Citations and quotation marks omitted]",
    # Id. and Ibid.
    "[Id.]",
    "[Id. at 5]",
    "[Ibid.]",
    "[Ibid]",
    "[Ibid., p. 12]",
)
# Brackets neither main nor the site's fax check lists, which are still not
# listed: reference numbers, a bare statute, a quotation's [sic].
NEVER_LISTED = (
    "[1]",
    "[12]",
    "[3, 4]",
    "[2-5]",
    "[42 CFR 438.210]",
    "[§ 2719]",
    "[sic]",
)
# Blanks main never listed (too short, too long, or not starting with a
# capital) that the site's fax check stops a letter for, so they are listed
# now: the assistant fills them in before a fax would stop.
FAX_CHECK_BLOCKS = (
    "[It]",
    "[We]",
    "[National Comprehensive Cancer Network Clinical Practice Guidelines]",
    "[doctor name]",
    "[doctor's name]",
    "[his/her]",
    "[ Your Name ]",
    "<Patient Name>",
    "{{ FIRST_NAME }}",
    "_______________",
    "XX/XX/XXXX",
    "XXXXXX",
    "MM/DD/YYYY",
    "(Insert claim number)",
    "INSERT DATE HERE",
    "FirstName",
    "$DATE",
)


def fax_finds(letter):
    """What the site's fax check stops this letter for, as the letter has it."""
    return [letter[start:end] for start, end in find_placeholder_spans(letter)]


def covered(letter, start, end, listed):
    """Whether letter[start:end] is listed, or is inside a listed fill-in
    where the letter has one."""
    for other in listed:
        at = letter.find(other, max(0, end - len(other)))
        while at != -1 and at <= start:
            if at + len(other) >= end:
                return True
            at = letter.find(other, at + 1)
    return False


def generated_brackets():
    """Brackets made from parts, as (fill-ins, citations): every pairing of
    a label with a fill-in shape, and of authors, regulations and notes
    with the ways a letter writes them. Only the ones main listed."""
    labels = (
        "Name",
        "Your Name",
        "Dr. Name",
        "Patient Name",
        "Member ID",
        "Policy No.",
        "Claim Number",
        "ICD-10 Code",
        "CPT Code",
        "Physician Name, M.D.",
        "Date of Birth",
        "Group Number",
        "NPI",
        "Date of Service",
        "Provider",
        "Insurance Company",
    )
    shapes = (
        "[{}]",
        "[{}:]",
        "[{}: ]",
        "[{}: XXXXXX]",
        "[{}: MM/DD/YYYY]",
        "[{}: ________]",
        "[{}, e.g. 2026]",
        "[{}, e.g. 98.6]",
        "[{} No.]",
        "[{}; if any]",
        "[{}: {{{{SCSID}}}}]",
        "[{} $your_name_here]",
    )
    fill_ins = {shape.format(label) for label in labels for shape in shapes}
    authors = (
        "Smith",
        "Smith and Jones",
        "Smith & Jones",
        "O'Brien",
        "Lee, Park and Kim",
        "American Diabetes Association",
        "NCCN",
        "Smith et al.",
    )
    years = ("1998", "2020", "2020a", "2024b")
    citations = {
        shape.format(author, year)
        for author in authors
        for year in years
        for shape in ("[{} {}]", "[{}, {}]", "[{} ({})]")
    }
    citations |= {
        f"[{lead} {cite}]"
        for lead in ("See", "Cf.", "Under", "Per")
        for cite in (
            "42 CFR 438.210",
            "45 C.F.R. § 147.136",
            "29 U.S.C. § 1133",
            "42 USC 300gg-19",
            "NCD 220.2",
            "LCD L33822",
            "Pub. L. 111-148",
            "§ 2719",
            "Section 438.210",
            "Section 2.1",
            "Sec. 2.1",
        )
    }
    notes = (
        "emphasis added",
        "emphasis ours",
        "emphasis in original",
        "emphasis omitted",
        "internal citations omitted",
        "internal quotation marks omitted",
        "footnotes omitted",
        "footnotes and citations omitted",
        "citations omitted",
        "alterations in original",
        "brackets in original",
        "cleaned up",
        "sic",
    )
    written = (str.capitalize, str.title, str.upper)
    citations |= {f"[{case(note)}]" for note in notes for case in written}
    citations |= {
        f"[{first.capitalize()}{between}{second}]"
        for first in notes
        for second in notes
        if first != second
        for between in ("; ", ", ", " and ")
    }
    citations |= {f"[{cite}]" for cite in ("Id.", "Id. at 12", "Ibid.", "Ibid")}

    def main_lists(bracket):
        return MAIN_PLACEHOLDER.fullmatch(bracket) is not None

    return (
        sorted(filter(main_lists, fill_ins)),
        sorted(filter(main_lists, citations)),
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

    def _answered(self, status, expired_for=timedelta(seconds=1)):
        denial = a_denial()
        merge_qa(denial, {"What happened?": "a fall"}, source="test")
        denial.save()
        new = drafts.create_draft(denial)
        AssistantDraft.objects.filter(pk=new.draft.pk).update(
            status=status,
            answers_at=timezone.now() - expired_for,
            expires_at=timezone.now() - expired_for,
        )
        return denial, new

    @override_settings(**ALL_ON)
    def test_filed_answers_survive_the_sweep_while_the_run_drafts(self):
        for status in (drafts.QUESTIONS, drafts.DRAFTING):
            with self.subTest(status=status):
                denial, new = self._answered(status)
                self.assertEqual(drafts.sweep_expired(), 0)
                self.assertIsNone(drafts.find_draft(new.draft_id))
                self.assertEqual(
                    drafts.answers_for_generation(denial), {"What happened?": "a fall"}
                )

    def test_an_answered_draft_goes_once_its_run_ends(self):
        for status in (drafts.READY, drafts.STOPPED, drafts.ON_SITE, drafts.EXPIRED):
            with self.subTest(status=status):
                _, new = self._answered(status)
                self.assertEqual(drafts.sweep_expired(), 1)
                self.assertFalse(AssistantDraft.objects.filter(pk=new.draft.pk).exists())

    def test_an_answered_draft_still_drafting_goes_a_day_after_its_expiry(self):
        _, new = self._answered(
            drafts.DRAFTING, expired_for=drafts.ANSWERED_KEPT_FOR + timedelta(seconds=1)
        )
        self.assertEqual(drafts.sweep_expired(), 1)
        self.assertFalse(AssistantDraft.objects.filter(pk=new.draft.pk).exists())

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
        self.assertEqual(cleaned[4]["question"], "x" * 400)
        for q in cleaned:
            self.assertEqual(set(q), {"name", "kind", "label", "choices", "question"})
            self.assertEqual(q["name"], drafts.question_field_name(q["question"]))
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

    def test_answers_file_under_the_questions_shown_after_the_denials_change(self):
        denial = a_denial()
        long_question = "What did your doctor say about the scan? " + "Details. " * 40
        draft = asking(denial, [("Is the MRI for an injury?", ""), (long_question, "")])
        # The site, or a retried step, wrote new questions since.
        Denial.objects.filter(pk=denial.pk).update(
            generated_questions=[("How long have you had back pain?", "")]
        )
        yes_no, text = draft.questions
        drafts.file_answers(
            draft,
            [
                {"name": yes_no["name"], "value": "no"},
                {"name": text["name"], "value": "It's needed."},
            ],
        )
        denial.refresh_from_db()
        self.assertEqual(
            load_qa(denial),
            {"Is the MRI for an injury?": "No", long_question.strip(): "It's needed."},
        )

    def test_answers_file_for_a_draft_stored_before_its_question_text_was(self):
        # A draft asking when the question text was first kept on it has only
        # name, kind, label and choices: the text comes from the denial.
        denial = a_denial()
        draft = asking(denial, [("Is the MRI for an injury?", "")])
        draft.questions = [
            {k: v for k, v in q.items() if k != "question"} for q in draft.questions
        ]
        draft.save(update_fields=["questions"])
        drafts.file_answers(draft, [{"name": draft.questions[0]["name"], "value": "yes"}])
        denial.refresh_from_db()
        self.assertEqual(load_qa(denial), {"Is the MRI for an injury?": "Yes"})

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

    def test_a_long_bracket_that_says_what_to_put_there_is_listed(self):
        letter = (
            "Claim/Reference Number: [Insert Reference Number from Denial Letter]. "
            "As shown in [Shi et al., Nighttime Respiratory Disturbance in Children]."
        )
        self.assertEqual(
            drafts.placeholders_in(letter),
            ["[Insert Reference Number from Denial Letter]"],
        )

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

    def _signed_letter(self, length):
        body = "Dear Example Health, I appeal the denial of my MRI."
        while len(body) < length:
            body += " The scan is medically necessary for my back pain."
        return body + "\n\nSincerely,\n{{FIRST_NAME}} {{LAST_NAME}}"

    def test_a_letter_cut_short_lists_the_fill_ins_in_the_part_cut_off(self):
        denial = a_denial()
        self._row(denial, self._signed_letter(drafts.LETTER_MAX_CHARS + 100))
        [letter] = drafts.collect_letters(denial)
        self.assertTrue(letter["cut_short"])
        self.assertNotIn("{{FIRST_NAME}}", letter["text"])
        self.assertEqual(letter["placeholders"], ["{{FIRST_NAME}}", "{{LAST_NAME}}"])

    def test_a_letter_as_long_as_a_real_one_comes_back_whole(self):
        denial = a_denial()
        # Past the 6,000 the chat once cut at: model letters run to 7,000.
        self._row(denial, self._signed_letter(7_500))
        [letter] = drafts.collect_letters(denial)
        self.assertFalse(letter["cut_short"])

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
    """What a letter lists for the assistant to fill in: what main listed,
    less citations and a quotation's notes, and nothing main never listed."""

    def test_fill_ins_main_listed_are_listed(self):
        for prompt in FILL_INS_MAIN_LISTED + (
            "[DATE]",
            "{{FIRST_NAME}}",
            "{diagnosis}",
            "$your_name_here",
        ):
            with self.subTest(prompt=prompt):
                self.assertEqual(
                    drafts.placeholders_in(f"Signed {prompt} today."), [prompt]
                )

    def test_fill_ins_with_a_digit_stop_or_colon_are_all_listed(self):
        letter = (
            "Physician: [Dr. Name]\nCode: [ICD-10 Code]\nPolicy: [Policy No.]\n"
            "Claim: [Claim Number: ]\nMember: [Member ID:]\n"
            "Signed, [Physician Name, M.D.]"
        )
        self.assertEqual(
            drafts.placeholders_in(letter),
            [
                "[Claim Number: ]",
                "[Dr. Name]",
                "[ICD-10 Code]",
                "[Member ID:]",
                "[Physician Name, M.D.]",
                "[Policy No.]",
            ],
        )

    def test_citations_and_quotation_notes_are_not_listed(self):
        for citation in CITATIONS_MAIN_LISTED:
            with self.subTest(citation=citation):
                self.assertEqual(
                    drafts.placeholders_in(f"As required {citation}, reverse it."),
                    [],
                )

    def test_exhibits_and_other_brackets_main_listed_stay_listed(self):
        for bracket in OTHER_BRACKETS_MAIN_LISTED:
            with self.subTest(bracket=bracket):
                self.assertEqual(
                    drafts.placeholders_in(f"See {bracket} enclosed."), [bracket]
                )

    def test_what_neither_main_nor_the_fax_check_lists_is_not_listed(self):
        for bracket in NEVER_LISTED:
            with self.subTest(bracket=bracket):
                self.assertEqual(drafts.placeholders_in(f"So {bracket} said."), [])

    def test_what_the_fax_check_blocks_is_listed(self):
        for blank in FAX_CHECK_BLOCKS:
            letter = f"Dear plan, signed {blank} today. Sincerely."
            with self.subTest(blank=blank):
                self.assertTrue(find_unfilled_placeholders(letter))
                self.assertEqual(drafts.placeholders_in(letter), [blank])

    def test_a_blank_the_fax_check_finds_inside_a_listed_one_is_not_listed_again(self):
        self.assertEqual(
            drafts.placeholders_in(
                "Born [DOB: MM/DD/YYYY], claim [Claim #: ________], [ Your {{SCSID}} ]."
            ),
            ["[ Your {{SCSID}} ]", "[Claim #: ________]", "[DOB: MM/DD/YYYY]"],
        )

    def test_a_quotation_note_the_fax_check_blocks_is_still_not_listed(self):
        for note in ("[Cleaned up]", "[Emphasis ours]", "[Brackets in original]", "[Ibid]"):
            letter = f"The plan must cover it {note}."
            with self.subTest(note=note):
                self.assertTrue(find_unfilled_placeholders(letter))
                self.assertEqual(drafts.placeholders_in(letter), [])

    def test_each_fill_in_is_listed_once_sorted(self):
        self.assertEqual(
            drafts.placeholders_in("[Your Name], [DATE], [Your Name] per [1]."),
            ["[DATE]", "[Your Name]"],
        )

    def test_a_fill_in_inside_a_listed_bracket_is_not_listed_again(self):
        # {{FIRST_NAME}} on its own is listed; inside a bracket, the bracket is.
        self.assertEqual(
            drafts.placeholders_in(
                "Dear {{FIRST_NAME}}, signed [Your {{FIRST_NAME}}] for "
                "[Patient $your_name_here]."
            ),
            ["[Patient $your_name_here]", "[Your {{FIRST_NAME}}]", "{{FIRST_NAME}}"],
        )

    def test_a_bracket_opened_inside_a_listed_one_is_not_listed_on_its_own(self):
        # Read left to right, the first bracket runs to the first "]", as
        # on main: [Your Name] is part of it, and {{X}} comes after it.
        self.assertEqual(
            drafts.placeholders_in("Ref [Dear [Your Name] Sir 2] {{X}} ok"),
            ["[Dear [Your Name]", "{{X}}"],
        )

    def test_a_bracket_the_fax_check_stops_for_whole_is_listed_whole(self):
        # The fax check takes [Your Name] and {{X}}, then the bracket round
        # them, so the letter is stopped for all of it.
        self.assertEqual(
            drafts.placeholders_in("Ref [Dear [Your Name] Sir {{X}}] ok"),
            ["[Dear [Your Name] Sir {{X}}]"],
        )

    def _listed_within_a_second(self, letter):
        started = time.perf_counter()
        listed = drafts.placeholders_in(letter)
        self.assertLess(time.perf_counter() - started, 1.0)
        return listed

    def test_a_long_bracket_with_many_joining_words_is_listed_at_once(self):
        # The fax check stops for a bracket of up to 200 characters, and each
        # one goes through the citation rules. A rule that read each "and",
        # "of" and "the" two ways took seconds on this one, and the server
        # stalled while it ran.
        bracket = (
            "[Name of the Doctor and the Name of the Clinic and the Address of "
            "the Clinic and the Phone Number of the Clinic and the Fax Number "
            "of the Clinic and the Name of the Plan/Insurer]"
        )
        self.assertEqual(
            self._listed_within_a_second(f"Please send {bracket} today."), [bracket]
        )

    def test_a_long_bracket_with_a_run_of_section_numbers_is_listed_at_once(self):
        # Section numbers with no spaces round each "and", which a rule could
        # read as one section or as a list.
        bracket = "[Date § " + "1and" * 24 + "1 x]"
        self.assertEqual(
            self._listed_within_a_second(f"Please send {bracket} today."), [bracket]
        )

    def test_a_bracket_with_a_fill_in_inside_is_never_a_citation(self):
        for bracket in (
            "[See 42 CFR [Your Name]",
            "[Emphasis added [Your Name]",
            "[Smith et al. {{YEAR}}]",
            "[Id. at $page_number]",
        ):
            with self.subTest(bracket=bracket):
                self.assertEqual(
                    drafts.placeholders_in(f"Per {bracket} today."), [bracket]
                )

    def test_what_is_listed_is_what_main_or_the_fax_check_finds_less_citations(self):
        """Letters made at random from generated and hand-written brackets
        (fill-ins, citations of every kind, ones main never listed, blanks
        only the fax check finds) with {{...}}, {...} and $name between
        them, and cut anywhere, as a long letter is: each lists what main's
        pattern and the site's fax check find in it, less the citations,
        and nothing else, with a fill-in inside another listed only as part
        of it."""
        fill_ins, citations = generated_brackets()
        # A large corpus, all of it brackets main listed.
        self.assertGreater(len(fill_ins), 150)
        self.assertGreater(len(citations), 300)
        fill_ins += FILL_INS_MAIN_LISTED + OTHER_BRACKETS_MAIN_LISTED
        citations += CITATIONS_MAIN_LISTED
        self.assertEqual(
            [
                bracket
                for bracket in fill_ins + citations
                if not MAIN_PLACEHOLDER.fullmatch(bracket)
            ],
            [],
        )
        self.assertEqual(
            [
                bracket
                for bracket in NEVER_LISTED + FAX_CHECK_BLOCKS
                if MAIN_PLACEHOLDER.search(bracket)
            ],
            [],
        )
        pieces = (
            fill_ins
            + citations
            + list(NEVER_LISTED)
            + list(FAX_CHECK_BLOCKS)
            + ["{{FIRST_NAME}}", "{diagnosis}", "$your_name_here", "It cost $500."]
        )
        rng = random.Random(20261006)
        shuffled = rng.sample(pieces, len(pieces))
        # Every piece at least once, then at random.
        letters = [
            "\n".join(shuffled[start : start + 7])
            for start in range(0, len(shuffled), 7)
        ]
        for _ in range(2_000):
            letter = "".join(
                rng.choice(pieces) + rng.choice((" ", ", ", "\n", " and ", ". "))
                for _ in range(rng.randint(1, 12))
            )
            letters.append(letter)
            letters.append(letter[: rng.randint(0, len(letter))])
        citations = set(citations)
        wrong = []
        for letter in letters:
            listed = drafts.placeholders_in(letter)
            found = [
                match.span()
                for match in MAIN_PLACEHOLDER.finditer(letter)
                if match.group(0) not in citations
            ] + [
                (start, end)
                for start, end in find_placeholder_spans(letter)
                if letter[start:end] not in citations
            ]
            missed = [
                letter[start:end]
                for start, end in found
                if not covered(letter, start, end, listed)
            ]
            from_elsewhere = [
                item
                for item in listed
                if item in citations
                or item not in set(MAIN_PLACEHOLDER.findall(letter)) | set(fax_finds(letter))
            ]
            if listed != sorted(set(listed)) or missed or from_elsewhere:
                wrong.append((letter, listed, missed, from_elsewhere))
        self.assertEqual(wrong[:5], [])

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
            listed = drafts.placeholders_in(letter)
            with self.subTest(template=template["name"]):
                self.assertEqual(
                    [
                        match.group(0)
                        for match in MAIN_PLACEHOLDER.finditer(letter)
                        if not covered(letter, *match.span(), listed)
                    ],
                    [],
                )
                self.assertEqual(
                    [
                        letter[start:end]
                        for start, end in find_placeholder_spans(letter)
                        if not covered(letter, start, end, listed)
                    ],
                    [],
                )

    def test_every_listed_placeholder_is_in_the_letter(self):
        """Whatever the letter, each fill-in listed is in it exactly as
        listed, and main or the fax check found it. Letters made at random from
        fill-ins, citations, links, stray and nested brackets and every
        example in letter_placeholders.json, and cut anywhere."""
        spec = json.loads(PATTERNS_FILE.read_text(encoding="utf-8"))
        pieces = (
            list(FILL_INS_MAIN_LISTED)
            + list(OTHER_BRACKETS_MAIN_LISTED)
            + list(CITATIONS_MAIN_LISTED)
            + list(NEVER_LISTED)
            + list(FAX_CHECK_BLOCKS)
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
                "[Coverage Policy][1]",
                "\n[1]: https://example.com/policy\n",
                "XXXXXX",
                "MM/DD/YYYY",
                "[",
                "]",
                "{{",
                "}}",
                "[Your ",
                "Name]",
                "[See 42 CFR ",
                "Dear Example Health,",
                "\n",
            ]
        )
        rng = random.Random(20261006)
        letters = list(pieces)
        for _ in range(1_000):
            letter = "".join(
                rng.choice(pieces) + rng.choice(("", " ", ", ", "\n"))
                for _ in range(rng.randint(1, 8))
            )
            letters.append(letter)
            letters.append(letter[: rng.randint(0, len(letter))])
        wrong = []
        for letter in letters:
            listed = drafts.placeholders_in(letter)
            if (
                listed != sorted(set(listed))
                or any(found not in letter for found in listed)
                or not set(listed)
                <= set(MAIN_PLACEHOLDER.findall(letter)) | set(fax_finds(letter))
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


@override_settings(FHI_SPEND_BACKGROUND=False)
class ActivityTest(TransactionTestCase):
    """The real activities the workflow runs, on the database."""

    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(**ALL_ON))

    def _run(self, step, denial):
        return async_to_sync(step)(denial.hashed_email, str(denial.uuid))

    def test_a_retried_ask_keeps_the_questions_the_assistant_was_shown(self):
        from fighthealthinsurance.activities import assistant_appeal as acts
        from fighthealthinsurance.common_view_logic import DenialCreatorHelper

        denial = a_denial()
        shown = asking(denial, [("Is the MRI for an injury?", "")]).questions
        fresh = AsyncMock(return_value=[("How long have you had back pain?", "")])
        with patch.object(DenialCreatorHelper, "generate_appeal_questions", fresh):
            self.assertEqual(self._run(acts.ask_questions, denial), 1)
        draft = AssistantDraft.objects.get(denial=denial)
        self.assertEqual(draft.questions, shown)
        fresh.assert_not_awaited()

    def test_drafting_starts_after_the_day_when_the_answers_came_in_time(self):
        from fighthealthinsurance.activities import assistant_appeal as acts

        denial = a_denial()
        draft = drafts.create_draft(denial).draft
        AssistantDraft.objects.filter(pk=draft.pk).update(
            status=drafts.QUESTIONS,
            answers_at=timezone.now() - timedelta(minutes=1),
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        self.assertTrue(self._run(acts.start_drafting, denial))
        self.assertEqual(AssistantDraft.objects.get(pk=draft.pk).status, drafts.DRAFTING)

    def test_drafting_does_not_start_after_the_day_without_answers(self):
        from fighthealthinsurance.activities import assistant_appeal as acts

        denial = a_denial()
        draft = drafts.create_draft(denial).draft
        AssistantDraft.objects.filter(pk=draft.pk).update(
            status=drafts.READING, expires_at=timezone.now() - timedelta(seconds=1)
        )
        self.assertFalse(self._run(acts.start_drafting, denial))
