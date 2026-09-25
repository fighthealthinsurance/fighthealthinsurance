"""Tests for the ML Model Usage staff dashboard."""

import datetime
import json
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from fighthealthinsurance.ml import model_health_check as mhc
from fighthealthinsurance.ml.model_identity import (
    LEGACY_UNATTRIBUTED_LABEL,
    SYNTHESIZED_MODEL_NAME,
    legacy_unresolved_label,
)
from fighthealthinsurance.models import (
    ChooserCandidate,
    ChooserSkip,
    ChooserTask,
    ChooserVote,
    Denial,
    ModelBackendHealthCheckResult,
    ModelCallAttempt,
    ProposedAppeal,
)
from fighthealthinsurance.staff_views import (
    NO_CONTEXT_LEVEL_LABEL,
    SHARED_APPEAL_LABEL,
    TEMPLATE_PICK_LABEL,
    UNKNOWN_MODEL_LABEL,
    ModelUsageDashboardView,
    _merge_stats,
    _model_states,
)

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
        # No denominator: win_rate must be None (rendered as an em dash),
        # never a misleading 0.0%.
        rows = _merge_stats({"a": 5}, {})
        self.assertEqual(rows[0]["chosen"], 5)
        self.assertEqual(rows[0]["presented"], 0)
        self.assertIsNone(rows[0]["win_rate"])

    def test_merge_zero_chosen_with_presented_is_zero_percent(self):
        # A real denominator with zero wins IS a genuine 0.0%.
        rows = _merge_stats({}, {"a": 4})
        self.assertEqual(rows[0]["chosen"], 0)
        self.assertEqual(rows[0]["presented"], 4)
        self.assertAlmostEqual(rows[0]["win_rate"], 0.0)

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

    def test_context_level_stats_bucket_by_level(self):
        # The dashboard buckets chosen/presented by the shed level the appeal
        # was generated at (rows key the level under "model_name" for the
        # shared table partial).
        d = self.denial
        ProposedAppeal.objects.create(
            for_denial=d,
            appeal_text="full-draft",
            chosen=False,
            model_name="m1",
            context_level="full",
        )
        ProposedAppeal.objects.create(
            for_denial=d,
            appeal_text="full-chosen",
            chosen=True,
            model_name="m1",
            context_level="full",
        )
        ProposedAppeal.objects.create(
            for_denial=d,
            appeal_text="shed-draft",
            chosen=False,
            model_name="m1",
            context_level="tier1_shed",
        )

        response = self.client.get(reverse("model_usage_dashboard"))
        rows = response.context["windows"][0]["context_level"]
        by_level = {r["model_name"]: r for r in rows}
        self.assertEqual(by_level["full"]["chosen"], 1)
        self.assertEqual(by_level["full"]["presented"], 1)
        self.assertEqual(by_level["tier1_shed"]["chosen"], 0)
        self.assertEqual(by_level["tier1_shed"]["presented"], 1)

    def test_context_level_stats_excludes_unpromoted_speculative(self):
        # A held-back speculative draft (never chosen) must not appear as a
        # presented context level -- it was reserved, not shown.
        d = self.denial
        ProposedAppeal.objects.create(
            for_denial=d,
            appeal_text="full-chosen",
            chosen=True,
            model_name="m1",
            context_level="full",
        )
        ProposedAppeal.objects.create(
            for_denial=d,
            appeal_text="spec-draft",
            chosen=False,
            model_name="spec",
            context_level="speculative",
            speculative=True,
        )
        response = self.client.get(reverse("model_usage_dashboard"))
        rows = response.context["windows"][0]["context_level"]
        levels = {r["model_name"] for r in rows}
        self.assertNotIn("speculative", levels)

    def test_proposed_appeal_stats_excludes_unpromoted_speculative(self):
        # A held-back speculative draft carries a real internal model_name (the
        # same models the live path uses), so if it isn't excluded it silently
        # pads that model's presented denominator on the main model-usage table.
        d = self.denial
        # A live draft by m1 that was presented and chosen.
        ProposedAppeal.objects.create(
            for_denial=d, appeal_text="live-draft", chosen=False, model_name="m1"
        )
        ProposedAppeal.objects.create(
            for_denial=d, appeal_text="live-draft", chosen=True, model_name="m1"
        )
        # A held-back speculative draft ALSO attributed to m1: reserved, never
        # shown -> must NOT count toward m1's presented total.
        ProposedAppeal.objects.create(
            for_denial=d,
            appeal_text="held-spec",
            chosen=False,
            model_name="m1",
            context_level="speculative",
            speculative=True,
        )
        response = self.client.get(reverse("model_usage_dashboard"))
        rows = response.context["windows"][0]["proposed_appeal"]
        row = next(r for r in rows if r["model_name"] == "m1")
        # Presented counts only the one live draft, not the held-back reserve.
        self.assertEqual(row["presented"], 1)
        self.assertEqual(row["chosen"], 1)

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
        # Both the attributed pick and the unattributed pick show up.
        self.assertIn("m1", by_name)
        self.assertIn(UNKNOWN_MODEL_LABEL, by_name)
        self.assertEqual(by_name["m1"]["chosen"], 1)
        self.assertEqual(by_name[UNKNOWN_MODEL_LABEL]["chosen"], 1)
        # Unattributed has no presented count (we don't know which model
        # generated the draft), so win_rate is undefined - None, not 0%.
        self.assertEqual(by_name[UNKNOWN_MODEL_LABEL]["presented"], 0)
        self.assertIsNone(by_name[UNKNOWN_MODEL_LABEL]["win_rate"])


class ChooserStatsHelperMixin:
    """Shared builders for chooser tasks/candidates/votes in dashboard tests."""

    def _make_task(self, task_type="appeal"):
        return ChooserTask.objects.create(
            task_type=task_type, status="EXHAUSTED", source="synthetic"
        )

    def _make_candidate(self, task, index, model_name, kind="appeal_letter", **kwargs):
        return ChooserCandidate.objects.create(
            task=task,
            candidate_index=index,
            kind=kind,
            model_name=model_name,
            content=f"content-{index}",
            **kwargs,
        )

    def _vote(self, task, chosen, presented, session="sess1"):
        return ChooserVote.objects.create(
            task=task,
            chosen_candidate=chosen,
            presented_candidate_ids=[c.id for c in presented],
            session_key=session,
        )


