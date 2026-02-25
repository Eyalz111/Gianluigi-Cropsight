# Google API Setup Guide

This guide walks through setting up Google API access for Gianluigi.

## Prerequisites

- Google Account (for Gianluigi's dedicated Gmail)
- Access to Google Cloud Console
- CropSight team Google Drive access

---

## Step 1: Create Gianluigi's Gmail Account

1. Go to https://accounts.google.com/signup
2. Create a new account: `gianluigi.cropsight@gmail.com`
3. Complete verification
4. Add this email to the CropSight Ops shared Drive folder

---

## Step 2: Create Google Cloud Project

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Click **Select a project** → **New Project**
3. Name: `gianluigi-cropsight`
4. Click **Create**

---

## Step 3: Enable Required APIs

In your new project, enable these APIs:

1. Go to **APIs & Services** → **Library**
2. Search and enable each:
   - **Gmail API**
   - **Google Drive API**
   - **Google Calendar API**
   - **Google Sheets API**

---

## Step 4: Configure OAuth Consent Screen

1. Go to **APIs & Services** → **OAuth consent screen**
2. Choose **External** (for testing with specific users)
3. Fill in:
   - App name: `Gianluigi`
   - User support email: Your email
   - Developer contact: Your email
4. Click **Save and Continue**
5. Add scopes:
   - `https://www.googleapis.com/auth/gmail.send`
   - `https://www.googleapis.com/auth/gmail.readonly`
   - `https://www.googleapis.com/auth/drive`
   - `https://www.googleapis.com/auth/calendar.readonly`
   - `https://www.googleapis.com/auth/spreadsheets`
6. Click **Save and Continue**
7. Add test users (all 4 founders' emails)
8. Click **Save and Continue**

---

## Step 5: Create OAuth Credentials

1. Go to **APIs & Services** → **Credentials**
2. Click **+ Create Credentials** → **OAuth client ID**
3. Application type: **Desktop app**
4. Name: `Gianluigi Desktop`
5. Click **Create**
6. Download the JSON file
7. Save as `credentials.json` in your project root (add to .gitignore!)

---

## Step 6: Get Refresh Token

Run this Python script to get your refresh token:

```python
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    'https://www.googleapis.com/auth/gmail.send',
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/drive',
    'https://www.googleapis.com/auth/calendar.readonly',
    'https://www.googleapis.com/auth/spreadsheets',
]

flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
creds = flow.run_local_server(port=0)

print(f"Refresh Token: {creds.refresh_token}")
print(f"Client ID: {creds.client_id}")
print(f"Client Secret: {creds.client_secret}")
```

Save these values in your `.env` file:
- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `GOOGLE_REFRESH_TOKEN`

---

## Step 7: Set Up Google Drive Structure

Create this folder structure in Google Drive:

```
CropSight Ops/ (shared with all 4 founders)
├── Raw Transcripts/
├── Meeting Summaries/
├── Meeting Prep/
├── Weekly Digests/
└── Documents/
```

Get the folder IDs:
1. Open each folder in Google Drive
2. Copy the ID from the URL: `https://drive.google.com/drive/folders/[THIS_IS_THE_ID]`
3. Add to `.env`:
   - `CROPSIGHT_OPS_FOLDER_ID`
   - `RAW_TRANSCRIPTS_FOLDER_ID`
   - `MEETING_SUMMARIES_FOLDER_ID`
   - `MEETING_PREP_FOLDER_ID`
   - `WEEKLY_DIGESTS_FOLDER_ID`

---

## Step 8: Set Up Google Sheets

### Task Tracker (NEW)

1. Create a new Google Sheet: "CropSight Task Tracker"
2. Add headers in row 1:
   - Task | Assignee | Source Meeting | Deadline | Status | Priority | Created Date | Updated Date
3. Copy the Sheet ID from URL
4. Add to `.env`: `TASK_TRACKER_SHEET_ID`

### Stakeholder Tracker (EXISTING)

1. Get the ID of Eyal's existing Stakeholder Tracker
2. Add to `.env`: `STAKEHOLDER_TRACKER_SHEET_ID`

---

## Step 9: Configure Tactiq

1. Open Tactiq settings
2. Go to **Workflows**
3. Create a new workflow:
   - Trigger: "Meeting processed"
   - Action: "Export to Google Drive"
   - Folder: Select `Raw Transcripts` folder
   - Format: TXT with timestamps
4. Do NOT enable any AI summary steps (saves credits)

---

## Step 10: Get Calendar Color ID

1. Open Google Calendar
2. Create a test event
3. Set the color to purple
4. Use this script to find the color ID:

```python
from googleapiclient.discovery import build
# ... authenticate ...
colors = service.colors().get().execute()
print(colors['event'])  # Find purple's ID
```

5. Add to `.env`: `CROPSIGHT_CALENDAR_COLOR_ID`

---

## Verification Checklist

- [ ] Gianluigi Gmail account created
- [ ] Google Cloud project created
- [ ] All 4 APIs enabled
- [ ] OAuth consent screen configured
- [ ] Test users added (all 4 founders)
- [ ] OAuth credentials created
- [ ] Refresh token obtained
- [ ] Drive folders created and IDs saved
- [ ] Task Tracker sheet created
- [ ] Stakeholder Tracker ID obtained
- [ ] Tactiq workflow configured
- [ ] Calendar color ID obtained

---

## Troubleshooting

### "Access blocked: This app's request is invalid"
- Check that all scopes in your code match the OAuth consent screen

### "Quota exceeded"
- Gmail API has limits; implement exponential backoff

### "File not found" errors
- Verify folder IDs are correct
- Check that Gianluigi's account has access to the shared folder

### Token expired
- Refresh tokens don't expire, but you may need to re-authorize if scopes change
