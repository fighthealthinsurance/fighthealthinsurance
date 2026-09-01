# Appeal intake journey: Temporal from the first screen

Status: DESIGN for review (Melanie + Holden). Revises the scope of the
tier 1 (#963) and tier 2 journey work: the durable journey should begin
the moment a person starts submitting information, not after a completed
denial exists.

## Shape: two workflows, deliberately separate

**IntakeJourneyWorkflow** (new, long-running, signal-driven)
- Starts when the user reaches the first denial-submission screen.
- Workflow id: `intake-{session_uuid}` — an anonymous session id minted
  client-side, because the first screen predates knowing any email.
- Receives a signal per meaningful step; each signal carries opaque ids
  only (session uuid; denial uuid once one exists; hashed email once
  known). All real content is written to Django exactly as today — the
  workflow tracks STATE, never data. The payload codec encrypts even the
  ids at rest in Temporal.
- Timers drive abandonment behavior: an optional nudge after N hours
  ("you started an appeal — want help finishing?") and a terminal
  cleanup/close at M days. N, M, and whether nudges exist at all are
  product decisions (open questions below).
- A workflow query answers "where did this session get to," giving the
  frontend durable resume-where-you-left-off for free.
- On the form-completed signal it starts **GenerateAppealWorkflow as a
  child workflow**. This deletes the dispatch-durability gap outright:
  intent to generate is held by a running workflow from screen one, so
  no crash window between "saved" and "dispatched" exists, and the
  previously proposed reconciliation sweep is unnecessary (at most a
  later safety net).

**GenerateAppealWorkflow** (exists — all tier 1/2 hardening stands)
- Unchanged in shape: short-lived, precheck -> generate with durable-row
  postcondition. It remains independently startable (management command,
  future surfaces) — the child-workflow path is an additional caller,
  not a replacement.

Separate workflows is the better practice here, not an implementation
convenience: intake is a long-lived state machine measured in days;
generation is a minutes-long task with its own retry policy, budget and
queue. Coupling them would force one workflow to carry both lifecycles
and make versioning each independently impossible.

## Interaction with the interactive (websocket) flow

The streaming generation the user watches stays exactly as it is,
in-process. The intake workflow OBSERVES the funnel via signals; it does
not sit in the request path. If the interactive flow already produced
enough drafts, the child generation workflow's precheck ends it as an
idempotent no-op — the two paths converge on the same durable-rows
truth established in tier 1.

## Temporal practices applied

- Signals for external events; no polling anywhere.
- Child workflow for generation: parent-child linkage in the UI, and
  the child keeps its own task queue (`fhi-appeals`) and retry policy.
- Continue-as-new if an intake run's history ever grows large (many
  signals); expected sizes make this a guard, not a need.
- Worker versioning (build ids) adopted alongside this work so intake
  workflows — which live for days — survive deploys without per-change
  patched() bookkeeping.
- ids-only payloads enforced by the existing contract test; the new
  signal payload dataclasses join that test.
- SDK metrics wired before rollout; schedule-to-start latency on both
  queues is the saturation alarm.

## What changes in tier 1 / tier 2

- Tier 1 (#963): no changes. It ships the child workflow this design
  composes.
- Tier 2: no structural changes; this document rides with it for review.
  The previously listed "reconciliation sweep" follow-up is REPLACED by
  this design.
- New (tier 3) PR once this design is approved: the intake workflow,
  signal dataclasses + contract-test additions, frontend signal calls,
  resume query endpoint, and the abandonment timer skeleton with nudges
  stubbed off until the product decisions land.

## Open product decisions (blockers for tier 3, not for tiers 1/2)

1. Nudges: do we contact people who abandon mid-form at all? Channel?
2. Abandonment windows: nudge after N hours; close the journey at M days.
3. Session linking: when an anonymous session later identifies (email
   entered), the workflow gains the hashed email by signal — confirm we
   are comfortable associating pre-identification steps with the
   identified journey.
4. Whether staff should see open intake journeys in the admin dashboard
   (funnel visibility) in v1 or later.
