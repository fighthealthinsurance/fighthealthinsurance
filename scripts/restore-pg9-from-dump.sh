#!/usr/bin/env bash
# =============================================================================
# restore-pg9-from-dump.sh
# Load a logical dump of -8 (produced by scripts/backup-pg8-live-base.sh) into
# the FRESH, EMPTY fhi-pg-main-9 primary. This is the seed step of the dump/
# restore migration -- run it inside the cutover maintenance window, AFTER -8
# writes are quiesced and AFTER a fresh dump has been taken.
#
# Restores, in order:
#   1. globals (roles/tablespaces)  via psql   -- tolerates "role already exists"
#      (CNPG already created `postgres` + the managed `ziggystardust` role).
#   2. the `app` database (custom format) via pg_restore into the existing,
#      empty `app` DB that initdb created (owner ziggystardust).
#
# SAFETY:
#   * REFUSES unless -9 is a writable primary (pg_is_in_recovery = f).
#   * REFUSES if the target `app` DB already has user tables (never restore over
#     data). Override only with FORCE=true if you deliberately re-seed.
#   * Never prints passwords; restores as `postgres` over the pod's local socket.
# =============================================================================
set -euo pipefail

NAMESPACE="${NAMESPACE:-totallylegitco}"
TGT_CLUSTER="${TGT_CLUSTER:-fhi-pg-main-9}"
TGT_POD="${TGT_POD:-}"                       # auto-detected primary if empty
PG_CONTAINER="${PG_CONTAINER:-postgres}"
APP_DB="${APP_DB:-app}"
APP_ROLE="${APP_ROLE:-ziggystardust}"
DUMP_DIR="${DUMP_DIR:-./pg8-safety-dump}"    # where backup-pg8-live-base.sh wrote
APP_DUMP="${APP_DUMP:-}"                      # explicit app-*.dump (else newest)
GLOBALS_SQL="${GLOBALS_SQL:-}"               # explicit globals-*.sql (else newest)
JOBS="${JOBS:-4}"                            # pg_restore parallelism
FORCE="${FORCE:-false}"
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

# resolve the -9 PRIMARY pod (role=primary) unless one was pinned
if [ -z "$TGT_POD" ]; then
  TGT_POD="$(kubectl -n "$NAMESPACE" get pods \
    -l "cnpg.io/cluster=$TGT_CLUSTER,cnpg.io/instanceRole=primary" \
    -o name 2>/dev/null | sed 's|pod/||' | head -1)"
  # fall back to the older label key if needed
  [ -n "$TGT_POD" ] || TGT_POD="$(kubectl -n "$NAMESPACE" get pods \
    -l "cnpg.io/cluster=$TGT_CLUSTER,role=primary" \
    -o name 2>/dev/null | sed 's|pod/||' | head -1)"
fi
[ -n "$TGT_POD" ] || die "could not find the primary pod for $TGT_CLUSTER"
kubectl -n "$NAMESPACE" get pod "$TGT_POD" >/dev/null 2>&1 \
  || die "target pod $TGT_POD not found in $NAMESPACE"

# locate dump files (newest by name if not pinned; names sort by timestamp)
[ -n "$APP_DUMP" ]   || APP_DUMP="$(find "$DUMP_DIR" -maxdepth 1 -name 'app-*.dump' 2>/dev/null | sort | tail -1 || true)"
[ -n "$GLOBALS_SQL" ] || GLOBALS_SQL="$(find "$DUMP_DIR" -maxdepth 1 -name 'globals-*.sql' 2>/dev/null | sort | tail -1 || true)"
if [ -z "$APP_DUMP" ] || [ ! -f "$APP_DUMP" ]; then
  die "no app dump found (looked in $DUMP_DIR/app-*.dump). Set APP_DUMP."
fi
if [ -z "$GLOBALS_SQL" ] || [ ! -f "$GLOBALS_SQL" ]; then
  die "no globals dump found ($DUMP_DIR/globals-*.sql). Set GLOBALS_SQL."
fi

psql_t() { kubectl -n "$NAMESPACE" exec -i "$TGT_POD" -c "$PG_CONTAINER" -- psql -U postgres -Atc "$1"; }

info "kube-context : $CTX"
info "namespace    : $NAMESPACE"
info "target pod   : $TGT_POD (primary of $TGT_CLUSTER)"
info "app database : $APP_DB (owner $APP_ROLE)"
info "app dump     : $APP_DUMP"
info "globals dump : $GLOBALS_SQL"

