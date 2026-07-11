#!/usr/bin/env bash
# =============================================================================
# check-pg8-source.sh
# READ-ONLY preflight against the LIVE source primary fhi-pg-main-8-3.
#
# Confirms -8 has the capacity + configuration to accept -9's streaming clone
# without exhausting connections or slots, and reports archiver health.
#
# SAFETY: this script is strictly READ-ONLY. It NEVER kills a session, NEVER
# writes, and NEVER touches -8's data or PVC. It only FLAGS clearly-stale app
# connections for a human to consider -- it will not auto-kill anything, and it
# explicitly ignores internal / replication / autovacuum / superuser sessions.
# =============================================================================
set -euo pipefail

NAMESPACE="${NAMESPACE:-totallylegitco}"
SRC_POD="${SRC_POD:-fhi-pg-main-8-3}"          # only-healthy primary; do NOT change to -8-2
PG_CONTAINER="${PG_CONTAINER:-postgres}"
# Minimum number of FREE normal connection slots we want to see before cloning.
MIN_FREE_CONNS="${MIN_FREE_CONNS:-10}"

log()  { printf '%s\n' "$*"; }
info() { printf '\033[0;36m[INFO]\033[0m %s\n' "$*"; }
pass() { printf '\033[0;32m[PASS]\033[0m %s\n' "$*"; }
warn() { printf '\033[0;33m[WARN]\033[0m %s\n' "$*"; }
fail() { printf '\033[0;31m[FAIL]\033[0m %s\n' "$*"; }
die()  { fail "$*"; exit 1; }

FAILED=0

# --- prereqs ---------------------------------------------------------------
command -v kubectl >/dev/null 2>&1 || die "kubectl not found on PATH"

CTX="$(kubectl config current-context 2>/dev/null || true)"
[ -n "$CTX" ] || die "no active kube-context (kubectl config current-context is empty)"
info "kube-context : $CTX"
info "namespace    : $NAMESPACE"
info "source pod   : $SRC_POD (READ-ONLY)"

kubectl -n "$NAMESPACE" get pod "$SRC_POD" >/dev/null 2>&1 \
  || die "source pod $SRC_POD not found in namespace $NAMESPACE"

# Run a read-only SQL snippet on -8-3 as the postgres superuser (local socket).
psql_ro() {
  kubectl -n "$NAMESPACE" exec -i "$SRC_POD" -c "$PG_CONTAINER" -- \
    psql -U postgres -d postgres -At -v ON_ERROR_STOP=1 -c "$1"
}

# --- 1. capacity settings --------------------------------------------------
info "=== connection / replication capacity ==="
SETTINGS="$(psql_ro "
  SELECT name || '=' || setting
  FROM pg_settings
  WHERE name IN ('max_connections','max_wal_senders','superuser_reserved_connections',
                 'max_replication_slots','wal_keep_size','max_slot_wal_keep_size')
  ORDER BY name;")"
log "$SETTINGS"

get() { printf '%s\n' "$SETTINGS" | grep "^$1=" | cut -d= -f2- ; }
MAX_CONN="$(get max_connections)"
MAX_SENDERS="$(get max_wal_senders)"
MAX_SLOTS="$(get max_replication_slots)"
MAX_SLOT_KEEP="$(get max_slot_wal_keep_size)"

# a streaming replica needs >=1 wal sender + >=1 replication slot headroom
if [ "${MAX_SENDERS:-0}" -ge 1 ] 2>/dev/null; then
  pass "max_wal_senders=$MAX_SENDERS (>=1 available for -9)"
else
  fail "max_wal_senders=$MAX_SENDERS -- no room for -9 to stream"; FAILED=1
fi
if [ "${MAX_SLOTS:-0}" -ge 1 ] 2>/dev/null; then
  pass "max_replication_slots=$MAX_SLOTS (>=1 for the -9 migration slot)"
else
  fail "max_replication_slots=$MAX_SLOTS -- cannot create a slot for -9"; FAILED=1
fi
# max_slot_wal_keep_size == -1 means UNBOUNDED: a stuck slot can fill the disk
# (this is the fhi-pg-main-8-1 failure mode). Warn loudly; the runbook sets it.
if [ "$MAX_SLOT_KEEP" = "-1" ]; then
  warn "max_slot_wal_keep_size=-1 (UNBOUNDED) -- a stuck slot could fill -8's disk."
  warn "  Set a bound on -8 BEFORE creating the -9 slot (see runbook Phase 1)."
else
  pass "max_slot_wal_keep_size=$MAX_SLOT_KEEP (bounded)"
