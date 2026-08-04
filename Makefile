.PHONY: help install deploy destroy ingest eval trace-local lint test

PYTHON := .venv/bin/python
CDK := cd infra && PATH="$(PWD)/.venv/bin:$$PATH" npx cdk

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install: ## Create the virtualenv and install the platform
	uv venv --python 3.11
	uv pip install -e '.[otel,ui,dev]'

env: ## Point .env at a tenant: make env TENANT=acme
	$(PYTHON) scripts/write_env.py --tenant $(TENANT)

deploy: ## Deploy every tenant (and shared stacks)
	$(CDK) deploy --all --require-approval never

deploy-tenant: ## Deploy one tenant: make deploy-tenant TENANT=acme
	$(CDK) deploy AiPlat-Safety AiPlat-Knowledge-$(TENANT) AiPlat-Api-$(TENANT) --require-approval never

deploy-obs: ## Deploy everything including self-hosted Langfuse (has a standing cost)
	$(CDK) deploy --all -c observability=true --require-approval never

destroy: ## Tear down. Documents bucket and knowledge base are RETAIN — delete by hand.
	$(CDK) destroy --all

ingest: ## Ingest a tenant's configured sources: make ingest TENANT=acme
	$(PYTHON) -m services.ingest.ingest --tenant $(TENANT) --wait

ingest-path: ## Ad-hoc ingest, no metadata gate: make ingest-path SRC=./docs
	$(PYTHON) -m services.ingest.ingest $(SRC) --wait

dataset: ## Download EnterpriseRAG-Bench corpus + questions (MIT, ~60MB)
	$(PYTHON) -m evals.datasets.fetch_enterprise_bench

generate: ## Write an eval set from a tenant's own corpus: make generate TENANT=acme
	$(PYTHON) -m evals.generate --tenant $(TENANT)

eval: ## Score a tenant against its own dataset: make eval TENANT=acme
	$(PYTHON) -m evals.run --dataset evals/datasets/$(TENANT).jsonl

eval-smoke: ## Quick harness check, no corpus needed
	$(PYTHON) -m evals.run --dataset evals/datasets/smoke.jsonl --judge

# Port 8000 is Chainlit's default and a popular one — SSH tunnels and other
# dev servers grab it constantly. Override with: make chat PORT=8001
CHAT_PORT ?= 8000

chat: ## Open the chat UI (make chat CHAT_PORT=8001 if 8000 is taken)
	@lsof -nP -iTCP:$(CHAT_PORT) -sTCP:LISTEN >/dev/null 2>&1 \
	  && { echo "Port $(CHAT_PORT) is already in use:"; lsof -nP -iTCP:$(CHAT_PORT) -sTCP:LISTEN; \
	       echo; echo "Run: make chat CHAT_PORT=8001"; exit 1; } || true
	.venv/bin/chainlit run app/chat.py --port $(CHAT_PORT)

ask: ## Ask the deployed agent: make ask Q="what is the deploy procedure?"
	$(PYTHON) scripts/ask.py "$(Q)"

trace-local: ## Run Langfuse locally on :3000 for development
	docker compose up -d
	@echo "Langfuse: http://localhost:3000 — create a project, then set OTEL_EXPORTER_OTLP_*"

gateway-local: ## Run the LiteLLM proxy on :4000 (LLM_ROUTE=gateway)
	docker compose --profile gateway up -d
	@echo "Gateway: http://localhost:4000 — set LLM_ROUTE=gateway and MODEL_ID=platform-default"

image-ingest: ## Build the ingest container (docling is too heavy for Lambda)
	docker build -f services/ingest/Dockerfile -t aiplat-ingest .

image-agent: ## Build the AgentCore Runtime image (arm64)
	docker build --platform linux/arm64 -f services/agent/Dockerfile -t aiplat-agent .

push-agent: ## Push the agent image to a tenant's repo: make push-agent TENANT=acme
	@test -n "$(TENANT)" || { echo "TENANT is required: make push-agent TENANT=acme"; exit 1; }
	$(eval REGION := $(shell aws configure get region || echo us-west-2))
	$(eval ACCOUNT := $(shell aws sts get-caller-identity --query Account --output text))
	$(eval REPO := $(ACCOUNT).dkr.ecr.$(REGION).amazonaws.com/aiplat-agent-$(TENANT))
	aws ecr get-login-password --region $(REGION) \
	  | docker login --username AWS --password-stdin $(ACCOUNT).dkr.ecr.$(REGION).amazonaws.com
	docker tag aiplat-agent:latest $(REPO):latest
	docker push $(REPO):latest
	@echo "Pushed $(REPO):latest — now: cd infra && npx cdk deploy -c agentcore=true"

lint: ## Check style
	.venv/bin/ruff check aiplat services evals infra tests scripts app

licenses: ## Fail if a dependency arrives under a licence we cannot ship
	$(PYTHON) scripts/check_licenses.py

test: ## Run the offline test suite (no AWS needed)
	.venv/bin/pytest -q
