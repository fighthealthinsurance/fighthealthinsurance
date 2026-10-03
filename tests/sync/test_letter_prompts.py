"""The appeal prompt setting, the version a picked letter carries, the
comparison of people's choices by version, and the staff page."""

import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from fighthealthinsurance.common_view_logic import mark_proposal_chosen
from fighthealthinsurance.ml import appeal_prompt_versions as apv
from fighthealthinsurance.ml.appeal_prompt_stats import (
    MIN_MIXED_PICKS,
    compare_prompt_versions,
    wilson_interval,
)
from fighthealthinsurance.models import Denial, LetterPromptMode, ProposedAppeal

User = get_user_model()


def _denial(suffix="a"):
    return Denial.objects.create(
        hashed_email=f"hash-{suffix}",
        denial_text="denied",
        procedure="MRI",
        diagnosis="back pain",
        insurance_company="TestIns",
    )


class LetterPromptModeSettingTest(TestCase):
    def setUp(self):
        apv.reset_letter_prompt_mode_cache()

    def tearDown(self):
        apv.reset_letter_prompt_mode_cache()

    def test_no_rows_means_original(self):
        self.assertEqual(apv.current_letter_prompt_mode(), apv.MODE_ORIGINAL)

    def test_the_newest_row_wins(self):
        LetterPromptMode.objects.create(mode=apv.MODE_NEW)
        LetterPromptMode.objects.create(mode=apv.MODE_SPLIT)
        self.assertEqual(apv.current_letter_prompt_mode(), apv.MODE_SPLIT)

    def test_an_unknown_value_means_original(self):
        LetterPromptMode.objects.create(mode="sideways")
        self.assertEqual(apv.current_letter_prompt_mode(), apv.MODE_ORIGINAL)

    def test_a_read_is_kept_until_the_cache_is_reset(self):
        self.assertEqual(apv.current_letter_prompt_mode(), apv.MODE_ORIGINAL)
        LetterPromptMode.objects.create(mode=apv.MODE_NEW)
        self.assertEqual(apv.current_letter_prompt_mode(), apv.MODE_ORIGINAL)
        apv.reset_letter_prompt_mode_cache()
        self.assertEqual(apv.current_letter_prompt_mode(), apv.MODE_NEW)


class PickedLetterVersionTest(TestCase):
    def setUp(self):
        self.denial = _denial()

    def test_a_matched_pick_copies_the_drafts_version(self):
        ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="text-v2",
            model_name="model-x",
            prompt_version=apv.PROMPT_V2,
        )
        pa = mark_proposal_chosen(self.denial, "text-v2")
        self.assertEqual(pa.prompt_version, apv.PROMPT_V2)

    def test_an_unmatched_pick_has_no_version(self):
        ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="the only draft",
            model_name="model-x",
            prompt_version=apv.PROMPT_V1,
        )
        pa = mark_proposal_chosen(self.denial, "something the person wrote")
        self.assertIsNone(pa.prompt_version)


