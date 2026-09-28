"""Tests for the ML Model Usage staff dashboard."""

import datetime
import json
import re
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from fighthealthinsurance.ml import model_health_check as mhc
from fighthealthinsurance.ml.model_identity import (
    LEGACY_UNATTRIBUTED_LABEL,
    SYNTHESIZED_MODEL_NAME,
    TEMPLATE_MODEL_NAME,
    legacy_unresolved_label,
)
from fighthealthinsurance.models import (
    ChatRoutingPolicy,
    ChatTurn,
    ChooserCandidate,
    ChooserSkip,
    ChooserTask,
    ChooserVote,
    Denial,
    ModelBackendHealthCheckResult,
    ModelCallAttempt,
    OngoingChat,
    ProposedAppeal,
)
from fighthealthinsurance.staff_views import (
    NO_CONTEXT_LEVEL_LABEL,
    SHARED_APPEAL_LABEL,
    TEMPLATE_PICK_LABEL,
    UNKNOWN_MODEL_LABEL,
    _merge_stats,
    _model_states,
    ModelUsageDashboardView,
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
            appeal_text="shed-draft",
            chosen=False,
            model_name="m1",
            context_level="tier1_shed",
        )
        # The pick comes last: presented counts only what existed when the
        # user chose.
        ProposedAppeal.objects.create(
            for_denial=d,
            appeal_text="full-chosen",
            chosen=True,
            model_name="m1",
            context_level="full",
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

    def test_multiple_chosen_rows_count_presented_once_per_pick(self):
        # Two picks on the same denial (re-submit): the draft was on offer at
        # each, so it is presented twice against two chosen -- 100%, not the
        # 200% that counting it once against both picks produced.
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
        self.assertEqual(m1["presented"], 2)
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
            for source_key, series_name in (
                ("proposed_appeal", "ProposedAppeal (denial flow)"),
                ("chooser_appeal", "Chooser - Appeal"),
                ("chooser_chat", "Chooser - Chat"),
            ):
                table = {r["model_name"]: r["chosen"] for r in w[source_key]}
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

    def _scored(
        self, model_name, quality, grounding, days_ago=0, chosen=False, scorer=None
    ):
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
        self._scored(
            "m1", 0.1, 0, scorer="typesafe/speed_latest/rubric-0", days_ago=1
        )  # newer, old rubric
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
            self._scored(
                "m1",
                0.1 + i * 0.001,
                0,
                scorer="typesafe/speed_latest/rubric-0",
                days_ago=1,
            )
        rows = {
            r["model_name"]: r
            for r in ModelUsageDashboardView._proposed_appeal_stats(None)
        }
        self.assertAlmostEqual(rows["m1"]["quality_avg"], 0.9)
        self.assertEqual(rows["m1"]["quality_other_scorer"], 20)

    def test_twenty_distinct_newer_old_scorers_do_not_hide_the_current_one(self):
        self._scored("m1", 0.9, 2, days_ago=5)
        for i in range(20):
            self._scored(
                "m1",
                0.1 + i * 0.001,
                0,
                scorer=f"typesafe/old-{i}/rubric-0",
                days_ago=1,
            )
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


class _StaffDashboardCase(TestCase):
    def setUp(self):
        User.objects.create_user(username="staff", password="pw123", is_staff=True)
        self.client.login(username="staff", password="pw123")
        self.denial = Denial.objects.create(
            hashed_email="hash",
            denial_text="denied",
            procedure="MRI",
            diagnosis="back pain",
            insurance_company="TestIns",
        )

    def _rows(self, source="proposed_appeal"):
        response = self.client.get(reverse("model_usage_dashboard"))
        return {r["model_name"]: r for r in response.context["windows"][0][source]}

    def _draft(self, model_name, text, **kwargs):
        return ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text=text,
            chosen=False,
            model_name=model_name,
            **kwargs,
        )

    def _pick(self, model_name, text, **kwargs):
        return ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text=text,
            chosen=True,
            model_name=model_name,
            **kwargs,
        )


