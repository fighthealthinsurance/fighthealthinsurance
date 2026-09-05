#!/bin/bash
set -ex
# Every pipe in this script is `envsubst < manifest | kubectl apply -f -`.
# Without pipefail only kubectl's status counts, so an envsubst that died
# partway through a multi-document manifest would apply the prefix it had
# already emitted and report success (external review).
set -o pipefail

SCRIPT_DIR="$(dirname "$0")"

BUILDX_CMD=${BUILDX_CMD:-push}

# --no-build: assume the container images are already built and pushed, and only
# run the kubectl deploy steps. This skips every build step -- template/static
# setup (mypy, migrations, collectstatic, npm build) and the image builds -- so
# the script can run on a deploy-only host or CI job that has just kubectl and
# envsubst (no Python/Node build toolchain).
#
# --skip-journey-gates: apply everything, but do not BLOCK the deploy on
# anything -- not the rollouts, not the writer drain, not the fingerprint
# backfill Job. The Job is still applied and still runs (with its own
# backoff); you just stop waiting for it here. Use it when you want the deploy
# to land promptly and intend to check the Job yourself -- and note the
# invariant it guards: until that strict pass is quiet, a
# `chosen=False, text_fingerprint IS NULL` row does not yet reliably mean
# "known duplicate", so journey counting should not be trusted (which matters
# only once the journey flags are on).
#
# --skip-backfill: do not apply or wait for the backfill Job, or for the
# writer drain that exists only to make that Job meaningful. The rollout waits
# still run -- a Deployment that never finishes rolling is a broken deploy
# whether or not we are fingerprinting anything.
#
# Worst-case waiting with no flags: 45m of rollouts + 20m of writer drain +
# 25m of Ray + 30m of backfill = about two hours. In practice the drains are
# already most-done by the time the rollouts report, and the backfill on a
# quiet table takes a couple of minutes.
NO_BUILD=false
SKIP_JOURNEY_GATES=false
SKIP_BACKFILL=false
usage() {
    echo "Usage: $0 [--no-build] [--skip-journey-gates] [--skip-backfill]"
    echo "  --no-build             Skip all build steps (template/static setup and image"
    echo "                         builds); assume images are already built and pushed."
    echo "  --skip-journey-gates   Apply everything but block on nothing: no rollout"
    echo "                         waits, no writer drain, no backfill wait (up to ~2h"
    echo "                         of waiting). The Job still runs; you check it yourself."
    echo "  --skip-backfill        Do not apply or wait for the fingerprint backfill Job,"
    echo "                         or for the writer drain that only that Job needs."
    echo "                         Rollout waits still run."
}
for arg in "$@"; do
    case "$arg" in
        --no-build)
            NO_BUILD=true
            ;;
        --skip-journey-gates)
            SKIP_JOURNEY_GATES=true
            ;;
        --skip-backfill)
            SKIP_BACKFILL=true
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown argument: $arg" >&2
            usage >&2
            exit 1
            ;;
    esac
done

# Prepare build artifacts (mypy, migrations check, collectstatic, npm build).
# These are only needed to build the images, so skip them with --no-build; a
# deploy-only host may not have the Python/Node build toolchain installed.
if [ "$NO_BUILD" = true ]; then
    echo "--no-build: skipping template/static setup (setup_templates.sh)"
else
    source "${SCRIPT_DIR}/setup_templates.sh"
fi

# BUILDKIT_NO_CLIENT_TOKEN=true
FHI_VERSION=v0.23.3a


MYORG=${MYORG:-totallylegitco}
RAY_BASE=${RAY_BASE:-${MYORG}/fhi-ray}
FHI_BASE=${FHI_BASE:-${MYORG}/fhi-base}
FHI_DOCKER_USERNAME=${FHI_DOCKER_USERNAME:-holdenk}
FHI_DOCKER_EMAIL=${FHI_DOCKER_EMAIL:-"holden@pigscanfly.ca"}

export BUILDKIT_NO_CLIENT_TOKEN
export FHI_VERSION
export FHI_BASE
export RAY_BASE
export MYORG

# Build the django dev container first
FHI_VERSION_OG=${FHI_VERSION}
FHI_VERSION=${FHI_VERSION}-dev
export FHI_VERSION

# Build the dev containers (skipped with --no-build; assumes image already exists)
if [ "$NO_BUILD" = true ]; then
    echo "--no-build: skipping django image build (${FHI_BASE}:${FHI_VERSION})"
else
    source "${SCRIPT_DIR}/build_django.sh"
fi