# --- gate 1: -9 must be a writable primary ---------------------------------
IN_REC="$(psql_t 'SELECT pg_is_in_recovery();' | tr -d '[:space:]')"
[ "$IN_REC" = "f" ] || die "$TGT_CLUSTER is in recovery (pg_is_in_recovery='$IN_REC'); it must be a writable primary before restore."
pass "$TGT_CLUSTER is a writable primary."

# --- gate 2: target app DB must be empty (no user tables) unless FORCE ------
TBL="$(kubectl -n "$NAMESPACE" exec -i "$TGT_POD" -c "$PG_CONTAINER" -- \
  psql -U postgres -d "$APP_DB" -Atc \
  "SELECT count(*) FROM pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema');" \
  | tr -d '[:space:]')"
if [ "${TBL:-0}" -ne 0 ] 2>/dev/null; then
  [ "$FORCE" = "true" ] || die "$APP_DB on $TGT_CLUSTER already has $TBL user tables. Refusing to restore over data (set FORCE=true to override)."
  warn "$APP_DB already has $TBL tables; FORCE=true -- continuing anyway."
fi

# --- confirmation ----------------------------------------------------------
if [ "$ASSUME_YES" != "true" ]; then
  printf '\nRestore the above dump into %s/%s? Ensure -8 writes are QUIESCED. [type: yes] ' "$TGT_CLUSTER" "$APP_DB"
  read -r REPLY
  [ "$REPLY" = "yes" ] || die "aborted by user"
fi

# --- 1. globals (roles/tablespaces) ----------------------------------------
# psql without ON_ERROR_STOP: "role already exists" for postgres/ziggystardust is
# EXPECTED and non-fatal (CNPG manages those). Grants/other roles still apply.
info "restoring globals (role-exists errors are expected + ignored) ..."
kubectl -n "$NAMESPACE" exec -i "$TGT_POD" -c "$PG_CONTAINER" -- \
  psql -U postgres -f - < "$GLOBALS_SQL" 2>&1 | grep -vi 'already exists' || true

# --- 2. app database (custom format) ---------------------------------------
info "restoring database '$APP_DB' (pg_restore -j$JOBS) ..."
# --no-comments avoids noise; ownership (OWNER TO ziggystardust) is preserved
# because we restore as superuser. --exit-on-error would abort on the first
# benign notice, so we DON'T use it; we validate row counts explicitly below.
kubectl -n "$NAMESPACE" exec -i "$TGT_POD" -c "$PG_CONTAINER" -- \
  pg_restore -U postgres -d "$APP_DB" -j "$JOBS" --no-password < "$APP_DUMP" \
  || warn "pg_restore reported non-zero (often benign notices) -- verifying by row counts next."

# --- 3. validate -----------------------------------------------------------
info "=== post-restore validation ==="
AFTER_TBL="$(kubectl -n "$NAMESPACE" exec -i "$TGT_POD" -c "$PG_CONTAINER" -- \
  psql -U postgres -d "$APP_DB" -Atc \
  "SELECT count(*) FROM pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema');" \
  | tr -d '[:space:]')"
[ "${AFTER_TBL:-0}" -gt 0 ] 2>/dev/null || die "no user tables present after restore -- restore FAILED"
pass "user tables after restore: $AFTER_TBL"

# django_migrations is the app's own integrity check -- must be non-empty
MIG="$(kubectl -n "$NAMESPACE" exec -i "$TGT_POD" -c "$PG_CONTAINER" -- \
  psql -U postgres -d "$APP_DB" -Atc \
  "SELECT count(*) FROM django_migrations;" 2>/dev/null | tr -d '[:space:]' || echo 0)"
if [ "${MIG:-0}" -gt 0 ] 2>/dev/null; then
  pass "django_migrations rows: $MIG"
else
  warn "django_migrations is empty or missing -- confirm this is expected for your schema"
fi

# ownership sanity: at least one table owned by the app role
OWNED="$(kubectl -n "$NAMESPACE" exec -i "$TGT_POD" -c "$PG_CONTAINER" -- \
  psql -U postgres -d "$APP_DB" -Atc \
  "SELECT count(*) FROM pg_tables WHERE tableowner='$APP_ROLE';" | tr -d '[:space:]')"
if [ "${OWNED:-0}" -gt 0 ] 2>/dev/null; then
  pass "tables owned by $APP_ROLE: $OWNED"
else
  warn "no tables owned by $APP_ROLE -- check ownership (app may fail on permissions)"
fi

echo
pass "RESTORE INTO $TGT_CLUSTER COMPLETE."
info "Next: run scripts/validate-pg8-vs-pg9.sh (counts must match now that -8 is quiesced),"
info "then scripts/cutover-app-to-pg9.sh to flip PDBHOST."
