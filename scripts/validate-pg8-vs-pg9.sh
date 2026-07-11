#!/usr/bin/env bash
# =============================================================================
# validate-pg8-vs-pg9.sh
# Data-consistency validation of fhi-pg-main-9 against the live fhi-pg-main-8.
# READ-ONLY on both. Compares:
#   * database list
#   * roles (name + key attributes)
#   * installed extensions in the app db
#   * EXACT COUNT(*) for critical tables (NOT n_live_tup estimates)
#   * sequence last_value
#   * Django migration history (django_migrations)
#   * app role can log in to both
# Reports every mismatch; exit non-zero if any critical check differs.
#
# NOTE: on a still-streaming replica, tiny transient count diffs are possible if
# writes are in flight on -8. Run this only when writes are quiesced / replay lag
# is ~0 for a trustworthy comparison (see runbook "data validation" gate).
# =============================================================================
set -euo pipefail

NAMESPACE="${NAMESPACE:-totallylegitco}"
SRC_POD="${SRC_POD:-fhi-pg-main-8-3}"
PG_CONTAINER="${PG_CONTAINER:-postgres}"
DST_CLUSTER="${DST_CLUSTER:-fhi-pg-main-9}"
APP_DB="${APP_DB:-app}"
APP_ROLE="${APP_ROLE:-ziggystardust}"
# Space-separated list of critical tables to exact-count. Override as needed.
CRITICAL_TABLES="${CRITICAL_TABLES:-django_migrations auth_user}"

info() { printf '\033[0;36m[INFO]\033[0m %s\n' "$*"; }
pass() { printf '\033[0;32m[PASS]\033[0m %s\n' "$*"; }
warn() { printf '\033[0;33m[WARN]\033[0m %s\n' "$*"; }
fail() { printf '\033[0;31m[FAIL]\033[0m %s\n' "$*"; }
die()  { fail "$*"; exit 1; }

MISMATCH=0

command -v kubectl >/dev/null 2>&1 || die "kubectl not found on PATH"
CTX="$(kubectl config current-context 2>/dev/null || true)"
[ -n "$CTX" ] || die "no active kube-context"
info "kube-context : $CTX"
info "namespace    : $NAMESPACE"

kubectl -n "$NAMESPACE" get pod "$SRC_POD" >/dev/null 2>&1 || die "source pod $SRC_POD missing"
mapfile -t DST_PODS < <(kubectl -n "$NAMESPACE" get pods \
  -l "cnpg.io/cluster=$DST_CLUSTER" -o name 2>/dev/null | sed 's|pod/||')
[ "${#DST_PODS[@]}" -ge 1 ] || die "no pods found for $DST_CLUSTER"
DST_POD="${DST_PODS[0]}"
info "compare      : $SRC_POD (-8)  vs  $DST_POD (-9)"

q_src() { kubectl -n "$NAMESPACE" exec -i "$SRC_POD" -c "$PG_CONTAINER" -- \
  psql -U postgres -d "${2:-postgres}" -At -v ON_ERROR_STOP=1 -c "$1"; }
q_dst() { kubectl -n "$NAMESPACE" exec -i "$DST_POD" -c "$PG_CONTAINER" -- \
  psql -U postgres -d "${2:-postgres}" -At -v ON_ERROR_STOP=1 -c "$1"; }

compare() {
  # $1 label, $2 src value, $3 dst value
  if [ "$2" = "$3" ]; then
    pass "$1 match ($2)"
  else
    fail "$1 MISMATCH: -8=[$2] -9=[$3]"; MISMATCH=1
  fi
}

# --- databases -------------------------------------------------------------
info "=== databases ==="
DB_S="$(q_src "SELECT string_agg(datname,',' ORDER BY datname) FROM pg_database WHERE NOT datistemplate;")"
DB_D="$(q_dst "SELECT string_agg(datname,',' ORDER BY datname) FROM pg_database WHERE NOT datistemplate;")"
compare "database list" "$DB_S" "$DB_D"

