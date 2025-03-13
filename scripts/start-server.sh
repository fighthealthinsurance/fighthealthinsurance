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
  python manage.py ensure_adminuser --no-input
  sleep 10
  exit 0
elif [ -n "$POLLING_ACTORS" ]; then
  # Some for polling actors
  python manage.py launch_polling_actors || (echo "Error starting ray actor?" && sleep 480)
  sleep 10
  exit 0
fi
# Same for dev _except_ we don't exit when were done since we use the locally created sqllite db to party on.
if [ "$ENVIRONMENT" == "Dev" ]; then
  python manage.py migrate || (sleep 600; exit 1)
  python manage.py loaddata initial
  python manage.py loaddata followup
  python manage.py loaddata plan_source
  python manage.py ensure_adminuser --no-input
fi
# Start gunicorn
# Needed for https://github.com/MacHu-GWU/uszipcode-project/blob/21242cad7cbb7eaf73235086b89499bd90c36531/uszipcode/db.py#L23
HOME=$(pwd)
export HOME
PYTHONUNBUFFERED=1
export PYTHONUNBUFFERED
sudo -E -u www-data uvicorn fighthealthinsurance.asgi:application --host 0.0.0.0 --port 8010 --workers 2 --proxy-headers --access-log  --log-config conf/uvlog_config.yaml 2>&1 | grep -v kube-probe  | grep -v kube-proxy &
sleep 2
nginx -g "daemon off;" 2>&1 |grep -v kube-proxy |grep -v kube-probe
