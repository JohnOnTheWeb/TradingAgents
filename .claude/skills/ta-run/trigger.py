"""ta-run skill helper: SigV4-sign and POST /runs or GET /runs/{arn}.

Uses boto3's SigV4Auth so AWS_PROFILE=IGENV credentials are picked up
from the shared credentials file. execute-api is the signing service.

Usage:
    python trigger.py --body '<json body>'           # POST /runs
    python trigger.py --status <executionArn>        # GET /runs/<arn>
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.parse
from typing import Any, Dict, Optional

import boto3
import botocore.auth
import botocore.awsrequest
import requests


def _resolve_api_url() -> str:
    url = os.environ.get("TA_API_URL")
    if url:
        return url.rstrip("/")
    profile = os.environ.get("AWS_PROFILE", "IGENV")
    out = subprocess.check_output(
        [
            "aws", "cloudformation", "describe-stacks",
            "--stack-name", "TradingAgentsStack",
            "--query", "Stacks[0].Outputs[?OutputKey=='WebApiUrl'].OutputValue",
            "--output", "text",
            "--profile", profile,
        ],
        text=True,
    ).strip()
    if not out or out == "None":
        sys.exit(
            "could not resolve WebApiUrl from TradingAgentsStack outputs. "
            "Set TA_API_URL env var or deploy the stack with -c apiEnabled=true."
        )
    os.environ["TA_API_URL"] = out
    return out.rstrip("/")


def _session() -> boto3.Session:
    profile = os.environ.get("AWS_PROFILE", "IGENV")
    return boto3.Session(profile_name=profile)


def _sigv4_request(method: str, url: str, body: Optional[str]) -> requests.Response:
    session = _session()
    creds = session.get_credentials().get_frozen_credentials()
    region = session.region_name or "us-east-1"

    req = botocore.awsrequest.AWSRequest(
        method=method,
        url=url,
        data=body or "",
        headers={"Content-Type": "application/json"} if body else {},
    )
    botocore.auth.SigV4Auth(creds, "execute-api", region).add_auth(req)

    prepared = requests.Request(
        method=method,
        url=url,
        headers=dict(req.headers.items()),
        data=body or None,
    ).prepare()
    with requests.Session() as s:
        return s.send(prepared, timeout=30)


def cmd_post(body: str) -> int:
    # Validate JSON up-front so we fail loud locally, not via API 400.
    try:
        json.loads(body)
    except json.JSONDecodeError as err:
        sys.exit(f"--body is not valid JSON: {err}")

    url = f"{_resolve_api_url()}/runs"
    resp = _sigv4_request("POST", url, body)
    try:
        parsed = resp.json()
    except ValueError:
        parsed = {"raw": resp.text}

    print(json.dumps(parsed, indent=2))
    if resp.status_code >= 400:
        return 1

    arn = parsed.get("executionArn")
    if arn:
        profile = os.environ.get("AWS_PROFILE", "IGENV")
        print(
            f"\nPoll with:\n  aws stepfunctions describe-execution "
            f"--execution-arn {arn} --profile {profile} --query 'status'",
            file=sys.stderr,
        )
    return 0


def cmd_status(execution_arn: str) -> int:
    encoded = urllib.parse.quote(execution_arn, safe="")
    url = f"{_resolve_api_url()}/runs/{encoded}"
    resp = _sigv4_request("GET", url, None)
    try:
        parsed = resp.json()
    except ValueError:
        parsed = {"raw": resp.text}
    print(json.dumps(parsed, indent=2, default=str))
    return 0 if resp.status_code < 400 else 1


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="ta-run trigger")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--body", help="JSON body for POST /runs")
    group.add_argument("--status", help="executionArn to GET /runs/<arn>")
    args = p.parse_args(argv)

    if args.body:
        return cmd_post(args.body)
    return cmd_status(args.status)


if __name__ == "__main__":
    sys.exit(main())
