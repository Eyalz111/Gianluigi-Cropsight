"""
Dedicated Drive+Sheets refresh-token generator for the ORG account.

Why this exists (2026-07-28): Drive and Sheets historically shared one
`GOOGLE_REFRESH_TOKEN`, which authenticates as Eyal's personal account
(eyalz111@gmail.com). The workspace migration copied all content into the
org-owned `CropSight Ops` Shared Drive, so the final step is to have the bot's
Drive/Sheets I/O run as the ORG account (gianluigi@cropsight.io) instead.

This produces a SEPARATE token, used only for Drive+Sheets
(`GOOGLE_DRIVE_REFRESH_TOKEN`). The token requests FULL `drive` scope (NOT
drive.file) because the bot must read/write org-owned Shared-Drive content it did
not itself create — drive.file only covers app-created files.

Differences from get_gmail_token.py (deliberate):
  * Drive + Sheets scopes (full `drive` + `spreadsheets`).
  * Verifies the authorized account AND that it can see the CropSight Ops Shared
    Drive (0AIZ…) — that confirms both full-drive scope and Shared-Drive
    membership in one shot, so we don't flip prod onto a token that can't see the
    content.
  * Prints GOOGLE_DRIVE_REFRESH_TOKEN.

Prereq: the same credentials.json (Desktop OAuth client) already used for the
project, in the project root.

Usage:
    python scripts/get_drive_token.py
    # When the browser opens, LOG IN AS gianluigi@cropsight.io (the org account)

Then set it (do NOT commit it):
    gcloud run services update gianluigi --region europe-west1 \
        --update-env-vars GOOGLE_DRIVE_REFRESH_TOKEN=<the-token>
"""

import os
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# Google may re-order / normalize the granted scope set vs. what was requested;
# oauthlib otherwise raises a "Scope has changed" warning-as-error AFTER the token
# is issued, losing it. Relax that check. [2026-07-28]
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

from google_auth_oauthlib.flow import InstalledAppFlow

# FULL drive (not drive.file) so the bot sees org-owned Shared-Drive content it
# didn't itself create, plus spreadsheets for the Sheets API. One org grant covers
# both services.
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

SHARED_DRIVE_ID = "0AIZNUiJD1vtpUk9PVA"  # CropSight Ops shared drive
EXPECTED_ACCOUNT = "gianluigi@cropsight.io"


def main() -> int:
    credentials_path = project_root / "credentials.json"
    if not credentials_path.exists():
        credentials_path = Path(__file__).parent / "credentials.json"
    if not credentials_path.exists():
        print("ERROR: credentials.json not found in project root or scripts/.")
        print("Use the same Desktop OAuth client JSON already used for this project.")
        return 1

    print("=" * 64)
    print("  Gianluigi — DEDICATED Drive+Sheets token (org account)")
    print("=" * 64)
    print()
    print("IMPORTANT: when the browser opens, log in as the ORG account:")
    print(f"    {EXPECTED_ACCOUNT}")
    print("NOT your personal account. If you see 'app isn't verified',")
    print("click Advanced -> Go to ... (unsafe) to proceed.")
    print(flush=True)

    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
    creds = flow.run_local_server(
        port=8080, access_type="offline", prompt="consent", open_browser=True
    )

    token_path = project_root / "token_drive.json"
    token_path.write_text(creds.to_json())

    # Sanity-check the account AND Shared-Drive visibility.
    who = "?"
    sees_shared = False
    try:
        from googleapiclient.discovery import build
        svc = build("drive", "v3", credentials=creds)
        who = svc.about().get(fields="user/emailAddress").execute().get(
            "user", {}
        ).get("emailAddress", "?")
        try:
            svc.drives().get(driveId=SHARED_DRIVE_ID).execute()
            sees_shared = True
        except Exception as e:
            print(f"(could not confirm CropSight Ops shared drive: {e})")
    except Exception as e:
        print(f"(could not verify account via about.get: {e})")

    print()
    print("=" * 64)
    print(f"  Authorized account: {who}")
    if who.lower() != EXPECTED_ACCOUNT:
        print(f"  ⚠️  This is NOT {EXPECTED_ACCOUNT} — re-run and log in as the")
        print("      org account, or Drive/Sheets will point at the wrong account.")
    print(f"  Sees CropSight Ops shared drive: {'YES' if sees_shared else 'NO'}")
    if not sees_shared:
        print("  ⚠️  This account cannot see the shared drive. Add it as a Content")
        print("      Manager member before flipping, or the bot will lose Drive access.")
    print("=" * 64)
    print()
    print("Set this in Cloud Run (do NOT commit it):")
    print()
    print(f"GOOGLE_DRIVE_REFRESH_TOKEN={creds.refresh_token}")
    print()
    print("  gcloud run services update gianluigi --region europe-west1 \\")
    print("      --update-env-vars GOOGLE_DRIVE_REFRESH_TOKEN=<the-token-above>")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
