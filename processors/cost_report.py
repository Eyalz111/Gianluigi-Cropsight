"""
Weekly cost/pricing report (no LLM — pure read off the token_usage ledger).

build_cost_report() returns a Telegram-ready message + a fuller markdown doc body
covering the last 7 days of Claude spend, week-over-week change, by-model and
top-feature breakdowns, plus a note that infra (Cloud Run) is not in this number.

Used by schedulers/cost_report_scheduler.py (weekly) and exposed for ad-hoc runs.
"""

import logging

from services.supabase_client import supabase_client
from core.cost_calculator import compute_cost_summary

logger = logging.getLogger(__name__)

# Rough always-on Cloud Run estimate (see docs/COST_ANALYSIS_2026_06.md). Not in
# token_usage — surfaced as context so the LLM number isn't mistaken for total.
_INFRA_NOTE = "Cloud Run (always-on) ~$25–70/mo + Supabase $0–25 are NOT in this figure."


def _money(x: float) -> str:
    return f"${x:,.2f}"


def _split_trend(daily_trend: list[dict]) -> tuple[float, float]:
    """Sum the last 7 days vs the prior 7 days from the 14-day daily trend."""
    days = sorted(daily_trend, key=lambda d: d.get("date", ""))
    last7 = sum(d.get("cost", 0.0) for d in days[-7:])
    prev7 = sum(d.get("cost", 0.0) for d in days[-14:-7])
    return last7, prev7


def build_cost_report() -> dict:
    """Build the weekly cost report. Returns {telegram, doc, total_7d, prev_7d}."""
    # 14 days so we can show week-over-week; the 7-day breakdown drives the body.
    records_14 = supabase_client.get_token_usage_summary(days=14)
    summary_14 = compute_cost_summary(records_14)
    last7, prev7 = _split_trend(summary_14.get("daily_trend", []))

    records_7 = supabase_client.get_token_usage_summary(days=7)
    s7 = compute_cost_summary(records_7)

    by_model = s7.get("by_model", {})
    by_site = s7.get("by_call_site", {})
    top_sites = sorted(by_site.items(), key=lambda kv: kv[1].get("cost", 0.0), reverse=True)[:6]

    delta = last7 - prev7
    if prev7 > 0:
        arrow = "▲" if delta > 0 else ("▼" if delta < 0 else "→")
        wow = f"{arrow} {_money(abs(delta))} ({delta / prev7 * 100:+.0f}%) vs prior 7d"
    else:
        wow = "(no prior-week data)"

    # ---- Telegram message (concise, HTML) ----
    tg = [f"<b>💸 Weekly Claude spend</b> — {_money(last7)}", f"<i>{wow}</i>", ""]
    if by_model:
        tg.append("<b>By model:</b>")
        for m, d in sorted(by_model.items(), key=lambda kv: kv[1].get("cost", 0.0), reverse=True):
            short = "Opus" if "opus" in m else "Sonnet" if "sonnet" in m else "Haiku" if "haiku" in m else m
            tg.append(f"  • {short}: {_money(d.get('cost', 0.0))} ({d.get('calls', 0)} calls)")
        tg.append("")
    if top_sites:
        tg.append("<b>Top features:</b>")
        for site, d in top_sites:
            tg.append(f"  • {site}: {_money(d.get('cost', 0.0))}")
        tg.append("")
    tg.append(f"<i>{_INFRA_NOTE}</i>")
    telegram_text = "\n".join(tg)

    # ---- Markdown doc (fuller, for the Drive archive) ----
    md = [
        "# CropSight — Weekly Cost Report",
        "",
        f"**Claude (LLM) spend, last 7 days:** {_money(last7)}  ",
        f"**Week-over-week:** {wow}  ",
        f"**Prior 7 days:** {_money(prev7)}",
        "",
        "> " + _INFRA_NOTE,
        "",
        "## By model",
        "",
        "| Model | Cost | Calls | Input tok | Output tok |",
        "|---|---|---|---|---|",
    ]
    for m, d in sorted(by_model.items(), key=lambda kv: kv[1].get("cost", 0.0), reverse=True):
        md.append(
            f"| {m} | {_money(d.get('cost', 0.0))} | {d.get('calls', 0)} | "
            f"{d.get('input_tokens', 0):,} | {d.get('output_tokens', 0):,} |"
        )
    md += ["", "## By feature (top spenders, 7d)", "", "| Feature | Cost | Calls |", "|---|---|---|"]
    for site, d in sorted(by_site.items(), key=lambda kv: kv[1].get("cost", 0.0), reverse=True):
        md.append(f"| {site} | {_money(d.get('cost', 0.0))} | {d.get('calls', 0)} |")
    md += [
        "",
        "## Daily trend (last 14 days)",
        "",
        "| Date | Cost |",
        "|---|---|",
    ]
    for d in sorted(summary_14.get("daily_trend", []), key=lambda x: x.get("date", "")):
        md.append(f"| {d.get('date', '?')} | {_money(d.get('cost', 0.0))} |")
    md += [
        "",
        "---",
        "*Claude spend is read from the `token_usage` ledger (every call is logged). "
        "Infra (Cloud Run, Supabase) and any Perplexity/ElevenLabs usage are billed "
        "separately — see `docs/COST_ANALYSIS_2026_06.md`.*",
    ]
    doc_markdown = "\n".join(md)

    return {
        "telegram": telegram_text,
        "doc": doc_markdown,
        "total_7d": last7,
        "prev_7d": prev7,
    }