class ModelUsageDashboardNormalizationTest(ChooserStatsHelperMixin, TestCase):
    """Object-repr model names must never leak into dashboard keys."""

    REPR_A1 = "<fighthealthinsurance.ml.ml_models.DeepInfra object at 0x7f81456da840>"
    REPR_A2 = "<fighthealthinsurance.ml.ml_models.DeepInfra object at 0x7deadbeef000>"
    REPR_B = (
        "<fighthealthinsurance.ml.ml_models.NewRemoteInternal object at 0x7d65196f8bc0>"
    )

    def setUp(self):
        self.staff = User.objects.create_user(
            username="staff", password="pw123", is_staff=True
        )
        self.client.login(username="staff", password="pw123")

    def _get_windows(self):
        response = self.client.get(reverse("model_usage_dashboard"))
        return response, response.context["windows"]

    def test_no_dashboard_key_contains_object_repr(self):
        task = self._make_task()
        c1 = self._make_candidate(task, 0, self.REPR_A1)
        c2 = self._make_candidate(task, 1, self.REPR_B)
        self._vote(task, c1, [c1, c2])
        # A chat vote with a repr-named candidate too.
        chat_task = self._make_task("chat")
        c3 = self._make_candidate(chat_task, 0, self.REPR_A2, kind="chat_response")
        self._vote(chat_task, c3, [c3])

        response, windows = self._get_windows()
        for w in windows:
            for source in ("proposed_appeal", "chooser_appeal", "chooser_chat"):
                for row in w[source]:
                    self.assertNotIn("object at 0x", row["model_name"])
            self.assertNotIn("object at 0x", w["chart_data_json"])
        self.assertNotContains(response, "object at 0x")

    def test_same_class_repr_instances_aggregate_into_one_row(self):
        # Two different memory addresses of the same class - historically two
        # separate dashboard rows - must aggregate under one class-level
        # legacy label.
        task1 = self._make_task()
        c1 = self._make_candidate(task1, 0, self.REPR_A1)
        c2 = self._make_candidate(task1, 1, "clean-model")
        self._vote(task1, c1, [c1, c2])
        task2 = self._make_task()
        c3 = self._make_candidate(task2, 0, self.REPR_A2)
        c4 = self._make_candidate(task2, 1, "clean-model")
        self._vote(task2, c3, [c3, c4], session="sess2")

        _, windows = self._get_windows()
        rows = windows[0]["chooser_appeal"]
        by_name = {r["model_name"]: r for r in rows}
        label = legacy_unresolved_label("DeepInfra")
        self.assertIn(label, by_name)
        self.assertEqual(by_name[label]["chosen"], 2)
        self.assertEqual(by_name[label]["presented"], 2)
        # And the two addresses did not create separate rows.
        self.assertEqual(
            len([n for n in by_name if "DeepInfra" in n]), 1, by_name.keys()
        )


class ModelUsageDashboardSemanticsTest(ChooserStatsHelperMixin, TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="staff", password="pw123", is_staff=True
        )
        self.client.login(username="staff", password="pw123")

    def _rows(self, source="chooser_appeal", window=0):
        response = self.client.get(reverse("model_usage_dashboard"))
        return response.context["windows"][window][source]

    def test_presented_and_chosen_candidate_counts_once_each(self):
        # One vote across three candidates: every candidate presented once,
        # only the selected one chosen; selected contributes to both.
        task = self._make_task()
        a = self._make_candidate(task, 0, "model-a")
        b = self._make_candidate(task, 1, "model-b")
        c = self._make_candidate(task, 2, "model-c")
        self._vote(task, a, [a, b, c])

        by_name = {r["model_name"]: r for r in self._rows()}
        self.assertEqual(by_name["model-a"]["presented"], 1)
        self.assertEqual(by_name["model-a"]["chosen"], 1)
        self.assertAlmostEqual(by_name["model-a"]["win_rate"], 100.0)
        for loser in ("model-b", "model-c"):
            self.assertEqual(by_name[loser]["presented"], 1)
            self.assertEqual(by_name[loser]["chosen"], 0)
            self.assertAlmostEqual(by_name[loser]["win_rate"], 0.0)

    def test_duplicate_presented_ids_counted_once(self):
        # A historical/hostile vote row with duplicated presented ids must
        # not inflate the denominator.
        task = self._make_task()
        a = self._make_candidate(task, 0, "model-a")
        b = self._make_candidate(task, 1, "model-b")
        ChooserVote.objects.create(
            task=task,
            chosen_candidate=a,
            presented_candidate_ids=[a.id, a.id, b.id, b.id, b.id],
            session_key="sess1",
        )
        by_name = {r["model_name"]: r for r in self._rows()}
        self.assertEqual(by_name["model-a"]["presented"], 1)
        self.assertEqual(by_name["model-b"]["presented"], 1)

    def test_multiple_chosen_rows_do_not_duplicate_presented(self):
        # Two picks on the same denial (re-submit): drafts still count once.
        denial = Denial.objects.create(
            hashed_email="hash",
            denial_text="denied",
            procedure="MRI",
            diagnosis="back pain",
            insurance_company="TestIns",
        )
        ProposedAppeal.objects.create(
            for_denial=denial, appeal_text="draft", chosen=False, model_name="m1"
        )
        for _ in range(2):
            ProposedAppeal.objects.create(
                for_denial=denial, appeal_text="draft", chosen=True, model_name="m1"
            )
        rows = self._rows(source="proposed_appeal")
        m1 = next(r for r in rows if r["model_name"] == "m1")
        self.assertEqual(m1["presented"], 1)
        self.assertEqual(m1["chosen"], 2)

    def test_zero_denominator_renders_em_dash(self):
        denial = Denial.objects.create(
            hashed_email="hash",
            denial_text="denied",
            procedure="MRI",
            diagnosis="back pain",
            insurance_company="TestIns",
        )
        # Legacy pick: no model, no created_at, no drafts.
        pa = ProposedAppeal.objects.create(
            for_denial=denial, appeal_text="legacy pick", chosen=True, model_name=None
        )
        ProposedAppeal.objects.filter(pk=pa.pk).update(created_at=None)

        response = self.client.get(reverse("model_usage_dashboard"))
        rows = response.context["windows"][0]["proposed_appeal"]
        legacy = next(r for r in rows if r["model_name"] == LEGACY_UNATTRIBUTED_LABEL)
        self.assertEqual(legacy["presented"], 0)
        self.assertIsNone(legacy["win_rate"])
        self.assertContains(response, '<td class="num">&mdash;</td>')

    def test_legacy_null_created_at_only_in_all_time(self):
        denial = Denial.objects.create(
            hashed_email="hash",
            denial_text="denied",
            procedure="MRI",
            diagnosis="back pain",
            insurance_company="TestIns",
        )
        pa = ProposedAppeal.objects.create(
            for_denial=denial, appeal_text="legacy pick", chosen=True, model_name=None
        )
        ProposedAppeal.objects.filter(pk=pa.pk).update(created_at=None)

        response = self.client.get(reverse("model_usage_dashboard"))
        by_slug = {w["slug"]: w for w in response.context["windows"]}
        global_names = {r["model_name"] for r in by_slug["global"]["proposed_appeal"]}
        self.assertIn(LEGACY_UNATTRIBUTED_LABEL, global_names)
        for slug in ("1d", "7d", "30d"):
            names = {r["model_name"] for r in by_slug[slug]["proposed_appeal"]}
            self.assertNotIn(LEGACY_UNATTRIBUTED_LABEL, names, slug)

    def test_synthesized_bucket_preserved(self):
        denial = Denial.objects.create(
            hashed_email="hash",
            denial_text="denied",
            procedure="MRI",
            diagnosis="back pain",
            insurance_company="TestIns",
        )
        ProposedAppeal.objects.create(
            for_denial=denial,
            appeal_text="synth draft",
            chosen=False,
            model_name=SYNTHESIZED_MODEL_NAME,
            synthesized=True,
        )
        ProposedAppeal.objects.create(
            for_denial=denial,
            appeal_text="synth draft",
            chosen=True,
            model_name=SYNTHESIZED_MODEL_NAME,
            synthesized=True,
        )
        task = self._make_task()
        base = self._make_candidate(task, 0, "model-a")
        synth = self._make_candidate(task, 1, SYNTHESIZED_MODEL_NAME, synthesized=True)
        self._vote(task, synth, [base, synth])

        response = self.client.get(reverse("model_usage_dashboard"))
        proposed = response.context["windows"][0]["proposed_appeal"]
        chooser = response.context["windows"][0]["chooser_appeal"]
        prow = next(r for r in proposed if r["model_name"] == SYNTHESIZED_MODEL_NAME)
        crow = next(r for r in chooser if r["model_name"] == SYNTHESIZED_MODEL_NAME)
        self.assertEqual(prow["chosen"], 1)
        self.assertEqual(prow["presented"], 1)
        self.assertEqual(crow["chosen"], 1)
        self.assertEqual(crow["presented"], 1)


