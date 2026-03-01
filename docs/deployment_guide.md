# Gianluigi Cloud Run Deployment Guide

## What This Deploys

Gianluigi runs on Google Cloud Run as an always-on container. It starts `main.py`, which launches the Telegram bot, Drive/email watchers, and all schedulers — same as running locally, but in the cloud with automatic restarts.

**Live test mode:** The deployment uses `ENVIRONMENT=staging`, which means all messages, emails, and notifications go **only to Eyal**. Nothing reaches the team until you change it to `production`.

## Prerequisites

- GCP project (you already have one)
- Supabase project (already set up)
- `.env` file with all values filled in (already done)

---

## Step 1: Install gcloud CLI

Download and install from: https://cloud.google.com/sdk/docs/install

Choose the Windows installer. After installation, open a **new** terminal and run:

```bash
gcloud init
```

This will:
- Open your browser to log in with your Google account
- Ask you to select your GCP project

Verify it worked:
```bash
gcloud config get-value project
```
Should print your project ID.

---

## Step 2: Enable GCP APIs

These are the Google services Gianluigi needs. Run all four:

```bash
gcloud services enable cloudbuild.googleapis.com
gcloud services enable run.googleapis.com
gcloud services enable containerregistry.googleapis.com
gcloud services enable secretmanager.googleapis.com
```

---

## Step 3: Grant Permissions

Cloud Build needs permission to deploy, and Cloud Run needs permission to read secrets.

```bash
# Get your project number (used in the next commands)
PROJECT_NUMBER=$(gcloud projects describe $(gcloud config get-value project) --format='value(projectNumber)')

# Allow Cloud Build to deploy to Cloud Run
gcloud projects add-iam-policy-binding $(gcloud config get-value project) \
  --member="serviceAccount:${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com" \
  --role="roles/run.admin"

# Allow Cloud Build to act as the service account
gcloud projects add-iam-policy-binding $(gcloud config get-value project) \
  --member="serviceAccount:${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com" \
  --role="roles/iam.serviceAccountUser"

# Allow Cloud Run to read secrets
gcloud projects add-iam-policy-binding $(gcloud config get-value project) \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"
```

---

## Step 4: Upload Secrets

This script reads your `.env` and creates all 25 secrets in GCP Secret Manager:

```bash
cd C:\Users\nogas\Desktop\gianluigi
bash scripts/upload_secrets.sh
```

It's safe to re-run — existing secrets get a new version, nothing is deleted.

---

## Step 5: Run SQL Migrations

Make sure all 15 tables exist in Supabase:

1. Open Supabase dashboard → SQL Editor
2. Paste the entire contents of `scripts/setup_supabase.sql`
3. Click Run

The script uses `IF NOT EXISTS` everywhere, so it's safe to re-run.

Verify with this query:
```sql
SELECT tablename FROM pg_tables
WHERE schemaname = 'public'
ORDER BY tablename;
```

You should see 15 tables:
`audit_log`, `calendar_classifications`, `commitments`, `decisions`, `documents`,
`embeddings`, `entities`, `entity_mentions`, `follow_up_meetings`, `meetings`,
`open_questions`, `pending_approvals`, `task_mentions`, `tasks`, `token_usage`

---

## Step 6: Seed Entities

Pre-populate the entity registry with known CropSight entities:

```bash
python scripts/seed_entities.py
```

---

## Step 7: Deploy!

```bash
cd C:\Users\nogas\Desktop\gianluigi
gcloud builds submit --config cloudbuild.yaml .
```

This uploads your code to GCP, builds the Docker image in the cloud (~3-5 min), and deploys it to Cloud Run. You'll see progress in the terminal.

---

## Step 8: Verify

### Check logs
```bash
gcloud run services logs read gianluigi --region=europe-west1 --limit=50
```

Look for:
```
Initializing Supabase... OK
Initializing Google Drive... OK
...
Gianluigi is ready!
```

### Check health endpoints
```bash
SERVICE_URL=$(gcloud run services describe gianluigi --region=europe-west1 --format='value(status.url)')
curl "$SERVICE_URL/health"
curl "$SERVICE_URL/ready"
```

### Test Telegram
Send `/start` to the bot, then ask a question.

---

## Step 9: Stop Local Instance

**Important:** Don't run two Gianluigi instances at the same time (duplicate Telegram responses, double-processing transcripts).

Kill any local `python main.py` process.

---

## Post-Deploy Checklist

- [ ] Logs show "Gianluigi is ready!" with all services OK
- [ ] `/health` returns 200, `/ready` returns 200
- [ ] Telegram bot responds to `/start` and free-text questions
- [ ] `/status` command works
- [ ] Drop a test transcript in Drive → gets processed
- [ ] Task Tracker Sheet gets updated after processing

---

## Going to Production

When live testing is complete and you're ready for the full team:

1. Open `cloudbuild.yaml` line 75
2. Change `ENVIRONMENT=staging` to `ENVIRONMENT=production`
3. Redeploy: `gcloud builds submit --config cloudbuild.yaml .`

---

## Ongoing Operations

| Task | Command |
|------|---------|
| Redeploy after code changes | `gcloud builds submit --config cloudbuild.yaml .` |
| Update a secret | `echo -n "new-val" \| gcloud secrets versions add SECRET_NAME --data-file=-` then redeploy |
| View logs | `gcloud run services logs read gianluigi --region=europe-west1 --limit=100` |
| Check service | `gcloud run services describe gianluigi --region=europe-west1` |

## Cost

Everything stays within GCP's free tier:

| Resource | Cost |
|----------|------|
| Cloud Run (1 instance, 512MB) | $0/month |
| Cloud Build, Registry, Secrets | $0/month |
| LLM + Embeddings | ~$3-5/month (unchanged) |
