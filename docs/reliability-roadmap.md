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

## 5. Chat turns in the database

Landed as its own table rather than in `ModelCallAttempt`: each chat turn
that reaches the models writes one `ChatTurn` row (chat/turn_record.py),
with every call's model, pass, status, time and score in a `calls` list.
Unlike `ModelCallAttempt` it keeps no response text at all, and its
non-nullable `chat` FK (CASCADE) gives it the same deletion path as the
chat. If aggregating the `calls` JSON on the usage dashboard gets slow,
the calls can move to a child table.

## 6. Real Perplexity health check

Landed as health recorded by inference rather than a probe.
`RemotePerplexity.model_is_ok` now returns `is_available()`, which is down
while every endpoint pair is marked missing, refused (key or account) or
cooling down after transport failures. `health_checked_live` is True, so the
router reads that live instead of an hourly copy, and it also skips
Perplexity while the provider is paused for credit or quota
(`ml/spend.py`). `MLRouter._citation_backend` never fails open, so the
citation helpers fall back to the supplemental sources while Perplexity is
down. Still open: there is no proactive models/HEAD probe, so Perplexity is
only marked down after calls fail (one refusal or missing-model answer, or
three transport failures within a minute), and those first failing citation
calls in each process still pay their timeout.

## 7. Client-side upload queue + in-flight retry dedupe

The chat client drops file uploads attempted while the socket is
reconnecting, and a retry clicked while the original request is still in
flight can double-send. Queue sends while disconnected; disable retry while
in flight.

## 8. `keep()` dedupe atomicity

One small generate_appeal correctness item: the appeal dedupe's
check-then-add isn't atomic under the executor's concurrency.

(The per-call backend attribution half of this item landed: `get_model_result`
returns the accepting backend's label with its futures instead of writing a
map keyed by model name, and `ModelCallAttempt.backend` carries
`RemoteModelLike.backend_descriptor()` -- class, wire model and endpoint host
-- rather than `str(model)`, which had become the registry name and so a copy
of `model_name`.)

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
