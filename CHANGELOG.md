# Changelog

Notable changes to `aiplat`. Versions follow [semantic versioning](https://semver.org),
with the pre-1.0 caveat that minor versions may still break interfaces.

The version lives in `pyproject.toml` and is read at runtime as
`aiplat.__version__`, so a deployed agent can say which build it is.

## [Unreleased]

## [0.2.0] — 2026-08-04

Minor rather than patch: a deployment with `LLM_ROUTE=gateway` and a guardrail
configured used to start and now refuses to, and the agent function gains a
concurrency ceiling it did not have. Both are behaviour changes to a running
system, not just bug fixes.

The theme is one defect repeated. Every item below is a seam the codebase
described in prose and never connected in code, invisible because the module
holding it was an optional extra that nothing imported, or a value nobody
asserted on.

### Fixed

- **`LLM_ROUTE=gateway` ran without the configured guardrail, and said nothing.**
  Bedrock Guardrails bind to `BedrockModel`; `_gateway_model` never looked at the
  guardrail settings, so a deployment with both `GUARDRAIL_ID` and the gateway
  route enforced nothing. The cost of the gateway was documented in the module
  docstring and nowhere in the code.

  This is worse than having no guardrail: the guardrail is the compliance
  artifact — a versioned policy in its own stack, built to be shown to an
  auditor — and it passed review while never being called.

  The combination is now refused at startup. Operators who enforce in the proxy
  instead set `GATEWAY_ALLOW_UNGUARDED=true`, which is honoured but logged at
  WARNING on every model construction. The flag fails closed: anything other
  than an explicit yes is a no.

- **No retry policy on any AWS client.** Bedrock throttles on tokens per minute,
  so rate limiting is the steady state of a busy tenant, not an incident.
  boto3's default — two attempts, no client-side pacing — turned a quota the
  account was always going to reach into an error the user sees.

  `aiplat/aws.py` now owns the policy: `adaptive` retries, because the failure
  is rate rather than chance, plus connect and read timeouts chosen to fit
  inside the Lambda timeout. Applied to model invocation (`boto_client_config`
  on `BedrockModel`), retrieval, and both ingest clients.

- **The agent function had no concurrency cap.** The Function URL is
  IAM-authenticated, which answers who may call but not how often. An authorised
  client in a retry loop scaled straight to the account limit, and Bedrock bills
  every token of it — a failure that looks like normal traffic until the invoice
  arrives.

  `reserved_concurrent_executions` now defaults to 10 per tenant, overridable
  with `cdk deploy -c agent_concurrency=N`. Reserved concurrency is drawn from
  the shared account pool, so the default does not scale to hundreds of tenants
  unchanged; the comment in `api_stack.py` says so and names the two ways out.

### Added

- `aiplat/aws.py` — retry and timeout policy for every AWS call.
- `tests/test_llm.py` — covers both routes by stubbing the model providers, so
  the gateway path is tested without the `gateway` extra installed. Like
  `bedrock-agentcore`, `litellm` is optional and therefore absent from CI, which
  is why this went unnoticed.
- `tests/test_aws.py` — pins the retry policy where it is applied, not only
  where it is defined.
- `tests/test_contract.py` — pins the response shape returned by `agent.ask()`
  and passed through by the Lambda handler. Any UI can front this platform, so
  the payload is the one thing every client agrees on, and it previously existed
  only as a dict literal. Renaming a key was a silent break: nothing failed to
  build, and clients surfaced it as an empty answer pane rather than an error.
  Adding a field stays free; removing or renaming one now has to argue with a
  test.

### Changed

- `services/ingest/ingest.py` builds its S3 client once instead of once per
  document. On the 5,189-document benchmark corpus that was 5,189 client
  constructions.
- `tests/test_services.py` stubbed `ask` with a `tenant` parameter the real
  function has never accepted — the same wrong mental model that shipped the
  AgentCore bug in 0.1.1. A stub looser than reality accepts calls the real
  function would reject.

## [0.1.1] — 2026-08-04

### Fixed

- **AgentCore entrypoint took its tenant from the caller's payload.**
  `services/agent/agentcore_app.py` called `build_agent(tenant=payload["tenant"])`,
  which both contradicted the platform's isolation rule — tenancy is resolved at
  deploy time, never per request — and raised `TypeError`, since `build_agent`
  has never accepted that argument. The file is an optional extra that nothing
  imported, so neither CI nor the test suite could see it.

  Nothing was deployed on this path, so there is no exposure to remediate. But
  the module's own docstring calls it "the production path", which made it the
  worst place to leave both defects sitting.

### Added

- `tests/test_agentcore.py` — pins the entrypoint contract by stubbing the
  `bedrock-agentcore` SDK rather than skipping when the extra is absent, so the
  check runs in CI, where the extra is not installed.
- `aiplat.__version__`, resolved from package metadata.
- This changelog.

### Changed

- `docs/platform.md` no longer describes multi-tenancy as unbuilt. Per-tenant
  knowledge bases, buckets, vector indexes and IAM boundaries have been in place
  and enforced by `tests/test_tenancy.py`; the document had not caught up. The
  "what it is not yet" table now lists what is actually still missing, and
  tenant isolation is stated as a property of the platform rather than a gap.

## [0.1.0]

Initial reference platform: Strands agent on Bedrock, S3 Vectors retrieval via
Bedrock Knowledge Bases, docling ingest with a metadata gate, per-tenant CDK
stacks, guardrail and observability stacks, eval harness, and a Chainlit chat UI.
