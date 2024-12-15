#!/usr/bin/env bash
set -ex
# start-server.sh
cd /opt/fighthealthinsurance
export DJANGO_CONFIGURATION=${DJANGO_CONFIGURATION:-"Prod"}
export DJANGO_SETTINGS_MODULE=${DJANGO_SETTINGS_MODULE:-"fighthealthinsurance.settings"}
# Run migrations on primary only
if [ -n "$PRIMARY" ]; then
  python manage.py migrate
  python manage.py loaddata initial
  python manage.py loaddata followup
  python manage.py loaddata plan_source
  python manage.py ensure_adminuser --no-input
  python manage.py launch_polling_actors || (echo "Error starting ray actor?" && sleep 480)
  sleep 60
  exit 0
fi
# Start gunicorn
# Needed for https://github.com/MacHu-GWU/uszipcode-project/blob/21242cad7cbb7eaf73235086b89499bd90c36531/uszipcode/db.py#L23
HOME=$(pwd)
export HOME
gunicorn fighthealthinsurance.wsgi --user www-data --bind 0.0.0.0:8010 --workers 2 --access-logformat  "{\"actual_ip\": \"%({X-Forwarded-For}i)s\", \"remote_ip\":\"%(h)s\",\"request_id\":\"%({X-Request-Id}i)s\",\"response_code\":\"%(s)s\",\"request_method\":\"%(m)s\",\"request_path\":\"%(U)s\",\"request_querystring\":\"%(q)s\",\"request_timetaken\":\"%(D)s\",\"response_length\":\"%(B)s\", \"agent\": \"\%(a)s\"}" --error-logfile /tmp/gerror.log --access-logfile /tmp/gaccess.log --capture-output --log-level debug 2>&1 | grep -v kube-probe  | grep -v kube-proxy &
sleep 2
# And nginx to proxy & serve static files
tail -f /tmp/*.log |grep -v kube-probe &
nginx -g "daemon off;" 2>&1 |grep -v kube-proxy |grep -v kube-probe
