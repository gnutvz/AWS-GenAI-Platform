#!/usr/bin/env python3
"""CDK entrypoint.

Stack split follows blast radius, not tidiness: knowledge and safety hold durable
state and policy, api holds disposable compute. Redeploying the agent fifty times a
day should never risk the index or the guardrail.

    cdk deploy --all                          # knowledge + safety + api
    cdk deploy --all -c observability=true    # ... plus self-hosted Langfuse
"""

from __future__ import annotations

import os

import aws_cdk as cdk
from stacks.api_stack import ApiStack
from stacks.knowledge_stack import KnowledgeStack
from stacks.observability_stack import ObservabilityStack
from stacks.safety_stack import SafetyStack

app = cdk.App()

env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION") or os.environ.get("AWS_REGION", "us-west-2"),
)

prefix = app.node.try_get_context("prefix") or "AiPlat"
model_id = app.node.try_get_context("model_id") or "global.anthropic.claude-sonnet-4-6"

knowledge = KnowledgeStack(app, f"{prefix}-Knowledge", env=env)
safety = SafetyStack(app, f"{prefix}-Safety", env=env)

otlp_endpoint = ""
if app.node.try_get_context("observability"):
    observability = ObservabilityStack(app, f"{prefix}-Observability", env=env)
    otlp_endpoint = f"{observability.endpoint}/api/public/otel"

api = ApiStack(
    app,
    f"{prefix}-Api",
    env=env,
    knowledge_base_id=knowledge.knowledge_base_id,
    documents_bucket_name=knowledge.documents_bucket.bucket_name,
    guardrail_id=safety.guardrail_id,
    guardrail_version=safety.guardrail_version,
    model_id=model_id,
    otlp_endpoint=otlp_endpoint,
    # Credentials are set after Langfuse is up and a project exists — see README.
    otlp_headers="",
)
api.add_stack_dependency(knowledge)
api.add_stack_dependency(safety)

cdk.Tags.of(app).add("project", "aiplat")

app.synth()
