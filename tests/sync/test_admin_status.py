"""Tests for the staff system-status dashboard (AdminStatusView) and the
fax/model health helpers it relies on."""

import datetime
import os
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from fighthealthinsurance.models import FaxesToSend

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
    def test_non_staff_redirected(self):
        # staff_member_required redirects anonymous users to the admin login.
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
        self._make_fax(
            should_send=True, sent=False, attempting_to_send_as_of=now
        )
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


class SonicCheckHealthTest(TestCase):
    @mock.patch.dict(
        os.environ,
        {"SONIC_USERNAME": "u", "SONIC_PASSWORD": "p", "SONIC_TOKEN": "t"},
    )
    def test_check_health_true_on_successful_login(self):
        from fighthealthinsurance.fax_utils import SonicFax

        sonic = SonicFax()
        with mock.patch.object(SonicFax, "_login", return_value={"c": "v"}) as m:
            self.assertTrue(sonic.check_health())
        m.assert_called_once()

    @mock.patch.dict(
        os.environ,
        {"SONIC_USERNAME": "u", "SONIC_PASSWORD": "p", "SONIC_TOKEN": "t"},
    )
    def test_check_health_propagates_login_failure(self):
        from fighthealthinsurance.fax_utils import SonicFax

        sonic = SonicFax()
        with mock.patch.object(
            SonicFax, "_login", side_effect=Exception("login rejected")
        ):
            with self.assertRaises(Exception):
                sonic.check_health()
