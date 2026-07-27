"""
Dedicated Gmail refresh-token generator for the BOT mailbox.

Why this exists (2026-07-27): Gmail, Drive and Sheets historically shared one
`GOOGLE_REFRESH_TOKEN`, which authenticates as Eyal's personal Gmail
(eyalz111@gmail.com). So the inbox watcher polled Eyal's personal inbox, and mail
forwarded/CC'd to gianluigi.cropsight@gmail.com was never seen or ingested.

This produces a SEPARATE token, used only for Gmail (`GMAIL_REFRESH_TOKEN`), so the
watcher reads the bot mailbox while Drive/Sheets stay on the shared token.

Differences from get_google_token.py (deliberate):
  * Gmail-only scopes (the bot mailbox never needs Drive/Sheets/Calendar).
  * Forces a FRESH consent into its OWN token file (token_gmail.json), so it never
    reprints the cached personal-account token.
  * Prints GMAIL_REFRESH_TOKEN (not GOOGLE_REFRESH_TOKEN).

Prereq: the same credentials.json (Desktop OAuth client) already used for the
project, in the project root.

Usage:
    python scripts/get_gmail_token.py
    # When the browser opens, LOG IN AS gianluigi.cropsight@gmail.com (the bot mailbox)

Then give Eyal-side output `GMAIL_REFRESH_TOKEN=...` to whoever deploys, and set it:
    gcloud run services update gianluigi --region europe-west1 \
        --update-env-vars GMAIL_REFRESH_TOKEN=<the-token>
"""

import json
import os
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# Google collapses gmail.send into gmail.modify (modify already grants send), so
# the granted scope set differs from what was requested — oauthlib otherwise
# raises a "Scope has changed" warning-as-error AFTER the token is issued, losing
# it. Relax that check; the granted modify+readonly do everything the bot needs.
# [2026-07-27]
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

from google_auth_oauthlib.flow import InstalledAppFlow

# Gmail only — the bot mailbox does not need Drive/Sheets/Calendar.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]


def main() -> int:
    credentials_path = project_root / "credentials.json"
    if not credentials_path.exists():
        credentials_path = Path(__file__).parent / "credentials.json"
    if not credentials_path.exists():
        print("ERROR: credentials.json not found in project root or scripts/.")
        print("Use the same Desktop OAuth client JSON already used for this project.")
        return 1

    print("=" * 64)
    print("  Gianluigi — DEDICATED Gmail token (bot mailbox)")
    print("=" * 64)
    print()
    print("IMPORTANT: when the browser opens, log in as the BOT mailbox:")
    print("    gianluigi@cropsight.io")
    print("NOT your personal account. If you see 'app isn't verified',")
    print("click Advanced -> Go to ... (unsafe) to proceed.")
    print(flush=True)

    # Always a fresh flow (no cached-token shortcut) so we can't reprint the
    # personal-account token. Own token file, separate from token.json. No
    # interactive input() — the run_local_server URL below is the only step, so
    # this can be launched non-interactively and the URL relayed. [2026-07-27]
    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
    creds = flow.run_local_server(
        port=8080, access_type="offline", prompt="consent", open_browser=True
    )

    token_path = project_root / "token_gmail.json"
    token_path.write_text(creds.to_json())

    # Sanity-check WHICH account we just authorized (catch a wrong-account login).
    who = "?"
    try:
        from googleapiclient.discovery import build
        svc = build("gmail", "v1", credentials=creds)
        who = svc.users().getProfile(userId="me").execute().get("emailAddress", "?")
    except Exception as e:
        print(f"(could not verify account via getProfile: {e})")

    print()
    print("=" * 64)
    print(f"  Authorized account: {who}")
    if who.lower() != "gianluigi@cropsight.io":
        print("  ⚠️  This is NOT gianluigi@cropsight.io — re-run and log in")
        print("      as the bot mailbox, or the fix will point at the wrong inbox.")
    print("=" * 64)
    print()
    print("Set this in Cloud Run (do NOT commit it):")
    print()
    print(f"GMAIL_REFRESH_TOKEN={creds.refresh_token}")
    print()
    print("  gcloud run services update gianluigi --region europe-west1 \\")
    print("      --update-env-vars GMAIL_REFRESH_TOKEN=<the-token-above>")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
