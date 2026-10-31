"""The health probe's diagnostics: the not-served sample, and each probe
problem reported once per outage rather than on every recheck. No network."""

import pytest
from loguru import logger

from fighthealthinsurance.ml import ml_models
from fighthealthinsurance.ml.ml_models import (
    _log_probe_ok,
    _log_probe_problem,
    _served_sample,
)


@pytest.fixture
def records():
    """The (level, message) pairs logged while the test runs."""
    seen = []
    sink = logger.add(
        lambda m: seen.append((m.record["level"].name, m.record["message"])),
        level="DEBUG",
    )
    ml_models._PROBE_PROBLEM_LOGGED.clear()
    yield seen
    logger.remove(sink)
    ml_models._PROBE_PROBLEM_LOGGED.clear()


def test_the_sample_puts_related_names_first():
    served = {"BAAI/bge-m3", "fhi-local", "fhi-2025-nov-q8", "zz-other"}

    sample = _served_sample("/models/fhi-2025-nov-q8-vllm-compressed", served)

    assert sample[:2] == ["fhi-2025-nov-q8", "fhi-local"]


def test_numeric_model_ids_do_not_break_the_sample():
    """A /models listing may carry numeric ids; the diagnostic must not
    raise inside the probe and end it before the backup leg is asked."""
    assert set(_served_sample("fhi-local", {1, "fhi-local", 2})) == {
        "1",
        "2",
        "fhi-local",
    }


def test_a_problem_is_reported_once_then_at_debug(records):
    pair = ("http://host/v1", "model-a")
    for _ in range(3):
        _log_probe_problem(pair, "not served", "model-a is not served", "info")

    assert [level for level, _ in records] == ["INFO", "DEBUG", "DEBUG"]


def test_a_different_problem_is_reported_again(records):
    pair = ("http://host/v1", "model-a")
    _log_probe_problem(pair, "unreachable", "cannot reach host")
    _log_probe_problem(pair, "status 401", "host refused")

    assert [level for level, _ in records] == ["WARNING", "WARNING"]


def test_recovery_is_reported_and_rearms_the_report(records):
    pair = ("http://host/v1", "model-a")
    _log_probe_problem(pair, "not served", "model-a is not served", "info")
    _log_probe_ok(pair)
    _log_probe_ok(pair)
    _log_probe_problem(pair, "not served", "model-a is not served", "info")

    assert [level for level, _ in records] == ["INFO", "INFO", "INFO"]
    assert "again" in records[1][1]


def test_a_backup_problem_is_reported_again_after_the_primary_was_back(records):
    """While the primary serves, the backup is not asked, so its problem
    from the last outage must not make its next failure read as the same
    one and go out at DEBUG."""
    from unittest.mock import MagicMock, patch

    import requests

    primary, backup = "http://primary.internal/v1", "http://backup.internal/v1"
    model = ml_models.RemoteFullOpenLike(primary, "tok", "m", backup_api_base=backup)
    serving = MagicMock(status_code=200)
    serving.json.return_value = {"data": [{"id": "m"}]}
    down = requests.Timeout("read timed out")

    def probe(primary_answer):
        def get(url, **kwargs):
            answer = primary_answer if url.startswith(primary) else down
            if isinstance(answer, Exception):
                raise answer
            return answer

        with patch("fighthealthinsurance.ml.ml_models.requests.get", side_effect=get):
            return model.model_is_ok()

    assert probe(down) is False
    assert probe(serving) is True
    assert probe(down) is False

    backup_lines = [level for level, message in records if backup in message]
    assert backup_lines == ["WARNING", "WARNING"]
