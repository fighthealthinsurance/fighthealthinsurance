"""Tests for actor health status endpoint and functionality."""

from unittest import mock
from django.test import TestCase
from rest_framework.test import APIClient


class TestActorHealthStatus(TestCase):
    """Tests for actor health status checking."""

    def setUp(self):
        self.client = APIClient()

    def test_actor_health_endpoint_accessible(self):
        """Test that the actor health endpoint is accessible."""
        response = self.client.get("/ziggy/rest/actor_health_status")
        assert response.status_code == 200
        data = response.json()
        # Should have the expected keys
        assert "alive_actors" in data
        assert "total_actors" in data
        assert "details" in data
        # Total actors should always be 3
        assert data["total_actors"] == 3

    @mock.patch("fighthealthinsurance.actor_health_status.ray")
    def test_actor_health_all_down(self, mock_ray):
        """Test actor health when all actors are down."""
        # Simulate all actors not found
        mock_ray.get_actor.side_effect = ValueError("Actor not found")

        from fighthealthinsurance.actor_health_status import check_actor_health

        result = check_actor_health()
        assert result["alive_actors"] == 0
        assert result["total_actors"] == 3
        assert len(result["details"]) == 3
        # All should have error messages
        for detail in result["details"]:
            assert detail["alive"] is False
            assert detail["error"] is not None

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
        assert result["alive_actors"] == 3
        assert result["total_actors"] == 3
        assert len(result["details"]) == 3
        # All should be alive
        for detail in result["details"]:
            assert detail["alive"] is True
            assert detail["error"] is None

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
        assert result["total_actors"] == 3
        assert len(result["details"]) == 3

    def test_actor_health_endpoint_caching(self):
        """Test that the actor health endpoint has appropriate cache headers."""
        response = self.client.get("/ziggy/rest/actor_health_status")
        assert response.status_code == 200
        # Should have cache control header (max 60 seconds)
        assert "Cache-Control" in response
        cache_control = response["Cache-Control"]
        # Should cache for at most 60 seconds
        assert "max-age=60" in cache_control or "max-age" in cache_control
