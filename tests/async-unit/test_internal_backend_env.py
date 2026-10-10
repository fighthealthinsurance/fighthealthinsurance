"""Internal (vLLM) backend configuration from the environment, without Ray
or a server."""

import os
from unittest.mock import patch

from fighthealthinsurance.ml.ml_models import (
    AlphaRemoteInternal,
    NewRemoteInternal,
    RemoteHealthInsurance,
    _internal_model_name,
)


class TestInternalModelNames:
    def test_the_friendly_name_is_the_last_path_segment(self):
        assert _internal_model_name("/models/fhi-2025-nov") == "fhi-2025-nov"

    def test_a_trailing_slash_is_not_a_name(self):
        """A path ending in "/" used to register a model named ""."""
        assert _internal_model_name("/models/fhi-2025-nov/") == "fhi-2025-nov"

    def test_a_blank_model_env_means_the_default(self):
        """k8s templating materialises unset values as empty strings; a blank
        wire model sent model "" on every request."""
        with patch.dict(os.environ, {"NEW_HEALTH_BACKEND_MODEL": ""}):
            [desc] = NewRemoteInternal.models()
        assert desc.name == "fhi-2025-may-0.3-float16-q8-vllm-compressed"
        with patch.dict(os.environ, {"ALPHA_HEALTH_BACKEND_MODEL": ""}):
            [desc] = AlphaRemoteInternal.models()
        assert desc.name == "fhi-2025-nov-q8-vllm-compressed"
        with patch.dict(os.environ, {"HEALTH_BACKEND_MODEL": ""}):
            [desc] = RemoteHealthInsurance.models()
        assert desc.internal_name == "totallylegitco/fighthealthinsurance_model_v0.5"


class TestAlphaBackup:
    def test_a_backup_host_alone_gets_the_primary_port_and_model(self):
        """The backup port defaulted to None, so a backup host set on its own
        was discarded, and the backup model was the literal "/app/model"."""
        env = {
            "ALPHA_HEALTH_BACKEND_HOST": "alpha",
            "ALPHA_HEALTH_BACKEND_PORT": "",
            "ALPHA_HEALTH_BACKUP_BACKEND_HOST": "alpha-backup",
            "ALPHA_HEALTH_BACKUP_BACKEND_PORT": "",
            "ALPHA_HEALTH_BACKUP_BACKEND_MODEL": "",
        }
        with patch.dict(os.environ, env):
            backend = AlphaRemoteInternal(model="/models/x")
        assert backend.backup_api_base == "http://alpha-backup:8000/v1"
        assert backend.backup_model == "/models/x"
