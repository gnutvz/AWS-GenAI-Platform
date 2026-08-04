# AWS requirements — request checklist

Hand this to whoever administers the AWS account. It lists every service the
platform touches, what has to be switched on, and what can safely stay off.

**Summary for the approver:** one region, no VPC, no always-on compute, no data
leaves the account. The only thing that must be explicitly enabled is Bedrock
model access — everything else is standard service usage that existing account
permissions already cover.

---

## 1. Region

Pick **one** region and use it for everything. Cross-region adds latency, cost
and a data-residency question nobody asked for.

| Requirement | Why |
|---|---|
| Amazon Bedrock available | Runs the models |
| **S3 Vectors available** | The vector store. Availability is narrower than S3 itself — confirm before choosing |
| Your chosen chat model available | Model availability differs by region |

Recommended: `us-west-2` or `us-east-1` — widest model and feature coverage.
If data residency requires otherwise, verify all three rows above first.

- [ ] Region chosen: `________________`
- [ ] S3 Vectors confirmed available in that region

---

## 2. Bedrock model access — the one true prerequisite

Console → **Amazon Bedrock** → **Model access** → *Modify model access*.

Without this, deployment succeeds and then every question fails with
`AccessDeniedException`. It looks like a code bug and is not one.

- [ ] Chat model enabled (the one in `MODEL_ID`, e.g. a Claude model)
- [ ] **`amazon.titan-embed-text-v2`** enabled — required for indexing; easy to miss

Some models require a one-time use-case form. Submit it early; approval is not
always instant.

---

## 3. Services used

Nothing here needs a support ticket. Listed so security review can see the full
surface at once.

| Service | Used for | Notes for review |
|---|---|---|
| **Amazon Bedrock** | Model inference, Knowledge Bases, Guardrails | Model access must be enabled (§2) |
| **Amazon S3** | Source documents, conversation state | Encrypted (SSE-S3), TLS enforced, public access blocked |
| **Amazon S3 Vectors** | Vector index, one per tenant | Newer service — confirm regional availability |
| **AWS Lambda** | Agent runtime, one function per tenant | ARM64, 1 GB, 5-minute timeout, no VPC |
| **Amazon CloudWatch Logs** | Function logs | 1-month retention |
| **AWS IAM** | Per-tenant roles | Each agent role is scoped to exactly one knowledge base ARN |
| **AWS CloudFormation** | Deployment via CDK | |
| **Amazon ECR** + **SSM Parameter Store** | CDK bootstrap only | Created once by `cdk bootstrap`; holds deployment assets, not data |

**No VPC, no NAT gateway, no load balancer, no database** in the default
deployment. Nothing runs when nobody is asking a question.

---

## 4. Optional — only if self-hosted tracing is wanted

The observability stack is opt-in (`cdk deploy -c observability=true`) and is
the **only** part with a standing cost. Skip it unless someone specifically
wants prompt-level traces inside the account; Langfuse also runs locally on a
laptop for free.

| Service | Used for |
|---|---|
| Amazon VPC + NAT Gateway | Network for the tracing stack |
| Application Load Balancer | Reaching the tracing UI |
| Amazon ECS Fargate | Runs Langfuse |
| Amazon Aurora Serverless v2 (PostgreSQL) | Langfuse storage, scales to zero |
| AWS Secrets Manager | Database and session secrets |

- [ ] Decision: self-hosted tracing **yes / no**

---

## 5. Quotas to check before scaling past a pilot

Console → **Service Quotas**. Defaults are fine for a pilot; these are the ones
that bite as tenants are added. Verify current values in the account rather than
assuming — defaults change.

| Quota | Why it matters |
|---|---|
| Bedrock — **Knowledge Bases per account** | **One per tenant.** This is the hard ceiling on tenant count — check it first |
| Bedrock — model invocation tokens/requests per minute | Throttling shows up as slow or failed answers under load |
| S3 Vectors — vector buckets and indexes per account | Also one per tenant |
| Lambda — concurrent executions per region | Shared with everything else in the account |
| CloudFormation — stacks per region | Two stacks per tenant, plus shared |

- [ ] Bedrock Knowledge Bases per account: current value `______` → enough for `______` tenants

---

## 6. Permissions for whoever deploys

CDK creates and updates CloudFormation stacks, so the deploying principal needs
to create the resource types in §3 — including **IAM roles**, which many
restricted policies exclude.

Two workable options:

1. **CDK deploy role (preferred).** `cdk bootstrap` provisions dedicated roles;
   humans and CI then only need `sts:AssumeRole` onto them. Least standing
   privilege, and the bootstrap is a one-time admin action.
2. **A deployment principal** with permission to manage CloudFormation, IAM,
   Lambda, S3, S3 Vectors, Bedrock, CloudWatch Logs, ECR and SSM.

- [ ] `cdk bootstrap` run once per account and region (needs elevated rights)
- [ ] Deploy principal decided: **CDK role / dedicated principal**

Runtime permissions are separate and already minimal: each agent function may
invoke Bedrock models, retrieve from **its own** knowledge base, apply the
guardrail, and read/write its own session bucket. Nothing else.

---

## 7. Cost

Idle cost is close to zero — Lambda bills per invocation, S3 Vectors per query
and per GB stored, Bedrock per token. There is no hourly compute.

Order of magnitude for a pilot corpus of a few thousand documents: **single-digit
dollars** to index and evaluate. Tracing on Fargate is the exception — an ALB
plus NAT gateway is roughly **tens of dollars per month even when idle**, which
is why it is opt-in.

- [ ] Budget alert created (Billing → Budgets) — recommended before first deploy
- [ ] Cost allocation tag **`tenant`** activated in Billing, so spend splits per tenant

---

## 8. Security review notes

Answers to the questions a reviewer usually asks:

- **Data never leaves the account.** Documents stay in S3; Bedrock inference runs
  within AWS. No third-party API calls.
- **Endpoint is not public.** The agent's Function URL uses IAM authentication;
  unsigned requests get a 403.
- **Encryption.** At rest via SSE-S3; in transit via TLS, and buckets reject
  non-TLS requests.
- **PII.** Bedrock Guardrails anonymise email, phone, name, address and card
  numbers in both directions.
- **Tenant isolation.** Each tenant has its own knowledge base and its own agent
  role scoped to a single knowledge base ARN — enforced by an automated test
  (`tests/test_tenancy.py`), not just by convention.
- **Audit.** All API activity is CloudTrail-visible; every model and tool call
  emits an OpenTelemetry span.

**Known gap, stated plainly:** there is no end-user authorization yet. IAM
protects the endpoint from outsiders; it does not stop one authorized user
reading another's documents *within the same tenant*. If a corpus has per-user
access rules, that has to be built before it goes in.

---

## One-line request for a ticket

> Please enable Amazon Bedrock model access for `<chat model>` and
> `amazon.titan-embed-text-v2` in `<region>`, run `cdk bootstrap` once for that
> account and region, and confirm the account's Bedrock *Knowledge Bases per
> account* quota. No VPC or always-on infrastructure is required.
