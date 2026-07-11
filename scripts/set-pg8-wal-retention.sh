#!/usr/bin/env bash
# =============================================================================
# set-pg8-wal-retention.sh
# Set wal_keep_size (and bound max_slot_wal_keep_size) on the LIVE -8 primary so
# the -9 clone window is protected WITHOUT ever risking a disk-full on the only
# healthy primary. Runbook Phase 1c.
#
# FAIL-CLOSED by construction (this is the whole point of the script):
#   * wal_keep_size is COMPUTED from the observed WAL rate * clone window * margin,
#     then VALIDATED against ACTUAL free disk on -8's pgdata filesystem.
#   * if the computed size does NOT fit inside a safe budget (leaves RESERVE_MB
#     absolute headroom AND stays under MAX_KEEP_FRACTION of free disk) the script
#     REFUSES to change anything and exits non-zero. It NEVER blindly raises the
#     cap. When it refuses, retention falls back to mechanism #1 -- the archive
#     restore_command (externalCluster plugin, serverName fhi-pg-main-8).
#   * max_slot_wal_keep_size is only ever TIGHTENED toward a safe, disk-bounded
#     value; an already-safe existing bound is left alone (never raised).
#
# The -8-1 failure mode was an UNBOUNDED slot + dead archive filling pgdata. This
# script must never be able to re-create that: the safe path is "keep less WAL and
# lean on restore_command", not "raise the cap and hope".
# =============================================================================
set -euo pipefail

NAMESPACE="${NAMESPACE:-totallylegitco}"
SRC_POD="${SRC_POD:-fhi-pg-main-8-3}"          # only-healthy primary; do NOT change to -8-2
PG_CONTAINER="${PG_CONTAINER:-postgres}"
DATA_DIR="${DATA_DIR:-/var/lib/postgresql/data}"   # pgdata filesystem to measure

# --- WAL-rate sampling + clone-window sizing -------------------------------
WINDOW="${WINDOW:-300}"                 # seconds between the two LSN samples
CLONE_SECONDS="${CLONE_SECONDS:-7200}"  # worst-case clone duration; MEASURE, do not guess low
MARGIN="${MARGIN:-3}"                   # safety multiple over rate*clone_seconds

# --- fail-closed safety budget (all tunable, all conservative) -------------
# wal_keep_size may consume at most MAX_KEEP_FRACTION of FREE disk, and must leave
# at least RESERVE_MB free after it (for normal WAL churn + any archive backlog).
MAX_KEEP_FRACTION="${MAX_KEEP_FRACTION:-0.25}"
RESERVE_MB="${RESERVE_MB:-20480}"       # 20 GiB absolute headroom that stays free
# max_slot_wal_keep_size upper bound: at most this fraction of free disk, and
# still leaving RESERVE_MB. Slots may never be allowed to fill the disk.
MAX_SLOT_FRACTION="${MAX_SLOT_FRACTION:-0.5}"

ASSUME_YES="${ASSUME_YES:-false}"

info() { printf '\033[0;36m[INFO]\033[0m %s\n' "$*"; }
pass() { printf '\033[0;32m[PASS]\033[0m %s\n' "$*"; }
warn() { printf '\033[0;33m[WARN]\033[0m %s\n' "$*"; }
fail() { printf '\033[0;31m[FAIL]\033[0m %s\n' "$*"; }
die()  { fail "$*"; exit 1; }

# --- prereqs ---------------------------------------------------------------
command -v kubectl >/dev/null 2>&1 || die "kubectl not found on PATH"
command -v awk     >/dev/null 2>&1 || die "awk not found on PATH"
CTX="$(kubectl config current-context 2>/dev/null || true)"
[ -n "$CTX" ] || die "no active kube-context (kubectl config current-context is empty)"
kubectl -n "$NAMESPACE" get pod "$SRC_POD" >/dev/null 2>&1 \
  || die "source pod $SRC_POD not found in namespace $NAMESPACE"

info "kube-context : $CTX"
info "namespace    : $NAMESPACE"
info "source pod   : $SRC_POD (LIVE primary -- this script WRITES config to it)"

