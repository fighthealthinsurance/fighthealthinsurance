#!/usr/bin/env bash
set -ex
# start-server.sh
cd /opt/fighthealthinsurance
export DJANGO_CONFIGURATION=${DJANGO_CONFIGURATION:-"Prod"}
export DJANGO_SETTINGS_MODULE=${DJANGO_SETTINGS_MODULE:-"fighthealthinsurance.settings"}
# Run migrations on the migrations container only
if [ -n "$MIGRATIONS" ]; then
  # Here we sleep for 10 minutes IF we fail so that we can debug -- I know it's a hack
  # but it happens pretty rarely and it's a pain to debug otherwise.
  python manage.py migrate || (sleep 600; exit 1)
  python manage.py loaddata initial
  python manage.py loaddata followup
  python manage.py loaddata plan_source
  python manage.py loaddata insurance_companies
  python manage.py loaddata pa_requirements
  python manage.py ensure_adminuser --no-input
  sleep 10
  exit 0
elif [ -n "$POLLING_ACTORS" ]; then
  # Launch-time chooser backfill (docs/chooser.md): once every current appeal
  # case has been judged, a one-shot Ray actor writes a hundred new ones, one
  # at a time. Non-blocking: the actor carries on after this job exits.
  # Before the polling actors, on purpose: the backfill is due exactly when
  # no case is unscored, which is also when the refill actor's first tick
  # wants a batch, and whichever takes the appeal generation claim first
  # writes. Started second, the refill always won and the backfill never ran.
  # The claim the backfill usually finds held is the old cluster's refill
  # actor's, killed mid-batch by this deploy; the backfill waits that out and
  # counts whatever is written meanwhile toward its hundred.
  python manage.py launch_chooser_backfill || echo "Chooser backfill launch failed (non-blocking)"
  # Some for polling actors
  python manage.py launch_polling_actors || (echo "Error starting ray actor?" && sleep 480)
  # Deployment-time model-backend health check (supersedes probe_models).
  # Runs once per deployment: the command claims a database leader slot keyed
  # on the deployment id, so even if this job retries (or another pod races
  # it) the check and its consolidated alert email happen at most once. It
  # tests every enabled backend with a tiny prompt, logs a
  # MODEL_BACKEND_HEALTH_SUMMARY block, and emails support on failures.
  # Non-blocking by default: a failing model backend must not fail the
  # deploy. Set FHI_MODEL_HEALTH_STRICT=1 to fail this job instead.
  if ! python manage.py check_model_backends --deploy-hook; then
    if [ "${FHI_MODEL_HEALTH_STRICT:-0}" = "1" ]; then
      echo "Model backend health check failed and FHI_MODEL_HEALTH_STRICT=1; failing deploy job"
      exit 1
    fi
    echo "Model backend health check failed (non-blocking)"
  fi
  sleep 10
  exit 0
elif [ -n "$PREFETCH_EXTRALINKS" ]; then
  # Launch extralink + payer-policy pre-fetch actors (one-time job, non-blocking).
  # Both run as Ray actors so neither blocks the deploy on slow upstream payers.
  python manage.py launch_prefetch_actor || echo "Pre-fetch failed (non-blocking)"
  sleep 10
  exit 0
elif [ -n "$IMPORT_PLAN_DOCUMENTS" ]; then
  # Deploy Job web-plan-documents-import (k8s/deploy.yaml): load the public
  # plan-document corpus this image ships, plan_documents/plan-docs.zip
  # (fetched at build time from the release pinned in plan_documents.lock;
  # docs/plan-document-corpus.md). No pinned corpus, no file: nothing to do.
  PLAN_DOCUMENTS_ZIP=plan_documents/plan-docs.zip
  if [ ! -f "$PLAN_DOCUMENTS_ZIP" ]; then
    echo "No plan-document corpus in this image ($PLAN_DOCUMENTS_ZIP); nothing to import"
    exit 0
  fi
  # web-migrations is applied in the same kubectl apply, so wait for it
  # (bounded, about 20 minutes) instead of importing into an old schema.
  # Records resolve their insurer against plan_source and
  # insurance_companies, so load them here too (idempotent) rather than
  # import before web-migrations has. That loaddata can run at the same time
  # as web-migrations' own (migrate --check passes as soon as migrate is
  # done), and when a fixture adds rows one of the two can fail on a
  # duplicate key. Here that failure is retried inside this loop, not a Job
  # failure; web-migrations' Job retries its own.
  plan_documents_waits=0
  until python manage.py migrate --check &&
    python manage.py loaddata plan_source insurance_companies; do
    plan_documents_waits=$((plan_documents_waits + 1))
    if [ "$plan_documents_waits" -ge 40 ]; then
      echo "Migrations or insurer fixtures still pending after $plan_documents_waits checks; not importing plan documents"
      exit 1
    fi
    sleep 30
  done
  # --deactivate-missing: the corpus owns the carriers it names, so their
  # documents it no longer lists are retired. A refused record is skipped and
  # counted in the log line; the Job fails only if the bundle itself cannot
  # be read. Unchanged files are not parsed again.
  exec python manage.py import_plan_documents --source "$PLAN_DOCUMENTS_ZIP" --store-file --deactivate-missing
