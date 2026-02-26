"""
Google OAuth Token Generator for Gianluigi.

This script handles the OAuth2 flow to obtain a refresh token for Google APIs.
The refresh token is needed for Gianluigi to access Gmail, Drive, Calendar, and Sheets.

Prerequisites:
1. Go to Google Cloud Console (https://console.cloud.google.com)
2. Create a new project (or use existing)
3. Enable these APIs:
   - Gmail API
   - Google Drive API
   - Google Calendar API
   - Google Sheets API
4. Go to "Credentials" -> "Create Credentials" -> "OAuth client ID"
5. Choose "Desktop app" as application type
6. Download the credentials JSON file
7. Save it as "credentials.json" in the project root (same folder as this script's parent)

Usage:
    python scripts/get_google_token.py

The script will:
1. Open a browser for you to authenticate with gianluigi.cropsight@gmail.com
2. Save the token to token.json
3. Print the refresh token for your .env file
"""

import json
import os
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request


# All scopes needed by Gianluigi
SCOPES = [
    # Gmail - send meeting summaries, read responses, mark as read
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",

    # Google Drive - read transcripts, write summaries
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive.readonly",

    # Google Calendar - read events for meeting prep
    "https://www.googleapis.com/auth/calendar.readonly",

    # Google Sheets - read/write task tracker and stakeholder tracker
    "https://www.googleapis.com/auth/spreadsheets",
]


def get_credentials_path() -> Path:
    """Get path to credentials.json file."""
    # Check project root first
    credentials_path = project_root / "credentials.json"
    if credentials_path.exists():
        return credentials_path

    # Check scripts folder
    credentials_path = Path(__file__).parent / "credentials.json"
    if credentials_path.exists():
        return credentials_path

    return project_root / "credentials.json"


def get_token_path() -> Path:
    """Get path to token.json file."""
    return project_root / "token.json"


def main():
    """Run the OAuth flow and save the refresh token."""
    print("=" * 60)
    print("  Gianluigi Google OAuth Token Generator")
    print("=" * 60)
    print()

    credentials_path = get_credentials_path()
    token_path = get_token_path()

    # Check if credentials.json exists
    if not credentials_path.exists():
        print("ERROR: credentials.json not found!")
        print()
        print("Please follow these steps:")
        print("1. Go to https://console.cloud.google.com")
        print("2. Create or select a project")
        print("3. Enable these APIs:")
        print("   - Gmail API")
        print("   - Google Drive API")
        print("   - Google Calendar API")
        print("   - Google Sheets API")
        print("4. Go to 'Credentials' -> 'Create Credentials' -> 'OAuth client ID'")
        print("5. Choose 'Desktop app' as application type")
        print("6. Download the JSON file")
        print(f"7. Save it as: {credentials_path}")
        print()
        sys.exit(1)

    print(f"Found credentials at: {credentials_path}")
    print()

    # Check if we already have a valid token
    creds = None
    if token_path.exists():
        print(f"Found existing token at: {token_path}")
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
            if creds and creds.valid:
                print("Existing token is still valid!")
                print()
                print("=" * 60)
                print("  REFRESH TOKEN (copy this to your .env file)")
                print("=" * 60)
                print()
                print(f"GOOGLE_REFRESH_TOKEN={creds.refresh_token}")
                print()
                return
            elif creds and creds.expired and creds.refresh_token:
                print("Token expired, refreshing...")
                creds.refresh(Request())
        except Exception as e:
            print(f"Could not load existing token: {e}")
            creds = None

    # Run OAuth flow if we don't have valid credentials
    if not creds or not creds.valid:
        print()
        print("Starting OAuth flow...")
        print()
        print("IMPORTANT: When the browser opens, log in with:")
        print("  gianluigi.cropsight@gmail.com")
        print()
        print("You may see a warning that the app isn't verified.")
        print("Click 'Advanced' -> 'Go to [app name] (unsafe)' to proceed.")
        print()
        input("Press Enter to open the browser...")
        print()

        # Create flow with offline access to get refresh token
        flow = InstalledAppFlow.from_client_secrets_file(
            str(credentials_path),
            SCOPES,
        )

        # Run the OAuth flow
        # access_type='offline' ensures we get a refresh token
        # prompt='consent' forces the consent screen to appear (needed to get refresh token)
        creds = flow.run_local_server(
            port=8080,
            access_type='offline',
            prompt='consent',
        )

        print()
        print("Authentication successful!")

    # Save the token
    print(f"Saving token to: {token_path}")
    with open(token_path, 'w') as token_file:
        token_file.write(creds.to_json())

    # Extract and display the refresh token
    print()
    print("=" * 60)
    print("  SUCCESS! Copy these values to your .env file:")
    print("=" * 60)
    print()

    # Read the credentials.json to get client ID and secret
    with open(credentials_path, 'r') as f:
        cred_data = json.load(f)

    # Handle both "installed" and "web" credential types
    if "installed" in cred_data:
        client_id = cred_data["installed"]["client_id"]
        client_secret = cred_data["installed"]["client_secret"]
    elif "web" in cred_data:
        client_id = cred_data["web"]["client_id"]
        client_secret = cred_data["web"]["client_secret"]
    else:
        client_id = "CHECK_CREDENTIALS_JSON"
        client_secret = "CHECK_CREDENTIALS_JSON"

    print(f"GOOGLE_CLIENT_ID={client_id}")
    print(f"GOOGLE_CLIENT_SECRET={client_secret}")
    print(f"GOOGLE_REFRESH_TOKEN={creds.refresh_token}")
    print()
    print("=" * 60)
    print()
    print("Token saved successfully!")
    print()
    print("The token.json file contains your access credentials.")
    print("Keep it secure and don't commit it to version control.")
    print()

    # Verify the scopes
    print("Authorized scopes:")
    for scope in SCOPES:
        scope_name = scope.split("/")[-1]
        print(f"  - {scope_name}")
    print()


if __name__ == "__main__":
    main()
