#!/bin/bash

set -ex

SCRIPT_DIR="$(dirname "$0")"

"${SCRIPT_DIR}/build_static.sh" > logs || (echo "ERROR"; cat logs; exit 1)
