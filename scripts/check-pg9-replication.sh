#!/usr/bin/env bash
# =============================================================================
# check-pg9-replication.sh
# Verify fhi-pg-main-9 is a HEALTHY streaming replica of fhi-pg-main-8 and that
# its barman-cloud v0.13 sidecar + archiver are in place.
#
# Checks (all READ-ONLY):
#   1. -8 sees -9 in pg_stat_replication (correlated by -9's REAL pod IPs),
#      state=streaming, same pid across two samples, and sent_lsn advancing
#   2. -8 physical slots are reported for VISIBILITY only -- NOT a gate: replica
#      clusters stream slotless, so retention is wal_keep_size + restore_command
#   3. -9 is in recovery (pg_is_in_recovery() = true)
#   4. -9 receive/replay LSN + replay lag are sane
#   5. the plugin-barman-cloud v0.13 sidecar is injected into EVERY -9 pod
#   6. -9's archiver verdict (separate; a rising failed_count FAILS the run)
# =============================================================================
set -euo pipefail

NAMESPACE="${NAMESPACE:-totallylegitco}"
SRC_POD="${SRC_POD:-fhi-pg-main-8-3}"
PG_CONTAINER="${PG_CONTAINER:-postgres}"
DST_CLUSTER="${DST_CLUSTER:-fhi-pg-main-9}"
SRC_CLUSTER="${SRC_CLUSTER:-fhi-pg-main-8}"
PLUGIN_IMAGE_MATCH="${PLUGIN_IMAGE_MATCH:-plugin-barman-cloud}"
PLUGIN_VERSION_MATCH="${PLUGIN_VERSION_MATCH:-:v0.13}"
MAX_REPLAY_LAG_BYTES="${MAX_REPLAY_LAG_BYTES:-104857600}"   # 100 MiB soft ceiling
# When true, sent_lsn MUST advance across the two samples (set this for the
# under-load Phase 4 re-run). Default false so a genuinely IDLE -8 -- which
# produces no new WAL -- does not fail a structurally-healthy stream.
REQUIRE_LSN_ADVANCE="${REQUIRE_LSN_ADVANCE:-false}"

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

SAMPLE_INTERVAL="${SAMPLE_INTERVAL:-8}"   # seconds between LSN samples

# --- 1. PROVE -9 is the connection actually streaming from -8 ---------------
# Correlate by -9's REAL pod IPs (not just any row / any slot). A stale row or a
# leftover -8 HA slot must NOT be able to satisfy this gate.
info "=== [1] correlate pg_stat_replication on $SRC_CLUSTER with -9's pod IPs ==="
mapfile -t DST_IPS < <(kubectl -n "$NAMESPACE" get pods \
  -l "cnpg.io/cluster=$DST_CLUSTER" -o jsonpath='{range .items[*]}{.status.podIP}{"\n"}{end}' \
  2>/dev/null | grep -v '^$')
[ "${#DST_IPS[@]}" -ge 1 ] || die "could not resolve any -9 pod IP"
info "-9 pod IPs    : ${DST_IPS[*]}"
# build a SQL IN-list of -9 IPs
IN_LIST=""
for ip in "${DST_IPS[@]}"; do IN_LIST="${IN_LIST:+$IN_LIST,}'$ip'"; done

# sample the -9 row twice and confirm the SAME connection is streaming + advancing
sample_repl() {
  psql_src "
    SELECT COALESCE(state,'')||'|'||COALESCE(pid::text,'')||'|'||
           COALESCE(sent_lsn::text,'')||'|'||COALESCE(client_addr::text,'')||'|'||
           COALESCE(application_name,'')
    FROM pg_stat_replication
    WHERE client_addr::text IN ($IN_LIST)
    ORDER BY backend_start LIMIT 1;"
}
R1="$(sample_repl)"
if [ -z "$R1" ]; then
  fail "no pg_stat_replication row on -8 whose client_addr is a -9 pod IP --"
  fail "  -9 is NOT streaming from -8 (a stale slot/row cannot mask this)."; FAILED=1
