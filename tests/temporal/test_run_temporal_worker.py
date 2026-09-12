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


@pytest.fixture(autouse=True)
def _preserve_signal_dispositions():
    """Every test that reaches _run() replaces the SIGTERM and SIGINT loop
    handlers, and asyncio's loop close resets them to the defaults rather
    than to what the test runner had. Put both back after each test
    (review)."""
    import signal as _signal

    saved = {s: _signal.getsignal(s) for s in (_signal.SIGTERM, _signal.SIGINT)}
    yield
    for s, h in saved.items():
        if h is not None:
            _signal.signal(s, h)


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
        m = re.search(r'name: TEMPORAL_WORKER_QUEUES\s*\n\s*value: "(\w+)"', text)
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
        assert re.search(
            r"name: metrics\s*\n\s*containerPort: 9464", text
        ), f"{name} must expose the metrics port"
        assert "name: TEMPORAL_METRICS_BIND" in text
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
            assert (
                label_ns == ns
            ), f"rule filters on Temporal namespace {label_ns!r}, workers use {ns!r}"
    for needle in (
        "absent(up{",
        "kube_deployment_status_replicas_available",
        "approximate_backlog_count",
        "FhiTemporalFaxWorkerAbsent",
        "FhiTemporalAppealWorkerAbsent",
    ):
        assert needle in rules, f"worker-loss/server-side coverage missing: {needle}"


def _draining_worker_cls(shutdown_calls):
    """Worker stub whose run() only returns once shutdown() has been called,
    like the real one: this is what makes a dropped SIGTERM observable."""
    from unittest.mock import Mock

    cls = Mock()

    def _make(*args, **kwargs):
        inst = Mock()
        done = asyncio.Event()

        async def run():
            await done.wait()

        async def shutdown():
            shutdown_calls.append(kwargs.get("task_queue"))
            done.set()

        inst.run = run
        inst.shutdown = shutdown
        return inst

    cls.side_effect = _make
    return cls


def _run_until_signal(role, sig, command=None, **flags):
    """Start _run, deliver ``sig`` to this process once _run has installed
    its handlers, and return the task queues whose worker was shut down.

    Two guards keep a real signal safe inside pytest (review): a Python-level
    fallback handler is installed first, so if _run installs no loop handler
    the signal is recorded and asserted on instead of terminating the test
    runner; and the kill waits for an explicit readiness handshake around
    install_shutdown_handlers rather than a sleep.
    """
    import os
    import signal as _signal
    from django.test import override_settings
    from unittest.mock import AsyncMock, Mock

    import fighthealthinsurance.management.commands.run_temporal_worker as cmd

    shutdown_calls: list = []
    leaked: list = []
    ready = asyncio.Event()
    real_install = cmd.install_shutdown_handlers

    def _install_and_signal_ready(*args, **kwargs):
        real_install(*args, **kwargs)
        ready.set()

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

    async def main():
        task = asyncio.create_task((command or Command())._run({"queues": role}))
        await asyncio.wait_for(ready.wait(), timeout=2)
        await asyncio.sleep(0)  # let _run reach its first await past install
        os.kill(os.getpid(), sig)
        await asyncio.wait_for(task, timeout=2)

    _signal.signal(sig, lambda signum, frame: leaked.append(signum))
    with (
        patch("temporalio.worker.Worker", _draining_worker_cls(shutdown_calls)),
        patch(
            "fighthealthinsurance.temporal_client.get_temporal_client",
            AsyncMock(return_value=Mock()),
        ),
        patch.object(cmd, "install_shutdown_handlers", _install_and_signal_ready),
        override_settings(**settings),
    ):
        asyncio.run(main())
    assert not leaked, "signal reached the fallback handler: _run installed none"
    return shutdown_calls


def test_sigterm_shuts_workers_down_instead_of_being_dropped():
    """Regression: as PID 1 in the container the process had no SIGTERM
    handler, the kernel discarded the signal, and the old worker kept
    polling until SIGKILL at the end of the grace period."""
    import signal

    calls = _run_until_signal("all", signal.SIGTERM)
    assert sorted(calls) == ["q-appeal", "q-fax"]


def test_sigint_shuts_workers_down_too():
    import signal

    calls = _run_until_signal("fax", signal.SIGINT)
    assert calls == ["q-fax"]


def test_idle_appeal_role_exits_on_sigterm():
    """The dark-journey idle branch must not sleep through the grace period."""
    import signal

    calls = _run_until_signal(
        "appeal", signal.SIGTERM, TEMPORAL_APPEAL_JOURNEY_ENABLED=False
    )
    assert calls == []  # nothing hosted, but _run returned within the timeout