class ModelUsageDashboardWindowTest(ChooserStatsHelperMixin, TestCase):
    """Rolling-window boundary behavior for 1d / 7d / 30d / All Time."""

    def setUp(self):
        self.staff = User.objects.create_user(
            username="staff", password="pw123", is_staff=True
        )
        self.client.login(username="staff", password="pw123")

    def _make_vote_hours_ago(self, model_name, hours, session):
        task = self._make_task()
        cand = self._make_candidate(task, 0, model_name)
        vote = self._vote(task, cand, [cand], session=session)
        ChooserVote.objects.filter(pk=vote.pk).update(
            created_at=timezone.now() - datetime.timedelta(hours=hours)
        )
        return vote

    def test_window_slugs_and_order(self):
        response = self.client.get(reverse("model_usage_dashboard"))
        slugs = [w["slug"] for w in response.context["windows"]]
        self.assertEqual(slugs, ["global", "1d", "7d", "30d"])
        labels = [w["label"] for w in response.context["windows"]]
        self.assertEqual(
            labels, ["All Time", "Last 1 Day", "Last 7 Days", "Last 30 Days"]
        )

    def test_rolling_window_boundaries(self):
        # 23h old: in every window. 25h: out of 1d, in 7d/30d.
        # 6d23h: in 7d. 7d1h: out of 7d, in 30d. 45d: All Time only.
        self._make_vote_hours_ago("m-23h", 23, "s1")
        self._make_vote_hours_ago("m-25h", 25, "s2")
        self._make_vote_hours_ago("m-6d23h", 7 * 24 - 1, "s3")
        self._make_vote_hours_ago("m-7d1h", 7 * 24 + 1, "s4")
        self._make_vote_hours_ago("m-45d", 45 * 24, "s5")

        response = self.client.get(reverse("model_usage_dashboard"))
        by_slug = {w["slug"]: w for w in response.context["windows"]}

        def names(slug):
            return {r["model_name"] for r in by_slug[slug]["chooser_appeal"]}

        self.assertEqual(
            names("global"), {"m-23h", "m-25h", "m-6d23h", "m-7d1h", "m-45d"}
        )
        self.assertEqual(names("1d"), {"m-23h"})
        self.assertEqual(names("7d"), {"m-23h", "m-25h", "m-6d23h"})
        self.assertEqual(names("30d"), {"m-23h", "m-25h", "m-6d23h", "m-7d1h"})


class ModelUsageDashboardChartTableAgreementTest(ChooserStatsHelperMixin, TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="staff", password="pw123", is_staff=True
        )
        self.client.login(username="staff", password="pw123")

    def test_chart_series_match_table_rows(self):
        denial = Denial.objects.create(
            hashed_email="hash",
            denial_text="denied",
            procedure="MRI",
            diagnosis="back pain",
            insurance_company="TestIns",
        )
        ProposedAppeal.objects.create(
            for_denial=denial, appeal_text="a", chosen=False, model_name="m1"
        )
        # Picked twice (a re-submit): the chart follows the table's one case,
        # not the two chosen rows.
        for _ in range(2):
            ProposedAppeal.objects.create(
                for_denial=denial, appeal_text="a", chosen=True, model_name="m1"
            )
        task = self._make_task()
        a = self._make_candidate(task, 0, "model-a")
        b = self._make_candidate(task, 1, "model-b")
        self._vote(task, a, [a, b])
        chat_task = self._make_task("chat")
        c = self._make_candidate(chat_task, 0, "chat-model", kind="chat_response")
        self._vote(chat_task, c, [c])

        response = self.client.get(reverse("model_usage_dashboard"))
        for w in response.context["windows"]:
            chart = json.loads(w["chart_data_json"])
            series_by_name = {s["name"]: s for s in chart["series"]}
            # Each series plots the count its table leads with: cases picked
            # for the denial flow, votes won for the chooser.
            for source_key, series_name, count_key in (
                ("proposed_appeal", "ProposedAppeal (denial flow)", "cases_picked"),
                ("chooser_appeal", "Chooser - Appeal", "chosen"),
                ("chooser_chat", "Chooser - Chat", "chosen"),
            ):
                table = {r["model_name"]: r[count_key] for r in w[source_key]}
                points = {
                    p["label"]: p["y"]
                    for p in series_by_name[series_name]["dataPoints"]
                }
                # Every table row appears in the chart with the same count.
                for name, chosen in table.items():
                    self.assertEqual(points.get(name, 0), chosen, (w["slug"], name))
                # And the chart never invents non-zero counts absent from
                # the table.
                for name, y in points.items():
                    if y:
                        self.assertEqual(table.get(name, 0), y, (w["slug"], name))


