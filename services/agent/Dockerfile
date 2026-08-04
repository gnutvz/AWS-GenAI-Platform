# Image for Bedrock AgentCore Runtime — the production path off Lambda.
#
# Deploy this when you need token streaming, sessions longer than 15 minutes, or
# AgentCore's managed identity and browser/code-interpreter tools. The agent code
# is identical to what Lambda runs; only the entrypoint differs.
#
#   docker build --platform linux/arm64 -f services/agent/Dockerfile -t aiplat-agent .
#   docker run --rm -p 8080:8080 -v ~/.aws:/root/.aws:ro \
#     -e AWS_REGION -e KNOWLEDGE_BASE_ID -e SESSION_BUCKET aiplat-agent
#
# AgentCore Runtime requires linux/arm64 — build with --platform on an x86 host.
# Build context is the repo root, not this directory.

FROM --platform=linux/arm64 python:3.11-slim

WORKDIR /app

COPY pyproject.toml ./
COPY aiplat ./aiplat
COPY services ./services

RUN pip install --no-cache-dir '.[agentcore,otel]'

# AgentCore Runtime health-checks and invokes on 8080.
EXPOSE 8080

ENV LLM_ROUTE=bedrock \
    OTEL_SERVICE_NAME=aiplat-agent

CMD ["python", "-m", "services.agent.agentcore_app"]
