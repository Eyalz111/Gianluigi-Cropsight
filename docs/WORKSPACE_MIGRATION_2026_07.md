# CropSight → Workspace Migration Runbook (2026-07)

Move CropSight off **Eyal's personal accounts** (Google `eyalz111@gmail.com`, personal
Supabase login) onto **org-owned** infrastructure: the existing `cropsight.io` Google
Workspace + a CropSight Supabase org. End state: **full disconnection of personal
accounts**; every phase leaves the system working.

> Supersedes the 2026-07-22 decision "keep the bot as an external member, no seat"
> (`OFFICE_MANAGER_UPGRADE_2026_07.md` §2, decision #1). New decision: **give Gianluigi a
> real `gianluigi@cropsight.io` Workspace seat.** Forced by the `gianluigi.cropsight@gmail.com`
> external mailbox being locked behind SMS 2FA on a phone nobody holds (2026-07-27).

---

## Current state (verified live 2026-07-27)

| Surface | Auth today | Account it actually uses |
|---|---|---|
| Gmail (ingest + send) | `GOOGLE_REFRESH_TOKEN` | **`eyalz111@gmail.com`** (Eyal personal) |
| Drive (transcripts/summaries/prep, ~14 folders) | `GOOGLE_REFRESH_TOKEN` | **`eyalz111@gmail.com`** |
| Sheets (Tasks / Decisions / Gantt) | `GOOGLE_REFRESH_TOKEN` | **`eyalz111@gmail.com`** |
| Calendar (purple = CropSight) | `EYAL_CALENDAR_REFRESH_TOKEN` | **`eyalz111@gmail.com`** |
| Supabase (system-of-record DB) | `SUPABASE_URL` + `SUPABASE_KEY` | Eyal's **personal Supabase** org/login |

Everything runs on one personal Google account (two tokens) + a personal Supabase login.
`cropsight.io` Workspace already exists (`eyal.zror@`, `nechama@` are live) — so this is
**adding a seat**, not standing up a domain. `services/google_drive.py` has **zero Shared
Drive support** today (hard blocker for Phase 2).

## Target state

- **`gianluigi@cropsight.io`** Workspace seat → Gmail + Drive + Sheets, via a **service
  account with domain-wide delegation (DWD)** (no token/2FA/expiry).
- **`eyal.zror@cropsight.io`** calendar → the CropSight-events calendar (purple filter kept).
- **`CropSight Ops` Shared Drive** (org-owned) → all Gianluigi folders + the heavy CropSight
  data + Nechama's legal files (in a non-watched subfolder).
- **Supabase project** transferred into a **CropSight Supabase org** (URL/keys preserved).

---

## Hard rules (violating any breaks the system silently)

1. **MOVE, never COPY.** A copy (or Google Takeout export/import) mints **new file IDs** →
   breaks env vars + the DB↔Sheet chain. Only a *move into a Shared Drive* transfers
   ownership while preserving IDs.
2. **Sheet IDs are preserved on move** (sheets are files) → `TASK_TRACKER_SHEET_ID` etc. and
   the reconcile/DB connection are untouched. **Folder IDs may re-mint** → after the move,
   re-capture Gianluigi's ~14 folder IDs and update env vars.
3. **Dry-run one folder** before any bulk move; verify listing + create still work.
4. **Legal files must not land in a Gianluigi-watched folder** (`RAW_TRANSCRIPTS_FOLDER_ID`,
   `DOCUMENTS_FOLDER_ID`, `MEETING_SUMMARIES_FOLDER_ID`) or they'll be ingested/summarized.
5. **Nothing is deleted** during migration. Keep `eyalz111` as a Shared Drive member until a
   phase is verified; a move is reversible (an organizer can move an item back to My Drive).
6. Record every env-var value **before** changing it.

## Auth: why domain-wide delegation (DWD)

- **OAuth refresh token** (what failed 2026-07-27): bound to an account login + 2FA; can
  expire / need re-consent; breaks exactly like the locked bot mailbox did.
- **DWD**: a Google Cloud **service account** the Workspace admin authorizes to *impersonate*
  specific `cropsight.io` users for specific scopes. No password, no 2FA, no browser, no
  expiry — the standard for a server bot. One-time admin-console setup.

**DWD scopes to authorize** (Admin → Security → API controls → Domain-wide delegation → add
the service-account client ID with):
```
https://www.googleapis.com/auth/gmail.send
https://www.googleapis.com/auth/gmail.readonly
https://www.googleapis.com/auth/gmail.modify
https://www.googleapis.com/auth/drive          ← FULL drive, NOT drive.file
https://www.googleapis.com/auth/spreadsheets
https://www.googleapis.com/auth/calendar.readonly
```
> ⚠️ The Drive scope must be full **`drive`**. Today's `drive.file` only grants access to
> files the app itself created — it would NOT see transcripts/sheets moved into the Shared
> Drive by a human. This is a deliberate scope upgrade.

---

## Already done (Phase 1 foundation)

- `GMAIL_REFRESH_TOKEN` split shipped (branch `fix/gmail-dedicated-token-2026-07-27`, commit
  `3949b6b`, live `gianluigi-00203-4dd`, **inert**): `services/gmail.py` reads a dedicated
  Gmail token when set, else falls back to `GOOGLE_REFRESH_TOKEN`.
- `authenticate()` now logs the authenticated mailbox + warns if ≠ `GIANLUIGI_EMAIL`.
- `scripts/get_gmail_token.py` (Gmail-only OAuth helper).

These make Phase 1 a config flip. Under DWD, gmail.py switches to service-account
impersonation of `gianluigi@cropsight.io` instead of a refresh token.

---

## Phases  ( [E]=Eyal/admin · [C]=code/Claude · [B]=both )

### Phase 0 — Provision
- [ ] [E] Add **`gianluigi@cropsight.io`** (Business Standard — needed for Meet/Gemini
      transcripts + pooled Shared-Drive storage). Optionally confirm `nechama@`.
- [ ] [E] Google Cloud project → create a **service account**; note its **client ID**.
- [ ] [E] Admin → Security → API controls → **Domain-wide delegation** → add the client ID +
      the 6 scopes above.
- [ ] [E] Ensure Gmail / Drive / Sheets / Calendar APIs are enabled on the project.
- [ ] [E] Create **`CropSight Ops`** Shared Drive. Members: `gianluigi@` (Content Manager),
      `eyal.zror@` (Manager), `nechama@` (as needed).
- [ ] [E] Admin → Drive sharing → **allow external members on shared drives** (temporary, so
      `eyalz111` can perform the move).

### Phase 1 — Email disconnection  *(fast win: personal mail out)*
- [ ] [C] Switch `gmail.py` to service-account DWD impersonating `gianluigi@cropsight.io`
      (add `GOOGLE_SERVICE_ACCOUNT_JSON` + `GMAIL_IMPERSONATE` settings; keep token fallback).
- [ ] [B] Deploy; confirm the startup log reads **`Gmail API authenticated as
      gianluigi@cropsight.io`** (no ⚠️).
- [ ] [E] Test: forward a thread to `gianluigi@cropsight.io` → approval card appears in Telegram.
- [ ] ✅ Personal Gmail disconnected from ingestion **and** sending; self-loop class gone.

### Phase 2 — Drive + Sheets

> ### ⚠️ 2026-07-27 CORRECTION — "MOVE the folders" is IMPOSSIBLE (external-owned content)
> Verified live: **UI drag AND `files().update(addParents=…)` both 403.** Google's rule:
> *"You can't move folders or files EXTERNAL users own"* into an org shared drive — even when
> that external user is a Content Manager member. Everything is owned by the external
> `eyalz111@gmail.com` (the bot's Drive token), so the move path is dead. No admin toggle fixes
> external-OWNED content. (`gianluigi@` seat = Business Standard, live 0AIZ… drive verified.)
>
> **KEY ENABLER:** the bot (eyalz111, Content Manager) **CAN create NEW content in the shared
> drive** (verified — it made a folder + doc there → org-owned). So the migration is
> **CREATE fresh + COPY historical**, not move:
>
> 1. **Operational (bot going forward) — CREATE fresh in `CropSight Ops`:** the bot creates new
>    Raw Transcripts / Meeting Summaries / Meeting Prep / Weekly Digests / Documents folders in
>    the shared drive; re-point the ~14 folder-ID env vars to the new IDs. New summaries/preps →
>    org-owned immediately. **Tactiq must be re-pointed** to the new Raw Transcripts folder
>    (external dependency). Sheets: recreate the 3 in the shared drive (bot creates them) →
>    re-point sheet-ID env vars → the reconcile **repopulates from the DB** (source of truth);
>    or keep sheets in My Drive short-term (lower-priority disconnection gap).
> 2. **Historical files + heavy data — COPY in** (can't move): Google Takeout export → re-upload
>    by an internal account, or a migration tool (CloudM/BitTitan). New IDs (fine for archives).
> 3. **Identity switch (final hardening):** flip the Drive/Sheets token from eyalz111 →
>    `gianluigi@` (add Drive scope + a gianluigi@ token or DWD) once operational content lives in
>    the shared drive. Optional to test first: Admin "editors can move files into shared drives"
>    (Migration settings) + share a folder to eyal.zror@ as Editor + try a move — but Google says
>    external-owned still can't move, so expect failure.
>
> The steps below are the SUPERSEDED "move" plan, kept for context.

- [x] [C] Ship **Shared Drive support** in `services/google_drive.py` (DONE — `_list_scope()`,
      supportsAllDrives everywhere; verified live that the two flags list shared-drive folders).
- [ ] ~~[E] Move folders/Sheets into `CropSight Ops` via UI~~ — **BLOCKED** (external-owned; see above).
- [ ] [C] Create fresh operational folders in the shared drive; re-capture + set the ~14 env vars.
- [ ] [E] Re-point Tactiq to the new Raw Transcripts folder.
- [ ] [C] Recreate the 3 sheets in the shared drive; re-point; let reconcile repopulate.
- [ ] [E/tool] Copy historical files + heavy data into the shared drive (Takeout / CloudM).
- [ ] [C] Flip Drive/Sheets token to `gianluigi@` (final disconnection).
- [ ] ✅ Personal Drive disconnected; all content org-owned.

### Phase 3 — Calendar
- [ ] [C] Re-point calendar auth to **`eyal.zror@cropsight.io`** (DWD impersonation, or a
      re-issued token). Keep the **color-`3` (purple) = CropSight** filter.
- [ ] [B] Verify meeting-prep sees CropSight events and skips personal ones.
- [ ] ✅ Personal calendar disconnected — full Google disconnection complete.

### Phase D — Supabase  *(separate, last — do not couple to the Google move)*
- [ ] [E] Create a **CropSight Supabase org** (CropSight billing). Be owner of the source
      org + member of the target.
- [ ] [E] Project → Settings → General → **Transfer project** → CropSight org. **URL + API
      keys + DB + storage are preserved** → no code change, connection unbroken.
- [ ] [B] Verify `/health` + a live query post-transfer.

---

## Rollback per phase
- **P1:** revert gmail.py auth to `GOOGLE_REFRESH_TOKEN` (eyalz111). Instant.
- **P2:** an organizer moves items back out of the Shared Drive to My Drive (restores personal
  ownership); revert env vars from the recorded pre-move values; revert auth. Keep `eyalz111`
  a member until verified. Nothing is deleted.
- **P3:** revert `EYAL_CALENDAR_REFRESH_TOKEN`.
- **PD:** Supabase transfer is reversible (transfer back); keys unchanged either way.

## Cost
~$14/seat/mo (Business Standard) — `gianluigi@`, optionally `nechama@`. Supabase org billing
moves with the transfer. Offsets a Tactiq subscription once Meet/Gemini transcripts land.

## Open decisions
- Business Standard vs Starter for `gianluigi@` (Standard needed for Meet/Gemini transcript
  ingestion — the Tactiq replacement; Starter = mailbox only).
- Whether to pursue Meet/Gemini transcription (Tactiq replacement) as a follow-on after P2.