class DraftQualityColumnsTest(TestCase):
    """The leading indicator beside the lagging one: draft quality per model
    (ml/letter_quality.py) shares the window and the labels of win rate."""

    def setUp(self):
        self.staff = User.objects.create_user(
            username="staff-q", password="pw123", is_staff=True
        )
        self.client.login(username="staff-q", password="pw123")
        self.denial = Denial.objects.create(
            hashed_email="hash-q",
            denial_text="denied",
            procedure="MRI",
            diagnosis="back pain",
            insurance_company="TestIns",
        )

    def _scored(self, model_name, quality, grounding, days_ago=0, chosen=False, scorer=None):
        from fighthealthinsurance.ml import letter_quality

        pa = ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text=f"draft-{model_name}-{quality}-{days_ago}-{scorer}",
            model_name=model_name,
            chosen=chosen,
            quality_score=quality,
            grounding_score=grounding,
            quality_scorer=scorer or letter_quality.SCORER,
            quality_scored_at=timezone.now() - datetime.timedelta(days=days_ago),
        )
        if days_ago > 0:
            ProposedAppeal.objects.filter(pk=pa.pk).update(
                created_at=timezone.now() - datetime.timedelta(days=days_ago)
            )
        return pa

    def test_quality_is_averaged_per_model_in_the_window(self):
        self._scored("m1", 0.9, 2)
        self._scored("m1", 0.5, 0, chosen=True)
        self._scored("m1", 0.1, 0, days_ago=45)  # outside a 30-day window
        self._scored("m2", 0.7, 2)
        since = timezone.now() - datetime.timedelta(days=30)
        rows = {
            r["model_name"]: r
            for r in ModelUsageDashboardView._proposed_appeal_stats(since)
        }
        self.assertAlmostEqual(rows["m1"]["quality_avg"], 0.7)
        self.assertEqual(rows["m1"]["quality_scored"], 2)
        self.assertEqual(rows["m1"]["quality_ungrounded"], 1)
        self.assertAlmostEqual(rows["m2"]["quality_avg"], 0.7)

    def test_unscored_models_show_no_average_not_zero(self):
        ProposedAppeal.objects.create(
            for_denial=self.denial, appeal_text="plain", model_name="m3", chosen=True
        )
        rows = {
            r["model_name"]: r
            for r in ModelUsageDashboardView._proposed_appeal_stats(None)
        }
        self.assertIsNone(rows["m3"]["quality_avg"])
        self.assertEqual(rows["m3"]["quality_scored"], 0)

    def test_only_the_latest_scorer_is_averaged_and_the_rest_are_counted(self):
        from fighthealthinsurance.ml import letter_quality

        older = f"typesafe/speed_20260401/rubric-{letter_quality.RUBRIC_VERSION}"
        self._scored("m1", 0.1, 0, scorer="typesafe/speed_latest/rubric-0", days_ago=3)
        self._scored("m1", 0.7, 2, scorer=older, days_ago=2)
        self._scored("m1", 0.9, 2, days_ago=1)  # the current SCORER, most recent
        rows = {
            r["model_name"]: r
            for r in ModelUsageDashboardView._proposed_appeal_stats(None)
        }
        self.assertAlmostEqual(rows["m1"]["quality_avg"], 0.9)
        self.assertEqual(rows["m1"]["quality_scored"], 1)
        self.assertEqual(rows["m1"]["quality_scorer"], letter_quality.SCORER)
        self.assertEqual(rows["m1"]["quality_other_scorer"], 2)

    def test_a_newer_old_rubric_row_does_not_hide_the_current_series(self):
        from fighthealthinsurance.ml import letter_quality

        self._scored("m1", 0.9, 2, days_ago=2)  # current rubric
        self._scored("m1", 0.1, 0, scorer="typesafe/speed_latest/rubric-0", days_ago=1)  # newer, old rubric
        rows = {
            r["model_name"]: r
            for r in ModelUsageDashboardView._proposed_appeal_stats(None)
        }
        self.assertAlmostEqual(rows["m1"]["quality_avg"], 0.9)
        self.assertEqual(rows["m1"]["quality_other_scorer"], 1)

    def test_twenty_newer_old_rubric_rows_do_not_hide_the_current_scorer(self):
        self._scored("m1", 0.9, 2, days_ago=5)  # current rubric, oldest
        for i in range(20):
            # Distinct text per row: drafts are unique per denial by fingerprint.
            self._scored("m1", 0.1 + i * 0.001, 0, scorer="typesafe/speed_latest/rubric-0", days_ago=1)
        rows = {
            r["model_name"]: r
            for r in ModelUsageDashboardView._proposed_appeal_stats(None)
        }
        self.assertAlmostEqual(rows["m1"]["quality_avg"], 0.9)
        self.assertEqual(rows["m1"]["quality_other_scorer"], 20)

    def test_twenty_distinct_newer_old_scorers_do_not_hide_the_current_one(self):
        self._scored("m1", 0.9, 2, days_ago=5)
        for i in range(20):
            self._scored("m1", 0.1 + i * 0.001, 0, scorer=f"typesafe/old-{i}/rubric-0", days_ago=1)
        rows = {
            r["model_name"]: r
            for r in ModelUsageDashboardView._proposed_appeal_stats(None)
        }
        self.assertAlmostEqual(rows["m1"]["quality_avg"], 0.9)
        self.assertEqual(rows["m1"]["quality_other_scorer"], 20)

    def test_only_old_rubric_rows_still_count_as_other_scorer(self):
        self._scored("m1", 0.1, 0, scorer="typesafe/speed_latest/rubric-0")
        rows = {
            r["model_name"]: r
            for r in ModelUsageDashboardView._proposed_appeal_stats(None)
        }
        self.assertIsNone(rows["m1"]["quality_avg"])
        self.assertEqual(rows["m1"]["quality_other_scorer"], 1)

    def test_merge_keeps_old_callers_working(self):
        rows = _merge_stats({"a": 1}, {"a": 2})
        self.assertIsNone(rows[0]["quality_avg"])
        self.assertEqual(rows[0]["quality_scored"], 0)


class StaffClientMixin:
    """A logged-in staff client and a fresh denial per call."""

    def _login_staff(self):
        User.objects.create_user(username="staff-x", password="pw123", is_staff=True)
        self.client.login(username="staff-x", password="pw123")

    def _denial(self, suffix="d"):
        return Denial.objects.create(
            hashed_email=f"hash-{suffix}",
            denial_text="denied",
            procedure="MRI",
            diagnosis="back pain",
            insurance_company="TestIns",
        )

    def _draft(self, denial, model_name, text, **kwargs):
        return ProposedAppeal.objects.create(
            for_denial=denial,
            appeal_text=text,
            chosen=False,
            model_name=model_name,
            **kwargs,
        )

    def _pick(self, denial, model_name, text="picked", days_ago=0, **kwargs):
        pa = ProposedAppeal.objects.create(
            for_denial=denial,
            appeal_text=text,
            chosen=True,
            model_name=model_name,
            **kwargs,
        )
        if days_ago:
            ProposedAppeal.objects.filter(pk=pa.pk).update(
                created_at=timezone.now() - datetime.timedelta(days=days_ago)
            )
        return pa

    def _windows(self):
        response = self.client.get(reverse("model_usage_dashboard"))
        self.assertEqual(response.status_code, 200)
        return response, {w["slug"]: w for w in response.context["windows"]}