# Deploy dev
envsubst < k8s/deploy_dev.yaml | kubectl delete -f - || echo "No existing dev deployment present"
envsubst < k8s/deploy_dev.yaml | kubectl apply -f -
read -rp "Have you checked dev and are ready to deploy to staging? (y/n) " yn
case $yn in
    [Yy]* ) echo "Proceeding...";;
    [Nn]* ) echo "Exiting..."; exit;;
    * ) echo "Invalid response; treating as no. Exiting..."; exit 1;;
esac

# Reset to non-dev
FHI_VERSION_OG=${FHI_VERSION}
FHI_VERSION=${FHI_VERSION_OG}
export FHI_VERSION

# Build the ray container -- we don't use it in staging *BUT*
# better to have built than be stuck with a half deployed system if
# dockerhub is having a day.
# (skipped with --no-build; assumes image already exists)
if [ "$NO_BUILD" = true ]; then
    echo "--no-build: skipping ray image build (${RAY_BASE}:${FHI_VERSION})"
else
    source "${SCRIPT_DIR}/build_ray.sh"
fi

# Deploy a staging env
envsubst < k8s/deploy_staging.yaml | kubectl apply -f -
read -rp "Have you checked staging and are ready to deploy to prod? (y/n) " yn

case $yn in
    [Yy]* ) echo "Proceeding...";;
    [Nn]* ) echo "Exiting..."; exit;;
    * ) echo "Invalid response; treating as no. Exiting..."; exit 1;;
esac

# DB-host config (the pg8->pg9 cutover switch). This ConfigMap is the single,
# version-controlled source of truth for PDBHOST; the prod deploy.yaml and
# ray/cluster.yaml reference it via configMapKeyRef, so it MUST be applied BEFORE
# them or those pods fail with CreateContainerConfigError.
#
# TO SWAP IN THE NEW DB BACKEND: set PDBHOST in k8s/db-config.yaml to the target
# primary (fhi-pg-main-9-rw.totallylegitco.svc for the pg9 cutover) and run this
# script -- the ConfigMap is applied and the workloads below roll onto it.
# NOTE: this re-asserts the committed value. If scripts/cutover-app-to-pg9.sh
# already flipped the LIVE ConfigMap to -9, you MUST also commit that flip into
# k8s/db-config.yaml first, or this apply will revert the host back to -8.
kubectl apply -f k8s/db-config.yaml
echo "PDBHOST now set to: $(kubectl -n totallylegitco get configmap fhi-db-config -o jsonpath='{.data.PDBHOST}')"

# Backup schedule for fhi-pg-main-9 -- re-asserted on every deploy (same
# pattern as db-config above) so a deleted/suspended schedule self-heals and
# the 26h backup-age alert in k8s/fhi-pg-main-9-alerts.yaml always has a real
# schedule behind it. Idempotent. NOTE: the barman-cloud plugin INSTALL is
# deliberately NOT managed here -- colo-scripts owns it as a single pinned
# helm release; this script must never apply a second copy (July 2026
# incident: two plugin installs deadlocked reconciliation for 12 days).
kubectl apply -f k8s/fhi-pg-main-9-scheduledbackup.yaml

# --- deploy gate helpers ---------------------------------------------------
# Every gate call below carries an explicit --request-timeout. kubectl's
# default is 0, i.e. wait forever, so one stuck API request would hang the
# deploy straight through whatever budget the loops claim to enforce
# (external review).
KGET=(kubectl --request-timeout=30s -n totallylegitco)

# `if kubectl get deployment X >/dev/null 2>&1` is NOT a safe presence test.
# A command used as an `if` condition does not trip errexit, so a transient
# API error reads as "this Deployment does not exist" and silently skips its
# gate. Only a confirmed NotFound counts as absent; anything else aborts
# (external review).
deployment_present() {
    local dep="$1" out
    if out=$("${KGET[@]}" get deployment "$dep" -o name 2>&1); then
        return 0
    fi
    if printf '%s' "$out" | grep -qi "not found"; then
        return 1
    fi
    echo "kubectl get deployment $dep failed, refusing to guess: $out" >&2
    exit 1
}

# Pods under a selector that are marked for deletion but whose processes are
# still running.
terminating_pods() {
    "${KGET[@]}" get pods -l "$1" \
        -o go-template='{{range .items}}{{if .metadata.deletionTimestamp}}{{.metadata.name}}{{"\n"}}{{end}}{{end}}'
}

