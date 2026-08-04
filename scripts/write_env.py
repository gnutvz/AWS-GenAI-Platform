"""Pull CloudFormation outputs into .env after a deploy.

`cdk deploy` prints the IDs the platform needs — knowledge base, buckets,
guardrail, function URL — and then they scroll away. Copying six values out of
console by hand is the step where a working deploy still leaves you unable to
run anything.

    python scripts/write_env.py --tenant acme     # writes .env
    python scripts/write_env.py --tenant acme --print

Existing keys are updated in place; unrelated lines and comments are preserved.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from aiplat.tenants import load_all

# Which stack output maps to which .env key. Output keys come from the CfnOutput
# logical IDs in infra/stacks/.
OUTPUT_TO_ENV = {
    "KnowledgeBaseId": "KNOWLEDGE_BASE_ID",
    "DocumentsBucketName": "DOCUMENTS_BUCKET",
    "SessionBucketName": "SESSION_BUCKET",
    "GuardrailIdOutput": "GUARDRAIL_ID",
    "GuardrailVersionOutput": "GUARDRAIL_VERSION",
    "AgentFunctionUrl": "AGENT_FUNCTION_URL",
    "OtlpEndpoint": "OTEL_EXPORTER_OTLP_ENDPOINT",
}

# Shared across tenants.
SHARED_STACKS = ["Safety", "Observability"]
# One set per tenant — .env points at exactly one, since the CLI and eval runner
# each talk to a single deployment.
TENANT_STACKS = ["Knowledge", "Api"]


def stack_names(prefix: str, tenant: str) -> list[str]:
    return [f"{prefix}-{s}" for s in SHARED_STACKS] + [
        f"{prefix}-{s}-{tenant}" for s in TENANT_STACKS
    ]


def collect(prefix: str, region: str, tenant: str) -> dict[str, str]:
    """Read outputs from the shared stacks plus one tenant's. Missing ones are skipped."""
    cfn = boto3.client("cloudformation", region_name=region)
    values: dict[str, str] = {"TENANT": tenant}

    for stack_name in stack_names(prefix, tenant):
        try:
            stacks = cfn.describe_stacks(StackName=stack_name)["Stacks"]
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            message = exc.response.get("Error", {}).get("Message", "")
            # Observability is opt-in, so its absence is normal — say so once
            # rather than looking like a failure.
            if code == "ValidationError" and "does not exist" in message:
                print(f"  {stack_name}: not deployed, skipping", file=sys.stderr)
                continue
            raise

        for output in stacks[0].get("Outputs", []):
            env_key = OUTPUT_TO_ENV.get(output["OutputKey"])
            if env_key:
                values[env_key] = output["OutputValue"]
        print(f"  {stack_name}: ok", file=sys.stderr)

    return values


def merge_into_env(path: Path, values: dict[str, str]) -> tuple[int, int]:
    """Update keys in place, append the rest. Returns (updated, added)."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(values)
    updated = 0

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in remaining:
            lines[i] = f"{key}={remaining.pop(key)}"
            updated += 1

    if remaining:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("# Written by scripts/write_env.py")
        lines.extend(f"{k}={v}" for k, v in remaining.items())

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return updated, len(remaining)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", help="Which tenant to point .env at")
    parser.add_argument("--prefix", default="AiPlat", help="CDK stack name prefix")
    parser.add_argument("--region", help="Defaults to the configured region")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--print", action="store_true", dest="print_only")
    args = parser.parse_args(argv)

    # Checked before anything AWS-shaped: it costs nothing and is the mistake a
    # first-time user actually makes.
    tenant = args.tenant or os.environ.get("TENANT", "")
    if not tenant:
        known = ", ".join(t.slug for t in load_all()) or "none defined"
        raise SystemExit(
            f"Which tenant? Pass --tenant (or set TENANT). Defined tenants: {known}"
        )

    session = boto3.Session()
    # AWS_REGION is what CDK and the Lambda runtime use; boto3 resolves
    # AWS_DEFAULT_REGION and the shared config file. Accept all three.
    region = args.region or os.environ.get("AWS_REGION") or session.region_name
    if not region:
        raise SystemExit("No region configured. Set AWS_REGION or run `aws configure`.")
    if session.get_credentials() is None:
        raise SystemExit("No AWS credentials found. Run `aws configure` first.")

    print(f"Reading stack outputs for tenant {tenant!r} in {region}:", file=sys.stderr)
    values = collect(args.prefix, region, tenant)

    # TENANT alone is not evidence anything is deployed.
    if len(values) <= 1:
        raise SystemExit(
            f"No stacks found for tenant {tenant!r} with prefix {args.prefix!r}. "
            f"Deploy first: make deploy"
        )

    if args.print_only:
        for key, value in values.items():
            print(f"{key}={value}")
        return 0

    if not args.env_file.exists():
        example = Path(".env.example")
        if example.exists():
            args.env_file.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
            print(f"Created {args.env_file} from .env.example", file=sys.stderr)

    updated, added = merge_into_env(args.env_file, values)
    print(f"\n{args.env_file}: {updated} updated, {added} added", file=sys.stderr)
    print("Next: make ask Q=\"...\"", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