class PerCasePickRateTest(StaffClientMixin, TestCase):
    """The ProposedAppeal table counts cases, so its rate stays a rate."""

    def setUp(self):
        self._login_staff()

    def _by_name(self, since=None):
        return {
            r["model_name"]: r
            for r in ModelUsageDashboardView._proposed_appeal_stats(since)
        }

    def test_a_repeat_pick_is_one_case_not_200_percent(self):
        # The case that used to read 200%: one draft, picked twice.
        denial = self._denial()
        self._draft(denial, "m1", "draft")
        self._pick(denial, "m1")
        self._pick(denial, "m1")
        row = self._by_name()["m1"]
        # The row counts the backfill audit reads are unchanged.
        self.assertEqual(row["chosen"], 2)
        self.assertEqual(row["presented"], 1)
        self.assertEqual(row["cases_picked"], 1)
        self.assertEqual(row["cases_offered"], 1)
        self.assertAlmostEqual(row["pick_rate"], 100.0)
        # Only the ProposedAppeal table has a 100% here: the context-level
        # table beside it still counts per draft.
        response = self.client.get(reverse("model_usage_dashboard"))
        self.assertContains(response, '<td class="num">100.0%</td>', count=4)

    def test_two_drafts_in_one_case_do_not_halve_the_rate(self):
        # m1 writes two drafts per case (full and medically necessary). Per
        # draft it won 1 of 3; per case it was offered twice and picked once.
        first = self._denial("a")
        self._draft(first, "m1", "m1 full")
        self._draft(first, "m1", "m1 medically necessary")
        self._draft(first, "m2", "m2 full")
        self._pick(first, "m1")
        second = self._denial("b")
        self._draft(second, "m1", "m1 full")
        self._draft(second, "m2", "m2 full")
        self._pick(second, "m2")
        rows = self._by_name()
        self.assertEqual(rows["m1"]["presented"], 3)
        self.assertEqual(rows["m1"]["cases_offered"], 2)
        self.assertEqual(rows["m1"]["cases_picked"], 1)
        self.assertAlmostEqual(rows["m1"]["pick_rate"], 50.0)
        self.assertEqual(rows["m2"]["cases_offered"], 2)
        self.assertEqual(rows["m2"]["cases_picked"], 1)
        self.assertAlmostEqual(rows["m2"]["pick_rate"], 50.0)

    def test_the_pick_recorded_last_decides_the_case(self):
        denial = self._denial()
        self._draft(denial, "m1", "m1 draft")
        self._draft(denial, "m2", "m2 draft")
        self._pick(denial, "m1", text="m1 draft")
        self._pick(denial, "m2", text="m2 draft")
        rows = self._by_name()
        self.assertEqual(rows["m2"]["cases_picked"], 1)
        self.assertEqual(rows["m1"]["cases_picked"], 0)
        self.assertAlmostEqual(rows["m1"]["pick_rate"], 0.0)
        # The earlier pick still counts as a chosen row.
        self.assertEqual(rows["m1"]["chosen"], 1)

    def test_cases_follow_the_pick_window(self):
        fresh = self._denial("fresh")
        self._draft(fresh, "m1", "draft")
        self._pick(fresh, "m1")
        old = self._denial("old")
        self._draft(old, "m1", "draft")
        self._pick(old, "m1", days_ago=45)
        since = timezone.now() - datetime.timedelta(days=30)
        self.assertEqual(self._by_name(since)["m1"]["cases_offered"], 1)
        self.assertEqual(self._by_name(None)["m1"]["cases_offered"], 2)

    def test_a_bucket_with_no_offer_has_no_rate(self):
        denial = self._denial()
        self._pick(denial, None)
        row = self._by_name()[UNKNOWN_MODEL_LABEL]
        self.assertEqual(row["cases_picked"], 1)
        self.assertEqual(row["cases_offered"], 0)
        self.assertIsNone(row["pick_rate"])


class UnattributedSplitTest(StaffClientMixin, TestCase):
    """A pick with no model is a template letter, share-appeal text, a
    pre-tracking row or a real miss, and each gets its own bucket."""

    def setUp(self):
        self._login_staff()

    def test_null_model_picks_split_into_their_buckets(self):
        self._pick(self._denial("t"), None, context_level="template")
        # A template letter sent through the share form stays a template pick.
        self._pick(self._denial("te"), None, context_level="template", editted=True)
        self._pick(self._denial("s"), None, editted=True)
        self._pick(self._denial("u"), None)
        legacy = self._pick(self._denial("l"), None)
        ProposedAppeal.objects.filter(pk=legacy.pk).update(created_at=None)
        # A share-appeal pick whose text matched a draft is attributed and
        # stays with its model.
        attributed = self._denial("a")
        self._draft(attributed, "m1", "draft")
        self._pick(attributed, "m1", text="draft", editted=True)

        rows = {
            r["model_name"]: r
            for r in ModelUsageDashboardView._proposed_appeal_stats(None)
        }
        expected = {
            TEMPLATE_PICK_LABEL: 2,
            SHARED_APPEAL_LABEL: 1,
            UNKNOWN_MODEL_LABEL: 1,
            LEGACY_UNATTRIBUTED_LABEL: 1,
            "m1": 1,
        }
        self.assertEqual(
            {name: row["chosen"] for name, row in rows.items()}, expected
        )
        self.assertEqual(
            {name: row["cases_picked"] for name, row in rows.items()}, expected
        )
        # Pre-tracking rows stay out of every bounded window.
        since = timezone.now() - datetime.timedelta(days=1)
        bounded = {
            r["model_name"] for r in ModelUsageDashboardView._proposed_appeal_stats(since)
        }
        self.assertNotIn(LEGACY_UNATTRIBUTED_LABEL, bounded)
        self.assertIn(TEMPLATE_PICK_LABEL, bounded)

        response = self.client.get(reverse("model_usage_dashboard"))
        self.assertContains(response, f"<td>{TEMPLATE_PICK_LABEL}</td>")
        self.assertContains(response, f"<td>{SHARED_APPEAL_LABEL}</td>")

    def test_blank_and_whitespace_names_split_like_null(self):
        # A blank or whitespace-only model_name is no name at all, so it gets
        # the same template / share-appeal split as NULL, not the
        # unattributed bucket.
        self._pick(self._denial("bt"), "", context_level="template")
        self._pick(self._denial("ws"), "   ", editted=True)
        self._pick(self._denial("bu"), "")

        rows = {
            r["model_name"]: r
            for r in ModelUsageDashboardView._proposed_appeal_stats(None)
        }
        expected = {
            TEMPLATE_PICK_LABEL: 1,
            SHARED_APPEAL_LABEL: 1,
            UNKNOWN_MODEL_LABEL: 1,
        }
        self.assertEqual(
            {name: row["chosen"] for name, row in rows.items()}, expected
        )
        self.assertEqual(
            {name: row["cases_picked"] for name, row in rows.items()}, expected
        )


