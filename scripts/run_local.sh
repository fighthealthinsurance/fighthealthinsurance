#!/bin/bash
# shellcheck disable=SC2068


SCRIPT_DIR="$(dirname "$0")"

if [ ! -f "cert.pem" ]; then
  "${SCRIPT_DIR}/install.sh"
fi

# Activate the venv if present.
if [[ -z $VIRTUAL_ENV ]]; then
  if [ -f ./build_venv/bin/activate ]; then
    source ./build_venv/bin/activate
    echo "Using build_venv"
  elif [ -f ./.venv/bin/activate ]; then
    source ./.venv/bin/activate
    echo "Using .venv"
  fi
fi

# Check if requirements files have changed since last run
REQ_CHECKSUM_FILE=".requirements_checksum"
CURRENT_REQ_CHECKSUM=""
STORED_REQ_CHECKSUM=""

if [ -f "requirements.txt" ] && [ -f "requirements-dev.txt" ]; then
  CURRENT_REQ_CHECKSUM=$(md5sum requirements.txt requirements-dev.txt | sort | md5sum | cut -d ' ' -f 1)
  if [ -f "$REQ_CHECKSUM_FILE" ]; then
    STORED_REQ_CHECKSUM=$(cat "$REQ_CHECKSUM_FILE")
  fi

  if [ "$CURRENT_REQ_CHECKSUM" != "$STORED_REQ_CHECKSUM" ]; then
    echo "Requirements files have changed. Updating dependencies..."
    pip install -r requirements.txt
    pip install -r requirements-dev.txt
    echo "$CURRENT_REQ_CHECKSUM" > "$REQ_CHECKSUM_FILE"
  fi
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

if kubectl get service -n totallylegitco vllm-health-svc-slipstream; then
  export NEW_HEALTH_BACKEND_PORT=4281
  export NEW_HEALTH_BACKEND_HOST=localhost
  kubectl port-forward -n totallylegitco service/vllm-health-svc-slipstream 4281:80 &
else
  echo 'No connection to _new_ kube vllm health svc'
fi

if ping -c1 -W1 10.69.200.180 >/dev/null 2>&1; then
  echo "backup reachable"
  export HEALTH_BACKUP_BACKEND_PORT=8000
  export HEALTH_BACKUP_BACKEND_HOST=10.69.200.180
  export HEALTH_BACKUP_BACKEND_MODEL="/TotallyLegitCo/fighthealthinsurance_model_v0.5"
else
  echo "backup not reachable."
fi

if ping -c1 -W1 scrump.local.pigscanfly.ca >/dev/null 2>&1; then
  echo "alpha reachable"
  export ALPHA_HEALTH_BACKEND_HOST=scrump.local.pigscanfly.ca
fi

if [ "$FAST" != "FAST" ]; then
  python manage.py migrate
  python manage.py loaddata initial
  python manage.py loaddata followup
  python manage.py loaddata plan_source

  # Make sure we have an admin user so folks can test the admin view
  python manage.py ensure_adminuser --username admin --password admin

  # Make a test user with UserDomain and everything
  python manage.py make_user  --username "test@test.com" --domain testfarts1 --password farts12345678 --email "test@test.com" --visible-phone-number 42 --is-provider true --first-name TestFarts
  python manage.py make_user  --username "test-patient@test.com" --domain testfarts1 --password farts12345678 --email "test-patient@test.com" --visible-phone-number 42 --is-provider false
fi

# If --devserver is passed, use Django's dev server for hot-reloading templates
if [[ "$1" == "--devserver" ]]; then
  shift
  RECAPTCHA_TESTING=true OAUTHLIB_RELAX_TOKEN_SCOPE=1 python manage.py runserver_plus --cert-file cert.pem --key-file key.pem "$@"
  exit 0
fi

# Default: use Uvicorn
RECAPTCHA_TESTING=true OAUTHLIB_RELAX_TOKEN_SCOPE=1 uvicorn fighthealthinsurance.asgi:application --reload --reload-dir fighthealthinsurance --reload-exclude "*.pyc,__pycache__/*,*.pyo,*~,#*#,.#*,node_modules,static,.tox/*,*sqlite*,./tox/**,./tests/**" --access-log --log-config conf/uvlog_config.yaml --port 8000 --ssl-keyfile key.pem --ssl-certfile cert.pem "$@"
