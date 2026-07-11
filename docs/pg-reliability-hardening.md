# CNPG reliability hardening — root cause & prevention

Root-cause writeup of the `fhi-pg-main-8` outage + the concrete reliability
changes baked into `fhi-pg-main-9`. Grounded in `notes/incident-evidence.md`.
Scope this round is **reliability/availability, not security** — auth is left
unchanged deliberately (trusted all-local-network).

## What actually happened

Two *independent* faults, both silent, compounding:

### Fault A — DB availability was coupled to the backup plugin

A botched barman-cloud plugin upgrade (**v0.9 → v0.13**) left the plugin broken,
and because the CNPG operator reconciles the cluster *through* the plugin, the
broken plugin took **Postgres availability** down with it:

1. **Service selected zero pods.** The `barman-cloud` Service in `cnpg-system`
   required all three labels `app=barman-cloud` **and**
   `app.kubernetes.io/instance=barman-cloud` **and**
   `app.kubernetes.io/name=plugin-barman-cloud`. The new v0.13 pod carries only
   `app=barman-cloud`; the old v0.9 pod carries only the two
   `app.kubernetes.io/*` labels. **No pod matched all three** → empty
   EndpointSlice → the operator's gRPC/mTLS call to the plugin had **no
   backend** ("connection Unavailable").
2. **Old plugin held the leader lease.** The v0.9 deployment still owned the
   leader lease, so the v0.13 deployment stayed not-ready — nothing could take
   over.
3. **Cert/CA mTLS failure.** On top of the empty endpoint, the plugin's serving
   cert chain was invalid: `x509: certificate signed by unknown authority …
   parent certificate cannot sign this kind of certificate` — a CA cert issued
   without `isCA: true` / cert-sign keyUsage (classic cert-manager Issuer/CA
   misconfig after a rotation; a fresh `combined-tls-secret-2` appeared ~41h
   before the incident).

Because plugin discovery failed, **reconciliation stalled**, the cluster could
not complete/​maintain failover, and it dropped to **`readyInstances: 1` of 3** —
a production outage. Sentry surfaced app-level DB errors, but there was **no
infra-level "cluster degraded / plugin down" alert**.

### Fault B — WAL archiving dead for months (no PITR, bloated pgdata)

Separately (though rooted in the same plugin/cert break), WAL archiving had
**never succeeded on timeline 7**:

- `pg_stat_archiver`: `archived_count=0`, `failed_count≈247116`,
  `last_archived_wal` empty, `last_failed_wal=00000007.history`.
- Last successful **base** backup: `2026-03-30`; failing daily since.

Two consequences:

- **No usable backup / no PITR.** A restore would get data only as of
  2026-03-30, with no WAL to roll forward.
- **pgdata bloat.** Postgres archives strictly in order; one stuck item
  (`00000007.history`, plus a missing `pg_wal/archive_status` dir on the pod)
  wedged the **entire** archive queue, so unarchivable WAL accumulated — the
  50Gi *request* hides ~229GB actual usage. Retained-but-unarchivable WAL is
  what filled and killed `fhi-pg-main-8-1`.

**Lesson:** a backup/plugin problem must **never** be able to take Postgres down,
block failover, or silently fill the disk — and none of it should be invisible.

---

## Prevention — what `fhi-pg-main-9` does differently

### 1. Decouple availability from the plugin
The DB's liveness must not depend on the plugin being reachable. Concretely:
- Keep the **isolation-check liveness probe** (`spec.probes.liveness.isolationCheck`)
  so fencing/failover decisions are made by the DB's own health, not the plugin.
- Treat the plugin as **best-effort for archiving**, and **alert** when it is
  unreachable rather than letting it wedge reconciliation (see §6).
- Operationally: never uninstall/replace the plugin Helm release without first
  transferring ownership of the shared Service/cert resources (the exact move
  that broke `-8`).

### 2. Pin the plugin with a version-INDEPENDENT Service selector
The outage was a **label/selector mismatch across versions**. Durable fix:
- Select the plugin Service on a **single stable label** the deployment always
  carries (e.g. `app: barman-cloud`), never on version-coupled
  `app.kubernetes.io/*` labels that differ between v0.9 and v0.13.
- **Pin one plugin version** (single deployment, single leader) — no two
  versions co-resident fighting over the leader lease.
- This selector + version pin lives in the **colo-scripts** repo (the plugin
  install is not in this app repo); track it as declarative IaC, not a live
  hand-patch (a Helm reconcile can otherwise restore the broken selector).

### 3. Correctly-issued serving-cert CA
- The plugin's serving-cert **CA** must be a real CA: cert-manager `Certificate`
  with `isCA: true` and `usages: [cert sign, crl sign, …]`, and the leaf issued
  by *that* CA. Validate the chain (`openssl verify`) after any rotation.
- **Monitor cert expiry** and alert well before expiry.

### 4. Replication slots WITH a retention bound (and NO no-op slot)
- `fhi-pg-main-9` enables HA replication slots
  (`spec.replicationSlots.highAvailability.enabled: true`) for its own
  primary→replica streaming.
