"""Agent compute: a Lambda function behind an IAM-authenticated Function URL.

Lambda over Fargate because the agent is stateless and traffic is bursty — idle cost
is zero and there is no cluster to keep alive. The limits you are accepting: a 15
minute ceiling, no token streaming through a buffered Function URL, and cold starts
in the low seconds. All three are survivable for internal tools and none of them
require an application rewrite to escape (see services/agent/agentcore_app.py).

Function URL is IAM-authenticated rather than public. A public LLM endpoint is an
open invitation to run up someone else's Bedrock bill.

Authentication answers who may call. It does not answer how often, and the two
failures look nothing alike on an invoice: an authorised client in a retry loop
spends real money without anything looking wrong. Hence the concurrency cap
below — the second half of the same argument.
"""

from __future__ import annotations

from pathlib import Path

from aws_cdk import BundlingOptions, CfnOutput, Duration, RemovalPolicy, Stack, Tags
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from constructs import Construct

from aiplat.tenants import Tenant

REPO_ROOT = Path(__file__).resolve().parents[2]

# Ceiling on simultaneous agent invocations per tenant.
#
# Every concurrent execution is a model call in flight, so this is the only place
# that bounds how fast one tenant can spend. Without it a client stuck in a retry
# loop scales straight to the account limit, and Bedrock bills every token of it.
#
# The number is deliberately small. Reserved concurrency is taken from the
# account pool (1,000 by default and shared with every other function in the
# account), so this does not scale to hundreds of tenants unmodified — at that
# point either raise the account limit or move to AgentCore Runtime, which is the
# migration this stack already leaves open. Override per deployment with
# `cdk deploy -c agent_concurrency=N`.
DEFAULT_AGENT_CONCURRENCY = 10


class ApiStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        tenant: Tenant,
        knowledge_base_id: str,
        documents_bucket_name: str,
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

        # Conversation state. Lifecycle rule rather than manual cleanup: sessions are
        # cheap to keep for a month and pointless to keep forever.
        session_bucket = s3.Bucket(
            self,
            "SessionBucket",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(30))],
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        # Declared explicitly rather than via log_retention so the retention policy
        # is plain CloudFormation instead of a custom-resource Lambda.
        log_group = logs.LogGroup(
            self,
            "AgentLogGroup",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        agent_fn = lambda_.Function(
            self,
            "AgentFunction",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,  # cheaper per ms than x86
            handler="services.agent.lambda_handler.handler",
            code=lambda_.Code.from_asset(
                str(REPO_ROOT),
                # Keeps the local virtualenv and CDK output out of the asset hash, so
                # a `cdk synth` does not invalidate the bundle on every run.
                exclude=[
                    ".venv",
                    ".git",
                    "**/__pycache__",
                    "infra/cdk.out",
                    "evals/results",
                    "docs",
                ],
                bundling=BundlingOptions(
                    image=lambda_.Runtime.PYTHON_3_12.bundling_image,
                    command=[
                        "bash",
                        "-c",
                        # Install the package and its deps, then drop in the source so
                        # `services.*` and `aiplat.*` resolve as top-level modules.
                        (
                            "pip install --no-cache-dir . -t /asset-output "
                            "&& cp -r aiplat services /asset-output/"
                        ),
                    ],
                ),
            ),
            # Long enough for a multi-tool reasoning loop, short enough that a runaway
            # agent stops burning tokens.
            timeout=Duration.minutes(5),
            memory_size=1024,
            # Caps spend rate, not just load. See DEFAULT_AGENT_CONCURRENCY.
            reserved_concurrent_executions=(
                self.node.try_get_context("agent_concurrency") or DEFAULT_AGENT_CONCURRENCY
            ),
            log_group=log_group,
            environment={
                "TENANT": tenant.slug,
                "LLM_ROUTE": "bedrock",
                # The tenant's own choice wins over the deployment default. This
                # is the whole of per-tenant behaviour: one Lambda per tenant was
                # already the shape, so it is a value in an environment rather
                # than a lookup at request time.
                "MODEL_ID": tenant.agent.model or model_id,
                "PROMPT_NAME": tenant.agent.prompt,
                # Empty means "highest version on disk". Written as a string
                # because a Lambda environment has no other type.
                "PROMPT_VERSION": (
                    str(tenant.agent.prompt_version)
                    if tenant.agent.prompt_version is not None
                    else ""
                ),
                "KNOWLEDGE_BASE_ID": knowledge_base_id,
                "DOCUMENTS_BUCKET": documents_bucket_name,
                "SESSION_BUCKET": session_bucket.bucket_name,
                "GUARDRAIL_ID": guardrail_id,
                "GUARDRAIL_VERSION": guardrail_version,
                "OTEL_EXPORTER_OTLP_ENDPOINT": otlp_endpoint,
                "OTEL_EXPORTER_OTLP_HEADERS": otlp_headers,
                "OTEL_SERVICE_NAME": "aiplat-agent",
            },
        )

        session_bucket.grant_read_write(agent_fn)

        # Inference is scoped to foundation models and inference profiles rather than
        # "*" so a compromised function cannot reach the control plane.
        agent_fn.add_to_role_policy(
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
        agent_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:Retrieve"],
                resources=[
                    f"arn:aws:bedrock:{self.region}:{self.account}:knowledge-base/{knowledge_base_id}"
                ],
            )
        )
        if guardrail_id:
            agent_fn.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["bedrock:ApplyGuardrail"],
                    resources=[
                        f"arn:aws:bedrock:{self.region}:{self.account}:guardrail/{guardrail_id}"
                    ],
                )
            )

        function_url = agent_fn.add_function_url(
            auth_type=lambda_.FunctionUrlAuthType.AWS_IAM,
        )

        self.function_url = function_url.url
        self.session_bucket = session_bucket

        CfnOutput(self, "AgentFunctionUrl", value=function_url.url)
        CfnOutput(self, "AgentFunctionName", value=agent_fn.function_name)
        CfnOutput(self, "SessionBucketName", value=session_bucket.bucket_name)
