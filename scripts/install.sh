#!/bin/bash

set -e

# Retry helper: runs a command up to max_attempts times with exponential backoff.
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

set -x
python min_version.py

# shellcheck disable=SC1091
python -m venv .venv &&
  . .venv/bin/activate &&
  retry pip install -r requirements.txt &&
  retry pip install -r requirements-dev.txt

package_command=''
if command -v apt-get; then
  package_command="apt-get install -y"
elif command -v brew; then
  package_command="brew install"
fi

install_package() {
  package_name=$1
  # shellcheck disable=SC2029,SC2086
  ${package_command} "${package_name}" || sudo ${package_command} "${package_name}" ||
    (printf 'Can not install %s. Please install it manually.\n' "${package_name}" >/dev/stderr &&
       exit 1)
}

if ! command -v tesseract &>/dev/null; then
  # We need either the tesseract-ocr package OR easyocr
  install_package tesseract-ocr || pip install easyocr
fi

if [ ! -f "cert.pem" ]; then
  if ! command -v mkcert &>/dev/null; then
    install_package mkcert
  fi
  mkcert -cert-file cert.pem -key-file key.pem localhost 127.0.0.1
fi
