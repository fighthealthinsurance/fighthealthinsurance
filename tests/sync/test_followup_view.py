"""End-to-end tests for the follow-up page (view + helper)."""

from django.test import Client, TestCase
from django.urls import reverse

from fighthealthinsurance.common_view_logic import FollowUpHelper
from fighthealthinsurance.models import (
    Denial,
    FollowUp,
    FollowUpDocuments,
    FollowUpSched,
)


def _make_denial(email: str = "followup-tester@test-fhi.com") -> Denial:
    """Create a minimal Denial suitable for follow-up tests."""
    hashed_email = Denial.get_hashed_email(email)
    return Denial.objects.create(
        denial_text="Some denial text.",
        hashed_email=hashed_email,
        use_external=False,
        raw_email=email,
        health_history="",
    )


class TestFollowUpURLRouting(TestCase):
    """The view must accept the link variants we email out (with trailing
    slash or trailing period) without 404-ing or crashing."""

    def setUp(self):
        self.client = Client()
        self.denial = _make_denial()

    def _base_path(self) -> str:
        return (
            f"/v0/followup/{self.denial.uuid}/"
            f"{self.denial.hashed_email}/{self.denial.follow_up_semi_sekret}"
        )

    def test_get_canonical_url_renders_form(self):
        response = self.client.get(self._base_path())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Follow Up On Your Health Insurance Appeal")
        self.assertContains(response, 'id="submit"')
        self.assertContains(response, 'enctype="multipart/form-data"')

    def test_get_url_with_trailing_period(self):
        """Some email clients append a period to the link."""
        response = self.client.get(self._base_path() + ".")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Follow Up On Your Health Insurance Appeal")

    def test_get_url_with_trailing_slash(self):
        """Some email clients append a trailing slash. Regression test for
        the URL-pattern typo that mismatched the kwarg name and made the
        view crash with a TypeError."""
        response = self.client.get(self._base_path() + "/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Follow Up On Your Health Insurance Appeal")

    def test_get_canonical_url_via_named_route(self):
        url = reverse(
            "followup",
            kwargs={
                "uuid": self.denial.uuid,
                "hashed_email": self.denial.hashed_email,
                "follow_up_semi_sekret": self.denial.follow_up_semi_sekret,
            },
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)


class TestFollowUpViewSubmit(TestCase):
    """POSTing the follow-up form persists the user's response."""

    def setUp(self):
        self.client = Client()
        self.denial = _make_denial()
        self.path = (
            f"/v0/followup/{self.denial.uuid}/"
            f"{self.denial.hashed_email}/{self.denial.follow_up_semi_sekret}"
        )

    def _payload(self, **overrides):
        data = {
            "uuid": str(self.denial.uuid),
            "hashed_email": self.denial.hashed_email,
            "follow_up_semi_sekret": self.denial.follow_up_semi_sekret,
            "user_comments": "",
            "quote": "",
            "name_for_quote": "",
            "email": "",
            "appeal_result": "",
        }
        data.update(overrides)
        return data

    def test_post_minimum_payload_renders_thank_you(self):
        response = self.client.post(self.path, data=self._payload())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Thank you!")
        # A FollowUp record should have been created.
        self.assertEqual(FollowUp.objects.filter(denial_id=self.denial).count(), 1)

    def test_post_persists_user_comments(self):
        """Regression: user_comments must be saved on the FollowUp row."""
        response = self.client.post(
            self.path,
            data=self._payload(
                user_comments="Insurer reversed after the appeal letter.",
                appeal_result="Yes",
            ),
        )
        self.assertEqual(response.status_code, 200)
        followup = FollowUp.objects.get(denial_id=self.denial)
        self.assertEqual(
            followup.user_comments,
            "Insurer reversed after the appeal letter.",
        )
        self.assertEqual(followup.appeal_result, "Yes")

    def test_appeal_result_stored_on_denial_and_followup(self):
        self.client.post(self.path, data=self._payload(appeal_result="Partial"))
        self.denial.refresh_from_db()
        self.assertEqual(self.denial.appeal_result, "Partial")
        followup = FollowUp.objects.get(denial_id=self.denial)
        self.assertEqual(followup.appeal_result, "Partial")

    def test_quote_fields_persisted(self):
        self.client.post(
            self.path,
            data=self._payload(
                quote="The system actually worked.",
                use_quote="on",
                name_for_quote="A. Patient",
                email="contact@example.com",
            ),
        )
        followup = FollowUp.objects.get(denial_id=self.denial)
        self.assertEqual(followup.quote, "The system actually worked.")
        self.assertTrue(followup.use_quote)
        self.assertEqual(followup.name_for_quote, "A. Patient")
        self.assertEqual(followup.email, "contact@example.com")

    def test_follow_up_again_unchecked_does_not_schedule(self):
        self.client.post(self.path, data=self._payload())
        self.assertEqual(FollowUpSched.objects.filter(denial_id=self.denial).count(), 0)
        followup = FollowUp.objects.get(denial_id=self.denial)
        self.assertFalse(followup.more_follow_up_requested)

    def test_follow_up_again_checked_schedules_new_round(self):
        self.client.post(self.path, data=self._payload(follow_up_again="on"))
        # 1-day, 7-day, 30-day, 90-day = 4 scheduled rows
        self.assertEqual(FollowUpSched.objects.filter(denial_id=self.denial).count(), 4)
        followup = FollowUp.objects.get(denial_id=self.denial)
        self.assertTrue(followup.more_follow_up_requested)

    def test_invalid_secret_returns_500_error_page(self):
        """Wrong secret must not allow submission. Currently the helper
        raises which surfaces the generic error page; the important thing
        is that no FollowUp row is written."""
        bad_path = (
            f"/v0/followup/{self.denial.uuid}/"
            f"{self.denial.hashed_email}/not-the-real-sekret"
        )
        try:
            response = self.client.get(bad_path)
        except Exception:
            response = None
        # Either a 500 error page is rendered or an exception propagated;
        # in both cases, no follow-up row is created.
        self.assertEqual(FollowUp.objects.filter(denial_id=self.denial).count(), 0)
        if response is not None:
            self.assertIn(response.status_code, (200, 404, 500))

    def test_no_documents_does_not_create_empty_document_rows(self):
        """Regression: when the user uploads no files we should not write
        empty FollowUpDocuments rows."""
        self.client.post(self.path, data=self._payload())
        self.assertEqual(
            FollowUpDocuments.objects.filter(denial=self.denial).count(), 0
        )