# `kubectl rollout status` is NOT a drain gate (external review). A pod stops
# counting as an active replica the moment it is marked for deletion, so
# rollout status returns while the old pods are still inside their grace
# period -- running preStop, finishing in-flight work, and still able to
# INSERT a ProposedAppeal. web allows 420s plus a 15s preStop and the appeal
# worker 360s, so that window is minutes wide, which is long enough for an
# old pod to write a NULL fingerprint just after the strict backfill observed
# a quiet table. Wait for those pods to actually be gone.
wait_for_drain() {
    local selector="$1" name="$2" deadline=$((SECONDS + 600)) left
    while [ "$SECONDS" -lt "$deadline" ]; do
        if ! left="$(terminating_pods "$selector")"; then
            echo "$name: could not list pods, refusing to guess that the drain finished" >&2
            exit 1
        fi
        if [ -z "$left" ]; then
            echo "$name: no pods still terminating"
            return 0
        fi
        sleep 10
    done
    echo "$name: pods still terminating after 10m ($(terminating_pods "$selector" | tr '\n' ' ')); not running the fingerprint backfill Job" >&2
    exit 1
}

# Same rule as deployment_present: a `[ -n "$(kubectl get ...)" ]` test turns
# any API error into "no pods here", which silently drops the Ray half of the
# writer gate. An empty LIST is absence; a failed call is not.
ray_pods_exist() {
    local out
    if ! out=$("${KGET[@]}" get pods -l ray.io/cluster=raycluster-kuberay \
                 --field-selector=status.phase!=Succeeded,status.phase!=Failed \
                 -o name 2>&1); then
        echo "kubectl get pods (ray) failed, refusing to guess: $out" >&2
        exit 1
    fi
    [ -n "$out" ]
}

ray_pod_uids() {
    "${KGET[@]}" get pods -l ray.io/cluster=raycluster-kuberay \
        -o jsonpath='{.items[*].metadata.uid}'
}

# Snapshot the Ray pods BEFORE the delete below. `kubectl delete` defaults to
# background cascading deletion, so the RayCluster object disappears before
# its pods do, and old and new generations share the ray.io/cluster label --
# an old Ready pod, still running SpeculativeAppealsActor and still able to
# write, would otherwise satisfy the readiness gate on its own (external
# review).
if ! RAY_PODS_BEFORE="$(ray_pod_uids)"; then
    echo "Could not list the current Ray pods; aborting before touching the cluster" >&2
    exit 1
fi

ray_old_pods_remaining() {
    local now uid
    # Called as a `while` condition, where errexit is suppressed -- so check
    # the call explicitly. `exit` here is a real exit: the function body runs
    # in the current shell, unlike a command substitution.
    if ! now="$(ray_pod_uids)"; then
        echo "kubectl get pods (ray uids) failed, refusing to guess" >&2
        exit 1
    fi
    now=" $now "
    for uid in $RAY_PODS_BEFORE; do
        case "$now" in *" $uid "*) return 0 ;; esac
    done
    return 1
}

# The raycluster operator doesn't handle upgrades well so delete + recreate instead.
# --ignore-not-found rather than `|| echo "No raycluster present"`: that
# swallowed every failure -- 403, API error, timeout -- and reported a missing
# cluster, after which the apply would merely update the CR still standing
# (external review).
kubectl --request-timeout=60s delete raycluster -n totallylegitco raycluster-kuberay --ignore-not-found
envsubst < k8s/ray/cluster.yaml | kubectl apply -f -

# Deploy a staging env
envsubst < k8s/deploy.yaml | kubectl apply -f -

# The Temporal fax worker (k8s/temporal/worker.yaml) runs the same app image as
# the web pods and has to roll with every prod deploy. It was applied by hand at
# Temporal go-live and nothing here re-applied it, so by 2026-08-26 it was two
# versions behind prod (v0.22.4a-dev vs v0.23.1a-dev). Same ${FHI_BASE}/${FHI_VERSION}
# substitution as the manifests above.
envsubst < k8s/temporal/worker.yaml | kubectl apply -f -
# The appeal worker Deployment must roll with every deploy too -- Temporal
# accepts workflow starts for a queue with NO pollers and queues them
# silently, so an applied-by-hand-once appeal worker (or a forgotten one)
# would look healthy while nothing executes (external review).
# This apply must stay ABOVE the rollout gate below, which waits on this very
# Deployment: waiting first would either time out on a Deployment that does
# not exist yet or, worse, pass against the previous image.
envsubst < k8s/temporal/appeal-worker.yaml | kubectl apply -f -
# Observability FIRST, before any gate that can exit (external review): a
# backfill or rollout problem below used to skip the PDBs, both PodMonitors,
# both PrometheusRules and the relay CronJob -- shipping the new image with no
# metrics and no alerts, which is the exact silent gap these manifests exist
# to close. They are cheap, idempotent applies with nothing to wait for, so
# they belong above the waits.
kubectl apply -f k8s/temporal/worker-pdb.yaml
if kubectl get crd podmonitors.monitoring.coreos.com >/dev/null 2>&1; then
    kubectl apply -f k8s/temporal/worker-podmonitor.yaml
