#!/bin/bash
# Build static assets for the application
#
# Optimizations:
# - Uses checksum-based caching to skip JS builds when source files haven't changed
# - Checksum includes .ts, .tsx, .js, .jsx files (excluding .min.js & .bundle.js), package.json, and webpack.config.js
# - Uses a separate checksum for collectstatic/compress to skip when all static files are unchanged
# - This can save 8-10 seconds on subsequent runs when no changes are made
#
# We expect npm depcheck to _maybe_ fail
set +ex

JS_PATH=fighthealthinsurance/static/js

# Check if JS source files have changed since last build
JS_CHECKSUM_FILE=".js_build_checksum"
STATIC_CHECKSUM_FILE=".static_build_checksum"
CURRENT_JS_CHECKSUM=""
STORED_JS_CHECKSUM=""
SKIP_JS_BUILD=false

if [ "$FAST" = "FAST" ]; then
  echo "Skipping static changes."
  exit 0
fi

if [ -d "${JS_PATH}" ]; then
  # Calculate checksum of JS/TS source files
  # Using -maxdepth 1 because source files are in the js directory, not subdirectories
  # (node_modules and dist are excluded by design)
  echo "Computing checksum..."
  CURRENT_JS_CHECKSUM=$(find "${JS_PATH}" -maxdepth 1 -type f \( -name "*.ts" -o -name "*.tsx" -o -name "*.js" -o -name "*.jsx" \) ! -name "*.bundle.js" ! -name "*.min.js" -exec md5sum {} \; 2>/dev/null | sort | md5sum | cut -d ' ' -f 1)

  # Add checksums of package.json and webpack config if they exist
  if [ -f "${JS_PATH}/package.json" ]; then
    PACKAGE_JSON_SUM=$(md5sum "${JS_PATH}/package.json" 2>/dev/null | cut -d ' ' -f 1)
    CURRENT_JS_CHECKSUM="${CURRENT_JS_CHECKSUM}${PACKAGE_JSON_SUM}"
  fi
  if [ -f "${JS_PATH}/webpack.config.js" ]; then
    WEBPACK_SUM=$(md5sum "${JS_PATH}/webpack.config.js" 2>/dev/null | cut -d ' ' -f 1)
    CURRENT_JS_CHECKSUM="${CURRENT_JS_CHECKSUM}${WEBPACK_SUM}"
  fi

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
    npm i || npm i --ignore-scripts || (echo "Can't install?" >/dev/stderr; exit 1)
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

# Checksum for collectstatic/compress: covers templates, CSS, blog posts, and the JS build output
# This avoids re-running collectstatic + compress when nothing has changed
CURRENT_STATIC_CHECKSUM=""
STORED_STATIC_CHECKSUM=""
SKIP_STATIC_COLLECT=false

compute_static_checksum() {
  local parts=""
  # Include all static files (JS dist, non-bundled JS like jquery.sticky.js, CSS, images, blog posts)
  # Excludes node_modules and source .ts/.tsx files (those are covered by the JS build checksum)
  if [ -d "fighthealthinsurance/static" ]; then
    parts="${parts}$(find fighthealthinsurance/static -type f \
      -not -path '*/node_modules/*' \
      -not -name '*.ts' -not -name '*.tsx' \
      -exec md5sum {} \; 2>/dev/null | sort | md5sum | cut -d ' ' -f 1)"
  fi
  # Include templates (referenced by compress)
  if [ -d "fighthealthinsurance/templates" ]; then
    parts="${parts}$(find fighthealthinsurance/templates -type f -exec md5sum {} \; 2>/dev/null | sort | md5sum | cut -d ' ' -f 1)"
  fi
  echo "$parts" | md5sum | cut -d ' ' -f 1
}

CURRENT_STATIC_CHECKSUM=$(compute_static_checksum)
if [ -f "$STATIC_CHECKSUM_FILE" ]; then
  STORED_STATIC_CHECKSUM=$(cat "$STATIC_CHECKSUM_FILE")
fi

if [ "$CURRENT_STATIC_CHECKSUM" = "$STORED_STATIC_CHECKSUM" ] && [ -d "static" ]; then
  echo "Static files unchanged, skipping collectstatic/compress..."
  SKIP_STATIC_COLLECT=true
fi

if [ "$SKIP_STATIC_COLLECT" = false ]; then
  ./manage.py collectstatic --noinput --clear
  ./manage.py compress --force
  # Generate the blog metadata so it's included in the container.
  ./manage.py generate_blog_metadata || echo "Warning: Failed to generate blog metadata. Continuing build without it."

  # Save checksum after successful collect
  if [ -n "$CURRENT_STATIC_CHECKSUM" ]; then
    echo "$CURRENT_STATIC_CHECKSUM" > "$STATIC_CHECKSUM_FILE"
  fi
fi
