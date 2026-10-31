"""A letter starts at the letter, and a letter to the plan is addressed to it.

First-level appeals go to the insurer's appeals department; a draft that
opens "Dear Esteemed Members of the Review Board," names a body it will
never reach. letter_openings rewrites that salutation to the plan, and
substitute_appeal_fields runs the rewrite on every appeal letter. A draft
that opens with the assistant talking ("Sure, I can help you draft... Here's
a simple one:") loses that preamble on the appeal-generation path, and only
there: in a chat it is the assistant's reply.
"""

from django.test import SimpleTestCase, TestCase

from fighthealthinsurance.common_view_logic import substitute_appeal_fields
from fighthealthinsurance.letter_openings import (
    APPEAL_SALUTATION_INSTRUCTION,
    addressed_to_review_board,
    is_letter_preamble,
    readdress_review_board_salutation,
    strip_letter_preamble,
)
from fighthealthinsurance.models import Denial

BODY = (
    "\n\nI am writing to appeal the denial of coverage for physical therapy. "
    "If this denial is upheld I will request an external review by an "
    "independent review board, as the plan documents allow.\n\n"
    "Sincerely,\nJane Doe\n"
)

REVIEW_BOARD_SALUTATIONS = (
    "Dear Esteemed Members of the Review Board,",
    "Dear Esteemed Members of the Health Plan Review Board,",
    "Esteemed Members of the Review Board,",
    "Dear Members of the Independent Medical Review Board,",
    "Dear Review Board,",
    "To the Review Board,",
    "Dear Esteemed Review Board Members,",
    "Dear Honorable Members of the Utilization Review Panel,",
    "Dear Esteemed Members of the Appeals Review Committee,",
    "dear esteemed members of the review board,",
)

PLAN_SALUTATIONS = (
    "Dear Example Health,",
    "Dear Appeals Department,",
    "Dear Example Health Appeals Department,",
    "To Whom It May Concern:",
    "Dear Sir or Madam,",
)


class ReaddressReviewBoardSalutationTest(SimpleTestCase):
    def test_each_review_board_salutation_is_readdressed_to_the_plan(self):
        for salutation in REVIEW_BOARD_SALUTATIONS:
            with self.subTest(salutation=salutation):
                self.assertTrue(addressed_to_review_board(salutation + BODY))
                self.assertEqual(
                    readdress_review_board_salutation(
                        salutation + BODY, "Example Health"
                    ),
                    "Dear Example Health," + BODY,
                )

    def test_a_salutation_to_the_plan_is_left_alone(self):
        for salutation in PLAN_SALUTATIONS:
            with self.subTest(salutation=salutation):
                self.assertFalse(addressed_to_review_board(salutation + BODY))
                self.assertEqual(
                    readdress_review_board_salutation(
                        salutation + BODY, "Example Health"
                    ),
                    salutation + BODY,
                )

    def test_a_review_board_named_in_the_body_is_not_a_salutation(self):
        letter = (
            "Dear Example Health,\n\n"
            "To the extent the review board, if any, disagrees, I ask that "
            "the plan reconsider.\n"
            "Review board decisions are binding on the plan.\n"
            "I will request an external review by an independent review "
            "board, as the law allows.\n"
        )
        self.assertFalse(addressed_to_review_board(letter))
        self.assertEqual(
            readdress_review_board_salutation(letter, "Example Health"), letter
        )

    def test_the_salutations_own_punctuation_is_kept(self):
        for punctuation in (",", ":", ";"):
            with self.subTest(punctuation=punctuation):
                self.assertEqual(
                    readdress_review_board_salutation(
                        f"Dear Esteemed Members of the Review Board{punctuation}\n",
                        "Example Health",
                    ),
                    f"Dear Example Health{punctuation}\n",
                )

    def test_markdown_and_indentation_around_the_salutation_are_kept(self):
        self.assertEqual(
            readdress_review_board_salutation(
                "Re: Claim 123\n\n  **Dear Esteemed Members of the Review Board,**\n",
                "Example Health",
            ),
            "Re: Claim 123\n\n  **Dear Example Health,**\n",
        )

    def test_a_placeholder_recipient_is_written_in_as_given(self):
        # substitute_appeal_fields passes its own placeholder when the plan
        # is not known, so the person is prompted to fill it in.
        self.assertEqual(
            readdress_review_board_salutation(
                "Dear Esteemed Members of the Review Board,\n", "{insurance_company}"
            ),
            "Dear {insurance_company},\n",
        )

    def test_a_blank_recipient_leaves_the_letter_as_it_is(self):
        letter = "Dear Esteemed Members of the Review Board,\n"
        self.assertEqual(readdress_review_board_salutation(letter, ""), letter)
        self.assertEqual(readdress_review_board_salutation(letter, "  "), letter)

    def test_a_recipient_with_regex_characters_is_written_literally(self):
        self.assertEqual(
            readdress_review_board_salutation(
                "Dear Esteemed Members of the Review Board,\n",
                r"Blue Cross (BCBS) \1 Plan $2",
            ),
            "Dear Blue Cross (BCBS) \\1 Plan $2,\n",
        )

    def test_empty_text_stays_empty(self):
        self.assertEqual(readdress_review_board_salutation("", "Example Health"), "")
        self.assertFalse(addressed_to_review_board(""))

    def test_the_prompt_line_names_the_department_and_forbids_the_board(self):
        self.assertIn("appeals department", APPEAL_SALUTATION_INSTRUCTION)
        self.assertIn("Review Board", APPEAL_SALUTATION_INSTRUCTION)
        self.assertIn("Esteemed Members", APPEAL_SALUTATION_INSTRUCTION)