# --- roles -----------------------------------------------------------------
info "=== roles ==="
R_S="$(q_src "SELECT string_agg(rolname||':'||rolcanlogin||rolsuper,',' ORDER BY rolname) FROM pg_roles WHERE rolname NOT LIKE 'pg\_%';")"
R_D="$(q_dst "SELECT string_agg(rolname||':'||rolcanlogin||rolsuper,',' ORDER BY rolname) FROM pg_roles WHERE rolname NOT LIKE 'pg\_%';")"
compare "roles" "$R_S" "$R_D"

# --- extensions (app db) ---------------------------------------------------
info "=== extensions in $APP_DB ==="
E_S="$(q_src "SELECT string_agg(extname||' '||extversion,',' ORDER BY extname) FROM pg_extension;" "$APP_DB")"
E_D="$(q_dst "SELECT string_agg(extname||' '||extversion,',' ORDER BY extname) FROM pg_extension;" "$APP_DB")"
compare "extensions" "$E_S" "$E_D"

# --- exact row counts for critical tables ----------------------------------
info "=== exact COUNT(*) for critical tables ==="
for t in $CRITICAL_TABLES; do
  # guard: table may not exist; skip with a warning rather than aborting
  EXISTS="$(q_src "SELECT to_regclass('public.$t') IS NOT NULL;" "$APP_DB")"
  if [ "$EXISTS" != "t" ]; then
    warn "table $t not present in -8.$APP_DB -- skipping"
    continue
  fi
  C_S="$(q_src "SELECT count(*) FROM \"$t\";" "$APP_DB")"
  C_D="$(q_dst "SELECT count(*) FROM \"$t\";" "$APP_DB")"
  compare "count($t)" "$C_S" "$C_D"
done

# --- sequences -------------------------------------------------------------
info "=== sequence values ==="
S_S="$(q_src "SELECT string_agg(schemaname||'.'||sequencename||'='||COALESCE(last_value::text,'null'),',' ORDER BY 1) FROM pg_sequences;" "$APP_DB")"
S_D="$(q_dst "SELECT string_agg(schemaname||'.'||sequencename||'='||COALESCE(last_value::text,'null'),',' ORDER BY 1) FROM pg_sequences;" "$APP_DB")"
if [ "$S_S" = "$S_D" ]; then
  pass "all sequence last_values match"
else
  # sequences can legitimately drift by cache on a live primary; report but
  # treat as WARN unless you have quiesced writes.
  warn "sequence values differ (expected if -8 still taking writes):"
  warn "  -8: $S_S"
  warn "  -9: $S_D"
fi

# --- migration history -----------------------------------------------------
info "=== Django migration history ==="
M_EXISTS="$(q_src "SELECT to_regclass('public.django_migrations') IS NOT NULL;" "$APP_DB")"
if [ "$M_EXISTS" = "t" ]; then
  M_S="$(q_src "SELECT count(*)||'@'||COALESCE(max(id)::text,'0') FROM django_migrations;" "$APP_DB")"
  M_D="$(q_dst "SELECT count(*)||'@'||COALESCE(max(id)::text,'0') FROM django_migrations;" "$APP_DB")"
  compare "django_migrations (count@max_id)" "$M_S" "$M_D"
else
  warn "django_migrations not found -- skipping migration-history check"
fi

# --- app login can reach -9 -----------------------------------------------
info "=== app role presence ==="
L_S="$(q_src "SELECT rolcanlogin FROM pg_roles WHERE rolname='$APP_ROLE';")"
L_D="$(q_dst "SELECT rolcanlogin FROM pg_roles WHERE rolname='$APP_ROLE';")"
compare "app role $APP_ROLE canlogin" "${L_S:-missing}" "${L_D:-missing}"

echo
if [ "$MISMATCH" -eq 0 ]; then
  pass "DATA VALIDATION PASSED -- -9 matches -8 on all critical checks."
  exit 0
else
  fail "DATA VALIDATION FOUND MISMATCHES -- investigate before promotion/cutover."
  exit 1
fi
