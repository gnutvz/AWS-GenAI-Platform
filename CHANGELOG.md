# Changelog

Notable changes to `aiplat`. Versions follow [semantic versioning](https://semver.org),
with the pre-1.0 caveat that minor versions may still break interfaces.

The version lives in `pyproject.toml` and is read at runtime as
`aiplat.__version__`, so a deployed agent can say which build it is.

## [Unreleased]

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

### Added

- `tests/test_llm.py` — covers both routes by stubbing the model providers, so
  the gateway path is tested without the `gateway` extra installed. Like
  `bedrock-agentcore`, `litellm` is optional and therefore absent from CI, which
  is why this went unnoticed.

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
