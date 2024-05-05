#!/bin/bash

JS_PATH=fighthealthinsurance/static/js

python -c 'import configurations' 2>&1>/dev/null
python_dep_check=$?

pushd "${JS_PATH}"
npm ls 2>&1>/dev/null
npm_dep_check=$?
popd

if [ ${python_dep_check} != 0 ]; then
  set +x
  printf 'Python dependencies may be missing. Please install dependencies via:\n' >/dev/stderr
  printf 'pip install -r requirements.txt\n' >/dev/stderr
  exit 1
fi

set -ex

python min_version.py

package_command=''

if command -v apt-get; then
  package_command="apt-get install -y"
elif command -v brew; then
  package_command="brew install"
fi

install_package() {
  package_name=$1
  if [ -z ${package_command} ]; then
    printf 'Can not install %s. Please install it manually.\n' ${package_name} >/dev/stderr
    exit 1
  fi
  ${package_command} ${package_name}
}

if [ ! -f "cert.pem" ]; then
  if ! command -v mkcert &> /dev/null; then
    install_package mkcert
  fi

  mkcert -cert-file cert.pem -key-file key.pem localhost 127.0.0.1
fi

pushd "${JS_PATH}"
if [ ${npm_dep_check} != 0 ]; then
  npm i || echo "Can't install?" >/dev/stderr
fi
npm run build
popd


RECAPTCHA_TESTING=true OAUTHLIB_RELAX_TOKEN_SCOPE=1 \
  python manage.py runserver_plus --cert-file cert.pem --key-file key.pem
