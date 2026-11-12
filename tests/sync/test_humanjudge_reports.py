"""HumanJudge's verdicts beside the Arena's own record: the public rankings'
column (counted apart, never part of the rank) and the staff breakdown in
the admin, which only signed-in staff who may view verdicts can read."""

import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse

from fighthealthinsurance import humanjudge_reports, public_rankings
from fighthealthinsurance.models import (
    ChooserCandidate,
    ChooserTask,
    ChooserVote,
    HumanJudgeSubmission,
    HumanJudgeVerdict,
)

BREAKDOWN = reverse("admin:fighthealthinsurance_humanjudgeverdict_breakdown")
FEEDBACK = "Invents a 2019 trial that is not in the case"


def _case(models=("model-a", "model-b"), kind="appeal_letter", task_type="appeal"):
    task = ChooserTask.objects.create(
        task_type=task_type, status="READY", source="synthetic"
    )
    candidates = [
        ChooserCandidate.objects.create(
            task=task,
            candidate_index=i,
            kind=kind,
            model_name=name,
            content=f"Draft {name}",
        )
        for i, name in enumerate(models)
    ]
    return task, candidates


def _vote(task, candidates, chosen, session):
    return ChooserVote.objects.create(
        task=task,
        chosen_candidate=chosen,
        presented_candidate_ids=[c.id for c in candidates],
        session_key=session,
    )


def _verdicts(candidate, *verdicts, feedback=""):
    submission, _ = HumanJudgeSubmission.objects.get_or_create(
        candidate=candidate,
        defaults={
            "task": candidate.task,
            "inference_id": f"gj_inf_{candidate.id}",
            "status": "SENT",
        },
    )
    for n, verdict in enumerate(verdicts):
        HumanJudgeVerdict.objects.create(
            submission=submission,
            task=candidate.task,
            candidate=candidate,
            external_key=f"id:{candidate.id}-{n}-{verdict}",
            verdict=verdict,
            flag_category="Wrong" if verdict == "flag" else "",
            feedback=feedback,
        )


def _appeal_rows(data, window=0):
    table = next(
        t for t in data["windows"][window]["tables"] if t["kind"] == "appeal_letter"
    )
    return {r["model"]: r for r in table["rows"]}


class PublicRankingsHumanJudgeTest(TestCase):
    def setUp(self):
        # Arena voters prefer model-a; HumanJudge passes model-b more.
        self.task, (self.a, self.b) = _case()
        _vote(self.task, [self.a, self.b], self.a, "s1")
        _vote(self.task, [self.a, self.b], self.a, "s2")
        _vote(self.task, [self.a, self.b], self.b, "s3")

    def test_appeal_rows_carry_the_humanjudge_pass_rate(self):
        _verdicts(self.a, "pass", "flag", "flag")
        with mock.patch.object(public_rankings, "PUBLIC_RANKINGS_MIN_SHOWN", 3):
            rows = _appeal_rows(public_rankings.compute_rankings())
        self.assertEqual(
            rows["model-a"]["humanjudge"],
            {"reviews": 3, "passes": 1, "pass_rate": 33.3, "warming_up": False},
        )

    def test_humanjudge_verdicts_never_change_the_rank_or_win_rate(self):
        before = _appeal_rows(public_rankings.compute_rankings())
        _verdicts(self.a, "flag", "flag", "flag")
        _verdicts(self.b, "pass", "pass", "pass")
        after = _appeal_rows(public_rankings.compute_rankings())
        self.assertEqual(
            [(m, r["rank"], r["win_rate"]) for m, r in after.items()],
            [(m, r["rank"], r["win_rate"]) for m, r in before.items()],
        )

    def test_a_model_with_few_reviews_is_warming_up(self):
        _verdicts(self.a, "pass")
        rows = _appeal_rows(public_rankings.compute_rankings())
        self.assertTrue(rows["model-a"]["humanjudge"]["warming_up"])

    def test_chat_tables_have_no_humanjudge_column(self):
        task, (c, d) = _case(kind="chat_response", task_type="chat")
        _vote(task, [c, d], c, "s4")
        data = public_rankings.compute_rankings()
        chat = next(
            t for t in data["windows"][0]["tables"] if t["kind"] == "chat_response"
        )
        self.assertEqual(
            (chat["has_humanjudge"], chat["rows"][0]["humanjudge"]), (False, None)
        )

    def test_reviewer_feedback_never_reaches_the_public_page(self):
        _verdicts(self.a, "flag", feedback=FEEDBACK)
        response = self.client.get(reverse("chooser_rankings"), {"format": "json"})
        self.assertNotIn(FEEDBACK, response.content.decode())

    def test_the_page_shows_the_outside_reviewers_column(self):
        _verdicts(self.a, "pass")
        response = self.client.get(reverse("chooser_rankings"))
        self.assertContains(response, "Outside reviewers passed")