class PresentedCountsOnlyDraftsBeforeThePickTest(_StaffDashboardCase):
    """Every visit to the appeals page reruns generation, so a denial keeps
    accumulating drafts after the user chose; those were never in the
    running and must not count as presented."""

    def test_drafts_stored_after_the_pick_are_not_presented(self):
        self._draft("m1", "seen")
        self._pick("m1", "seen")
        # A later visit regenerated: never shown before the pick.
        self._draft("m2", "later")
        self._draft("m1", "later too")
        rows = self._rows()
        self.assertEqual(rows["m1"]["presented"], 1)
        self.assertAlmostEqual(rows["m1"]["win_rate"], 100.0)
        self.assertNotIn("m2", rows)

    def test_a_second_pick_widens_the_window_to_what_it_saw(self):
        self._draft("m1", "first round")
        self._pick("m1", "first round")
        self._draft("m2", "second round")
        self._pick("m2", "second round")
        rows = self._rows()
        # m1's draft was on offer at both picks (and passed over at the
        # second); m2's only at the second.
        self.assertEqual(rows["m1"]["presented"], 2)
        self.assertEqual(rows["m2"]["presented"], 1)

    def test_context_level_table_uses_the_same_bound(self):
        self._draft("m1", "seen", context_level="full")
        self._pick("m1", "seen", context_level="full")
        self._draft("m1", "later", context_level="tier1_shed")
        rows = self._rows(source="context_level")
        self.assertEqual(rows["full"]["presented"], 1)
        self.assertNotIn("tier1_shed", rows)

    def test_several_drafts_from_one_model_are_counted_once_per_draft(self):
        # Pinned on purpose: per draft is the fan-out-neutral unit. On one
        # denial every draft competes with every other, so a model with two
        # of three cards is picked two thirds of the time per denial but one
        # third per draft, the same as a single-card model. Counting per
        # denial instead would reward fan-out.
        self._draft("m1", "temperature 0.6 leg")
        self._draft("m1", "temperature 0.1 leg")
        self._draft("m1", "medically necessary draft")
        self._draft("m2", "the other model")
        self._pick("m1", "temperature 0.6 leg")
        rows = self._rows()
        self.assertEqual(rows["m1"]["presented"], 3)
        self.assertAlmostEqual(rows["m1"]["win_rate"], 100.0 / 3)
        self.assertEqual(rows["m2"]["presented"], 1)