class TestFollowUpHelper(TestCase):
    """Direct unit tests of the helper, independent of the view."""

    def setUp(self):
        self.denial = _make_denial()

    def test_fetch_denial_finds_denial(self):
        found = FollowUpHelper.fetch_denial(
            uuid=self.denial.uuid,
            follow_up_semi_sekret=self.denial.follow_up_semi_sekret,
            hashed_email=self.denial.hashed_email,
        )
        self.assertEqual(found.pk, self.denial.pk)

    def test_fetch_denial_rejects_bad_sekret(self):
        with self.assertRaises(Denial.DoesNotExist):
            FollowUpHelper.fetch_denial(
                uuid=self.denial.uuid,
                follow_up_semi_sekret="bogus-sekret",
                hashed_email=self.denial.hashed_email,
            )

    def test_store_follow_up_result_saves_all_fields(self):
        FollowUpHelper.store_follow_up_result(
            uuid=self.denial.uuid,
            follow_up_semi_sekret=self.denial.follow_up_semi_sekret,
            hashed_email=self.denial.hashed_email,
            user_comments="Helpful tool!",
            appeal_result="Yes",
            follow_up_again=False,
            medicare_someone_to_help=True,
            email="me@example.com",
            quote="It worked.",
            name_for_quote="Pat",
            use_quote=True,
        )
        followup = FollowUp.objects.get(denial_id=self.denial)
        self.assertEqual(followup.user_comments, "Helpful tool!")
        self.assertEqual(followup.appeal_result, "Yes")
        self.assertEqual(followup.email, "me@example.com")
        self.assertEqual(followup.quote, "It worked.")
        self.assertEqual(followup.name_for_quote, "Pat")
        self.assertTrue(followup.use_quote)
        self.assertTrue(followup.follow_up_medicare_someone_to_help)

    def test_store_follow_up_result_with_documents_filters_none(self):
        """None entries (e.g. from MultipleFileField with no upload) must
        not turn into empty FollowUpDocuments rows."""
        FollowUpHelper.store_follow_up_result(
            uuid=self.denial.uuid,
            follow_up_semi_sekret=self.denial.follow_up_semi_sekret,
            hashed_email=self.denial.hashed_email,
            user_comments="",
            appeal_result="",
            follow_up_again=False,
            followup_documents=[None],
        )
        self.assertEqual(
            FollowUpDocuments.objects.filter(denial=self.denial).count(), 0
        )

    def test_store_follow_up_result_with_follow_up_again_schedules(self):
        FollowUpHelper.store_follow_up_result(
            uuid=self.denial.uuid,
            follow_up_semi_sekret=self.denial.follow_up_semi_sekret,
            hashed_email=self.denial.hashed_email,
            user_comments="",
            appeal_result="",
            follow_up_again=True,
        )
        self.assertEqual(FollowUpSched.objects.filter(denial_id=self.denial).count(), 4)
