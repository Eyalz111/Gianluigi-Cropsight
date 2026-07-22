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

# Fallback note when the real GCP billing export isn't wired yet (estimate from
# docs/COST_ANALYSIS_2026_06.md). Not in token_usage — surfaced so the LLM number
# isn't mistaken for the total.
_INFRA_NOTE = "Cloud Run (always-on) ~$25–70/mo + Supabase $0–25 are NOT in this figure."


def _infra_lines() -> tuple[str, list[str]]:
    """Return (telegram_line, doc_lines) for infrastructure cost — REAL GCP
    month-to-date when the billing export is configured, else the estimate."""
    try:
        from services.gcp_billing import get_gcp_mtd_costs
        gcp = get_gcp_mtd_costs()
    except Exception as e:
        logger.warning(f"GCP cost lookup failed: {e}")
        gcp = {"available": False}

    if gcp.get("available"):
        cr = gcp.get("cloud_run_usd") or 0.0
        total = gcp.get("total_usd") or 0.0
        tg = f"☁️ <b>GCP this month (actual):</b> {_money(total)} — Cloud Run {_money(cr)}"
        doc = [
            "## Infrastructure — GCP (actual, month-to-date)",
            "",
            f"**Total GCP:** {_money(total)}  ·  **Cloud Run:** {_money(cr)}",
            "",
            "| Service | Cost (MTD) |",
            "|---|---|",
        ]
        for name, cost in (gcp.get("by_service") or [])[:8]:
            doc.append(f"| {name} | {_money(cost)} |")
        return tg, doc

    # Not wired yet — show the estimate + how to make it real.
    tg = f"<i>{_INFRA_NOTE}</i>"
    doc = [
        "## Infrastructure — estimate",
        "",
        f"> {_INFRA_NOTE}",
        "> Set `GCP_BILLING_EXPORT_TABLE` (after enabling BigQuery billing export) "
        "to replace this estimate with the real Cloud Run / GCP month-to-date spend.",
    ]
    return tg, doc


def _drive_storage_lines() -> tuple[str, list[str]]:
    """Return (telegram_line, doc_lines) for Google Drive/Workspace storage cost —
    the ~89 GB CropSight Data Package + account total. Dark-safe: silent (empty)
    until WORKSPACE_STORAGE_USD_PER_GB_MONTH is set (mirrors the GCP-infra pattern)."""
    try:
        from services.drive_storage_cost import get_drive_storage_cost
        st = get_drive_storage_cost()
    except Exception as e:
        logger.warning(f"Drive storage cost lookup failed: {e}")
        st = {"available": False}

    if not st.get("available"):
        return "", []

    used = st.get("used_gb") or 0.0
    mo = st.get("monthly_usd") or 0.0
    pkg = st.get("package_gb")
    quota = st.get("quota_gb")
    quota_str = f" / {quota:,.0f} GB" if quota else ""
    pkg_str = f" · CropSight Data Package {pkg:,.0f} GB" if pkg else ""
    tg = f"💾 <b>Drive/Workspace storage:</b> {_money(mo)}/mo — {used:,.0f}{quota_str} used{pkg_str}"
    doc = [
        "## Storage — Google Drive / Workspace",
        "",
        f"**Est. storage:** {_money(mo)}/mo  ·  **Used:** {used:,.1f} GB{quota_str}"
        + (f"  ·  **CropSight Data Package:** {pkg:,.1f} GB" if pkg else ""),
    ]
    return tg, doc


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
    infra_tg, infra_doc = _infra_lines()
    tg.append(infra_tg)
    storage_tg, storage_doc = _drive_storage_lines()
    if storage_tg:
        tg.append(storage_tg)
    telegram_text = "\n".join(tg)

    # ---- Markdown doc (fuller, for the Drive archive) ----
    md = [
        "# CropSight — Weekly Cost Report",
        "",
        f"**Claude (LLM) spend, last 7 days:** {_money(last7)}  ",
        f"**Week-over-week:** {wow}  ",
        f"**Prior 7 days:** {_money(prev7)}",
        "",
        *infra_doc,
        *(["", *storage_doc] if storage_doc else []),
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
        "GCP infra + Google Drive/Workspace storage are shown above when configured; "
        "Supabase / Perplexity / ElevenLabs are billed separately — see `docs/COST_ANALYSIS_2026_06.md`.*",
    ]
    doc_markdown = "\n".join(md)

    return {
        "telegram": telegram_text,
        "doc": doc_markdown,
        "total_7d": last7,
        "prev_7d": prev7,
    }
