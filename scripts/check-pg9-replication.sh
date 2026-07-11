#!/usr/bin/env bash
# =============================================================================
# check-pg9-replication.sh
# Verify fhi-pg-main-9 is a HEALTHY streaming replica of fhi-pg-main-8 and that
# its barman-cloud v0.13 sidecar + archiver are in place.
#
# Checks (all READ-ONLY):
#   1. -8 sees -9 in pg_stat_replication with state=streaming
#   2. the physical slot feeding -9 is active on -8
#   3. -9 is in recovery (pg_is_in_recovery() = true)
#   4. -9 receive/replay LSN + replay lag are sane
#   5. the plugin-barman-cloud v0.13 sidecar is injected into EVERY -9 pod
#   6. -9's archiver is advancing (no rising failures)
# =============================================================================
set -euo pipefail

NAMESPACE="${NAMESPACE:-totallylegitco}"
SRC_POD="${SRC_POD:-fhi-pg-main-8-3}"
PG_CONTAINER="${PG_CONTAINER:-postgres}"
DST_CLUSTER="${DST_CLUSTER:-fhi-pg-main-9}"
SRC_CLUSTER="${SRC_CLUSTER:-fhi-pg-main-8}"
# -9 stream user as -8 sees it (the migration role) -- used to match the row.
STREAM_USER="${STREAM_USER:-fhi_pg9_migration}"
PLUGIN_IMAGE_MATCH="${PLUGIN_IMAGE_MATCH:-plugin-barman-cloud}"
PLUGIN_VERSION_MATCH="${PLUGIN_VERSION_MATCH:-:v0.13}"
MAX_REPLAY_LAG_BYTES="${MAX_REPLAY_LAG_BYTES:-104857600}"   # 100 MiB soft ceiling

info() { printf '\033[0;36m[INFO]\033[0m %s\n' "$*"; }
pass() { printf '\033[0;32m[PASS]\033[0m %s\n' "$*"; }
warn() { printf '\033[0;33m[WARN]\033[0m %s\n' "$*"; }
fail() { printf '\033[0;31m[FAIL]\033[0m %s\n' "$*"; }
die()  { fail "$*"; exit 1; }

FAILED=0

command -v kubectl >/dev/null 2>&1 || die "kubectl not found on PATH"
CTX="$(kubectl config current-context 2>/dev/null || true)"
[ -n "$CTX" ] || die "no active kube-context"
info "kube-context : $CTX"
info "namespace    : $NAMESPACE"

kubectl -n "$NAMESPACE" get pod "$SRC_POD" >/dev/null 2>&1 || die "source pod $SRC_POD missing"

# find -9 pods
mapfile -t DST_PODS < <(kubectl -n "$NAMESPACE" get pods \
  -l "cnpg.io/cluster=$DST_CLUSTER" -o name 2>/dev/null | sed 's|pod/||')
[ "${#DST_PODS[@]}" -ge 1 ] || die "no pods found for cluster $DST_CLUSTER"
DST_POD="${DST_PODS[0]}"
info "-9 pods       : ${DST_PODS[*]}"

psql_src() { kubectl -n "$NAMESPACE" exec -i "$SRC_POD" -c "$PG_CONTAINER" -- \
  psql -U postgres -d postgres -At -v ON_ERROR_STOP=1 -c "$1"; }
psql_dst() { kubectl -n "$NAMESPACE" exec -i "$DST_POD" -c "$PG_CONTAINER" -- \
  psql -U postgres -d postgres -At -v ON_ERROR_STOP=1 -c "$1"; }

# --- 1. -8 sees -9 streaming ----------------------------------------------
info "=== [1] pg_stat_replication on $SRC_CLUSTER ==="
REPL="$(psql_src "
  SELECT application_name||'|'||client_addr||'|'||state||'|'||COALESCE(sync_state,'')
  FROM pg_stat_replication
  WHERE usename = '$STREAM_USER' OR application_name LIKE '%${DST_CLUSTER}%';")"
if [ -n "$REPL" ]; then
  printf '%s\n' "$REPL"
  if printf '%s' "$REPL" | grep -q '|streaming|'; then
    pass "-9 is streaming from -8"
  else
    fail "-9 present but not in 'streaming' state"; FAILED=1
  fi
else
  fail "no pg_stat_replication row for -9 (user $STREAM_USER) on -8"; FAILED=1
fi

# --- 2. physical slot active on -8 ----------------------------------------
info "=== [2] physical replication slot on $SRC_CLUSTER ==="
SLOTS="$(psql_src "
  SELECT slot_name||'|'||slot_type||'|active='||active
  FROM pg_replication_slots WHERE slot_type='physical';")"
