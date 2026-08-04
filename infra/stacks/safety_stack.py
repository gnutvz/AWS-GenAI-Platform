"""Bedrock Guardrail.

Separated from the agent on purpose. Safety policy changes on a different cadence
than application code and is usually owned by a different team — coupling them means
a prompt tweak and a policy change ship together, which is exactly what an auditor
will object to.

Contextual grounding is the part that earns its keep in a RAG system: it scores
whether the answer is actually supported by retrieved passages and blocks it if not.
That is a hallucination control with a number attached, which is what enterprise
reviewers ask for.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Stack
from aws_cdk import aws_bedrock as bedrock
from constructs import Construct


class SafetyStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        guardrail = bedrock.CfnGuardrail(
            self,
            "Guardrail",
            name=f"{self.stack_name}-guardrail",
            description="Baseline safety and grounding policy for the platform",
            blocked_input_messaging="This request cannot be processed.",
            blocked_outputs_messaging="The response was withheld by the safety policy.",
            content_policy_config=bedrock.CfnGuardrail.ContentPolicyConfigProperty(
                filters_config=[
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        type=filter_type, input_strength="HIGH", output_strength="HIGH"
                    )
                    for filter_type in ("SEXUAL", "VIOLENCE", "HATE", "INSULTS", "MISCONDUCT")
                ]
                + [
                    # Prompt attack filtering applies to input only — Bedrock rejects
                    # an output strength on this filter type.
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        type="PROMPT_ATTACK", input_strength="HIGH", output_strength="NONE"
                    )
                ]
            ),
            sensitive_information_policy_config=bedrock.CfnGuardrail.SensitiveInformationPolicyConfigProperty(
                pii_entities_config=[
                    # Anonymise rather than block: an answer with a masked phone number
                    # is still useful, a blocked answer is not.
                    bedrock.CfnGuardrail.PiiEntityConfigProperty(type=entity, action="ANONYMIZE")
                    for entity in (
                        "EMAIL",
                        "PHONE",
                        "NAME",
                        "ADDRESS",
                        "CREDIT_DEBIT_CARD_NUMBER",
                    )
                ]
            ),
            contextual_grounding_policy_config=bedrock.CfnGuardrail.ContextualGroundingPolicyConfigProperty(
                filters_config=[
                    # Is the answer supported by the retrieved passages?
                    bedrock.CfnGuardrail.ContextualGroundingFilterConfigProperty(
                        type="GROUNDING", threshold=0.75
                    ),
                    # Is the answer actually about what was asked?
                    bedrock.CfnGuardrail.ContextualGroundingFilterConfigProperty(
                        type="RELEVANCE", threshold=0.5
                    ),
                ]
            ),
        )

        version = bedrock.CfnGuardrailVersion(
            self,
            "GuardrailVersion",
            guardrail_identifier=guardrail.attr_guardrail_id,
            description="Pinned version consumed by the agent",
        )

        self.guardrail_id = guardrail.attr_guardrail_id
        self.guardrail_version = version.attr_version

        CfnOutput(self, "GuardrailIdOutput", value=self.guardrail_id)
        CfnOutput(self, "GuardrailVersionOutput", value=self.guardrail_version)
