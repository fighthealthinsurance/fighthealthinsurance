#!/bin/bash
# shellcheck disable=SC2068


SCRIPT_DIR="$(dirname "$0")"

if [ ! -f "cert.pem" ]; then
  "${SCRIPT_DIR}/install.sh"
fi

# Activate the venv if present.
if [ -f ./build_venv/bin/activate ]; then
  source ./build_venv/bin/activate
  echo "Using build_venv"
elif [ -f ./.venv/bin/activate ]; then
  source ./.venv/bin/activate
  echo "Using .venv"
fi

check_python_environment() {
	python -c 'import configurations'  >/dev/null 2>&1
	python_dep_check=$?
	if [ ${python_dep_check} != 0 ]; then
		set +x
		printf 'Python dependencies may be missing. Please install dependencies via:\n' >/dev/stderr
		printf 'pip install -r requirements.txt\n' >/dev/stderr
		exit 1
	fi
	python min_version.py
}

set -ex

check_python_environment

"${SCRIPT_DIR}/build_static.sh"

# Are we sort of connected to the backend?
if kubectl get service -n totallylegitco vllm-health-svc; then
   export HEALTH_BACKEND_PORT=4280
   export HEALTH_BACKEND_HOST=localhost
   kubectl port-forward -n totallylegitco service/vllm-health-svc 4280:80 &
else
   echo 'No connection to kube vllm health svc'
fi

python manage.py migrate
python manage.py loaddata initial
python manage.py loaddata followup
python manage.py loaddata plan_source

# Make sure we have an admin user so folks can test the admin view
FIGHT_HEALTH_ADMIN_USER="admin" FIGHT_HEALTH_ADMIN_PASSWORD="admin" python manage.py ensure_adminuser --no-input

python manage.py make_user  --username test_user --domain farts --password farts2

RECAPTCHA_TESTING=true OAUTHLIB_RELAX_TOKEN_SCOPE=1 uvicorn fighthealthinsurance.asgi:application --reload --reload-dir fighthealthinsurance --reload-include="*.py" --reload-include="*.html" --reload-include="*.css" --reload-exclude "*.pyc,__pycache__/*,*.pyo,*~,#*#,.#*,node_modules,static" --access-log --log-config conf/uvlog_config.yaml --port 8000 --ssl-keyfile key.pem --ssl-certfile cert.pem $@
