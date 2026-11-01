"""The public Appeal Arena rankings page: synthetic votes only, aggregate
conditions and treatments only, nothing about any one person."""

import datetime
import json
from unittest import mock

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from fighthealthinsurance import condition_rankings as cr
from fighthealthinsurance import public_rankings
from fighthealthinsurance.models import (
    ChooserCandidate,
    ChooserSkip,
    ChooserTask,
    ChooserVote,
    Denial,
    MedicalTermCheck,
    ProposedAppeal,
)


def _store_counts():
    """What a refresh stores for the page, without its outside lookups: the
    people count of every k-anonymous term (synchronously, on the test's
    own connection)."""
    now = timezone.now()
    for kind in cr.KINDS:
        cr._record_counts(kind, cr.count_terms(kind), now)


def _task(source="synthetic", models=("model-a", "model-b")):
    task = ChooserTask.objects.create(task_type="appeal", status="READY", source=source)
    candidates = [
        ChooserCandidate.objects.create(
            task=task,
            candidate_index=i,
            kind="appeal_letter",
            model_name=name,
            content=f"Draft {name}",
        )
        for i, name in enumerate(models)
    ]
    return task, candidates


def _vote(task, candidates, chosen, session, flagged=()):
    return ChooserVote.objects.create(
        task=task,
        chosen_candidate=chosen,
        presented_candidate_ids=[c.id for c in candidates],
        flagged_candidate_ids=[c.id for c in flagged],
        session_key=session,
    )