else
    echo "WARNING: no PodMonitor CRD in this cluster -- Temporal worker metrics will not be scraped"
fi
if kubectl get crd prometheusrules.monitoring.coreos.com >/dev/null 2>&1; then
    kubectl apply -f k8s/temporal/worker-alerts.yaml
else
    echo "WARNING: no PrometheusRule CRD in this cluster -- Temporal worker alerts not installed"
fi
# Intake outbox relay: a CronJob (every minute, no overlap) that re-delivers
# intake-journey events whose Temporal ack never landed. Its own process,
# not a hook in the web/Ray pods, so a crash there cannot take the relay
# with it (external review). Inert while the intake flags are off.
envsubst < k8s/temporal/intake-outbox-cronjob.yaml | kubectl apply -f -
# Alerts for the relay itself: a stalled outbox is invisible in worker and
# web metrics (the events simply stop moving), so backlog age and CronJob
# success age are the only signals that catch it (external review).
if kubectl get crd prometheusrules.monitoring.coreos.com >/dev/null 2>&1; then
    kubectl apply -f k8s/temporal/intake-outbox-alerts.yaml
else
    echo "WARNING: no PrometheusRule CRD in this cluster -- intake outbox alerts not installed"
fi

# ROLLOUT GATE. Independent of the backfill: a Deployment that never finishes
# rolling is a broken deploy whether or not we are about to fingerprint
# anything. Skipped only by --skip-journey-gates.
if [ "$SKIP_JOURNEY_GATES" = true ]; then
    echo "--skip-journey-gates: not waiting for any rollout"
else
    for dep in web fhi-fax-worker fhi-appeal-worker; do
      if deployment_present "$dep"; then
        kubectl --request-timeout=60s -n totallylegitco rollout status deployment "$dep" --timeout=15m \
          || { echo "Rollout of $dep did not complete"; exit 1; }
      else
        echo "Deployment $dep not present; skipping rollout wait"
      fi
    done
fi

# Post-rollout second pass of the appeal fingerprint backfill (see the Job
# manifest): --strict keeps failing -- and the Job keeps retrying -- until no
# pre-fingerprint writer is left, so the "run again after old pods drain"
# step is enforced by the deploy, not by a README. Jobs are immutable:
# delete the previous run before applying.
#
# WRITER GATE: a strict pass that runs while ANY pod on pre-fingerprint code
# can still handle a request proves nothing -- two quiet scans succeed, then
# an idle old pod inserts a NULL fingerprint or edits text under a stale one.
# The ProposedAppeal writers are the web pods (save_appeal), the appeal
# worker (the generation workflow) and the Ray cluster (its
# SpeculativeAppealsActor). fhi-fax-worker polls the fax queue only and
# writes no ProposedAppeal, so it is rolled above but deliberately NOT
# drained here -- its 1860s grace period would add half an hour to every
# deploy for nothing.
if [ "$SKIP_BACKFILL" = true ]; then
    echo "--skip-backfill: not applying or waiting for the fingerprint backfill Job"
elif [ "$SKIP_JOURNEY_GATES" = true ]; then
    echo "--skip-journey-gates: applying the backfill Job WITHOUT gating on the writers"
    kubectl --request-timeout=60s delete job fhi-backfill-appeal-fingerprints -n totallylegitco --ignore-not-found
    envsubst < k8s/temporal/backfill-fingerprints-job.yaml | kubectl apply -f -
    echo "NOTE: check it yourself -- kubectl -n totallylegitco get job/fhi-backfill-appeal-fingerprints"
