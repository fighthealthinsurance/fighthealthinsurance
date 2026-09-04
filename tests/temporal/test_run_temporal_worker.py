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


def test_metrics_runtime_is_off_when_unset():
    from fighthealthinsurance.management.commands.run_temporal_worker import (
        metrics_runtime,
    )

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("TEMPORAL_METRICS_BIND", None)
        assert metrics_runtime() is None


def test_metrics_runtime_builds_prometheus_config_when_set():
    """Runtime exposes no config introspection (and binds the port on
    construction), so capture what it was built WITH."""
    from unittest.mock import Mock

    from temporalio.runtime import PrometheusConfig

    from fighthealthinsurance.management.commands.run_temporal_worker import (
        metrics_runtime,
    )

    runtime_cls = Mock()
    with (
        patch.dict(os.environ, {"TEMPORAL_METRICS_BIND": "0.0.0.0:9464"}),
        patch("temporalio.runtime.Runtime", runtime_cls),
    ):
        metrics_runtime()
    cfg = runtime_cls.call_args.kwargs["telemetry"].metrics
    assert isinstance(cfg, PrometheusConfig)
    assert cfg.bind_address == "0.0.0.0:9464"
    assert cfg.durations_as_seconds is True


def test_worker_passes_metrics_runtime_to_the_client():
    """The runtime reaches Client.connect only through get_temporal_client's
    runtime kwarg; web/Ray callers never pass one."""
    from django.test import override_settings
    from unittest.mock import AsyncMock, Mock

    connect = AsyncMock(return_value=Mock())
    sentinel_runtime = Mock(name="sentinel-runtime")
    with (
        patch.dict(os.environ, {"TEMPORAL_METRICS_BIND": "127.0.0.1:9464"}),
        patch(
            "fighthealthinsurance.management.commands.run_temporal_worker."
            "metrics_runtime",
            return_value=sentinel_runtime,
        ),
        patch("temporalio.worker.Worker", _recording_worker_cls()),
        patch("fighthealthinsurance.temporal_client.get_temporal_client", connect),
        override_settings(
            TEMPORAL_ENABLED=True,
            TEMPORAL_APPEAL_JOURNEY_ENABLED=False,
            TEMPORAL_TASK_QUEUE="q-fax",
            TEMPORAL_HOST="test-host",
            TEMPORAL_NAMESPACE="test-ns",
        ),
    ):
        asyncio.run(asyncio.wait_for(Command()._run({"queues": "fax"}), timeout=2))
    assert connect.call_args.kwargs.get("runtime") is sentinel_runtime


def test_worker_manifests_are_redundant_and_scraped():
    """Review-5 finding 8: two pollers per queue, a PDB per Deployment, a
    hostname spread constraint, and a metrics port on both workers."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[2]
    tdir = root / "k8s" / "temporal"
    for name in ("worker.yaml", "appeal-worker.yaml"):
        text = (tdir / name).read_text()
        replicas = int(re.search(r"^\s*replicas:\s*(\d+)", text, re.M).group(1))
        assert replicas >= 2, f"{name} must run >=2 replicas"
        assert "topologySpreadConstraints:" in text, f"{name} needs a spread constraint"
        assert "kubernetes.io/hostname" in text
        assert re.search(r"name: metrics\s*\n\s*containerPort: 9464", text), (
            f"{name} must expose the metrics port"
        )
        assert 'name: TEMPORAL_METRICS_BIND' in text
    pdb = (tdir / "worker-pdb.yaml").read_text()
    assert pdb.count("kind: PodDisruptionBudget") == 2
    for group in (
        "fight-health-insurance-prod-temporal-worker",
        "fight-health-insurance-prod-temporal-appeal-worker",
    ):
        assert group in pdb, f"PDB missing for {group}"
    build = (root / "scripts" / "build.sh").read_text()
    for manifest in ("worker-pdb.yaml", "worker-podmonitor.yaml", "worker-alerts.yaml"):
        assert f"k8s/temporal/{manifest}" in build, f"build.sh must apply {manifest}"


def _run_with_options(options, **flags):
    """Like _run_with but with arbitrary command options (task_queue etc.)."""
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
        asyncio.run(asyncio.wait_for(Command()._run(options), timeout=2))
    return [c.kwargs for c in worker_cls.call_args_list]


def test_task_queue_override_applies_to_the_selected_role():
    """--task-queue must override the queue the selected role actually
    polls; for the appeal role it previously set a fax queue that role
    never used (review)."""
    fax = _run_with_options({"queues": "fax", "task_queue": "custom"})
    assert [k["task_queue"] for k in fax] == ["custom"]
    appeal = _run_with_options({"queues": "appeal", "task_queue": "custom"})
    assert [k["task_queue"] for k in appeal] == ["custom"]
    both = _run_with_options({"queues": "all", "task_queue": "custom"})
    assert [k["task_queue"] for k in both] == ["custom", "q-appeal"]


def test_fax_worker_slots_match_the_thread_executor():
    """More activity slots than executor threads would let accepted
    activities queue locally while their start-to-close clock runs."""
    kwargs = _run_with_options({"queues": "fax", "max_workers": 7})
    assert kwargs[0]["max_concurrent_activities"] == 7


def test_alert_rules_use_the_workers_temporal_namespace_and_cover_worker_loss():
    """Review-9: rules filtered on the Kubernetes namespace could never match
    SDK series (which carry the Temporal namespace, 'default'); and worker-
    side series vanish with the last worker, so worker-loss must be alerted
    from scrape/kube-state data and server-side metrics."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[2]
    tdir = root / "k8s" / "temporal"
    configured = set()
    for name in ("worker.yaml", "appeal-worker.yaml"):
        text = (tdir / name).read_text()
        m = re.search(r'name: TEMPORAL_NAMESPACE\s*\n\s*value: "([^"]+)"', text)
        assert m, f"{name} must pin TEMPORAL_NAMESPACE"
        configured.add(m.group(1))
    assert len(configured) == 1
    (ns,) = configured

    rules = (tdir / "worker-alerts.yaml").read_text()
    # Every Temporal-namespace matcher in a PromQL expr must be the configured
    # one; the Kubernetes namespace may only appear as a kube_* label or in
    # the manifest's own metadata.
    exprs = re.findall(r"expr:\s*>-\s*\n((?:\s{12}.*\n)+)", rules)
    assert exprs, "no rule expressions parsed"
    for expr in exprs:
        for label_ns in re.findall(r'(?<!kube_)\bnamespace="([^"]+)"', expr):
            if "kube_deployment" in expr:
                continue  # kube-state label: Kubernetes namespace is correct
            assert label_ns == ns, f"rule filters on Temporal namespace {label_ns!r}, workers use {ns!r}"
    for needle in (
        "absent(up{",
        "kube_deployment_status_replicas_available",
        "approximate_backlog_count",
        "FhiTemporalFaxWorkerAbsent",
        "FhiTemporalAppealWorkerAbsent",
    ):
        assert needle in rules, f"worker-loss/server-side coverage missing: {needle}"
