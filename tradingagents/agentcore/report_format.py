"""Render per-ticker and summary reports as Markdown.

Isolating the format here keeps ``app.py`` small and lets the aggregator
Lambda reuse :func:`render_summary` without pulling in FastAPI.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, Iterable, List

from .bedrock_rates import summarize, total_cost


def _fmt_usd(value: float) -> str:
    return f"${value:,.4f}"


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


_MARKET_CONCL_HEADING = re.compile(
    r"(?m)^## +(.*(?:conclusion|final (?:verdict|thoughts|recommendation"
    r"|conviction|synthesis|transaction)|bottom line).*)$",
    re.I,
)


def _extract_market_conclusion(market_report: str) -> str:
    """Pull the trailing conclusion section out of a market analyst report.

    Mirror of the aggregator Lambda's extractor. Keyword-matches the last H2
    that looks conclusion-like, falls back to the last H2 otherwise, and
    trims at the first line-starting ``---`` HR after the heading so
    disclaimers and summary tables don't bleed in.
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
    # Strip the leading H2 heading line — the prose speaks for itself.
    body = re.sub(r"^##\s+[^\n]*\n+", "", body, count=1)
    return body.rstrip()


def _strip_horizontal_rules(text: str) -> str:
    """Remove Markdown horizontal rule lines (`---`, `***`, `___`) from prose.

    Lines inside fenced code blocks are preserved. A rule flanked by blank
    lines leaves a single blank in its place so adjacent paragraphs still
    separate cleanly; a rule directly beneath a text line is NOT stripped
    since Markdown treats ``text\\n---`` as an H2 setext heading, which
    would change the outline if removed.
    """
    if not text:
        return text
    lines = text.split("\n")
    out: List[str] = []
    in_fence = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            out.append(line)
            continue
        if not in_fence and re.fullmatch(r"[-*_]{3,}\s*", stripped):
            prev_blank = i == 0 or lines[i - 1].strip() == ""
            # If prior line is non-blank, this is a setext H2 underline — leave it.
            if not prev_blank:
                out.append(line)
                continue
            # Drop the rule; collapse to a single blank line.
            if out and out[-1] != "":
                out.append("")
            continue
        out.append(line)
    # Collapse any runs of >2 blanks introduced by the removal.
    collapsed: List[str] = []
    blank_run = 0
    for line in out:
        if line.strip() == "":
            blank_run += 1
            if blank_run <= 1:
                collapsed.append(line)
        else:
            blank_run = 0
            collapsed.append(line)
    return "\n".join(collapsed)