else
    # Old web / appeal-worker processes gone, not merely uncounted.
    deployment_present web \
      && wait_for_drain group=fight-health-insurance-prod-webbackend web
    deployment_present fhi-appeal-worker \
      && wait_for_drain group=fight-health-insurance-prod-temporal-appeal-worker fhi-appeal-worker

    # Ray: the pods that existed before the delete must be gone before their
    # replacements can vouch for anything.
    ray_deadline=$((SECONDS + 300))
    while ray_old_pods_remaining; do
      if [ "$SECONDS" -ge "$ray_deadline" ]; then
        echo "Ray pods from before the cluster delete are still running after 5m; not running the fingerprint backfill Job" >&2
        exit 1
      fi
      sleep 10
    done

    # Then wait for the new cluster. Existence guard first (external review):
    # the cluster was just deleted and re-applied, so waiting on a selector
    # the operator has not populated yet fails instantly with "no matching
    # resources found" and would kill an otherwise healthy deploy.
    ray_pods_present=false
    ray_deadline=$((SECONDS + 300))
    while [ "$SECONDS" -lt "$ray_deadline" ]; do
      if ray_pods_exist; then
        ray_pods_present=true
        break
      fi
      sleep 10
    done
    if [ "$ray_pods_present" = true ]; then
      # `kubectl wait` resolves its selector ONCE per invocation, so a single
      # long wait started when only the head pod existed would never look at
      # the worker pods created after it (external review). Call it
      # repeatedly with a short timeout -- each call re-lists -- and require
      # two consecutive clean passes so a lone head cannot satisfy the gate
      # in the window before its workers are scheduled.
      ray_ready_deadline=$((SECONDS + 900))
      ray_stable=0
      while [ "$SECONDS" -lt "$ray_ready_deadline" ]; do
        if kubectl --request-timeout=60s -n totallylegitco wait --for=condition=Ready pod \
             -l ray.io/cluster=raycluster-kuberay \
             --field-selector=status.phase!=Succeeded,status.phase!=Failed \
             --timeout=30s >/dev/null 2>&1; then
          ray_stable=$((ray_stable + 1))
          [ "$ray_stable" -ge 2 ] && break
        else
          ray_stable=0
        fi
        sleep 10
      done
      if [ "$ray_stable" -lt 2 ]; then
        echo "Ray cluster pods not consistently Ready within 15m; not running the fingerprint backfill Job" >&2
        exit 1
      fi
    else
      echo "No Ray cluster pods after 5m; skipping the Ray readiness wait (no Ray writers to drain)"
    fi

    kubectl --request-timeout=60s delete job fhi-backfill-appeal-fingerprints -n totallylegitco --ignore-not-found
    envsubst < k8s/temporal/backfill-fingerprints-job.yaml | kubectl apply -f -
    # Wait for the strict backfill, and FAIL FAST on a failed Job (external
    # review): `kubectl wait --for=condition=complete` ignores condition=Failed,
    # so a Job that gives up would still cost the full 30 minutes before the
    # deploy heard about it. Poll both conditions instead, against a wall-clock
    # deadline rather than a loop count (a loop count is not a time bound once
    # each API call takes a moment of its own).
    # Read once per tick and split the two conditions out of the same object,
    # so a Job that flips Complete between two separate reads cannot be missed.
    # This runs inside $( ), where `exit` would only kill the subshell -- hence
    # `return 1` here and an explicit check at the call site.
    job_conditions() {
        "${KGET[@]}" get job fhi-backfill-appeal-fingerprints \
            -o go-template='{{range .status.conditions}}{{.type}}={{.status}}{{"\n"}}{{end}}' \
            || return 1
    }
    backfill_done=false
    backfill_deadline=$((SECONDS + 1800))
    while :; do
      if ! conds="$(job_conditions)"; then
        echo "Could not read the backfill Job status; refusing to treat that as 'still running'" >&2
        exit 1
      fi
      case "$conds" in *"Complete=True"*)
        backfill_done=true
        break ;;
      esac
      case "$conds" in *"Failed=True"*)
        echo "Fingerprint backfill Job FAILED; inspect it: kubectl -n totallylegitco logs job/fhi-backfill-appeal-fingerprints" >&2
        exit 1 ;;
      esac
      # Deadline checked AFTER a status read, so a Job that completes in the
      # last few seconds is not reported as a timeout.
      [ "$SECONDS" -lt "$backfill_deadline" ] || break
      sleep 10
    done
    if [ "$backfill_done" != true ]; then
      echo "Fingerprint backfill Job did not complete within 30m; inspect it: kubectl -n totallylegitco logs job/fhi-backfill-appeal-fingerprints" >&2
      exit 1
    fi
fi

# In-cluster scraping of the app's /metrics (which is no longer reachable from
# the internet -- see docs/metrics-endpoint-access.md). The apply is skipped
# only where the Prometheus operator's CRD is absent; every other failure (bad
# manifest, RBAC, API error) is fatal, because a deploy that "succeeded" with no
# metrics targets is exactly the silent gap this endpoint lockdown could create.
if kubectl get crd podmonitors.monitoring.coreos.com >/dev/null 2>&1; then
    kubectl apply -f k8s/fhi-web-podmonitor.yaml
else
    echo "WARNING: no PodMonitor CRD in this cluster -- app metrics will not be scraped"
fi
