#!/bin/bash
set -ex

SCRIPT_DIR="$(dirname "$0")"

BUILDX_CMD=${BUILDX_CMD:-push}

# --no-build: assume the container images are already built and pushed, and only
# run the kubectl deploy steps. This skips every build step -- template/static
# setup (mypy, migrations, collectstatic, npm build) and the image builds -- so
# the script can run on a deploy-only host or CI job that has just kubectl and
# envsubst (no Python/Node build toolchain).
#
# --skip-journey-gates: apply everything, but do not BLOCK the deploy on the
# writer rollouts or the fingerprint backfill Job. The Job is still applied and
# still runs (with its own backoff); you just stop waiting for it here. Use it
# when you want the deploy to land promptly and intend to check the Job
# yourself -- and note the invariant it guards: until that strict pass is
# quiet, a `chosen=False, text_fingerprint IS NULL` row does not yet reliably
# mean "known duplicate", so journey counting should not be trusted (which
# matters only once the journey flags are on).
#
# --skip-backfill: do not apply or wait for the backfill Job at all. For
# clusters where it has already run clean, or a deploy that must not touch it.
NO_BUILD=false
SKIP_JOURNEY_GATES=false
SKIP_BACKFILL=false
usage() {
    echo "Usage: $0 [--no-build] [--skip-journey-gates] [--skip-backfill]"
    echo "  --no-build             Skip all build steps (template/static setup and image"
    echo "                         builds); assume images are already built and pushed."
    echo "  --skip-journey-gates   Apply everything but do not block on the writer"
    echo "                         rollouts or the fingerprint backfill Job (~90m of"
    echo "                         waits). The Job still runs; you check it yourself."
    echo "  --skip-backfill        Do not apply or wait for the fingerprint backfill Job."
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
    * ) echo "Invalid response. Please enter y or n.";;
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
    * ) echo "Invalid response. Please enter y or n.";;
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

# The raycluster operator doesn't handle upgrades well so delete + recreate instead.
kubectl delete raycluster -n totallylegitco raycluster-kuberay || echo "No raycluster present"
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

# Post-rollout second pass of the appeal fingerprint backfill (see the Job
# manifest): --strict keeps failing -- and the Job keeps retrying -- until no
# pre-fingerprint writer is left, so the "run again after old pods drain"
# step is enforced by the deploy, not by a README. Jobs are immutable:
# delete the previous run before applying.
#
# ROLLOUT GATE (external review): a strict pass that runs while ANY pod on
# pre-fingerprint code can still handle a request proves nothing -- two quiet
# scans succeed, then an idle old pod inserts a NULL fingerprint or edits text
# under a stale one. So wait for every ProposedAppeal writer to finish rolling
# (rollout status returns only after new replicas are available AND the old
# ReplicaSet's pods are gone): the web Deployment, both Temporal workers, and
# the Ray cluster (its SpeculativeAppealsActor writes speculative drafts).
# A rollout that does not finish fails the deploy rather than letting the Job
# run against a mixed fleet. --skip-journey-gates skips the waiting only.
if [ "$SKIP_BACKFILL" = true ]; then
    echo "--skip-backfill: not applying or waiting for the fingerprint backfill Job"
elif [ "$SKIP_JOURNEY_GATES" = true ]; then
    echo "--skip-journey-gates: applying the backfill Job WITHOUT waiting for writer rollouts"
    kubectl delete job fhi-backfill-appeal-fingerprints -n totallylegitco --ignore-not-found
    envsubst < k8s/temporal/backfill-fingerprints-job.yaml | kubectl apply -f -
    echo "NOTE: check it yourself -- kubectl -n totallylegitco get job/fhi-backfill-appeal-fingerprints"
else
    for dep in web fhi-fax-worker fhi-appeal-worker; do
      if kubectl -n totallylegitco get deployment "$dep" >/dev/null 2>&1; then
        kubectl -n totallylegitco rollout status deployment "$dep" --timeout=15m \
          || { echo "Rollout of $dep did not complete; not running the fingerprint backfill Job"; exit 1; }
      else
        echo "Deployment $dep not present; skipping rollout wait"
      fi
    done
    # Ray gets the same existence guard the Deployments have (external review):
    # the cluster was deleted and re-applied above, so `kubectl wait` on a
    # selector the operator has not populated yet errors out immediately with
    # "no matching resources found" and would fail an otherwise fine deploy.
    # Give the operator a chance to create pods, then wait on readiness.
    ray_pods_present=false
    for _ in $(seq 1 30); do
      if [ -n "$(kubectl -n totallylegitco get pods -l ray.io/cluster=raycluster-kuberay \
                   --field-selector=status.phase!=Succeeded,status.phase!=Failed \
                   -o name 2>/dev/null)" ]; then
        ray_pods_present=true
        break
      fi
      sleep 10
    done
    if [ "$ray_pods_present" = true ]; then
      # Same field selector as the presence check: a Succeeded/Failed pod left
      # over from a previous cluster never goes Ready and would hang the wait.
      kubectl -n totallylegitco wait --for=condition=Ready pod -l ray.io/cluster=raycluster-kuberay \
        --field-selector=status.phase!=Succeeded,status.phase!=Failed --timeout=15m \
        || { echo "Ray cluster pods not Ready; not running the fingerprint backfill Job"; exit 1; }
    else
      echo "No Ray cluster pods after 5m; skipping the Ray readiness wait (no Ray writers to drain)"
    fi
    kubectl delete job fhi-backfill-appeal-fingerprints -n totallylegitco --ignore-not-found
    envsubst < k8s/temporal/backfill-fingerprints-job.yaml | kubectl apply -f -
    # Wait for the strict backfill, and FAIL FAST on a failed Job (external
    # review): `kubectl wait --for=condition=complete` ignores condition=Failed,
    # so a Job that gives up would still cost the full 30 minutes before the
    # deploy heard about it. Poll both conditions instead.
    backfill_done=false
    for _ in $(seq 1 180); do
      if [ "$(kubectl -n totallylegitco get job fhi-backfill-appeal-fingerprints \
                -o jsonpath='{.status.conditions[?(@.type=="Complete")].status}' 2>/dev/null)" = "True" ]; then
        backfill_done=true
        break
      fi
      if [ "$(kubectl -n totallylegitco get job fhi-backfill-appeal-fingerprints \
                -o jsonpath='{.status.conditions[?(@.type=="Failed")].status}' 2>/dev/null)" = "True" ]; then
        echo "Fingerprint backfill Job FAILED; inspect it: kubectl -n totallylegitco logs job/fhi-backfill-appeal-fingerprints"
        exit 1
      fi
      sleep 10
    done
    if [ "$backfill_done" != true ]; then
      echo "Fingerprint backfill Job did not complete within 30m; inspect it: kubectl -n totallylegitco logs job/fhi-backfill-appeal-fingerprints"
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
