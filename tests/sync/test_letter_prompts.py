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
    head_to_head,
)
from fighthealthinsurance.models import Denial, LetterPromptMode, ProposedAppeal
from fighthealthinsurance.staff_views import ModelUsageDashboardView

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

    def test_the_new_modes_are_read_back(self):
        for mode in (apv.MODE_SECTIONED, apv.MODE_THIRDS):
            with self.subTest(mode=mode):
                LetterPromptMode.objects.create(mode=mode)
                apv.reset_letter_prompt_mode_cache()
                self.assertEqual(apv.current_letter_prompt_mode(), mode)

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


def _pair(pairs, first, second):
    (h2h,) = [h for h in pairs if (h.first, h.second) == (first, second)]
    return h2h


class HeadToHeadTest(TestCase):
    def _draft(self, denial, version, model="model-x"):
        return ProposedAppeal.objects.create(
            for_denial=denial,
            appeal_text=f"{model}-{version}-{ProposedAppeal.objects.count()}",
            model_name=model,
            prompt_version=version,
        )

    def _pick(self, denial, draft, shown):
        return ProposedAppeal.objects.create(
            for_denial=denial,
            appeal_text=draft.appeal_text,
            chosen=True,
            model_name=draft.model_name,
            prompt_version=draft.prompt_version,
            presented_ids=[d.id for d in shown],
        )

    def test_only_pages_with_both_versions_count(self):
        mixed = _denial("mixed")
        one_version = _denial("single")
        a = self._draft(mixed, apv.PROMPT_V1)
        b = self._draft(mixed, apv.PROMPT_V2)
        c = self._draft(mixed, apv.PROMPT_V2, model="model-y")
        self._pick(mixed, a, [a, b, c])
        d = self._draft(one_version, apv.PROMPT_V2)
        self._pick(one_version, d, [d])
        h2h = _pair(head_to_head(), apv.PROMPT_V1, apv.PROMPT_V2)
        self.assertEqual(h2h.picks, 1)
        self.assertEqual((h2h.first_chosen, h2h.second_chosen), (1, 0))
        # Two of the three drafts on that page were v2.
        self.assertAlmostEqual(h2h.second_expected, 2 / 3)
        self.assertFalse(h2h.enough)

    def test_a_pick_of_an_unversioned_letter_adds_no_expectation(self):
        denial = _denial()
        a = self._draft(denial, apv.PROMPT_V1)
        b = self._draft(denial, apv.PROMPT_V2)
        synthesized = ProposedAppeal.objects.create(
            for_denial=denial, appeal_text="combined", model_name="synthesized"
        )
        self._pick(denial, synthesized, [a, b, synthesized])
        h2h = _pair(head_to_head(), apv.PROMPT_V1, apv.PROMPT_V2)
        self.assertEqual(
            (h2h.picks, h2h.other_chosen, h2h.second_expected), (1, 1, 0.0)
        )

    def test_a_pick_before_the_window_is_left_out(self):
        denial = _denial()
        a = self._draft(denial, apv.PROMPT_V1)
        b = self._draft(denial, apv.PROMPT_V2)
        pick = self._pick(denial, a, [a, b])
        ProposedAppeal.objects.filter(pk=pick.pk).update(
            created_at=timezone.now() - datetime.timedelta(days=40)
        )
        since = timezone.now() - datetime.timedelta(days=30)
        self.assertEqual(
            _pair(head_to_head(since), apv.PROMPT_V1, apv.PROMPT_V2).picks, 0
        )

    def test_every_pair_of_versions_is_reported(self):
        self.assertEqual(
            [(h.first, h.second) for h in head_to_head()],
            [
                (apv.PROMPT_V1, apv.PROMPT_V2),
                (apv.PROMPT_V1, apv.PROMPT_V3),
                (apv.PROMPT_V2, apv.PROMPT_V3),
            ],
        )

    def test_a_page_with_all_three_versions_counts_for_each_pair(self):
        denial = _denial()
        a = self._draft(denial, apv.PROMPT_V1)
        b = self._draft(denial, apv.PROMPT_V2)
        c = self._draft(denial, apv.PROMPT_V3)
        self._pick(denial, c, [a, b, c])
        pairs = head_to_head()
        v1_v2 = _pair(pairs, apv.PROMPT_V1, apv.PROMPT_V2)
        # A v3 pick says nothing about v1 against v2.
        self.assertEqual(
            (v1_v2.picks, v1_v2.other_chosen, v1_v2.second_expected), (1, 1, 0.0)
        )
        for first in (apv.PROMPT_V1, apv.PROMPT_V2):
            h2h = _pair(pairs, first, apv.PROMPT_V3)
            self.assertEqual(
                (h2h.picks, h2h.first_chosen, h2h.second_chosen, h2h.other_chosen),
                (1, 0, 1, 0),
            )
            # One draft of each of the two: blind chance is a half.
            self.assertAlmostEqual(h2h.second_expected, 0.5)

    def test_a_third_version_on_the_page_leaves_the_pairs_numbers_alone(self):
        # The v1-v2 numbers for a page are what they were before v3 existed:
        # the v3 draft beside them is not part of that pair's chance.
        denial = _denial()
        a = self._draft(denial, apv.PROMPT_V1)
        b = self._draft(denial, apv.PROMPT_V2)
        c = self._draft(denial, apv.PROMPT_V2, model="model-y")
        d = self._draft(denial, apv.PROMPT_V3)
        self._pick(denial, a, [a, b, c, d])
        v1_v2 = _pair(head_to_head(), apv.PROMPT_V1, apv.PROMPT_V2)
        self.assertEqual((v1_v2.picks, v1_v2.first_chosen), (1, 1))
        self.assertAlmostEqual(v1_v2.second_expected, 2 / 3)
        v1_v3 = _pair(head_to_head(), apv.PROMPT_V1, apv.PROMPT_V3)
        self.assertEqual((v1_v3.first_chosen, v1_v3.second_chosen), (1, 0))
        # One v1 and one v3 on the page.
        self.assertAlmostEqual(v1_v3.second_expected, 0.5)