class SubstitutedSalutationTest(TestCase):
    def setUp(self):
        self.denial = Denial.objects.create(
            hashed_email=Denial.get_hashed_email("salutation@example.com"),
            insurance_company="Example Health",
        )

    def test_an_appeal_to_the_plan_is_readdressed_from_the_review_board(self):
        letter = substitute_appeal_fields(
            self.denial, "Dear Esteemed Members of the Review Board," + BODY
        )
        self.assertEqual(letter, "Dear Example Health," + BODY)

    def test_the_appeals_committee_salutation_still_becomes_the_plan(self):
        # The pre-existing literal substitution, unchanged.
        letter = substitute_appeal_fields(
            self.denial, "Dear Esteemed Members of the Appeals Committee," + BODY
        )
        self.assertEqual(letter, "Dear Example Health," + BODY)

    def test_an_unknown_plan_leaves_the_placeholder_for_the_person(self):
        self.denial.insurance_company = "UNKNOWN"
        letter = substitute_appeal_fields(
            self.denial, "Dear Esteemed Members of the Review Board," + BODY
        )
        self.assertEqual(letter, "Dear {insurance_company}," + BODY)


LETTER = (
    "Jane Doe\n123 Main Street\n\n"
    "Dear Example Health,\n\n"
    "I am writing to appeal the denial of coverage for physical therapy.\n\n"
    "Sincerely,\nJane Doe\n"
)

PREAMBLES = (
    "Sure, I can help you draft a template for an appeal letter. Here's a simple one:",
    "Certainly! Here is a draft of an appeal letter you can use:",
    "Here's a draft appeal letter based on the information provided:",
    "Okay, here's a comprehensive appeal letter:",
    "Of course. Below is an appeal letter addressing the denial.",
    "I'd be happy to help you with your appeal. Here is a draft:",
    "I can certainly help with that. Below is a template you can adapt.",
    "Let me draft an appeal letter for you:",
    "**Here is your appeal letter:**",
    "Absolutely, here is a sample appeal letter.",
)

LETTER_LINES = (
    "Dear Example Health,",
    "Jane Doe",
    "Re: Appeal of claim 123",
    "To Whom It May Concern:",
    "Here is my formal appeal of your denial dated March 3, 2026.",
    "I am writing to appeal the denial of coverage for physical therapy.",
    "Okay is not how our conversation on March 3 ended, so I am appealing.",
    "Sure Health Plan denied the claim on March 3.",
)


class StripLetterPreambleTest(SimpleTestCase):
    def test_each_preamble_comes_off_the_top(self):
        for preamble in PREAMBLES:
            with self.subTest(preamble=preamble):
                self.assertTrue(is_letter_preamble(preamble))
                self.assertEqual(strip_letter_preamble(f"{preamble}\n\n{LETTER}"), LETTER)
                # With no blank line after it either.
                self.assertEqual(strip_letter_preamble(f"{preamble}\n{LETTER}"), LETTER)

    def test_a_two_line_preamble_comes_off(self):
        self.assertEqual(
            strip_letter_preamble(f"Certainly!\n\nHere is the appeal letter:\n\n{LETTER}"),
            LETTER,
        )

    def test_a_markdown_fence_around_the_letter_comes_off_with_the_preamble(self):
        self.assertEqual(
            strip_letter_preamble(
                f"Okay, here's a comprehensive appeal letter:\n\n```\n{LETTER}```\n"
            ),
            LETTER.rstrip("\n"),
        )
        self.assertEqual(
            strip_letter_preamble(f"Here is the letter:\n```markdown\n{LETTER}\n```"),
            LETTER,
        )

    def test_a_letter_without_a_preamble_is_unchanged(self):
        self.assertEqual(strip_letter_preamble(LETTER), LETTER)
        for line in LETTER_LINES:
            with self.subTest(line=line):
                self.assertFalse(is_letter_preamble(line))
                text = f"{line}\n\nThe rest of the letter.\n"
                self.assertEqual(strip_letter_preamble(text), text)

    def test_a_fence_without_a_preamble_is_left_alone(self):
        text = f"```\n{LETTER}```\n"
        self.assertEqual(strip_letter_preamble(text), text)

    def test_nothing_but_a_preamble_is_unchanged(self):
        for text in ("Sure, here is the appeal letter:", "Okay.\n\n", "", "\n\n"):
            with self.subTest(text=text):
                self.assertEqual(strip_letter_preamble(text), text)

    def test_only_the_top_is_touched(self):
        text = f"{LETTER}\nHere is a summary of the key points:\n- Medical necessity\n"
        self.assertEqual(strip_letter_preamble(text), text)
