#!/bin/bash
# Shared shell utilities for CI and build scripts.
# Source this file to get common helper functions.

# retry: runs a command up to max_attempts times with exponential backoff.
# Usage: retry <command> [args...]
retry() {
  local max_attempts=3
  local attempt=1
  until "$@"; do
    if [ "${attempt}" -ge "${max_attempts}" ]; then
      echo "Command failed after ${max_attempts} attempts: $*" >&2
      return 1
    fi
    echo "Attempt ${attempt} failed. Waiting before retry..." >&2
    sleep $((attempt * 5))
    attempt=$((attempt + 1))
  done
}