class BreakdownTest(TestCase):
    def setUp(self):
        self.task, (self.a, self.b) = _case()

    def test_model_rows_count_verdicts_by_category(self):
        _verdicts(self.a, "pass", "flag", "flag")
        row = next(
            r for r in humanjudge_reports.breakdown()["models"] if r["model"] == "model-a"
        )
        self.assertEqual(
            (row["reviews"], row["passes"], row["flag_categories"]),
            (3, 1, {"Wrong": 2}),
        )

    def test_agreement_with_the_arena_counts_cases_both_picked(self):
        self.task.scoring_json = {
            "humanjudge": {"candidate_id": self.a.id},
            "jev": {"candidate_id": self.b.id},
        }
        self.task.save()
        _vote(self.task, [self.a, self.b], self.a, "s1")
        agreement = {
            a["judge"]: (a["cases"], a["agree"])
            for a in humanjudge_reports.breakdown()["agreement"]
        }
        self.assertEqual(
            agreement,
            {"Arena voters": (1, 1), "Jev": (1, 0), "fhi-local": (0, 0)},
        )

    def test_a_tied_arena_has_no_winner_to_agree_with(self):
        self.task.scoring_json = {"humanjudge": {"candidate_id": self.a.id}}
        self.task.save()
        _vote(self.task, [self.a, self.b], self.a, "s1")
        _vote(self.task, [self.a, self.b], self.b, "s2")
        arena = humanjudge_reports.breakdown()["agreement"][0]
        self.assertEqual(arena["cases"], 0)


class BreakdownAdminAccessTest(TestCase):
    def setUp(self):
        task, (a, _) = _case()
        _verdicts(a, "flag", feedback=FEEDBACK)
        self.users = get_user_model()

    def _staff(self, **perms):
        user = self.users.objects.create_user(
            username="staffer", password="pw123", email="s@example.com", is_staff=True
        )
        if perms.get("view"):
            user.user_permissions.add(
                Permission.objects.get(codename="view_humanjudgeverdict")
            )
        self.client.force_login(user)

    def test_anonymous_is_sent_to_the_admin_login(self):
        response = self.client.get(BREAKDOWN, {"format": "json"})
        self.assertEqual(
            (response.status_code, "/login/" in response["Location"]), (302, True)
        )

    def test_a_signed_in_non_staff_user_is_sent_to_the_admin_login(self):
        user = self.users.objects.create_user(
            username="patient", password="pw123", email="p@example.com"
        )
        self.client.force_login(user)
        response = self.client.get(BREAKDOWN, {"format": "json"})
        self.assertNotContains(response, FEEDBACK, status_code=302)

    def test_staff_without_view_permission_is_refused(self):
        self._staff()
        self.assertEqual(
            self.client.get(BREAKDOWN, {"format": "json"}).status_code, 403
        )

    def test_staff_with_view_permission_read_the_json(self):
        self._staff(view=True)
        response = self.client.get(BREAKDOWN, {"format": "json"})
        self.assertEqual(
            json.loads(response.content)["recent_feedback"][0]["feedback"], FEEDBACK
        )

    def test_the_json_is_not_cached(self):
        self._staff(view=True)
        response = self.client.get(BREAKDOWN, {"format": "json"})
        self.assertIn("no-store", response["Cache-Control"])

    def test_superuser_sees_the_page(self):
        admin = self.users.objects.create_superuser(
            username="admin", password="pw123", email="admin@example.com"
        )
        self.client.force_login(admin)
        self.assertContains(self.client.get(BREAKDOWN), 'id="humanjudge-models"')
