"""
Get a calendar-only OAuth refresh token for a team member.

This runs a local OAuth consent flow so Gianluigi can read a user's
calendar AS THAT USER — which means we see their event colors, declined
status, and everything exactly as they see it.

Usage:
    python scripts/get_calendar_token.py

    # Or specify the Google account email upfront:
    python scripts/get_calendar_token.py --email eyalz111@gmail.com

The script prints the refresh token. Copy it into .env as:
    EYAL_CALENDAR_REFRESH_TOKEN=<token>

Architecture note (Phase B / future):
    When CropSight moves to Google Workspace with a @cropsight.com domain,
    replace per-user OAuth tokens with a service account + domain-wide
    delegation. That gives the same access without individual consent flows.
    See: https://support.google.com/a/answer/162106
"""

import argparse
import sys

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:
    print("Missing dependency. Run: pip install google-auth-oauthlib")
    sys.exit(1)

# Calendar read-only — minimal scope for what we need
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


def main():
    parser = argparse.ArgumentParser(
        description="Get a Google Calendar OAuth refresh token for a team member"
    )
    parser.add_argument(
        "--email",
        help="Google account email (for display purposes only)",
        default=None,
    )
    parser.add_argument(
        "--credentials",
        help="Path to OAuth client credentials JSON",
        default="credentials.json",
    )
    args = parser.parse_args()

    if args.email:
        print(f"\nGetting calendar token for: {args.email}")
    print(f"Using credentials file: {args.credentials}")
    print(f"Scope: {SCOPES[0]}")
    print()
    print("A browser window will open. Sign in with the Google account")
    print("whose calendar Gianluigi should read, then grant calendar access.")
    print()

    try:
        flow = InstalledAppFlow.from_client_secrets_file(args.credentials, SCOPES)
        creds = flow.run_local_server(port=0)
    except FileNotFoundError:
        print(f"ERROR: {args.credentials} not found.")
        print("Download it from Google Cloud Console → APIs & Services → Credentials")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    print()
    print("=" * 60)
    print("SUCCESS — Copy this refresh token into your .env file:")
    print("=" * 60)
    print()
    print(f"EYAL_CALENDAR_REFRESH_TOKEN={creds.refresh_token}")
    print()
    print("(If this is for a different team member, use the appropriate")
    print(" env var name, e.g. ROYE_CALENDAR_REFRESH_TOKEN)")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\nFATAL: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        input("\nPress Enter to exit...")
        sys.exit(1)