class ContextLevelLabelTest(StaffClientMixin, TestCase):
    def setUp(self):
        self._login_staff()

    def test_levels_show_their_readable_names(self):
        denial = self._denial()
        self._draft(denial, "m1", "shed draft", context_level="tier1_shed")
        self._draft(denial, "m2", "old draft")  # no level recorded
        self._pick(denial, "m1", text="shed draft", context_level="tier1_shed")
        rows = {
            r["model_name"]: r
            for r in ModelUsageDashboardView._context_level_stats(None)
        }
        # The key stays the stored level; only the shown label changes.
        self.assertEqual(rows["tier1_shed"]["label"], "Tier-1 shed (enrichment dropped)")
        self.assertEqual(rows[UNKNOWN_MODEL_LABEL]["label"], NO_CONTEXT_LEVEL_LABEL)
        response = self.client.get(reverse("model_usage_dashboard"))
        self.assertContains(response, "<td>Tier-1 shed (enrichment dropped)</td>")
        self.assertContains(response, f"<td>{NO_CONTEXT_LEVEL_LABEL}</td>")
        self.assertNotContains(response, "<td>tier1_shed</td>")


class QualityColumnsOnlyWhereScoredTest(ChooserStatsHelperMixin, StaffClientMixin, TestCase):
    """Only the ProposedAppeal table can carry scorer data."""

    def setUp(self):
        self._login_staff()

    def test_chooser_and_context_tables_have_no_quality_columns(self):
        task = self._make_task()
        a = self._make_candidate(task, 0, "model-a")
        self._vote(task, a, [a])
        response = self.client.get(reverse("model_usage_dashboard"))
        self.assertContains(response, "Votes won")
        self.assertNotContains(response, "Ungrounded")
        self.assertNotContains(response, "Other scorer")

    def test_only_the_proposed_appeal_table_shows_them(self):
        denial = self._denial()
        self._draft(denial, "m1", "draft", context_level="full")
        self._pick(denial, "m1", text="draft", context_level="full")
        response = self.client.get(reverse("model_usage_dashboard"))
        html = response.content.decode()
        # Four windows, and in each only the ProposedAppeal table (not the
        # context-level one beside it) has the column.
        self.assertEqual(html.count(">Ungrounded</th>"), 4)
        self.assertEqual(html.count(">Context level</th>"), 4)


class WindowTotalsTest(StaffClientMixin, ChooserStatsHelperMixin, TestCase):
    def setUp(self):
        self._login_staff()

    def _vote_at(self, model_name, session, kind="appeal_letter", days_ago=0):
        task = self._make_task("chat" if kind == "chat_response" else "appeal")
        cand = self._make_candidate(task, 0, model_name, kind=kind)
        vote = self._vote(task, cand, [cand], session=session)
        if days_ago:
            ChooserVote.objects.filter(pk=vote.pk).update(
                created_at=timezone.now() - datetime.timedelta(days=days_ago)
            )

    def _skip_at(self, session, days_ago=0):
        skip = ChooserSkip.objects.create(task=self._make_task(), session_key=session)
        if days_ago:
            ChooserSkip.objects.filter(pk=skip.pk).update(
                created_at=timezone.now() - datetime.timedelta(days=days_ago)
            )

    def test_totals_count_cases_votes_skips_and_sessions(self):
        today = self._denial("today")
        self._draft(today, "m1", "draft")
        self._pick(today, "m1", text="draft")
        self._pick(today, "m1", text="draft")  # a re-submit, still one case
        self._pick(self._denial("today-2"), None)
        self._pick(self._denial("old"), "m1", days_ago=45)
        self._vote_at("model-a", "session-key-1")
        self._vote_at("chat-model", "session-key-2", kind="chat_response")
        self._vote_at("model-a", "session-key-4", days_ago=45)
        self._skip_at("session-key-3")
        self._skip_at("session-key-1")  # voted and skipped: one session
        self._skip_at("session-key-5", days_ago=45)

        response, windows = self._windows()
        self.assertEqual(
            windows["1d"]["totals"],
            {
                "cases_picked": 2,
                "chooser_votes": 2,
                "chooser_skips": 2,
                "chooser_sessions": 3,
            },
        )
        self.assertEqual(
            windows["global"]["totals"],
            {
                "cases_picked": 3,
                "chooser_votes": 3,
                "chooser_skips": 3,
                "chooser_sessions": 5,
            },
        )
        self.assertContains(response, "Distinct chooser sessions: <strong>3</strong>")
        # Counts only: no session key reaches the page.
        self.assertNotContains(response, "session-key-")


class ChartTypeTest(StaffClientMixin, TestCase):
    def setUp(self):
        self._login_staff()

    def test_populations_sit_side_by_side_with_a_caption(self):
        response = self.client.get(reverse("model_usage_dashboard"))
        self.assertContains(response, 'type: "column"')
        self.assertNotContains(response, "stackedColumn")
        self.assertContains(response, "different populations")

    def test_jump_links_reach_every_window_section(self):
        response = self.client.get(reverse("model_usage_dashboard"))
        for w in response.context["windows"]:
            anchor = f"window-{w['slug']}"
            self.assertContains(response, f'<a href="#{anchor}">{w["label"]}</a>')
            self.assertContains(
                response, f'<div class="window-section" id="{anchor}">', count=1
            )

    def test_the_intro_names_each_bucket_on_its_own_line(self):
        response = self.client.get(reverse("model_usage_dashboard"))
        for bucket in (
            SYNTHESIZED_MODEL_NAME,
            LEGACY_UNATTRIBUTED_LABEL,
            "legacy-unresolved (Class)",
            TEMPLATE_PICK_LABEL,
            SHARED_APPEAL_LABEL,
            UNKNOWN_MODEL_LABEL,
            "unknown",
        ):
            self.assertContains(response, f"<li>&ldquo;{bucket}&rdquo;: ")

    def test_staff_dashboard_link_names_every_window(self):
        response = self.client.get(reverse("staff_dashboard"))
        self.assertContains(response, "all time / 1 day / 7 days / 30 days")


def _check(model_name, category, ok, minutes_ago=0):
    row = ModelBackendHealthCheckResult.objects.create(
        run_id=f"run-{model_name}-{minutes_ago}",
        model_name=model_name,
        category=category,
        ok=ok,
        started_at=timezone.now(),
    )
    ModelBackendHealthCheckResult.objects.filter(pk=row.pk).update(
        created_at=timezone.now() - datetime.timedelta(minutes=minutes_ago)
    )


def _backend(model_name, category=mhc.CATEGORY_OTHER, enabled=True):
    return mhc.BackendCheckResult(
        provider="Test",
        model_name=model_name,
        internal_name=model_name,
        category=category,
        enabled=enabled,
    )


INTERNAL = SimpleNamespace(external=False, context_only=False)
EXTERNAL = SimpleNamespace(external=True, context_only=False)
CONTEXT = SimpleNamespace(external=True, context_only=True)


