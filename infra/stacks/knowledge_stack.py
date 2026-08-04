"""Documents bucket + vector store + Bedrock Knowledge Base.

Vector store is **S3 Vectors**, not OpenSearch Serverless. That is the single most
consequential choice in this stack: OpenSearch Serverless bills a minimum OCU floor
around the clock whether or not anyone asks a question — a few hundred dollars a
month for an idle reference environment. S3 Vectors bills storage and queries, so an
idle platform costs approximately nothing.

The trade-off is real and worth stating out loud: S3 Vectors has higher query latency
than a warm OpenSearch cluster and fewer knobs (no custom analyzers, no BM25 tuning).
For sub-100ms retrieval at sustained QPS, switch `storage_configuration` to
OpenSearch Serverless — the Knowledge Base, data source and agent code are unchanged.

**One stack per tenant.** Each tenant gets its own knowledge base, vector index and
documents bucket, rather than sharing an index and filtering by tenant on retrieval.
Two reasons. Isolation becomes an IAM boundary — a tenant's agent role names exactly
one knowledge base ARN, so cross-tenant retrieval is not a bug that can be written.
And the embedding model becomes a per-tenant choice, which a shared index could not
offer at all; that matters as soon as one corpus is Japanese and another is English.

This is affordable only because the vector store is S3 Vectors. With a per-index
standing cost, one knowledge base per tenant would be a non-starter.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, RemovalPolicy, Stack, Tags
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3vectors as s3vectors
from constructs import Construct

from aiplat.tenants import Tenant

# Bedrock stores the chunk text under this key; it must not be filterable.
BEDROCK_TEXT_KEY = "AMAZON_BEDROCK_TEXT"


class KnowledgeStack(Stack):
    def __init__(
        self, scope: Construct, construct_id: str, *, tenant: Tenant, **kwargs
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.tenant = tenant
        # Every resource in this stack belongs to one tenant — tag it so cost
        # reports and audits can answer "whose is this?" without reading CDK.
        Tags.of(self).add("tenant", tenant.slug)

        # Changing this means re-indexing the tenant, which is possible precisely
        # because knowledge bases are not shared.
        embedding_model, embedding_dimension = tenant.embedding

        # --- Source documents ------------------------------------------------
        self.documents_bucket = s3.Bucket(
            self,
            "DocumentsBucket",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=True,  # a bad ingest run should be recoverable
            removal_policy=RemovalPolicy.RETAIN,
        )

        # --- Vector store ----------------------------------------------------
        vector_bucket = s3vectors.CfnVectorBucket(
            self,
            "VectorBucket",
            vector_bucket_name=f"{self.stack_name.lower()}-vectors",
        )

        index = s3vectors.CfnIndex(
            self,
            "VectorIndex",
            index_name=f"{tenant.slug}-index",
            vector_bucket_arn=vector_bucket.attr_vector_bucket_arn,
            data_type="float32",
            dimension=embedding_dimension,
            distance_metric="cosine",
            metadata_configuration=s3vectors.CfnIndex.MetadataConfigurationProperty(
                non_filterable_metadata_keys=[BEDROCK_TEXT_KEY]
            ),
        )
        index.add_resource_dependency(vector_bucket)

        # --- Service role ----------------------------------------------------
        kb_role = iam.Role(
            self,
            "KnowledgeBaseRole",
            assumed_by=iam.ServicePrincipal(
                "bedrock.amazonaws.com",
                conditions={"StringEquals": {"aws:SourceAccount": self.account}},
            ),
            description="Lets Bedrock read source documents and write vectors",
        )
        self.documents_bucket.grant_read(kb_role)
        kb_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=[
                    f"arn:aws:bedrock:{self.region}::foundation-model/{embedding_model}"
                ],
            )
        )
        kb_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "s3vectors:GetIndex",
                    "s3vectors:QueryVectors",
                    "s3vectors:PutVectors",
                    "s3vectors:GetVectors",
                    "s3vectors:DeleteVectors",
                    "s3vectors:ListVectors",
                ],
                resources=[
                    vector_bucket.attr_vector_bucket_arn,
                    index.attr_index_arn,
                ],
            )
        )

        # --- Knowledge base --------------------------------------------------
        self.knowledge_base = bedrock.CfnKnowledgeBase(
            self,
            "KnowledgeBase",
            name=f"{self.stack_name}-kb",  # already carries the tenant slug
            role_arn=kb_role.role_arn,
            knowledge_base_configuration=bedrock.CfnKnowledgeBase.KnowledgeBaseConfigurationProperty(
                type="VECTOR",
                vector_knowledge_base_configuration=bedrock.CfnKnowledgeBase.VectorKnowledgeBaseConfigurationProperty(
                    embedding_model_arn=(
                        f"arn:aws:bedrock:{self.region}::foundation-model/{embedding_model}"
                    ),
                ),
            ),
            storage_configuration=bedrock.CfnKnowledgeBase.StorageConfigurationProperty(
                type="S3_VECTORS",
                s3_vectors_configuration=bedrock.CfnKnowledgeBase.S3VectorsConfigurationProperty(
                    vector_bucket_arn=vector_bucket.attr_vector_bucket_arn,
                    index_arn=index.attr_index_arn,
                ),
            ),
        )
        self.knowledge_base.node.add_dependency(index)

        # --- Data source -----------------------------------------------------
        # Hierarchical chunking keeps a parent-child relationship: children are
        # matched for precision, parents are returned for context. It costs more
        # storage than fixed-size chunking and is worth it for structured docs.
        data_source = bedrock.CfnDataSource(
            self,
            "DocumentsDataSource",
            knowledge_base_id=self.knowledge_base.attr_knowledge_base_id,
            name="documents",
            data_deletion_policy="RETAIN",
            data_source_configuration=bedrock.CfnDataSource.DataSourceConfigurationProperty(
                type="S3",
                s3_configuration=bedrock.CfnDataSource.S3DataSourceConfigurationProperty(
                    bucket_arn=self.documents_bucket.bucket_arn,
                    inclusion_prefixes=["documents/"],
                ),
            ),
            vector_ingestion_configuration=bedrock.CfnDataSource.VectorIngestionConfigurationProperty(
                chunking_configuration=bedrock.CfnDataSource.ChunkingConfigurationProperty(
                    chunking_strategy="HIERARCHICAL",
                    hierarchical_chunking_configuration=bedrock.CfnDataSource.HierarchicalChunkingConfigurationProperty(
                        level_configurations=[
                            bedrock.CfnDataSource.HierarchicalChunkingLevelConfigurationProperty(
                                max_tokens=1500
                            ),
                            bedrock.CfnDataSource.HierarchicalChunkingLevelConfigurationProperty(
                                max_tokens=300
                            ),
                        ],
                        overlap_tokens=60,
                    ),
                )
            ),
        )
        data_source.add_resource_dependency(self.knowledge_base)

        self.knowledge_base_id = self.knowledge_base.attr_knowledge_base_id

        CfnOutput(self, "KnowledgeBaseId", value=self.knowledge_base_id)
        CfnOutput(self, "DocumentsBucketName", value=self.documents_bucket.bucket_name)
