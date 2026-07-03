"""Tests for actor health status endpoint and functionality."""

import contextlib
from unittest import mock
from django.test import TestCase, override_settings
from rest_framework.test import APIClient


def _patched_actor_refs():
    """Patch every polling actor-ref singleton so relaunch_actors() can run
    without touching Ray. Each ref's ``.get`` yields an (actor, task) tuple."""
    modules = [
        "email_polling_actor_ref",
        "fax_polling_actor_ref",
        "chooser_refill_actor_ref",
        "imr_refresh_actor_ref",
        "pa_refresh_actor_ref",
        "ucr_refresh_actor_ref",
    ]
    stack = contextlib.ExitStack()
    for module_name in modules:
        ref = mock.MagicMock()
        ref.get = (mock.MagicMock(), mock.MagicMock())
        stack.enter_context(
            mock.patch(f"fighthealthinsurance.{module_name}.{module_name}", ref)
        )
    return stack


class TestActorHealthStatus(TestCase):
    """Tests for actor health status checking."""

    def setUp(self):
        self.client = APIClient()

    @override_settings(TEMPORAL_ENABLED=False)
    def test_actor_health_endpoint_accessible(self):
        """Test that the actor health endpoint is accessible."""
        response = self.client.get("/ziggy/rest/actor_health_status")
        assert response.status_code == 200
        data = response.json()
        # Should have the expected keys
        assert "alive_actors" in data
        assert "total_actors" in data
        assert "details" in data
        # Total actors: email, fax, chooser, IMR refresh, UCR refresh, PA refresh
        assert data["total_actors"] == 6

    @override_settings(TEMPORAL_ENABLED=False)
    @mock.patch("fighthealthinsurance.actor_health_status.ray")
    def test_actor_health_all_down(self, mock_ray):
        """Test actor health when all actors are down."""
        # Simulate all actors not found
        mock_ray.get_actor.side_effect = ValueError("Actor not found")

        from fighthealthinsurance.actor_health_status import check_actor_health

        result = check_actor_health()
        assert result["alive_actors"] == 0
        assert result["total_actors"] == 6
        assert len(result["details"]) == 6
        # All should have error messages
        for detail in result["details"]:
            assert detail["alive"] is False
            assert detail["error"] is not None

    @override_settings(TEMPORAL_ENABLED=False)
    @mock.patch("fighthealthinsurance.actor_health_status.ray")
    def test_actor_health_all_up(self, mock_ray):
        """Test actor health when all actors are up and healthy."""
        # Mock actor that returns True for health_check
        mock_actor = mock.MagicMock()
        mock_health_result = mock.MagicMock()
        mock_actor.health_check.remote.return_value = mock_health_result

        mock_ray.get_actor.return_value = mock_actor
        mock_ray.get.return_value = True

        from fighthealthinsurance.actor_health_status import check_actor_health

        result = check_actor_health()
        assert result["alive_actors"] == 6
        assert result["total_actors"] == 6
        assert len(result["details"]) == 6
        # All should be alive
        for detail in result["details"]:
            assert detail["alive"] is True
            assert detail["error"] is None

    @override_settings(TEMPORAL_ENABLED=False)
    @mock.patch("fighthealthinsurance.actor_health_status.ray")
    def test_actor_health_partial(self, mock_ray):
        """Test actor health when some actors are up and some are down."""
        call_count = [0]

        def get_actor_side_effect(name, namespace):
            call_count[0] += 1
            if call_count[0] == 1:  # First actor is up
                mock_actor = mock.MagicMock()
                mock_health_result = mock.MagicMock()
                mock_actor.health_check.remote.return_value = mock_health_result
                return mock_actor
            else:  # Other actors are down
                raise ValueError("Actor not found")

        mock_ray.get_actor.side_effect = get_actor_side_effect
        mock_ray.get.return_value = True

        from fighthealthinsurance.actor_health_status import check_actor_health

        result = check_actor_health()
        assert result["alive_actors"] == 1
        assert result["total_actors"] == 6
        assert len(result["details"]) == 6

    def test_actor_health_endpoint_caching(self):
        """Test that the actor health endpoint has appropriate cache headers."""
        response = self.client.get("/ziggy/rest/actor_health_status")
        assert response.status_code == 200
        # Should have cache control header (max 60 seconds)
        assert "Cache-Control" in response
        cache_control = response["Cache-Control"]
        # Should cache for at most 60 seconds
        assert "max-age=60" in cache_control or "max-age" in cache_control

    @override_settings(TEMPORAL_ENABLED=True)
    @mock.patch("fighthealthinsurance.actor_health_status.ray")
    def test_check_actor_health_excludes_fax_when_temporal_enabled(self, mock_ray):
        """When Temporal owns fax sending, the fax polling actor is not checked."""
        mock_ray.get_actor.return_value = mock.MagicMock()
        mock_ray.get.return_value = True

        from fighthealthinsurance.actor_health_status import check_actor_health

        result = check_actor_health()
        # Five actors remain: email, chooser, IMR, UCR, PA (no fax).
        assert result["total_actors"] == 5
        assert result["alive_actors"] == 5
        names = [d["name"] for d in result["details"]]
        assert "fax_polling_actor" not in names

    @override_settings(TEMPORAL_ENABLED=True)
    def test_relaunch_actors_excludes_fax_when_temporal_enabled(self):
        """relaunch_actors must not resurrect the fax polling actor under Temporal."""
        from fighthealthinsurance.actor_health_status import relaunch_actors

        with _patched_actor_refs():
            results = relaunch_actors(force=False)

        assert "fax_polling_actor" not in results
        assert len(results) == 5
        assert all(r["status"] == "launched" for r in results.values())

    @override_settings(TEMPORAL_ENABLED=False)
    def test_relaunch_actors_includes_fax_by_default(self):
        """With Temporal off, the fax polling actor is still relaunched as before."""
        from fighthealthinsurance.actor_health_status import relaunch_actors

        with _patched_actor_refs():
            results = relaunch_actors(force=False)

        assert "fax_polling_actor" in results
        assert len(results) == 6
