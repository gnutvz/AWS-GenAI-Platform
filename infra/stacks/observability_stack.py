"""Langfuse on Fargate — trace and cost visibility for every model call.

Opt-in (`cdk deploy -c observability=true`) because unlike the rest of this platform
it is NOT free at idle: an ALB is roughly $16/month regardless of traffic, and
Aurora Serverless v2 costs its minimum ACU whenever it is awake. Everything else here
scales to zero, so this is the one stack with a standing bill — deploy it when you
want prompt-level debugging, skip it while you are only kicking tyres.

Self-hosted rather than SaaS so traces and eval datasets stay inside the account.
That matters for the same reason the OTLP choice does: this data is the asset.

Note: this runs the single-container Langfuse v2 image, which needs only Postgres.
Langfuse v3 splits into web + worker and additionally requires ClickHouse, Redis and
S3 — a different and considerably larger stack.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_ecs_patterns as ecs_patterns
from aws_cdk import aws_rds as rds
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

LANGFUSE_IMAGE = "langfuse/langfuse:2"
LANGFUSE_PORT = 3000


class ObservabilityStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # One NAT gateway, not one per AZ. Halves the standing network cost and the
        # blast radius of a single-AZ outage is acceptable for an internal tool.
        vpc = ec2.Vpc(self, "Vpc", max_azs=2, nat_gateways=1)

        db_secret = rds.Credentials.from_generated_secret("langfuse")

        database = rds.DatabaseCluster(
            self,
            "Database",
            engine=rds.DatabaseClusterEngine.aurora_postgres(
                version=rds.AuroraPostgresEngineVersion.VER_16_6
            ),
            credentials=db_secret,
            writer=rds.ClusterInstance.serverless_v2("Writer"),
            serverless_v2_min_capacity=0,  # scales to zero when idle
            serverless_v2_max_capacity=2,
            serverless_v2_auto_pause_duration=Duration.minutes(10),
            default_database_name="langfuse",
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Signing keys for Langfuse sessions. Generated once and stored, never in code.
        app_secret = secretsmanager.Secret(
            self,
            "LangfuseAppSecret",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                password_length=32, exclude_punctuation=True
            ),
        )

        cluster = ecs.Cluster(
            self,
            "Cluster",
            vpc=vpc,
            container_insights_v2=ecs.ContainerInsights.ENABLED,
        )

        service = ecs_patterns.ApplicationLoadBalancedFargateService(
            self,
            "Langfuse",
            cluster=cluster,
            cpu=512,
            memory_limit_mib=1024,
            desired_count=1,
            public_load_balancer=True,
            # Without this a bad image rolls forward for up to three hours before
            # ECS gives up. Fail in minutes instead.
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
            # Single task, so never take the only one down mid-deploy.
            min_healthy_percent=100,
            max_healthy_percent=200,
            task_image_options=ecs_patterns.ApplicationLoadBalancedTaskImageOptions(
                image=ecs.ContainerImage.from_registry(LANGFUSE_IMAGE),
                container_port=LANGFUSE_PORT,
                environment={
                    "PORT": str(LANGFUSE_PORT),
                    "HOSTNAME": "0.0.0.0",
                    "TELEMETRY_ENABLED": "false",
                    # Sign-up stays open for the first user only; close it after you
                    # have created your account.
                    "AUTH_DISABLE_SIGNUP": "false",
                },
                secrets={
                    # Langfuse v2 accepts discrete DATABASE_* variables; only the
                    # password needs to come from Secrets Manager.
                    "DATABASE_PASSWORD": ecs.Secret.from_secrets_manager(
                        database.secret, "password"
                    ),
                    "NEXTAUTH_SECRET": ecs.Secret.from_secrets_manager(app_secret),
                    "SALT": ecs.Secret.from_secrets_manager(app_secret),
                },
            ),
        )

        # Langfuse expects a single DATABASE_URL. Build it from the cluster endpoint
        # rather than hardcoding, and inject the password from Secrets Manager.
        container = service.task_definition.default_container
        assert container is not None
        container.add_environment("DATABASE_HOST", database.cluster_endpoint.hostname)
        container.add_environment("DATABASE_PORT", str(database.cluster_endpoint.port))
        container.add_environment("DATABASE_USERNAME", "langfuse")
        container.add_environment("DATABASE_NAME", "langfuse")
        container.add_environment(
            "NEXTAUTH_URL", f"http://{service.load_balancer.load_balancer_dns_name}"
        )

        database.connections.allow_default_port_from(service.service, "Langfuse to Postgres")

        # Langfuse boots slowly on first run (it applies migrations), so the default
        # health check grace period is not enough.
        service.target_group.configure_health_check(
            path="/api/public/health",
            healthy_http_codes="200",
            interval=Duration.seconds(30),
            timeout=Duration.seconds(10),
            unhealthy_threshold_count=5,
        )

        self.endpoint = f"http://{service.load_balancer.load_balancer_dns_name}"

        CfnOutput(self, "LangfuseUrl", value=self.endpoint)
        CfnOutput(self, "OtlpEndpoint", value=f"{self.endpoint}/api/public/otel")
        CfnOutput(
            self,
            "NextStep",
            value=(
                "Create a Langfuse project, then set OTEL_EXPORTER_OTLP_HEADERS="
                "Authorization=Basic <base64 of publickey:secretkey> on the agent function"
            ),
        )
