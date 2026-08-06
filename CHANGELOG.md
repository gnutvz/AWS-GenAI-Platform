# Changelog

Notable changes to `aiplat`. Versions follow [semantic versioning](https://semver.org),
with the pre-1.0 caveat that minor versions may still break interfaces.

The version lives in `pyproject.toml` and is read at runtime as
`aiplat.__version__`, so a deployed agent can say which build it is.

## [Unreleased]

## [0.9.3] — 2026-08-06

### Changed

- **Local tracing moved from Langfuse v2 to v4, and was booted rather than
  assumed.** v2 is two majors behind and receives no patches — not even the
  security ones v3 still gets. v4 splits into web + worker and requires
  ClickHouse, Redis and S3-compatible storage on top of Postgres, so
  `docker-compose.yml` grew from two services to six.

  Four defects surfaced only by actually running it, each of which fails in a
  way that does not look like its cause:

  - Upstream's placeholder `ENCRYPTION_KEY` is sixty-four zeros. Unquoted, YAML
    reads it as the integer `0`, the container receives `"0"`, and both
    containers crash reporting the key is too short — which reads as a wrong
    key rather than wrong quoting.
  - `LANGFUSE_INIT_USER_EMAIL=dev@localhost` fails Langfuse's email validation,
    which has no TLD to match. The web container then returns 500 for every
    request including its own health endpoint.
  - Both images bind only the container's own IP unless `HOSTNAME=0.0.0.0` is
    set. The published port works, so the UI serves fine while the healthcheck
    is refused over loopback and the container never leaves `starting`.
  - `localhost` inside the containers resolves to `::1`, and there is no IPv6
    listener, so healthchecks must use `127.0.0.1`.

  The worker now has a healthcheck. Without one `docker compose up --wait`
  reports success while it crash-loops, and since the web container still
  accepts spans and returns 200, the failure presents as traces that silently
  never appear.

  `tests/test_compose.py` asserts all of the above without Docker, and each
  assertion was checked against the mutation it exists to catch.

- `make trace-local` now waits for health and prints the ready-to-paste config.
  The project, user and API keys are provisioned on first boot, so there is no
  longer a step where you click through a UI to copy keys out of it. Added
  `make trace-stop` (`WIPE=1` to drop stored traces).

### Removed

- **The self-hosted Langfuse CDK stack.** `ObservabilityStack` deployed v2 to
  Fargate. Upgrading it in place means running ClickHouse — which AWS offers no
  managed version of — as a stateful service on ECS-on-EC2 with EBS: several
  hundred lines of infrastructure and roughly 4× the standing cost, for a
  component whose architecture has changed twice in two majors.

  The stack had already gone two majors stale without anyone noticing, precisely
  because nothing in the platform depends on it. `aiplat/telemetry.py` speaks
  OTLP and does not know what is on the other end, so where spans land is a
  config decision. `docs/tracing.md` documents the three real options — local
  compose, ADOT to CloudWatch/X-Ray with no new infrastructure, or Langfuse
  Cloud/Helm — and records this reasoning so the stack does not get rebuilt.

  Removes the only standing bill in the repo.

### Fixed

- `docs/licenses.md` listed neither MinIO (AGPL-3.0) nor Redis 7 (RSALv2/SSPLv1),
  both of which the tracing stack now pulls in. Neither would be acceptable in
  the shipped tree; both are fine here because the compose file is a developer's
  laptop and nothing deploys it. That distinction is now stated rather than left
  implicit.

## [0.9.2] — 2026-08-06

### Changed

- **The setup guide was written for the wrong reader.** It walked whoever
  received it through cloning the repository and running `make deploy` — which
  assumes they are the operator. In the case it was built for they are not: IT
  creates the AWS resources, hands back one file, and the requester runs
  everything locally with no console access and nothing deployed.

  Rewritten as a console walkthrough: model access, two buckets, the Knowledge
  Base, the guardrail, an IAM policy that can call and read but not create, and
  the config block to fill in. No repository, no CDK, no deploy.

  The Knowledge Base settings are given exactly, because several of them —
  embedding model, dimensions, hierarchical chunking at 1500/300 with 60 overlap
  — cannot be changed afterwards without re-indexing every document.

- `docs/handover.env.template` now matches that flow. It previously listed
  CloudFormation outputs that only exist if someone ran the deploy, including an
  `AGENT_FUNCTION_URL` this path never produces.

## [0.9.1] — 2026-08-06

### Added

- **`docs/setup-guide.pdf`** — a printable walkthrough for whoever deploys this
  into an AWS account and hands the result back. Written for someone who has
  never seen this repository: what gets created and what does not, the Bedrock
  model-access step that fails silently, the deploy, generating the config file,
  which credentials to attach, how to verify before sending, and a
  symptom/cause/fix table.

  Source is `docs/setup-guide.html`; `make guide` re-renders the PDF.

## [0.9.0] — 2026-08-06

For the case where someone else owns the AWS account: they deploy, fill in one
file, and hand it back. Two real bugs surfaced while testing that path.

### Fixed

- **`.env` only reached `make ask`.** It was loaded in `scripts/ask.py` alone, so
  a machine configured entirely through `.env` could question the deployed agent
  and then fail at `make ingest`, `make eval` and `make chat` — each reporting a
  missing setting rather than a file nobody read. Loading moved to
  `aiplat/config.py`, which already owns reading the environment, so every entry
  point gets it.

- **A blank value beat its default.** `os.environ.get(name, default)` returns `""`
  for a variable that exists and is empty, so `MODEL_ID=` in a `.env` produced an
  agent with no model id, and `PROMPT_NAME=` resolved the prompt path to the
  prompts *directory* rather than a prompt. Both failed far from the blank line
  that caused them — and a handover file that tells someone to leave optional
  fields blank is exactly how a key comes to exist with no value.

### Added

- **`docs/handover.env.template`** — every value the application needs from AWS,
  with the CloudFormation output name or CLI command for each. Filled in by
  whoever deployed, saved as `.env` by whoever runs it. `handover.env` is now
  git-ignored too, since a filled copy carries live credentials and tends to
  arrive under the name it was sent as.

## [0.8.3] — 2026-08-06

### Fixed

- **`docs/account-intake.yaml` invited a credential leak.** It asks IT for a
  `deploy_principal`, and nothing in the file said not to paste an access key
  there — in a form that lives in a **public repository** and travels by email.

  The file now leads with an explicit no-secrets warning, spells out that
  `deploy_principal` wants the *name* of a role or SSO permission set rather than
  a key, and adds `auth_method` / `sso_start_url` so the normal answer is an
  identity to assume. A key that must be issued goes through the usual secret
  channel and into `aws configure` locally.

  The only other secrets the platform uses — Langfuse headers and a gateway key —
  are optional, set after the deploy, and live in `.env`, which is git-ignored.

## [0.8.2] — 2026-08-06

### Fixed

- **`docs/account-intake.yaml` asked for three of the six things IT has to do.**
  It captured region, Bedrock model access and `cdk bootstrap`, and omitted the
  deploy principal — so the form could come back fully filled and still leave
  nobody able to run `make deploy`. A form that completes and blocks anyway is
  worse than no form.

  Also adds the two billing items from `docs/aws-requirements.md` §1. The cost
  allocation tag matters more than it reads: every resource is already tagged by
  tenant, but the tag does nothing in cost reports until it is activated, and
  **activation is not retroactive** — miss it and the early months cannot be split
  by tenant at all.

## [0.8.1] — 2026-08-06

### Added

- **Two fillable intake files**, so onboarding an account is handing over a form
  rather than a conversation.

  `docs/account-intake.yaml` — the facts about an AWS account that a deployment
  needs and cannot discover: account id, region, whether Bedrock model access is
  actually enabled, the *Knowledge bases per account* quota (which is the ceiling
  on how many tenants can exist), and the constraints that would change the design
  rather than the config — an SCP, a VPC requirement, a data-residency rule.

  `tenants/_template.yaml` — one per department: slug, corpus paths, optional
  metadata gate, optional prompt and model. Underscore-prefixed so an unfilled
  template is never deployed as a tenant named `REPLACE_ME`; a test asserts both
  that and that a minimally-filled copy parses.

  `.env` is deliberately not among them: `make env TENANT=<slug>` generates it
  from the deployed stacks, so there is nothing to hand-copy and nothing to
  mistype.

## [0.8.0] — 2026-08-06

### Added

- **Per-tenant agent config.** `tenants/<slug>.yaml` gains an `agent:` block with
  `prompt`, `prompt_version` and `model`. Two departments sharing one account can
  now be answered by differently-instructed agents on different models.

  Until now a tenant owned data and labels only, on the argument that shared
  behaviour is what separates a platform from several forks wearing a config
  file. That held with one use case and stopped holding with two: telling a legal
  team and an engineering team that the platform's prompt is the platform's is
  telling them to fork it.

  Chunking, the guardrail and retrieval stay shared — those are the parts an
  auditor asks about and the parts a mistake in is expensive.

  **No new machinery.** Per-tenant behaviour is normally a config service resolved
  per request, because most platforms serve every tenant from one process.
  `infra/app.py` already deploys one Lambda per tenant, so this is a value in an
  environment variable: no new component, nothing in the request path, and a bad
  value fails at deploy rather than at runtime.

  No `tools` field: exactly one tool exists, so a list to choose from would be
  configuration for a decision nobody can make yet. It goes there when a second
  tool does.

- `make env TENANT=x` now writes the tenant's prompt and model into `.env`, so a
  local `make ask` or eval run answers the way that tenant's deployment does.
  Without it a local result would look like evidence about a deployment it did
  not represent.

### Changed

- `PROMPT_NAME` selects a directory under `services/agent/prompts/`, set per
  tenant. Defaults to `system`, so an unchanged tenant file behaves unchanged.

## [0.7.0] — 2026-08-05

### Added

- **`DETECT_VECTOR_FIGURES`** — treat clusters of lines and fills as figures, so a
  schematic *drawn* rather than embedded becomes searchable. That is most drawings
  in engineering PDFs, and every figure path until now was blind to them.

  Off by default, and separately from `FIGURE_PROCESSOR` on purpose. Everything
  else in the ingest path is extraction: a figure is in the file or it is not.
  This is inference, and a wrong guess spends a model call *and* files a
  description of a page border in the index as though it were content. Turning it
  on is a decision about that trade, so it gets its own switch.

  Implemented upstream in prismdoc v0.11.0, which rejects ruled tables, header and
  footer rules, page borders, lone boxes, icon clusters and CAD-dense pages — each
  with a test. The heuristic has no benchmark behind it: enable it on a sample and
  read the counts before a large corpus.

## [0.6.1] — 2026-08-05

### Changed

- Pinned to prismdoc v0.10.0, which extracts figures from **PPTX and DOCX**, not
  just PDF. Slide decks were the worst case before this: a deck's meaning lives in
  its diagrams, so a forty-slide architecture review ingested as text alone left
  forty titles and nothing to retrieve — and it failed silently, since the deck
  looked ingested.

  Office figures are placed exactly, better than the PDF path manages. Docling
  marks each picture's position in the Markdown and prismdoc yields pictures in
  that same order, so a description is substituted where its picture sat instead
  of being appended per page.

## [0.6.0] — 2026-08-05

### Changed

- **All parsing belongs to prismdoc; this repo no longer routes by format.**
  0.5.0 kept a docling branch here for DOCX, PPTX and HTML, because prismdoc had
  no loader for them. That meant every format prismdoc gained or lost was a
  change in this repo too — a consumer tracking another project's internals.

  prismdoc v0.8.1 adds an `OfficeLoader`, so it now dispatches every supported
  format itself. `parsing.py` hands it a file and takes Markdown. What stays here
  is the one decision prismdoc cannot make: what a figure should *become*.

- **Parser is `passthrough`, not `docling`.** Docling returns whole-document
  Markdown with no page markers, and figure placement needs them — every figure
  in a document was landing at the end rather than on its page, so a chunker
  would pair a page-1 diagram with the last page's prose. Fixed upstream in
  prismdoc v0.8.1 (`PdfPlumberParser` emits page markers, and the merge stage
  warns when it cannot place); this repo picks the parser that preserves them.

### Fixed

- `.gif` was missing from `PARSEABLE`, so GIFs were never discovered even though
  prismdoc reads them. Caught by a new test asserting that discovery and prismdoc
  agree on the format list rather than drifting apart silently.

## [0.5.1] — 2026-08-05

### Fixed

- **The `ingest` extra was uninstallable.** 0.5.0 declared `prismdoc>=0.8`, but
  prismdoc is not published to PyPI at all — so `pip install -e '.[ingest]'`
  failed to resolve. It went unnoticed because the development install came from
  a local path.

  Pinned to `prismdoc @ git+https://github.com/gnutvz/prismdoc@v0.8.0`. The tag
  is load-bearing rather than cosmetic: v0.8.0 is the first prismdoc release
  whose core dependencies are permissive — before it, prismdoc carried PyMuPDF
  (AGPL-3.0), which `make licenses` refuses. Verified by installing the ref into
  a clean environment: prismdoc 0.8.0, no PyMuPDF, permissive engine by default.

## [0.5.0] — 2026-08-05

Ingest now runs on [prismdoc](https://github.com/gnutvz/prismdoc), which closes
the gap that mattered most: **figures in documents were never indexed at all.**

### Fixed

- **Diagrams, charts and screenshots were silently dropped.** `parse_to_markdown`
  called docling with default options, and docling disables every enrichment by
  default — picture description, picture classification, formula and code
  understanding. So a figure became an empty placeholder in the Markdown: no
  text, no description, no warning. The document logged as ingested, the corpus
  looked complete, and the agent could not answer a question about anything drawn
  rather than written.

  prismdoc cuts each figure out of the page, a processor turns it into text, and
  the text is merged back where the figure sat. `FIGURE_PROCESSOR` chooses how:
  `off` (default), `ocr` (reads text printed inside the figure, local and free),
  or `vlm` (describes it with `MODEL_ID`).

  Default `off` on purpose: `vlm` is one model call per figure, and a 5,189-document
  corpus should discover that as a decision rather than an invoice.

### Added

- `services/ingest/parsing.py` — owns document → Markdown and the routing between
  the two parsing stacks.
- `services/ingest/figures.py` — `BedrockFigureProcessor` describes a figure with
  the model `aiplat.llm` already hands out, so the figure path inherits the
  configured route, region and credentials instead of growing a second way to
  reach a model. The prompt targets retrieval, not prose: name the entities and
  relationships, transcribe tables and labels, and return `NO INFORMATION` for a
  decorative logo rather than writing a confident paragraph about it.
- `FIGURE_PROCESSOR` setting, validated against its allowed values — a typo fails
  at startup rather than silently indexing no figures.
- `tests/fixtures/page_with_figure.pdf` — a committed 5KB PDF, so the figure path
  is tested on a real document rather than a mock of the library doing the work.

### Changed

- The `ingest` extra is `prismdoc>=0.8` plus docling. prismdoc drives the
  pipeline; docling stays for DOCX, PPTX and HTML, which prismdoc has no loader
  for, and because its layout model reads tables pdfplumber misses.

  The version floor is load-bearing: prismdoc before 0.8 carries PyMuPDF
  (AGPL-3.0) as a core dependency, which `make licenses` refuses and rightly so.

## [0.4.1] — 2026-08-04

Two ingest defects that only show up on a real corpus — the benchmark corpus is
one flat directory of uniquely-named files, so neither could surface there.

### Fixed

- **Documents with the same filename overwrote each other.** Discovery recurses
  (`rglob`), but the S3 key was built from `path.stem` alone, so a corpus with
  `2023/report.pdf` beside `2024/report.pdf` produced one key and indexed one
  document. The upload logged success for both.

  Keys now keep the path relative to the source root, and the original extension
  — `report.pdf` and `report.docx` in one folder are two documents. Files that
  are already Markdown skip the added suffix, since they cannot collide.

- **One unparseable document ended the whole run.** `parse_to_markdown` was
  called without a guard, so a corrupt PDF or a password-protected spreadsheet
  aborted ingestion partway, leaving a half-uploaded corpus and no way to resume:
  re-running started from the beginning and hit the same file again.

  Failures are now collected and skipped. They are reported separately from
  metadata refusals — a refusal is the gate working as designed, a failure is a
  document whose owner still expects it to be searchable — and the process exits
  non-zero so a scheduled ingest cannot report success while part of the corpus
  is missing. The sync still runs, because holding back the documents that did
  parse helps nobody.

  A missing docling install still raises: it is identical for every file, so
  continuing would print it once per document and upload nothing.

### Changed

- `ingest_source()` returns a `SourceReport` (uploaded / refused / failed)
  instead of a 2-tuple.

## [0.4.0] — 2026-08-04

The eval suite measured only generation. A RAG pipeline fails in stages that fail
independently, so three generation-side numbers could not tell a model that
ignored good passages from a model that never received them.

### Added

- **Context recall and context precision.** Scored against the retriever
  directly — `retrieve()` is called with the question rather than capturing what
  the agent searched for. That is deliberate: it measures the retriever as a
  component, so the number moves when chunking, embeddings or top-k move and not
  when the prompt changes.

  Precision is rank-weighted (average precision) rather than a flat hit rate. A
  relevant passage at rank 1 and the same passage at rank 6 are different
  outcomes for a model reading top-down under a token budget — and a flat rate
  cannot see the difference, which would make it useless for the decision it
  exists to inform, since reranking changes ordering and not membership.

  This closes a gap the repo had documented against itself:
  `docs/architecture.drawio` named the trigger for building reranking as "eval
  shows recall, not generation, is the ceiling" — a condition the eval suite had
  no way to evaluate.

- **`unanswerable_retrieval_rate`** — how often a question the corpus cannot
  answer still returned passages. Each one is plausible-looking noise the model
  has to refuse, so a high number means refusal accuracy is carrying weight that
  belongs to the retrieval threshold.

- `tests/test_retrieval_metrics.py` — covers the three diagnoses the pair is
  meant to separate (retrieval ceiling, ranking problem, healthy retrieval), and
  pins that scoring degrades to a missing number rather than a failed case when
  the index is unreachable.

### Changed

- Retrieval scoring runs concurrently with the answer and is kept even when
  generation fails — knowing the retriever was healthy is what separates a model
  outage from an index problem.
- Retrieval metrics are averaged over answerable cases only. On an unanswerable
  question the expected evidence does not exist, so recall against it is
  meaningless.

## [0.3.0] — 2026-08-04

Three seams the repo had argued for and not built: filtered retrieval, versioned
prompts, and a second runtime. Each was cheap now and expensive later — the
retrieval filter reads metadata the ingest pipeline has always written precisely
so it would not need a re-index, and a prompt registry added after a regression
cannot tell you whether the regression is new.

Answers gain a `prompt` field, which is a contract change.

### Added

- **Filtered retrieval.** `retrieve()` accepts metadata attributes every passage
  must match, and `make_search_tool(filters)` bakes them into the tool an agent
  is given. The ingest pipeline has always written a metadata sidecar next to
  each document specifically so this would not require a re-index later.

  Filters are deliberately *not* a tool argument. The model is a caller like any
  other, and unlike the others it reads attacker-influenced text on every turn —
  a retrieved passage can carry instructions. A filter the model can name is a
  filter it can drop, with the tool schema documenting how. They live in a
  closure instead, so `build_agent(retrieval_filters=...)` is the only way to set
  them and there is no way to widen them from inside the conversation.

  The filter grammar is equality and AND only. Every operator added is another
  way to write a restriction that does not restrict.

  Nothing supplies filters yet: end-user identity does not exist here. This is
  the seam it will arrive through.

- `tests/test_retrieval_filters.py` — covers the filter shape, and separately
  asserts the tool exposes no filter parameter to the model. The second is the
  control; the first is just correctness.

- **Versioned prompts.** The system prompt moved from a string literal in
  `services/agent/agent.py` to `services/agent/prompts/system/v1.md`, loaded by
  `aiplat.prompts`. A prompt is the most frequently changed and least reviewed
  part of an agent, and as a literal it had no version — so an eval score had
  nothing to attribute it to, a regression could not be rolled back
  independently of the deploy that carried it, and a trace recorded what the
  model said but not what it was told.

  `v2.md` next to `v1.md` is the whole mechanism. `PROMPT_VERSION` pins a
  deployment; unset takes the highest on disk, which is right locally and wrong
  in production, so the setting exists to make adding a file and shipping it
  different acts.

  Not a prompt service with a database and an approval flow. That solves a
  problem that starts when non-engineers edit prompts.

  The loader is in `aiplat` because any workload needs it; the prompt text is
  not, because it instructs one use case. Same test as everything else in that
  package.

- **An AgentCore Runtime stack.** `infra/stacks/agentcore_stack.py`, opt-in with
  `cdk deploy -c agentcore=true`. Lambda stays the default; this is the
  promotion taken when streaming, long sessions or managed identity start to
  matter.

  The platform's central claim is that changing runtime is a deployment
  decision rather than a rewrite. That was half checkable while only one runtime
  had a stack. The tests now assert the properties that make the Lambda path
  defensible survive the move — retrieval scoped to one knowledge base ARN,
  inference scoped away from the control plane, the tenant fixed by the
  deployment.

  The image is not built at synth time on purpose: `DockerImageAsset` would make
  `cdk synth` and every stack test require Docker. The stack creates a per-tenant
  ECR repository and `make push-agent TENANT=<slug>` pushes to it.

  Not deployed or verified against a live account. Synthesis proves the
  template's shape, not that AgentCore accepts it.

### Changed

- **`infra/app.py` builds its stacks inside `main()` behind a `__main__` guard.**
  It used to build them at import. Any test putting `infra/` on `sys.path` made
  that module shadow the `app/` package, so `from app import chat` in
  `tests/test_chat_ui.py` started a Docker bundle of the Lambda instead of
  importing the chat UI. It only stayed hidden because the two tests that add
  that path sorted after the one that imports `app`. Adding a third, earlier
  one surfaced it. The tests now append rather than prepend, and the guard makes
  importing the module harmless either way.

- Answers now carry a `prompt` field (`system@v1`), and the same label is
  stamped on every trace and included in the eval report. A client reporting a
  bad answer was describing behaviour nobody could reproduce.

  This is a contract change, declared in `tests/test_contract.py` rather than
  noticed later — which is what asserting the field list exactly, rather than as
  a subset, is for.

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
