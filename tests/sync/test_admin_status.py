"""Tests for the staff system-status dashboard (AdminStatusView) and the
fax/model health helpers it relies on."""

import datetime
import os
import tempfile
import threading
import time
from unittest import mock

import requests
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from fighthealthinsurance.models import (
    Denial,
    ExternalServiceHealth,
    FaxesToSend,
    ProposedAppeal,
)

User = get_user_model()


class _FakeBackend:
    """A non-Sonic fax backend stand-in (never actively probed)."""

    professional = False

    def check_health(self) -> bool:  # pragma: no cover - should not be called
        return True


# Patch targets for the lazily-imported subsystem checks the view calls.
_MODELS = "fighthealthinsurance.ml.health_status.compute_model_health_details"
_ACTORS = "fighthealthinsurance.actor_health_status.check_actor_health"
_FAX = "fighthealthinsurance.fax_health_status.check_fax_backends_health"


def _ok_fax_backends():
    return {
        "backends": [
            {
                "name": "SonicFax",
                "ok": True,
                "professional": False,
                "probed": True,
                "error": None,
            }
        ],
        "total_backends": 1,
        "sonic": {"configured": True, "active": True, "ok": True, "error": None},
    }


class AdminStatusAccessTest(TestCase):
    def test_anonymous_user_redirected(self):
        # staff_member_required redirects anonymous users to the admin login.
        response = self.client.get(reverse("admin_status"))
        self.assertEqual(response.status_code, 302)

    def test_authenticated_non_staff_user_redirected(self):
        # An authenticated but non-staff user is also bounced (not just anon).
        User.objects.create_user(username="plain", password="pw123", is_staff=False)
        self.client.login(username="plain", password="pw123")
        response = self.client.get(reverse("admin_status"))
        self.assertEqual(response.status_code, 302)

    @mock.patch(_FAX)
    @mock.patch(_ACTORS)
    @mock.patch(_MODELS)
    def test_staff_user_gets_200_and_renders_sections(
        self, mock_models, mock_actors, mock_fax
    ):
        mock_models.return_value = [
            {"name": "fhi-2025", "ok": True, "external": False, "error": None}
        ]
        mock_actors.return_value = {
            "alive_actors": 6,
            "total_actors": 6,
            "details": [
                {"name": "fax_polling_actor", "alive": True, "error": None},
            ],
        }
        mock_fax.return_value = _ok_fax_backends()

        User.objects.create_user(username="staff", password="pw123", is_staff=True)
        self.client.login(username="staff", password="pw123")

        response = self.client.get(reverse("admin_status"))
        self.assertEqual(response.status_code, 200)
        # All major sections present.
        self.assertContains(response, "System Status")
        self.assertContains(response, "ML Model Backends")
        self.assertContains(response, "Ray Polling Actors")
        self.assertContains(response, "Fax Backends")
        self.assertContains(response, "Fax Queue")
        self.assertContains(response, "External Storage")
        self.assertContains(response, "Letter scoring")
        # Surfaced details from the mocked subsystems.
        self.assertContains(response, "fhi-2025")
        self.assertContains(response, "fax_polling_actor")
        # Sonic working badge.
        self.assertContains(response, "WORKING")


class AdminStatusFaxQueueTest(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="staff", password="pw123", is_staff=True
        )
        self.client.login(username="staff", password="pw123")

    def _make_fax(self, date=None, **kwargs):
        defaults = dict(
            hashed_email="h", paid=True, email="a@b.com", appeal_text="x", name="Test"
        )
        defaults.update(kwargs)
        fax = FaxesToSend.objects.create(**defaults)
        if date is not None:
            # date is auto_now_add, so backdate via update() to bypass it.
            FaxesToSend.objects.filter(pk=fax.pk).update(date=date)
        return fax

    @mock.patch(_FAX)
    @mock.patch(_ACTORS)
    @mock.patch(_MODELS)
    def test_fax_queue_counts(self, mock_models, mock_actors, mock_fax):
        mock_models.return_value = []
        mock_actors.return_value = {
            "alive_actors": 0,
            "total_actors": 6,
            "details": [],
        }
        mock_fax.return_value = _ok_fax_backends()

        now = timezone.now()
        two_hours_ago = now - datetime.timedelta(hours=2)

        # A: queued and due (older than 1h).
        self._make_fax(should_send=True, sent=False, date=two_hours_ago)
        # B: queued but recent (not yet due).
        self._make_fax(should_send=True, sent=False)
        # C: awaiting confirmation (not marked should_send).
        self._make_fax(should_send=False, sent=False)
        # D: in-flight / attempting to send.
        self._make_fax(should_send=True, sent=False, attempting_to_send_as_of=now)
        # E: a recent failure.
        self._make_fax(should_send=True, sent=True, fax_success=False)
        # F: a success (must be ignored everywhere).
        self._make_fax(should_send=True, sent=True, fax_success=True)

        response = self.client.get(reverse("admin_status"))
        self.assertEqual(response.status_code, 200)
        q = response.context["fax_queue"]
        self.assertTrue(q["ok"])
        self.assertEqual(q["unsent_total"], 4)  # A, B, C, D
        self.assertEqual(q["ready_queued"], 3)  # A, B, D
        self.assertEqual(q["due_now"], 1)  # A only
        self.assertEqual(q["awaiting_confirmation"], 1)  # C
        self.assertEqual(q["in_flight"], 1)  # D
        self.assertEqual(q["failures_recent"], 1)  # E


