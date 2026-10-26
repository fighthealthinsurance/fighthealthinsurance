# Phoenix

[Arize Phoenix](https://github.com/Arize-ai/phoenix) stores the traces and
results of our letter evals: which judge saw which letter, under which prompt
and rubric, and what it said. Eval Jobs run in this cluster and send their
traces to it.

`phoenix.yaml` holds the `fhi-eval` namespace, one Phoenix pod and an internal
Service. Phoenix keeps its data in a `phoenix` database on the app's Postgres,
logs people in with its own accounts, and makes no outbound calls of its own:
telemetry, external resources and the built-in agent are off. The image is
pinned by digest and runs as a non-root user with no capabilities.

Setup for the FHI cluster (database, secrets, first sign-in) is in the
internal runbook.

## Sending traces

From a Job in the cluster, with `arize-phoenix-otel`:

```sh
PHOENIX_COLLECTOR_ENDPOINT=http://phoenix.fhi-eval.svc:6006
PHOENIX_API_KEY=<a Phoenix API key>
```

`register(project_name=...)` then sends OTLP over HTTP to `/v1/traces`; gRPC
is on port 4317. Projects are one per frozen eval set. Answer keys never go to
Phoenix.

## Upgrading

Change the image digest in `phoenix.yaml`. The Deployment uses `Recreate`, so
the old pod stops before the new one migrates the schema.
