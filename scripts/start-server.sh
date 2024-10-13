#!/usr/bin/env bash
set -ex
# start-server.sh
cd /opt/fighthealthinsurance
# Run migrations
if [ ! -z "$PRIMARY" ]; then
  ./manage.py migrate
  ./manage.py loaddata initial
  ./manage.py loaddata followup
  ./manage.py loaddata plan_source
  ./manage.py ensure_adminuser --no-input
  sleep 20
  exit 0
fi
# Start gunicorn
export DJANGO_CONFIGURATION=${DJANGO_CONFIGURATION:-"Prod"}
export DJANGO_SETTINGS_MODULE=${DJANGO_SETTINGS_MODULE:-"fighthealthinsurance.settings"}
# Needed for https://github.com/MacHu-GWU/uszipcode-project/blob/21242cad7cbb7eaf73235086b89499bd90c36531/uszipcode/db.py#L23
export HOME=$(pwd)
gunicorn fighthealthinsurance.wsgi --user www-data --bind 0.0.0.0:8010 --workers 2 --access-logformat  "{\"actual_ip\": \"%({X-Forwarded-For}i)s\", \"remote_ip\":\"%(h)s\",\"request_id\":\"%({X-Request-Id}i)s\",\"response_code\":\"%(s)s\",\"request_method\":\"%(m)s\",\"request_path\":\"%(U)s\",\"request_querystring\":\"%(q)s\",\"request_timetaken\":\"%(D)s\",\"response_length\":\"%(B)s\", \"agent\": \"\%(a)s\"}" --error-logfile /tmp/gerror.log --access-logfile /tmp/gaccess.log --capture-output --log-level debug 2>&1 | grep -v kube-probe  | grep -v kube-proxy &
sleep 2
# And nginx to proxy & serve static files
tail -f /tmp/*.log |grep -v kube-probe &
nginx -g "daemon off;" 2>&1 |grep -v kube-proxy |grep -v kube-probe
