"""The AgentCore Runtime stack, checked against synthesized CloudFormation.

The platform's central claim is that changing runtime is a deployment decision:
`services/agent/agent.py` does not know whether it is running in Lambda or
AgentCore, so promoting one to the other should not touch application code. That
claim was only ever half checkable — the Lambda stack existed and the AgentCore
one did not, so nothing could compare them.

What matters is not that the stack synthesizes. It is that the properties which
make the Lambda path defensible survive the move: retrieval scoped to one
knowledge base, inference scoped away from the control plane, the tenant fixed
by the deployment. A runtime migration that quietly widens a permission is how
an isolation boundary gets lost.

What these tests cannot tell you: whether AgentCore accepts the template. Nothing
here has been deployed. Synthesis proves shape, not acceptance.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from aiplat.tenants import Tenant

# Appended, not prepended: infra/app.py would otherwise shadow the app/
# package for every test collected after this one.
sys.path.append(str(Path(__file__).resolve().parents[1] / "infra"))
from stacks.agentcore_stack import AgentCoreStack

ACME = Tenant(slug="acme", display_name="Acme Corp")


def build(context: dict | None = None) -> Template:
    app = cdk.App(context={"aws:cdk:bundling-stacks": [], **(context or {})})
    stack = AgentCoreStack(
        app,
        "AiPlat-AgentCore-acme",
        env=cdk.Environment(account="111111111111", region="us-west-2"),
        tenant=ACME,
        knowledge_base_id="kb-acme-123",
        documents_bucket_name="docs-acme",
        session_bucket_name="sessions-acme",
        guardrail_id="gr-abc",
        guardrail_version="3",
        model_id="test-model",
    )
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def template() -> Template:
    return build()


class TestRuntime:
    def test_a_runtime_is_created(self, template):
        template.resource_count_is("AWS::BedrockAgentCore::Runtime", 1)

    def test_runtime_name_avoids_characters_the_service_rejects(self, template):
        """Every other resource carries the slug verbatim; this one cannot."""
        runtimes = template.find_resources("AWS::BedrockAgentCore::Runtime")
        name = next(iter(runtimes.values()))["Properties"]["AgentRuntimeName"]
        assert "-" not in name
        assert "acme" in name

    def test_tenant_is_fixed_by_the_deployment(self, template):
        """The invariant the AgentCore entrypoint got wrong in 0.1.1."""
        runtimes = template.find_resources("AWS::BedrockAgentCore::Runtime")
        env = next(iter(runtimes.values()))["Properties"]["EnvironmentVariables"]
        assert env["TENANT"] == "acme"

    def test_environment_matches_the_lambda(self, template):
        """Same settings either side of the migration, or it is not the same agent."""
        runtimes = template.find_resources("AWS::BedrockAgentCore::Runtime")
        env = next(iter(runtimes.values()))["Properties"]["EnvironmentVariables"]
        assert env["KNOWLEDGE_BASE_ID"] == "kb-acme-123"
        assert env["GUARDRAIL_ID"] == "gr-abc"
        assert env["LLM_ROUTE"] == "bedrock"

    def test_image_tag_is_overridable(self):
        """`latest` is convenient and a poor audit trail."""
        template = build({"agent_image_tag": "sha-abc123"})
        assert "sha-abc123" in json.dumps(template.to_json())


class TestPermissionsSurviveTheMigration:
    def test_retrieval_is_scoped_to_one_knowledge_base(self, template):
        statements = _statements(template)
        retrieve = [s for s in statements if "bedrock:Retrieve" in _actions(s)]

        assert retrieve, "the runtime cannot retrieve at all"
        for statement in retrieve:
            rendered = json.dumps(statement["Resource"])
            assert "knowledge-base/*" not in rendered, (
                "moving to AgentCore widened retrieval to every knowledge base in the "
                "account — the isolation boundary does not survive the migration"
            )
            assert "kb-acme-123" in rendered

    def test_inference_cannot_reach_the_control_plane(self, template):
        statements = _statements(template)
        invoke = [s for s in statements if "bedrock:InvokeModel" in _actions(s)]

        assert invoke
        for statement in invoke:
            resources = json.dumps(statement["Resource"])
            assert "foundation-model" in resources or "inference-profile" in resources
            assert '"*"' not in resources, "inference granted on every Bedrock resource"

    def test_session_access_is_limited_to_this_tenants_bucket(self, template):
        statements = _statements(template)
        s3 = [s for s in statements if any(a.startswith("s3:") for a in _actions(s))]

        assert s3
        for statement in s3:
            assert "sessions-acme" in json.dumps(statement["Resource"])

    def test_only_bedrock_agentcore_can_assume_the_role(self, template):
        template.has_resource_properties(
            "AWS::IAM::Role",
            {
                "AssumeRolePolicyDocument": Match.object_like(
                    {
                        "Statement": Match.array_with(
                            [
                                Match.object_like(
                                    {
                                        "Principal": {
                                            "Service": "bedrock-agentcore.amazonaws.com"
                                        }
                                    }
                                )
                            ]
                        )
                    }
                )
            },
        )


class TestImageRepository:
    def test_the_repository_is_per_tenant(self, template):
        template.has_resource_properties(
            "AWS::ECR::Repository", {"RepositoryName": "aiplat-agent-acme"}
        )

    def test_the_image_survives_a_stack_delete(self, template):
        """A running runtime still pulling from it should not lose its image."""
        repos = template.find_resources("AWS::ECR::Repository")
        assert all(r["DeletionPolicy"] == "Retain" for r in repos.values())

    def test_images_are_scanned(self, template):
        template.has_resource_properties(
            "AWS::ECR::Repository",
            {"ImageScanningConfiguration": {"ScanOnPush": True}},
        )


def _statements(template: Template) -> list[dict]:
    policies = template.find_resources("AWS::IAM::Policy")
    return [
        statement
        for policy in policies.values()
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
    ]


def _actions(statement: dict) -> list[str]:
    action = statement.get("Action", [])
    return [action] if isinstance(action, str) else action
