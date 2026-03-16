"""
Quick Google re-authentication script.
Double-click this file or run: python scripts/reauth_google.py
"""
import json
import sys
import os
import traceback

# Add project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from google_auth_oauthlib.flow import InstalledAppFlow

    SCOPES = [
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/drive.file",
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/spreadsheets",
    ]

    cred_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "credentials.json")
    token_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "token.json")

    print("Starting Google OAuth flow...")
    print("A browser window will open. Log in with gianluigi.cropsight@gmail.com")
    print()

    flow = InstalledAppFlow.from_client_secrets_file(cred_path, SCOPES)
    creds = flow.run_local_server(port=8080, access_type='offline', prompt='consent')

    # Save token
    with open(token_path, 'w') as f:
        f.write(creds.to_json())

    print()
    print("=" * 60)
    print("SUCCESS! Update your .env file with:")
    print("=" * 60)
    print()
    print(f"GOOGLE_REFRESH_TOKEN={creds.refresh_token}")
    print()
    print("Token also saved to token.json")
    print()

except Exception as e:
    print()
    print("ERROR:")
    traceback.print_exc()
    print()

# Keep window open
input("Press Enter to close...")
