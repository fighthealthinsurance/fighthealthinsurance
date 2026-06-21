"""Tests for the deployment-time model probe and dead-models email report."""

from io import StringIO
from unittest.mock import AsyncMock, patch

import pytest
from django.core.management import call_command

from fighthealthinsurance.ml.startup_probe import (
    SUPPORT_EMAIL,
    _send_dead_models_report,
    run_startup_model_probe,
)


def _enable_probe(monkeypatch):
    """Turn off the test-environment gate so the probe actually runs."""
    monkeypatch.setenv("TESTING", "false")
    monkeypatch.setenv("FHI_STARTUP_MODEL_PROBE", "1")


def _patch_probe_results(results):
    """Patch the router's probe_all_models to return ``results``."""
    mock_router = patch("fighthealthinsurance.ml.startup_probe.ml_router")
    started = mock_router.start()
    started.probe_all_models = AsyncMock(return_value=results)
    return mock_router


class TestSendDeadModelsReport:
    def test_emails_support_alias_with_details(self, mailoutbox):
        _send_dead_models_report([("RemotePerplexity(model=sonar)", "HTTP 401")])
        assert len(mailoutbox) == 1
        msg = mailoutbox[0]
        assert msg.to == [SUPPORT_EMAIL]
        assert "unreachable" in msg.subject.lower()
        assert "RemotePerplexity(model=sonar)" in msg.body
        assert "HTTP 401" in msg.body

    def test_honors_explicit_recipients(self, mailoutbox):
        _send_dead_models_report([("backend", "boom")], recipients=["ops@example.com"])
        assert mailoutbox[0].to == ["ops@example.com"]


class TestRunStartupModelProbe:
    def test_skipped_under_test_environment(self, monkeypatch, mailoutbox):
        # TESTING is set by the test config; the probe must no-op.
        monkeypatch.setenv("TESTING", "True")
        assert run_startup_model_probe() is None
        assert mailoutbox == []

    def test_skipped_when_disabled(self, monkeypatch, mailoutbox):
        monkeypatch.setenv("TESTING", "false")
        monkeypatch.setenv("FHI_STARTUP_MODEL_PROBE", "0")
        assert run_startup_model_probe() is None
        assert mailoutbox == []

    def test_emails_report_when_models_dead(self, monkeypatch, mailoutbox):
        _enable_probe(monkeypatch)
        results = [
            ("good-model", True, None),
            ("dead-model", False, "HTTP 401 (insufficient_quota)"),
        ]
        mock_router = _patch_probe_results(results)
        try:
            returned = run_startup_model_probe()
        finally:
            mock_router.stop()

        assert returned == results
        assert len(mailoutbox) == 1
        assert mailoutbox[0].to == [SUPPORT_EMAIL]
        assert "dead-model" in mailoutbox[0].body
        assert "insufficient_quota" in mailoutbox[0].body
        # Healthy backends are not named in the report.
        assert "good-model" not in mailoutbox[0].body

    def test_no_email_when_all_alive(self, monkeypatch, mailoutbox):
        _enable_probe(monkeypatch)
        results = [("a", True, None), ("b", True, None)]
        mock_router = _patch_probe_results(results)
        try:
            returned = run_startup_model_probe()
        finally:
            mock_router.stop()

        assert returned == results
        assert mailoutbox == []

    def test_no_email_when_no_backends(self, monkeypatch, mailoutbox):
        _enable_probe(monkeypatch)
        mock_router = _patch_probe_results([])
        try:
            returned = run_startup_model_probe()
        finally:
            mock_router.stop()

        assert returned == []
        assert mailoutbox == []

    def test_swallows_probe_exception(self, monkeypatch, mailoutbox):
        _enable_probe(monkeypatch)
        mock_router = patch("fighthealthinsurance.ml.startup_probe.ml_router")
        started = mock_router.start()
        started.probe_all_models = AsyncMock(side_effect=RuntimeError("kaboom"))
        try:
            returned = run_startup_model_probe()
        finally:
            mock_router.stop()

        assert returned is None
        assert mailoutbox == []


class TestProbeModelsCommand:
    """The standalone `probe_models` management command (run once per deploy)."""

    def _run(self, results):
        out = StringIO()
        with patch(
            "fighthealthinsurance.ml.startup_probe.run_startup_model_probe",
            return_value=results,
        ):
            call_command("probe_models", stdout=out)
        return out.getvalue()

    def test_reports_unreachable_backends(self):
        output = self._run([("good", True, None), ("dead", False, "HTTP 401")])
        assert "1/2" in output
        assert "unreachable" in output

    def test_reports_all_healthy(self):
        output = self._run([("a", True, None), ("b", True, None)])
        assert "all 2 backend(s) responded" in output

    def test_reports_skipped(self):
        output = self._run(None)
        assert "skipped" in output.lower()
