"""One-off: dump audit_log entries from the last 4 hours. Read-only."""
import json, os, sys
from datetime import datetime, timedelta, timezone

if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.supabase_client import supabase_client

cutoff = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()
resp = (
    supabase_client.client.table("audit_log")
    .select("created_at,action,details,triggered_by")
    .gte("created_at", cutoff)
    .order("created_at", desc=True)
    .limit(50)
    .execute()
)
for e in resp.data or []:
    print(e["created_at"], "|", e["action"], "|", e.get("triggered_by"), "|",
          json.dumps(e.get("details") or {}, ensure_ascii=False)[:220])
