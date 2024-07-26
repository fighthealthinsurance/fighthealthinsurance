#!/usr/bin/env bash
set -ex
# start-server.sh
cd /opt/app
# Run migrations
if [ ! -z "$PRIMARY" ]; then
  ./manage.py migrate
  ./manage.py loaddata initial
  ./manage.py ensure_admin --no-input
  exit 0
fi
# Start gunicorn
export DJANGO_CONFIGURATION=${DJANGO_CONFIGURATION:-"Prod"}
export DJANGO_SETTINGS_MODULE=${DJANGO_SETTINGS_MODULE:-"fighthealthinsurance.settings"}
# Needed for https://github.com/MacHu-GWU/uszipcode-project/blob/21242cad7cbb7eaf73235086b89499bd90c36531/uszipcode/db.py#L23
export HOME=$(pwd)
gunicorn fighthealthinsurance.wsgi --user www-data --bind 0.0.0.0:8010 --workers 4 --access-logformat  "{\"actual_ip\": \"%({x-forwarded-for}i)s\", \"remote_ip\":\"%(h)s\",\"request_id\":\"%({X-Request-Id}i)s\",\"response_code\":\"%(s)s\",\"request_method\":\"%(m)s\",\"request_path\":\"%(U)s\",\"request_querystring\":\"%(q)s\",\"request_timetaken\":\"%(D)s\",\"response_length\":\"%(B)s\"}" 2>&1 | grep -v kube-probe | grep -v kube-proxy &
# And nginx to proxy & serve static files
nginx -g "daemon off;" 2>&1 |grep -v kube-proxy |grep -v kube-probe
