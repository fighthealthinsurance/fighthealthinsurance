"""Tests for the ML Model Usage staff dashboard."""

import datetime
import json
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from fighthealthinsurance.ml.model_identity import (
    LEGACY_UNATTRIBUTED_LABEL,
    SYNTHESIZED_MODEL_NAME,
    legacy_unresolved_label,
)
from fighthealthinsurance.models import (
    ChooserCandidate,
    ChooserTask,
    ChooserVote,
    Denial,
    ProposedAppeal,
)
from fighthealthinsurance.ml import letter_quality
from fighthealthinsurance.staff_views import (
    MODEL_USAGE_VIEWS,
    UNKNOWN_MODEL_LABEL,
    ModelUsageDashboardView,
    _calendar_windows,
    _merge_stats,
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


def _utc(*args):
    return datetime.datetime(*args, tzinfo=datetime.timezone.utc)


class CalendarWindowsTest(SimpleTestCase):
    """The period arithmetic behind the monthly and quarterly views."""

    NOW = _utc(2026, 2, 10, 12)

    def test_months_run_newest_first_across_a_year_boundary(self):
        windows = _calendar_windows("monthly", self.NOW, _utc(2025, 11, 3), 12)
        self.assertEqual(
            [w[0] for w in windows],
            ["m-2026-02", "m-2026-01", "m-2025-12", "m-2025-11"],
        )

    def test_the_current_month_is_labelled_to_date(self):
        windows = _calendar_windows("monthly", self.NOW, _utc(2025, 11, 3), 12)
        self.assertEqual(
            [w[1] for w in windows[:2]], ["February 2026 (to date)", "January 2026"]
        )

    def test_a_month_runs_from_its_first_midnight_to_the_next(self):
        windows = _calendar_windows("monthly", self.NOW, _utc(2025, 11, 3), 12)
        december = {w[0]: w for w in windows}["m-2025-12"]
        self.assertEqual(december[2:], (_utc(2025, 12, 1), _utc(2026, 1, 1)))

    def test_quarters_run_newest_first_across_a_year_boundary(self):
        windows = _calendar_windows("quarterly", self.NOW, _utc(2025, 5, 20), 8)
        self.assertEqual(
            [w[0] for w in windows],
            ["q-2026-1", "q-2025-4", "q-2025-3", "q-2025-2"],
        )

    def test_quarter_labels_name_their_months(self):
        windows = _calendar_windows("quarterly", self.NOW, _utc(2025, 5, 20), 8)
        self.assertEqual(
            [w[1] for w in windows[:2]],
            ["2026 Q1 (Jan\u2013Mar, to date)", "2025 Q4 (Oct\u2013Dec)"],
        )

    def test_a_quarter_runs_from_its_first_midnight_to_the_next(self):
        windows = _calendar_windows("quarterly", self.NOW, _utc(2025, 5, 20), 8)
        self.assertEqual(windows[1][2:], (_utc(2025, 10, 1), _utc(2026, 1, 1)))

    def test_the_view_is_capped_at_max_periods(self):
        windows = _calendar_windows("monthly", self.NOW, _utc(2020, 1, 1), 12)
        self.assertEqual(len(windows), 12)

    def test_with_nothing_stored_only_the_current_period_is_listed(self):
        windows = _calendar_windows("quarterly", self.NOW, None, 8)
        self.assertEqual([w[0] for w in windows], ["q-2026-1"])

    def test_a_future_earliest_still_lists_the_current_period(self):
        windows = _calendar_windows("monthly", self.NOW, _utc(2026, 5, 1), 12)
        self.assertEqual([w[0] for w in windows], ["m-2026-02"])

    def test_boundaries_follow_the_current_timezone(self):
        """Late on the last day of February in New York it is already March
        in UTC; the view must still call it February, starting and ending at
        New York midnights."""
        with timezone.override(ZoneInfo("America/New_York")):
            windows = _calendar_windows(
                "monthly", _utc(2026, 3, 1, 3), _utc(2026, 2, 5), 12
            )
        self.assertEqual(
            windows[0], windows[0][:2] + (_utc(2026, 2, 1, 5), _utc(2026, 3, 1, 5))
        )
        self.assertEqual(windows[0][0], "m-2026-02")


class UsageStatsUpperBoundTest(ChooserStatsHelperMixin, TestCase):
    """A calendar period closes at ``until``: a pick or vote at that instant
    belongs to the next period, one just before it to this one."""

    SINCE = _utc(2026, 8, 1)
    UNTIL = _utc(2026, 9, 1)
    JUST_BEFORE_UNTIL = UNTIL - datetime.timedelta(seconds=1)

    def setUp(self):
        self.denial = Denial.objects.create(
            hashed_email="hash",
            denial_text="denied",
            procedure="MRI",
            diagnosis="back pain",
            insurance_company="TestIns",
        )

    def _vote_at(self, model_name, when, session):
        task = self._make_task()
        cand = self._make_candidate(task, 0, model_name)
        vote = self._vote(task, cand, [cand], session=session)
        ChooserVote.objects.filter(pk=vote.pk).update(created_at=when)

    def _pick_at(self, model_name, when):
        pick = ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text=f"pick-{model_name}",
            chosen=True,
            model_name=model_name,
            context_level="full",
        )
        ProposedAppeal.objects.filter(pk=pick.pk).update(created_at=when)

    def test_chooser_votes_are_bounded_on_both_sides(self):
        self._vote_at("before", self.SINCE - datetime.timedelta(seconds=1), "s1")
        self._vote_at("first-instant", self.SINCE, "s2")
        self._vote_at("last-instant", self.JUST_BEFORE_UNTIL, "s3")
        self._vote_at("next-period", self.UNTIL, "s4")
        rows = ModelUsageDashboardView._chooser_stats(
            "appeal_letter", self.SINCE, self.UNTIL
        )
        self.assertEqual(
            {r["model_name"] for r in rows}, {"first-instant", "last-instant"}
        )

    def test_denial_flow_picks_stop_at_the_period_end(self):
        self._pick_at("in-period", self.JUST_BEFORE_UNTIL)
        self._pick_at("next-period", self.UNTIL)
        rows = ModelUsageDashboardView._proposed_appeal_stats(self.SINCE, self.UNTIL)
        self.assertEqual({r["model_name"] for r in rows}, {"in-period"})

    def test_context_levels_stop_at_the_period_end(self):
        self._pick_at("in-period", self.JUST_BEFORE_UNTIL)
        self._pick_at("next-period", self.UNTIL)
        rows = ModelUsageDashboardView._context_level_stats(self.SINCE, self.UNTIL)
        self.assertEqual(sum(r["chosen"] for r in rows), 1)

    def test_draft_quality_stops_at_the_period_end(self):
        for name, when in (
            ("in-period", self.JUST_BEFORE_UNTIL),
            ("next-period", self.UNTIL),
        ):
            draft = ProposedAppeal.objects.create(
                for_denial=self.denial, appeal_text=f"draft-{name}", model_name=name
            )
            ProposedAppeal.objects.filter(pk=draft.pk).update(
                created_at=when,
                quality_score=0.5,
                grounding_score=2.0,
                quality_scorer=letter_quality.SCORER,
                quality_scored_at=when,
            )
        stats = ModelUsageDashboardView._draft_quality_stats(self.SINCE, self.UNTIL)
        self.assertEqual(set(stats), {"in-period"})


