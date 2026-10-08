"""Sharing an appeal finds the case by its id, email and secret together.

Choosing an appeal checks all three (ChooseAppealHelper), and sharing one
now does too. The share page saves the letter only when all three match,
answers with the same page when they do not, and turns away a form it
cannot read with a 400.
"""

import re
from pathlib import Path

from django.test import Client, TestCase
from django.urls import reverse

from fighthealthinsurance import models
from fighthealthinsurance.forms import ShareAppealForm

TEMPLATES = Path(__file__).resolve().parents[2] / "fighthealthinsurance" / "templates"

OWNERS_LETTER = "The letter the owner chose."
SHARED_LETTER = "Dear Reviewer, please reconsider the denial of my claim."

# Posts that name no case, each a change to the owner's own form.
NO_MATCH = {
    "another secret": {"semi_sekret": "not-the-case-secret"},
    "another email": {"email": "someone-else@example.com"},
    "an unknown id": {"denial_id": 9_999_999},
    "an id past the column": {"denial_id": 2**70},
}

# Forms the page cannot read, each a change to the owner's own form.
UNREADABLE = {
    "no secret": {"semi_sekret": None},
    "no letter": {"appeal_text": None},
    "an id that is not a number": {"denial_id": "abc"},
    "a letter over the cap": {
        "appeal_text": "a" * (ShareAppealForm.APPEAL_TEXT_MAX_CHARS + 1)
    },
}


class ShareAppealChecksTheCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.email = "owner@example.com"
        self.denial = models.Denial.objects.create(
            denial_id=4343,
            semi_sekret="the-case-secret",
            hashed_email=models.Denial.get_hashed_email(self.email),
            appeal_text=OWNERS_LETTER,
        )

    def _post(self, **changes):
        data = {
            "denial_id": self.denial.denial_id,
            "email": self.email,
            "semi_sekret": "the-case-secret",
            "appeal_text": SHARED_LETTER,
        }
        data.update(changes)
        data = {key: value for key, value in data.items() if value is not None}
        return self.client.post(reverse("share_appeal"), data)

    def _assert_case_unchanged(self):
        self.denial.refresh_from_db()
        self.assertEqual(self.denial.appeal_text, OWNERS_LETTER)
        self.assertFalse(
            models.ProposedAppeal.objects.filter(for_denial=self.denial).exists()
        )

    def test_the_owner_with_the_case_secret_saves_the_letter(self):
        response = self._post()
        self.assertEqual(response.status_code, 200)
        self.denial.refresh_from_db()
        self.assertEqual(self.denial.appeal_text, SHARED_LETTER)

    def test_the_owners_letter_is_kept_as_an_edited_pick_with_no_model(self):
        self._post()
        chosen = models.ProposedAppeal.objects.get(for_denial=self.denial, chosen=True)
        self.assertEqual(
            (chosen.appeal_text, chosen.editted, chosen.model_name),
            (SHARED_LETTER, True, None),
        )

    def test_a_post_that_matches_no_case_changes_nothing(self):
        for name, changes in NO_MATCH.items():
            with self.subTest(name):
                self._post(**changes)
                self._assert_case_unchanged()

    def test_a_post_that_matches_no_case_gets_the_page_a_match_gets(self):
        misses = {name: self._post(**changes) for name, changes in NO_MATCH.items()}
        match = self._post()
        for name, miss in misses.items():
            with self.subTest(name):
                self.assertEqual(
                    (miss.status_code, miss.content),
                    (match.status_code, match.content),
                )

    def test_a_form_the_page_cannot_read_gets_a_400(self):
        for name, changes in UNREADABLE.items():
            with self.subTest(name):
                self.assertEqual(self._post(**changes).status_code, 400)

    def test_a_form_the_page_cannot_read_changes_nothing(self):
        for name, changes in UNREADABLE.items():
            with self.subTest(name):
                self._post(**changes)
                self._assert_case_unchanged()


class AppealPageShareFormCarriesTheSecret(TestCase):
    def test_the_share_form_posts_the_case_secret(self):
        page = (TEMPLATES / "appeal.html").read_text()
        share_form = re.search(
            r"<form action=\"\{% url 'share_appeal' %\}\".*?</form>", page, re.S
        )
        assert share_form is not None
        self.assertIn('name="semi_sekret"', share_form.group(0))
