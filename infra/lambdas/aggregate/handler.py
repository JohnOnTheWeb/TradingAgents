"""Step 3 of the state machine: aggregate per-ticker results → summary report.

With the invoke step running on Fargate instead of a Lambda, we can't
collect per-ticker results via Map output (Fargate tasks don't return
payload). Instead every Fargate task writes its result JSON to
``s3://<TA_CONFIG_BUCKET>/runs/<run_id>/<TICKER>.json`` and this
aggregator reads them by listing the prefix.

Input:
    {
        "run_id": "<uuid>",
        "trade_date": "2026-04-30",
        "tickers": [{"symbol": "AMZN", ...}, ...],
        "config_bucket": "ta-config-<account>"
    }

Environment:
    MD_STORE_ENDPOINT        md-store MCP endpoint URL
    MD_STORE_SECRET_ID       Secrets Manager id holding the bearer token
    MD_STORE_AGENT_ID        agent id header (default tauric-traders)
    SNS_NOTIFICATIONS_TOPIC  success-notification SNS topic ARN
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

import boto3

logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_sns = boto3.client("sns")
_secrets = boto3.client("secretsmanager")
_s3 = boto3.client("s3")

_cached_bearer: Optional[str] = None


def _bearer() -> str:
    global _cached_bearer
    if _cached_bearer:
        return _cached_bearer
    secret_id = os.environ["MD_STORE_SECRET_ID"]
    raw = _secrets.get_secret_value(SecretId=secret_id).get("SecretString") or ""
    token = raw
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            token = parsed.get(
                os.environ.get("MD_STORE_SECRET_JSON_KEY", "bearer"), raw
            )
    except json.JSONDecodeError:
        pass
    _cached_bearer = str(token).strip()
    return _cached_bearer


def _write_md_store(key: str, content: str) -> None:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "write_file",
            "arguments": {"key": key, "content": content},
        },
    }
    endpoint = os.environ.get(
        "MD_STORE_ENDPOINT",
        "https://jjjtiltcja.execute-api.us-east-1.amazonaws.com/prod/mcp/v2",
    )
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_bearer()}",
        "X-Agent-Id": os.environ.get("MD_STORE_AGENT_ID", "tauric-traders"),
    }
    req = urllib.request.Request(
        endpoint, data=json.dumps(payload).encode("utf-8"),
        headers=headers, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as err:
        raise RuntimeError(f"md-store HTTP {err.code}: {err.read().decode()!r}")
    parsed = json.loads(body)
    if "error" in parsed:
        raise RuntimeError(f"md-store rpc error: {parsed['error']}")


def _fmt_usd(value: float) -> str:
    return f"${value:,.4f}"


def _load_ticker_result(
    bucket: str, run_id: str, ticker: str
) -> Dict[str, Any]:
    key = f"runs/{run_id}/{ticker}.json"
    try:
        obj = _s3.get_object(Bucket=bucket, Key=key)
    except _s3.exceptions.NoSuchKey:
        return {
            "ticker": ticker,
            "run_id": run_id,
            "status": "task_no_output",
            "error": (
                f"Fargate task for {ticker} did not write s3://{bucket}/{key}"
            ),
            "cost_usd": 0.0,
            "token_usage": [],
        }
    body = obj["Body"].read().decode("utf-8")
    try:
        return json.loads(body)
    except json.JSONDecodeError as err:
        return {
            "ticker": ticker,
            "run_id": run_id,
            "status": "result_parse_failed",
            "error": f"{type(err).__name__}: {err}",
            "cost_usd": 0.0,
            "token_usage": [],
        }


def _decision_oneline(decision: str, max_len: int = 140) -> str:
    """First non-empty line of the decision, escaped for a table cell."""
    text = (decision or "").strip()
    if not text:
        return "_no decision_"
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    # Escape pipe/newline chars that would break a Markdown table row.
    first = first.replace("|", "\\|")
    if len(first) > max_len:
        first = first[: max_len - 3] + "..."
    return first


def _strip_investment_thesis(text: str) -> str:
    """Drop the verbose '**Investment Thesis**: ...' paragraph from a PM
    decision so the cross-ticker summary stays readable. Keeps Rating,
    Executive Summary, Price Target, Time Horizon."""
    if not text:
        return text
    import re
    pattern = re.compile(
        r"\*\*Investment Thesis\*\*:.*?(?=\n\s*\*\*[A-Z][^*]*\*\*:|\Z)",
        re.DOTALL,
    )
    stripped = pattern.sub("", text)
    stripped = re.sub(r"\n{3,}", "\n\n", stripped).strip()
    return stripped


def _collapse_pm_header_fields(text: str) -> str:
    """Combine the PM decision's Rating, Price Target, and Time Horizon
    fields into a single inline header line."""
    if not text:
        return text
    field_pat = re.compile(
        r"(?m)^\*\*(Rating|Price Target|Time Horizon)\*\*:\s*([^\n]+)\s*$"
    )
    found: Dict[str, str] = {}
    positions: List[tuple] = []
    for m in field_pat.finditer(text):
        name, value = m.group(1), m.group(2).strip()
        found.setdefault(name, value)
        positions.append((m.start(), m.end()))
    if not positions:
        return text
    # Remove all matched lines (walk in reverse so offsets stay valid).
    out = text
    for start, end in reversed(positions):
        out = out[:start] + out[end:]
    out = re.sub(r"\n{3,}", "\n\n", out).lstrip("\n")
    parts = [
        f"**{k}**: {found[k]}"
        for k in ("Rating", "Price Target", "Time Horizon")
        if k in found
    ]
    header = "  |  ".join(parts)
    return f"{header}\n\n{out}".rstrip() + "\n"


def _tighten_paragraphs(text: str) -> str:
    """Replace paragraph breaks (blank lines) with Markdown hard line breaks
    so prose renders at roughly half the vertical spacing. Blank lines
    adjacent to headings, list items, table rows, and code fences are left
    alone so structural blocks still separate properly."""
    if not text:
        return text
    lines = text.split("\n")
    out: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.strip() == "" and out and i + 1 < len(lines):
            prev = out[-1].strip()
            nxt = lines[i + 1].strip()
            structural = lambda s: (
                not s
                or s.startswith(("#", ">", "- ", "* ", "|", "```", "~~~"))
                or bool(re.match(r"^\d+\.\s", s))
            )
            if structural(prev) or structural(nxt):
                out.append(line)
            else:
                # Collapse the blank line: append two trailing spaces to the
                # previous line to make a hard break, then drop the blank.
                if not out[-1].endswith("  "):
                    out[-1] = out[-1].rstrip() + "  "
        else:
            out.append(line)
        i += 1
    return "\n".join(out)


def _demote_markdown_headings(text: str, shift: int = 1) -> str:
    """Bump all ATX heading levels in ``text`` by ``shift`` so they nest
    cleanly under the summary's section heading."""
    if not text or shift <= 0:
        return text
    return re.sub(
        r"(?m)^(#{1,6}) ",
        lambda m: "#" * min(6, len(m.group(1)) + shift) + " ",
        text,
    )