class ModelStateTagTest(StaffClientMixin, ChooserStatsHelperMixin, TestCase):
    """Every model row says what the model is today, from stored and
    in-memory state only."""

    def setUp(self):
        self._login_staff()
        static = [
            _backend("off/not-configured", mhc.CATEGORY_NOT_CONFIGURED, enabled=False),
            _backend("off/disabled", mhc.CATEGORY_DISABLED, enabled=False),
            _backend("ext/no-key", mhc.CATEGORY_MISSING_CREDENTIALS),
        ]
        checkable = [
            (_backend("fhi-internal"), INTERNAL),
            (_backend("ext/good"), EXTERNAL),
            (_backend("ctx/search"), CONTEXT),
            (_backend("ext/broken"), EXTERNAL),
            (_backend("ext/new"), EXTERNAL),
            (_backend("fhi-new"), INTERNAL),
            (_backend("ext/reconfigured"), EXTERNAL),
            # Configured and constructable, but not in the router: the
            # instance enumerate built is the only one there is.
            (_backend("ext/unrouted"), EXTERNAL),
        ]
        registered = {
            result.model_name: [instance]
            for result, instance in checkable
            if result.model_name != "ext/unrouted"
        }
        self.enumerate = mock.patch.object(
            mhc, "enumerate_backend_checks", return_value=(static, checkable)
        )
        self.router = mock.patch(
            "fighthealthinsurance.ml.ml_router.ml_router",
            SimpleNamespace(models_by_name=registered),
        )
        self.enumerate.start()
        self.router.start()
        self.addCleanup(self.enumerate.stop)
        self.addCleanup(self.router.stop)
        _check("fhi-internal", mhc.CATEGORY_PASS, True)
        _check("ext/good", mhc.CATEGORY_PASS, True)
        _check("ctx/search", mhc.CATEGORY_PASS, True)
        _check("ext/broken", mhc.CATEGORY_PASS, True, minutes_ago=90)
        _check("ext/broken", mhc.CATEGORY_TIMEOUT, False, minutes_ago=5)
        _check("ext/unrouted", mhc.CATEGORY_PASS_UNREGISTERED, True)
        # Its newest row is the settings verdict from before it was
        # configured, not a health result.
        _check("ext/reconfigured", mhc.CATEGORY_NOT_CONFIGURED, False)

    def test_each_state(self):
        names = [
            "fhi-internal",
            "ext/good",
            "ctx/search",
            "ext/broken",
            "ext/new",
            "fhi-new",
            "ext/reconfigured",
            "ext/unrouted",
            "off/not-configured",
            "off/disabled",
            "ext/no-key",
            "gone/retired-model",
            SYNTHESIZED_MODEL_NAME,
            LEGACY_UNATTRIBUTED_LABEL,
            legacy_unresolved_label("DeepInfra"),
            UNKNOWN_MODEL_LABEL,
            TEMPLATE_PICK_LABEL,
            SHARED_APPEAL_LABEL,
            "unknown",
        ]
        states = _model_states(names)
        self.assertEqual(
            {name: state["key"] for name, state in states.items()},
            {
                "fhi-internal": "internal_ok",
                "ext/good": "external_ok",
                "ctx/search": "context_only",
                "ext/broken": "failing",
                "ext/new": "external_unchecked",
                "fhi-new": "internal_unchecked",
                "ext/reconfigured": "external_unchecked",
                "ext/unrouted": "external_ok",
                "off/not-configured": "not_configured",
                "off/disabled": "disabled",
                "ext/no-key": "failing",
                "gone/retired-model": "retired",
                SYNTHESIZED_MODEL_NAME: "placeholder",
                LEGACY_UNATTRIBUTED_LABEL: "placeholder",
                legacy_unresolved_label("DeepInfra"): "placeholder",
                UNKNOWN_MODEL_LABEL: "placeholder",
                TEMPLATE_PICK_LABEL: "placeholder",
                SHARED_APPEAL_LABEL: "placeholder",
                "unknown": "placeholder",
            },
        )
        self.assertEqual(states["ext/broken"]["category"], mhc.CATEGORY_TIMEOUT)
        self.assertEqual(
            states["ext/no-key"]["category"], mhc.CATEGORY_MISSING_CREDENTIALS
        )

    def test_a_failure_survives_many_newer_rows_for_other_models(self):
        # ext/broken's newest row is a timeout. 2001 newer rows for another
        # model must not push it out of the read and leave ext/broken looking
        # unchecked.
        ModelBackendHealthCheckResult.objects.bulk_create(
            ModelBackendHealthCheckResult(
                run_id=f"run-good-{i}",
                model_name="ext/good",
                category=mhc.CATEGORY_PASS,
                ok=True,
            )
            for i in range(2001)
        )
        states = _model_states(["ext/broken", "ext/good"])
        self.assertEqual(
            states["ext/broken"], {"key": "failing", "category": mhc.CATEGORY_TIMEOUT}
        )
        self.assertEqual(states["ext/good"], {"key": "external_ok"})

        denial = self._denial()
        self._draft(denial, "ext/broken", "draft")
        self._pick(denial, "ext/broken", text="draft")
        response = self.client.get(reverse("model_usage_dashboard"))
        status = reverse("model_backend_status")
        self.assertContains(
            response,
            f'<a class="state-tag state-fail" href="{status}">failing: FAIL_TIMEOUT</a>',
        )

    def test_the_health_read_is_one_query(self):
        with CaptureQueriesContext(connection) as queries:
            _model_states(["fhi-internal", "ext/good", "ext/broken", "gone/x"])
        self.assertEqual(len(queries.captured_queries), 1)

    def test_every_model_row_renders_its_tag_with_a_literal_class(self):
        task = self._make_task()
        shown = [
            self._make_candidate(task, i, name)
            for i, name in enumerate(
                ["ext/broken", "fhi-internal", "gone/retired-model", "ctx/search"]
            )
        ]
        self._vote(task, shown[0], shown)
        denial = self._denial()
        self._draft(denial, "ext/new", "draft")
        self._pick(denial, "ext/new", text="draft")
        self._pick(self._denial("t"), None, context_level="template")
        ModelCallAttempt.objects.create(
            for_denial=denial, model_name="off/disabled", outcome="not_registered"
        )

        response, windows = self._windows()
        status = reverse("model_backend_status")
        for fragment in (
            f'<a class="state-tag state-fail" href="{status}">failing: FAIL_TIMEOUT</a>',
            f'<a class="state-tag state-ok" href="{status}">internal, healthy</a>',
            f'<a class="state-tag state-off" href="{status}">not in the code any more</a>',
            f'<a class="state-tag state-context" href="{status}">context only</a>',
            f'<a class="state-tag state-warn" href="{status}">external, no health check yet</a>',
            f'<a class="state-tag state-off" href="{status}">disabled</a>',
            '<span class="state-tag state-placeholder">placeholder bucket</span>',
        ):
            self.assertContains(response, fragment)
        for w in windows.values():
            tables = [w["proposed_appeal"], w["chooser_appeal"], w["chooser_chat"]]
            if w["call_attempts"]:
                tables.append(w["call_attempts"]["rows"])
            for rows in tables:
                for row in rows:
                    self.assertIn("state", row, (w["slug"], row["model_name"]))

    def test_an_unreadable_catalog_costs_the_tags_not_the_page(self):
        self.enumerate.stop()
        with mock.patch.object(
            mhc, "enumerate_backend_checks", side_effect=RuntimeError("bad config")
        ):
            states = _model_states(["ext/good", SYNTHESIZED_MODEL_NAME])
            task = self._make_task()
            a = self._make_candidate(task, 0, "ext/good")
            self._vote(task, a, [a])
            response = self.client.get(reverse("model_usage_dashboard"))
        self.enumerate.start()
        self.assertEqual(states["ext/good"]["key"], "unavailable")
        self.assertEqual(states[SYNTHESIZED_MODEL_NAME]["key"], "placeholder")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, ">state unavailable</a>")


