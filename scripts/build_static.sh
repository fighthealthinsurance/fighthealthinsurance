#!/bin/bash
# We expect npm depcheck to _maybe_ fail
set +ex

JS_PATH=fighthealthinsurance/static/js

# Check if JS source files have changed since last build
JS_CHECKSUM_FILE=".js_build_checksum"
CURRENT_JS_CHECKSUM=""
STORED_JS_CHECKSUM=""
SKIP_JS_BUILD=false

if [ -d "${JS_PATH}" ]; then
  # Calculate checksum of JS/TS source files, package.json, and webpack config
  CURRENT_JS_CHECKSUM=$(find "${JS_PATH}" -maxdepth 1 -type f \( -name "*.ts" -o -name "*.tsx" -o -name "*.js" -o -name "*.jsx" \) ! -name "*.min.js" -exec md5sum {} \; 2>/dev/null | sort | md5sum | cut -d ' ' -f 1)
  PACKAGE_JSON_SUM=$(md5sum "${JS_PATH}/package.json" "${JS_PATH}/webpack.config.js" 2>/dev/null | sort | md5sum | cut -d ' ' -f 1)
  CURRENT_JS_CHECKSUM="${CURRENT_JS_CHECKSUM}${PACKAGE_JSON_SUM}"
  
  if [ -f "$JS_CHECKSUM_FILE" ]; then
    STORED_JS_CHECKSUM=$(cat "$JS_CHECKSUM_FILE")
  fi
  
  if [ "$CURRENT_JS_CHECKSUM" = "$STORED_JS_CHECKSUM" ] && [ -d "${JS_PATH}/dist" ]; then
    echo "JavaScript source files unchanged, skipping build..."
    SKIP_JS_BUILD=true
  fi
fi

if [ "$SKIP_JS_BUILD" = false ]; then
  pushd "${JS_PATH}"
  npm ls >/dev/stderr 2>&1
  npm_dep_check=$?

  set -ex

  if [ ${npm_dep_check} != 0 ]; then
  	npm i || echo "Can't install?" >/dev/stderr
  fi
  npm run build
  popd
  
  # Save the checksum after successful build
  if [ -n "$CURRENT_JS_CHECKSUM" ]; then
    echo "$CURRENT_JS_CHECKSUM" > "$JS_CHECKSUM_FILE"
  fi
else
  set -ex
fi

if [ "$FAST" != "FAST" ]; then
  rm -rf static
  ./manage.py collectstatic
  # Generate the blog metadata so it's included in the container.
  ./manage.py generate_blog_metadata || echo "Warning: Failed to generate blog metadata. Continuing build without it."
fi
