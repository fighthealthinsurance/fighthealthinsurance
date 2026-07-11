"""Tests for the ML Model Usage staff dashboard."""

import datetime
import json

from django.contrib.auth import get_user_model
from django.test import TestCase
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
