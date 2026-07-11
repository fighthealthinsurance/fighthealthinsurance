#!/usr/bin/env bash
# =============================================================================
# backup-pg8-live-base.sh
# SAFETY-NET logical dump of the LIVE -8 database, independent of the streaming
# clone and independent of the (broken) object-store archive. If the live-raw
# seed of -9 fails, this dump is how we recover.
#
# Produces:
#   * a custom-format dump of the `app` database         (pg_dump -Fc)
#   * a globals-only dump of roles/tablespaces           (pg_dumpall -g)
# to a CONFIGURABLE destination directory. Validates the app dump with
# `pg_restore --list` and a size sanity check.
#
# SAFETY: read-only against -8 (pg_dump takes only shared locks; it does NOT
# block writers and does NOT modify -8). Never prints passwords. Never restores.
# =============================================================================
set -euo pipefail

NAMESPACE="${NAMESPACE:-totallylegitco}"
SRC_POD="${SRC_POD:-fhi-pg-main-8-3}"          # only-healthy primary
PG_CONTAINER="${PG_CONTAINER:-postgres}"
APP_DB="${APP_DB:-app}"
DEST_DIR="${DEST_DIR:-./pg8-safety-dump}"       # CONFIGURABLE destination
MIN_DUMP_BYTES="${MIN_DUMP_BYTES:-10000}"       # sanity floor for the app dump
ASSUME_YES="${ASSUME_YES:-false}"

info() { printf '\033[0;36m[INFO]\033[0m %s\n' "$*"; }
pass() { printf '\033[0;32m[PASS]\033[0m %s\n' "$*"; }
warn() { printf '\033[0;33m[WARN]\033[0m %s\n' "$*"; }
fail() { printf '\033[0;31m[FAIL]\033[0m %s\n' "$*"; }
die()  { fail "$*"; exit 1; }

# --- prereqs ---------------------------------------------------------------
command -v kubectl >/dev/null 2>&1 || die "kubectl not found on PATH"
CTX="$(kubectl config current-context 2>/dev/null || true)"
[ -n "$CTX" ] || die "no active kube-context"
kubectl -n "$NAMESPACE" get pod "$SRC_POD" >/dev/null 2>&1 \
  || die "source pod $SRC_POD not found in $NAMESPACE"

info "kube-context : $CTX"
info "namespace    : $NAMESPACE"
info "source pod   : $SRC_POD (read-only)"
info "app database : $APP_DB"
info "destination  : $DEST_DIR"

# --- confirmation ----------------------------------------------------------
if [ "$ASSUME_YES" != "true" ]; then
  printf '\nProceed with a logical dump of "%s" from %s? [type: yes] ' "$APP_DB" "$SRC_POD"
  read -r REPLY
  [ "$REPLY" = "yes" ] || die "aborted by user"
fi

mkdir -p "$DEST_DIR"
# Timestamp comes from the CLUSTER, not the local shell, so it reflects the pod.
STAMP="$(kubectl -n "$NAMESPACE" exec "$SRC_POD" -c "$PG_CONTAINER" -- date -u +%Y%m%dT%H%M%SZ)"
APP_OUT="$DEST_DIR/app-${STAMP}.dump"
GLOBALS_OUT="$DEST_DIR/globals-${STAMP}.sql"

# --- 1. app database, custom format ---------------------------------------
info "dumping database '$APP_DB' (custom format) ..."
# -Fc custom format streamed to a local file; superuser via local socket so no
# password is passed on any command line or printed.
kubectl -n "$NAMESPACE" exec "$SRC_POD" -c "$PG_CONTAINER" -- \
  pg_dump -U postgres -Fc --no-password "$APP_DB" > "$APP_OUT" \
  || die "pg_dump of $APP_DB failed"

# --- 2. globals (roles + tablespaces), no passwords in transit -------------
info "dumping globals (roles/tablespaces) ..."
kubectl -n "$NAMESPACE" exec "$SRC_POD" -c "$PG_CONTAINER" -- \
  pg_dumpall -U postgres --globals-only --no-password > "$GLOBALS_OUT" \
  || die "pg_dumpall --globals-only failed"

# --- 3. validate -----------------------------------------------------------
info "=== validating app dump ==="
SIZE="$(wc -c < "$APP_OUT" | tr -d ' ')"
info "app dump size: $SIZE bytes -> $APP_OUT"
if [ "${SIZE:-0}" -lt "$MIN_DUMP_BYTES" ] 2>/dev/null; then
  die "app dump is suspiciously small (<$MIN_DUMP_BYTES bytes) -- treat as FAILED"
fi

# pg_restore --list proves the archive is readable + structurally sane. The dump
# is ALREADY a local file, so validate it LOCALLY when possible -- no stdin pipe,
# and (critically) NO kubectl cp INTO -8: SRC_POD is the disk-pressured, only-
# healthy primary, and writing a multi-GB dump onto its PVC could tip it over.
# Fall back to streaming through the pod's pg_restore over stdin (which reads the
# stream and writes NOTHING to -8's disk) if there is no usable local pg_restore.
info "pg_restore --list sanity (table of contents) ..."
TOC_LINES=0
if command -v pg_restore >/dev/null 2>&1; then
  # 2>/dev/null: an older local pg_restore may reject a newer archive version;
  # that yields 0 lines and we fall back to the in-pod check below (not a failure).
  TOC_LINES="$(pg_restore --list "$APP_OUT" 2>/dev/null | grep -c ';' || true)"
  [ "${TOC_LINES:-0}" -gt 0 ] 2>/dev/null \
    && info "(validated locally with $(pg_restore --version 2>/dev/null | head -1))"
fi
if [ "${TOC_LINES:-0}" -eq 0 ] 2>/dev/null; then
  info "(validating via $SRC_POD over stdin -- reads the stream, writes nothing to -8's disk)"
  TOC_LINES="$(kubectl -n "$NAMESPACE" exec -i "$SRC_POD" -c "$PG_CONTAINER" -- \
    pg_restore --list < "$APP_OUT" | grep -c ';' || true)"
fi
if [ "${TOC_LINES:-0}" -gt 0 ] 2>/dev/null; then
  pass "app dump TOC readable ($TOC_LINES entries)"
else
  die "pg_restore --list produced no TOC -- dump is not valid"
fi

if [ -s "$GLOBALS_OUT" ]; then
  pass "globals dump written -> $GLOBALS_OUT ($(wc -c < "$GLOBALS_OUT" | tr -d ' ') bytes)"
else
  die "globals dump is empty"
fi

echo
pass "SAFETY-NET DUMP COMPLETE:"
pass "  app     : $APP_OUT"
pass "  globals : $GLOBALS_OUT"
info "Keep these off-cluster. They are the fallback if the -9 live-raw seed fails."
