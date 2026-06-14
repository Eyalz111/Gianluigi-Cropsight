"""
Actual GCP spend from the BigQuery billing export — the only supported way to
read real Cloud Run / GCP cost programmatically (there is no "current spend" API).

DARK-SAFE: returns {available: False, ...} until ALL of these are true:
  1. Standard usage-cost billing export is enabled to BigQuery (one-time, console).
  2. The Cloud Run runtime service account has BigQuery read on the dataset
     (roles/bigquery.dataViewer + roles/bigquery.jobUser).
  3. GCP_BILLING_EXPORT_TABLE is set to the full table path:
     `project.dataset.gcp_billing_export_v1_XXXXXX_XXXXXX_XXXXXX`.

Uses the BigQuery REST API via the existing googleapiclient + Application Default
Credentials (the Cloud Run SA) — no new dependency. Never raises; the weekly cost
report calls this and simply shows the estimate when actuals aren't available yet.
"""

import logging

from config.settings import settings

logger = logging.getLogger(__name__)

_BQ_SCOPE = "https://www.googleapis.com/auth/bigquery.readonly"


def get_gcp_mtd_costs() -> dict:
    """Month-to-date GCP spend by service from the billing export.

    Returns:
        {available: bool, cloud_run_usd: float|None, total_usd: float|None,
         by_service: list[(name, usd)], currency: str, reason: str}
    """
    out = {
        "available": False, "cloud_run_usd": None, "total_usd": None,
        "by_service": [], "currency": "USD", "reason": "",
    }
    table = (getattr(settings, "GCP_BILLING_EXPORT_TABLE", "") or "").strip()
    if not table:
        out["reason"] = "GCP_BILLING_EXPORT_TABLE not set"
        return out

    try:
        import google.auth
        from googleapiclient.discovery import build

        creds, default_project = google.auth.default(scopes=[_BQ_SCOPE])
        project = (settings.GCP_PROJECT_ID or default_project)
        bq = build("bigquery", "v2", credentials=creds, cache_discovery=False)

        # Gross cost per service for the current invoice month. (Credits — discounts —
        # are reported separately and net them down a few %; gross is the right
        # at-a-glance figure for a CEO cost view.)
        sql = (
            "SELECT service.description AS svc, SUM(cost) AS cost "
            f"FROM `{table}` "
            "WHERE invoice.month = FORMAT_DATE('%Y%m', CURRENT_DATE()) "
            "GROUP BY svc ORDER BY cost DESC"
        )
        resp = bq.jobs().query(
            projectId=project,
            body={"query": sql, "useLegacySql": False, "timeoutMs": 30000},
        ).execute()

        rows = resp.get("rows", []) or []
        by_service, total, cloud_run = [], 0.0, 0.0
        for r in rows:
            f = r.get("f", [])
            name = (f[0].get("v") or "Unknown") if len(f) > 0 else "Unknown"
            cost = float((f[1].get("v") if len(f) > 1 else 0) or 0)
            by_service.append((name, cost))
            total += cost
            if name == "Cloud Run":
                cloud_run = cost

        out.update(
            available=True, cloud_run_usd=cloud_run, total_usd=total,
            by_service=by_service, reason="ok",
        )
        return out

    except Exception as e:
        out["reason"] = f"{type(e).__name__}: {str(e)[:140]}"
        logger.warning(f"GCP billing query unavailable: {out['reason']}")
        return out
