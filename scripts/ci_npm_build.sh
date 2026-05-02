#!/bin/bash
# Install npm dependencies and build static JS assets for CI.
# Uses npm's built-in retry flags for network resilience.

set -e

JS_PATH="fighthealthinsurance/static/js"

cd "${JS_PATH}"

# Use npm's built-in retry/timeout flags so transient network errors
# don't fail the build outright.
npm install \
  --fetch-retries=5 \
  --fetch-retry-mintimeout=20000 \
  --fetch-retry-maxtimeout=120000 \
  --fetch-retry-factor=2

npm run build
npm cache clean --force
