"""Tests for the ML Model Usage staff dashboard."""

import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from fighthealthinsurance.models import (
    ChooserCandidate,
    ChooserTask,
    ChooserVote,
    Denial,
    ProposedAppeal,
)
from fighthealthinsurance.staff_views import UNKNOWN_MODEL_LABEL, _merge_stats

User = get_user_model()


class MergeStatsTest(TestCase):
    def test_merge_computes_win_rate(self):
        rows = _merge_stats({"a": 10, "b": 8}, {"a": 20, "b": 10})
        # Sorted by -chosen then -win_rate
        self.assertEqual(rows[0]["model_name"], "a")
        self.assertEqual(rows[0]["chosen"], 10)
        self.assertEqual(rows[0]["presented"], 20)
        self.assertAlmostEqual(rows[0]["win_rate"], 50.0)
        self.assertEqual(rows[1]["model_name"], "b")
        self.assertAlmostEqual(rows[1]["win_rate"], 80.0)

    def test_merge_handles_zero_presented(self):
        rows = _merge_stats({"a": 5}, {})
        self.assertEqual(rows[0]["chosen"], 5)
        self.assertEqual(rows[0]["presented"], 0)
        self.assertEqual(rows[0]["win_rate"], 0.0)

    def test_merge_includes_presented_only_models(self):
        rows = _merge_stats({"a": 5}, {"a": 10, "b": 2})
        self.assertEqual({r["model_name"] for r in rows}, {"a", "b"})


