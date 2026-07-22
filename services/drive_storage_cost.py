"""
Google Drive / Workspace storage cost — for the ~89 GB "CropSight Data Package"
shared with the team plus the account total. This is real recurring storage the
weekly cost report otherwise misses (it lives on Google Workspace/Drive, NOT GCP,
so it never appears in the BigQuery billing export that gcp_billing.py reads).

DARK-SAFE (mirrors gcp_billing.py): returns {available: False, ...} until BOTH:
  1. Google OAuth is configured (GOOGLE_REFRESH_TOKEN etc — already used by google_drive.py).
  2. WORKSPACE_STORAGE_USD_PER_GB_MONTH > 0 is set (the storage rate).

Reads total Drive usage via Drive v3 about().get(storageQuota) using the existing
drive.readonly OAuth scope — no new credentials. Best-effort also reads the data
package's own footprint from its _metrics.json (written by the nightly sync). Never raises.
"""

import io
import json
import logging

from config.settings import settings

logger = logging.getLogger(__name__)

_GB = 1024 ** 3


def get_drive_storage_cost() -> dict:
    """Google Drive/Workspace storage usage + estimated monthly cost.

    Returns:
        {available: bool, used_gb: float|None, quota_gb: float|None,
         package_gb: float|None, monthly_usd: float|None, currency: str, reason: str}
    """
    out = {
        "available": False, "used_gb": None, "quota_gb": None, "package_gb": None,
        "monthly_usd": None, "currency": "USD", "reason": "",
    }
    rate = float(getattr(settings, "WORKSPACE_STORAGE_USD_PER_GB_MONTH", 0.0) or 0.0)
    if rate <= 0:
        out["reason"] = "WORKSPACE_STORAGE_USD_PER_GB_MONTH not set"
        return out
    if not settings.GOOGLE_REFRESH_TOKEN:
        out["reason"] = "Google OAuth not configured"
        return out

    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build

        creds = Credentials(
            token=None,
            refresh_token=settings.GOOGLE_REFRESH_TOKEN,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=settings.GOOGLE_CLIENT_ID,
            client_secret=settings.GOOGLE_CLIENT_SECRET,
            scopes=["https://www.googleapis.com/auth/drive.readonly"],
        )
        if creds.expired or not creds.token:
            creds.refresh(Request())
        drive = build("drive", "v3", credentials=creds, cache_discovery=False)

        quota = drive.about().get(fields="storageQuota").execute().get("storageQuota", {})
        used = int(quota.get("usage") or 0)
        limit = int(quota["limit"]) if quota.get("limit") else None  # unlimited/pooled -> absent
        used_gb = round(used / _GB, 2)
        quota_gb = round(limit / _GB, 2) if limit else None

        package_gb = _read_package_gb(drive)
        # Attribute cost to the data package when we know its size (its share of the
        # flat storage plan); else fall back to total account usage.
        billable_gb = package_gb if package_gb else used_gb
        out.update(
            available=True,
            used_gb=used_gb,
            quota_gb=quota_gb,
            package_gb=package_gb,
            monthly_usd=round(billable_gb * rate, 2),
            reason="ok",
        )
        return out

    except Exception as e:
        out["reason"] = f"{type(e).__name__}: {str(e)[:140]}"
        logger.warning(f"Drive storage cost unavailable: {out['reason']}")
        return out


def _read_package_gb(drive) -> float | None:
    """Best-effort: total_gb from CropSight-Data-Package/_metrics.json (written by the sync)."""
    try:
        from googleapiclient.http import MediaIoBaseDownload

        folder_id = (getattr(settings, "DATA_PACKAGE_FOLDER_ID", "") or "").strip()
        q = "name = '_metrics.json' and trashed = false"
        if folder_id:
            q += f" and '{folder_id}' in parents"
        files = drive.files().list(q=q, spaces="drive", fields="files(id)", pageSize=1).execute().get("files", [])
        if not files:
            return None
        buf = io.BytesIO()
        dl = MediaIoBaseDownload(buf, drive.files().get_media(fileId=files[0]["id"]))
        done = False
        while not done:
            _, done = dl.next_chunk()
        # utf-8-sig, not utf-8: the sync writes _metrics.json with a BOM, and a
        # plain utf-8 decode leaves the BOM in place so json.loads raises and the
        # package line silently stays empty (2026-07-13). utf-8-sig strips it.
        data = json.loads(buf.getvalue().decode("utf-8-sig"))
        return float(data["total_gb"]) if data.get("total_gb") is not None else None
    except Exception as e:
        logger.debug(f"package _metrics.json read skipped: {e}")
        return None
