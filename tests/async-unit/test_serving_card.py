"""What a model server's /models reply says about the model it serves."""

from unittest.mock import MagicMock, patch

import pytest
import requests

from fighthealthinsurance.ml.ml_models import (
    RemoteFullOpenLike,
    _model_card,
    fetch_model_card,
)

VLLM_REPLY = {
    "object": "list",
    "data": [
        {
            "id": "fhi-local",
            "object": "model",
            "owned_by": "vllm",
            "root": "/models/gemma-4-26b-a4b-it-awq",
            "parent": None,
            "max_model_len": 32768,
        },
        {"id": "other-model", "root": "/models/other"},
    ],
}


def test_reads_the_weights_and_context_length_vllm_reports():
    card = _model_card(VLLM_REPLY, "fhi-local", "http://10.0.0.5:8000/v1")
    assert card == {
        "endpoint": "10.0.0.5:8000",
        "model_id": "fhi-local",
        "weights": "/models/gemma-4-26b-a4b-it-awq",
        "parent": "",
        "max_model_len": 32768,
        "owned_by": "vllm",
    }


def test_a_server_that_reports_only_ids_leaves_the_rest_blank():
    card = _model_card({"data": [{"id": "m"}]}, "m", "https://api.example.com/v1")
    assert card["weights"] == "" and card["max_model_len"] is None


def test_no_card_for_a_model_the_server_does_not_list():
    assert _model_card(VLLM_REPLY, "missing", "http://h:1/v1") is None


def test_the_endpoint_never_carries_credentials_or_a_path():
    card = _model_card(VLLM_REPLY, "fhi-local", "http://user:secret@h:9/v1?k=x")
    assert card["endpoint"] == "h:9"


def test_a_passing_health_check_keeps_the_card():
    backend = RemoteFullOpenLike("http://h:8000/v1", "", "fhi-local")
    reply = MagicMock(status_code=200)
    reply.json.return_value = VLLM_REPLY
    with patch("fighthealthinsurance.ml.ml_models.requests.get", return_value=reply):
        assert backend.model_is_ok()
    assert backend.last_model_card["weights"] == "/models/gemma-4-26b-a4b-it-awq"


def test_a_failing_health_check_drops_the_card():
    backend = RemoteFullOpenLike("http://h:8000/v1", "", "fhi-local")
    backend.last_model_card = {"weights": "stale"}
    reply = MagicMock(status_code=200)
    reply.json.return_value = {"data": [{"id": "something-else"}]}
    with patch("fighthealthinsurance.ml.ml_models.requests.get", return_value=reply):
        assert not backend.model_is_ok()
    assert backend.last_model_card is None


@pytest.mark.parametrize(
    "failure",
    [
        {"side_effect": requests.ConnectionError("refused")},
        {"return_value": MagicMock(status_code=503)},
    ],
)
def test_a_probe_that_cannot_reach_the_server_drops_the_card(failure):
    # A card must describe this round's successful probe, never an old one.
    backend = RemoteFullOpenLike("http://h:8000/v1", "", "fhi-local")
    backend.last_model_card = {"weights": "stale"}
    backend.last_backup_model_card = {"weights": "stale"}
    with patch("fighthealthinsurance.ml.ml_models.requests.get", **failure):
        assert not backend.model_is_ok()
    assert backend.last_model_card is None
    assert backend.last_backup_model_card is None


def test_a_backup_leg_on_the_same_server_gets_its_own_card():
    backend = RemoteFullOpenLike(
        "http://h:8000/v1",
        "",
        "fhi-local",
        backup_api_base="http://h:8000/v1",
        backup_model="other-model",
    )
    reply = MagicMock(status_code=200)
    reply.json.return_value = VLLM_REPLY
    with patch("fighthealthinsurance.ml.ml_models.requests.get", return_value=reply):
        assert backend.model_is_ok()
    assert backend.last_backup_model_card["weights"] == "/models/other"


def test_serving_legs():
    one = RemoteFullOpenLike("http://a:1/v1", "", "m")
    assert [leg for leg, _b, _m in one.serving_legs()] == ["primary"]
    same = RemoteFullOpenLike("http://a:1/v1", "", "m", backup_api_base="http://a:1/v1")
    assert len(same.serving_legs()) == 1
    two = RemoteFullOpenLike(
        "http://a:1/v1", "", "m", backup_api_base="http://b:1/v1", backup_model="n"
    )
    assert two.serving_legs()[1] == ("backup", "http://b:1/v1", "n")
    backup_only = RemoteFullOpenLike(None, "", "m", backup_api_base="http://b:1/v1")
    assert [leg for leg, _b, _m in backup_only.serving_legs()] == ["backup"]


def test_fetch_model_card_never_raises():
    with patch(
        "fighthealthinsurance.ml.ml_models.requests.get",
        side_effect=requests.Timeout("slow"),
    ):
        assert fetch_model_card("http://b:1/v1", "m") is None


def _status():
    from fighthealthinsurance.ml import health_status

    status = health_status._HealthStatus()
    status._last_candidates = ["backend-a"]
    return status


def test_the_sweep_hands_its_round_to_the_registry_without_waiting():
    status = _status()
    with patch.object(
        status, "_refresh_unlocked", return_value=(0, 0, [], None)
    ), patch.object(status, "_alert_if_all_internal_dead"), patch.object(
        status, "_schedule_refresh"
    ) as schedule, patch(
        "fighthealthinsurance.ml.serving_registry.record_backends_async"
    ) as record:
        status._refresh()
    record.assert_called_once_with(["backend-a"])
    schedule.assert_called_once()


def test_the_next_round_is_scheduled_even_when_this_one_goes_wrong():
    status = _status()
    with patch.object(
        status, "_refresh_unlocked", return_value=(0, 0, [], None)
    ), patch.object(
        status, "_alert_if_all_internal_dead", side_effect=RuntimeError("smtp down")
    ), patch.object(
        status, "_schedule_refresh"
    ) as schedule:
        with pytest.raises(RuntimeError):
            status._refresh()
    schedule.assert_called_once()


def test_the_first_synchronous_sweep_is_recorded_too():
    status = _status()
    with patch.object(
        status, "_refresh_unlocked", return_value=(1, 1, [], None)
    ), patch.object(status, "_ensure_sweep_scheduled"), patch(
        "fighthealthinsurance.ml.serving_registry.record_backends_async"
    ) as record:
        status.get_snapshot()
    record.assert_called_once_with(["backend-a"])
