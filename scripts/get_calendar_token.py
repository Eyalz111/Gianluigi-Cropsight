"""
Get a calendar-only OAuth refresh token for the account whose calendar
Gianluigi should read.

This runs a local OAuth consent flow so Gianluigi reads the calendar AS THAT
USER — seeing event colors, declined status, everything as they see it.

[2026-07-28] Migration: the target is now **eyal.zror@cropsight.io** (Eyal's
Workspace account, where CropSight invites land) instead of the personal
eyalz111@gmail.com — the final calendar disconnection from the personal account.
Hardened to match get_drive_token.py: forces access_type=offline + prompt=consent
(so a refresh token is actually returned), opens the browser directly (no
copy-paste URL that gets truncated), and verifies which account was authorized.

Usage:
    python scripts/get_calendar_token.py
    # When the browser opens, LOG IN AS eyal.zror@cropsight.io

Then set it (do NOT commit it):
    gcloud run services update gianluigi --region europe-west1 \
        --update-env-vars EYAL_CALENDAR_REFRESH_TOKEN=<the-token>
"""

import argparse
import os
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# Avoid oauthlib's "Scope has changed" warning-as-error losing the token. [2026-07-28]
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
EXPECTED_ACCOUNT = "eyal.zror@cropsight.io"


def main() -> int:
    parser = argparse.ArgumentParser(description="Get a Google Calendar OAuth refresh token")
    parser.add_argument("--email", default=EXPECTED_ACCOUNT,
                        help="Google account to authorize (display + verify)")
    parser.add_argument("--credentials", default=str(project_root / "credentials.json"),
                        help="Path to OAuth client credentials JSON")
    args = parser.parse_args()

    if not Path(args.credentials).exists():
        alt = Path(__file__).parent / "credentials.json"
        if alt.exists():
            args.credentials = str(alt)
        else:
            print(f"ERROR: {args.credentials} not found.")
            return 1

    print("=" * 64)
    print("  Gianluigi — Calendar token")
    print("=" * 64)
    print()
    print(f"IMPORTANT: when the browser opens, log in as: {args.email}")
    print("NOT your personal account. If you see 'app isn't verified',")
    print("click Advanced -> Go to ... (unsafe) to proceed.")
    print(flush=True)

    flow = InstalledAppFlow.from_client_secrets_file(args.credentials, SCOPES)
    creds = flow.run_local_server(
        port=8080, access_type="offline", prompt="consent", open_browser=True
    )

    token_path = project_root / "token_calendar.json"
    token_path.write_text(creds.to_json())

    # Verify which account we authorized (primary calendar id == the account email).
    who = "?"
    try:
        from googleapiclient.discovery import build
        svc = build("calendar", "v3", credentials=creds)
        who = svc.calendars().get(calendarId="primary").execute().get("id", "?")
    except Exception as e:
        print(f"(could not verify account via calendars.get: {e})")

    print()
    print("=" * 64)
    print(f"  Authorized account: {who}")
    if who.lower() != args.email.lower():
        print(f"  ⚠️  This is NOT {args.email} — re-run and log in as that account,")
        print("      or the calendar will read the wrong events.")
    print("=" * 64)
    print()
    print("Set this in Cloud Run (do NOT commit it):")
    print()
    print(f"EYAL_CALENDAR_REFRESH_TOKEN={creds.refresh_token}")
    print()
    print("  gcloud run services update gianluigi --region europe-west1 \\")
    print("      --update-env-vars EYAL_CALENDAR_REFRESH_TOKEN=<the-token-above>")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