class ModelStateMakesNoCallsTest(StaffClientMixin, ChooserStatsHelperMixin, TestCase):
    def setUp(self):
        self._login_staff()

    def test_the_page_never_probes_or_sweeps_a_backend(self):
        from fighthealthinsurance.ml import health_status
        from fighthealthinsurance.ml.ml_models import RemoteOpenLike

        task = self._make_task()
        a = self._make_candidate(task, 0, "anthropic/claude-sonnet-4-6")
        b = self._make_candidate(task, 1, "gone/retired-model")
        self._vote(task, a, [a, b])
        with mock.patch.object(
            RemoteOpenLike, "model_is_ok", side_effect=AssertionError("probe")
        ) as probe, mock.patch.object(
            health_status.health_status,
            "get_snapshot",
            side_effect=AssertionError("sweep"),
        ) as snapshot, mock.patch.object(
            health_status,
            "compute_model_health_details",
            side_effect=AssertionError("live details"),
        ) as details, mock.patch(
            "requests.sessions.Session.request",
            side_effect=AssertionError("network"),
        ) as network:
            response = self.client.get(reverse("model_usage_dashboard"))
        self.assertEqual(response.status_code, 200)
        for patched in (probe, snapshot, details, network):
            self.assertFalse(patched.called, patched)


class CallAttemptTableTest(StaffClientMixin, TestCase):
    """Per-model appeal-generation call outcomes from ModelCallAttempt."""

    def setUp(self):
        self._login_staff()
        self.denial = self._denial()

    def _attempt(self, model_name, outcome, stage="primary", duration_ms=None,
                 run_kind="live", hours_ago=0, **kwargs):
        row = ModelCallAttempt.objects.create(
            for_denial=self.denial,
            model_name=model_name,
            outcome=outcome,
            stage=stage,
            duration_ms=duration_ms,
            run_kind=run_kind,
            response_text="PHI-SENTINEL-RESPONSE",
            error_detail="PHI-SENTINEL-ERROR",
            **kwargs,
        )
        if hours_ago:
            ModelCallAttempt.objects.filter(pk=row.pk).update(
                created_at=timezone.now() - datetime.timedelta(hours=hours_ago)
            )
        return row

    def _seed(self):
        self._attempt("m1", "ok", duration_ms=100)
        self._attempt("m1", "ok", stage="backup", duration_ms=600)
        self._attempt("m1", "ok", duration_ms=200)
        self._attempt("m1", "runt_only")
        self._attempt("m1", "rejected_at_peek")
        self._attempt("m1", "no_output")
        self._attempt("m1", "error", stage="retry_tier_1")
        self._attempt("m1", "not_registered")
        self._attempt("m1", "no_prompt")
        self._attempt("m1", "all_backends_failed")
        # The background precompute is not what users waited on.
        self._attempt("m1", "error", run_kind="speculative")
        self._attempt("spec-only", "ok", run_kind="speculative", duration_ms=5)
        # Ten days old: in the 30-day window only.
        self._attempt("m1", "ok", duration_ms=900, hours_ago=240)
        self._attempt("m3", "ok", duration_ms=100)
        self._attempt("m3", "ok", duration_ms=200)
        self._attempt("unknown", "all_backends_failed")

    def test_outcomes_backup_share_and_median(self):
        self._seed()
        _, windows = self._windows()
        rows = {r["model_name"]: r for r in windows["1d"]["call_attempts"]["rows"]}
        m1 = rows["m1"]
        self.assertEqual(
            {k: m1[k] for k in (
                "calls", "ok", "runt_only", "rejected_at_peek", "no_output",
                "error", "other", "fallback",
            )},
            {
                "calls": 10,
                "ok": 3,
                "runt_only": 1,
                "rejected_at_peek": 1,
                "no_output": 1,
                "error": 1,
                "other": 3,
                "fallback": 2,
            },
        )
        self.assertAlmostEqual(m1["fallback_share"], 20.0)
        # The median of 100, 200 and 600 ms, not their 300 ms mean.
        self.assertEqual(m1["median_ms"], 200)
        self.assertEqual(rows["m3"]["median_ms"], 150)
        self.assertIsNone(rows["unknown"]["median_ms"])
        self.assertEqual(rows["unknown"]["state"]["key"], "placeholder")
        self.assertNotIn("spec-only", rows)
        month = {r["model_name"]: r for r in windows["30d"]["call_attempts"]["rows"]}
        self.assertEqual(month["m1"]["calls"], 11)
        self.assertEqual(month["m1"]["median_ms"], 400)

    def test_all_time_is_skipped_and_says_why(self):
        self._seed()
        response, windows = self._windows()
        self.assertIsNone(windows["global"]["call_attempts"])
        for slug in ("1d", "7d", "30d"):
            self.assertIsNotNone(windows[slug]["call_attempts"], slug)
        self.assertContains(response, "Not shown for All Time")
        self.assertContains(response, '<td class="num">200 ms</td>')

    def test_no_phi_column_is_read_or_shown(self):
        self._seed()
        ProposedAppeal.objects.create(
            for_denial=self.denial, appeal_text="PHI-SENTINEL-DRAFT", chosen=True,
            model_name="m1",
        )
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(reverse("model_usage_dashboard"))
        sql = "\n".join(q["sql"] for q in queries.captured_queries)
        for column in ("response_text", "error_detail", "appeal_text"):
            self.assertNotIn(column, sql)
        self.assertNotContains(response, "PHI-SENTINEL")

    def test_the_duration_cap_flags_only_the_windows_it_cut(self):
        self._attempt("m1", "ok", duration_ms=100, hours_ago=1)
        self._attempt("m1", "ok", duration_ms=200, hours_ago=72)
        self._attempt("m1", "ok", duration_ms=300, hours_ago=240)
        with mock.patch("fighthealthinsurance.staff_views.CALL_DURATION_SAMPLE_CAP", 2):
            response, windows = self._windows()
        self.assertFalse(windows["1d"]["call_attempts"]["median_capped"])
        self.assertTrue(windows["7d"]["call_attempts"]["median_capped"])
        self.assertTrue(windows["30d"]["call_attempts"]["median_capped"])
        # The 1-day window was read whole; the 30-day one lost its oldest row.
        self.assertEqual(windows["1d"]["call_attempts"]["rows"][0]["median_ms"], 100)
        self.assertEqual(windows["30d"]["call_attempts"]["rows"][0]["median_ms"], 150)
        self.assertContains(response, "Medians here use only the newest 2 OK calls")
