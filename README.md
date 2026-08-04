# AWS GenAI Platform

A reference AI platform on AWS — the companion to
[AWS-GenAI-Cookbook](https://github.com/gnutvz/AWS-GenAI-Cookbook). Where the cookbook
shows each service on its own, this assembles them into one system.

The Python package is `aiplat`.

A working RAG agent platform built the way an internal platform team would build it:
AWS managed services for the parts where running your own buys nothing, open-source
components at the seams where lock-in actually hurts.

Idle cost is approximately zero. Nothing here runs a cluster, a warm vector database,
or a node pool waiting for traffic.

## The one design rule

> **Managed where operational risk is high and differentiation is low.
> Open-source where the ecosystem moves fast or where the data is the asset.**

| Layer | Choice | Why this side of the line |
|---|---|---|
| Model inference | Bedrock | Running GPUs costs the most effort and creates the least differentiation |
| Model routing | LiteLLM-compatible seam (`aiplat/llm.py`) | Multi-model routing and per-team budgets change monthly; app code should not |
| Agent framework | Strands (open source, AWS-maintained) | Runs anywhere, *and* deploys to AgentCore Runtime. No fork in the road |
| Vector store | S3 Vectors | Serverless. OpenSearch Serverless bills a standing OCU floor even at zero traffic |
| Retrieval | Bedrock Knowledge Bases | Chunking, embedding and indexing are solved problems |
| Document parsing | docling (open source) | Parsing quality decides RAG quality — this is worth owning |
| Safety | Bedrock Guardrails | Auditors want a policy artifact with a version number |
| Tracing & eval | OpenTelemetry → Langfuse (self-hosted) | Traces and eval datasets are your asset; never hand them to a proprietary format |

## Layout

```
aiplat/            shared library — config, model construction, tracing, retrieval
  config.py        the only module that reads os.environ
  llm.py           the only module that constructs a model  ← the important seam
  telemetry.py     OTLP tracing setup
  knowledge.py     KB retrieval, exposed as a Strands tool
services/
  agent/           agent.py (portable) + lambda_handler.py + agentcore_app.py
  ingest/          docling → S3 → Knowledge Base sync
evals/             dataset + scoring harness
infra/             CDK: knowledge, safety, api, observability
```

The shape matters more than the file count: `services/agent/agent.py` knows nothing
about Lambda. That is what makes the Lambda → AgentCore promotion a deployment
decision rather than a rewrite.

## Quickstart

Requires Python 3.11+, Node 22+, Docker (for Lambda bundling), and AWS credentials
with Bedrock model access enabled in your region.

```bash
uv venv && uv pip install -e '.[otel,dev]'
cp .env.example .env

cd infra
npx cdk bootstrap                 # once per account/region
npx cdk deploy --all              # knowledge + safety + api
```

Copy the stack outputs into `.env`, then load a corpus and ask a question:

```bash
uv pip install -e '.[ingest]'
python -m services.ingest.ingest ./your-docs --department engineering --wait

python -c "
import asyncio; from services.agent.agent import ask
print(asyncio.run(ask('What is the warranty period?'))['answer'])
"
```

Measure before you tune:

```bash
python -m evals.run --dataset evals/datasets/smoke.jsonl --judge
```

## Turning things on

Everything optional is off by default, and the code degrades honestly rather than
crashing — no knowledge base means no retrieval tool, and the agent says so.

| Want | Do |
|---|---|
| Tracing locally, free | `make trace-local` (Langfuse on `localhost:3000`) |
| Tracing and cost per call, deployed | `cdk deploy -c observability=true`, then set `OTEL_EXPORTER_OTLP_*` |
| The prebuilt Strands tool catalogue | `pip install -e '.[tools]'` — adds ~95MB, so it is not in the Lambda bundle |
| Per-team budgets, non-Bedrock models | Run a LiteLLM proxy, set `LLM_ROUTE=gateway` |
| Streaming, long sessions | Deploy `services/agent/agentcore_app.py` to AgentCore Runtime |
| Sub-100ms retrieval at high QPS | Swap `storage_configuration` to OpenSearch Serverless |

## What this costs

At rest, with the default three stacks: S3 storage and S3 Vectors storage. Cents.
There is no always-on compute — Lambda bills per invocation, S3 Vectors bills per
query, Bedrock bills per token.

Adding `-c observability=true` changes that. An ALB is ~$16/month whether or not it
serves a request, plus a NAT gateway and Aurora's minimum ACU while awake. That is
the only stack here with a standing bill, which is why it is opt-in.

## Known limits

- **Function URL does not stream.** Answers arrive complete, after a pause. Fix by
  moving to AgentCore Runtime, not by adding a websocket layer to Lambda.
- **Guardrails bind to Bedrock, not to the gateway.** Setting `LLM_ROUTE=gateway`
  drops native guardrail enforcement — move it to the proxy or call ApplyGuardrail
  explicitly. This trade-off is the reason `bedrock` is the default route.
- **The eval dataset is a placeholder.** Five smoke cases prove the harness runs.
  Real numbers need questions from your own corpus, with the expected keywords filled
  in — until then the pass rate means nothing.
- **Langfuse here is v2** (Postgres only). v3 splits into web + worker and adds
  ClickHouse, Redis and S3.
