# Appeal & Chat Reliability Roadmap

Follow-ups from the July 2026 reliability hardening pass (the
`claude/appeal-chat-reliability` branch). That pass landed transport-edge
correctness, bounded waits everywhere, health-gated routing with transport
cooldowns, executor partitioning + cooperative deadlines, transactional chat
persistence, yield recovery, tool hardening, Prometheus/Sentry observability,
and WS origin + chat API auth. The items below were identified in the same
review but deliberately deferred; they are ordered by expected impact on the
remaining failure rate.

## 1. LLM token streaming end to end (`stream: true`)

The terminal fix for every silence-based failure mode. Today a completion is
one long silent HTTP request bounded by heartbeats and watchdogs; streaming
tokens through `__infer` -> the appeal/chat streams -> the client would make
progress continuously visible, let proxies see constant traffic, cut
time-to-first-token dramatically, and allow partial results to survive a
mid-generation death. Obsoletes much of the heartbeat/keepalive machinery.
Large change: touches the transport layer, both stream protocols, and both
frontends.

## 2. 429/backoff handling for DeepInfra and internal backends

`RateLimitedRemoteOpenLike` gives the paid providers per-model backoff, but
DeepInfra and the internal vLLM pool treat a 429 like any other HTTP error:
no Retry-After honor, no backoff, so a rate-limited backend keeps eating
fanout slots. Extend the rate-limiter pattern (or the new transport-cooldown
pattern) to them.

## 3. Cross-pod cooldown / health store

The missing-model and transport-failure cooldowns are per-process dicts;
every pod (and every worker process) rediscovers a dead backend on its own.
A small shared store (Redis, or a DB table with a short TTL) would make one
pod's discovery immediately effective fleet-wide. Same for the hourly health
sweep's `_health_map`.

## 4. Single transport-level retry for connect-phase failures

A connection refused / DNS failure that happens BEFORE the request body is
sent is safe to retry immediately against the same endpoint (no idempotency
concern). One fast retry would paper over transient pod restarts without
waiting for the backup-endpoint leg.

## 5. Chat turns in ModelCallAttempt

Appeal generation has per-attempt DB rows; chat has only logs + the new
Prometheus counters. Extending `ModelCallAttempt` to chat needs a schema
decision first: `for_denial` is deliberately non-nullable as the PHI
deletion guarantee, so chat rows need a nullable `for_denial` plus a
`chat` FK (CASCADE) and a CheckConstraint enforcing exactly-one-owner so
every row still has a deletion path.

## 6. Real Perplexity health check

`RemotePerplexity.model_is_ok` returns True unconditionally ("assume it's
up"), so the health sweep can never mark it down and citation calls burn
their timeout when Perplexity is broken. A tiny models/HEAD probe (or
scoring recent failure counts) would close the gap.

## 7. Client-side upload queue + in-flight retry dedupe

The chat client drops file uploads attempted while the socket is
reconnecting, and a retry clicked while the original request is still in
flight can double-send. Queue sends while disconnected; disable retry while
in flight.

## 8. `keep()` dedupe atomicity + per-call backend attribution

Two small generate_appeal correctness items: the appeal dedupe's
check-then-add isn't atomic under the executor's concurrency, and
`winning_backend_by_model` is keyed per model NAME so concurrent calls can
misattribute which backend produced a result.

## 9. 200-OK error-body detection

Some OpenAI-compatible backends return HTTP 200 with an `{"error": ...}`
body. The empty-`choices` warning added in the hardening pass surfaces
these, but they could be short-circuited and classified (and counted as
their own failure reason) before the choices parse.

## 10. Prod log level + loguru -> Sentry sink

Prod runs loguru at its default level with Sentry capturing only Django
integration events; an explicit `LOGURU_LEVEL=INFO` plus a loguru sink that
forwards ERROR records to Sentry would catch error paths that bypass the
new capture_reliability_event choke points.

## 11. Chat WebSocket rate limiting

The chat consumer accepts unlimited messages per connection; the existing
`RateLimiter` utility could bound per-session message rates to keep one
misbehaving client from monopolizing the shared internal model pool.

## 12. OCTOAI docs/.env.example cleanup

CLAUDE.md/.env.example still describe OCTOAI_TOKEN as the primary ML
config; the code has moved on. Update docs to the current backend set and
their env variables (including the new FHI_ML_TIMEOUT* / executor / cooldown
knobs from the hardening pass).
