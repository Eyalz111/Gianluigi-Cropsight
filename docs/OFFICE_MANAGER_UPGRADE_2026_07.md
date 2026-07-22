# Office-Manager Operational Upgrade — Plan (2026-07-22)

**Context.** Nechama joins as office manager. The team becomes three entities: Eyal (CEO,
sole approver), Nechama (ops execution), Gianluigi (system). Nechama consumes Gianluigi's
**outputs** — Telegram group, Google Sheets, Drive folders. She does not command the system,
does not approve, does not edit meeting summaries, does not extract from the DB.

**Design constraint that governs everything below:** *Gianluigi proposes, Eyal approves.*
Nechama gains **read + operational-status write** (marking a meeting scheduled, a question
chased). She never gains approval rights, distribution rights, or content-authoring rights.

---

## 0. Ground truth (verified 2026-07-22, live prod env)

Prod diverges from code defaults. Verified via `gcloud run services describe`:

| Flag | Code default | Live prod |
|---|---|---|
| `ENVIRONMENT` | development | **production** |
| `TEAM_ROSTER_DB_ENABLED` | False | **true** |
| `TASK_SHEET_URGENCY_AREA_ENABLED` | False | **true** |
| `RECONCILE_ENABLED` / `RECONCILE_SHADOW_MODE` | False / True | **true / false** |
| `DECISION_RECONCILE_ENABLED` | False | **true** |
| `GANTT_RECONCILE_ENABLED` | False | **true** |
| `STRICT_CALENDAR_FILTER` | False | **true** |
| `INPUT_HYGIENE_SHADOW_MODE` | True | **false** |
| `TRANSCRIPT_WATCHER_ENABLED` | False | **true** |
| `DISTRIBUTION_TIER_CAPPING_ENABLED` | True | **false** ⚠️ |
| `MCP_ALLOW_AUTHLESS` | False | **true** (must stay until OAuth activated) |

Implications:
- The roster is **DB-backed** → adding Nechama needs no deploy.
- The Tasks sheet is **A:K** (Urgency in col K), not A:J.
- The calendar OR-chain false positive is **already closed** in prod.
- **Content tier-capping is OFF.** Recipient bands apply; per-item filtering does not.

### Current output surfaces

| Surface | State |
|---|---|
| Tasks tab (A:K) | Priority, Label, Task, Owner, Deadline, Status, Category, Source Meeting, Created, ID, Urgency. Live reconcile. |
| Decisions tab (A:H) | Label, Decision, Rationale, Confidence, Source Meeting, Date, Status, ID. Live reconcile. |
| Archive tab | Sanctioned removal target (`status=archived`). |
| Stakeholder Tracker | Eyal's pre-existing CRM. Read + append. |
| Gantt sheet | Separate spreadsheet: `2026-2027`, `Log`, `Config`, `Meeting Cadence`. |
| **follow_up_meetings** | **No sheet.** DB-only. Smuggled into Tasks as `"Schedule: X"` rows. |
| **open_questions** | **No sheet. No surface at all** outside the summary doc + `/questions`. |
| Topics / areas | `topic_threads.brief_json`, `areas.brief_json` — DB JSONB. **No human-readable rendering.** MCP-only. |

---

## 1. "Log only" — ingest without distributing  *(your point 1)*

### Problem
There is **no** approve-without-send path. `guardrails/approval_flow.py:1337-1341` calls
`distribute_approved_content` unconditionally for every meeting summary. The only early exit is
the blank-summary safety rail — an error state, not a choice.

Today's two options are both wrong:
- **CEO-only band** → still writes Drive `.md` + PDF, still pushes all children to Sheets, still
  emails you the full PDF, still DMs you.
- **Reject** → sends nothing, but `delete_meeting_cascade` **destroys** the tasks, decisions,
  questions, follow-ups and embeddings. The opposite of what's wanted.

### Design
Add a **distribution mode**, orthogonal to sensitivity. Sensitivity = *who may ever see this*.
Mode = *do we push a document this time*. Conflating them is what forced the CEO-tier workaround.

New approval-card row:

```
[📋 Log only — no send   -> dmode:log:{meeting_id}]
```

`mode` persists in `pending_approvals.content['__distribution']['mode']`, same mechanism as the
existing custom-recipient selection — so a Cloud Run restart mid-decision cannot lose it.

**Log-only executes:**
- ✅ All DB writes, `_promote_children_to_approved`, cross-refs, topic state, semantic index
- ✅ Tasks → Sheets, Decisions → Sheets, follow-ups, stakeholders
- ✅ Drive archive (`.md` + PDF) — the audit trail is kept
- ✅ `audit_log` entry (`content_logged_only`)
- ❌ **No email to anyone**
- ❌ **No Telegram group teaser**
- ✅ One-line DM to Eyal: `Logged: {title} — 4 tasks, 2 decisions, 1 follow-up. Not sent.`