_MARKET_CONCL_HEADING = re.compile(
    r"(?m)^## +(.*(?:conclusion|final (?:verdict|thoughts|recommendation"
    r"|conviction|synthesis|transaction)|bottom line).*)$",
    re.I,
)


def _extract_market_conclusion(market_report: str) -> str:
    """Pull the trailing conclusion section out of a market analyst report.

    The market prompt doesn't mandate a heading name, so we keyword-match on
    the last H2 that looks conclusion-like (``Conclusion``, ``Final X``,
    ``Bottom Line``, ``FINAL TRANSACTION PROPOSAL``). Falls back to the last
    H2 in the document if nothing matches.

    Trims at the first line-starting ``---`` horizontal rule AFTER the heading
    so trailing disclaimers and Summary/Metrics tables following the
    conclusion don't bleed in.
    """
    if not market_report:
        return ""
    matches = list(_MARKET_CONCL_HEADING.finditer(market_report))
    if matches:
        start = matches[-1].start()
    else:
        h2s = list(re.finditer(r"(?m)^## (.+)$", market_report))
        start = h2s[-1].start() if h2s else max(0, len(market_report) - 1500)
    body = market_report[start:]
    nl = body.find("\n")
    search_from = nl + 1 if nl >= 0 else 0
    hr = re.search(r"(?m)^---\s*$", body[search_from:])
    if hr:
        body = body[: search_from + hr.start()]
    return body.rstrip()


