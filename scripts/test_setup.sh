#!/bin/bash

set -ex

SCRIPT_DIR="$(dirname "$0")"

# A hard-killed prior run can leave WAL sidecars beside a pinned DBNAME
# (TestActor runs sqlite in journal_mode=WAL; see settings.py). Django's
# test-DB creation removes only the main file, and sqlite would replay the
# stale -wal into the freshly created database at the same path. Safe to do
# here: nothing has opened the database yet. Only the explicit DBNAME pin
# needs this -- the default name is timestamped per run.
if [ -n "${DBNAME:-}" ]; then
  rm -f "${DBNAME}-wal" "${DBNAME}-shm"
fi

"${SCRIPT_DIR}/"build_static.sh &> logs || (echo "ERROR building"; cat logs; exit 1)

echo "Tests ready to run."