- **Crucially**, `max_slot_wal_keep_size` is set (`16GB` on `-9`; recommend a
  bounded value on `-8` too) so a **stuck/inactive slot can never fill the
  disk** — the direct antidote to the `-8-1` failure. `max_slot_wal_keep_size`
  provides the ceiling.
- **We deliberately do NOT create a physical slot on `-8` for the clone.** CNPG
  1.28 cannot make a replica cluster consume a named slot on an external primary
  (`primary_slot_name`/`primary_conninfo` are fixed, user-unsettable GUCs — see
  `pkg/postgres/configuration.go`; the replica-cluster docs stream slotless).
  Such a slot would retain WAL nothing consumes → disk pressure on the only
  healthy primary with zero protection. The clone's WAL retention is instead a
  **computed `wal_keep_size` on `-8`** (belt, sized from live WAL rate) plus the
  **`restore_command` fallback** to `-8`'s repaired object-store archive (wired
  via `externalClusters[].plugin`, hard-gated on Phase 0). Runbook Phase 0/1c.

### 5. Replication timeouts that don't self-inflict failover
- The draft used `wal_sender_timeout=wal_receiver_timeout=5s`. Under load that
  makes each side declare the peer dead → **replication flap → spurious
  unsupervised failover**. `-9` uses **`60s` for both** — a reliability fix, not
  a security one.
- `-9` runs **`primaryUpdateStrategy: supervised`** through the cutover window so
  the operator won't auto-promote a replica mid-flap on local-path storage
  (revert to `unsupervised` for steady state afterward).

### 6. Alerting (the whole incident was invisible)
Add infra alerts (wired via the PodMonitor — `-9` sets
`monitoring.enablePodMonitor: true`; consumed by Plan 02):

| Alert | Condition | Catches |
|---|---|---|
| **Cluster degraded** | `readyInstances < instances` (or phase ≠ healthy) for > N min | Fault A (the outage) |
| **Plugin endpoints zero** | `barman-cloud` Service EndpointSlice has **0** ready endpoints | the selector/leader-lease break |
| **Archiver failing** | `pg_stat_archiver.failed_count` rising, **or** `last_archived_time` stale > 1h | Fault B (dead archiving) |
| **Backup age** | age(last successful backup) > 26h (daily schedule) | silent backup failure |
| **Cert expiry** | plugin serving-cert / CA expiring < 14d | pre-empts the mTLS break |
| **Slot retention** | any slot's retained WAL approaching `max_slot_wal_keep_size` / disk | pgdata bloat / `-8-1` mode |

---

## Should we bump the CNPG operator / plugin version?

> **Current live state (verified on-cluster):** `-8-2` and `-8-3` both run sidecar
> **`v0.9.0`** — `-8` was rolled back to v0.9 to recover from the outage, and
> there is no longer an `-8-1` pod (it died disk-full). **Decision: move to
> `v0.13.0`** for `-9`. This section is therefore not hypothetical — we ARE
> re-attempting the v0.9→v0.13 jump that caused the incident, so §2 (selector) and
> §3 (CA) are hard prerequisites, not advice.

**Short answer: a version bump alone is NOT the fix — the durable fixes are the
selector, the version *pin*, and the CA (§2/§3). But yes, converging to a single,
recent, pinned plugin version materially reduces risk.**

- **Operator:** stay on **`v1.28.0`** for this migration. Every field in
  `fhi-pg-main-9`'s manifests was verified offline against the `v1.28.0` CRD; a
  jump to a newer *minor* (1.29/1.30) risks CRD-shape drift and is out of scope
  for this manifest set. Apply operator patch releases within 1.28.x normally.
- **Plugin:** the concrete reliability win is to **eliminate the v0.9/v0.13
  co-residency** — run exactly one version. Pin the barman-cloud plugin to a
  **single, immutable image digest** at the **latest `v0.13.x` patch** (and
  evaluate the next minor's release notes specifically for a *version-independent
  Service selector* fix before adopting it). Whatever the number, the pin +
  consistent selector is what prevents recurrence, not the number itself.
- **Where it's applied:** the operator/plugin install lives in the **separate
  `colo-scripts` repo**, not this one. This PR pins the *sidecar image
  expectation* to `plugin-barman-cloud:v0.13.0` in `check-pg9-replication.sh` so
  drift is detected; the actual install-version pin, the Service-selector fix,
  and the CA reissue are colo-scripts changes.

## Offline-verification caveats (confirm live)

These behaviours are flagged inline in `k8s/fhi-pg-main-9-cluster.yaml` and the
runbook's VERIFY-LIVE checklist:

1. **RESOLVED (was: named slot on `-8`).** Verified offline against the CNPG
   1.28 source (`primary_slot_name` is a fixed, user-unsettable GUC), CRD
   (`externalClusters` has no slot field), and the replica-cluster docs (stream
   slotless; WAL comes back via `restore_command`). No named slot is possible or
   created — retention is computed `wal_keep_size` + archive `restore_command`.
   No live check required.
2. **Managed roles / superuser** reconciliation timing on a **read-only
   replica** (deferred until promotion) — confirm live.
3. **`archive_mode on` vs `always`** for a replica archiving its own WAL before
   promotion — confirm live; the real archiving gate is a completed `-9` backup
   (runbook Phase 5).
