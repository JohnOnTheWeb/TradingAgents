"""Render per-ticker and summary reports as Markdown.

Isolating the format here keeps ``app.py`` small and lets the aggregator
Lambda reuse :func:`render_summary` without pulling in FastAPI.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Iterable, List

from .bedrock_rates import summarize, total_cost


def _fmt_usd(value: float) -> str:
    return f"${value:,.4f}"


def _demote_headings(text: str, min_level: int = 2) -> str:
    """Demote any ATX headings in ``text`` so nested content can't collide
    with the document's single top-level H1. A line starting with ``# `` is
    rewritten to ``## `` (or deeper to preserve relative hierarchy); lines
    inside fenced code blocks are left alone.
    """
    if not text:
        return text
    out: List[str] = []
    in_fence = False
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        # Count leading '#' then require a space (ATX heading).
        i = 0
        while i < len(stripped) and stripped[i] == "#":
            i += 1
        if i > 0 and i <= 6 and i < len(stripped) and stripped[i] == " ":
            bump = max(0, min_level - i)
            if bump:
                line = line.replace("#" * i, "#" * (i + bump), 1)
        out.append(line)
    return "\n".join(out)


def render_ticker_report(
    *,
    ticker: str,
    trade_date: str,
    run_id: str,
    status: str,
    duration_seconds: float,
    final_state: Dict[str, Any],
    decision: str,
    token_buckets: List[Dict[str, Any]],
) -> str:
    """Render the per-ticker Markdown report written to md-store."""
    priced = summarize(token_buckets)
    total = total_cost(token_buckets)

    lines: List[str] = []
    # Single H1. Every section body gets _demote_headings applied so any
    # stray '# Foo' inside analyst output becomes H2+.
    lines.append(f"# {ticker.upper()} — {trade_date}")
    lines.append("")
    lines.append(
        f"**Run ID:** `{run_id}`  **Status:** {status}  "
        f"**Duration:** {duration_seconds:.1f}s  "
        f"**Generated:** {datetime.utcnow().isoformat(timespec='seconds')}Z"
    )
    lines.append("")

    # Conclusion at the top.
    lines.append("## Decision")
    lines.append("")
    lines.append(_demote_headings(decision.strip()) or "_no decision returned_")
    lines.append("")

    sections = [
        ("Market", "market_report"),
        ("Social / Sentiment", "sentiment_report"),
        ("News", "news_report"),
        ("Fundamentals", "fundamentals_report"),
        ("Investment plan (Research Manager)", "investment_plan"),
        ("Trader investment plan", "trader_investment_plan"),
        ("Final trade decision (Portfolio Manager)", "final_trade_decision"),
    ]
    lines.append("## Analyst reports")
    lines.append("")
    for title, key in sections:
        body = final_state.get(key)
        if not body:
            continue
        lines.append(f"### {title}")
        lines.append("")
        lines.append(_demote_headings(str(body).strip(), min_level=4))
        lines.append("")

    debate = final_state.get("investment_debate_state") or {}
    if debate.get("bull_history") or debate.get("bear_history"):
        lines.append("## Research debate")
        lines.append("")
        if debate.get("bull_history"):
            lines.append("### Bull")
            lines.append("")
            lines.append(_demote_headings(str(debate["bull_history"]).strip(), min_level=4))
            lines.append("")
        if debate.get("bear_history"):
            lines.append("### Bear")
            lines.append("")
            lines.append(_demote_headings(str(debate["bear_history"]).strip(), min_level=4))
            lines.append("")

    risk = final_state.get("risk_debate_state") or {}
    risk_bits = [risk.get(k) for k in (
        "aggressive_history", "conservative_history", "neutral_history"
    )]
    if any(risk_bits):
        lines.append("## Risk discussion")
        lines.append("")
        for label, key in (
            ("Aggressive", "aggressive_history"),
            ("Conservative", "conservative_history"),
            ("Neutral", "neutral_history"),
        ):
            body = risk.get(key)
            if body:
                lines.append(f"### {label}")
                lines.append("")
                lines.append(_demote_headings(str(body).strip(), min_level=4))
                lines.append("")

    # Cost at the end.
    lines.append("## Cost (Bedrock tokens)")
    lines.append("")
    lines.append("| Model | Input tokens | Output tokens | Cost (USD) |")
    lines.append("|---|---:|---:|---:|")
    for row in priced:
        lines.append(
            f"| {row['model']} "
            f"| {row['input_tokens']:,} "
            f"| {row['output_tokens']:,} "
            f"| {_fmt_usd(float(row['cost_usd']))} |"
        )
    lines.append(f"| **Total** | | | **{_fmt_usd(total)}** |")
    lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _decision_oneline(decision: str, max_len: int = 140) -> str:
    text = (decision or "").strip()
    if not text:
        return "_no decision_"
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    first = first.replace("|", "\\|")
    if len(first) > max_len:
        first = first[: max_len - 3] + "..."
    return first


def render_summary(
    *,
    trade_date: str,
    run_id: str,
    ticker_results: Iterable[Dict[str, Any]],
) -> str:
    """Render the cross-ticker summary report.

    ``ticker_results`` items must include at minimum:
    ``ticker``, ``status``, ``decision``, ``report_key``, ``cost_usd``.
    """
    items = list(ticker_results)
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
        report_key = r.get("report_key")
        if report_key:
            filename = report_key.rsplit("/", 1)[-1]
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

    lines.append("## Conclusions")
    lines.append("")
    for r in items:
        ticker = str(r.get("ticker", "?")).upper()
        decision = str(r.get("decision", "") or "").strip()
        lines.append(f"### {ticker}")
        lines.append("")
        lines.append(decision or "_no decision returned_")
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
