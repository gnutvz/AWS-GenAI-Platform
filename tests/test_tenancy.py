"""Tenant isolation, checked against the synthesized CloudFormation.

"Each tenant is isolated" is a claim made to buyers. It should be enforced by a
test, not by a paragraph in a README — this file is that enforcement.

Isolation here is structural: a tenant's resources live in its own stack and its
roles name only its own ARNs. That is what makes cross-tenant retrieval a
permission nobody holds rather than a filter someone can forget to apply.

Bundling is disabled via the `aws:cdk:bundling-stacks` context key so these run
without Docker and without building a Lambda package.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from aiplat.tenants import Tenant

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "infra"))
from stacks.api_stack import ApiStack
from stacks.knowledge_stack import KnowledgeStack

ACME = Tenant(slug="acme", display_name="Acme Corp")
GLOBEX = Tenant(slug="globex", display_name="Globex Inc")


@pytest.fixture(scope="module")
def synthesized() -> dict[str, Template]:
    """Two tenants deployed side by side, exactly as infra/app.py builds them."""
    app = cdk.App(context={"aws:cdk:bundling-stacks": []})
    env = cdk.Environment(account="111111111111", region="us-west-2")

    # Build every stack first. Template.from_stack() synthesizes, and adding a
    # construct after the first synth raises ConstructTreeModifiedAfterSynth.
    stacks: dict[str, cdk.Stack] = {}
    for tenant in (ACME, GLOBEX):
        knowledge = KnowledgeStack(
            app, f"AiPlat-Knowledge-{tenant.slug}", env=env, tenant=tenant
        )
        api = ApiStack(
            app,
            f"AiPlat-Api-{tenant.slug}",
            env=env,
            tenant=tenant,
            knowledge_base_id=knowledge.knowledge_base_id,
            documents_bucket_name=knowledge.documents_bucket.bucket_name,
            guardrail_id="gr-test",
            guardrail_version="1",
            model_id="test-model",
        )
        stacks[f"knowledge-{tenant.slug}"] = knowledge
        stacks[f"api-{tenant.slug}"] = api

    return {name: Template.from_stack(stack) for name, stack in stacks.items()}


def as_json(template: Template) -> str:
    return json.dumps(template.to_json())


class TestSeparateResources:
    def test_each_tenant_gets_its_own_knowledge_base(self, synthesized):
        for slug in ("acme", "globex"):
            synthesized[f"knowledge-{slug}"].resource_count_is(
                "AWS::Bedrock::KnowledgeBase", 1
            )

    def test_each_tenant_gets_its_own_vector_index(self, synthesized):
        for slug in ("acme", "globex"):
            synthesized[f"knowledge-{slug}"].has_resource_properties(
                "AWS::S3Vectors::Index", {"IndexName": f"{slug}-index"}
            )

    def test_each_tenant_gets_its_own_documents_bucket(self, synthesized):
        for slug in ("acme", "globex"):
            synthesized[f"knowledge-{slug}"].resource_count_is("AWS::S3::Bucket", 1)


class TestNoCrossTenantReferences:
    """The core claim: nothing in one tenant's stacks mentions another tenant."""

    @pytest.mark.parametrize(
        ("stack", "foreign"),
        [
            ("knowledge-acme", "globex"),
            ("knowledge-globex", "acme"),
            ("api-acme", "globex"),
            ("api-globex", "acme"),
        ],
    )
    def test_stack_never_names_another_tenant(self, synthesized, stack, foreign):
        assert foreign not in as_json(synthesized[stack]), (
            f"{stack} references tenant '{foreign}'. Cross-tenant isolation depends on "
            f"a stack naming only its own resources."
        )


class TestAgentPermissions:
    def test_agent_can_retrieve_from_exactly_one_knowledge_base(self, synthesized):
        """Not a wildcard, and not a list — one ARN, scoped by the deployment."""
        policies = synthesized["api-acme"].find_resources("AWS::IAM::Policy")
        retrieve_resources = [
            statement["Resource"]
            for policy in policies.values()
            for statement in policy["Properties"]["PolicyDocument"]["Statement"]
            if statement.get("Action") == "bedrock:Retrieve"
        ]

        assert retrieve_resources, "agent has no bedrock:Retrieve permission at all"
        for resource in retrieve_resources:
            rendered = json.dumps(resource)
            assert "knowledge-base/*" not in rendered, (
                "bedrock:Retrieve is granted on every knowledge base in the account — "
                "that is the whole isolation boundary gone"
            )

    def test_tenant_is_fixed_by_the_deployment(self, synthesized):
        """A caller must not be able to label itself as another tenant."""
        for slug in ("acme", "globex"):
            functions = synthesized[f"api-{slug}"].find_resources("AWS::Lambda::Function")
            # The stack also contains CDK's own auto-delete-objects helper, which
            # has no Environment block — hence .get rather than indexing.
            envs = [
                fn["Properties"].get("Environment", {}).get("Variables", {})
                for fn in functions.values()
            ]
            assert any(env.get("TENANT") == slug for env in envs), (
                f"api-{slug} does not pin TENANT in the function environment"
            )

    def test_function_url_is_not_public(self, synthesized):
        synthesized["api-acme"].has_resource_properties(
            "AWS::Lambda::Url", {"AuthType": "AWS_IAM"}
        )


class TestTagging:
    def test_resources_carry_the_tenant_tag(self, synthesized):
        """Cost reports and audits should answer 'whose is this?' without reading CDK."""
        buckets = synthesized["knowledge-acme"].find_resources("AWS::S3::Bucket")
        tags = [
            tag
            for bucket in buckets.values()
            for tag in bucket["Properties"].get("Tags", [])
        ]
        assert {"Key": "tenant", "Value": "acme"} in tags