def _render_summary(
    trade_date: str, run_id: str, results: Iterable[Dict[str, Any]]
) -> str:
    items = list(results)
    successes = [r for r in items if r.get("status") == "success"]
    failures = [r for r in items if r.get("status") != "success"]
    total = round(sum(float(r.get("cost_usd", 0.0) or 0.0) for r in items), 4)

    lines: List[str] = []
    lines.append(f"# TradingAgents run summary — {trade_date}")
    lines.append("")
    lines.append(
        f"**Run ID:** `{run_id}`  **Tickers:** {len(items)}  "
        f"**Successes:** {len(successes)}  **Failures:** {len(failures)}  "
        f"**Generated:** {datetime.utcnow().isoformat(timespec='seconds')}Z"
    )
    lines.append("")
    lines.append("## Decisions at a glance")
    lines.append("")
    lines.append("| Ticker | Status | Decision | Cost (USD) | Report |")
    lines.append("|---|---|---|---:|---|")
    for r in items:
        ticker = str(r.get("ticker", "?")).upper()
        status = str(r.get("status", "?"))
        decision_line = _decision_oneline(str(r.get("decision", "") or ""))
        cost = _fmt_usd(float(r.get("cost_usd", 0.0) or 0.0))
        key = r.get("report_key")
        if key:
            filename = key.rsplit("/", 1)[-1]
            link = f"[{filename}]({filename})"
        else:
            link = "_no report_"
        lines.append(
            f"| {ticker} | {status} | {decision_line} | {cost} | {link} |"
        )
    lines.append(
        f"| **Total Bedrock cost** | | | **{_fmt_usd(total)}** | |"
    )
    lines.append("")

    # Full per-ticker conclusion blocks. Prefer the Portfolio Manager's full
    # final_trade_decision text (captured in the per-ticker S3 result); fall
    # back to the short label that signal_processing parsed out. The
    # Investment Thesis section is stripped here — it's too verbose for the
    # cross-ticker summary; readers can click through to the ticker report.
    lines.append("## Conclusions")
    lines.append("")
    for r in items:
        ticker = str(r.get("ticker", "?")).upper()
        full = str((r.get("final_state") or {}).get("final_trade_decision") or "").strip()
        short = str(r.get("decision", "") or "").strip()
        body = _strip_investment_thesis(full) if full else short
        body = _collapse_pm_header_fields(body).rstrip()
        market_report = str(
            (r.get("final_state") or {}).get("market_report") or ""
        )
        market_concl = _extract_market_conclusion(market_report)
        lines.append(f"### {ticker}")
        lines.append("")
        lines.append(_tighten_paragraphs(body) if body else "_no decision returned_")
        if market_concl:
            # Strip the section's leading H2 heading line ("## Conclusion" /
            # "## Final Thoughts" / etc.) so only the prose body appears
            # beneath the PM block. No sub-heading is inserted — per request.
            stripped = re.sub(r"^##\s+[^\n]*\n+", "", market_concl, count=1)
            stripped = _demote_markdown_headings(stripped, shift=3)
            lines.append("")
            lines.append(_tighten_paragraphs(stripped.rstrip()))
        lines.append("")

    if failures:
        lines.append("## Failures")
        lines.append("")
        for r in failures:
            ticker = str(r.get("ticker", "?")).upper()
            err = str(r.get("error", "unknown") or "unknown")
            lines.append(f"- **{ticker}**: {err}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def handler(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    trade_date = event["trade_date"]
    run_id = event["run_id"]
    bucket = event.get("config_bucket") or os.environ.get("TRADINGAGENTS_CONFIG_BUCKET")
    if not bucket:
        raise ValueError(
            "Aggregate input missing config_bucket and "
            "TRADINGAGENTS_CONFIG_BUCKET env var not set"
        )
    tickers_raw = event.get("tickers") or []

    tickers: List[str] = []
    for t in tickers_raw:
        if isinstance(t, str):
            tickers.append(t.upper())
        elif isinstance(t, dict) and t.get("symbol"):
            tickers.append(str(t["symbol"]).upper())

    results = [_load_ticker_result(bucket, run_id, t) for t in tickers]
    total_cost = round(sum(float(r.get("cost_usd", 0.0) or 0.0) for r in results), 4)
    successes = sum(1 for r in results if r.get("status") == "success")
    failures = len(results) - successes

    summary_key = "TauricTraders/_summary.md"
    _write_md_store(summary_key, _render_summary(trade_date, run_id, results))

    topic = os.environ.get("SNS_NOTIFICATIONS_TOPIC")
    if topic:
        report_lines = [
            f"- {r['ticker']}: {r.get('report_key') or '(no report)'} "
            f"[{r.get('status')}, {_fmt_usd(float(r.get('cost_usd') or 0.0))}]"
            for r in results
        ]
        conclusion_blocks: List[str] = []
        for r in results:
            ticker = str(r.get("ticker", "?")).upper()
            full = str((r.get("final_state") or {}).get("final_trade_decision") or "").strip()
            short = str(r.get("decision", "") or "").strip()
            pm_text = (_strip_investment_thesis(full) if full else short) or "(no decision)"
            pm_text = _collapse_pm_header_fields(pm_text)
            market_report = str(
                (r.get("final_state") or {}).get("market_report") or ""
            )
            market_text = _extract_market_conclusion(market_report)
            if market_text:
                # Drop the leading H2 heading so the market block reads as prose.
                market_text = re.sub(
                    r"^##\s+[^\n]*\n+", "", market_text, count=1
                )
            block = f"=== {ticker} ===\n{pm_text}"
            if market_text:
                block += f"\n{market_text}"
            conclusion_blocks.append(block)
        body = (
            f"TradingAgents run {run_id}\n"
            f"Date: {trade_date}\n"
            f"Tickers: {len(results)}  Successes: {successes}  Failures: {failures}\n"
            f"Total Bedrock cost: {_fmt_usd(total_cost)}\n\n"
            f"Reports (md-store keys under TauricTraders/):\n"
            + "\n".join(report_lines)
            + f"\n\nSummary: {summary_key}\n\n"
            "Conclusions:\n\n"
            + "\n\n".join(conclusion_blocks)
            + "\n"
        )
        subject = (
            f"TradingAgents run complete — {trade_date} — "
            f"{successes}/{len(results)} ok, {_fmt_usd(total_cost)}"
        )[:100]
        _sns.publish(TopicArn=topic, Subject=subject, Message=body)

    return {
        "run_id": run_id,
        "trade_date": trade_date,
        "summary_key": summary_key,
        "ticker_count": len(results),
        "successes": successes,
        "failures": failures,
        "total_cost_usd": total_cost,
    }
