"""A page rendered before an edit must not post its stale text back over it.

The health history step is reachable from several places, and the box is
posted on every Next whether or not anyone touched it. So a copy of the page
opened in another tab, or reached with Back after the history was changed or
removed elsewhere, would submit what it was rendered with and quietly undo
the newer edit.

The page carries a digest of the history it was rendered with. A submit whose
box still matches that digest is nobody typing, and if the stored history has
moved since, that submit is refused. A box that differs from the digest is
somebody typing, and the last person to type wins.
"""

import html as html_module
import re

from django.test import Client, TestCase
from django.urls import reverse

from fighthealthinsurance import common_view_logic
from fighthealthinsurance.denial_context import health_history_digest
from fighthealthinsurance.models import Denial


class StaleHealthHistorySubmitTest(TestCase):
    EMAIL = "someone@example.com"

    def _denial(self, history):
        denial = Denial.objects.create(
            denial_text="a denial",
            hashed_email=Denial.get_hashed_email(self.EMAIL),
            health_history=history,
        )
        return denial

    def _update(self, denial, submitted, seen):
        return common_view_logic.DenialCreatorHelper.update_denial(
            email=self.EMAIL,
            denial_id=denial.denial_id,
            semi_sekret=denial.semi_sekret,
            health_history=submitted,
            health_history_seen=seen,
        )

    def test_a_stale_untouched_page_does_not_overwrite_a_newer_edit(self) -> None:
        denial = self._denial("first, from the phone")
        seen = health_history_digest("first, from the phone")
        # somewhere else, the person rewrites it
        denial.health_history = "second, from the laptop"
        denial.save()
        # the old tab presses Next without touching the box
        self._update(denial, "first, from the phone", seen)
        denial.refresh_from_db()
        self.assertEqual(denial.health_history, "second, from the laptop")

    def test_a_stale_untouched_page_does_not_restore_a_removal(self) -> None:
        denial = self._denial("a diagnosis they later thought better of")
        seen = health_history_digest(denial.health_history)
        denial.health_history = ""
        denial.save()
        self._update(denial, "a diagnosis they later thought better of", seen)
        denial.refresh_from_db()
        self.assertEqual(denial.health_history, "")

    def test_typing_into_a_stale_page_still_wins(self) -> None:
        """Refusing an edit would be worse than the problem being fixed."""
        denial = self._denial("first")
        seen = health_history_digest("first")
        denial.health_history = "second"
        denial.save()
        self._update(denial, "what they actually want to say", seen)
        denial.refresh_from_db()
        self.assertEqual(denial.health_history, "what they actually want to say")

    def test_an_ordinary_edit_from_a_current_page_is_saved(self) -> None:
        denial = self._denial("before")
        self._update(denial, "after", health_history_digest("before"))
        denial.refresh_from_db()
        self.assertEqual(denial.health_history, "after")

    def test_clearing_the_box_from_a_current_page_still_removes_it(self) -> None:
        denial = self._denial("remove me")
        self._update(denial, "", health_history_digest("remove me"))
        denial.refresh_from_db()
        self.assertEqual(denial.health_history, "")

    def test_a_caller_that_sends_no_digest_is_unaffected(self) -> None:
        """The REST API and the professional flow never render the box."""
        denial = self._denial("from the api")
        self._update(denial, "replaced by the api", None)
        denial.refresh_from_db()
        self.assertEqual(denial.health_history, "replaced by the api")


def fingerprint_on(html: str) -> str:
    """The digest the rendered page will post back, or "" if it carries none."""
    match = re.search(
        r'<input[^>]*name="health_history_seen"[^>]*value="([^"]*)"',
        html,
    )
    return match.group(1) if match else ""


def textarea_on(html: str) -> str:
    match = re.search(
        r'<textarea[^>]*id="health_history"[^>]*>(.*?)</textarea>',
        html,
        re.DOTALL,
    )
    assert match is not None, "no health_history textarea in the response"
    return html_module.unescape(match.group(1))


