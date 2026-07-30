#!/bin/bash
# PostToolUse hook: keep edited Python files black-clean as they are written,
# using the same pinned black as CI (the tox py313-black env) when available,
# falling back to whatever black is on PATH, and doing nothing when neither
# exists.
#
# Scope matches what is black-enforced (the tox.ini black env's paths plus
# fhi_users, which CLAUDE.md formats): setup.py, fighthealthinsurance/,
# fhi_users/, charts/. tests/ is intentionally NOT auto-formatted -- it is
# not black-enforced and not currently black-clean, so formatting a touched
# test file would flood the diff with unrelated changes.
#
# Never blocks the edit: always exits 0.
set -uo pipefail

FILE="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("tool_input",{}).get("file_path",""))' 2>/dev/null || true)"
case "$FILE" in
  *.py) ;;
  *) exit 0 ;;
esac

ROOT="${CLAUDE_PROJECT_DIR:-$PWD}"
REL="${FILE#"$ROOT"/}"
case "$REL" in
  setup.py|fighthealthinsurance/*|fhi_users/*|charts/*) ;;
  *) exit 0 ;;
esac
[ -f "$FILE" ] || exit 0

BLACK="$ROOT/.tox/py313-black/bin/black"
if [ ! -x "$BLACK" ]; then
  BLACK="$(command -v black || true)"
  [ -n "$BLACK" ] || exit 0
fi

BEFORE="$(md5sum "$FILE" 2>/dev/null | cut -d ' ' -f 1)"
"$BLACK" -q "$FILE" >/dev/null 2>&1 || exit 0
AFTER="$(md5sum "$FILE" 2>/dev/null | cut -d ' ' -f 1)"
if [ "$BEFORE" != "$AFTER" ]; then
  # Stdout reaches the model: flag that the on-disk contents moved under it.
  echo "black reformatted $REL after this edit; re-read it before matching on its exact contents."
fi
exit 0