class ComparePromptVersionsTest(TestCase):
    def _draft(self, denial, version, model="model-x", text=None):
        return ProposedAppeal.objects.create(
            for_denial=denial,
            appeal_text=text or f"{model}-{version}-{ProposedAppeal.objects.count()}",
            model_name=model,
            prompt_version=version,
        )

    def _pick(self, denial, draft, shown, editted=False):
        return ProposedAppeal.objects.create(
            for_denial=denial,
            appeal_text=draft.appeal_text,
            chosen=True,
            editted=editted,
            model_name=draft.model_name,
            prompt_version=draft.prompt_version,
            presented_ids=[d.id for d in shown],
        )

    def test_counts_shown_and_picked_per_version(self):
        denial = _denial()
        a = self._draft(denial, apv.PROMPT_V1)
        b = self._draft(denial, apv.PROMPT_V2)
        self._pick(denial, b, [a, b], editted=True)
        result = compare_prompt_versions()
        v1, v2 = result.by_version
        self.assertEqual((v1.presented, v1.chosen), (1, 0))
        self.assertEqual((v2.presented, v2.chosen, v2.edited), (1, 1, 1))
        self.assertEqual(result.picks_considered, 1)

    def test_the_head_to_head_uses_only_pages_with_both_versions(self):
        mixed = _denial("mixed")
        one_version = _denial("single")
        a = self._draft(mixed, apv.PROMPT_V1)
        b = self._draft(mixed, apv.PROMPT_V2)
        c = self._draft(mixed, apv.PROMPT_V2, model="model-y")
        self._pick(mixed, a, [a, b, c])
        d = self._draft(one_version, apv.PROMPT_V2)
        self._pick(one_version, d, [d])
        h2h = compare_prompt_versions().head_to_head
        self.assertEqual(h2h.picks, 1)
        self.assertEqual((h2h.v1_chosen, h2h.v2_chosen), (1, 0))
        # Two of the three drafts on that page were v2.
        self.assertAlmostEqual(h2h.v2_expected, 2 / 3)
        self.assertFalse(h2h.enough)

    def test_a_pick_of_an_unversioned_letter_adds_no_expectation(self):
        denial = _denial()
        a = self._draft(denial, apv.PROMPT_V1)
        b = self._draft(denial, apv.PROMPT_V2)
        synthesized = ProposedAppeal.objects.create(
            for_denial=denial, appeal_text="combined", model_name="synthesized"
        )
        self._pick(denial, synthesized, [a, b, synthesized])
        h2h = compare_prompt_versions().head_to_head
        self.assertEqual((h2h.picks, h2h.other_chosen, h2h.v2_expected), (1, 1, 0.0))

    def test_picks_without_an_on_screen_report_are_left_out(self):
        denial = _denial()
        a = self._draft(denial, apv.PROMPT_V1)
        ProposedAppeal.objects.create(
            for_denial=denial,
            appeal_text=a.appeal_text,
            chosen=True,
            prompt_version=apv.PROMPT_V1,
        )
        result = compare_prompt_versions()
        self.assertEqual(result.picks_without_report, 1)
        self.assertEqual(result.picks_considered, 0)

    def test_a_pick_before_the_window_is_left_out(self):
        denial = _denial()
        a = self._draft(denial, apv.PROMPT_V1)
        pick = self._pick(denial, a, [a])
        ProposedAppeal.objects.filter(pk=pick.pk).update(
            created_at=timezone.now() - datetime.timedelta(days=40)
        )
        since = timezone.now() - datetime.timedelta(days=30)
        self.assertEqual(compare_prompt_versions(since).picks_considered, 0)

    def test_by_model_rows_split_each_model_by_version(self):
        denial = _denial()
        a = self._draft(denial, apv.PROMPT_V1, model="model-x")
        b = self._draft(denial, apv.PROMPT_V2, model="model-x")
        self._pick(denial, b, [a, b])
        rows = {(r.label, r.version): r for r in compare_prompt_versions().by_model}
        self.assertEqual(rows[("model-x", apv.PROMPT_V1)].chosen, 0)
        self.assertEqual(rows[("model-x", apv.PROMPT_V2)].chosen, 1)


def test_wilson_interval_brackets_the_rate():
    low, high = wilson_interval(30, 100)
    assert low < 0.30 < high
    assert wilson_interval(0, 0) is None


def test_the_head_to_head_threshold_is_meaningful():
    assert MIN_MIXED_PICKS >= 100


class LetterPromptsPageTest(TestCase):
    def setUp(self):
        apv.reset_letter_prompt_mode_cache()
        self.url = reverse("letter_prompts")

    def tearDown(self):
        apv.reset_letter_prompt_mode_cache()

    def _staff(self):
        user = User.objects.create_user(
            username="staffer", password="pw123", is_staff=True
        )
        self.client.force_login(user)
        return user

    def test_anonymous_visitors_are_sent_to_log_in(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)

    def test_people_who_are_not_staff_cannot_change_it(self):
        user = User.objects.create_user(username="visitor", password="pw123")
        self.client.force_login(user)
        response = self.client.post(self.url, {"mode": apv.MODE_NEW})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(LetterPromptMode.objects.exists())

    def test_staff_see_the_original_prompt_until_someone_changes_it(self):
        self._staff()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Never changed")
        self.assertContains(response, apv.OUTPUT_CONTRACT)

    def test_saving_a_mode_records_who_and_takes_effect(self):
        user = self._staff()
        response = self.client.post(
            self.url, {"mode": apv.MODE_SPLIT, "note": "start the test"}
        )
        self.assertEqual(response.status_code, 302)
        row = LetterPromptMode.objects.get()
        self.assertEqual(
            (row.mode, row.changed_by, row.changed_by_username, row.note),
            (apv.MODE_SPLIT, user, "staffer", "start the test"),
        )
        self.assertEqual(apv.current_letter_prompt_mode(), apv.MODE_SPLIT)

    def test_an_unknown_mode_is_refused(self):
        self._staff()
        response = self.client.post(self.url, {"mode": "everything"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(LetterPromptMode.objects.exists())

    def test_the_page_compares_from_when_half_and_half_began(self):
        self._staff()
        LetterPromptMode.objects.create(mode=apv.MODE_SPLIT)
        response = self.client.get(self.url)
        self.assertEqual(response.context["period"], "split")
        self.assertIsNotNone(response.context["comparison"])
        self.assertContains(response, "Pages that showed both versions")

    def test_asking_for_the_split_period_before_any_split_shows_nothing_to_compare(
        self,
    ):
        self._staff()
        response = self.client.get(self.url, {"period": "split"})
        self.assertIsNone(response.context["comparison"])
        self.assertContains(response, "Half and half hasn't run yet")
