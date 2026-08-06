"""Call the deployed agent from the command line.

The Function URL uses IAM auth, so a plain `curl` gets a 403 — every request has
to be SigV4-signed. That is the correct trade-off for an endpoint that spends
money on model tokens, but it needs a client. This is that client.

    python scripts/ask.py "What is the deploy procedure for perf-canary?"
    python scripts/ask.py --session-id demo-1 "And what rolls it back?"

Reads AGENT_FUNCTION_URL from the environment or .env, or takes --url.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

# Importing aiplat loads .env into the environment, which boto3 then reads for
# credentials and region. Kept as an explicit import so the reason is visible.
import aiplat  # noqa: F401

# Function URLs are signed against the Lambda service, not "execute-api".
SERVICE = "lambda"
TIMEOUT_SECONDS = 300


def sign(url: str, payload: dict, region: str) -> AWSRequest:
    session = boto3.Session()
    credentials = session.get_credentials()
    if credentials is None:
        raise SystemExit(
            "No AWS credentials found. Run `aws configure` (or `aws configure sso`) first."
        )

    request = AWSRequest(
        method="POST",
        url=url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    SigV4Auth(credentials.get_frozen_credentials(), SERVICE, region).add_auth(request)
    return request


def call(url: str, payload: dict, region: str) -> dict:
    signed = sign(url, payload, region)
    req = urllib.request.Request(
        url, data=signed.body, headers=dict(signed.headers), method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        if exc.code == 403:
            raise SystemExit(
                f"403 Forbidden. The credentials are valid but lack "
                f"lambda:InvokeFunctionUrl on this function.\n{body}"
            ) from exc
        raise SystemExit(f"HTTP {exc.code}: {body}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", help="The question to ask")
    parser.add_argument("--url", help="Agent Function URL (default: $AGENT_FUNCTION_URL)")
    parser.add_argument("--session-id", help="Continue an existing conversation")
    parser.add_argument("--tenant", default="cli", help="Stamped on traces")
    parser.add_argument("--json", action="store_true", help="Print the raw response")
    args = parser.parse_args(argv)

    url = args.url or os.environ.get("AGENT_FUNCTION_URL", "").strip()
    if not url:
        raise SystemExit(
            "No Function URL. Pass --url, or set AGENT_FUNCTION_URL in .env "
            "(it is a CDK output of the Api stack)."
        )

    region = os.environ.get("AWS_REGION") or boto3.Session().region_name
    if not region:
        raise SystemExit("No AWS region configured. Set AWS_REGION or run `aws configure`.")

    payload = {"prompt": args.prompt, "tenant": args.tenant}
    if args.session_id:
        payload["session_id"] = args.session_id

    result = call(url, payload, region)

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    if "error" in result:
        print(f"Error: {result['error']}", file=sys.stderr)
        return 1

    print(result.get("answer", ""))
    usage = result.get("usage") or {}
    if usage.get("total_tokens"):
        print(
            f"\n[{usage['input_tokens']} in / {usage['output_tokens']} out"
            f" = {usage['total_tokens']} tokens]",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
