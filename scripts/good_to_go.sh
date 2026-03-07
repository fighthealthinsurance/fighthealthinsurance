#!/bin/bash
# Run all tox test environments in parallel and report results.
# Usage: ./scripts/good_to_go.sh [--py 313] [-- extra tox args]
#
# Runs each tox environment as a background job, captures output to temp files,
# then prints a summary showing which passed and which failed (with output).

set -euo pipefail

# Determine python version slug (default: auto-detect)
PY_VERSION=""
TOX_EXTRA_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --py)
            PY_VERSION="$2"
            shift 2
            ;;
        --)
            shift
            TOX_EXTRA_ARGS=("$@")
            break
            ;;
        *)
            echo "Unknown argument: $1" >&2
            echo "Usage: $0 [--py 311|312|313] [-- extra tox args]" >&2
            exit 1
            ;;
    esac
done

# Auto-detect python version if not specified
if [[ -z "$PY_VERSION" ]]; then
    PY_FULL=$(python3 --version 2>&1 | grep -oP '\d+\.\d+')
    PY_VERSION="${PY_FULL//./}"
fi

echo "Using Python version slug: py${PY_VERSION}"

# Define the environments to run
ENVS=(
    "py${PY_VERSION}-django52-sync"
    "py${PY_VERSION}-django52-async"
    "py${PY_VERSION}-django52-async-unit"
    "py${PY_VERSION}-django52-sync-actor"
    "py${PY_VERSION}-django52-selenium"
    "py${PY_VERSION}-black"
    "py${PY_VERSION}-mypy"
    "prettier"
)

TMPDIR_BASE=$(mktemp -d)
trap 'rm -rf "$TMPDIR_BASE"' EXIT

declare -A PIDS
declare -A LOGFILES

echo "Starting ${#ENVS[@]} tox environments in parallel..."
echo ""

for env in "${ENVS[@]}"; do
    logfile="${TMPDIR_BASE}/${env}.log"
    LOGFILES["$env"]="$logfile"
    if [[ ${#TOX_EXTRA_ARGS[@]} -gt 0 ]]; then
        tox -e "$env" "${TOX_EXTRA_ARGS[@]}" > "$logfile" 2>&1 &
    else
        tox -e "$env" > "$logfile" 2>&1 &
    fi
    PIDS["$env"]=$!
    echo "  Started: $env (PID ${PIDS[$env]})"
done

echo ""
echo "Waiting for all environments to finish..."
echo ""

# Collect results
declare -A RESULTS
FAILED_ENVS=()

for env in "${ENVS[@]}"; do
    pid="${PIDS[$env]}"
    if wait "$pid"; then
        RESULTS["$env"]=0
    else
        RESULTS["$env"]=$?
        FAILED_ENVS+=("$env")
    fi
done

# Print summary
echo "============================================"
echo "                 RESULTS"
echo "============================================"
echo ""

for env in "${ENVS[@]}"; do
    rc="${RESULTS[$env]}"
    if [[ "$rc" -eq 0 ]]; then
        echo "  PASS  $env"
    else
        echo "  FAIL  $env (exit code $rc)"
    fi
done

echo ""

# Print failure details
if [[ ${#FAILED_ENVS[@]} -gt 0 ]]; then
    echo "============================================"
    echo "            FAILURE DETAILS"
    echo "============================================"

    for env in "${FAILED_ENVS[@]}"; do
        echo ""
        echo "--------------------------------------------"
        echo "  FAILED: $env"
        echo "--------------------------------------------"
        # Show last 50 lines of output for failed envs
        tail -n 50 "${LOGFILES[$env]}"
        echo ""
        echo "  Full log: ${LOGFILES[$env]}"
    done

    echo ""
    echo "NOT GOOD TO GO: ${#FAILED_ENVS[@]} environment(s) failed."
    exit 1
else
    echo "ALL GOOD TO GO: all ${#ENVS[@]} environments passed."
    exit 0
fi
