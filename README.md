# AWS GenAI Platform

[![CI](https://github.com/gnutvz/AWS-GenAI-Platform/actions/workflows/ci.yml/badge.svg)](https://github.com/gnutvz/AWS-GenAI-Platform/actions/workflows/ci.yml)

A reference AI platform on AWS — the companion to
[AWS-GenAI-Cookbook](https://github.com/gnutvz/AWS-GenAI-Cookbook). Where the cookbook
shows each service on its own, this assembles them into one system.

The Python package is `aiplat`.

A working RAG agent platform built the way an internal platform team would build it:
AWS managed services for the parts where running your own buys nothing, open-source
components at the seams where lock-in actually hurts.

Idle cost is approximately zero. Nothing here runs a cluster, a warm vector database,
or a node pool waiting for traffic.

Wondering why a repo with one agent calls itself a platform? That is the right
question — [docs/platform.md](docs/platform.md) answers it, including the parts that
are not a platform yet.

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

**Prerequisites**

| Need | Why | Install |
|---|---|---|
| Python 3.11+ | Runs everything | [python.org](https://www.python.org/downloads/) |
| [uv](https://docs.astral.sh/uv/) | Package manager used by the Makefile | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Node 22+ | Runs the CDK CLI via `npx` | [nodejs.org](https://nodejs.org/) |
| Docker | Bundles the Lambda; builds the ingest image | [docker.com](https://docs.docker.com/get-docker/) |
| AWS credentials | Deploying and calling Bedrock | `aws configure` |

Bedrock **model access must be enabled** in your region before anything works —
Bedrock console → *Model access* → enable your chat model and
`amazon.titan-embed-text-v2`. Skipping this deploys fine and then fails at the
first question with `AccessDeniedException`.

Deploying into someone else's account? [docs/aws-requirements.md](docs/aws-requirements.md)
is a checklist to hand their platform team: services touched, quotas to check,
permissions needed, and what the security review will ask.

**1. Verify it works before touching AWS.** Everything here runs offline:

```bash
git clone https://github.com/gnutvz/AWS-GenAI-Platform.git
cd AWS-GenAI-Platform
make install
make test          # everything stubbed — no credentials needed
make lint
make licenses      # no GPL/AGPL/SSPL in the dependency tree
```

**2. Deploy.** Three stacks, idle cost approximately zero:

```bash
cd infra && npx cdk bootstrap     # once per account/region
cd .. && make deploy
```

**3. Wire up `.env`.** The deploy prints the IDs you need and then they scroll
away, so read them back from CloudFormation instead of copying by hand. `.env`
points at one tenant at a time:

```bash
make env TENANT=acme
```

Each tenant in `tenants/*.yaml` gets its own knowledge base, its own agent
function, and an IAM role scoped to exactly one knowledge base ARN — so
cross-tenant retrieval is a permission nobody holds rather than a filter someone
can forget. `tests/test_tenancy.py` enforces that against the synthesized
CloudFormation.

**4. Load a corpus and ask something:**

```bash
make image-ingest                                  # docling is too heavy for Lambda
docker run --rm -v ~/.aws:/root/.aws:ro -v "$PWD/your-docs:/data:ro" \
  -e AWS_REGION -e DOCUMENTS_BUCKET -e KNOWLEDGE_BASE_ID \
  aiplat-ingest /data --wait

make ask Q="What is the warranty period?"
```

The Function URL is IAM-authenticated, so `curl` gets a 403 — `scripts/ask.py`
SigV4-signs each request. That is deliberate: a public LLM endpoint is an
invitation to run up someone else's Bedrock bill.

**5. Or use the chat UI** — `pip install -e '.[ui]'` then:

```bash
make chat                    # http://localhost:8000
make chat CHAT_PORT=8011     # if something already holds 8000
```

Port 8000 is Chainlit's default and a popular one — a stray container or SSH
tunnel will hold it, and the browser then lands on *that* instead, which looks
like the app misbehaving. `make chat` checks first and names whatever is
squatting rather than failing obscurely.

Retrieval is shown rather than hidden: each search is an expandable step listing
the passages and their sources, and a refusal is rendered as an outcome rather
than an error. An answer that simply materialises looks like every other chatbot;
watching the agent look something up, cite it, and decline when it has nothing is
the part worth demonstrating.

Built on [Chainlit](https://chainlit.io) (Apache 2.0). The same `app/chat.py`
also deploys to Slack, Teams or an embedded widget — those are deployment
targets, not rewrites, which matters because the people who would use this are
already in Teams all day.

Run `make help` for everything else.

**Tearing down:** `make destroy`. The documents bucket and knowledge base are
`RETAIN` on purpose — an accidental `destroy` should not delete a corpus that took
an hour to index. Delete those two by hand when you actually mean it.

Measure before you tune:

```bash
python -m evals.datasets.fetch_enterprise_bench    # 5,189 docs + 84 graded questions
python -m services.ingest.ingest evals/corpus --wait
python -m evals.run --dataset evals/datasets/enterprise-bench.jsonl
```

## Evaluation

The default dataset is [EnterpriseRAG-Bench](https://github.com/onyx-dot-app/EnterpriseRAG-Bench)
(MIT, fully synthetic). It was chosen over FinanceBench and Vectara's Open RAG
Benchmark for one reason: both of those are **CC-BY-NC**, and a non-commercial
licence is a problem for a repo meant to be shown to customers.

Three numbers, reported separately because they fail independently:

| Metric | What it catches |
|---|---|
| **fact recall** | Did the answer state every expected fact? Judge-scored, so paraphrase counts |
| **refusal accuracy** | Of the 20 unanswerable questions, how many got an honest "I don't know" |
| **citation rate** | Did the answer carry `[n]` markers back to sources |

Refusal accuracy is the one to watch. An agent that scores well on answerable
questions while confidently inventing answers to unanswerable ones is worse than no
agent at all, and a single blended pass rate hides exactly that.

The fetch script downloads Confluence only (5k of 512k documents) to keep embedding
costs sane. It says so loudly on every run: **scores here are not comparable to
published benchmark results**, because retrieval over a small corpus is easier. Pass
`--sources confluence github gmail` for a harder set.

## Turning things on

Everything optional is off by default, and the code degrades honestly rather than
crashing — no knowledge base means no retrieval tool, and the agent says so.

| Want | Do |
|---|---|
| Tracing locally, free | `make trace-local` (Langfuse on `localhost:3000`) |
| Tracing and cost per call, deployed | `cdk deploy -c observability=true`, then set `OTEL_EXPORTER_OTLP_*` |
| The prebuilt Strands tool catalogue | `pip install -e '.[tools]'` — adds ~95MB, so it is not in the Lambda bundle |
| Per-team budgets, non-Bedrock models | `make gateway-local`, then `LLM_ROUTE=gateway` + `MODEL_ID=platform-default` |
| Streaming, long sessions | `make image-agent` and deploy it to AgentCore Runtime |
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
- **Benchmark scores are not portable.** The default corpus is a 5k-document subset,
  which makes retrieval easier than the published benchmark. Use the numbers to
  compare your own changes against each other, not against anyone else's results.
- **The corpus is synthetic.** EnterpriseRAG-Bench models a fictional company. It has
  realistic structure and noise, but it is not your documents — treat a good score as
  "the pipeline works", not "this will work on our data".
- **Langfuse here is v2** (Postgres only). v3 splits into web + worker and adds
  ClickHouse, Redis and S3.
- **Nothing here has been deployed to a live account yet.** CI proves the code
  lints, the tests pass against stubs, all four stacks synthesize, and the Lambda
  bundle builds — it does not prove Bedrock returns what the eval suite expects.

## Licence

MIT — see [LICENSE](LICENSE). Every dependency is permissive; no GPL, AGPL,
SSPL or BUSL in the tree, enforced on every push. Full breakdown, including the
two things worth knowing about Langfuse and MPL, in [docs/licenses.md](docs/licenses.md).
