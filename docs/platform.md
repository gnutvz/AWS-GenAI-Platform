# What makes this a platform

A fair question to ask of any repo with "platform" in the name: this ships one
agent answering one kind of question. What separates it from an application with
good folder structure?

The short answer: an application is built to *do* something; a platform is built
to be *built on*. This repo is organised as the second, currently hosting one
instance of the first. That is a real architectural distinction, and it is also
less than a finished platform — both halves are documented below.

---

## The line, drawn concretely

```mermaid
flowchart TD
    subgraph workload["Workload layer — one per use case"]
        A["services/agent<br/>internal Q&A"]
        B["services/ingest<br/>corpus loading"]
        C["your next service<br/>(not written yet)"]
    end

    subgraph platform["Platform layer — shared by every workload"]
        L["aiplat/llm.py<br/>which model, which route"]
        K["aiplat/knowledge.py<br/>how retrieval works"]
        T["aiplat/telemetry.py<br/>where traces go"]
        CFG["aiplat/config.py<br/>the only reader of os.environ"]
        TEN["aiplat/tenants.py<br/>who gets their own everything"]
    end

    subgraph policy["Policy layer — owned by a different team"]
        G["Bedrock Guardrail<br/>separate stack, separate cadence"]
    end

    subgraph proof["Proof layer — evidence, not features"]
        E["evals/<br/>fact recall, refusal accuracy"]
    end

    A --> L & K & T
    B --> CFG & TEN
    C -.-> L & K & T
    L --> G
    A -.-> E
    TEN --> INFRA["infra/app.py<br/>one stack set per tenant"]
```

The test for whether something belongs in `aiplat/` rather than `services/`:
**would a second workload need it too?** Model construction, retrieval, tracing,
and configuration all pass. The agent's system prompt does not — that is specific
to one use case, so it lives in `services/agent/agent.py`.

| Layer | Who owns it | Changes | Example |
|---|---|---|---|
| Workload | The team with the use case | Weekly | A prompt, a new tool |
| Platform | Whoever runs the platform | Monthly | Swapping the vector store |
| Policy | Security / compliance | Quarterly, with audit | PII masking rules |
| Proof | Both | Every change | An eval case for a new failure mode |

Coupling these is what turns a platform back into an application. If a prompt
tweak and a guardrail change must ship together, an auditor will object — and
they will be right.

---

## Six things that make it a platform rather than an agent

**1. Application code does not name its dependencies.** `services/agent/agent.py`
never imports `BedrockModel`. It asks `aiplat` for a model and gets whatever the
platform is configured to hand out. That is the difference between "an app that
calls Bedrock" and "an app running on a platform that happens to use Bedrock."

**2. The seams are declared, not incidental.** Three decisions get revisited in
every real system — which model provider, which vector store, where traces go —
and each has exactly one file that owns it. From `docs/architecture.md`:

| Pressure | Change | Application code affected |
|---|---|---|
| Retrieval too slow | S3 Vectors → OpenSearch Serverless | None |
| Need streaming | Lambda → AgentCore Runtime | None |
| Multiple teams sharing budget | `LLM_ROUTE=gateway` | None |

That last column is the whole claim. If it read "rewrite the agent," this would
be an application.

**3. Runtime is a deployment decision.** `agent.py` knows nothing about Lambda.
The same object runs under `lambda_handler.py`, under `agentcore_app.py`, or in a
local process. Workloads that are portable across runtimes are the thing
platforms exist to produce.

**4. Safety is a separate plane.** The guardrail is its own stack with its own
lifecycle. Application teams consume it; they do not edit it.

**5. Quality is measured centrally.** `evals/` scores fact recall, refusal
accuracy, and citation rate for any workload that plugs into it. A platform that
cannot tell you whether a change made things worse is a deployment script.

**6. Tenants are an isolation boundary, not a label.** A tenant is a YAML file in
`tenants/`; `infra/app.py` turns each one into its own knowledge base, vector
index, documents bucket and agent function. The agent's role names exactly one
knowledge base ARN and `TENANT` is pinned in the function environment, so
cross-tenant retrieval is a permission nobody holds rather than a filter someone
can forget. `tests/test_tenancy.py` asserts that against synthesized
CloudFormation — including that no tenant's stack so much as mentions another's
name. Onboarding a tenant is a config file, not a CDK edit.

---

## What it is not yet

Honest gaps. Each is real work, not a footnote.

| Missing | Why it matters | Rough shape of the fix |
|---|---|---|
| **More than one workload** | One consumer cannot prove an abstraction is right. The second one always finds what the first got wrong. | Build a genuinely different service — summarisation, classification — on the same `aiplat` |
| **End-user identity** | IAM auth protects a tenant's endpoint from strangers. It does not protect user A's documents from user B *inside* that tenant. Retrieval can now be filtered per caller — `build_agent(retrieval_filters=...)` — but nothing knows who is calling, so nothing supplies them. | Identity in front (Cognito / OIDC), mapped to the filters the seam already accepts |
| **Self-service onboarding** | Adding a tenant is a config file, but it still means a commit to this repo and a deploy by whoever holds the AWS credentials. A team cannot onboard itself. | A pipeline that provisions per-tenant stacks from a merged tenant file, with the tenant directory read from outside the repo |
| **Per-tenant Bedrock spend** | Every resource carries a `tenant` tag, so S3, Lambda and storage split cleanly by cost allocation tag. Token spend does not — Bedrock bills the account, not the tag. | Per-tenant inference profiles, or virtual keys via the gateway; that is what it is for |
| **Nothing has been deployed** | Every stack synthesizes and every test passes against stubs. None of it has met a live account, so "the IAM policy has the right shape" is not "the IAM policy is sufficient". | Deploy one tenant, run the eval suite, believe the numbers over the templates |

Multi-tenancy has since been built — see point 6 above — so the remaining gaps
to a finished platform are the second workload and a first real deployment.
Until those exist, the accurate description is **reference platform**: correct
structure, several tenants, one use case. The README says exactly that, and the
phrasing is deliberate.

---

## Why build it this way before it is needed

The seams cost almost nothing now and are expensive to retrofit. `LLM_ROUTE` is
about fifteen lines; adding a gateway to a codebase where forty call sites
construct their own Bedrock client is a migration. Same for the guardrail stack
split, and for the eval harness — writing it after a quality regression means you
cannot tell whether the regression is new.

The parts deliberately *not* built early are the ones with a standing cost or a
guessable-wrong shape: self-service tooling for one team is overhead, and a
prompt registry with one prompt in it is a database nobody queries.

Multi-tenancy was the borderline case, and the reason it got built anyway is that
its boundary turned out not to be a guess. Choosing S3 Vectors made a knowledge
base per tenant cost roughly nothing at rest, so isolation could be drawn at the
IAM layer instead of as a metadata filter — and that is the version that is
expensive to retrofit, because retrofitting it means re-indexing every corpus.

---

## Deciding where a change goes

When adding something, ask in this order:

1. **Would a second workload need it?** No → `services/`. Yes → continue.
2. **Does it change based on who is calling?** Yes → it is a tenancy concern, and
   tenancy here is resolved at deploy time, not per request. Read it from
   `aiplat.tenants` or the pinned `TENANT` environment variable — never from the
   request body, which would let a caller name a tenant it does not own.
3. **Would security or compliance want to review it separately?** Yes → its own
   stack, like the guardrail.
4. **Can it be wrong in a way users would notice?** Yes → it needs an eval case
   before it ships.

Answering (1) with "maybe someday" is how platform layers accumulate abstractions
nobody uses. Wait for the second caller.
