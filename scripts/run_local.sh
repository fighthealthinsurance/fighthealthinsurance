#!/bin/bash
# Run local development server
#
# Optimizations:
# - Checksums requirements.txt and requirements-dev.txt to skip pip install when unchanged
# - Checksums JS source files to skip webpack build when unchanged (via build_static.sh)
# - Combines Python version and dependency checks into a single invocation
# - Parallelizes network connectivity checks (kubectl and ping commands)
# - All optimizations maintain full compatibility and don't skip necessary operations
#
# shellcheck disable=SC2068,SC1090


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
	# Combine both checks in a single Python invocation for speed
	python -c 'import sys; import configurations; sys.exit(0 if sys.version_info >= (3, 10) else -1)' >/dev/null 2>&1
	python_dep_check=$?
	if [ ${python_dep_check} != 0 ]; then
		set +x
		printf 'Python dependencies may be missing or Python version is too old. Please install dependencies via:\n' >/dev/stderr
		printf 'pip install -r requirements.txt\n' >/dev/stderr
		printf 'You need at least Python 3.10\n' >/dev/stderr
		exit 1
	fi
}

check_and_fix_inotify_limit() {
	# Check if we're on a system that uses inotify (Linux)
	if [ ! -f /proc/sys/fs/inotify/max_user_watches ]; then
		# Not a Linux system or inotify not available, skip check
		return 0
	fi

	# Read current limit
	current_limit=$(cat /proc/sys/fs/inotify/max_user_watches 2>/dev/null || echo "0")
	recommended_limit=524288

	if [ "$current_limit" -lt "$recommended_limit" ]; then
		echo "Current inotify watch limit is $current_limit, which may be too low for development."
		echo "Recommended limit is $recommended_limit for smooth file watching."

		# Check if user has sudo access (without prompting for password)
		if sudo -n true 2>/dev/null; then
			echo "Attempting to increase inotify limit to $recommended_limit..."
			if sudo sysctl -w fs.inotify.max_user_watches=$recommended_limit >/dev/null 2>&1; then
				echo "Successfully increased inotify limit to $recommended_limit"
				
				# Make the change persistent across reboots
				if [ -d /etc/sysctl.d ]; then
					echo "fs.inotify.max_user_watches=$recommended_limit" | sudo tee /etc/sysctl.d/99-fighthealthinsurance-watches.conf >/dev/null 2>&1 || true
					echo "Change persisted to /etc/sysctl.d/99-fighthealthinsurance-watches.conf"
				fi
			else
				echo "Warning: Failed to increase inotify limit. You may experience file watching issues."
			fi
		else
			echo ""
			echo "To fix this, run one of the following commands:"
			echo "  Temporary fix (until reboot):"
			echo "    sudo sysctl -w fs.inotify.max_user_watches=$recommended_limit"
			echo "  Permanent fix:"
			echo "    echo 'fs.inotify.max_user_watches=$recommended_limit' | sudo tee /etc/sysctl.d/99-fighthealthinsurance-watches.conf"
			echo "    sudo sysctl -p /etc/sysctl.d/99-fighthealthinsurance-watches.conf"
			echo ""
		fi
	fi
}


set -ex

check_python_environment
check_and_fix_inotify_limit

"${SCRIPT_DIR}/build_static.sh"

# Run network checks in parallel for speed
# Create a temp directory for storing check results
TMPDIR=$(mktemp -d)
trap 'rm -rf "${TMPDIR}"' EXIT

check_vllm_health() {
  if kubectl get service -n totallylegitco vllm-health-svc >/dev/null 2>&1; then
    echo "export HEALTH_BACKEND_PORT=4280" > "$TMPDIR/vllm_health"
    echo "export HEALTH_BACKEND_HOST=localhost" >> "$TMPDIR/vllm_health"
    kubectl port-forward -n totallylegitco service/vllm-health-svc 4280:80 &
  else
    echo 'No connection to kube vllm health svc'
  fi
}

check_vllm_health_slipstream() {
  if kubectl get service -n totallylegitco vllm-health-svc-slipstream >/dev/null 2>&1; then
    echo "export NEW_HEALTH_BACKEND_PORT=4281" > "$TMPDIR/vllm_slipstream"
    echo "export NEW_HEALTH_BACKEND_HOST=localhost" >> "$TMPDIR/vllm_slipstream"
    kubectl port-forward -n totallylegitco service/vllm-health-svc-slipstream 4281:80 &
  else
    echo 'No connection to _new_ kube vllm health svc'
  fi
}

check_backup_backend() {
  if ping -c1 -W1 10.69.200.180 >/dev/null 2>&1; then
    echo "backup reachable"
    echo "export HEALTH_BACKUP_BACKEND_PORT=8000" > "$TMPDIR/backup"
    echo "export HEALTH_BACKUP_BACKEND_HOST=10.69.200.180" >> "$TMPDIR/backup"
    echo "export HEALTH_BACKUP_BACKEND_MODEL=\"/TotallyLegitCo/fighthealthinsurance_model_v0.5\"" >> "$TMPDIR/backup"
  else
    echo "backup not reachable."
  fi
}

check_alpha_backend() {
  if ping -c1 -W1 scrump.local.pigscanfly.ca >/dev/null 2>&1; then
    echo "alpha reachable"
    echo "export ALPHA_HEALTH_BACKEND_HOST=scrump.local.pigscanfly.ca" > "$TMPDIR/alpha"
  fi
}

# Run all network checks in parallel
check_vllm_health &
vllm_pid=$!
check_vllm_health_slipstream &
slipstream_pid=$!
check_backup_backend &
backup_pid=$!
check_alpha_backend &
alpha_pid=$!

# Wait for all network checks to complete
wait $vllm_pid $slipstream_pid $backup_pid $alpha_pid 2>/dev/null || true

# Source the results to set environment variables
for result_file in "$TMPDIR"/*; do
  if [ -f "$result_file" ]; then
    source "$result_file"
  fi
done

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
# Exclude patterns to reduce file watch count:
# - Python cache files (*.pyc, __pycache__, *.pyo)
# - Temporary files (*~, #*#, .#*)
# - Static files (static/* - regenerated by collectstatic)
# - JS build artifacts and dependencies (node_modules, dist, *.js in static/js - built by webpack)
# - Test files and artifacts (tests/*, .tox/*, *.sqlite*)
# - Large data files (*.csv, *.m4a, *.pdf in static)
# - Migration files (migrations/* - only changed intentionally, not during normal dev)
RELOAD_EXCLUDE="*.pyc,__pycache__/*,*.pyo,*~,#*#,.#*,node_modules/*,static/*,dist/*,.tox/*,*sqlite*,./tox/*,./tests/*,fhi-0.1.0/*,migrations/*,*.csv,*.m4a,*.pdf,*.png,*.jpg,*.jpeg,*.gif,*.svg,*.woff,*.woff2,*.ttf,*.eot,*.ico"
RECAPTCHA_TESTING=true OAUTHLIB_RELAX_TOKEN_SCOPE=1 uvicorn fighthealthinsurance.asgi:application --reload --reload-dir fighthealthinsurance --reload-exclude "$RELOAD_EXCLUDE" --access-log --log-config conf/uvlog_config.yaml --port 8000 --ssl-keyfile key.pem --ssl-certfile cert.pem "$@" --host 0.0.0.0
