# Post-Phase 4 Manual Test Checklist

**Date:** 2026-03-16
**Scope:** Architecture review fixes (Phases A, B1, B2)
**Pre-requisite:** SQL migration must be run first (Step 0)

---

## Step 0: SQL Migration (REQUIRED BEFORE TESTING)

Run in **Supabase Dashboard > SQL Editor**:

```sql
ALTER TABLE pending_approvals ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
```

**Verify:** Run `SELECT column_name FROM information_schema.columns WHERE table_name = 'pending_approvals' AND column_name = 'expires_at';` — should return 1 row.

---

## Quick Checks (local, ~10 min)

### 1. /status command shows pending approvals
- [ ] Start bot locally: `python main.py`
- [ ] Send `/status` to Eyal DM
- [ ] Verify output includes "Pending Approvals" section (or "No pending approvals")
- [ ] Verify existing metrics still show (meetings, tasks, commitments, tokens)

### 2. Weekly digest scheduler doesn't fire on wrong day
- [ ] Check logs during startup — weekly digest scheduler should start normally
- [ ] Confirm it does NOT fire today (Sunday) — default is now Friday
- [ ] Check log for: `Starting weekly digest scheduler (interval: 3600s)`

### 3. Approval reminders schedule correctly
- [ ] Process a test transcript (or use `/reprocess`) to trigger an approval
- [ ] Check logs for: `Scheduled reminders for <id> at [2, 6]h`
- [ ] Approve the item
- [ ] Check logs for: reminder tasks cancelled (no "Reminder:" message should arrive)

### 4. Morning brief skip day
- [ ] If testing on Saturday: morning brief should log "Morning brief skipped (skip day)"
- [ ] If testing on another day: brief runs normally (if enabled)

### 5. Health report
- [ ] Set `DAILY_HEALTH_REPORT_ENABLED=true` and `MORNING_BRIEF_ENABLED=true`
- [ ] Trigger morning brief manually (or wait for scheduled time)
- [ ] Verify Telegram DM receives "Daily Health Report" after the brief
- [ ] Report should show: all systems operational, pending approvals count, meetings processed

### 6. Document watcher interval
- [ ] Check logs: `Starting document watcher (poll interval: 900s)` (was 300s)

---

## Deferred Checks (verify after deploy to Cloud Run)

### 7. Approval expiry (requires time to pass)
- [ ] Submit a morning brief approval
- [ ] Verify `expires_at` is set in DB: `SELECT approval_id, expires_at FROM pending_approvals WHERE content_type = 'morning_brief';`
- [ ] After 24h (or manually update `expires_at` to past): orphan cleanup should expire it
- [ ] Eyal should receive "Expired approvals" notification

### 8. Session locking
- [ ] Start a `/debrief` session
- [ ] Verify "Heads up: N approval(s) pending" message appears (if any pending)
- [ ] While debrief is active, try starting another interactive flow — should be blocked
- [ ] `/cancel` the debrief — session lock should release

### 9. Scheduler error alerts
- [ ] Intentionally break a scheduler (e.g., bad API key) to verify error alert arrives
- [ ] Check that `alert_critical_error` deduplicates within 1 hour

### 10. RAG source weights
- [ ] Ask a question via Telegram that touches debrief content
- [ ] Debrief results should rank higher than equivalent meeting transcript content
- [ ] No functional test needed — weights are config-only, verified by unit tests

---

## Pass Criteria

- [ ] Steps 1-6 pass locally
- [ ] No regressions in existing functionality (approvals, Q&A, search all work)
- [ ] 964+ tests passing (`pytest -q` shows only 2 pre-existing env-leak failures)
- [ ] Steps 7-10 verified after Cloud Run deploy (can be done async)
