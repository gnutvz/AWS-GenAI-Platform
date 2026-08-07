#!/usr/bin/env python3
"""Check a live AWS account against what this platform actually needs.

    python scripts/preflight.py
    python scripts/preflight.py --profile aiplat --region us-west-2

Every check is read-only except the model call, which spends a handful of tokens
because there is no way to prove model access without one. Nothing is created,
modified or deleted, so this is safe to run against an account you have only
been lent.

The point is to fail here rather than later. Most of what goes wrong on a first
deployment is not a bug — it is a permission nobody granted or a model nobody
enabled, and every one of those surfaces at a different moment far from its
cause. `bedrock:InvokeModel` denied looks like the agent is broken. A knowledge
base with no synced data source looks like retrieval is bad. This turns each of
them into one line saying which console page to open.

Exit status is 0 only if everything required passed, so it can gate a pipeline.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

# Run directly as a script, so the repo root is not on sys.path by default.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiplat.aws import boto_config
from aiplat.config import settings

PASS, FAIL, SKIP, WARN = "PASS", "FAIL", "SKIP", "WARN"

ICON = {PASS: "\033[32m✓\033[0m", FAIL: "\033[31m✗\033[0m", SKIP: "\033[90m–\033[0m", WARN: "\033[33m!\033[0m"}


@dataclass
class Result:
    status: str
    detail: str
    # What to do about it. Only ever set on FAIL and WARN — a passing check that
    # explains how to fix itself is noise.
    fix: str = ""


@dataclass
class Check:
    name: str
    run: Callable[[], Result]
    # A failure here means later checks cannot be trusted, so stop.
    fatal: bool = False
    # Not required for the platform to work, so it cannot fail the run.
    optional: bool = False


@dataclass
class Report:
    results: list[tuple[Check, Result]] = field(default_factory=list)

    def add(self, check: Check, result: Result) -> None:
        self.results.append((check, result))
        print(f"  {ICON[result.status]} {check.name:<34} {result.detail}")
        if result.fix:
            for line in result.fix.splitlines():
                print(f"      \033[90m{line}\033[0m")

    @property
    def failed(self) -> list[tuple[Check, Result]]:
        return [(c, r) for c, r in self.results if r.status == FAIL and not c.optional]


def describe(exc: Exception) -> str:
    """AWS errors are long and the useful part is at the front."""
    if isinstance(exc, ClientError):
        err = exc.response.get("Error", {})
        return f"{err.get('Code', 'Error')}: {err.get('Message', '')[:160]}"
    return f"{type(exc).__name__}: {str(exc)[:160]}"


def is_denied(exc: Exception) -> bool:
    if not isinstance(exc, ClientError):
        return False
    return exc.response.get("Error", {}).get("Code", "") in (
        "AccessDeniedException",
        "AccessDenied",
        "UnauthorizedOperation",
        "ForbiddenException",
    )


def build_checks(session: boto3.Session, region: str) -> list[Check]:
    cfg = settings()
    state: dict = {}

    def identity() -> Result:
        try:
            who = session.client("sts", config=boto_config()).get_caller_identity()
        except NoCredentialsError:
            return Result(
                FAIL,
                "no credentials found",
                fix="A console password cannot be used here — the CLI needs an access key or SSO.\n"
                "  aws configure --profile <name>     then rerun with --profile <name>\n"
                "  aws configure sso                  if the account uses Identity Center",
            )
        except (ClientError, BotoCoreError) as exc:
            return Result(FAIL, describe(exc))

        state["account"] = who["Account"]
        arn = who["Arn"]
        # A root-account key is worth saying out loud: it cannot be scoped and
        # cannot be revoked without disrupting everything else in the account.
        if ":root" in arn:
            return Result(
                WARN,
                f"account {who['Account']} as ROOT",
                fix="Root credentials cannot be scoped or revoked cleanly. Create an IAM\n"
                "user or role with the policy from docs/setup-guide.pdf instead.",
            )
        return Result(PASS, f"account {who['Account']} as {arn.rsplit('/', 1)[-1]}")

    def model_enabled() -> Result:
        """Listing is not access. Only a call proves it."""
        model = cfg.model_id
        try:
            client = session.client("bedrock-runtime", config=boto_config())
            client.converse(
                modelId=model,
                messages=[{"role": "user", "content": [{"text": "hi"}]}],
                inferenceConfig={"maxTokens": 1},
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "AccessDeniedException":
                return Result(
                    FAIL,
                    f"{model} not accessible",
                    fix="Bedrock console -> Model access -> enable this model, then wait a\n"
                    "minute. Enabling is per-region, so check the region matches.",
                )
            if code == "ValidationException":
                return Result(
                    FAIL,
                    f"{model} rejected: {describe(exc)}",
                    fix="Usually the model id is not offered in this region, or it needs an\n"
                    "inference profile prefix. aws bedrock list-inference-profiles",
                )
            return Result(FAIL, describe(exc))
        except (BotoCoreError, Exception) as exc:  # noqa: BLE001
            return Result(FAIL, describe(exc))
        return Result(PASS, f"{model} answers")

    def knowledge_base() -> Result:
        if not cfg.knowledge_base_id:
            return Result(
                SKIP,
                "KNOWLEDGE_BASE_ID not set",
                fix="Without it the agent answers from the model alone and cites nothing.",
            )
        try:
            client = session.client("bedrock-agent", config=boto_config())
            kb = client.get_knowledge_base(knowledgeBaseId=cfg.knowledge_base_id)[
                "knowledgeBase"
            ]
        except ClientError as exc:
            if is_denied(exc):
                return Result(
                    FAIL,
                    "denied",
                    fix="The policy needs bedrock:GetKnowledgeBase, or at minimum\n"
                    "bedrock:Retrieve if you only ever query it.",
                )
            return Result(FAIL, describe(exc))

        state["kb_ok"] = kb["status"] == "ACTIVE"
        if kb["status"] != "ACTIVE":
            return Result(WARN, f"status {kb['status']}", fix="Retrieval fails until ACTIVE.")
        return Result(PASS, f"{kb['name']} ACTIVE")

    def data_source_synced() -> Result:
        """An empty index is the failure that looks most like bad retrieval."""
        if not cfg.knowledge_base_id:
            return Result(SKIP, "no knowledge base")
        try:
            client = session.client("bedrock-agent", config=boto_config())
            sources = client.list_data_sources(knowledgeBaseId=cfg.knowledge_base_id)[
                "dataSourceSummaries"
            ]
        except ClientError as exc:
            if is_denied(exc):
                return Result(WARN, "denied", fix="Needs bedrock:ListDataSources.")
            return Result(WARN, describe(exc))

        if not sources:
            return Result(
                FAIL,
                "no data source",
                fix="The knowledge base has nothing attached to index. Add an S3 data\n"
                "source pointing at the documents bucket.",
            )
        return Result(PASS, f"{len(sources)} data source(s)")

    def retrieval_works() -> Result:
        if not cfg.knowledge_base_id:
            return Result(SKIP, "no knowledge base")
        try:
            client = session.client("bedrock-agent-runtime", config=boto_config())
            hits = client.retrieve(
                knowledgeBaseId=cfg.knowledge_base_id,
                retrievalQuery={"text": "test"},
                retrievalConfiguration={
                    "vectorSearchConfiguration": {"numberOfResults": 1}
                },
            )["retrievalResults"]
        except ClientError as exc:
            if is_denied(exc):
                return Result(FAIL, "denied", fix="The policy needs bedrock:Retrieve.")
            return Result(FAIL, describe(exc))

        if not hits:
            return Result(
                WARN,
                "reachable but empty",
                fix="Permissions are fine and the index has nothing in it. Run an ingest\n"
                "and start a sync: make ingest TENANT=<slug>",
            )
        return Result(PASS, f"{len(hits)} result(s)")

    def guardrail() -> Result:
        if not cfg.guardrail_id:
            return Result(
                SKIP,
                "GUARDRAIL_ID not set",
                fix="Valid, and means answers carry no PII masking and no grounding check.",
            )
        try:
            client = session.client("bedrock", config=boto_config())
            gr = client.get_guardrail(
                guardrailIdentifier=cfg.guardrail_id, guardrailVersion=cfg.guardrail_version
            )
        except ClientError as exc:
            if is_denied(exc):
                return Result(WARN, "denied", fix="Needs bedrock:GetGuardrail to verify.")
            return Result(FAIL, describe(exc))
        return Result(PASS, f"{gr['name']} v{cfg.guardrail_version} {gr['status']}")

    def bucket(attr: str, label: str) -> Callable[[], Result]:
        def run() -> Result:
            name = getattr(cfg, attr)
            if not name:
                return Result(SKIP, f"{label} not set")
            try:
                session.client("s3", config=boto_config()).head_bucket(Bucket=name)
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code in ("403", "AccessDenied"):
                    return Result(
                        FAIL, f"{name}: denied", fix="The policy needs s3 access to this bucket."
                    )
                if code in ("404", "NoSuchBucket"):
                    return Result(FAIL, f"{name}: does not exist")
                return Result(FAIL, describe(exc))
            return Result(PASS, name)

        return run

    def region_match() -> Result:
        """A resource in another region reads as a permissions problem for hours."""
        if region != cfg.region:
            return Result(
                WARN,
                f"session {region} vs config {cfg.region}",
                fix="Bedrock model access and every resource id are per-region.",
            )
        return Result(PASS, region)

    return [
        Check("credentials", identity, fatal=True),
        Check("region", region_match),
        Check("model access", model_enabled),
        Check("documents bucket", bucket("documents_bucket", "DOCUMENTS_BUCKET")),
        Check("session bucket", bucket("session_bucket", "SESSION_BUCKET"), optional=True),
        Check("knowledge base", knowledge_base),
        Check("data source", data_source_synced),
        Check("retrieval", retrieval_works),
        Check("guardrail", guardrail, optional=True),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", help="AWS profile to use")
    parser.add_argument("--region", help="Override the region from .env")
    args = parser.parse_args()

    cfg = settings()
    region = args.region or cfg.region
    session = boto3.Session(profile_name=args.profile, region_name=region)

    print(f"\nPreflight against {region}" + (f" (profile {args.profile})" if args.profile else ""))
    print()

    report = Report()
    for check in build_checks(session, region):
        try:
            result = check.run()
        except Exception as exc:  # noqa: BLE001
            result = Result(FAIL, describe(exc))
        report.add(check, result)
        if check.fatal and result.status == FAIL:
            print("\nStopping: nothing below can be checked without credentials.\n")
            return 1

    print()
    if report.failed:
        print(f"\033[31m{len(report.failed)} required check(s) failed.\033[0m\n")
        return 1
    print("\033[32mReady.\033[0m Next: make ingest TENANT=<slug>, then make chat\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
