#!/bin/bash
# We expect npm depcheck to _maybe_ fail
set +ex

JS_PATH=fighthealthinsurance/static/js

pushd "${JS_PATH}"
npm ls >/dev/stderr 2>&1
npm_dep_check=$?

set -ex

if [ ${npm_dep_check} != 0 ]; then
	npm i || echo "Can't install?" >/dev/stderr
fi
npm run build
popd
./manage.py collectstatic