.PHONY: help install deploy destroy ingest eval trace-local lint test

PYTHON := .venv/bin/python
CDK := cd infra && PATH="$(PWD)/.venv/bin:$$PATH" npx cdk

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install: ## Create the virtualenv and install the platform
	uv venv --python 3.11
	uv pip install -e '.[otel,dev]'

deploy: ## Deploy knowledge + safety + api
	$(CDK) deploy --all --require-approval never

deploy-obs: ## Deploy everything including self-hosted Langfuse (has a standing cost)
	$(CDK) deploy --all -c observability=true --require-approval never

destroy: ## Tear down. Documents bucket and knowledge base are RETAIN — delete by hand.
	$(CDK) destroy --all

ingest: ## Ingest documents: make ingest SRC=./docs
	$(PYTHON) -m services.ingest.ingest $(SRC) --wait

dataset: ## Download EnterpriseRAG-Bench corpus + questions (MIT, ~60MB)
	$(PYTHON) -m evals.datasets.fetch_enterprise_bench

eval: ## Run the eval suite against the deployed agent
	$(PYTHON) -m evals.run --dataset evals/datasets/enterprise-bench.jsonl

eval-smoke: ## Quick harness check, no corpus needed
	$(PYTHON) -m evals.run --dataset evals/datasets/smoke.jsonl --judge

trace-local: ## Run Langfuse locally on :3000 for development
	docker compose up -d
	@echo "Langfuse: http://localhost:3000 — create a project, then set OTEL_EXPORTER_OTLP_*"

lint:
	.venv/bin/ruff check aiplat services evals infra

test:
	.venv/bin/pytest -q