class FaxOutcomeStatusOrderingTest(TestCase):
    """The failure list must surface the most recent send *attempts*, not the
    most recently *created* faxes (PR #959 review)."""

    def _make_fax(self, date=None, **kwargs):
        defaults = dict(
            hashed_email="h", paid=True, email="a@b.com", appeal_text="x", name="Test"
        )
        defaults.update(kwargs)
        fax = FaxesToSend.objects.create(**defaults)
        if date is not None:
            # date is auto_now_add, so backdate via update() to bypass it.
            FaxesToSend.objects.filter(pk=fax.pk).update(date=date)
        return fax

    def test_old_fax_with_recent_attempt_not_displaced_from_failures(self):
        from fighthealthinsurance.staff_views import AdminStatusView

        now = timezone.now()
        # An old fax whose send was attempted (and failed) just now: admitted
        # by the attempt-date filter, and must survive the 10-row slice.
        old_but_fresh_failure = self._make_fax(
            sent=True,
            fax_success=False,
            attempting_to_send_as_of=now,
            date=now - datetime.timedelta(days=30),
        )
        # Eleven newer-created failures with older (or no) attempt timestamps.
        for i in range(11):
            self._make_fax(
                sent=True,
                fax_success=False,
                attempting_to_send_as_of=now - datetime.timedelta(hours=i + 1),
            )

        outcomes = AdminStatusView._fax_outcome_status()
        assert outcomes["error"] is None
        assert outcomes["failed"] == 12
        listed = [f["uuid"] for f in outcomes["recent_failures"]]
        assert len(listed) == 10
        # Ordered by attempt recency, the just-attempted old fax is first;
        # ordering by creation date would have dropped it entirely.
        assert listed[0] == str(old_but_fresh_failure.uuid)


_TEMPORAL_CLIENT = "fighthealthinsurance.temporal_client.get_temporal_client"


class _FakeCount:
    def __init__(self, count):
        self.count = count


class _FakeStatus:
    name = "COMPLETED"


class _FakeWorkflow:
    def __init__(self, id="send-fax-test-1234", started_seconds_ago=42, closed=True):
        self.id = id
        self.status = _FakeStatus()
        self.start_time = timezone.now() - datetime.timedelta(
            seconds=started_seconds_ago
        )
        self.close_time = timezone.now() if closed else None


class _FakeTemporalClient:
    """Stands in for a temporalio Client: two completed runs, one listed.

    Records every visibility query so tests can check their shape. Subclasses
    vary only what the listing does; the counts behave the same everywhere.
    """

    #: Runs the visibility store yields, in the order it yields them.
    listed: tuple = (_FakeWorkflow(),)

    def __init__(self):
        self.queries: list = []
        self.list_queries: list = []

    async def count_workflows(self, query):
        self.queries.append(query)
        return _FakeCount(2 if "Completed" in query else 0)

    def _listing(self):
        return self.listed

    def list_workflows(self, query, page_size=10):
        self.list_queries.append(query)

        async def gen():
            for wf in self._listing():
                yield wf

        return gen()


class _UnorderedTemporalClient(_FakeTemporalClient):
    """A visibility store that hands its runs back oldest-first."""

    listed = (
        _FakeWorkflow(id="send-fax-oldest", started_seconds_ago=900),
        _FakeWorkflow(id="send-fax-newest", started_seconds_ago=30),
        _FakeWorkflow(id="send-fax-middle", started_seconds_ago=300),
    )


class _StoreOrderTemporalClient(_FakeTemporalClient):
    """A visibility store ordering the way Temporal actually does.

    Temporal's default is "ClosedTime DESC NULL FIRST, StartTime DESC" and the
    SQL store will not accept an ORDER BY, so old runs that merely CLOSED
    recently come back ahead of a genuinely recent one. Enough of them to fill
    the panel, then the newest run last.
    """

    listed = tuple(
        [
            _FakeWorkflow(id=f"send-fax-old-{i}", started_seconds_ago=900 + i)
            for i in range(10)
        ]
        + [_FakeWorkflow(id="send-fax-newest", started_seconds_ago=5)]
    )