**Rails that must still run:** `_distribution_content_intact` stays mandatory — log-only is
exactly the mode where a silently-empty ingest would go unnoticed.

**Why keep the Drive archive:** you asked to keep "the rest of the outputs"; the PDF is the
record, and skipping it saves nothing operationally. If short meetings should skip doc
generation too, that's a trivial follow-on sub-mode.

**Risk:** low. Additive branch in an already step-wise function. No change to existing paths.

---

## 2. Google Workspace migration  *(your point 2)*

### What's actually true today
There is **no service account anywhere**. Drive, Sheets and Gmail all run on **one OAuth refresh
token belonging to `gianluigi.cropsight@gmail.com`** — itself a personal Gmail. Calendar runs on a
second personal token (Eyal's). Files Gianluigi writes are owned by *the bot's personal Gmail*,
not by Eyal. So there are **two personal accounts holding company IP**, and moving folders
transfers neither.

### The blocker
`services/google_drive.py` has **zero Shared Drive support** — no `supportsAllDrives`,
`includeItemsFromAllDrives`, `driveId`, or `corpora`. Every query is `spaces="drive"`.

**Drop the folders into a Shared Drive today and every `files().list` returns empty and every
`files().create` 404s — silently.** This must ship *before* the move.

Second constraint: the Drive write scope is `drive.file`, which grants access **only to files the
app itself created**. This is why `move_file_to_rejected` on Tactiq-created transcripts is already
best-effort (`google_drive.py:1078`).

### Migration order (do not reorder)

| # | Step | Owner | Notes |
|---|---|---|---|
| 0 | Confirm cropsight.io Workspace exists + plan tier | Eyal | Business Standard+ needed for Gemini/Meet transcripts |
| 1 | **Decide the bot identity** (see below) | Eyal | Gates everything downstream |
| 2 | Ship Shared Drive support in `google_drive.py` | Code | Inert while folders are in My Drive — safe to deploy early |
| 3 | Create Shared Drive `CropSight Ops`; add bot identity as **Content Manager** | Eyal | |
| 4 | **Dry run: move ONE low-value folder**, verify listing + create still work | Both | Non-negotiable |
| 5 | Move remaining folders **via web UI** | Eyal | The Drive **API cannot** move folders into a Shared Drive without recreating them with new IDs. UI preserves IDs. |
| 6 | Re-verify all 14 folder-ID env vars; update any that changed | Code | |
| 7 | Re-share the 3 spreadsheets (Tasks, Stakeholder, Gantt) to the bot identity | Eyal | |
| 8 | Rotate the old refresh token | Eyal | |

⚠️ **Permissions do not follow.** Inherited folder permissions are *not* carried into a Shared
Drive. Anyone relying on folder-inherited access loses it at step 5.

### ✅ DECIDED — bot identity: keep `gianluigi.cropsight@gmail.com` as an external member

Eyal holds the credentials for this account, so it stays. **This is the right call, not a
compromise** — an earlier draft of this plan wrongly called it "half a fix".

**Why it fully solves ownership:** Google's rule is that *the organization owning a shared drive
owns the files within it* — ownership follows the drive, not the creator. Cross-domain ownership
*transfer* (gmail.com → cropsight.io) is not supported, but the shared-drive move sidesteps it:
the documented path is precisely "the external owner moves their files into the shared drive",
after which cropsight.io owns them. Everything Gianluigi creates there afterwards is org-owned too.

Net: cropsight.io gets real ownership of all content; Eyal keeps credentials he already controls;
no Workspace seat purchased.

**Prerequisites this adds (admin settings — check before step 3):**
- Drive sharing must **allow users outside the organization to access files in shared drives**.
- The `CropSight Ops` shared drive must **permit external members**.
- The bot must be **Content Manager** (Contributor is not enough to move content in).

**Failure mode, and it's the right one:** if external-member access is ever revoked, the bot loses
access but **the files stay with cropsight.io**. Ownership is no longer coupled to the account.

*(Deferred alternative: service account + domain-wide delegation — the end state the code comments
already anticipate at `google_calendar.py:23-24`. Revisit only if external-member access becomes
awkward.)*

### Workspace upside (separate phase — do not couple to the migration)
Meet transcripts land as a **Google Doc in the organizer's `Meet Recordings` folder and auto-attach
to the Calendar event** — so the existing Drive-watcher pattern still applies, just a new folder.
A **Meet REST API v2** (`conferenceRecords.transcripts.entries`) gives structured speaker-tagged
transcripts, strictly better input than a Tactiq text dump, and **replaces a tool you pay for**.
Requires: Business Standard+, a parser change (Google Doc, not `.txt`), new scopes.

---

## 3. Drive folder cleanup  *(your point 3)*

14 folder-ID env vars exist. Known-dead or questionable:

| Folder var | Status |
|---|---|
| `DROPBOX_MIRROR_DRIVE_FOLDER_ID` | **Dead** — Dropbox sync disabled, needs SDK + creds |
| `DATA_PACKAGE_FOLDER_ID` | Storage-cost measurement only |
| `GANTT_SLIDES_FOLDER_ID`, `WEEKLY_REPORTS_FOLDER_ID`, `GANTT_BACKUP_FOLDER_ID`, `INTELLIGENCE_SIGNAL_FOLDER_ID`, `EMAIL_ATTACHMENTS_FOLDER_ID` | Wired, low traffic — **audit live contents before judging** |

**Action:** run a Drive inventory (file count + last-modified per folder) *before* the migration and
fold the answer into step 5 — migrate only what earns its place. Don't guess from code; the code
says "wired", not "used".

---

## 4. Nechama on the roster  *(your point 4)*

`TEAM_ROSTER_DB_ENABLED=true` → this is a **DB insert, not a deploy**:

```python
supabase_client.add_team_member(
    member_key="nechama", name="Nechama ...", role="Office Manager",
    primary_email="nechamatik@gmail.com", tier="founders",
    telegram_id=<from /myid>, is_admin=False,
)
```
Then `refresh_team_roster()` or a restart.

This propagates automatically to: `recipients_for_band`, the Custom picker checklist,
`CROPSIGHT_TEAM_EMAILS`, `is_team_email`, calendar filtering, email filter chain.

### ✅ DECIDED — full `founders` tier immediately

She joins the roster at `tier="founders"` and starts receiving founders-band meeting summaries by
email from day one.

### 🚩 HARD GATE — tier capping must be ON before the insert. Non-negotiable.

`DISTRIBUTION_TIER_CAPPING_ENABLED=false` in prod. Recipient bands apply, but **per-item content
capping does not** — a CEO-tier item inside a founders-band meeting is sent **unfiltered**. Today
that reaches 4 people who all effectively see everything, so the gap is invisible. Nechama is the
first recipient for whom "founders-band" and "sees CEO-tier items" are genuinely different, so the
day she is added, an existing latent leak becomes a live one.

**Order of operations, no exceptions:**
1. Flip `DISTRIBUTION_TIER_CAPPING_ENABLED=true`.
2. Send a founders-band summary containing at least one CEO-tier item; confirm the item is stripped
   and that `_render_team_safe_summary` renders the "some items are restricted" note correctly.
3. Confirm the regenerated PDF attachment is also filtered (capping rebuilds prose **and**
   attachment — verify both; the code drops the attachment entirely rather than ship a leaky one).
4. *Only then* insert her roster row.

Anything else ships a known leak to a new person on her first day.

### Two access surfaces — she already has one of them
1. **Group read access.** Already live. `_chat_privilege` caps *any* non-Eyal sender at FOUNDERS(3),
   read-only, with all inline buttons hard-blocked to Eyal's user ID. **No change needed.**
2. **Email recipient list.** *This* is what the roster row adds, and what the gate above protects.

### Side effect to accept knowingly
Roster membership makes her `is_team_email() == true`, so the email watcher will route her mail to
the agent (questions get answered). Approval-by-email is off (`APPROVAL_EMAIL_ENABLED` default
False, after the 2026-07-16 self-approval incident) so she cannot approve by email. Verify this
stays off.

---

## 5. The operational core — Sheets restructure  *(your points 5 + 7)*

### Why the current sheet fails an ops person
1. **11 columns**, 3 of which are system-owned noise to her (Source Meeting, Created, ID).
2. **Two priority axes** — `Priority` (importance) and `Urgency` (time pressure) — that she must
   reconcile mentally, with no combined view.
3. **No temporal view.** No "due this week", no per-owner cut, no "waiting on someone".
4. **Follow-up meetings — her core job — have no home.** They're smuggled into Tasks as
   `"Schedule: X"` rows.
5. **Open questions are completely invisible.**
6. **No topic visibility** on any row.

### Governing principle: add views, don't restructure the system-of-record
The reconcile engine is keyed to exact column indices plus `sheet_snapshots`. Restructuring the
Tasks tab risks precisely the incident class that has already bitten twice (the 293-row/100-dupe
event; the sheet wipe). **Leave `Tasks`, `Decisions`, `Archive` alone.** Add derived tabs.

### New tabs

**`This Week`** — the weekly-meeting agenda spine. Generated, read-only. Sections in order:
`Overdue` → `Due this week` → `Waiting on someone` → `Unassigned` → `No deadline`.
Columns: Topic, Task, Owner, Deadline, Status. Nothing else.

**`Meetings to Schedule`** — the missing `follow_up_meetings` surface, and Nechama's main tab.
Columns: Title, Led by, Proposed date, Participants, Agenda, Prep needed, Source meeting,
**Status** (`Not scheduled` / `Scheduled` / `Dropped`), ID.

**`Open Questions`** — Question, Raised by, Source meeting, Owner, Status (`open`/`resolved`), ID.

**`Topics`** *(point 6)* — read-only, weekly refresh. Topic, Area, Status
(`active`/`blocked`/`pending_decision`/`stale`/`closed`), Last meeting, Open tasks, Open questions,
One-line status from `brief_json`.

### The unifying idea — `Label` becomes `Topic`
There is **no `topic_id` on `tasks`** (verified: the only task↔topic association is a fire-and-forget
`knowledge_links` row). But `label` (Tasks col B) already exists, is already human-editable, is
already reconciled with manual-flagging, and is already an informal topic.

**Make `Label` the topic column, seeded from `topic_threads` as a soft vocabulary.** At extraction,
set `label` = matched topic-thread name when confident, else free text. Decisions already have a
`Label` column (col A).

Result: **Topic becomes the join key across Tasks, Decisions, Meetings-to-Schedule and Topics** —
solving point 7 with **zero schema change, zero new reconcile surface**, and reusing machinery
that already works. A real `topic_id` FK can come later if the soft vocabulary proves too loose.

### Phasing
- **Phase A — read-only.** All four tabs generated from DB. Immediate value, near-zero risk.
  Ships without touching reconcile.
- **Phase B — editable.** `Meetings to Schedule` and `Open Questions` become writable (she marks
  scheduled / resolved), via new reconcile entity types following the **exact** discipline used for
  tasks and decisions: `sheet_snapshots` rows, ID column, protected system columns, shadow mode
  first, then flip.

Phase B is the real work. Phase A is worth shipping alone.

### Weekly meeting shape
The EOS "Level 10" structure fits: fixed agenda, same order every week, majority of time on
solving not reporting. The `This Week` tab **is** the agenda — Gianluigi produces the spine,
the meeting walks it top to bottom, Nechama's edits land back in the sheet.

---

## 6. Topics/areas access  *(your point 6)*

Today `topic_threads.brief_json` and `areas.brief_json` have **no human-readable rendering** —
they reach a human only through MCP tools (Eyal) or a Telegram query.

**What she can do, in order of cost:**
1. **Ask in the group, today.** She already has FOUNDERS-capped read via `search_memory`,
   `list_decisions`, `get_topic_thread`. Zero work. This is the "how she accesses it" answer.
2. **The `Topics` tab** (above) — the map of what the company is working on, in a tool she
   already uses, joined to Tasks/Decisions by topic name.
3. **Not MCP.** MCP has *no user model at all* — one token, one PIN, no subject, no per-user
   scoping, and tier gating exists only on the email/Telegram side. **An MCP caller sees CEO-tier
   data unconditionally.** Giving her MCP access is a real project, not a config change. Your
   instinct to keep her off it is correct.

**What she should not do:** edit topic briefs. They're LLM-synthesized from meeting evidence;
hand-editing them corrupts the knowledge layer with no provenance.

---

## 7. Calendar  *(your point 8)*

**Gianluigi cannot create calendar events. At all.** Scope is `calendar.readonly`; there are zero
`events().insert/update/delete` calls in app code. `follow_up_meetings` rows are extracted, stored,
rendered as text — end of line. No auto-invite exists and none is possible without a new scope and
credential.

**This makes point 8 cheap:** Nechama scheduling meetings is a **human workflow**, not a feature.
She works directly in Google Calendar. Gianluigi owes her only *visibility* — the
`Meetings to Schedule` tab.

**Recommended once on Workspace:** a dedicated **CropSight calendar under cropsight.io**, shared to
Nechama with "make changes" rights. She sends invites without touching Eyal's personal calendar,
and calendar *identity* replaces the color-`3` heuristic — retiring a fragile signal rather than
working around it.

**Do not** grant her access to Eyal's personal calendar. The mixed personal/work calendar is the
root problem; a separate CropSight calendar solves it properly.

---

## 8. Additional items found during the audit (not on your list)

| # | Item | Severity |
|---|---|---|
| A | **`add_follow_ups_as_tasks` writes 9 columns with no UUID in col J** (`google_sheets.py:1458-1468`). Every follow-up row is ID-less → the next reconcile treats it as hand-added and **creates a duplicate DB task**. Exactly the class the col-J writeback was built to prevent. Fixed for free by the `Meetings to Schedule` tab. | **HIGH — live duplicate generator** |
| B | `DISTRIBUTION_TIER_CAPPING_ENABLED=false` in prod (see §4). | **HIGH — leak surface** |
| C | **MCP calendar leak.** `get_upcoming_meetings` (`mcp_server.py:1433-1472`) and `get_full_status` call the calendar with **no CropSight filter** — raw personal events flow into Claude.ai. Unrelated to Nechama; fix regardless. | **MEDIUM — personal-data leak** |
| D | **Assignee is unvalidated free text** (`TEXT NOT NULL`, no FK, no enum). `core/agent.py:744-754` resolves `"roye"` → `"Roye Tadmor"` then `ilike`-matches, **missing every row stored as `"Roye"`**. Adding a 5th person worsens this. Category has canonicalization + a QA check; assignee has neither. Add both, and add `Nechama` to the extraction/debrief prompts (which currently enumerate only Eyal/Roye/Paolo/Yoram). | **MEDIUM — will bite on day one** |
| E | Decisions-sheet hygiene: blank-id rows + cross-meeting duplicates; reconcile guards resurrection but doesn't prune superseded rows. | MEDIUM |
| F | Branch drift: 19 commits ahead of `origin/main`; this session adds more. Merge before it compounds. | MEDIUM |
| G | Onboarding doc for Nechama — what she can/can't do, how to query the group bot, sheet conventions. | Required at launch |

---

## 9. Suggested sequencing

**Wave 1 — no migration, no risk, immediate value**
1. 🚩 Flip `DISTRIBUTION_TIER_CAPPING_ENABLED=true` **and verify a founders-band send strips
   CEO-tier items from both prose and attachment** (B, §4). Blocks step 2.
2. Add Nechama to the DB roster at `tier="founders"` + add `Nechama` to the extraction/debrief
   prompts; register her Telegram ID via `/myid` (§4, D)
3. Ship **Log only** (§1)
4. Ship **Phase A read-only tabs**: This Week, Meetings to Schedule, Open Questions, Topics (§5, §6)
5. Fix the follow-up duplicate generator (A) and the MCP calendar leak (C)
6. Write her onboarding doc (G)

*After Wave 1 she is fully operational: group access, her own tabs, a weekly agenda, and topic
visibility — with no Workspace dependency.*

**Wave 2 — editable ops surface**
7. Phase B: `Meetings to Schedule` + `Open Questions` writable, shadow-mode first (§5)
8. Assignee normalization + QA check (D)
9. Decisions-sheet hygiene sweep (E)

**Wave 3 — Workspace**
10. Decide bot identity; ship Shared Drive support; dry-run one folder; migrate (§2)
11. Drive folder cleanup, informed by the inventory (§3)
12. Dedicated CropSight calendar shared to Nechama (§7)

**Wave 4 — optional**
13. Meet/Gemini transcription replacing Tactiq (§2)

---

## 10. Decisions — resolved 2026-07-22 (Eyal)

| # | Decision | Choice | Consequence |
|---|---|---|---|
| 1 | Bot identity for Workspace | **Keep `gianluigi.cropsight@gmail.com` as external member** — Eyal holds the credentials | No seat purchased. Shared-drive content is still org-owned (§2). Adds three admin prerequisites. |
| 2 | Nechama's tier | **Full `founders` immediately** | Receives founders-band summaries by email from day one. **Gated on tier-capping being ON first** (§4). |
| 3 | Sheet strategy | **Derived read-only tabs, then editable** | `Tasks`/`Decisions` untouched. Phase A read-only, Phase B writable (§5). |
| 4 | Log-only scope | **Skip email + group post, keep Drive archive** | Full ingest + Sheets + PDF record; no email, no group teaser, one-line DM to Eyal (§1). |

### Still open (not blocking Wave 1)
- Whether `Meetings to Schedule` / `Open Questions` editability (Phase B) lands before or after the
  Workspace migration.
- Whether to pursue Meet/Gemini transcription as a Tactiq replacement (Wave 4) — depends on the
  cropsight.io plan tier.
- Whether `Label`-as-Topic proves tight enough as a soft vocabulary, or eventually needs a real
  `topic_id` FK on `tasks`.