fi

# --- 2. free normal connection slots ---------------------------------------
info "=== connection headroom ==="
# reserved = superuser_reserved_connections; normal available = max - reserved - used(non-super)
FREE="$(psql_ro "
  SELECT
    (SELECT setting::int FROM pg_settings WHERE name='max_connections')
  - (SELECT setting::int FROM pg_settings WHERE name='superuser_reserved_connections')
  - (SELECT count(*) FROM pg_stat_activity WHERE backend_type='client backend');")"
log "approx free NORMAL connection slots: $FREE  (max_connections=$MAX_CONN)"
if [ "${FREE:-0}" -ge "$MIN_FREE_CONNS" ] 2>/dev/null; then
  pass "at least $MIN_FREE_CONNS normal slots free ($FREE) -- clone can connect"
else
  fail "only $FREE free normal slots (< $MIN_FREE_CONNS) -- clone may be refused"; FAILED=1
fi

# --- 3. replication slots (retention pressure) -----------------------------
info "=== existing replication slots ==="
psql_ro "
  SELECT slot_name, slot_type, active,
         restart_lsn,
         pg_size_pretty(COALESCE(
           pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn), 0)) AS retained
  FROM pg_replication_slots
  ORDER BY active DESC, slot_name;" || warn "could not read pg_replication_slots"

# --- 4. activity summary ---------------------------------------------------
info "=== pg_stat_activity summary (by state/backend_type) ==="
psql_ro "
  SELECT COALESCE(state,'(none)') AS state, backend_type, count(*)
  FROM pg_stat_activity
  GROUP BY 1,2 ORDER BY 3 DESC;" || warn "could not summarise pg_stat_activity"

# FLAG (do NOT kill) clearly-stale APP connections: idle-in-transaction or idle
# for a long time, on the app db, from a NON-superuser, NON-replication,
# NON-autovacuum, NON-internal backend. This is advisory only.
info "=== possibly-stale APP connections (ADVISORY -- never auto-killed) ==="
psql_ro "
  SELECT pid, usename, state,
         now() - state_change AS idle_for, left(query,60) AS query
  FROM pg_stat_activity
  WHERE backend_type = 'client backend'
    AND datname = 'app'
    AND usename NOT IN ('postgres','streaming_replica')
    AND NOT usesysid IN (SELECT oid FROM pg_roles WHERE rolsuper)
    AND state IN ('idle in transaction','idle in transaction (aborted)')
    AND state_change < now() - interval '30 minutes'
  ORDER BY idle_for DESC;" \
  && info "  (any rows above are a HUMAN decision -- this script will not kill them)"

# --- 5. archiver health ----------------------------------------------------
info "=== pg_stat_archiver ==="
ARCH="$(psql_ro "
  SELECT 'archived_count='||archived_count
       ||' failed_count='||failed_count
       ||' last_archived_wal='||COALESCE(last_archived_wal,'(none)')
       ||' last_archived_time='||COALESCE(last_archived_time::text,'(none)')
       ||' last_failed_wal='||COALESCE(last_failed_wal,'(none)')
  FROM pg_stat_archiver;")"
log "$ARCH"
FAILED_CNT="$(printf '%s\n' "$ARCH" | sed -n 's/.*failed_count=\([0-9]*\).*/\1/p')"
ARCHIVED_CNT="$(printf '%s\n' "$ARCH" | sed -n 's/archived_count=\([0-9]*\).*/\1/p')"
if [ "${ARCHIVED_CNT:-0}" -gt 0 ] 2>/dev/null && [ "${FAILED_CNT:-0}" -eq 0 ] 2>/dev/null; then
  pass "archiver healthy (archived>0, failed=0)"
else
  # This is EXPECTED to be red until Phase 0 fixes -8's archiving. Not fatal to
  # the CLONE (we stream direct, not via archive) but the runbook requires it
  # fixed before relying on PITR. Flag, do not hard-fail the preflight on it.
  warn "archiver NOT healthy (archived=$ARCHIVED_CNT failed=$FAILED_CNT)."
  warn "  Expected until Phase 0 clears the WAL wedge. Do NOT rely on -8's"
  warn "  object-store archive for the migration -- -9 streams directly."
fi

# --- verdict ---------------------------------------------------------------
echo
if [ "$FAILED" -eq 0 ]; then
  pass "SOURCE PREFLIGHT PASSED -- -8 can accept the -9 streaming clone."
  exit 0
else
  fail "SOURCE PREFLIGHT FAILED -- resolve the [FAIL] items before cloning."
  exit 1
fi