class ChooserRankingsPageTest(TestCase):
    def setUp(self):
        # Synthetic: model-a wins twice, model-b once and is flagged once.
        task, (a, b) = _task()
        _vote(task, [a, b], a, "session-secret-1")
        _vote(task, [a, b], a, "session-secret-2", flagged=[b])
        _vote(task, [a, b], b, "session-secret-3")
        ChooserSkip.objects.create(task=task, session_key="session-secret-4")
        # Not synthetic: a task built from something else must not count.
        other, (c, d) = _task(source="real_denial", models=("model-real", "model-b"))
        _vote(other, [c, d], c, "session-secret-5")

    def test_anonymous_can_read_the_page(self):
        response = self.client.get(reverse("chooser_rankings"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Appeal Arena Rankings")
        self.assertContains(response, "model-a")

    def test_only_synthetic_votes_are_counted(self):
        data = public_rankings.compute_rankings()

        all_time = data["windows"][0]
        appeals = all_time["tables"][0]["rows"]
        by_model = {r["model"]: r for r in appeals}
        self.assertNotIn("model-real", by_model)
        self.assertEqual(by_model["model-a"]["won"], 2)
        self.assertEqual(by_model["model-a"]["shown"], 3)
        self.assertEqual(by_model["model-b"]["won"], 1)
        self.assertEqual(by_model["model-b"]["shown"], 3)
        self.assertEqual(by_model["model-b"]["flagged"], 1)
        self.assertAlmostEqual(by_model["model-b"]["flag_rate"], 100.0 / 3)
        self.assertEqual(data["totals"]["votes"], 3)
        self.assertEqual(data["totals"]["tasks_judged"], 1)

    def test_nothing_about_a_session_or_a_person_reaches_the_page(self):
        Denial.objects.create(
            hashed_email="hash-of-a-real-person",
            denial_text="the whole letter",
            procedure="A very rare procedure",
            diagnosis="A very rare condition",
        )
        ProposedAppeal.objects.create(appeal_text="real patient text")

        body = self.client.get(reverse("chooser_rankings")).content.decode()

        for secret in (
            "session-secret",
            "hash-of-a-real-person",
            "very rare",
            "the whole letter",
            "real patient text",
        ):
            self.assertNotIn(secret, body)

    def test_json_format(self):
        response = self.client.get(reverse("chooser_rankings") + "?format=json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertIn("public", response["Cache-Control"])
        data = json.loads(response.content)
        self.assertEqual(data["totals"]["votes"], 3)
        self.assertEqual([w["slug"] for w in data["windows"]], ["all", "30d"])

    def test_votes_cast_before_blind_voting_began_are_left_out(self):
        """A vote from before the blind payload shipped was cast with the
        model name in the JSON; it does not belong in a leaderboard that
        says a vote is for the letter."""
        floor = public_rankings.blind_since()
        self.assertIsNotNone(floor)
        task, (a, b) = _task(models=("model-old", "model-older"))
        old = _vote(task, [a, b], a, "session-secret-old")
        ChooserVote.objects.filter(pk=old.pk).update(
            created_at=floor - datetime.timedelta(days=1)
        )

        data = public_rankings.compute_rankings()

        rows = data["windows"][0]["tables"][0]["rows"]
        self.assertNotIn("model-old", {r["model"] for r in rows})
        self.assertEqual(data["totals"]["votes"], 3)
        self.assertEqual(data["blind_since"], floor.isoformat())

    def test_an_empty_since_setting_counts_every_vote(self):
        task, (a, b) = _task(models=("model-old", "model-older"))
        old = _vote(task, [a, b], a, "session-secret-old")
        ChooserVote.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - datetime.timedelta(days=400)
        )
        # The module reads the setting at import time, so patch the module.
        with mock.patch.object(public_rankings, "PUBLIC_RANKINGS_SINCE", ""):
            self.assertIsNone(public_rankings.blind_since())
            data = public_rankings.compute_rankings()

        self.assertEqual(data["totals"]["votes"], 4)
        all_time = {r["model"] for r in data["windows"][0]["tables"][0]["rows"]}
        self.assertIn("model-old", all_time)

    def test_a_malformed_since_setting_counts_no_votes(self):
        """A bad override must not 500 the page, and must not count votes
        that may predate blind voting: it fails closed."""
        before = timezone.now()
        with mock.patch.object(public_rankings, "PUBLIC_RANKINGS_SINCE", "yesterday"):
            floor = public_rankings.blind_since()
            data = public_rankings.compute_rankings()

        self.assertIsNotNone(floor)
        self.assertGreaterEqual(floor, before)
        self.assertEqual(data["totals"]["votes"], 0)

    def test_no_migration_record_counts_no_votes(self):
        with mock.patch(
            "django.db.migrations.recorder.MigrationRecorder.Migration.objects"
        ) as objects:
            objects.filter.return_value.values_list.return_value.first.return_value = None
            floor = public_rankings.blind_since()
            data = public_rankings.compute_rankings()

        self.assertIsNotNone(floor)
        self.assertEqual(data["totals"]["votes"], 0)
        self.assertEqual(data["windows"][0]["tables"][0]["rows"], [])

    def test_models_with_too_few_votes_are_warming_up_and_sort_last(self):
        task, (e, f) = _task(models=("model-e", "model-f"))
        for i in range(public_rankings.PUBLIC_RANKINGS_MIN_SHOWN):
            _vote(task, [e, f], f, f"warm-{i}")

        rows = public_rankings.compute_rankings()["windows"][0]["tables"][0]["rows"]

        ranked = [r for r in rows if not r["warming_up"]]
        warming = [r for r in rows if r["warming_up"]]
        self.assertEqual([r["model"] for r in ranked], ["model-f", "model-e"])
        self.assertEqual({r["model"] for r in warming}, {"model-a", "model-b"})
        self.assertEqual([r["rank"] for r in rows], [1, 2, 3, 4])


class ChooserAllBadRankingsTest(TestCase):
    def test_all_bad_reviews_count_as_shown_with_no_wins(self):
        task, candidates = _task()
        ChooserVote.objects.create(
            task=task,
            chosen_candidate=None,
            presented_candidate_ids=[candidate.id for candidate in candidates],
            all_bad=True,
            rationale="Neither is safe to send.",
            session_key="all-bad-session",
        )

        data = public_rankings.compute_rankings()
        rows = data["windows"][0]["tables"][0]["rows"]

        self.assertEqual(
            {row["model"]: (row["shown"], row["won"]) for row in rows},
            {"model-a": (1, 0), "model-b": (1, 0)},
        )
        self.assertEqual(data["totals"]["votes"], 1)
        self.assertNotIn("Neither is safe", json.dumps(data))


class ChooserRankingsLeaderTest(TestCase):
    """Who is winning: each table names its leader, the first model past
    warming up, and the page says so at the top rather than leaving the
    reader to work it out from the table."""

    def setUp(self):
        task, (a, b) = _task()
        _vote(task, [a, b], a, "session-leader-1")
        _vote(task, [a, b], a, "session-leader-2", flagged=[b])
        _vote(task, [a, b], b, "session-leader-3")

    def test_no_leader_while_every_model_is_warming_up(self):
        data = public_rankings.compute_rankings()

        appeals = data["windows"][0]["tables"][0]
        self.assertTrue(all(r["warming_up"] for r in appeals["rows"]))
        self.assertIsNone(appeals["leader"])
        self.assertEqual(data["leaders"], {"appeal_letter": None, "chat_response": None})

    def test_the_first_ranked_model_leads_and_the_json_repeats_it(self):
        with mock.patch.object(public_rankings, "PUBLIC_RANKINGS_MIN_SHOWN", 3):
            data = public_rankings.compute_rankings()

        appeals = data["windows"][0]["tables"][0]
        self.assertEqual(appeals["leader"]["model"], "model-a")
        self.assertEqual(appeals["leader"]["rank"], 1)
        self.assertAlmostEqual(appeals["leader"]["win_rate"], 200.0 / 3)
        self.assertEqual(data["leaders"]["appeal_letter"]["model"], "model-a")
        # No chat replies have been judged: that table has rows to show
        # nothing for, and no leader.
        self.assertIsNone(data["windows"][0]["tables"][1]["leader"])
        self.assertIsNone(data["leaders"]["chat_response"])

    def test_a_warming_up_model_never_leads_however_high_its_rate(self):
        """A model on one unbeaten vote is not winning anything yet."""
        task, (c, d) = _task(models=("model-c", "model-d"))
        _vote(task, [c, d], c, "session-leader-4")
        with mock.patch.object(public_rankings, "PUBLIC_RANKINGS_MIN_SHOWN", 3):
            data = public_rankings.compute_rankings()

        rows = data["windows"][0]["tables"][0]["rows"]
        by_model = {r["model"]: r for r in rows}
        self.assertEqual(by_model["model-c"]["win_rate"], 100.0)
        self.assertTrue(by_model["model-c"]["warming_up"])
        self.assertEqual(data["leaders"]["appeal_letter"]["model"], "model-a")

    def test_only_ranked_models_count_as_below_the_leader(self):
        """A warming-up model is listed, not ranked, so the leader is not
        "ahead" of it yet."""
        task, (c, d) = _task(models=("model-c", "model-d"))
        _vote(task, [c, d], c, "session-leader-5")
        with mock.patch.object(public_rankings, "PUBLIC_RANKINGS_MIN_SHOWN", 3):
            data = public_rankings.compute_rankings()
            body = self.client.get(reverse("chooser_rankings")).content.decode()

        appeals = data["windows"][0]["tables"][0]
        self.assertEqual(len(appeals["rows"]), 4)
        self.assertEqual(appeals["ranked"], 2)
        card = body[body.index('id="leader-appeal_letter"') : body.index('id="leader-chat_response"')]
        self.assertIn("1 other model ranked below it", card)

    def test_the_page_opens_with_who_is_winning(self):
        with mock.patch.object(public_rankings, "PUBLIC_RANKINGS_MIN_SHOWN", 3):
            body = self.client.get(reverse("chooser_rankings")).content.decode()

        self.assertIn('href="#leaders">Who is winning</a>', body)
        card = body[body.index('id="leader-appeal_letter"') : body.index('id="leader-chat_response"')]
        self.assertIn("model-a", card)
        self.assertIn("67%", card)
        self.assertIn("2 of the 3 votes", card)
        self.assertIn("1 other model ranked below it", card)
        # The leader's row is marked in the table, and the win rate also
        # draws as a bar so the standings read at a glance.
        self.assertIn('class="arena-leading"', body)
        self.assertIn("Leading: model-a", body)
        self.assertIn('style="width: 67%"', body)
        self.assertLess(body.index('id="leaders"'), body.index('id="window-all"'))

    def test_the_page_says_when_nobody_leads_yet(self):
        body = self.client.get(reverse("chooser_rankings")).content.decode()

        card = body[body.index('id="leader-appeal_letter"') : body.index('id="leader-chat_response"')]
        self.assertIn("No leader yet", card)
        self.assertIn("still warming up", card)
        self.assertIn(reverse("chooser"), card)
        self.assertNotIn('class="arena-leading"', body)
        chat = body[body.index('id="leader-chat_response"') : body.index('class="fhi-notice')]
        self.assertIn("No votes have been cast yet", chat)

    def test_the_json_carries_the_leaders(self):
        with mock.patch.object(public_rankings, "PUBLIC_RANKINGS_MIN_SHOWN", 3):
            response = self.client.get(reverse("chooser_rankings") + "?format=json")

        data = json.loads(response.content)
        self.assertEqual(data["leaders"]["appeal_letter"]["model"], "model-a")
        self.assertEqual(data["windows"][0]["tables"][0]["leader"]["model"], "model-a")


class ChooserRankingsTermsTest(TestCase):
    def setUp(self):
        for i in range(7):
            Denial.objects.create(
                hashed_email=f"person-{i}",
                denial_text="denied",
                procedure="MRI",
                diagnosis="Lower back pain",
            )
        for i in range(6):
            Denial.objects.create(
                hashed_email=f"other-{i}",
                denial_text="denied",
                procedure="Something unvouched",
                diagnosis="Jane Doe",
            )
        now = timezone.now()
        MedicalTermCheck.objects.create(
            term="mri", kind=cr.KIND_TREATMENT, is_real=True, display="MRI", checked_at=now
        )
        MedicalTermCheck.objects.create(
            term="lower back pain", kind=cr.KIND_CONDITION, is_real=True, checked_at=now
        )
        MedicalTermCheck.objects.create(
            term="jane doe", kind=cr.KIND_CONDITION, is_real=False, checked_at=now
        )
        # The page reads the counts a refresh stored, never the denials.
        _store_counts()

    def test_only_vouched_for_terms_with_rounded_counts(self):
        data = public_rankings.compute_rankings()

        self.assertEqual(
            [(r["name"], r["count"], r["share"]) for r in data["conditions"]],
            [("Lower back pain", 5, 100)],
        )
        self.assertEqual(
            [(r["name"], r["count"]) for r in data["treatments"]], [("MRI", 5)]
        )
        self.assertEqual(data["k_anonymity"], cr.CONDITION_RANKING_MIN_PEOPLE)

    def test_bar_widths_come_from_the_rounded_counts(self):
        """Two terms shown as "5+" must get the same bar: a width from the
        true counts would tell them apart."""
        for i in range(9):
            Denial.objects.create(
                hashed_email=f"knee-{i}",
                denial_text="denied",
                procedure="MRI",
                diagnosis="Knee pain",
            )
        MedicalTermCheck.objects.create(
            term="knee pain", kind=cr.KIND_CONDITION, is_real=True, checked_at=timezone.now()
        )
        _store_counts()

        rows = public_rankings.compute_rankings()["conditions"]

        self.assertEqual([(r["count"], r["share"]) for r in rows], [(5, 100), (5, 100)])

    def test_the_page_shows_the_terms_and_not_the_rejected_ones(self):
        body = self.client.get(reverse("chooser_rankings")).content.decode()

        self.assertIn("Lower back pain", body)
        self.assertIn("5+", body)
        self.assertNotIn("Jane Doe", body)
        self.assertNotIn("Something unvouched", body)


class ChooserRankingsModelLabelTest(TestCase):
    """The page names a model without its provider, and counts every
    provider of one model as a single row."""

    def test_public_model_label_strips_the_provider_prefix(self):
        self.assertEqual(
            public_rankings.public_model_label("lunaroute/deepseek-4.1-flash"),
            "deepseek-4.1-flash",
        )

    def test_public_model_label_leaves_a_bare_name_alone(self):
        self.assertEqual(
            public_rankings.public_model_label("deepseek-4.1-flash"),
            "deepseek-4.1-flash",
        )

    def test_public_model_label_of_nothing_is_none(self):
        self.assertIsNone(public_rankings.public_model_label("  "))

    def test_providers_of_one_model_share_a_row(self):
        task, (a, b) = _task(models=("lunaroute/deepseek-4.1-flash", "model-b"))
        _vote(task, [a, b], a, "label-1")
        other, (c, d) = _task(models=("deepseek-4.1-flash", "model-b"))
        _vote(other, [c, d], d, "label-2")

        rows = public_rankings.compute_rankings()["windows"][0]["tables"][0]["rows"]

        by_model = {r["model"]: (r["won"], r["shown"]) for r in rows}
        self.assertEqual(
            by_model, {"deepseek-4.1-flash": (1, 2), "model-b": (1, 2)}
        )

    def test_flags_of_provider_variants_share_a_row(self):
        task, (a, b) = _task(models=("lunaroute/deepseek-4.1-flash", "model-b"))
        _vote(task, [a, b], b, "label-4", flagged=[a])
        other, (c, d) = _task(models=("deepseek-4.1-flash", "model-b"))
        _vote(other, [c, d], d, "label-5", flagged=[c])

        rows = public_rankings.compute_rankings()["windows"][0]["tables"][0]["rows"]

        merged = {r["model"]: r for r in rows}["deepseek-4.1-flash"]
        self.assertEqual((merged["flagged"], merged["flag_rate"]), (2, 100.0))

    def test_tie_credit_of_provider_variants_merges_before_rounding(self):
        """Two thirds-shares, one per provider, are 0.67 together; rounding
        each provider's 0.33 first would report 0.66."""
        for i, name in enumerate(("lunaroute/deepseek-4.1-flash", "deepseek-4.1-flash")):
            task, cands = _task(models=(name, "model-b", "model-c"))
            vote = _vote(task, cands, cands[0], f"tie-{i}")
            vote.tied_candidate_ids = [c.id for c in cands]
            vote.save()

        rows = public_rankings.compute_rankings()["windows"][0]["tables"][0]["rows"]

        self.assertEqual({r["model"]: r for r in rows}["deepseek-4.1-flash"]["won"], 0.67)

    def test_the_page_never_shows_the_provider(self):
        task, (a, b) = _task(models=("lunaroute/deepseek-4.1-flash", "model-b"))
        _vote(task, [a, b], a, "label-3")

        body = self.client.get(reverse("chooser_rankings")).content.decode()

        self.assertIn("deepseek-4.1-flash", body)
        self.assertNotIn("lunaroute", body)


class ChooserRankingsTermLimitTest(TestCase):
    def test_each_list_shows_at_most_the_term_limit(self):
        now = timezone.now()
        limit = cr.CONDITION_RANKING_LIMIT
        for n in range(limit + 2):
            MedicalTermCheck.objects.create(
                term=f"condition {chr(97 + n)}",
                kind=cr.KIND_CONDITION,
                is_real=True,
                people=cr.CONDITION_RANKING_MIN_PEOPLE,
                checked_at=now,
                counted_at=now,
            )

        rows = public_rankings.compute_rankings()["conditions"]

        self.assertEqual(len(rows), limit)