printf '%s\n' "${SLOTS:-<none>}"
if printf '%s' "$SLOTS" | grep -q 'active=t'; then
  pass "an active physical slot exists on -8 (WAL retained for -9)"
else
  warn "no ACTIVE physical slot on -8 -- -9 may be relying on wal_keep_size only."
  warn "  Confirm intended (see runbook: external-primary slot is a VERIFY-LIVE item)."
fi

# --- 3. -9 in recovery -----------------------------------------------------
info "=== [3] -9 recovery state ==="
IN_REC="$(psql_dst "SELECT pg_is_in_recovery();")"
if [ "$IN_REC" = "t" ]; then
  pass "-9 is in recovery (read-only replica, as expected pre-promotion)"
else
  fail "-9 is NOT in recovery (pg_is_in_recovery=$IN_REC) -- it may be a rogue primary!"; FAILED=1
fi

# --- 4. LSN + replay lag ---------------------------------------------------
info "=== [4] -9 receive/replay LSN + lag ==="
LSNS="$(psql_dst "
  SELECT COALESCE(pg_last_wal_receive_lsn()::text,'?')||'|'||
         COALESCE(pg_last_wal_replay_lsn()::text,'?')||'|'||
         COALESCE(pg_wal_lsn_diff(pg_last_wal_receive_lsn(),
                                  pg_last_wal_replay_lsn())::text,'?');")"
RECV="$(printf '%s' "$LSNS" | cut -d'|' -f1)"
REPLAY="$(printf '%s' "$LSNS" | cut -d'|' -f2)"
LAG="$(printf '%s' "$LSNS" | cut -d'|' -f3)"
info "receive_lsn=$RECV replay_lsn=$REPLAY replay_lag_bytes=$LAG"
if [ "${LAG:-0}" -le "$MAX_REPLAY_LAG_BYTES" ] 2>/dev/null; then
  pass "replay lag within ${MAX_REPLAY_LAG_BYTES} bytes"
else
  warn "replay lag $LAG bytes exceeds soft ceiling -- catching up? re-run to confirm trend."
fi

# --- 5. plugin sidecar in EVERY -9 pod ------------------------------------
info "=== [5] barman-cloud v0.13 sidecar injection ==="
for pod in "${DST_PODS[@]}"; do
  IMAGES="$(kubectl -n "$NAMESPACE" get pod "$pod" \
    -o jsonpath='{range .spec.containers[*]}{.name}={.image}{"\n"}{end}{range .spec.initContainers[*]}init:{.name}={.image}{"\n"}{end}')"
  if printf '%s' "$IMAGES" | grep -q "$PLUGIN_IMAGE_MATCH"; then
    if printf '%s' "$IMAGES" | grep "$PLUGIN_IMAGE_MATCH" | grep -q "$PLUGIN_VERSION_MATCH"; then
      pass "$pod: plugin sidecar present at $PLUGIN_VERSION_MATCH"
    else
      fail "$pod: plugin sidecar present but NOT $PLUGIN_VERSION_MATCH ->"; FAILED=1
      printf '%s\n' "$IMAGES" | grep "$PLUGIN_IMAGE_MATCH"
    fi
  else
    fail "$pod: NO $PLUGIN_IMAGE_MATCH sidecar injected"; FAILED=1
  fi
done

# --- 6. -9 archiver advancing ---------------------------------------------
info "=== [6] -9 pg_stat_archiver ==="
ARCH="$(psql_dst "
  SELECT 'archived='||archived_count||' failed='||failed_count
       ||' last_archived_time='||COALESCE(last_archived_time::text,'(none)')
  FROM pg_stat_archiver;")"
printf '%s\n' "$ARCH"
A_CNT="$(printf '%s' "$ARCH" | sed -n 's/archived=\([0-9]*\).*/\1/p')"
F_CNT="$(printf '%s' "$ARCH" | sed -n 's/.*failed=\([0-9]*\).*/\1/p')"
if [ "${A_CNT:-0}" -gt 0 ] 2>/dev/null && [ "${F_CNT:-0}" -eq 0 ] 2>/dev/null; then
  pass "-9 archiver healthy (archived>0, failed=0)"
else
  # See cluster manifest VERIFY-LIVE note: a replica only archives when
  # archive_mode=always; on some setups -9 begins archiving only after promotion.
  warn "-9 archiver not yet advancing (archived=$A_CNT failed=$F_CNT)."
  warn "  If failed is RISING -> real problem (check B2 creds/checksum env)."
  warn "  If both 0 while still a replica -> may be expected (archive_mode on vs always)."
fi

echo
if [ "$FAILED" -eq 0 ]; then
  pass "-9 REPLICATION HEALTHY."
  exit 0
else
  fail "-9 REPLICATION NOT HEALTHY -- resolve [FAIL] items before promotion."
  exit 1
fi