else
  ST1="$(printf '%s' "$R1" | cut -d'|' -f1)"
  PID1="$(printf '%s' "$R1" | cut -d'|' -f2)"
  SENT1="$(printf '%s' "$R1" | cut -d'|' -f3)"
  info "  match: state=$ST1 pid=$PID1 sent_lsn=$SENT1 client=$(printf '%s' "$R1" | cut -d'|' -f4)"
  if [ "$ST1" != "streaming" ]; then
    fail "-9's connection is present but state='$ST1' (not streaming)"; FAILED=1
  else
    sleep "$SAMPLE_INTERVAL"
    R2="$(sample_repl)"
    PID2="$(printf '%s' "$R2" | cut -d'|' -f2)"
    SENT2="$(printf '%s' "$R2" | cut -d'|' -f3)"
    if [ -z "$R2" ] || [ "$PID2" != "$PID1" ]; then
      fail "-9's streaming connection changed/dropped between samples (pid $PID1 -> ${PID2:-none}) -- flapping"; FAILED=1
    else
      ADV="$(psql_src "SELECT (pg_wal_lsn_diff('$SENT2','$SENT1') > 0);" 2>/dev/null || echo '?')"
      if [ "$ADV" = "t" ]; then
        pass "-9 is streaming from -8 AND sent_lsn advanced ($SENT1 -> $SENT2)"
      elif [ "$REQUIRE_LSN_ADVANCE" = "true" ]; then
        # strict (under-load) mode: no forward progress is a real failure.
        fail "-9 sent_lsn did NOT advance in ${SAMPLE_INTERVAL}s while REQUIRE_LSN_ADVANCE=true"
        fail "  -> stream is not making forward progress under load ($SENT1 -> $SENT2)."; FAILED=1
      else
        # -8 may simply be idle (no new WAL); streaming+stable pid still holds.
        warn "-9 streaming (stable pid $PID1) but sent_lsn did not advance in ${SAMPLE_INTERVAL}s"
        warn "  -> likely -8 idle. Re-run under load (REQUIRE_LSN_ADVANCE=true) to enforce progress."
        pass "-9 is the active streaming connection from -8 (pid stable)"
      fi
    fi
  fi
fi

# --- 2. slot expectation (RESOLVED: replica clusters stream SLOTLESS) -------
# CNPG 1.28 cannot bind a replica cluster to a NAMED slot on an external primary
# (primary_slot_name is a fixed/unsettable GUC; externalClusters has no slot
# field). So a slot on -8 is NOT expected and is NOT the retention mechanism --
# wal_keep_size + the archive restore_command are (see runbook Phase 0/1c). We
# therefore do NOT gate on "some physical slot is active" (a stale -8 HA slot
# would falsely satisfy it). We only report slots for visibility.
info "=== [2] -8 physical slots (informational; NOT a gate) ==="
psql_src "
  SELECT slot_name||'|active='||active||'|retained='||
         pg_size_pretty(COALESCE(pg_wal_lsn_diff(pg_current_wal_lsn(),restart_lsn),0))
  FROM pg_replication_slots WHERE slot_type='physical' ORDER BY active DESC;" \
  || warn "could not read -8 slots"
info "  (retention for the clone is wal_keep_size + archive restore_command, not a slot)"

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

# --- 6. -9 archiver (SEPARATE verdict; does NOT define streaming health) ----
# Item 7: a failing archiver must FAIL the run, and the "not archiving yet
# because standby / archive_mode=on" case must be explicit -- NOT folded into
# the replication-health line. The REAL archiving gate is Phase 5 (a fresh -9
# backup completing); this is an early smoke check.
ARCH_FAILED=0
info "=== [6] -9 pg_stat_archiver (separate verdict) ==="
ARCH="$(psql_dst "
  SELECT 'archived='||archived_count||' failed='||failed_count
       ||' last_archived_time='||COALESCE(last_archived_time::text,'(none)')
       ||' last_failed_time='||COALESCE(last_failed_time::text,'(none)')
  FROM pg_stat_archiver;")"
printf '%s\n' "$ARCH"
A_CNT="$(printf '%s' "$ARCH" | sed -n 's/archived=\([0-9]*\).*/\1/p')"
F_CNT="$(printf '%s' "$ARCH" | sed -n 's/.*failed=\([0-9]*\) .*/\1/p')"
if [ "${F_CNT:-0}" -gt 0 ] 2>/dev/null; then
  fail "-9 archiver has failed_count=$F_CNT (>0) -- REAL problem (B2 creds/checksum env/plugin)."
  fail "  Re-run to confirm it is RISING; do not depend on -9 backups until fixed."; ARCH_FAILED=1
elif [ "${A_CNT:-0}" -gt 0 ] 2>/dev/null; then
  pass "-9 archiver advancing (archived=$A_CNT, failed=0)"
else
  # archived=0 AND failed=0: explicit expected case, not a silent pass.
  info "-9 archiver idle (archived=0, failed=0): EXPECTED for a standby with"
  info "  archive_mode=on (a replica archives its own WAL only when archive_mode"
  info "  =always or after promotion). This is NOT the archiving gate --"
  info "  Phase 5 (a fresh -9 backup reaching phase=completed) is."
fi

# --- verdicts (streaming health and archiver reported SEPARATELY) ----------
echo
RC=0
if [ "$FAILED" -eq 0 ]; then
  pass "-9 REPLICATION HEALTHY (streaming from -8, in recovery, sidecar injected)."
else
  fail "-9 REPLICATION NOT HEALTHY -- resolve [FAIL] items before promotion."
  RC=1
fi
if [ "$ARCH_FAILED" -ne 0 ]; then
  fail "-9 ARCHIVER FAILING -- fix before relying on -9 PITR/backups (Phase 5 gate)."
  RC=1
fi
exit "$RC"