class _RejectingListTemporalClient(_FakeTemporalClient):
    """Counts fine, refuses the listing the way SQL visibility refuses a
    clause it does not implement."""

    def _listing(self):
        raise RuntimeError(
            "invalid query: operation is not supported: 'ORDER BY' clause"
        )


_FAKE_CLIENTS: list = []  # every fake handed to the view, newest last


def _client_factory(cls):
    """A ``get_temporal_client`` stand-in that hands out *cls* instances."""

    async def _get_client(*args, **kwargs):
        client = cls()
        _FAKE_CLIENTS.append(client)
        return client

    return _get_client


_fake_get_client = _client_factory(_FakeTemporalClient)


async def _broken_get_client():
    raise RuntimeError("temporal-frontend unreachable")


class AdminStatusTemporalTest(TestCase):
    """The Temporal section must reflect the flag, degrade on errors, and
    never raise."""

    def setUp(self):
        User.objects.create_user(username="staff", password="pw123", is_staff=True)
        self.client.login(username="staff", password="pw123")
        _FAKE_CLIENTS.clear()

    def _get(self):
        with mock.patch(_MODELS, return_value=[]), mock.patch(
            _ACTORS, return_value={"alive_actors": 0, "total_actors": 0, "details": []}
        ), mock.patch(_FAX, return_value=_ok_fax_backends()):
            return self.client.get(reverse("admin_status"))

    @override_settings(TEMPORAL_ENABLED=False)
    @mock.patch(_TEMPORAL_CLIENT)
    def test_disabled_makes_no_connection(self, mock_client):
        response = self._get()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "DISABLED")
        self.assertFalse(response.context["temporal"]["enabled"])
        mock_client.assert_not_called()

    @override_settings(TEMPORAL_ENABLED=True)
    @mock.patch(_TEMPORAL_CLIENT, new=_broken_get_client)
    def test_unreachable_frontend_is_an_error_row(self):
        response = self._get()
        self.assertEqual(response.status_code, 200)
        t = response.context["temporal"]
        self.assertFalse(t["ok"])
        self.assertIn("unreachable", t["error"])
        self.assertContains(response, "ERROR")

    @override_settings(TEMPORAL_ENABLED=True)
    @mock.patch(_TEMPORAL_CLIENT, new=_fake_get_client)
    def test_healthy_client_renders_counts_and_recent_runs(self):
        response = self._get()
        self.assertEqual(response.status_code, 200)
        t = response.context["temporal"]
        self.assertTrue(t["ok"])
        self.assertEqual(t["counts"]["Completed"], 2)
        self.assertEqual(t["counts"]["Failed"], 0)
        self.assertEqual(len(t["recent"]), 1)
        self.assertEqual(t["recent"][0]["duration_s"], 42)
        # Running is a live count (no time window); terminal states are 7-day scoped.
        self.assertEqual(len(_FAKE_CLIENTS), 1)
        queries = _FAKE_CLIENTS[0].queries
        running = [q for q in queries if "Running" in q]
        completed = [q for q in queries if "Completed" in q]
        self.assertTrue(running and all("StartTime" not in q for q in running))
        self.assertTrue(completed and all("StartTime" in q for q in completed))
        self.assertContains(response, "CONNECTED")
        self.assertContains(response, "send-fax-test-1234")

    @override_settings(TEMPORAL_ENABLED=True)
    @mock.patch(_TEMPORAL_CLIENT, new=_fake_get_client)
    def test_recent_runs_query_carries_no_order_by_clause(self):
        # Visibility on this cluster is SQL (Postgres, no Elasticsearch), and
        # only Elasticsearch-backed visibility accepts ORDER BY -- a listing
        # query carrying one is rejected outright with
        # "operation is not supported: 'ORDER BY' clause".
        self._get()
        listed = _FAKE_CLIENTS[0].list_queries
        self.assertEqual(len(listed), 1)
        self.assertNotIn("ORDER BY", listed[0].upper())

    @override_settings(TEMPORAL_ENABLED=True)
    @mock.patch(_TEMPORAL_CLIENT, new=_client_factory(_UnorderedTemporalClient))
    def test_recent_runs_are_sorted_most_recent_first(self):
        # Ordering is the panel's own guarantee now that the store cannot be
        # asked for it, so a store yielding oldest-first still renders newest
        # at the top.
        response = self._get()
        ids = [row["id"] for row in response.context["temporal"]["recent"]]
        self.assertEqual(ids, ["send-fax-newest", "send-fax-middle", "send-fax-oldest"])

    @override_settings(TEMPORAL_ENABLED=True)
    @mock.patch(_TEMPORAL_CLIENT, new=_client_factory(_RejectingListTemporalClient))
    def test_rejected_listing_keeps_the_counts_and_explains_itself(self):
        # A refused listing costs the table, not the whole panel: the counts
        # are what on-call pages on.
        response = self._get()
        t = response.context["temporal"]
        self.assertTrue(t["ok"])
        self.assertIsNone(t["error"])
        self.assertEqual(t["counts"]["Completed"], 2)
        self.assertEqual(t["recent"], [])
        self.assertContains(response, "CONNECTED")
        self.assertContains(response, "RECENT RUNS UNAVAILABLE")

    @override_settings(TEMPORAL_ENABLED=True)
    @mock.patch(_TEMPORAL_CLIENT, new=_client_factory(_RejectingListTemporalClient))
    def test_rejected_listing_message_is_not_the_raw_grpc_string(self):
        # The raw text names a symptom and nothing else; an operator needs to
        # be told whose problem it is.
        response = self._get()
        message = response.context["temporal"]["recent_error"]
        self.assertIn("Elasticsearch", message)
        self.assertNotEqual(
            message, "invalid query: operation is not supported: 'ORDER BY' clause"
        )