class PresentedIsWhatThePickSawTest(_StaffDashboardCase):
    """The appeals page folds drafts past its visible limit behind a button,
    so a pick reports the ids that were on screen; those, not everything
    generated for the denial, are the drafts that lost."""

    def test_folded_drafts_the_pick_did_not_see_are_not_presented(self):
        seen_a = self._draft("m1", "seen a")
        seen_b = self._draft("m2", "seen b")
        self._draft("m3", "folded behind show more")
        self._pick("m1", "seen a", presented_ids=[seen_a.id, seen_b.id])
        rows = self._rows()
        self.assertEqual(rows["m1"]["presented"], 1)
        self.assertEqual(rows["m2"]["presented"], 1)
        self.assertNotIn("m3", rows)

    def test_a_pick_that_reported_nothing_falls_back_to_drafts_before_it(self):
        self._draft("m1", "one")
        self._draft("m2", "two")
        self._pick("m1", "one")
        rows = self._rows()
        self.assertEqual(rows["m1"]["presented"], 1)
        self.assertEqual(rows["m2"]["presented"], 1)

    def test_reported_and_unreported_denials_are_not_double_counted(self):
        other = Denial.objects.create(
            hashed_email="hash2",
            denial_text="denied",
            procedure="MRI",
            diagnosis="back pain",
            insurance_company="TestIns",
        )
        a = self._draft("m1", "reported denial, seen")
        self._draft("m1", "reported denial, folded")
        self._pick("m1", "reported denial, seen", presented_ids=[a.id])
        ProposedAppeal.objects.create(
            for_denial=other, appeal_text="old style", chosen=False, model_name="m1"
        )
        ProposedAppeal.objects.create(
            for_denial=other, appeal_text="old style", chosen=True, model_name="m1"
        )
        rows = self._rows()
        # One from the report, one from the fallback; the folded draft and
        # the reported denial's fallback never count.
        self.assertEqual(rows["m1"]["presented"], 2)
        self.assertEqual(rows["m1"]["chosen"], 2)

    def test_context_level_table_uses_the_report_too(self):
        seen = self._draft("m1", "seen", context_level="full")
        self._draft("m1", "folded", context_level="tier1_shed")
        self._pick("m1", "seen", context_level="full", presented_ids=[seen.id])
        rows = self._rows(source="context_level")
        self.assertEqual(rows["full"]["presented"], 1)
        self.assertNotIn("tier1_shed", rows)

    def test_an_unreported_then_a_reported_pick_on_one_denial_count_both(self):
        # A denial picked before this deploy and again after it, inside the
        # window: the first pick's candidates come from the fallback, the
        # second's from its report, and the picked model wins both.
        a = self._draft("m1", "a")
        self._draft("m2", "b")
        self._pick("m1", "a")
        self._pick("m1", "a", presented_ids=[a.id])
        rows = self._rows()
        self.assertEqual(rows["m1"]["chosen"], 2)
        self.assertEqual(rows["m1"]["presented"], 2)
        self.assertAlmostEqual(rows["m1"]["win_rate"], 100.0)
        self.assertEqual(rows["m2"]["presented"], 1)

    def test_an_empty_report_does_not_fall_back_to_every_stored_draft(self):
        # The browser said nothing stored was on screen (the picked card was
        # never saved): that is not "nobody said".
        self._draft("m1", "a")
        self._draft("m2", "b")
        self._pick(None, "an unsaved card", presented_ids=[])
        rows = self._rows()
        self.assertNotIn("m1", rows)
        self.assertNotIn("m2", rows)

    def test_each_unreported_pick_counts_the_drafts_it_saw(self):
        # Two re-submits of the same draft are two picks; the fallback used
        # to count the drafts once, which read as a 200% win rate.
        self._draft("m1", "a")
        self._draft("m2", "b")
        self._pick("m1", "a")
        self._pick("m1", "a")
        rows = self._rows()
        self.assertEqual(rows["m1"]["chosen"], 2)
        self.assertEqual(rows["m1"]["presented"], 2)
        self.assertAlmostEqual(rows["m1"]["win_rate"], 100.0)
        self.assertEqual(rows["m2"]["presented"], 2)


class TemplateDraftsAreAModelBucketTest(_StaffDashboardCase):
    """Non-AI template drafts are presented and picked like any model's; with
    model_name NULL their picks read as attribution misses and they never
    counted as presented."""

    def test_template_pick_is_bucketed_as_template_not_unattributed(self):
        self._draft(TEMPLATE_MODEL_NAME, "template letter", context_level="template")
        self._draft("m1", "model letter")
        self._pick(TEMPLATE_MODEL_NAME, "template letter", context_level="template")
        rows = self._rows()
        self.assertEqual(rows[TEMPLATE_MODEL_NAME]["chosen"], 1)
        self.assertEqual(rows[TEMPLATE_MODEL_NAME]["presented"], 1)
        self.assertEqual(rows["m1"]["presented"], 1)
        self.assertNotIn(UNKNOWN_MODEL_LABEL, rows)