def _tighten_paragraphs(text: str) -> str:
    """Replace paragraph breaks (blank lines) with Markdown hard line breaks
    to cut prose spacing by roughly half. Blank lines adjacent to headings,
    list items, tables, or code fences are preserved so structural blocks
    still separate properly."""
    if not text:
        return text
    lines = text.split("\n")
    out: List[str] = []
    i = 0
    structural = lambda s: (
        not s
        or s.startswith(("#", ">", "- ", "* ", "|", "```", "~~~"))
        or bool(re.match(r"^\d+\.\s", s))
    )
    while i < len(lines):
        line = lines[i]
        if line.strip() == "" and out and i + 1 < len(lines):
            prev = out[-1].strip()
            nxt = lines[i + 1].strip()
            if structural(prev) or structural(nxt):
                out.append(line)
            else:
                if not out[-1].endswith("  "):
                    out[-1] = out[-1].rstrip() + "  "
        else:
            out.append(line)
        i += 1
    return "\n".join(out)


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
    # Metadata line only — the ticker + date is captured by the filename and
    # the first section header, so skip the redundant H1 title. Analyst body
    # headings are still demoted so no stray top-level '#' can appear.
    lines.append(
        f"**{ticker.upper()} — {trade_date}**  "
        f"**Run ID:** `{run_id}`  **Status:** {status}  "
        f"**Duration:** {duration_seconds:.1f}s  "
        f"**Generated:** {datetime.utcnow().isoformat(timespec='seconds')}Z"
    )
    lines.append("")

    # Conclusion at the top. Prefer the full Portfolio Manager text
    # (final_state["final_trade_decision"]) and fall back to the short label
    # that signal_processing parsed out. Rating/Price Target/Time Horizon
    # collapse into a single inline header; the market analyst's own
    # conclusion is spliced in right after the Executive Summary so readers
    # see the trade view + supporting technical read in one block.
    lines.append("## Decision")
    lines.append("")
    full_decision = str(final_state.get("final_trade_decision") or "").strip()
    if full_decision or decision.strip():
        body = _collapse_pm_header_fields(full_decision or decision.strip())
        market_concl = _extract_market_conclusion(
            str(final_state.get("market_report") or "")
        )
        if market_concl:
            # Market conclusion sits as the SECOND paragraph, right after the
            # inline Rating / Price Target / Time Horizon header line. The
            # collapsed header ends at the first blank line; insert there.
            insert_at = body.find("\n\n")
            if insert_at < 0:
                insert_at = len(body)
            body = (
                body[:insert_at].rstrip()
                + "\n\n"
                + market_concl.rstrip()
                + "\n\n"
                + body[insert_at:].lstrip()
            )
        body = _tighten_paragraphs(_strip_horizontal_rules(_demote_headings(body)))
        lines.append(body.rstrip())
    else:
        lines.append("_no decision returned_")
    lines.append("")

    # "final_trade_decision" is already rendered at the top as the Decision
    # section — omit it here to avoid duplicating the full PM rationale.
    sections = [
        ("Market", "market_report"),
        ("Social / Sentiment", "sentiment_report"),
        ("News", "news_report"),
        ("Fundamentals", "fundamentals_report"),
        ("Investment plan (Research Manager)", "investment_plan"),
        ("Trader investment plan", "trader_investment_plan"),
    ]
    lines.append("## Analyst reports")
    lines.append("")
    for title, key in sections:
        body = final_state.get(key)
        if not body:
            continue
        lines.append(f"### {title}")
        lines.append("")
        lines.append(_strip_horizontal_rules(_demote_headings(str(body).strip(), min_level=4)))
        lines.append("")

    debate = final_state.get("investment_debate_state") or {}
    if debate.get("bull_history") or debate.get("bear_history"):
        lines.append("## Research debate")
        lines.append("")
        if debate.get("bull_history"):
            lines.append("### Bull")
            lines.append("")
            lines.append(_strip_horizontal_rules(_demote_headings(str(debate["bull_history"]).strip(), min_level=4)))
            lines.append("")
        if debate.get("bear_history"):
            lines.append("### Bear")
            lines.append("")
            lines.append(_strip_horizontal_rules(_demote_headings(str(debate["bear_history"]).strip(), min_level=4)))
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
                lines.append(_strip_horizontal_rules(_demote_headings(str(body).strip(), min_level=4)))
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


def _strip_investment_thesis(text: str) -> str:
    """Drop the '**Investment Thesis**: ...' paragraph from a PM decision.

    The thesis is a multi-paragraph justification that's valuable in the
    per-ticker report but too verbose for the cross-ticker summary. It
    runs from the ``**Investment Thesis**:`` marker until the next
    ``**Field**:`` marker (Price Target, Time Horizon, etc.) or end of text.
    """
    if not text:
        return text
    import re
    pattern = re.compile(
        r"\*\*Investment Thesis\*\*:.*?(?=\n\s*\*\*[A-Z][^*]*\*\*:|\Z)",
        re.DOTALL,
    )
    stripped = pattern.sub("", text)
    # Collapse resulting triple-blank-lines back to double.
    stripped = re.sub(r"\n{3,}", "\n\n", stripped).strip()
    return stripped


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
        full = str((r.get("final_state") or {}).get("final_trade_decision") or "").strip()
        short = str(r.get("decision", "") or "").strip()
        body = _strip_investment_thesis(full) if full else short
        lines.append(f"### {ticker}")
        lines.append("")
        lines.append(body or "_no decision returned_")
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