class ModelUsageDashboardCalendarViewTest(ChooserStatsHelperMixin, TestCase):
    """The View dropdown: rolling windows by default, or calendar months or
    quarters."""

    def setUp(self):
        User.objects.create_user(username="staff", password="pw123", is_staff=True)
        self.client.login(username="staff", password="pw123")

    def _vote_at(self, model_name, when, session):
        task = self._make_task()
        cand = self._make_candidate(task, 0, model_name)
        vote = self._vote(task, cand, [cand], session=session)
        ChooserVote.objects.filter(pk=vote.pk).update(created_at=when)

    def _get(self, view):
        return self.client.get(reverse("model_usage_dashboard"), {"view": view})

    @staticmethod
    def _sections(response):
        return [
            (w["slug"], {r["model_name"] for r in w["chooser_appeal"]})
            for w in response.context["windows"]
        ]

    def test_the_dropdown_offers_every_view(self):
        response = self.client.get(reverse("model_usage_dashboard"))
        for value, label in MODEL_USAGE_VIEWS:
            self.assertContains(response, f'<option value="{value}"')

    def test_the_dropdown_marks_the_selected_view(self):
        response = self._get("quarterly")
        self.assertContains(response, '<option value="quarterly" selected>')

    def test_an_unknown_view_falls_back_to_the_rolling_windows(self):
        response = self._get("weekly")
        self.assertEqual(
            [w["slug"] for w in response.context["windows"]],
            ["global", "1d", "7d", "30d"],
        )

    def test_the_monthly_view_puts_each_vote_in_its_calendar_month(self):
        this_month = timezone.localtime().replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        last_month = this_month - relativedelta(months=1)
        self._vote_at("this-month", timezone.now(), "s1")
        self._vote_at("last-month", last_month + datetime.timedelta(days=1), "s2")

        response = self._get("monthly")

        self.assertEqual(
            self._sections(response),
            [
                (f"m-{this_month:%Y-%m}", {"this-month"}),
                (f"m-{last_month:%Y-%m}", {"last-month"}),
            ],
        )

    def test_the_quarterly_view_puts_each_vote_in_its_calendar_quarter(self):
        now = timezone.localtime()
        this_quarter = now.replace(
            month=(now.month - 1) // 3 * 3 + 1,
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        last_quarter = this_quarter - relativedelta(months=3)
        self._vote_at("this-quarter", timezone.now(), "s1")
        self._vote_at("last-quarter", last_quarter + datetime.timedelta(days=1), "s2")

        response = self._get("quarterly")

        def slug(start):
            return f"q-{start.year}-{(start.month - 1) // 3 + 1}"

        self.assertEqual(
            self._sections(response),
            [
                (slug(this_quarter), {"this-quarter"}),
                (slug(last_quarter), {"last-quarter"}),
            ],
        )

    def test_the_current_period_is_labelled_to_date(self):
        response = self._get("monthly")
        self.assertTrue(response.context["windows"][0]["label"].endswith("(to date)"))

    def test_the_note_describes_the_calendar_view(self):
        response = self._get("quarterly")
        self.assertContains(response, "Periods are calendar")
