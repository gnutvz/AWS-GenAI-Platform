"""Agent compute on AgentCore Runtime — the promotion path off Lambda.

Opt-in, because Lambda is the right default: idle cost is genuinely zero and the
agent is stateless. Take this path when one of Lambda's three ceilings starts to
bite — no token streaming through a buffered Function URL, a fifteen minute
timeout, and no managed identity for tools that need one.

What this stack demonstrates, and the reason the split was worth building
before it was needed: `services/agent/agent.py` does not change. The Lambda
handler and `agentcore_app.py` are two entrypoints onto one `build_agent()`, and
the environment below is the same environment `api_stack` sets. Moving runtime is
a deployment decision, which is the claim docs/platform.md makes and this is the
stack that makes it checkable.

**The image is not built here.** `aws_ecr_assets.DockerImageAsset` would build at
synth time, which would make `cdk synth` — and every test that synthesizes —
require Docker. So this stack creates the repository and points the runtime at a
tag in it; pushing the image is a separate step:

    make image-agent
    make push-agent TENANT=acme
    cdk deploy -c agentcore=true AiPlat-AgentCore-acme

**Not deployed or verified against a live account.** CDK synthesis proves the
template's shape, not that AgentCore accepts it. The execution role below mirrors
the Lambda's grants — the same model, retrieval and guardrail permissions, scoped
the same way — plus ECR pull and log writes. If the service turns out to want
more, that is a finding this repo has not made yet, and the README's "nothing
here has been deployed" caveat covers it.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, RemovalPolicy, Stack, Tags
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from constructs import Construct

from aiplat.tenants import Tenant

# Overridable with `-c agent_image_tag=<tag>`. A moving tag is convenient and a
# poor audit trail — pin a digest or an immutable tag for anything real.
DEFAULT_IMAGE_TAG = "latest"


class AgentCoreStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        tenant: Tenant,
        knowledge_base_id: str,
        documents_bucket_name: str,
        session_bucket_name: str,
        guardrail_id: str,
        guardrail_version: str,
        model_id: str,
        otlp_endpoint: str = "",
        otlp_headers: str = "",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.tenant = tenant
        Tags.of(self).add("tenant", tenant.slug)

        # Per tenant, like everything else: one tenant's image cannot be pulled by
        # another tenant's runtime, because the role below names one repository.
        repository = ecr.Repository(
            self,
            "AgentImage",
            repository_name=f"aiplat-agent-{tenant.slug}",
            image_scan_on_push=True,
            # Deleting the stack should not silently delete the image a running
            # runtime is still pulling.
            removal_policy=RemovalPolicy.RETAIN,
        )

        log_group = logs.LogGroup(
            self,
            "AgentCoreLogs",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        execution_role = iam.Role(
            self,
            "AgentCoreExecutionRole",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            description=f"AgentCore Runtime execution role for tenant '{tenant.slug}'",
        )

        repository.grant_pull(execution_role)
        log_group.grant_write(execution_role)

        # Identical scoping to the Lambda. Inference is bounded to foundation
        # models and inference profiles rather than "*", so a compromised runtime
        # cannot reach the Bedrock control plane.
        execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=[
                    f"arn:aws:bedrock:{self.region}::foundation-model/*",
                    f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/*",
                    # Cross-region and global profiles route to other regions.
                    f"arn:aws:bedrock:*:{self.account}:inference-profile/*",
                ],
            )
        )

        # One knowledge base ARN. The isolation boundary is the same whichever
        # runtime is serving — that is the property worth preserving across a
        # migration, and tests/test_tenancy.py checks it for both stacks.
        execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:Retrieve"],
                resources=[
                    f"arn:aws:bedrock:{self.region}:{self.account}:knowledge-base/{knowledge_base_id}"
                ],
            )
        )

        if guardrail_id:
            execution_role.add_to_policy(
                iam.PolicyStatement(
                    actions=["bedrock:ApplyGuardrail"],
                    resources=[
                        f"arn:aws:bedrock:{self.region}:{self.account}:guardrail/{guardrail_id}"
                    ],
                )
            )

        # Conversation state lives in the bucket api_stack already created, so the
        # same session survives the move between runtimes.
        execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"],
                resources=[
                    f"arn:aws:s3:::{session_bucket_name}",
                    f"arn:aws:s3:::{session_bucket_name}/*",
                ],
            )
        )

        image_tag = self.node.try_get_context("agent_image_tag") or DEFAULT_IMAGE_TAG

        runtime = agentcore.CfnRuntime(
            self,
            "AgentRuntime",
            # Hyphens are not allowed in a runtime name, unlike every other
            # resource here that carries the slug verbatim.
            agent_runtime_name=f"aiplat_agent_{tenant.slug.replace('-', '_')}",
            description=f"Agent runtime for tenant '{tenant.slug}'",
            role_arn=execution_role.role_arn,
            agent_runtime_artifact=agentcore.CfnRuntime.AgentRuntimeArtifactProperty(
                container_configuration=agentcore.CfnRuntime.ContainerConfigurationProperty(
                    container_uri=f"{repository.repository_uri}:{image_tag}",
                ),
            ),
            network_configuration=agentcore.CfnRuntime.NetworkConfigurationProperty(
                network_mode="PUBLIC",
            ),
            environment_variables={
                "TENANT": tenant.slug,
                "LLM_ROUTE": "bedrock",
                "MODEL_ID": model_id,
                "KNOWLEDGE_BASE_ID": knowledge_base_id,
                "DOCUMENTS_BUCKET": documents_bucket_name,
                "SESSION_BUCKET": session_bucket_name,
                "GUARDRAIL_ID": guardrail_id,
                "GUARDRAIL_VERSION": guardrail_version,
                "OTEL_EXPORTER_OTLP_ENDPOINT": otlp_endpoint,
                "OTEL_EXPORTER_OTLP_HEADERS": otlp_headers,
                "OTEL_SERVICE_NAME": "aiplat-agent",
            },
        )
        runtime.node.add_dependency(execution_role)

        self.repository = repository
        self.runtime = runtime

        CfnOutput(
            self,
            "AgentImageRepository",
            value=repository.repository_uri,
            description="Push the agent image here before deploying the runtime",
        )
        CfnOutput(
            self,
            "AgentRuntimeArn",
            value=runtime.attr_agent_runtime_arn,
            description="AgentCore Runtime ARN",
        )
