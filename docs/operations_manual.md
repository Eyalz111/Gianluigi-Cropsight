# Gianluigi Operations Manual

Quick reference for common operations after deployment.

## Redeploy After Code Changes

Whenever code is updated (bug fix, new feature, config change):

```bash
cd C:\Users\nogas\Desktop\gianluigi
gcloud builds submit --config cloudbuild.yaml .
```

Takes ~3-5 minutes. Zero downtime — the old container keeps running until the new one is healthy.

## Update a Secret

If you need to change an API key, token, or any value stored in Secret Manager:

```bash
echo -n "new-value-here" | gcloud secrets versions add SECRET_NAME --data-file=-
```

Then redeploy for the change to take effect:
```bash
gcloud builds submit --config cloudbuild.yaml .
```

## Switch to Production (Team Rollout)

When live testing is done and you want the team to start receiving content:

1. Open `cloudbuild.yaml` line 75
2. Change `ENVIRONMENT=staging` to `ENVIRONMENT=production`
3. Redeploy: `gcloud builds submit --config cloudbuild.yaml .`

To go back to staging (Eyal-only), reverse the change and redeploy.

## View Logs

```bash
gcloud run services logs read gianluigi --region=europe-west1 --limit=50
```

Add `--format=json` for structured logs, or increase `--limit` for more history.

## Check Service Health

```bash
gcloud run services describe gianluigi --region=europe-west1 --format="value(status.url)"
```

Then visit `<URL>/health` and `<URL>/ready` in your browser.

## Restart the Service

Force a fresh restart without code changes:
```bash
gcloud run services update gianluigi --region=europe-west1 --no-traffic
gcloud run services update gianluigi --region=europe-west1 --update-env-vars=RESTART=$(date +%s)
```

## Wipe Data and Start Fresh

Run in **Supabase SQL Editor** (deletes all data, keeps table structure):

```sql
DELETE FROM entity_mentions;
DELETE FROM task_mentions;
DELETE FROM commitments;
DELETE FROM tasks;
DELETE FROM decisions;
DELETE FROM follow_up_meetings;
DELETE FROM open_questions;
DELETE FROM embeddings;
DELETE FROM pending_approvals;
DELETE FROM token_usage;
DELETE FROM audit_log;
DELETE FROM calendar_classifications;
DELETE FROM documents;
DELETE FROM meetings;
-- Entities (seed data) are kept
```

Then clear the data rows in Google Sheets (Task Tracker + Stakeholder Tracker) and delete the "Commitments" tab — it will be recreated automatically.

## Cost Summary

| Resource | Monthly Cost |
|----------|-------------|
| Cloud Run (1 instance, 512MB) | $0 (free tier) |
| Cloud Build, Registry, Secrets | $0 (free tier) |
| Supabase (free tier, 500MB) | $0 |
| LLM + Embeddings (Anthropic/OpenAI) | ~$3-5 |