class ContextLevelLegacyBucketTest(_StaffDashboardCase):
    """The context-level table follows the model table's rules for rows
    without a level, so the two tables agree on what a bucket means."""

    def test_pre_tracking_pick_is_legacy_and_level_less_drafts_are_not_presented(
        self,
    ):
        self._draft(None, "old draft", context_level=None)
        pick = self._pick(None, "old pick", context_level=None)
        ProposedAppeal.objects.filter(pk=pick.pk).update(created_at=None)
        rows = self._rows(source="context_level")
        self.assertEqual(rows[LEGACY_UNATTRIBUTED_LABEL]["chosen"], 1)
        self.assertEqual(rows[LEGACY_UNATTRIBUTED_LABEL]["presented"], 0)
        self.assertIsNone(rows[LEGACY_UNATTRIBUTED_LABEL]["win_rate"])
        self.assertNotIn(UNKNOWN_MODEL_LABEL, rows)

    def test_post_tracking_pick_without_a_level_stays_unattributed(self):
        self._pick(None, "recent pick", context_level=None)
        rows = self._rows(source="context_level")
        self.assertEqual(rows[UNKNOWN_MODEL_LABEL]["chosen"], 1)
        self.assertNotIn(LEGACY_UNATTRIBUTED_LABEL, rows)
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
        self.assertEqual({name: row["chosen"] for name, row in rows.items()}, expected)
        self.assertEqual({name: row["chosen"] for name, row in rows.items()}, expected)
        # Pre-tracking rows stay out of every bounded window.
        since = timezone.now() - datetime.timedelta(days=1)
        bounded = {
            r["model_name"]
            for r in ModelUsageDashboardView._proposed_appeal_stats(since)
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
        self.assertEqual({name: row["chosen"] for name, row in rows.items()}, expected)
        self.assertEqual({name: row["chosen"] for name, row in rows.items()}, expected)


class ContextLevelLabelTest(StaffClientMixin, TestCase):
    def setUp(self):
        self._login_staff()

    def test_levels_show_their_readable_names(self):
        denial = self._denial()
        self._draft(denial, "m1", "shed draft", context_level="tier1_shed")
        # A pick recorded after level tracking but without a level.
        self._pick(self._denial("no-level"), "m2", text="no level")
        self._pick(denial, "m1", text="shed draft", context_level="tier1_shed")
        rows = {
            r["model_name"]: r
            for r in ModelUsageDashboardView._context_level_stats(None)
        }
        # The key stays the stored level; only the shown label changes.
        self.assertEqual(
            rows["tier1_shed"]["label"], "Tier-1 shed (enrichment dropped)"
        )
        self.assertEqual(rows[UNKNOWN_MODEL_LABEL]["label"], NO_CONTEXT_LEVEL_LABEL)
        response = self.client.get(reverse("model_usage_dashboard"))
        self.assertContains(response, "<td>Tier-1 shed (enrichment dropped)</td>")
        self.assertContains(response, f"<td>{NO_CONTEXT_LEVEL_LABEL}</td>")
        self.assertNotContains(response, "<td>tier1_shed</td>")


class QualityColumnsOnlyWhereScoredTest(
    ChooserStatsHelperMixin, StaffClientMixin, TestCase
):
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

    def test_totals_count_picks_votes_skips_and_sessions(self):
        today = self._denial("today")
        self._draft(today, "m1", "draft")
        self._pick(today, "m1", text="draft")
        self._pick(today, "m1", text="draft")  # a re-submit counts as a pick
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
                "picks": 3,
                "chooser_votes": 2,
                "chooser_skips": 2,
                "chooser_sessions": 3,
            },
        )
        self.assertEqual(
            windows["global"]["totals"],
            {
                "picks": 4,
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
        self.assertContains(response, "(all time / 1 day / 7 days / 30 days)")
        # The call table skips All Time, so the link must not promise it.
        self.assertContains(response, "appeal call outcomes (1 day / 7 days / 30 days)")


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

    def _attempt(
        self,
        model_name,
        outcome,
        stage="primary",
        duration_ms=None,
        run_kind="live",
        hours_ago=0,
        **kwargs,
    ):
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
            {
                k: m1[k]
                for k in (
                    "calls",
                    "ok",
                    "runt_only",
                    "rejected_at_peek",
                    "no_output",
                    "error",
                    "other",
                    "fallback",
                )
            },
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
            for_denial=self.denial,
            appeal_text="PHI-SENTINEL-DRAFT",
            chosen=True,
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


def _call(model, status="scored", ms=100, pass_kind="primary"):
    return {
        "model": model,
        "backend": "",
        "external": None,
        "pass": pass_kind,
        "depth": 0,
        "history": "truncated",
        "variant": "",
        "status": status,
        "error": "RuntimeError" if status == "error" else "",
        "ms": None if status == "late" else ms,
        "score": 1.0 if status == "scored" else None,
    }


class LiveChatSectionTest(StaffClientMixin, TestCase):
    """The live chat model race from ChatTurn: a section per bounded window,
    kept out of the chart."""

    def setUp(self):
        self._login_staff()
        self.chat = OngoingChat.objects.create()

    def _turn(self, days_ago=0, hours_ago=0, **fields):
        defaults = dict(
            outcome="ok",
            use_external=True,
            backends=["model-a"],
            winner_model="model-a",
            calls=[_call("model-a")],
        )
        defaults.update(fields)
        turn = ChatTurn.objects.create(chat=self.chat, **defaults)
        if days_ago or hours_ago:
            ChatTurn.objects.filter(pk=turn.pk).update(
                created_at=timezone.now()
                - datetime.timedelta(days=days_ago, hours=hours_ago)
            )
        return turn

    @staticmethod
    def _rows(window):
        return {r["model_name"]: r for r in window["live_chat"]["rows"]}

    def test_only_the_bounded_windows_have_a_chat_section(self):
        self._turn()
        response, windows = self._windows()
        self.assertIsNone(windows["global"]["live_chat"])
        for slug in ("1d", "7d", "30d"):
            self.assertIsNotNone(windows[slug]["live_chat"], slug)
        self.assertContains(response, "<h3>Live chat <span")
        self.assertContains(response, "turn records only exist since turn recording")

    def test_turns_fall_in_the_windows_they_belong_to(self):
        self._turn(hours_ago=2)
        self._turn(days_ago=3)
        self._turn(days_ago=10)
        self._turn(days_ago=40)
        _response, windows = self._windows()
        self.assertEqual(
            {
                slug: windows[slug]["live_chat"]["summary"]["turns"]
                for slug in ("1d", "7d", "30d")
            },
            {"1d": 1, "7d": 2, "30d": 3},
        )

    def test_win_rate_is_wins_over_turns_asked(self):
        # model-a is listed twice, so it gets more calls than turns asked:
        # the rate is per turn, not per call.
        self._turn(
            backends=["model-a", "model-a", "model-b"],
            winner_model="model-a",
            calls=[_call("model-a"), _call("model-a"), _call("model-b")],
        )
        self._turn(backends=["model-a", "model-b"], winner_model="model-b")
        self._turn(backends=["model-a", "model-b"], winner_model="", outcome="failed")
        _response, windows = self._windows()
        rows = self._rows(windows["1d"])
        self.assertEqual((rows["model-a"]["asked"], rows["model-a"]["wins"]), (3, 1))
        self.assertAlmostEqual(rows["model-a"]["win_rate"], 100 / 3)
        self.assertEqual((rows["model-b"]["asked"], rows["model-b"]["wins"]), (3, 1))

    def test_a_model_never_asked_shows_a_dash_not_zero(self):
        self._turn(calls=[_call("model-a"), _call("stray-model")])
        response, windows = self._windows()
        rows = self._rows(windows["1d"])
        self.assertEqual(rows["stray-model"]["asked"], 0)
        self.assertIsNone(rows["stray-model"]["win_rate"])
        row_html = re.search(
            r"<tr>\s*<td>stray-model</td>.*?</tr>", response.content.decode(), re.S
        )
        self.assertIsNotNone(row_html)
        self.assertIn('<td class="num">&mdash;</td>', row_html.group(0))
        self.assertNotIn("0.0%", row_html.group(0))

    def test_fallbacks_count_as_asked_only_when_the_retry_ran(self):
        self._turn(fallback_backends=["claude"], retry_ran=False)
        self._turn(
            fallback_backends=["claude"],
            retry_ran=True,
            retry_used=True,
            winner_model="claude",
            winner_external=True,
            calls=[_call("model-a", "empty"), _call("claude", pass_kind="retry")],
        )
        _response, windows = self._windows()
        rows = self._rows(windows["1d"])
        self.assertEqual(rows["claude"]["asked"], 1)
        self.assertEqual(rows["claude"]["wins"], 1)
        summary = windows["1d"]["live_chat"]["summary"]
        self.assertEqual((summary["retry_ran"], summary["retry_used"]), (1, 1))

    def test_call_statuses_and_the_median_of_calls_that_answered(self):
        self._turn(
            calls=[
                _call("model-a", "scored", ms=100),
                _call("model-a", "repeat", ms=300),
                _call("model-a", "empty", ms=500),
                _call("model-a", "error", ms=9000),
                _call("model-a", "late"),
            ]
        )
        _response, windows = self._windows()
        row = self._rows(windows["1d"])["model-a"]
        self.assertEqual(row["calls"], 5)
        self.assertEqual(
            (row["late"], row["error"], row["empty"], row["repeat"]), (1, 1, 1, 1)
        )
        # The error's time is not an answer time.
        self.assertEqual(row["median_ms"], 300)

    def test_an_unscored_call_keeps_its_time_and_is_not_late(self):
        self._turn(
            calls=[
                _call("model-a", "unscored", ms=200),
                _call("model-a", "scored", ms=400),
                _call("model-a", "late"),
            ]
        )
        response, windows = self._windows()
        row = self._rows(windows["1d"])["model-a"]
        self.assertEqual((row["late"], row["unscored"]), (1, 1))
        # It answered, so its time counts toward the median.
        self.assertEqual(row["median_ms"], 300)
        self.assertContains(response, ">Unscored</th>")

    def test_wins_go_to_the_model_whose_reply_was_delivered(self):
        # A tool follow-up won by model-b replaced model-a's first-pass reply.
        self._turn(
            backends=["model-a", "model-b"],
            first_pass_model="model-a",
            winner_model="model-b",
            winner_pass="tool",
            winner_external=True,
            tool_rewrote=True,
            calls=[_call("model-a"), _call("model-b", pass_kind="tool")],
        )
        _response, windows = self._windows()
        rows = self._rows(windows["1d"])
        self.assertEqual((rows["model-a"]["wins"], rows["model-b"]["wins"]), (0, 1))
        summary = windows["1d"]["live_chat"]["summary"]
        self.assertEqual(summary["external_wins"], 1)

    def test_summary_counts_outcomes_outside_models_and_picks(self):
        self._turn(winner_model="claude", winner_external=True)
        self._turn(winner_external=False)
        self._turn(use_external=False, winner_external=False)
        self._turn(outcome="failed", winner_model="")
        self._turn(outcome="timeout", winner_model="")
        _response, windows = self._windows()
        summary = windows["1d"]["live_chat"]["summary"]
        self.assertEqual(
            (summary["turns"], summary["ok"], summary["failed"], summary["timeout"]),
            (5, 3, 1, 1),
        )
        self.assertEqual(summary["external_allowed"], 4)
        self.assertAlmostEqual(summary["external_share"], 80.0)
        self.assertEqual(summary["external_wins"], 1)
        self.assertAlmostEqual(summary["external_win_share"], 100 / 3)

    def test_side_by_side_counts_only_pairs_from_two_models(self):
        cross = dict(
            alternate_offered=True,
            alternate_model="model-b",
            alternate_cross_model=True,
        )
        self._turn(preferred="alternate", **cross)
        self._turn(preferred="primary", **cross)
        self._turn(**cross)
        self._turn(
            alternate_offered=True,
            alternate_model="model-a",
            alternate_cross_model=False,
            preferred="alternate",
        )
        _response, windows = self._windows()
        chat = windows["1d"]["live_chat"]
        summary = chat["summary"]
        self.assertEqual((summary["alternates"], summary["cross_alternates"]), (4, 3))
        self.assertEqual(
            (summary["picks"], summary["picked_primary"], summary["picked_alternate"]),
            (3, 1, 2),
        )
        self.assertEqual(
            (summary["same_model_pairs"], summary["same_model_picked_alternate"]),
            (1, 1),
        )
        self.assertEqual(
            chat["pairs"],
            [
                {
                    "primary_model": "model-a",
                    "alternate_model": "model-b",
                    "offered": 3,
                    "answered": 2,
                    "primary": 1,
                    "alternate": 1,
                }
            ],
        )
        rows = self._rows(windows["1d"])
        model_a = rows["model-a"]
        self.assertEqual(
            (model_a["sbs_shown"], model_a["sbs_answered"], model_a["sbs_preferred"]),
            (3, 2, 1),
        )
        self.assertAlmostEqual(rows["model-b"]["sbs_rate"], 50.0)
        self.assertEqual(
            [r["model_name"] for r in chat["side_by_side_rows"]], ["model-a", "model-b"]
        )

    def test_a_pair_with_no_pick_has_no_pick_rate(self):
        self._turn(
            alternate_offered=True,
            alternate_model="model-b",
            alternate_cross_model=True,
        )
        _response, windows = self._windows()
        self.assertIsNone(self._rows(windows["1d"])["model-b"]["sbs_rate"])

    def test_chat_models_stay_out_of_the_chart(self):
        self._turn(backends=["chat-only-model"], winner_model="chat-only-model")
        _response, windows = self._windows()
        for w in windows.values():
            self.assertNotIn(
                "chat-only-model", json.loads(w["chart_data_json"])["labels"]
            )

    def test_chat_rows_carry_their_state_tag(self):
        self._turn(
            alternate_offered=True,
            alternate_model="model-b",
            alternate_cross_model=True,
        )
        response, windows = self._windows()
        for slug in ("1d", "7d", "30d"):
            for row in windows[slug]["live_chat"]["rows"]:
                self.assertIn("state", row, (slug, row["model_name"]))
        self.assertContains(response, "Live chat side by side")
        self.assertContains(response, "<td>model-b</td>")

    def test_only_metadata_columns_are_read(self):
        self._turn()
        with CaptureQueriesContext(connection) as queries:
            self.client.get(reverse("model_usage_dashboard"))
        sql = "\n".join(q["sql"] for q in queries.captured_queries)
        self.assertNotIn("chat_history", sql)
        self.assertNotIn("summary_for_next_call", sql)

    def test_calls_held_back_and_never_sent_are_not_calls_or_asks(self):
        self._turn(
            backends=["model-a", "claude"],
            external_start="skipped",
            external_delay_seconds=8.0,
            calls=[
                _call("model-a"),
                _call("claude", "skipped", ms=None),
                _call("claude", "skipped", ms=None),
            ],
        )
        self._turn(
            backends=["model-a", "claude"],
            external_start="early",
            external_delay_seconds=8.0,
            calls=[_call("model-a", "error"), _call("claude")],
            winner_model="claude",
            winner_external=True,
        )
        self._turn(external_start="after_delay")
        self._turn(external_start="skipped")
        self._turn(external_start="immediate")
        # The first pass never sent them, but our reply was too short and
        # the retry asked one: not "never sent".
        retried = _call("deepseek", pass_kind="retry")
        retried["external"] = True
        self._turn(
            backends=["model-a", "claude"],
            external_start="skipped",
            calls=[_call("model-a"), _call("claude", "skipped", ms=None), retried],
            retry_ran=True,
        )
        # Nor when our reply asked for a tool and the follow-up pass sent
        # one, with no retry.
        followed_up = _call("deepseek", pass_kind="tool")
        followed_up["external"] = True
        self._turn(
            backends=["model-a", "claude"],
            external_start="skipped",
            calls=[
                _call("model-a"),
                _call("claude", "skipped", ms=None),
                followed_up,
            ],
        )
        response, windows = self._windows()
        rows = self._rows(windows["1d"])
        claude = rows["claude"]
        self.assertEqual((claude["asked"], claude["calls"]), (1, 1))
        self.assertEqual(claude["skipped"], 4)
        self.assertAlmostEqual(claude["win_rate"], 100.0)
        self.assertEqual(rows["model-a"]["skipped"], 0)
        summary = windows["1d"]["live_chat"]["summary"]
        self.assertEqual(
            (
                summary["externals_held_back"],
                summary["externals_after_delay"],
                summary["externals_early"],
                summary["externals_skipped"],
                summary["externals_skipped_later"],
            ),
            (6, 1, 1, 2, 2),
        )
        self.assertContains(response, "Outside models held back while ours answered")
        self.assertContains(response, "asked later in the turn on 2")
        self.assertContains(response, ">Skipped</th>")


def _policy_row(minutes_ago=0, **fields):
    defaults = dict(
        source="manual",
        window_minutes=1440,
        turns_considered=240,
        external_excluded=["claude-sonnet"],
        external_delay_seconds=8.0,
        outside_order=["claude-opus", "new-model", "deepseek"],
        order_scores={"claude-opus": [0.05, 200], "deepseek": [0.0, 180]},
        internal_usable_rate=0.9,
        internal_ttu_p75_ms=8000,
        reason="ok,ordered",
    )
    defaults.update(fields)
    row = ChatRoutingPolicy.objects.create(**defaults)
    if minutes_ago:
        ChatRoutingPolicy.objects.filter(pk=row.pk).update(
            created_at=timezone.now() - datetime.timedelta(minutes=minutes_ago)
        )
    return row


class ChatRoutingPolicyPanelTest(StaffClientMixin, TestCase):
    """The newest chat routing policy, shown once in the Live chat section,
    with whether chat follows it now."""

    def setUp(self):
        self._login_staff()

    def _page(self):
        response = self.client.get(reverse("model_usage_dashboard"))
        self.assertEqual(response.status_code, 200)
        return response

    def test_no_policy_yet(self):
        response = self._page()
        self.assertEqual(response.context["chat_policy"]["state"], "none")
        self.assertContains(response, "No routing policy has been computed yet")

    def test_a_fresh_policy_with_the_switch_off_is_shadow(self):
        _policy_row()
        with override_settings(FHI_CHAT_POLICY_APPLY=False):
            response = self._page()
        self.assertEqual(response.context["chat_policy"]["state"], "shadow")
        self.assertContains(response, "Shadow mode")
        self.assertContains(response, "up to <strong>8.0 s</strong>")
        self.assertContains(response, "<strong>claude-sonnet</strong>")
        self.assertContains(response, "a usable answer on 90.0% of turns")
        self.assertContains(response, "8000 ms")
        self.assertContains(response, "ok,ordered")

    def test_a_fresh_policy_with_the_switch_on_is_applied(self):
        _policy_row()
        with override_settings(FHI_CHAT_POLICY_APPLY=True):
            response = self._page()
        self.assertEqual(response.context["chat_policy"]["state"], "applied")
        self.assertContains(response, "<strong>Applied</strong>")

    def test_the_newest_row_is_the_one_shown(self):
        _policy_row(minutes_ago=30, external_delay_seconds=6.0)
        newest = _policy_row(external_delay_seconds=11.0)
        response = self._page()
        self.assertEqual(response.context["chat_policy"]["row"].pk, newest.pk)
        self.assertContains(response, "up to <strong>11.0 s</strong>")

    def test_a_stale_policy_is_not_followed(self):
        _policy_row(minutes_ago=90)
        with override_settings(
            FHI_CHAT_POLICY_APPLY=True, FHI_CHAT_POLICY_MAX_AGE_MINUTES=60
        ):
            response = self._page()
        self.assertEqual(response.context["chat_policy"]["state"], "stale")
        self.assertContains(response, "Too old to follow")

    def test_an_unreadable_policy_is_not_followed(self):
        _policy_row(schema_version=2)
        with override_settings(FHI_CHAT_POLICY_APPLY=True):
            response = self._page()
        self.assertEqual(response.context["chat_policy"]["state"], "invalid")
        self.assertContains(response, "<strong>Unreadable</strong>")

    def test_the_learned_order_is_listed_with_its_scores(self):
        _policy_row()
        response = self._page()
        self.assertEqual(
            response.context["chat_policy"]["order_rows"],
            [
                {
                    "place": 1,
                    "model": "claude-opus",
                    "score_percent": 5.0,
                    "turns": 200,
                },
                {
                    "place": 2,
                    "model": "new-model",
                    "score_percent": None,
                    "turns": None,
                },
                {"place": 3, "model": "deepseek", "score_percent": 0.0, "turns": 180},
            ],
        )
        self.assertContains(response, "Answer delivered")

    def test_this_months_provider_spend_is_listed(self):
        from fighthealthinsurance.ml import spend

        spend._ledger.reset_for_tests()
        spend.record(spend.TYPESAFE, spend.CHAT, 12_345)
        spend.record(spend.AZURE, spend.CHAT, 7)
        response = self._page()
        rows = response.context["chat_policy"]["spend_rows"]
        self.assertIn(
            {"counter": "typesafe:chat", "amount": 0.012345, "calls": False}, rows
        )
        self.assertIn({"counter": "azure:chat", "amount": 7.0, "calls": True}, rows)
        self.assertContains(response, "Provider spend this month")
        self.assertContains(response, "7 calls")

    def test_the_panel_appears_once_in_the_all_time_section(self):
        _policy_row()
        content = self._page().content.decode()
        self.assertEqual(content.count('id="chat-routing-policy"'), 1)
        all_time = content.split('id="window-global"', 1)[1].split('id="window-1d"', 1)[
            0
        ]
        self.assertIn('id="chat-routing-policy"', all_time)
