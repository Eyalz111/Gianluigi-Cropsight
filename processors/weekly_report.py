"""
HTML weekly report generator.

Generates a self-contained HTML report from weekly review agenda data,
served via Cloud Run health server with per-report access tokens.
"""

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

from config.settings import settings
from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)

# Template path — resolve relative to this file
_TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")
_TEMPLATE_FILE = os.path.join(_TEMPLATE_DIR, "weekly_report.html")


async def generate_html_report(
    session_id: str,
    agenda_data: dict,
    week_number: int,
    year: int,
) -> dict:
    """
    Render Jinja2 HTML report, generate per-report access token, store in DB.

    Args:
        session_id: Weekly review session UUID.
        agenda_data: Compiled weekly review data dict.
        week_number: ISO week number.
        year: Year.

    Returns:
        Dict with report_url, access_token, report_id.
    """
    # Generate per-report access token
    access_token = secrets.token_urlsafe(32)

    # Render HTML
    html_content = _render_html_template(agenda_data, week_number, year)

    # Store in weekly_reports
    existing = supabase_client.get_weekly_report(week_number, year)
    expires_at = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()

    if existing:
        report_id = existing["id"]
        supabase_client.update_weekly_report(
            report_id,
            html_content=html_content,
            access_token=access_token,
            session_id=session_id,
            status="draft",
            data=agenda_data,
            expires_at=expires_at,
        )
    else:
        report = supabase_client.create_weekly_report(
            week_number=week_number,
            year=year,
            data=agenda_data,
        )
        report_id = report["id"]
        supabase_client.update_weekly_report(
            report_id,
            html_content=html_content,
            access_token=access_token,
            session_id=session_id,
            expires_at=expires_at,
        )

    # Build URL (fall back to localhost for local dev)
    base_url = settings.REPORTS_BASE_URL.rstrip("/") if settings.REPORTS_BASE_URL else "http://localhost:8080"
    report_url = f"{base_url}/reports/weekly/{access_token}"

    logger.info(f"HTML report generated for W{week_number}/{year}, token={access_token[:8]}...")

    return {
        "report_url": report_url,
        "access_token": access_token,
        "report_id": report_id,
    }


def _render_html_template(
    agenda_data: dict,
    week_number: int,
    year: int,
) -> str:
    """
    Render the weekly report HTML template.

    Uses Jinja2 if available, falls back to simple string formatting.
    """
    try:
        from jinja2 import Environment, FileSystemLoader
        env = Environment(
            loader=FileSystemLoader(_TEMPLATE_DIR),
            autoescape=True,
        )
        template = env.get_template("weekly_report.html")
        return template.render(
            week_number=week_number,
            year=year,
            data=agenda_data,
            generated_at=datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        )
    except ImportError:
        logger.warning("Jinja2 not installed, using fallback template")
        return _render_fallback(agenda_data, week_number, year)
    except Exception as e:
        logger.error(f"Jinja2 rendering failed: {e}")
        return _render_fallback(agenda_data, week_number, year)


def _render_fallback(
    agenda_data: dict,
    week_number: int,
    year: int,
) -> str:
    """Simple fallback HTML rendering without Jinja2."""
    wir = agenda_data.get("week_in_review", {})
    meetings_count = wir.get("meetings_count", 0)
    decisions_count = wir.get("decisions_count", 0)
    task_summary = wir.get("task_summary", {})
    completed = len(task_summary.get("completed_this_week", []))
    overdue = len(task_summary.get("overdue", []))

    attention = agenda_data.get("attention_needed", {})
    stale_tasks = attention.get("stale_tasks", [])

    gantt = agenda_data.get("gantt_proposals", {})
    proposals = gantt.get("proposals", [])

    preview = agenda_data.get("next_week_preview", {})
    upcoming = preview.get("upcoming_meetings", [])

    generated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    stale_rows = ""
    for t in stale_tasks[:20]:
        title = _esc(t.get("title", ""))
        assignee = _esc(t.get("assignee", ""))
        stale_rows += f"<tr><td>{title}</td><td>{assignee}</td></tr>"

    upcoming_rows = ""
    for e in upcoming[:20]:
        title = _esc(e.get("title", ""))
        start = e.get("start", "")
        upcoming_rows += f"<tr><td>{title}</td><td>{start}</td></tr>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CropSight Weekly Report — W{week_number}/{year}</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 0; padding: 20px; background: #f5f5f5; color: #333; }}
.container {{ max-width: 800px; margin: 0 auto; background: #fff; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); overflow: hidden; }}
.header {{ background: linear-gradient(135deg, #2d5016, #4a7c23); color: white; padding: 24px 32px; }}
.header h1 {{ margin: 0; font-size: 24px; }}
.header p {{ margin: 8px 0 0; opacity: 0.85; }}
.content {{ padding: 24px 32px; }}
.section {{ margin-bottom: 24px; }}
.section h2 {{ font-size: 18px; color: #2d5016; border-bottom: 2px solid #e0e0e0; padding-bottom: 8px; }}
.stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 16px; margin-bottom: 24px; }}
.stat {{ background: #f8faf5; border-radius: 6px; padding: 16px; text-align: center; }}
.stat .number {{ font-size: 28px; font-weight: bold; color: #2d5016; }}
.stat .label {{ font-size: 12px; color: #666; text-transform: uppercase; }}
table {{ width: 100%; border-collapse: collapse; }}
th, td {{ padding: 8px 12px; text-align: left; border-bottom: 1px solid #eee; }}
th {{ background: #f8faf5; font-weight: 600; }}
.footer {{ padding: 16px 32px; background: #f8f8f8; color: #999; font-size: 12px; text-align: center; }}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>CropSight Weekly Report</h1>
    <p>Week {week_number}, {year}</p>
  </div>
  <div class="content">
    <div class="stats">
      <div class="stat"><div class="number">{meetings_count}</div><div class="label">Meetings</div></div>
      <div class="stat"><div class="number">{decisions_count}</div><div class="label">Decisions</div></div>
      <div class="stat"><div class="number">{completed}</div><div class="label">Tasks Done</div></div>
      <div class="stat"><div class="number">{overdue}</div><div class="label">Overdue</div></div>
    </div>

    {"<div class='section'><h2>Attention Needed</h2><table><tr><th>Task</th><th>Assignee</th></tr>" + stale_rows + "</table></div>" if stale_rows else ""}

    {"<div class='section'><h2>Gantt Proposals (" + str(len(proposals)) + ")</h2><p>" + str(len(proposals)) + " pending proposals to review.</p></div>" if proposals else ""}

    {"<div class='section'><h2>Next Week</h2><table><tr><th>Meeting</th><th>Time</th></tr>" + upcoming_rows + "</table></div>" if upcoming_rows else ""}
  </div>
  <div class="footer">
    Generated by Gianluigi on {generated_at}<br>
    CropSight — Confidential
  </div>
</div>
</body>
</html>"""


def _esc(text: str) -> str:
    """Escape HTML."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