class ModelUsageDashboardAccessTest(TestCase):
    def test_non_staff_redirected(self):
        response = self.client.get(reverse("model_usage_dashboard"))
        # staff_member_required redirects to login
        self.assertEqual(response.status_code, 302)

    def test_staff_user_gets_200(self):
        User.objects.create_user(username="staff", password="pw123", is_staff=True)
        self.client.login(username="staff", password="pw123")
        response = self.client.get(reverse("model_usage_dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ML Model Usage Dashboard")


class ModelUsageDashboardContentTest(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="staff", password="pw123", is_staff=True
        )
        self.client.login(username="staff", password="pw123")
        self.denial = Denial.objects.create(
            hashed_email="hash",
            denial_text="denied",
            procedure="MRI",
            diagnosis="back pain",
            insurance_company="TestIns",
        )

    def _make_proposed(self, model_name, chosen, days_ago=0, denial=None):
        pa = ProposedAppeal.objects.create(
            for_denial=denial or self.denial,
            appeal_text=f"appeal-{model_name}-{chosen}-{days_ago}",
            chosen=chosen,
            model_name=model_name,
        )
        if days_ago > 0:
            # Backdate created_at via update() to bypass auto_now_add.
            ProposedAppeal.objects.filter(pk=pa.pk).update(
                created_at=timezone.now() - datetime.timedelta(days=days_ago)
            )
        return pa

    def _make_denial(self, suffix):
        return Denial.objects.create(
            hashed_email=f"hash-{suffix}",
            denial_text="denied",
            procedure="MRI",
            diagnosis="back pain",
            insurance_company="TestIns",
        )

    def test_proposed_appeal_window_filtering(self):
        # Use distinct denials so the window definition
        # ("denials picked within the window") isolates fresh and old.
        d_fresh = self.denial
        d_old = self._make_denial("old")

        # Model "fresh" picked today (within 1d and 30d).
        self._make_proposed("fresh", chosen=False, days_ago=0, denial=d_fresh)
        self._make_proposed("fresh", chosen=True, days_ago=0, denial=d_fresh)
        # Model "old" picked 45 days ago (only in global).
        self._make_proposed("old", chosen=False, days_ago=45, denial=d_old)
        self._make_proposed("old", chosen=True, days_ago=45, denial=d_old)

        response = self.client.get(reverse("model_usage_dashboard"))
        windows = response.context["windows"]
        by_slug = {w["slug"]: w for w in windows}

        global_models = {r["model_name"] for r in by_slug["global"]["proposed_appeal"]}
        day_models = {r["model_name"] for r in by_slug["1d"]["proposed_appeal"]}
        month_models = {r["model_name"] for r in by_slug["30d"]["proposed_appeal"]}
        self.assertIn("fresh", global_models)
        self.assertIn("old", global_models)
        self.assertIn("fresh", day_models)
        self.assertNotIn("old", day_models)
        self.assertIn("fresh", month_models)
        self.assertNotIn("old", month_models)

    def test_presented_counts_drafts_generated_before_window(self):
        # Regression for the "1-day window" review concern: a draft generated
        # 45 days ago but picked today should still count as presented in
        # the 1-day window, because the window is anchored on the pick.
        denial = self.denial
        old_draft = self._make_proposed(
            "old-draft-model", chosen=False, days_ago=45, denial=denial
        )
        # Pick is "today" - the chosen row is in window.
        self._make_proposed("old-draft-model", chosen=True, days_ago=0, denial=denial)

        response = self.client.get(reverse("model_usage_dashboard"))
        day_rows = response.context["windows"][1]["proposed_appeal"]
        row = next(r for r in day_rows if r["model_name"] == "old-draft-model")
        self.assertEqual(row["chosen"], 1)
        self.assertEqual(row["presented"], 1)
        self.assertAlmostEqual(row["win_rate"], 100.0)

    def test_proposed_appeal_excludes_abandoned_from_presented(self):
        # Denial #1 has a chosen appeal => counts toward presented.
        d1 = self.denial
        ProposedAppeal.objects.create(
            for_denial=d1,
            appeal_text="d1-a",
            chosen=False,
            model_name="m1",
        )
        ProposedAppeal.objects.create(
            for_denial=d1, appeal_text="d1-a", chosen=True, model_name="m1"
        )
        # Denial #2 has only generated rows, no chosen => abandoned.
        d2 = Denial.objects.create(
            hashed_email="hash2",
            denial_text="denied",
            procedure="MRI",
            diagnosis="back pain",
            insurance_company="TestIns",
        )
        ProposedAppeal.objects.create(
            for_denial=d2, appeal_text="d2-a", chosen=False, model_name="m1"
        )

        response = self.client.get(reverse("model_usage_dashboard"))
        rows = response.context["windows"][0]["proposed_appeal"]
        m1 = next(r for r in rows if r["model_name"] == "m1")
        # Only the d1 presented row counts; d2 abandoned doesn't.
        self.assertEqual(m1["chosen"], 1)
        self.assertEqual(m1["presented"], 1)

    def test_chooser_vote_aggregation(self):
        task = ChooserTask.objects.create(
            task_type="appeal", status="EXHAUSTED", source="synthetic"
        )
        cand_a = ChooserCandidate.objects.create(
            task=task,
            candidate_index=0,
            kind="appeal_letter",
            model_name="model-a",
            content="A",
        )
        cand_b = ChooserCandidate.objects.create(
            task=task,
            candidate_index=1,
            kind="appeal_letter",
            model_name="model-b",
            content="B",
        )
        ChooserVote.objects.create(
            task=task,
            chosen_candidate=cand_a,
            presented_candidate_ids=[cand_a.id, cand_b.id],
            session_key="sess1",
        )

        response = self.client.get(reverse("model_usage_dashboard"))
        rows = response.context["windows"][0]["chooser_appeal"]
        by_name = {r["model_name"]: r for r in rows}
        self.assertEqual(by_name["model-a"]["chosen"], 1)
        self.assertEqual(by_name["model-a"]["presented"], 1)
        self.assertEqual(by_name["model-b"]["chosen"], 0)
        self.assertEqual(by_name["model-b"]["presented"], 1)
        # Win rate
        self.assertAlmostEqual(by_name["model-a"]["win_rate"], 100.0)
        self.assertAlmostEqual(by_name["model-b"]["win_rate"], 0.0)

    def test_chooser_vote_filters_kind(self):
        # An appeal_letter vote should NOT appear in chat_response stats.
        task = ChooserTask.objects.create(
            task_type="appeal", status="EXHAUSTED", source="synthetic"
        )
        cand = ChooserCandidate.objects.create(
            task=task,
            candidate_index=0,
            kind="appeal_letter",
            model_name="appeal-only",
            content="A",
        )
        ChooserVote.objects.create(
            task=task,
            chosen_candidate=cand,
            presented_candidate_ids=[cand.id],
            session_key="sess1",
        )

        response = self.client.get(reverse("model_usage_dashboard"))
        chat_rows = response.context["windows"][0]["chooser_chat"]
        self.assertEqual(chat_rows, [])

    def test_proposed_appeal_chosen_with_null_model_name_bucketed_as_unknown(self):
        # mark_proposal_chosen falls back to model_name=None when the
        # picked text doesn't match any generated draft. Those picks
        # are still real signal — they must surface as "(unknown)"
        # instead of being silently dropped from the dashboard.
        d = self.denial
        ProposedAppeal.objects.create(
            for_denial=d,
            appeal_text="generated by m1",
            chosen=False,
            model_name="m1",
        )
        # A pick where the model couldn't be attributed (heavily-edited text).
        ProposedAppeal.objects.create(
            for_denial=d,
            appeal_text="heavily edited",
            chosen=True,
            model_name=None,
        )
        # A pick that IS attributable.
        ProposedAppeal.objects.create(
            for_denial=d,
            appeal_text="generated by m1",
            chosen=True,
            model_name="m1",
        )

        response = self.client.get(reverse("model_usage_dashboard"))
        rows = response.context["windows"][0]["proposed_appeal"]
        by_name = {r["model_name"]: r for r in rows}
        # Both the attributed pick and the unknown pick show up.
        self.assertIn("m1", by_name)
        self.assertIn(UNKNOWN_MODEL_LABEL, by_name)
        self.assertEqual(by_name["m1"]["chosen"], 1)
        self.assertEqual(by_name[UNKNOWN_MODEL_LABEL]["chosen"], 1)
        # Unknown has no presented count (we don't know which model
        # generated the draft), so win_rate is 0.
        self.assertEqual(by_name[UNKNOWN_MODEL_LABEL]["presented"], 0)
        self.assertEqual(by_name[UNKNOWN_MODEL_LABEL]["win_rate"], 0.0)