class AdminStatusStorageTest(TestCase):
    """_storage_status must degrade to an error row, never raise."""

    @staticmethod
    def _status_for(location):
        from django.core.files.storage import FileSystemStorage

        from fighthealthinsurance.staff_views import AdminStatusView

        with override_settings(EXTERNAL_STORAGE=FileSystemStorage(location=location)):
            return AdminStatusView._storage_status()

    def test_reachable_storage_reports_ok_and_location(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = self._status_for(tmp)
        self.assertTrue(status["ok"])
        self.assertIsNone(status["error"])
        self.assertEqual(status["location"], tmp)

    def test_missing_storage_location_reports_error_without_raising(self):
        """The dev/unmounted case: /external_data (or any absent root) must
        render as UNREACHABLE naming the path, not blow up the status page."""
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "not-mounted")
        status = self._status_for(missing)
        self.assertFalse(status["ok"])
        self.assertIn(missing, status["error"])
        self.assertEqual(status["location"], missing)


_SCORING_ON = dict(TYPESAFE_API_KEY="test-key", TYPESAFE_LETTER_RANKING_ENABLED=True)


class AdminStatusLetterScoringTest(TestCase):
    """_letter_scoring_status: on or off, scoring or not, and if not, why.

    Built on the database (draft rows and the ExternalServiceHealth row), not
    on this process's counters: the page is served by whichever web pod the
    browser is pinned to, and the Temporal worker scores drafts too.
    """

    @staticmethod
    def _status():
        from fighthealthinsurance.staff_views import AdminStatusView

        return AdminStatusView._letter_scoring_status()

    @staticmethod
    def _denial(use_external=True):
        return Denial.objects.create(
            semi_sekret="sekret",
            hashed_email=Denial.get_hashed_email("scoring-status@example.com"),
            use_external=use_external,
        )

    _drafts_made = 0

    @classmethod
    def _draft(cls, denial, *, minutes_ago=5, speculative=False, scored=False, now=None):
        # Distinct text per row: (denial, text fingerprint) is unique.
        cls._drafts_made += 1
        row = ProposedAppeal.objects.create(
            for_denial=denial,
            appeal_text=f"Draft number {cls._drafts_made}, long enough to read as a letter.",
            speculative=speculative,
        )
        stamp = (now or timezone.now()) - datetime.timedelta(minutes=minutes_ago)
        fields = {"created_at": stamp}
        if scored:
            fields.update(
                quality_score=0.8,
                grounding_score=2.0,
                quality_scorer="typesafe/speed_latest/rubric-1",
                quality_scored_at=stamp,
            )
        # auto_now_add wins on create, so the clock is set afterwards.
        ProposedAppeal.objects.filter(pk=row.pk).update(**fields)
        return row

    @staticmethod
    def _health(**fields):
        return ExternalServiceHealth.objects.create(service="typesafe", **fields)

    def test_off_under_the_test_settings(self):
        status = self._status()
        self.assertTrue(status["ok"])
        self.assertIsNone(status["error"])
        self.assertEqual(status["level"], "off")
        self.assertFalse(status["key_present"])
        self.assertFalse(status["flag_on"])

    def test_a_key_without_the_flag_is_off_and_says_which_half_is_missing(self):
        with override_settings(TYPESAFE_API_KEY="k", TYPESAFE_LETTER_RANKING_ENABLED=False):
            status = self._status()
        self.assertEqual(status["level"], "off")
        self.assertTrue(status["key_present"])
        self.assertFalse(status["flag_on"])

    def test_scoring_when_a_draft_was_scored_in_the_window(self):
        self._draft(self._denial(), scored=True)
        with override_settings(**_SCORING_ON):
            status = self._status()
        self.assertEqual(status["level"], "scoring")
        self.assertEqual(status["scored"], 1)
        self.assertEqual(status["unscored"], 0)

    def test_not_scoring_when_eligible_drafts_have_no_score(self):
        self._draft(self._denial())
        with override_settings(**_SCORING_ON):
            status = self._status()
        self.assertEqual(status["level"], "not_scoring")
        self.assertEqual(status["unscored"], 1)

    def test_drafts_that_were_never_eligible_do_not_count_as_unscored(self):
        """Still in flight (younger than the drain window), speculative, or
        from a denial that did not allow external models."""
        # The clock is frozen for the helper: the in-flight draft is five
        # seconds old at the instant the page reads it, however long the
        # test takes to get there (review).
        frozen = timezone.now()
        denial = self._denial()
        ProposedAppeal.objects.filter(pk=self._draft(denial, now=frozen).pk).update(
            created_at=frozen - datetime.timedelta(seconds=5)
        )
        self._draft(denial, speculative=True, now=frozen)
        self._draft(self._denial(use_external=False), now=frozen)
        with override_settings(**_SCORING_ON), mock.patch(
            "django.utils.timezone.now", return_value=frozen
        ):
            status = self._status()
        self.assertEqual(status["unscored"], 0)
        self.assertEqual(status["level"], "idle")

    def test_unscored_drafts_newer_than_the_last_score_mean_not_scoring(self):
        """One score in the morning must not keep the badge green all day
        after scoring silently stopped (review)."""
        now = timezone.now()
        self._health(last_success_at=now - datetime.timedelta(hours=2))
        denial = self._denial()
        self._draft(denial, minutes_ago=120, scored=True)
        self._draft(denial, minutes_ago=5)
        with override_settings(**_SCORING_ON):
            status = self._status()
        self.assertEqual(status["scored"], 1)
        self.assertEqual(status["stalled"], 1)
        self.assertEqual(status["level"], "not_scoring")

    def test_unscored_drafts_older_than_the_last_score_do_not_stall(self):
        """A miss followed by a success is scoring again."""
        now = timezone.now()
        self._health(last_success_at=now - datetime.timedelta(minutes=1))
        denial = self._denial()
        self._draft(denial, minutes_ago=30)
        self._draft(denial, minutes_ago=1, scored=True)
        with override_settings(**_SCORING_ON):
            status = self._status()
        self.assertEqual(status["unscored"], 1)
        self.assertEqual(status["stalled"], 0)
        self.assertEqual(status["level"], "scoring")

    def test_a_score_outside_the_window_does_not_count(self):
        self._draft(self._denial(), minutes_ago=25 * 60, scored=True)
        with override_settings(**_SCORING_ON):
            status = self._status()
        self.assertEqual(status["scored"], 0)
        self.assertEqual(status["level"], "idle")

    def test_failing_when_the_last_failure_is_newer_than_the_last_success(self):
        now = timezone.now()
        self._health(
            last_success_at=now - datetime.timedelta(hours=1),
            last_failure_at=now - datetime.timedelta(minutes=2),
            last_failure="HTTP 402",
        )
        # Even with a score in the window: the freshest signal wins.
        self._draft(self._denial(), scored=True)
        with override_settings(**_SCORING_ON):
            status = self._status()
        self.assertEqual(status["level"], "failing")
        self.assertEqual(status["last_failure"], "HTTP 402")
        self.assertIn("credits", status["last_failure_hint"])
        self.assertFalse(status["recovered"])

    def test_a_failure_followed_by_a_success_reads_as_recovered(self):
        now = timezone.now()
        self._health(
            last_success_at=now - datetime.timedelta(minutes=1),
            last_failure_at=now - datetime.timedelta(minutes=30),
            last_failure="HTTP 503",
        )
        with override_settings(**_SCORING_ON):
            status = self._status()
        self.assertNotEqual(status["level"], "failing")
        self.assertTrue(status["recovered"])
        self.assertIn("server error", status["last_failure_hint"])

    def test_an_old_failure_does_not_fail_the_row_forever(self):
        self._health(
            last_failure_at=timezone.now() - datetime.timedelta(days=3),
            last_failure="timeout",
        )
        with override_settings(**_SCORING_ON):
            status = self._status()
        self.assertEqual(status["level"], "idle")

    def test_the_helper_degrades_to_an_error_row_instead_of_raising(self):
        with mock.patch.object(
            ProposedAppeal.objects, "filter", side_effect=RuntimeError("db down")
        ):
            status = self._status()
        self.assertFalse(status["ok"])
        self.assertEqual(status["error"], "db down")

    def test_the_page_names_the_failure_and_what_it_means(self):
        self._health(
            last_failure_at=timezone.now() - datetime.timedelta(minutes=2),
            last_failure="HTTP 402",
        )
        User.objects.create_user(username="staff", password="pw123", is_staff=True)
        self.client.login(username="staff", password="pw123")
        with override_settings(**_SCORING_ON), mock.patch(_MODELS, return_value=[]), mock.patch(
            _ACTORS, return_value={"alive_actors": 0, "total_actors": 0, "details": []}
        ), mock.patch(_FAX, return_value=_ok_fax_backends()):
            response = self.client.get(reverse("admin_status"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "FAILING")
        self.assertContains(response, "HTTP 402")
        self.assertContains(response, "credits or billing")
        self.assertNotContains(response, "test-key")

    def test_the_page_counts_drafts_stalled_since_the_last_score(self):
        self._health(last_success_at=timezone.now() - datetime.timedelta(hours=1))
        self._draft(self._denial(), minutes_ago=5)
        User.objects.create_user(username="staff", password="pw123", is_staff=True)
        self.client.login(username="staff", password="pw123")
        with override_settings(**_SCORING_ON), mock.patch(_MODELS, return_value=[]), mock.patch(
            _ACTORS, return_value={"alive_actors": 0, "total_actors": 0, "details": []}
        ), mock.patch(_FAX, return_value=_ok_fax_backends()):
            response = self.client.get(reverse("admin_status"))
        self.assertContains(response, "NOT SCORING")
        self.assertContains(response, "1 eligible draft since the last score, none scored")


class ComputeModelHealthDetailsTest(TestCase):
    def test_classifies_and_sorts_problems_first(self):
        from fighthealthinsurance.ml.health_status import compute_model_health_details

        class Good:
            model = "good-internal"
            external = False

            def model_is_ok(self):
                return True

        class Bad:
            model = "bad-external"
            external = True

            def model_is_ok(self):
                return False

        fake_router = mock.MagicMock()
        fake_router.all_models_by_cost = [Good(), Bad()]
        with mock.patch("fighthealthinsurance.ml.ml_router.ml_router", fake_router):
            details = compute_model_health_details(timeout_seconds=2)

        by_name = {d["name"]: d for d in details}
        self.assertTrue(by_name["good-internal"]["ok"])
        self.assertFalse(by_name["good-internal"]["external"])
        self.assertFalse(by_name["bad-external"]["ok"])
        self.assertTrue(by_name["bad-external"]["external"])
        # Down backends sort first so on-call sees problems at the top.
        self.assertFalse(details[0]["ok"])

    def test_empty_router_returns_empty_list(self):
        from fighthealthinsurance.ml.health_status import compute_model_health_details

        fake_router = mock.MagicMock()
        fake_router.all_models_by_cost = []
        with mock.patch("fighthealthinsurance.ml.ml_router.ml_router", fake_router):
            self.assertEqual(compute_model_health_details(), [])

    def test_enumeration_failure_propagates(self):
        """A broken router must raise, not mask the failure as 0 backends."""
        from fighthealthinsurance.ml.health_status import compute_model_health_details

        class BrokenRouter:
            @property
            def all_models_by_cost(self):
                raise RuntimeError("router not ready")

        with mock.patch("fighthealthinsurance.ml.ml_router.ml_router", BrokenRouter()):
            with self.assertRaisesRegex(RuntimeError, "router not ready"):
                compute_model_health_details()

    def test_returns_at_deadline_without_blocking_on_hung_probe(self):
        """A hung model_is_ok() must not stall the call past the deadline.

        Regression for the executor shutdown(wait=True) gap: the call must
        return ~timeout_seconds with the slow backend marked as a timeout,
        not block until the hung probe finishes.
        """
        from fighthealthinsurance.ml.health_status import compute_model_health_details

        release = threading.Event()

        class Slow:
            model = "slow-internal"
            external = False

            def model_is_ok(self):
                # Blocks well past the deadline unless released.
                release.wait(timeout=10)
                return True

        class Fast:
            model = "fast-internal"
            external = False

            def model_is_ok(self):
                return True

        fake_router = mock.MagicMock()
        fake_router.all_models_by_cost = [Slow(), Fast()]
        try:
            with mock.patch("fighthealthinsurance.ml.ml_router.ml_router", fake_router):
                start = time.monotonic()
                details = compute_model_health_details(timeout_seconds=1)
                elapsed = time.monotonic() - start

            # Returned promptly at the deadline, not after the 10s hung probe.
            self.assertLess(elapsed, 5)
            by_name = {d["name"]: d for d in details}
            self.assertTrue(by_name["fast-internal"]["ok"])
            self.assertFalse(by_name["slow-internal"]["ok"])
            self.assertIn("timeout", by_name["slow-internal"]["error"] or "")
        finally:
            # Let the orphaned probe thread finish so it doesn't linger.
            release.set()


class FaxBackendsHealthTest(TestCase):
    @mock.patch.dict(
        os.environ,
        {"SONIC_USERNAME": "u", "SONIC_PASSWORD": "p", "SONIC_TOKEN": "t"},
    )
    def test_sonic_probe_reports_working(self):
        from fighthealthinsurance import fax_utils
        from fighthealthinsurance.fax_health_status import check_fax_backends_health
        from fighthealthinsurance.fax_utils import SonicFax

        sonic = SonicFax()
        sonic.check_health = mock.Mock(return_value=True)
        with mock.patch.object(fax_utils.flexible_fax_magic, "backends", [sonic]):
            result = check_fax_backends_health(probe_timeout=2.0)

        self.assertTrue(result["sonic"]["active"])
        self.assertTrue(result["sonic"]["configured"])
        self.assertTrue(result["sonic"]["ok"])
        self.assertEqual(result["total_backends"], 1)
        self.assertEqual(result["backends"][0]["name"], "SonicFax")
        self.assertTrue(result["backends"][0]["probed"])

    @mock.patch.dict(
        os.environ,
        {"SONIC_USERNAME": "u", "SONIC_PASSWORD": "p", "SONIC_TOKEN": "t"},
    )
    def test_sonic_probe_reports_failure(self):
        from fighthealthinsurance import fax_utils
        from fighthealthinsurance.fax_health_status import check_fax_backends_health
        from fighthealthinsurance.fax_utils import SonicFax

        sonic = SonicFax()
        sonic.check_health = mock.Mock(side_effect=Exception("bad login"))
        with mock.patch.object(fax_utils.flexible_fax_magic, "backends", [sonic]):
            result = check_fax_backends_health(probe_timeout=2.0)

        self.assertTrue(result["sonic"]["active"])
        self.assertFalse(result["sonic"]["ok"])
        self.assertIn("bad login", result["sonic"]["error"])
        self.assertFalse(result["backends"][0]["ok"])

    def test_probe_outer_cap_covers_sequential_per_request_budgets(self):
        """A backend whose sequential round-trips each fit the per-request
        budget but sum past it (e.g. Sonic's login GET + POST + members GET,
        each just under ``timeout``) is slow-but-working, not dead. The outer
        probe deadline covers the sum, so it must report healthy instead of a
        false ``timeout>Ns`` failure."""
        from fighthealthinsurance.fax_health_status import _probe_backend

        def slow_but_healthy(timeout):
            # Longer than one per-request budget, well within the 3x cap.
            time.sleep(timeout * 1.5)
            return True

        backend = mock.Mock()
        backend.check_health = slow_but_healthy
        ok, error = _probe_backend(backend, timeout=0.2)
        self.assertTrue(ok)
        self.assertIsNone(error)

    def test_sonic_not_configured_reports_reason(self):
        from fighthealthinsurance import fax_utils
        from fighthealthinsurance.fax_health_status import check_fax_backends_health

        with mock.patch.object(
            fax_utils.flexible_fax_magic, "backends", [_FakeBackend()]
        ):
            with mock.patch.dict(os.environ, {}, clear=False):
                for k in ("SONIC_USERNAME", "SONIC_PASSWORD", "SONIC_TOKEN"):
                    os.environ.pop(k, None)
                result = check_fax_backends_health()

        self.assertFalse(result["sonic"]["configured"])
        self.assertFalse(result["sonic"]["active"])
        self.assertFalse(result["sonic"]["ok"])
        self.assertEqual(result["total_backends"], 1)
        self.assertEqual(result["backends"][0]["name"], "_FakeBackend")
        self.assertFalse(result["backends"][0]["probed"])


_LOGIN = "fighthealthinsurance.fax_utils.SonicFax._login"
_SESSION_GET = "requests.Session.get"
_SONIC_ENV = {"SONIC_USERNAME": "u", "SONIC_PASSWORD": "p", "SONIC_TOKEN": "t"}


def _members_response(text="Fax Console", http_error=False):
    """Fake members-page response for the post-login verification GET."""
    resp = mock.Mock()
    resp.text = text
    resp.raise_for_status = mock.Mock(
        side_effect=requests.HTTPError("500") if http_error else None
    )
    return resp


class SonicCheckHealthTest(TestCase):
    @mock.patch.dict(os.environ, _SONIC_ENV)
    @mock.patch(_SESSION_GET)
    @mock.patch(_LOGIN, return_value={"c": "v"})
    def test_check_health_true_when_login_and_members_page_ok(
        self, mock_login, mock_get
    ):
        from fighthealthinsurance.fax_utils import SonicFax

        mock_get.return_value = _members_response()
        self.assertTrue(SonicFax().check_health())
        mock_login.assert_called_once()

    @mock.patch.dict(os.environ, _SONIC_ENV)
    @mock.patch(_LOGIN, side_effect=RuntimeError("login rejected"))
    def test_check_health_propagates_login_failure(self, mock_login):
        from fighthealthinsurance.fax_utils import SonicFax

        with self.assertRaisesRegex(RuntimeError, "login rejected"):
            SonicFax().check_health()

    @mock.patch.dict(os.environ, _SONIC_ENV)
    @mock.patch(_SESSION_GET)
    @mock.patch(_LOGIN, return_value={"c": "v"})
    def test_check_health_raises_on_http_error(self, mock_login, mock_get):
        """A 4xx/5xx members page (e.g. 500/maintenance) is not healthy."""
        from fighthealthinsurance.fax_utils import SonicFax

        mock_get.return_value = _members_response(http_error=True)
        with self.assertRaises(requests.HTTPError):
            SonicFax().check_health()

    @mock.patch.dict(os.environ, _SONIC_ENV)
    @mock.patch(_SESSION_GET)
    @mock.patch(_LOGIN, return_value={"c": "v"})
    def test_check_health_raises_when_bounced_to_login(self, mock_login, mock_get):
        """A 200 that is really the login form must not count as healthy."""
        from fighthealthinsurance.fax_utils import SonicFax

        mock_get.return_value = _members_response(text="Please Member Login")
        with self.assertRaisesRegex(Exception, "not authenticated"):
            SonicFax().check_health()


class AdminStatusTemporalOrderingTest(AdminStatusTemporalTest):
    """The panel's ordering must be its own, not the visibility store's."""

    @override_settings(TEMPORAL_ENABLED=True)
    @mock.patch(_TEMPORAL_CLIENT, new=_client_factory(_StoreOrderTemporalClient))
    def test_a_recently_closed_old_run_cannot_displace_a_recent_one(self):
        """Taking the first ten in the STORE's order and sorting only those
        lets an old workflow that merely closed recently push a genuinely
        recent run off the list. The panel then shows the wrong ten and looks
        entirely correct doing it."""
        from fighthealthinsurance.staff_views import AdminStatusView

        response = self._get()
        rows = response.context["temporal"]["recent"]
        self.assertEqual(len(rows), AdminStatusView._RECENT_WORKFLOW_LIMIT)
        self.assertEqual(rows[0]["id"], "send-fax-newest", [r["id"] for r in rows])

    def test_the_candidate_scan_is_wider_than_the_panel_but_bounded(self):
        """It has to look past the store's ordering to sort honestly, without
        turning a status page into a full scan of a busy namespace."""
        from fighthealthinsurance.staff_views import AdminStatusView

        self.assertGreater(
            AdminStatusView._RECENT_WORKFLOW_SCAN,
            AdminStatusView._RECENT_WORKFLOW_LIMIT,
        )
        self.assertLessEqual(AdminStatusView._RECENT_WORKFLOW_SCAN, 500)


class AdminStatusTemporalScopeTest(AdminStatusTemporalTest):
    """The listing must carry the same window as the counts beside it."""

    @override_settings(TEMPORAL_ENABLED=True)
    @mock.patch(_TEMPORAL_CLIENT, new=_client_factory(_FakeTemporalClient))
    def test_the_listing_is_scoped_to_the_same_seven_days_as_the_counts(self):
        """Dropping ORDER BY meant rebuilding the query, and the time scope
        went with it. Unscoped, the bounded candidate scan runs over ALL
        history: a namespace with more old runs than the scan limit fills the
        candidates with them and crowds out the recent ones, so the bounded
        scan would reintroduce exactly the defect it was added to fix -- and
        the table would disagree with the counts printed above it."""
        self._get()
        client = _FAKE_CLIENTS[-1]
        assert client.list_queries, "no visibility listing was issued"
        listed = client.list_queries[-1]
        assert "StartTime >" in listed, listed
        # ...and still no ORDER BY, which is what started all this.
        assert "ORDER BY" not in listed.upper(), listed

    @override_settings(TEMPORAL_ENABLED=True)
    @mock.patch(_TEMPORAL_CLIENT, new=_client_factory(_FakeTemporalClient))
    def test_the_listing_shares_the_window_of_the_terminal_counts(self):
        """The counts are deliberately NOT uniform: Running is a live state
        and is counted unscoped, while the terminal states are bounded to
        seven days. The listing belongs with the terminal ones -- it is a
        "recent runs" table, and a run still open after seven days is
        reported by the Running count rather than dropped."""
        self._get()
        client = _FAKE_CLIENTS[-1]
        listed = client.list_queries[-1]
        terminal = [
            q for q in client.queries if "ExecutionStatus" in q and "Running" not in q
        ]
        assert terminal, client.queries
        for q in terminal:
            assert q.startswith(listed), (q, listed)
        # ...and Running really is exempt, so this is a documented asymmetry
        # rather than one query that got missed.
        running = [q for q in client.queries if "Running" in q]
        assert running, client.queries
        for q in running:
            assert "StartTime >" not in q, q