def test_sigterm_during_connect_exits_without_hosting():
    """A signal while the client connect is still pending must not be lost,
    and the pod must not start polling afterwards (review)."""
    import os
    import signal as _signal
    from django.test import override_settings
    from unittest.mock import Mock

    worker_cls = _recording_worker_cls()
    connect_started = asyncio.Event()
    release = asyncio.Event()

    async def slow_connect(**kwargs):
        connect_started.set()
        await release.wait()
        return Mock()

    async def main():
        task = asyncio.create_task(Command()._run({"queues": "fax"}))
        await asyncio.wait_for(connect_started.wait(), timeout=2)
        os.kill(os.getpid(), _signal.SIGTERM)
        await asyncio.wait_for(task, timeout=2)
        release.set()

    leaked: list = []
    _signal.signal(_signal.SIGTERM, lambda signum, frame: leaked.append(signum))
    with (
        patch("temporalio.worker.Worker", worker_cls),
        patch(
            "fighthealthinsurance.temporal_client.get_temporal_client",
            slow_connect,
        ),
        override_settings(
            TEMPORAL_ENABLED=True,
            TEMPORAL_TASK_QUEUE="q-fax",
            TEMPORAL_HOST="test-host",
            TEMPORAL_NAMESPACE="test-ns",
        ),
    ):
        asyncio.run(main())
    assert not leaked
    assert worker_cls.call_args_list == []  # no worker was ever constructed


def test_sigterm_caught_before_the_loop_exists_still_stops_startup():
    """Django bootstrap and the lazy imports run before the asyncio loop; a
    SIGTERM caught by the early handler in that window must still end the
    process before it hosts a worker (review)."""
    import signal as _signal
    from django.test import override_settings
    from unittest.mock import AsyncMock, Mock

    from fighthealthinsurance.worker_signals import early_stop

    worker_cls = _recording_worker_cls()
    before = _signal.getsignal(_signal.SIGTERM)
    event = early_stop.install()
    try:
        assert _signal.getsignal(_signal.SIGTERM) == early_stop._handler
        early_stop._handler(_signal.SIGTERM, None)  # what the kernel would do
        assert event.is_set()
        with (
            patch("temporalio.worker.Worker", worker_cls),
            patch(
                "fighthealthinsurance.temporal_client.get_temporal_client",
                AsyncMock(return_value=Mock()),
            ),
            override_settings(
                TEMPORAL_ENABLED=True,
                TEMPORAL_TASK_QUEUE="q-fax",
                TEMPORAL_HOST="test-host",
                TEMPORAL_NAMESPACE="test-ns",
            ),
        ):
            asyncio.run(
                asyncio.wait_for(
                    Command()._run({"queues": "fax"}, early_stop=event), timeout=2
                )
            )
    finally:
        early_stop.restore()
    # restore() hands back the previous disposition and a FRESH flag, so
    # one early stop cannot blank a later invocation (review).
    assert _signal.getsignal(_signal.SIGTERM) == before
    assert early_stop.event is not event and not early_stop.event.is_set()
    assert worker_cls.call_args_list == []


def test_handle_installs_and_restores_the_early_sigterm_handler():
    import signal as _signal

    from fighthealthinsurance.worker_signals import early_stop

    seen = {}

    async def fake_run(self, options, early_stop=None):
        seen["handler"] = _signal.getsignal(_signal.SIGTERM)
        seen["flag"] = early_stop

    before = _signal.getsignal(_signal.SIGTERM)
    with patch.object(Command, "_run", fake_run):
        Command().handle(queues="fax")
    assert seen["handler"] == early_stop._handler
    assert seen["flag"] is not None and not seen["flag"].is_set()
    assert _signal.getsignal(_signal.SIGTERM) == before  # restored after


