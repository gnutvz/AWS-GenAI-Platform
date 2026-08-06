#!/usr/bin/env python3
"""CDK entrypoint.

Two axes to the stack split.

Across tenants: every tenant gets its own knowledge base and its own agent
function. Deploying, breaking or deleting one tenant cannot touch another, and a
tenant's Lambda role names exactly one knowledge base ARN — cross-tenant
retrieval is a permission nobody holds rather than a filter someone can forget.

Within a tenant: knowledge holds durable state, api holds disposable compute.
Redeploying the agent fifty times a day should never risk the index.

Shared by everyone: the guardrail, because it is policy rather than data.

    cdk deploy --all                                    # every tenant
    cdk deploy AiPlat-Knowledge-acme AiPlat-Api-acme    # just one

Tracing is deliberately not a stack here. The agent exports OTLP and any
collector can receive it, so where those spans land is a config decision rather
than infrastructure this repo owns — see docs/tracing.md.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import aws_cdk as cdk

# infra/ is executed as a script by the CDK CLI, so the repo root — and with it
# the aiplat package — is not on sys.path by default.
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from stacks.agentcore_stack import AgentCoreStack
from stacks.api_stack import ApiStack
from stacks.knowledge_stack import KnowledgeStack
from stacks.safety_stack import SafetyStack

from aiplat.tenants import load_all


def main() -> None:
    """Build every stack and synthesize.

    Behind a __main__ guard rather than at module level, which matters more than
    it looks. Anything that puts `infra/` on sys.path — the stack tests do —
    makes this module shadow the `app/` package, and importing it used to build
    the whole app as a side effect: `from app import chat` would start a Docker
    bundle of the Lambda. cdk.json runs `python3 app.py`, so the guard costs
    nothing.
    """
    app = cdk.App()

    env = cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION") or os.environ.get("AWS_REGION", "us-west-2"),
    )

    prefix = app.node.try_get_context("prefix") or "AiPlat"
    model_id = app.node.try_get_context("model_id") or "global.anthropic.claude-sonnet-4-6"

    tenants = load_all(REPO_ROOT / "tenants")
    if not tenants:
        raise SystemExit(
            "No tenants defined. Copy tenants/_example.yaml to tenants/<slug>.yaml — "
            "there is nothing to deploy without one."
        )

    # Shared: safety policy is owned by a different team and changes on a different
    # cadence than any tenant's data.
    safety = SafetyStack(app, f"{prefix}-Safety", env=env)

    # Empty unless set in .env after the fact: the collector lives outside this
    # deployment, so CDK has no endpoint to hand the function at synth time.
    otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")

    for tenant in tenants:
        knowledge = KnowledgeStack(
            app,
            f"{prefix}-Knowledge-{tenant.slug}",
            env=env,
            tenant=tenant,
            description=f"Knowledge base and documents for tenant '{tenant.slug}'",
        )

        api = ApiStack(
            app,
            f"{prefix}-Api-{tenant.slug}",
            env=env,
            tenant=tenant,
            knowledge_base_id=knowledge.knowledge_base_id,
            documents_bucket_name=knowledge.documents_bucket.bucket_name,
            guardrail_id=safety.guardrail_id,
            guardrail_version=safety.guardrail_version,
            model_id=model_id,
            otlp_endpoint=otlp_endpoint,
            # Never from CDK: these are credentials, and stack parameters are
            # readable by anyone with describe-stacks. Set on the function after
            # the fact — see docs/tracing.md.
            otlp_headers="",
            description=f"Agent runtime for tenant '{tenant.slug}'",
        )
        api.add_stack_dependency(knowledge)
        api.add_stack_dependency(safety)

        # Opt-in: Lambda is the default runtime and AgentCore is the promotion, taken
        # when streaming or long sessions start to matter. Same build_agent() either
        # way — that is the point of the split, and this stack is what makes the
        # claim checkable rather than asserted.
        if app.node.try_get_context("agentcore"):
            agentcore = AgentCoreStack(
                app,
                f"{prefix}-AgentCore-{tenant.slug}",
                env=env,
                tenant=tenant,
                knowledge_base_id=knowledge.knowledge_base_id,
                documents_bucket_name=knowledge.documents_bucket.bucket_name,
                session_bucket_name=api.session_bucket.bucket_name,
                guardrail_id=safety.guardrail_id,
                guardrail_version=safety.guardrail_version,
                model_id=model_id,
                otlp_endpoint=otlp_endpoint,
                otlp_headers="",
                description=f"AgentCore Runtime for tenant '{tenant.slug}'",
            )
            # Reuses the session bucket the api stack owns, so state survives the move.
            agentcore.add_stack_dependency(api)

    cdk.Tags.of(app).add("project", "aiplat")

    app.synth()


if __name__ == "__main__":
    main()
