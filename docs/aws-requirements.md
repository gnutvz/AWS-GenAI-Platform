# AWS request checklist

Hand to the account administrator. One region, no VPC, no always-on compute.

---

## 1 · What to grant

| # | Item | Value | Who does it |
|---|---|---|---|
| 1 | Region | `us-west-2` *(or `us-east-1`)* | Requester picks |
| 2 | **Bedrock model access** | See §2 — chat model **+ embedding model** | Admin, console |
| 3 | `cdk bootstrap` | Once per account + region | Admin, elevated rights |
| 4 | Deploy principal | CDK deploy role *(preferred)* or a principal that can manage the services in §4 | Admin |
| 5 | Budget alert | Recommended before first deploy | Admin |
| 6 | Cost allocation tag | Activate tag `tenant` in Billing | Admin |

> **The one that breaks things silently:** skip #2 and deployment still succeeds — then every question fails with `AccessDeniedException`. It looks like a code bug and is not one.

---

## 2 · Models to enable

Console → **Bedrock** → **Model access** → *Modify model access*.

**Chat — enable at least one:**

| Purpose | Model | Inference profile ID | Cost |
|---|---|---|---|
| Balanced *(default)* | Claude Sonnet 4.6 | `global.anthropic.claude-sonnet-4-6` | $$ |
| Newer, promo pricing | Claude Sonnet 5 | `global.anthropic.claude-sonnet-5` | $$ *(promo until 2026-08-31)* |
| Hardest reasoning | Claude Opus 4.8 | `global.anthropic.claude-opus-4-8` | $$$$ |
| Cheap / high volume | Claude Haiku 4.5 | `global.anthropic.claude-haiku-4-5` | $ |

**Embedding — required, pick one:**

| Model | Model ID | Notes |
|---|---|---|
| Titan Text Embeddings v2 *(default)* | `amazon.titan-embed-text-v2:0` | 1024 dims, multilingual, cheapest |
| Cohere Embed Multilingual v3 | `cohere.embed-multilingual-v3` | Alternative for non-English corpora |

- [ ] At least one chat model enabled
- [ ] Embedding model enabled ← **most commonly forgotten**

> Some models need a one-time use-case form. Submit early — approval is not always instant.
> Availability varies by region. Verify with §5 before committing to an ID.

---

## 3 · Quotas to check

Service Quotas console. Defaults are fine for a pilot.

| Quota | Why | Current |
|---|---|---|
| **Bedrock — Knowledge Bases per account** | **One per tenant → this is the ceiling on tenant count** | `______` |
| Bedrock — tokens/requests per minute | Throttling shows up as slow or failed answers | `______` |
| S3 Vectors — buckets and indexes | Also one per tenant | `______` |
| Lambda — concurrent executions | Shared with the rest of the account | `______` |

---

## 4 · Services used

No support ticket needed. Listed for security review.

| Service | For | Note |
|---|---|---|
| Bedrock | Inference, Knowledge Bases, Guardrails | Model access required (§2) |
| S3 | Documents, session state | SSE-S3, TLS enforced, public access blocked |
| **S3 Vectors** | Vector index, one per tenant | **Regional availability is narrower than S3** |
| Lambda | Agent runtime, one per tenant | ARM64, 1 GB, 5 min, **no VPC** |
| CloudWatch Logs | Function logs | 1-month retention |
| IAM | Per-tenant roles | Each scoped to exactly one knowledge base ARN |
| CloudFormation | Deployment via CDK | |
| ECR + SSM Parameter Store | CDK bootstrap only | Deployment assets, not data |

**Not used:** VPC, NAT Gateway, load balancer, database, EC2.

*Optional tracing stack only* (`-c observability=true`, **~tens of $/month even idle** — skip unless asked): VPC, NAT, ALB, ECS Fargate, Aurora Serverless v2, Secrets Manager.

---

## 5 · Verify before deploying

```bash
REGION=us-west-2

# Which Claude profiles exist here
aws bedrock list-inference-profiles --region $REGION \
  --query "inferenceProfileSummaries[?contains(inferenceProfileId,'claude')].inferenceProfileId" \
  --output table

# Which embedding models exist here
aws bedrock list-foundation-models --region $REGION \
  --query "modelSummaries[?contains(modelId,'embed')].modelId" --output table

# Knowledge-base quota = your tenant ceiling
aws service-quotas list-service-quotas --service-code bedrock --region $REGION \
  --query "Quotas[?contains(QuotaName,'knowledge base')].[QuotaName,Value]" --output table
```

---

## 6 · Security review answers

| Question | Answer |
|---|---|
| Does data leave the account? | No. Documents in S3, inference inside AWS, no third-party calls |
| Is the endpoint public? | No. Function URL uses IAM auth; unsigned requests get 403 |
| Encryption | SSE-S3 at rest; TLS in transit, non-TLS requests rejected |
| PII | Guardrails anonymise email, phone, name, address, card numbers both directions |
| Tenant isolation | Own knowledge base + IAM role per tenant, scoped to one ARN — enforced by `tests/test_tenancy.py` |
| Audit | CloudTrail for API activity; OpenTelemetry span per model and tool call |
| **Known gap** | **No end-user authorization within a tenant.** IAM protects the endpoint, not row-level access. Must be built before a corpus with per-user rules goes in |

---

## What to send

Two fillable files, both with placeholders:

Which files depends on who runs the deploy.

**If you deploy it yourself**, you need one thing from IT — access — and
`make env TENANT=<slug>` generates the rest from the deployed stacks:

| File | Filled by | Contains |
|---|---|---|
| [`docs/account-intake.yaml`](account-intake.yaml) | IT | Account id, region, model access, quotas, constraints |
| [`tenants/_template.yaml`](../tenants/_template.yaml) | Each department | Slug, corpus paths, metadata rules, optional prompt and model |

**If IT deploys and hands the result back** — the common case when you have no
console access — they fill in one file instead:

| File | Filled by | Contains |
|---|---|---|
| [`docs/handover.env.template`](handover.env.template) | IT, after deploying | Credentials, region, and every resource id the application needs |

Saved as `.env` in the repository root, it is the whole configuration: nothing
else to look up, and nothing to run but the application. It carries live
credentials, so it goes back through a secrets channel — `.env` and
`handover.env` are both git-ignored.

## Ticket text

> Please enable Bedrock model access in `<region>` for `<chat model>` and
> `amazon.titan-embed-text-v2`, run `cdk bootstrap` once for this account and
> region, and report the *Knowledge Bases per account* quota. No VPC or
> always-on infrastructure required.
