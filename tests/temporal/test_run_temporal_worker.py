"""Role selection for ``run_temporal_worker``: the queue split must fail
loudly on a bad role and never silently host the wrong queues."""

import asyncio
import os
from unittest.mock import patch

import pytest
from django.core.management.base import CommandError

from fighthealthinsurance.management.commands.run_temporal_worker import (
    QUEUE_ROLES,
    Command,
)


def test_known_roles_are_exactly_fax_appeal_all():
    assert QUEUE_ROLES == ("fax", "appeal", "all")


@patch.dict(os.environ, {"TEMPORAL_WORKER_QUEUES": "bogus"})
def test_bad_env_role_fails_before_connecting():
    """argparse validates --queues, but the env var path must be checked
    too -- a typo'd Deployment env must crash loudly, not default to
    hosting every queue."""
    with pytest.raises(CommandError):
        asyncio.run(Command()._run({}))


def _recording_worker_cls():
    """Stand-in for temporalio.worker.Worker capturing construction kwargs;
    run() completes immediately so _run's gather returns."""
    from unittest.mock import AsyncMock, Mock

    cls = Mock()

    def _make(*args, **kwargs):
        inst = Mock()
        inst.run = AsyncMock()
        return inst

    cls.side_effect = _make
    return cls


def _run_with(role, **flags):
    from django.test import override_settings
    from unittest.mock import AsyncMock, Mock

    worker_cls = _recording_worker_cls()
    settings = dict(
        TEMPORAL_ENABLED=True,
        TEMPORAL_APPEAL_JOURNEY_ENABLED=True,
        TEMPORAL_INTAKE_JOURNEY_ENABLED=False,
        TEMPORAL_TASK_QUEUE="q-fax",
        TEMPORAL_APPEAL_TASK_QUEUE="q-appeal",
        TEMPORAL_HOST="test-host",
        TEMPORAL_NAMESPACE="test-ns",
    )
    settings.update(flags)
    with (
        patch("temporalio.worker.Worker", worker_cls),
        patch(
            "fighthealthinsurance.temporal_client.get_temporal_client",
            AsyncMock(return_value=Mock()),
        ),
        override_settings(**settings),
    ):
        asyncio.run(asyncio.wait_for(Command()._run({"queues": role}), timeout=2))
    return [c.kwargs.get("task_queue") for c in worker_cls.call_args_list]


def test_fax_role_hosts_only_the_fax_queue():
    assert _run_with("fax") == ["q-fax"]


def test_appeal_role_hosts_only_the_appeal_queue():
    assert _run_with("appeal") == ["q-appeal"]


def test_all_role_hosts_both_queues():
    assert _run_with("all") == ["q-fax", "q-appeal"]


def test_all_role_with_journey_dark_hosts_fax_only():
    assert _run_with("all", TEMPORAL_APPEAL_JOURNEY_ENABLED=False) == ["q-fax"]


def test_appeal_role_with_journey_dark_idles_hosting_nothing():
    """The kill switch: role=appeal + dark flags must construct NO worker
    and block (idle) rather than exit into a Deployment crash-loop."""
    with pytest.raises(asyncio.TimeoutError):
        _run_with("appeal", TEMPORAL_APPEAL_JOURNEY_ENABLED=False)


def test_deploy_script_applies_both_worker_manifests():
    """Temporal queues work for a pollerless task queue silently, so a
    deploy that forgets the appeal worker looks healthy while nothing
    executes. The deploy script must apply BOTH worker manifests, and the
    manifests must pin complementary queue roles (external review)."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[2]
    build = (root / "scripts" / "build.sh").read_text()
    assert "k8s/temporal/worker.yaml" in build
    assert "k8s/temporal/appeal-worker.yaml" in build

    roles = {}
    for name in ("worker.yaml", "appeal-worker.yaml"):
        text = (root / "k8s" / "temporal" / name).read_text()
        m = re.search(
            r'name: TEMPORAL_WORKER_QUEUES\s*\n\s*value: "(\w+)"', text
        )
        assert m, f"{name} must pin TEMPORAL_WORKER_QUEUES"
        roles[name] = m.group(1)
    assert roles == {"worker.yaml": "fax", "appeal-worker.yaml": "appeal"}