# read-only SQL as the postgres superuser over the local socket
psql_ro() {
  kubectl -n "$NAMESPACE" exec -i "$SRC_POD" -c "$PG_CONTAINER" -- \
    psql -U postgres -d postgres -At -v ON_ERROR_STOP=1 -c "$1"
}

# --- 1. observe the WAL generation rate ------------------------------------
info "=== sampling WAL rate over ${WINDOW}s (run this at PEAK load, not idle) ==="
L1="$(psql_ro "SELECT pg_current_wal_lsn();")"
sleep "$WINDOW"
L2="$(psql_ro "SELECT pg_current_wal_lsn();")"
BYTES="$(psql_ro "SELECT pg_wal_lsn_diff('$L2','$L1');")"
printf '%s' "$BYTES" | grep -Eq '^-?[0-9]+$' || die "could not measure WAL bytes (got '$BYTES')"
[ "$BYTES" -ge 0 ] 2>/dev/null || die "WAL diff negative ($BYTES) -- refusing to size retention"
info "WAL advanced ${BYTES} bytes in ${WINDOW}s"
if [ "$BYTES" -eq 0 ]; then
  warn "no WAL generated during the sample window -- -8 looks IDLE."
  warn "  The computed size will be ~0; re-run under real load for a meaningful bound."
fi

# --- 2. compute the desired wal_keep_size (MB), ceil ------------------------
KEEP_MB="$(awk -v b="$BYTES" -v w="$WINDOW" -v c="$CLONE_SECONDS" -v m="$MARGIN" \
  'BEGIN{ rate=b/w/1048576; need=rate*c*m; printf "%d", (need==int(need))?need:int(need)+1 }')"
info "computed wal_keep_size need: ${KEEP_MB} MB  (rate * ${CLONE_SECONDS}s * ${MARGIN}x margin)"

# --- 3. read ACTUAL free disk on -8's pgdata filesystem --------------------
DF_LINE="$(kubectl -n "$NAMESPACE" exec -i "$SRC_POD" -c "$PG_CONTAINER" -- \
  df -Pm "$DATA_DIR" 2>/dev/null | tail -1)"
[ -n "$DF_LINE" ] || die "could not read df for $DATA_DIR on $SRC_POD"
FREE_MB="$(printf '%s\n' "$DF_LINE" | awk '{print $4}')"
printf '%s' "$FREE_MB" | grep -Eq '^[0-9]+$' || die "could not parse free MB from df ('$DF_LINE')"
info "free disk on ${DATA_DIR}: ${FREE_MB} MB"

# --- 4. FAIL-CLOSED validation of wal_keep_size against the budget ---------
# Budget = MAX_KEEP_FRACTION of free disk, capped so RESERVE_MB always stays free.
FRAC_MB="$(awk -v f="$FREE_MB" -v r="$MAX_KEEP_FRACTION" 'BEGIN{ printf "%d", f*r }')"
HEADROOM_MB=$(( FREE_MB - RESERVE_MB ))          # most we could keep and still leave RESERVE
BUDGET_MB="$FRAC_MB"
[ "$HEADROOM_MB" -lt "$BUDGET_MB" ] && BUDGET_MB="$HEADROOM_MB"
info "safe wal_keep_size budget: ${BUDGET_MB} MB"
info "  (min of ${MAX_KEEP_FRACTION}*free=${FRAC_MB}MB and free-RESERVE=${HEADROOM_MB}MB)"

if [ "$BUDGET_MB" -le 0 ]; then
  fail "free disk (${FREE_MB}MB) does not even clear the RESERVE (${RESERVE_MB}MB)."
  fail "  Refusing to set wal_keep_size. Rely on the archive restore_command"
  fail "  (externalCluster plugin, serverName fhi-pg-main-8) for clone-window WAL."
  exit 1
