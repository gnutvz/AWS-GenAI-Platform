# Tracing

The agent emits OpenTelemetry spans for every model call and tool invocation.
Two environment variables decide where they go, and nothing else in the codebase
knows the difference:

```
OTEL_EXPORTER_OTLP_ENDPOINT=<collector>
OTEL_EXPORTER_OTLP_HEADERS=<auth, if the collector wants it>
```

Unset the endpoint and the platform runs untraced. That is the default, and it
is not a degraded mode — `aiplat/telemetry.py` returns `False` and everything
else carries on.

## Why there is no tracing stack in this repo

There used to be one: `ObservabilityStack` deployed Langfuse v2 to Fargate. It
was removed in 0.9.3, and the reasoning is worth keeping because it applies to
anything else someone is tempted to add here.

Langfuse v2 was a single container plus Postgres, which is a reasonable thing to
express in CDK. v3 and v4 are not: the supported release is two containers over
Postgres, **ClickHouse**, Redis and S3. AWS has no managed ClickHouse, so a
faithful CDK version means running a stateful database on ECS-on-EC2 with EBS —
several hundred lines of infrastructure, roughly 4× the standing cost, for a
component whose architecture has now changed twice in two majors.

The deeper problem is that the stack was written once and then silently went two
majors out of date, because nothing in the platform depends on it and nobody
deploys it. A hand-rolled CDK translation of someone else's evolving deployment
is a liability that only announces itself when you need it.

So the platform stays OTLP-generic and the collector is a config decision. The
three options below are all reachable from the same two variables.

## Option 1 — local, free

```
make trace-local     # http://localhost:3000
make trace-stop      # add WIPE=1 to delete stored traces
```

`docker-compose.yml` runs the upstream Langfuse v4 stack: web, worker,
Postgres, ClickHouse, Redis and MinIO. It wants roughly 4GB of RAM.

The project, user and API keys are provisioned on first boot, so there is
nothing to click through — `make trace-local` prints the exact two lines to
paste into `.env`.

This is the right option for developing prompts and reading what retrieval
actually returned. It is not a place to keep production traces.

## Option 2 — AWS-native, no new infrastructure

Point the same variables at an ADOT collector and traces land in CloudWatch and
X-Ray, which the account already has. Nothing to deploy, nothing to patch, and
the retention and access model is whatever the account already enforces.

The trade-off is real: X-Ray shows you latency, errors and a call graph. It does
not show you the prompt, the completion, or cost per call, which is the thing
Langfuse is actually for.

## Option 3 — Langfuse, hosted by someone who maintains it

Either Langfuse Cloud, or self-hosted from the [official Helm
chart](https://langfuse.com/self-hosting) on EKS. Both are maintained against
the current release; neither is a translation of it that goes stale in this
repo.

Set:

```
OTEL_EXPORTER_OTLP_ENDPOINT=https://<host>/api/public/otel
OTEL_EXPORTER_OTLP_HEADERS=Authorization=Basic <base64 of publickey:secretkey>
```

## Setting the credentials on a deployed function

`infra/app.py` reads `OTEL_EXPORTER_OTLP_ENDPOINT` from the deploy environment
and passes it through, but it never passes the headers, because stack parameters
are readable by anyone who can call `describe-stacks`. Set them on the function
afterwards:

```
aws lambda update-function-configuration \
  --function-name AiPlat-Api-<tenant>-Agent \
  --environment "Variables={OTEL_EXPORTER_OTLP_HEADERS=Authorization=Basic <...>}"
```

Or read them from Secrets Manager at startup, which is the better answer once
more than one person is doing this.

## What the spans carry

`trace_attributes()` in `aiplat/telemetry.py` stamps `service.name` and
`llm.route` on every span, plus the tenant. The tenant comes from the process
environment and never from the request — a caller must not be able to label
itself as someone else, or per-tenant cost attribution becomes fiction.

## A note on Langfuse v4 read APIs

Only relevant if you query Langfuse rather than just write to it. v4 runs in
`events_only` mode and has removed the v3 `GET /api/public/traces` endpoint; the
replacement is `GET /api/public/v2/observations?fromStartTime=…&toStartTime=…`.
Writing is unaffected — this platform speaks OTLP, which is the supported
ingestion path in v4 and does not use the deprecated Langfuse SDK batch API.
