#!/usr/bin/env bash
set -ex
# start-server.sh
cd /opt/app
# Run migrations
if [ ! -z "$PRIMARY" ]; then
  ./manage.py migrate
  ./manage.py loaddata initial
fi
# Start gunicorn
export DJANGO_CONFIGURATION=${DJANGO_CONFIGURATION:-"Prod"}
export DJANGO_SETTINGS_MODULE=${DJANGO_SETTINGS_MODULE:-"fighthealthinsurance.settings"}
# Needed for https://github.com/MacHu-GWU/uszipcode-project/blob/21242cad7cbb7eaf73235086b89499bd90c36531/uszipcode/db.py#L23
export HOME=$(pwd)
gunicorn fighthealthinsurance.wsgi --user www-data --bind 0.0.0.0:8010 --workers 4 2>&1 | grep -v kube-probe &
# And nginx to proxy & serve static files
nginx -g "daemon off;" 2>&1 |grep -v kube-proxy