fi
if [ "$KEEP_MB" -gt "$BUDGET_MB" ]; then
  fail "computed wal_keep_size ${KEEP_MB}MB EXCEEDS the safe budget ${BUDGET_MB}MB."
  fail "  Raising it could fill -8's pgdata -- the exact -8-1 failure mode. REFUSING."
  fail "  Options: (a) shorten the clone window (lower CLONE_SECONDS by measuring the"
  fail "  real basebackup throughput), (b) free disk on -8 first (drain stuck WAL,"
  fail "  Phase 0), or (c) rely SOLELY on the archive restore_command and leave"
  fail "  wal_keep_size unchanged. This script will NOT blindly raise the cap."
  exit 1
fi
pass "wal_keep_size ${KEEP_MB}MB fits the safe budget (${BUDGET_MB}MB) -- OK to apply"

# --- 5. compute a SAFE, disk-bounded max_slot_wal_keep_size ----------------
# Never unbounded, never above free-RESERVE. Only tighten an existing bound.
SLOT_FRAC_MB="$(awk -v f="$FREE_MB" -v r="$MAX_SLOT_FRACTION" 'BEGIN{ printf "%d", f*r }')"
SLOT_CAP_MB="$SLOT_FRAC_MB"
[ "$HEADROOM_MB" -lt "$SLOT_CAP_MB" ] && SLOT_CAP_MB="$HEADROOM_MB"
if [ "$SLOT_CAP_MB" -le 0 ]; then
  die "no safe max_slot_wal_keep_size fits free disk (${FREE_MB}MB, reserve ${RESERVE_MB}MB) -- fix disk first"
fi
# current value in MB (-1 = unbounded/unsafe). Only lower it; never raise it.
CUR_SLOT_MB="$(psql_ro "SELECT setting FROM pg_settings WHERE name='max_slot_wal_keep_size';")"
TARGET_SLOT_MB="$SLOT_CAP_MB"
if printf '%s' "$CUR_SLOT_MB" | grep -Eq '^[0-9]+$' && [ "$CUR_SLOT_MB" -ge 0 ]; then
  # already bounded: keep the SMALLER (tighter) of current vs our cap; never raise
  [ "$CUR_SLOT_MB" -lt "$TARGET_SLOT_MB" ] && TARGET_SLOT_MB="$CUR_SLOT_MB"
fi
info "max_slot_wal_keep_size: current=${CUR_SLOT_MB}MB -> target=${TARGET_SLOT_MB}MB (bounded, <= free-RESERVE)"

# --- 6. confirm, then apply ------------------------------------------------
if [ "$ASSUME_YES" != "true" ]; then
  printf '\nApply wal_keep_size=%sMB, max_slot_wal_keep_size=%sMB on LIVE %s? [type: yes] ' \
    "$KEEP_MB" "$TARGET_SLOT_MB" "$SRC_POD"
  read -r REPLY
  [ "$REPLY" = "yes" ] || die "aborted by user"
fi

info "=== applying (ALTER SYSTEM + reload) ==="
psql_ro "ALTER SYSTEM SET wal_keep_size = '${KEEP_MB}MB';"
psql_ro "ALTER SYSTEM SET max_slot_wal_keep_size = '${TARGET_SLOT_MB}MB';"
psql_ro "SELECT pg_reload_conf();" >/dev/null

# --- 7. verify the live values match what we intended ----------------------
NEW_KEEP="$(psql_ro "SELECT setting FROM pg_settings WHERE name='wal_keep_size';")"
NEW_SLOT="$(psql_ro "SELECT setting FROM pg_settings WHERE name='max_slot_wal_keep_size';")"
info "live now: wal_keep_size=${NEW_KEEP}MB max_slot_wal_keep_size=${NEW_SLOT}MB"
if [ "$NEW_KEEP" = "$KEEP_MB" ] && [ "$NEW_SLOT" = "$TARGET_SLOT_MB" ]; then
  echo
  pass "WAL RETENTION SET (fail-closed): wal_keep_size=${KEEP_MB}MB, bounded slots=${TARGET_SLOT_MB}MB."
  pass "  Belt = wal_keep_size; suspenders = archive restore_command (serverName fhi-pg-main-8)."
  exit 0
else
  die "post-apply readback mismatch (wanted keep=${KEEP_MB} slot=${TARGET_SLOT_MB}, got keep=${NEW_KEEP} slot=${NEW_SLOT})"
fi
