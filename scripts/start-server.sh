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
sudo -E -u www-data uvicorn fighthealthinsurance.asgi:application --host 0.0.0.0 --port 8010 --workers "${UVICORN_WORKERS:-2}" --proxy-headers --access-log --use-colors --log-config conf/uvlog_config.yaml 2>&1 | grep -v kube-probe  | grep -v kube-proxy &
sleep 2
nginx -g "daemon off;" 2>&1 |grep -v kube-proxy |grep -v kube-probe
