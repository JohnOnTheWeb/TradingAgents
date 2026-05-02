"""Re-render a per-ticker report from a captured run fixture.

Iterate on ``report_format.render_ticker_report`` without running the
pipeline. Fixture source is the per-ticker result JSON the Fargate
task_runner writes to S3 at
``s3://<config-bucket>/runs/<run_id>/<ticker>.json`` — since the app
started including ``final_state`` + ``token_buckets`` in the result
event, these objects contain everything the renderer needs.

Usage:

    # Pull from S3 by run_id/ticker (inferred bucket: $TA_CONFIG_BUCKET
    # or ta-config-<account>)
    python -m tradingagents.agentcore.replay_report RUN_ID/NVDA

    # Or point directly at a fixture file
    python -m tradingagents.agentcore.replay_report --fixture path/to/NVDA.json

    # Diff against md-store's current copy
    python -m tradingagents.agentcore.replay_report RUN_ID/NVDA --diff

Output goes to ``./reports/<TICKER>_<YYYY-MM-DD>.md``.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from .report_format import render_ticker_report


def _load_fixture_from_s3(run_id: str, ticker: str) -> Dict[str, Any]:
    import boto3

    bucket = os.environ.get("TA_CONFIG_BUCKET")
    if not bucket:
        sts = boto3.client("sts")
        account = sts.get_caller_identity()["Account"]
        bucket = f"ta-config-{account}"
    key = f"runs/{run_id}/{ticker.upper()}.json"
    s3 = boto3.client("s3")
    obj = s3.get_object(Bucket=bucket, Key=key)
    return json.loads(obj["Body"].read().decode("utf-8"))


def _load_fixture_from_file(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _fetch_md_store_copy(ticker: str, trade_date: str) -> Optional[str]:
    """Return the current Markdown stored in md-store, or None on 404."""
    from .report_writer import (
        ReportWriteError,
        _agent_id,
        _endpoint,
        _load_bearer_token,
        _REPORT_PREFIX,
    )
    import urllib.error
    import urllib.request

    key = f"{_REPORT_PREFIX}{ticker.upper()}_{trade_date}.md"
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "read_file", "arguments": {"key": key}},
    }
    req = urllib.request.Request(
        _endpoint(),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_load_bearer_token()}",
            "X-Agent-Id": _agent_id(),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
    except (urllib.error.HTTPError, urllib.error.URLError, ReportWriteError):
        return None
    parsed = json.loads(body)
    if "error" in parsed:
        return None
    result = parsed.get("result", {})
    content = result.get("content")
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict) and "text" in first:
            return first["text"]
    if isinstance(content, str):
        return content
    return None


def _render(fixture: Dict[str, Any]) -> tuple[str, str, str]:
    ticker = str(fixture["ticker"]).upper()
    trade_date = fixture["trade_date"]
    markdown = render_ticker_report(
        ticker=ticker,
        trade_date=trade_date,
        run_id=fixture.get("run_id", ""),
        status=fixture.get("status", "success"),
        duration_seconds=float(fixture.get("duration_seconds", 0.0) or 0.0),
        final_state=fixture.get("final_state") or {},
        decision=fixture.get("decision", "") or "",
        token_buckets=fixture.get("token_buckets") or [],
    )
    return ticker, trade_date, markdown


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="replay_report", description=__doc__)
    p.add_argument(
        "ref",
        nargs="?",
        help="Run reference: <run_id>/<ticker>. Omit when using --fixture.",
    )
    p.add_argument(
        "--fixture",
        type=Path,
        help="Path to a local fixture JSON (overrides positional ref)",
    )
    p.add_argument(
        "--out-dir",
        type=Path,
        default=Path("reports"),
        help="Directory for rendered Markdown (default: ./reports)",
    )
    p.add_argument(
        "--diff",
        action="store_true",
        help="Diff rendered output against md-store's current copy",
    )
    args = p.parse_args(argv)

    if args.fixture:
        fixture = _load_fixture_from_file(args.fixture)
    elif args.ref:
        if "/" not in args.ref:
            p.error("ref must be in the form <run_id>/<ticker>")
        run_id, ticker = args.ref.split("/", 1)
        fixture = _load_fixture_from_s3(run_id, ticker)
    else:
        p.error("provide either a positional ref or --fixture")

    if not fixture.get("final_state"):
        print(
            "WARNING: fixture has no 'final_state' — likely from a run before "
            "the replay-capture change shipped. Report will be minimal.",
            file=sys.stderr,
        )

    ticker, trade_date, markdown = _render(fixture)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.out_dir / f"{ticker}_{trade_date}.md"
    out_path.write_text(markdown, encoding="utf-8")
    print(f"wrote {out_path} ({len(markdown):,} bytes)")

    if args.diff:
        current = _fetch_md_store_copy(ticker, trade_date)
        if current is None:
            print("no md-store copy to diff against", file=sys.stderr)
        else:
            diff = difflib.unified_diff(
                current.splitlines(keepends=True),
                markdown.splitlines(keepends=True),
                fromfile=f"md-store/{ticker}_{trade_date}.md",
                tofile=f"local/{ticker}_{trade_date}.md",
            )
            sys.stdout.writelines(diff)

    return 0


if __name__ == "__main__":
    sys.exit(main())
