#!/bin/bash
# shellcheck disable=SC2068


SCRIPT_DIR="$(dirname "$0")"

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

check_python_environment

set -ex

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

RECAPTCHA_TESTING=true OAUTHLIB_RELAX_TOKEN_SCOPE=1 \
	python manage.py runserver_plus --cert-file cert.pem --key-file key.pem $@
