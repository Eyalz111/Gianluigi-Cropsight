#!/bin/bash
# Upload secrets from .env to GCP Secret Manager
#
# This script reads your local .env file and creates each secret
# in Google Cloud Secret Manager, so Cloud Run can access them.
#
# Prerequisites:
#   1. gcloud CLI installed and authenticated (gcloud init)
#   2. Secret Manager API enabled:
#      gcloud services enable secretmanager.googleapis.com
#   3. .env file exists in the project root with all values filled in
#
# Usage:
#   cd C:\Users\nogas\Desktop\gianluigi
#   bash scripts/upload_secrets.sh
#
# Safe to re-run: skips secrets that already exist.

set -e

# The 25 secrets that cloudbuild.yaml expects
SECRETS=(
    "ANTHROPIC_API_KEY"
    "OPENAI_API_KEY"
    "SUPABASE_URL"
    "SUPABASE_KEY"
    "TELEGRAM_BOT_TOKEN"
    "TELEGRAM_GROUP_CHAT_ID"
    "TELEGRAM_EYAL_CHAT_ID"
    "EYAL_TELEGRAM_ID"
    "GOOGLE_CLIENT_ID"
    "GOOGLE_CLIENT_SECRET"
    "GOOGLE_REFRESH_TOKEN"
    "EYAL_EMAIL"
    "ROYE_EMAIL"
    "PAOLO_EMAIL"
    "YORAM_EMAIL"
    "CROPSIGHT_OPS_FOLDER_ID"
    "RAW_TRANSCRIPTS_FOLDER_ID"
    "MEETING_SUMMARIES_FOLDER_ID"
    "MEETING_PREP_FOLDER_ID"
    "WEEKLY_DIGESTS_FOLDER_ID"
    "DOCUMENTS_FOLDER_ID"
    "TASK_TRACKER_SHEET_ID"
    "STAKEHOLDER_TRACKER_SHEET_ID"
    "CROPSIGHT_CALENDAR_COLOR_ID"
    "GIANLUIGI_EMAIL"
)

# Check prerequisites
if ! command -v gcloud &> /dev/null; then
    echo "ERROR: gcloud CLI not installed."
    echo "Download from: https://cloud.google.com/sdk/docs/install"
    exit 1
fi

if [ ! -f ".env" ]; then
    echo "ERROR: .env file not found. Run this from the project root."
    exit 1
fi

PROJECT=$(gcloud config get-value project 2>/dev/null)
if [ -z "$PROJECT" ]; then
    echo "ERROR: No GCP project selected. Run: gcloud init"
    exit 1
fi

echo "======================================"
echo "Uploading secrets to GCP Secret Manager"
echo "Project: $PROJECT"
echo "======================================"
echo ""

SUCCESS=0
SKIPPED=0
FAILED=0

for SECRET_NAME in "${SECRETS[@]}"; do
    # Extract value from .env (handles = in values, strips quotes)
    VALUE=$(grep "^${SECRET_NAME}=" .env | head -1 | cut -d'=' -f2-)

    # Remove surrounding quotes if present
    VALUE=$(echo "$VALUE" | sed "s/^['\"]//;s/['\"]$//")

    if [ -z "$VALUE" ] || [[ "$VALUE" == your_* ]]; then
        echo "SKIP: $SECRET_NAME (no value or placeholder in .env)"
        SKIPPED=$((SKIPPED + 1))
        continue
    fi

    # Check if secret already exists
    if gcloud secrets describe "$SECRET_NAME" --project="$PROJECT" &>/dev/null; then
        echo "EXISTS: $SECRET_NAME (adding new version)"
        echo -n "$VALUE" | gcloud secrets versions add "$SECRET_NAME" \
            --data-file=- --project="$PROJECT" 2>/dev/null
        if [ $? -eq 0 ]; then
            SUCCESS=$((SUCCESS + 1))
        else
            FAILED=$((FAILED + 1))
        fi
    else
        echo "CREATE: $SECRET_NAME"
        echo -n "$VALUE" | gcloud secrets create "$SECRET_NAME" \
            --data-file=- --project="$PROJECT" 2>/dev/null
        if [ $? -eq 0 ]; then
            SUCCESS=$((SUCCESS + 1))
        else
            echo "  FAILED to create $SECRET_NAME"
            FAILED=$((FAILED + 1))
        fi
    fi
done

echo ""
echo "======================================"
echo "Done! Created/updated: $SUCCESS, Skipped: $SKIPPED, Failed: $FAILED"
echo "======================================"
echo ""
echo "Next step: Deploy with Cloud Build:"
echo "  gcloud builds submit --config cloudbuild.yaml ."