class DashboardPromptVersionTableTest(TestCase):
    """The per-window tables count like the model table: per draft shown."""

    def test_counts_shown_and_picked_per_version_and_model(self):
        denial = _denial()
        a = ProposedAppeal.objects.create(
            for_denial=denial,
            appeal_text="a",
            model_name="model-x",
            prompt_version=apv.PROMPT_V1,
        )
        b = ProposedAppeal.objects.create(
            for_denial=denial,
            appeal_text="b",
            model_name="model-x",
            prompt_version=apv.PROMPT_V2,
        )
        ProposedAppeal.objects.create(
            for_denial=denial,
            appeal_text="b",
            chosen=True,
            model_name="model-x",
            prompt_version=apv.PROMPT_V2,
            presented_ids=[a.id, b.id],
        )
        stats = ModelUsageDashboardView._prompt_version_stats(None)
        versions = {
            r["model_name"]: (r["chosen"], r["presented"]) for r in stats["versions"]
        }
        self.assertEqual(versions, {apv.PROMPT_V1: (0, 1), apv.PROMPT_V2: (1, 1)})
        models = {r["model_name"]: r["chosen"] for r in stats["models"]}
        self.assertEqual(models["model-x · v2"], 1)

    def test_v3_drafts_get_their_own_rows(self):
        denial = _denial()
        a = ProposedAppeal.objects.create(
            for_denial=denial,
            appeal_text="a",
            model_name="model-x",
            prompt_version=apv.PROMPT_V2,
        )
        b = ProposedAppeal.objects.create(
            for_denial=denial,
            appeal_text="b",
            model_name="model-y",
            prompt_version=apv.PROMPT_V3,
        )
        ProposedAppeal.objects.create(
            for_denial=denial,
            appeal_text="b",
            chosen=True,
            model_name="model-y",
            prompt_version=apv.PROMPT_V3,
            presented_ids=[a.id, b.id],
        )
        stats = ModelUsageDashboardView._prompt_version_stats(None)
        rows = {r["model_name"]: r for r in stats["versions"]}
        self.assertEqual(
            (rows[apv.PROMPT_V3]["chosen"], rows[apv.PROMPT_V3]["presented"]), (1, 1)
        )
        self.assertEqual(
            rows[apv.PROMPT_V3]["label"],
            "v3: sectioned prompt plus the output contract",
        )
        models = {r["model_name"]: r["chosen"] for r in stats["models"]}
        self.assertEqual(models["model-y · v3"], 1)


