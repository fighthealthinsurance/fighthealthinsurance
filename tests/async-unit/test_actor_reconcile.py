"""Guards for the Ray polling-actor reconciler.

The polling actors are created once per deploy and are detached, so they
survive the pod that made them but not the Ray head. When a node reboot took
the head, Ray recovered and the actors did not: production ran 0 of 5 alive,
with email polling and three refresh loops silently stopped, until someone
read the status page. Only a deploy would have restored them.
"""

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[2]
COMMAND = (
    REPO
    / "fighthealthinsurance"
    / "management"
    / "commands"
    / "reconcile_polling_actors.py"
)
CRONJOB = REPO / "k8s" / "actor-reconcile-cronjob.yaml"
ALERTS = REPO / "k8s" / "actor-reconcile-alerts.yaml"
ENTRYPOINT = REPO / "scripts" / "start-server.sh"
BUILD = REPO / "scripts" / "build.sh"


class TestReconcileCommand:
    def test_it_refuses_to_run_without_a_real_cluster(self):
        """Ray auto-initializes on first use and that is NOT a no-op: with no
        cluster configured it starts a brand-new LOCAL one in this process.
        The reconciler would then find every actor missing, "restore" them
        into a cluster that dies with the pod, and report success -- worse
        than doing nothing, because it would look fixed."""
        src = COMMAND.read_text()
        assert "ray_cluster_available()" in src, src
        # The gate has to come before anything that touches Ray.
        assert src.index("ray_cluster_available()") < src.index(
            "check_actor_health()"
        ), src
        # ...and it is a failure, not a quiet success.
        gate = src[src.index("ray_cluster_available()") :]
        assert "sys.exit(1)" in gate[: gate.index("before = ")], gate

    def test_it_only_relaunches_what_is_missing(self):
        """A force relaunch on a schedule would kill healthy actors mid-work
        every five minutes."""
        src = COMMAND.read_text()
        assert "relaunch_actors(force=False)" in src, src
        assert "force=True" not in src, src

    def test_a_healthy_cluster_costs_one_health_check(self):
        """It runs every five minutes forever; doing work when nothing is
        wrong is how a reconciler becomes the problem."""
        src = COMMAND.read_text()
        # Returns before relaunching when nothing is missing.
        assert re.search(r"if not missing:.*?return", src, re.S), src
        assert src.index("if not missing:") < src.index("relaunch_actors("), src

    def test_success_is_measured_after_the_relaunch(self):
        """An actor can be created and still fail to come up, so the exit code
        must reflect a fresh health check rather than what relaunch claimed."""
        src = COMMAND.read_text()
        assert src.count("check_actor_health()") >= 2, src
        assert "still_missing" in src, src
        # Non-zero when any actor is still absent, so a CronJob failure means
        # "could not restore" and the alert is real.
        tail = src[src.index("still_missing") :]
        assert "sys.exit(1)" in tail, tail


class TestReconcileSchedule:
    def test_runs_are_never_overlapped(self):
        """Two pods creating the same detached actor at once is exactly the
        race get-or-create cannot win."""
        text = CRONJOB.read_text()
        assert "concurrencyPolicy: Forbid" in text, text

    def test_it_does_not_retry_into_a_cluster_that_is_still_down(self):
        """The schedule is the retry. A backoff loop would stack attempts."""
        text = CRONJOB.read_text()
        assert "backoffLimit: 0" in text, text
        assert "activeDeadlineSeconds:" in text, text

    def test_it_is_sized_for_the_image_it_runs(self):
        """The intake outbox relay was OOMKilled on every run for months
        because it was sized below this image's Django/ML import footprint."""
        text = CRONJOB.read_text()
        assert "memory: 1536Mi" in text, text
        assert "memory: 3Gi" in text, text

    def test_the_entrypoint_dispatches_it(self):
        text = ENTRYPOINT.read_text()
        assert "RECONCILE_POLLING_ACTORS" in text, text
        assert "python manage.py reconcile_polling_actors" in text, text

    def test_the_deploy_applies_it(self):
        text = BUILD.read_text()
        assert "k8s/actor-reconcile-cronjob.yaml" in text, text
        assert "k8s/actor-reconcile-alerts.yaml" in text, text


class TestReconcileAlert:
    def test_it_can_fire_for_a_reconciler_broken_from_birth(self):
        """kube-state-metrics emits NO last_successful_time series for a
        CronJob that has never succeeded, so a single-armed expression is an
        empty vector and cannot fire for the one case that matters. That exact
        hole kept the intake outbox relay silent for six hours."""
        text = ALERTS.read_text()
        assert "absent(kube_cronjob_status_last_successful_time" in text, text
        assert "kube_cronjob_created" in text, text

    def test_the_arm_is_joined_with_on(self):
        """absent() yields only the labels in its matcher while
        kube_cronjob_created carries namespace/job/instance, so a bare `and`
        matches no label set and is silently empty."""
        text = ALERTS.read_text()
        assert "and on()" in text, text

    def test_both_selectors_pin_the_namespace(self):
        """Otherwise absent() can be satisfied by a same-named CronJob
        elsewhere in the cluster."""
        text = ALERTS.read_text()
        assert text.count('namespace="totallylegitco"') >= 3, text

    def test_the_runbook_does_not_point_at_last_schedule(self):
        """`kubectl get cronjob` shows LAST SCHEDULE, which keeps advancing
        every five minutes even while every single run fails -- it reads
        healthy during exactly this failure."""
        text = ALERTS.read_text()
        assert ".status.lastSuccessfulTime" in text, text