def test_manage_py_installs_the_guard_before_django_bootstraps():
    """The window before handle() -- Django setup and system checks -- is
    covered only if manage.py installs the guard first (review)."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    text = (root / "manage.py").read_text()
    guard = text.index("early_stop.install()")
    app_import = text.index("from fighthealthinsurance.utils import")
    django = text.index("execute_from_command_line(sys.argv)")
    # Above the app import too: fighthealthinsurance.utils pulls Django mail,
    # models and templates in at import time (review).
    assert guard < app_import < django
    assert "fighthealthinsurance.worker_signals" in text


def test_worker_signals_imports_nothing_from_django():
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    tree = ast.parse((root / "fighthealthinsurance" / "worker_signals.py").read_text())
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    assert modules <= {"signal", "typing"}, modules


def test_early_stop_flag_is_lock_free():
    from fighthealthinsurance.worker_signals import Flag, early_stop

    flag = Flag()
    assert not flag.is_set()
    flag.set()
    assert flag.is_set()
    assert not hasattr(flag, "_cond")  # not a threading.Event in disguise
    assert isinstance(early_stop.event, Flag)


def _fake_run_that_replaces_both_signals(raise_after=None):
    """A stand-in _run that does what the real one does to the process:
    installs loop handlers for SIGTERM and SIGINT (asyncio resets both to
    the defaults on loop close), then optionally raises."""

    async def fake_run(self, options, early_stop=None):
        loop = asyncio.get_running_loop()
        for sig in (_signal_module().SIGTERM, _signal_module().SIGINT):
            loop.add_signal_handler(sig, lambda: None)
        if raise_after is not None:
            raise raise_after

    return fake_run


def _signal_module():
    import signal as _signal

    return _signal


def test_handle_restores_both_signals_after_success_and_after_failure():
    """Regression for the round-5 review: the earlier test's fake _run never
    replaced SIGINT, so dropping the restoration would still have passed."""
    from django.core.management.base import CommandError

    _signal = _signal_module()
    marker_term = lambda signum, frame: None  # noqa: E731
    marker_int = lambda signum, frame: None  # noqa: E731
    for raise_after in (None, CommandError("boom")):
        _signal.signal(_signal.SIGTERM, marker_term)
        _signal.signal(_signal.SIGINT, marker_int)
        with patch.object(
            Command, "_run", _fake_run_that_replaces_both_signals(raise_after)
        ):
            if raise_after is None:
                Command().handle(queues="fax")
            else:
                with pytest.raises(CommandError):
                    Command().handle(queues="fax")
        assert _signal.getsignal(_signal.SIGTERM) is marker_term
        assert _signal.getsignal(_signal.SIGINT) is marker_int


def test_handle_does_not_resurrect_a_released_bootstrap_guard():
    """manage.py installs the guard before handle() runs. handle()'s own
    snapshot then sees the guard's handler; after restore() releases it,
    handle() must not put it back (review, round 5)."""
    from fighthealthinsurance.worker_signals import early_stop

    _signal = _signal_module()
    before_guard = lambda signum, frame: None  # noqa: E731
    _signal.signal(_signal.SIGTERM, before_guard)
    early_stop.install()  # what manage.py does
    assert _signal.getsignal(_signal.SIGTERM) == early_stop._handler
    with patch.object(Command, "_run", _fake_run_that_replaces_both_signals()):
        Command().handle(queues="fax")
    assert _signal.getsignal(_signal.SIGTERM) is before_guard


def test_native_signal_handlers_are_left_alone():
    """A previous handler of None means it was installed outside Python and
    could never be handed back, so neither the bootstrap guard nor the loop
    handlers may take it (review)."""
    from fighthealthinsurance import worker_signals
    from fighthealthinsurance.management.commands.run_temporal_worker import (
        install_shutdown_handlers,
    )

    _signal = _signal_module()
    with patch.object(_signal, "getsignal", return_value=None), patch.object(
        _signal, "signal"
    ) as set_signal:
        guard = worker_signals.EarlyStop()
        guard.install()
        set_signal.assert_not_called()
        guard.restore()  # nothing installed: nothing to put back
        set_signal.assert_not_called()

        async def main():
            loop = asyncio.get_running_loop()
            with patch.object(loop, "add_signal_handler") as add:
                install_shutdown_handlers([], asyncio.Event(), lambda m: None)
                add.assert_not_called()

        asyncio.run(main())


def test_shutdown_is_scheduled_even_when_the_log_write_fails():
    """stop.set() before a failing log write used to leave the workers
    polling forever, with every later signal a no-op (review, round 5)."""
    import signal
    from unittest.mock import Mock

    def write(text):
        # Startup lines still print; only the signal's own log line finds the
        # pipe gone (that is the failure mode: stdout's reader left first).
        if "received: stopping polling" in text:
            raise BrokenPipeError
        return None

    cmd = Command()
    cmd.stdout = Mock()
    cmd.stdout.write = Mock(side_effect=write)
    calls = _run_until_signal("fax", signal.SIGTERM, command=cmd)
    assert calls == ["q-fax"]
    assert any(
        "received: stopping polling" in c.args[0]
        for c in cmd.stdout.write.call_args_list
    )


def test_handle_off_the_main_thread_leaves_a_preinstalled_guard_alone():
    """A guard installed by manage.py on the main thread, then handle()
    invoked from a worker thread (library use): the thread can neither
    acquire nor release signal handlers, so it must not try, and must not
    turn a successful run into a ValueError in finally (review, round 6)."""
    import threading

    from fighthealthinsurance.worker_signals import early_stop

    _signal = _signal_module()
    early_stop.install()  # main thread, as manage.py would
    guard = _signal.getsignal(_signal.SIGTERM)
    assert guard == early_stop._handler

    async def fake_run(self, options, early_stop=None):
        return None  # no loop signal handlers: not allowed off-main-thread

    outcome = {}

    def run_in_thread():
        try:
            with patch.object(Command, "_run", fake_run):
                Command().handle(queues="fax")
            outcome["ok"] = True
        except Exception as e:  # pragma: no cover - the assertion below reports it
            outcome["error"] = e

    try:
        th = threading.Thread(target=run_in_thread)
        th.start()
        th.join(timeout=5)
        assert outcome.get("ok"), outcome.get("error")
        # Still installed: the thread did not release what it never owned.
        assert _signal.getsignal(_signal.SIGTERM) == early_stop._handler
    finally:
        early_stop.restore()