class DashboardPromptSwitchTest(TestCase):
    def setUp(self):
        apv.reset_letter_prompt_mode_cache()
        self.url = reverse("model_usage_dashboard")

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
        self.assertContains(response, "Appeal prompt versions")
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

    def test_the_new_modes_can_be_saved(self):
        self._staff()
        for mode in (apv.MODE_SECTIONED, apv.MODE_THIRDS):
            with self.subTest(mode=mode):
                response = self.client.post(self.url, {"mode": mode})
                self.assertEqual(response.status_code, 302)
                self.assertEqual(LetterPromptMode.objects.first().mode, mode)
                self.assertEqual(apv.current_letter_prompt_mode(), mode)

    def test_the_switch_offers_every_mode(self):
        self._staff()
        response = self.client.get(self.url)
        for mode, label in apv.MODE_CHOICES:
            with self.subTest(mode=mode):
                self.assertContains(response, f'value="{mode}"')
                self.assertContains(response, label)

    def test_an_unknown_mode_is_refused(self):
        self._staff()
        response = self.client.post(self.url, {"mode": "everything"})
        self.assertEqual(response.status_code, 400)
        self.assertContains(
            response,
            "Choose original, new, half and half, sectioned or thirds.",
            status_code=400,
        )
        self.assertFalse(LetterPromptMode.objects.exists())

    def test_the_head_to_head_starts_when_half_and_half_began(self):
        self._staff()
        LetterPromptMode.objects.create(mode=apv.MODE_SPLIT)
        response = self.client.get(self.url)
        self.assertIsNotNone(response.context["letter_prompts"]["head_to_head"])
        self.assertContains(response, "Pages that showed both versions")

    def _shown_pairs(self, response):
        return [
            (h.first, h.second)
            for h in response.context["letter_prompts"]["head_to_head"]
        ]

    def test_half_and_half_shows_only_the_v1_v2_pair(self):
        self._staff()
        LetterPromptMode.objects.create(mode=apv.MODE_SPLIT)
        response = self.client.get(self.url)
        self.assertEqual(self._shown_pairs(response), [(apv.PROMPT_V1, apv.PROMPT_V2)])

    def test_thirds_shows_every_pair(self):
        self._staff()
        LetterPromptMode.objects.create(mode=apv.MODE_THIRDS)
        response = self.client.get(self.url)
        self.assertEqual(
            self._shown_pairs(response),
            [
                (apv.PROMPT_V1, apv.PROMPT_V2),
                (apv.PROMPT_V1, apv.PROMPT_V3),
                (apv.PROMPT_V2, apv.PROMPT_V3),
            ],
        )
        self.assertContains(response, "v2 and v3")
        self.assertContains(response, "v3 picked ÷ chance")

    def test_moving_from_half_and_half_to_thirds_keeps_the_run(self):
        self._staff()
        split = LetterPromptMode.objects.create(mode=apv.MODE_SPLIT)
        LetterPromptMode.objects.create(mode=apv.MODE_THIRDS)
        response = self.client.get(self.url)
        self.assertEqual(
            response.context["letter_prompts"]["split_started"], split.created_at
        )

    def test_v3_for_every_letter_has_no_head_to_head(self):
        self._staff()
        LetterPromptMode.objects.create(mode=apv.MODE_SECTIONED)
        response = self.client.get(self.url)
        self.assertIsNone(response.context["letter_prompts"]["head_to_head"])

    def test_without_half_and_half_there_is_no_head_to_head(self):
        self._staff()
        LetterPromptMode.objects.create(mode=apv.MODE_NEW)
        response = self.client.get(self.url)
        self.assertIsNone(response.context["letter_prompts"]["head_to_head"])
        self.assertContains(response, "no head-to-head yet")