class StaleSubmitThroughThePagesTest(TestCase):
    """The rule above, driven through the two ways into the page.

    The unit tests cannot see a template that drops the hidden input or a
    render path that forgets to set it, and either leaves the save with
    no digest and so no opinion.
    """

    EMAIL = "two-tabs@example.com"
    SEMI_SEKRET = "sekret-for-the-stale-page"
    STORED = "Type 2 diabetes since 2019."
    TYPED = "Type 2 diabetes since 2019, and an MS diagnosis this spring."

    def setUp(self):
        self.client = Client()
        self.denial = Denial.objects.create(
            denial_text="Your claim has been denied.",
            hashed_email=Denial.get_hashed_email(self.EMAIL),
            semi_sekret=self.SEMI_SEKRET,
            health_history=self.STORED,
        )

    def denial_ref(self) -> dict:
        return {
            "denial_id": self.denial.denial_id,
            "email": self.EMAIL,
            "semi_sekret": self.SEMI_SEKRET,
        }

    def stored_history(self) -> str:
        return Denial.objects.get(denial_id=self.denial.denial_id).health_history

    def render_by_back_navigation(self):
        page = self.client.get(reverse("hh"), self.denial_ref())
        self.assertEqual(page.status_code, 200)
        return page.content.decode()

    def submit_exactly(self, page_html: str):
        """What that page posts when nobody touches the box."""
        payload = self.denial_ref()
        payload["health_history"] = textarea_on(page_html)
        payload["health_history_seen"] = fingerprint_on(page_html)
        return self.client.post(reverse("hh"), payload)

    def test_back_navigation_carries_the_fingerprint(self):
        html = self.render_by_back_navigation()

        self.assertEqual(fingerprint_on(html), health_history_digest(self.STORED))

    def test_the_render_after_the_upload_step_carries_the_fingerprint(self):
        """The other way in: a resubmitted upload renders this page itself."""
        upload = {
            "email": self.EMAIL,
            "denial_text": "Your claim has been denied.",
            "pii": "on",
            "tos": "on",
            "privacy": "on",
        }
        first = self.client.post(reverse("process"), upload, follow=True)
        self.assertEqual(first.status_code, 200)
        reused_id = self.client.session["denial_id"]
        Denial.objects.filter(denial_id=reused_id).update(health_history=self.STORED)

        second = self.client.post(reverse("process"), upload, follow=True)

        self.assertEqual(second.status_code, 200)
        html = second.content.decode()
        self.assertEqual(textarea_on(html).strip(), self.STORED)
        self.assertEqual(fingerprint_on(html), health_history_digest(self.STORED))

    def test_a_stale_untouched_page_does_not_undo_an_edit_made_since(self):
        stale_tab = self.render_by_back_navigation()
        # The other tab: a real edit, from a page rendered with the same text.
        edit = self.denial_ref()
        edit["health_history"] = self.TYPED
        edit["health_history_seen"] = fingerprint_on(stale_tab)
        self.assertEqual(self.client.post(reverse("hh"), edit).status_code, 200)
        self.assertEqual(self.stored_history(), self.TYPED)

        response = self.submit_exactly(stale_tab)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.stored_history(), self.TYPED)

    def test_a_stale_untouched_page_does_not_restore_a_removal(self):
        stale_tab = self.render_by_back_navigation()
        removal = self.denial_ref()
        removal["health_history"] = ""
        removal["health_history_seen"] = fingerprint_on(stale_tab)
        self.assertEqual(self.client.post(reverse("hh"), removal).status_code, 200)
        self.assertEqual(self.stored_history(), "")

        response = self.submit_exactly(stale_tab)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.stored_history(), "")

    def test_an_untouched_current_page_changes_nothing_and_breaks_nothing(self):
        """The ordinary Back then Next, which #1032 fixed: still fine here."""
        page = self.render_by_back_navigation()

        response = self.submit_exactly(page)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.stored_history(), self.STORED)
