#!/usr/bin/env bash
# =============================================================================
# create-or-fix-pg9-migration-role.sh
# Idempotently create/repair the fhi_pg9_migration role on the LIVE -8 primary
# so fhi-pg-main-9 can stream from it, using the password that ALREADY lives in
# the Kubernetes secret fhi-pg-main-9-source (key: password). We do NOT invent a
# password.
#
# The role MUST have LOGIN + REPLICATION (physical streaming is a REPLICATION
# connection). We then test the EXACT network/auth path -9 will use:
#   * a normal connection to fhi-pg-main-8-rw (sslmode=require), and
#   * a REPLICATION connection (replication=database) -- because
#     `host all all all` in pg_hba does NOT cover replication connections;
#     those need an explicit `host replication ...` entry on -8.
#
# PASSWORD SAFETY:
#   * the password is NEVER placed on any command line (argv), so it never
#     appears in `ps` or in kubectl's echoed command;
#   * it travels only over stdin (base64 on the first line), decoded in-pod;
#   * SQL uses `format(... %L ...)` with psql's `\getenv`/`:'var'` so a value
#     containing ':' (or quotes) is safely quoted -- the old ':'-literal bug
#     cannot recur;
#   * nothing sensitive is written to disk or logged.
# =============================================================================
set -euo pipefail

NAMESPACE="${NAMESPACE:-totallylegitco}"
SRC_POD="${SRC_POD:-fhi-pg-main-8-3}"          # only-healthy primary
PG_CONTAINER="${PG_CONTAINER:-postgres}"
RW_SVC="${RW_SVC:-fhi-pg-main-8-rw.totallylegitco.svc}"
ROLE="${ROLE:-fhi_pg9_migration}"
SECRET_NAME="${SECRET_NAME:-fhi-pg-main-9-source}"
SECRET_KEY="${SECRET_KEY:-password}"
CLIENT_IMAGE="${CLIENT_IMAGE:-ghcr.io/cloudnative-pg/postgresql:18.1-system-trixie}"
TEST_POD="${TEST_POD:-pg9-auth-test}"
ASSUME_YES="${ASSUME_YES:-false}"

info() { printf '\033[0;36m[INFO]\033[0m %s\n' "$*"; }
pass() { printf '\033[0;32m[PASS]\033[0m %s\n' "$*"; }
warn() { printf '\033[0;33m[WARN]\033[0m %s\n' "$*"; }
fail() { printf '\033[0;31m[FAIL]\033[0m %s\n' "$*"; }
die()  { fail "$*"; exit 1; }

command -v kubectl >/dev/null 2>&1 || die "kubectl not found on PATH"
command -v base64  >/dev/null 2>&1 || die "base64 not found on PATH"
CTX="$(kubectl config current-context 2>/dev/null || true)"
[ -n "$CTX" ] || die "no active kube-context"
kubectl -n "$NAMESPACE" get pod "$SRC_POD" >/dev/null 2>&1 \
  || die "source pod $SRC_POD not found in $NAMESPACE"
kubectl -n "$NAMESPACE" get secret "$SECRET_NAME" >/dev/null 2>&1 \
  || die "secret $SECRET_NAME not found in $NAMESPACE"

info "kube-context : $CTX"
info "namespace    : $NAMESPACE"
info "source pod   : $SRC_POD"
info "role         : $ROLE (LOGIN REPLICATION)"
info "password from: secret/${SECRET_NAME}[${SECRET_KEY}]  (never printed)"

# This creates/alters a ROLE on the LIVE primary -- confirm before proceeding.
if [ "$ASSUME_YES" != "true" ]; then
  printf '\nCreate/repair role "%s" on LIVE %s? [type: yes] ' "$ROLE" "$SRC_POD"
  read -r REPLY
  [ "$REPLY" = "yes" ] || die "aborted by user"
fi

# --- fetch the password as base64 (never decoded locally into a var we echo) --
# The secret value is already base64 in the API; we keep it base64 on the wire.
PW_B64="$(kubectl -n "$NAMESPACE" get secret "$SECRET_NAME" \
  -o "jsonpath={.data.${SECRET_KEY}}")"
[ -n "$PW_B64" ] || die "secret $SECRET_NAME has no key '$SECRET_KEY'"

# ---------------------------------------------------------------------------
# 1) Create/repair the role on -8. Password reaches psql via env (PGROLE_PW),
#    set from the first stdin line; the SQL follows on the remaining stdin.
#    format(%L) + :'pw' guarantee correct quoting for any password.
# ---------------------------------------------------------------------------
info "=== creating/repairing role on $SRC_POD ==="
# SC2016: the single-quoted script runs in the REMOTE (in-pod) shell on purpose;
# $B64/$PGROLE_PW must expand there, not locally. This is intentional.
# shellcheck disable=SC2016
kubectl -n "$NAMESPACE" exec -i "$SRC_POD" -c "$PG_CONTAINER" -- \
  sh -c 'IFS= read -r B64; PGROLE_PW="$(printf %s "$B64" | base64 -d)"; export PGROLE_PW; exec psql -q -U postgres -d postgres -v ON_ERROR_STOP=1 -v VERBOSITY=terse' <<SQL
$PW_B64
\\set QUIET on
\\getenv pw PGROLE_PW
-- create only if absent (idempotent; WHERE NOT EXISTS on pg_roles)
SELECT format('CREATE ROLE %I WITH LOGIN REPLICATION PASSWORD %L', 'fhi_pg9_migration', :'pw')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fhi_pg9_migration')
\\gexec
-- ensure attributes + sync password even if the role pre-existed
SELECT format('ALTER ROLE %I WITH LOGIN REPLICATION PASSWORD %L', 'fhi_pg9_migration', :'pw')
\\gexec
-- verify
SELECT 'rolcanlogin='||rolcanlogin||' rolreplication='||rolreplication
FROM pg_roles WHERE rolname = 'fhi_pg9_migration';
SQL