elif [ -n "$BACKFILL_APPEAL_FINGERPRINTS" ]; then
  # Post-rollout Job (k8s/temporal/backfill-fingerprints-job.yaml): second
  # pass of the ProposedAppeal fingerprint backfill. --strict exits non-zero
  # while pre-fingerprint writers are still active, so the Job's backoff
  # retries until the database is quiescent.
  exec python manage.py backfill_appeal_fingerprints --strict
elif [ -n "$RECONCILE_POLLING_ACTORS" ]; then
  # Actor reconciler (k8s/actor-reconcile-cronjob.yaml). The polling actors
  # are detached and created ONCE, by the POLLING_ACTORS job above, so losing
  # the Ray head loses all of them with nothing to put them back until the
  # next deploy -- production ran 0 of 5 after a node reboot. This relaunches
  # only what is missing; a healthy cluster costs one health check.
  exec python manage.py reconcile_polling_actors
elif [ -n "$DELIVER_INTAKE_EVENTS" ]; then
  # Intake outbox relay (k8s/temporal/intake-outbox-cronjob.yaml, every
  # minute): re-deliver intake-journey events whose Temporal ack never
  # landed. Independent of the web/Ray processes so a crash there cannot
  # take the relay with it. Inert while the intake flags are off.
  exec python manage.py deliver_intake_events
elif [ -n "$SWEEP_ASSISTANT_HANDOFFS" ]; then
  # Assistant handoff sweep (k8s/assistant-handoff-sweep-cronjob.yaml, every
  # 10 minutes): delete prepare_appeal links past their 2 hours. Runs
  # whatever the MCP flags say, so turning the feature off still empties
  # the table.
  exec python manage.py sweep_assistant_handoffs
elif [ -n "$SWEEP_ASSISTANT_DRAFTS" ]; then
  # Assistant drafts sweep (k8s/assistant-drafts-sweep-cronjob.yaml, hourly):
  # delete drafts past their expiry. Runs whatever the MCP flags say.
  exec python manage.py sweep_assistant_drafts
elif [ -n "$TEMPORAL_WORKER" ]; then
  # Long-running Temporal worker hosting SendFaxWorkflow + fax activities.
  # Unlike the Ray launchers above this stays in the foreground; exec so signals
  # propagate for clean Kubernetes shutdown.
  exec python manage.py run_temporal_worker
fi
# Same for dev _except_ we don't exit when were done since we use the locally created sqllite db to party on.
if [ "$ENVIRONMENT" == "Dev" ]; then
  python manage.py migrate || (sleep 600; exit 1)
  python manage.py loaddata initial
  python manage.py loaddata followup
  python manage.py loaddata plan_source
  python manage.py loaddata insurance_companies
  python manage.py loaddata pa_requirements
  python manage.py ensure_adminuser --no-input
fi
# Start gunicorn
# Needed for https://github.com/MacHu-GWU/uszipcode-project/blob/21242cad7cbb7eaf73235086b89499bd90c36531/uszipcode/db.py#L23
HOME=$(pwd)
export HOME
PYTHONUNBUFFERED=1
export PYTHONUNBUFFERED
# UVICORN_WORKERS defaults to 2 for standalone use; prod sets 1 so each pod is
# a single worker whose health the readiness/liveness probes can actually see
# (with 2+ workers a wedged worker hides behind its healthy sibling: probes
# mostly pass while a share of real traffic hangs).
# Graceful shutdown: this bash is container PID 1, and bash neither forwards
# SIGTERM to the backgrounded uvicorn pipeline nor to a foreground child --
# without a trap the pod just sits until the kubelet SIGKILLs everything at
# the end of terminationGracePeriodSeconds, hard-killing mid-flight appeal
# generations AND making every rollout wait out the full grace period. On
# SIGTERM: tell nginx to finish in-flight requests and stop (quit), signal
# uvicorn to drain its connections, then wait for both; the kubelet's
# SIGKILL at the grace-period deadline remains the backstop.
_drain() {
  trap - TERM INT
  nginx -s quit 2>/dev/null || true
  # Matches the sudo wrapper too; sudo relays the signal to uvicorn.
  pkill -TERM -f "uvicorn fighthealthinsurance.asgi" 2>/dev/null || true
  wait || true
  exit 0
}
trap _drain TERM INT

# grep block-buffers into a pipe; without --line-buffered, log lines arrive late in 4 KB batches or not at all.
sudo -E -u www-data uvicorn fighthealthinsurance.asgi:application --host 0.0.0.0 --port 8010 --workers "${UVICORN_WORKERS:-2}" --proxy-headers --access-log --use-colors --log-config conf/uvlog_config.yaml 2>&1 | grep --line-buffered -v kube-probe | grep --line-buffered -v kube-proxy &
sleep 2
nginx -g "daemon off;" 2>&1 | grep --line-buffered -v kube-proxy | grep --line-buffered -v kube-probe &
wait