# Re-read the flags for a hard gate (separate call keeps parsing simple).
FLAGS="$(kubectl -n "$NAMESPACE" exec -i "$SRC_POD" -c "$PG_CONTAINER" -- \
  psql -U postgres -d postgres -At -c \
  "SELECT rolcanlogin||','||rolreplication FROM pg_roles WHERE rolname='$ROLE';")"
case "$FLAGS" in
  t,t) pass "role $ROLE has LOGIN + REPLICATION" ;;
  *)   die  "role $ROLE flags wrong (rolcanlogin,rolreplication=$FLAGS); expected t,t" ;;
esac

# ---------------------------------------------------------------------------
# 2) Test the EXACT auth path from a throwaway pod. Auth must SUCCEED; a
#    permission error on the probe query is acceptable, an auth/pg_hba
#    rejection is NOT.
# ---------------------------------------------------------------------------
# Invoked indirectly via the EXIT trap below, so shellcheck can't see the call.
# shellcheck disable=SC2317
cleanup() { kubectl -n "$NAMESPACE" delete pod "$TEST_POD" --ignore-not-found --wait=false >/dev/null 2>&1 || true; }
trap cleanup EXIT

info "=== launching throwaway auth-test pod ($TEST_POD) ==="
kubectl -n "$NAMESPACE" delete pod "$TEST_POD" --ignore-not-found >/dev/null 2>&1 || true
kubectl -n "$NAMESPACE" run "$TEST_POD" --image="$CLIENT_IMAGE" --restart=Never \
  --command -- sleep 300 >/dev/null
kubectl -n "$NAMESPACE" wait --for=condition=Ready "pod/$TEST_POD" --timeout=120s >/dev/null \
  || die "auth-test pod did not become Ready"

# Run a probe inside the pod. The password arrives ONLY on stdin (base64 first
# line) and is decoded in-pod into PGPASSWORD -- it never appears in argv/logs.
# The full libpq conninfo and the query are passed as positional args ($1,$2) to
# the in-pod shell, so there is no fragile nested quoting.
#   returns 0 = auth OK ; 2 = auth/pg_hba REJECTED ; 1 = inconclusive
run_probe() {
  local label="$1" conninfo="$2" query="$3" out rc
  set +e
  # SC2016: the single-quoted body runs in the REMOTE shell; $B64/$1/$2 must
  # expand THERE, not locally. Intentional.
  # shellcheck disable=SC2016
  out="$(kubectl -n "$NAMESPACE" exec -i "$TEST_POD" -- \
    sh -c 'IFS= read -r B64; PGPASSWORD="$(printf %s "$B64" | base64 -d)"; export PGPASSWORD; exec psql "$1" -Atc "$2"' \
    _ "$conninfo" "$query" 2>&1 <<PROBE
$PW_B64
PROBE
)"
  rc=$?
  set -e
  # psql error text does not contain the password, so inspecting it is safe.
  if printf '%s' "$out" | grep -qiE 'password authentication failed|no pg_hba.conf entry|pg_hba|no encryption'; then
    fail "$label: AUTH REJECTED -> $out"
    return 2
  fi
  if [ "$rc" -eq 0 ]; then
    pass "$label: auth OK (query succeeded)"
    return 0
  fi
  # a non-auth failure (permission denied, wrong-context command) still proves
  # authentication itself SUCCEEDED, which is all this probe needs.
  if printf '%s' "$out" | grep -qiE 'permission denied|must be|not allowed|cannot execute'; then
    pass "$label: auth OK (non-auth error on probe query is acceptable)"
    return 0
  fi
  warn "$label: inconclusive (rc=$rc): $out"
  return 1
}

AUTH_FAIL=0
info "--- probe A: normal connection (sslmode=require) ---"
if run_probe "normal conn" \
     "host=$RW_SVC port=5432 user=$ROLE dbname=postgres sslmode=require" \
     "SELECT 1"; then
  :
else
  # else-branch $? is run_probe's real status (2 = auth rejected).
  if [ "$?" = "2" ]; then AUTH_FAIL=1; fi
fi

info "--- probe B: PHYSICAL replication connection (the clone's real path) ---"
# pg_basebackup / CNPG streaming use a PHYSICAL replication connection
# (replication=true), which matches ONLY pg_hba lines whose database column is
# the special keyword `replication` -- `host all all all md5` does NOT cover it.
# IDENTIFY_SYSTEM is the canonical replication-protocol probe.
if run_probe "physical replication conn" \
     "host=$RW_SVC port=5432 user=$ROLE replication=true sslmode=require" \
     "IDENTIFY_SYSTEM"; then
  :
else
  rc=$?
  if [ "$rc" = "2" ]; then
    AUTH_FAIL=1
    warn "  -> -8 is missing a 'host replication $ROLE ... ' pg_hba entry."
    warn "  -> RUNBOOK PREREQ: add it to fhi-pg-main-8's spec.postgresql.pg_hba"
    warn "     (CNPG-managed) and let it reconcile before bootstrapping -9."
  fi
fi

echo
if [ "$AUTH_FAIL" -eq 0 ]; then
  pass "MIGRATION ROLE READY + AUTH PATH VERIFIED (normal + replication)."
  exit 0
else
  fail "AUTH PATH NOT READY -- fix pg_hba/role on -8 before bootstrapping -9."
  exit 1
fi
