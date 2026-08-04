# Transcription migration: Tactiq → Gemini / Google Meet

Status: **PLAN — nothing built, and mostly SHOULD NOT BE.** Written 2026-08-04,
revised same day after Eyal's decisions.
Owner: Eyal (decisions) · Gianluigi (implementation)

> ## ⚠️ DECISIONS TAKEN 2026-08-04 — read before building anything
>
> 1. **No Business Plus upgrade.** Transcription stays manual-start per meeting.
> 2. **Zoom/Teams still in use for some external meetings** → Gemini can never give
>    full coverage, so **Tactiq stays as the backbone**.
> 3. Meet artifacts should end up in `CropSight Ops` — but see the cost note in §5.
> 4. Option A vs B deferred until we have real samples.
>
> **Consequence — the dual-source migration in §5 Phases 2–4 is NOT recommended.**
> With Tactiq staying anyway, building a second automated source buys no new coverage,
> adds dedup/parser/recursion complexity, and *worsens* ergonomics (manual click per
> call). The remaining arguments for Gemini are governance (Tactiq is a third party
> that sees FOUNDERS-tier content) and redundancy — real, but not urgent. Cost is
> **not** an argument: the Tactiq subscription is needed for Zoom/Teams regardless.
>
> **What IS worth doing — and it is free.** `drive_service.download_file()` exports
> Google Docs to `text/plain` (`services/google_drive.py:450`), so **a Meet transcript
> Doc dropped into `Raw Transcripts` already ingests with zero code changes** — the
> same path as Eyal's 2026-07-30 manual drop. That makes Gemini usable *today* for
> sensitive internal meetings, gated on exactly one unknown: **does the Meet Doc body
> parse as `[HH:MM:SS] Speaker: text`?** (G4/G5). If not, speaker extraction fails →
> the "2+ team speakers" classifier degrades to *uncertain* and starts DMing Eyal
> rather than erroring visibly.
>
> **→ Next action: Phase 1 only.** One real Meet call with transcription on, Doc
> dropped into `Raw Transcripts`, inspect the actual format. That single sample
> decides whether the free path works and whether A/B was ever worth considering.

Goal: move meeting capture from Tactiq to native Google Meet transcription, **without
degrading the extraction pipeline that already works**. This document is the holistic
view — what we do today, what Google actually gives us, every gap, and a phased path.

---

## 1. What we do today (Tactiq)

| Aspect | Today |
|---|---|
| Capture | Tactiq auto-records, no per-meeting action |
| Delivery | Auto-export to a **flat** Drive folder |
| Filename | `2026-07-30CropSight R&D&Ido` (date jammed onto title, no separator) |
| Body format | `[HH:MM:SS] Speaker: text` — or unbracketed `MM:SS Speaker: text` |
| Platforms | Google Meet **+ Zoom + Teams** |
| Ingestion | `schedulers/transcript_watcher.py` polls every 900 s |
| Dedup | `meetings.source_file_path ILIKE %filename%` |

**Code that depends on the Tactiq format** (this is the blast radius of any format change):

- `schedulers/transcript_watcher.py::_parse_filename` — three date patterns, incl. the
  no-separator form specific to Tactiq exports.
- `schedulers/transcript_watcher.py::_extract_participants_from_transcript` — regexes
  speaker names out of `[HH:MM:SS] Speaker:`. **This feeds the CropSight/not-CropSight
  classifier** (the "2+ known team speakers ⇒ CropSight" rule), so if speaker parsing
  breaks, classification silently degrades to "uncertain" and starts DMing Eyal.
- `services/embeddings.py::_parse_utterances` — same two formats; powers semantic search
  chunking.
- Extraction prompt output cites `(ref: ~25:09)` timestamps — these come from the
  transcript body, so timestamp fidelity is a user-visible quality feature.

---

## 2. What Google actually gives us

**Two different features. They are not interchangeable.**

| | "Transcribe" | "Take notes for me" (Gemini) |
|---|---|---|
| Output | Verbatim Doc, speaker labels + timestamps | AI **summary** notes |
| Right input for us? | ✅ **yes** | ❌ no — summarising a summary |
| Business Standard | included | included (default-ON from **2026-09-21**) |
| Automatic? | ❌ **manual start per meeting** | ✅ automatic |

> **The core tension.** The artifact we want (verbatim transcript) is the one that is
> *not* automatic on Business Standard. Admin-set auto-transcription requires **Business
> Plus or Enterprise**. So on the current plan, someone must click "Start transcription"
> in every meeting — a regression from Tactiq's zero-touch capture.

**Where artifacts land** (changed July 2026): the **host's My Drive** → `Google Meet/` →
**one subfolder per meeting** (recurring instances share a folder). Attendees with access
get **shortcuts** in their own `Google Meet` folder. The old `Meet Recordings` folder is
renamed `Legacy Meet Recordings`. Transcripts appear within a few hours, **up to 24 h**.

**Consent gate** (April 2026): admin-controlled, **OFF by default**, settable at
domain/OU/group. Not forced on us. Participants still see the standard "transcription is
on" notice. → *Not a blocker.*

---

## 3. Gaps to close

| # | Gap | Impact | Fix |
|---|---|---|---|
| G1 | Watcher lists **direct children only** and excludes folders | Per-meeting subfolders are invisible → **nothing ingests** | Recursive listing, or resolve subfolders one level |
| G2 | Attendee copies are **shortcuts** (`application/vnd.google-apps.shortcut`) | Skipped or downloaded as empty | Resolve `shortcutDetails.targetId` before download |
| G3 | Artifacts land in **host's personal My Drive** | Re-creates the exact personal-ownership problem the migration just removed | Drive rule/automation to copy into `CropSight Ops`, or read via API instead |
| G4 | Meet transcript Doc format ≠ Tactiq format | Speaker + timestamp parsing breaks → classifier degrades, `ref:` citations lost | Format-detecting parser (see §4) |
| G5 | Meet filenames ≠ `YYYY-MM-DDTitle` | Wrong meeting date/title | Extend `_parse_filename`, or take metadata from the API/Calendar |
| G6 | Manual start per meeting (Business Standard) | Missed meetings = silent data loss | Business Plus upgrade, **or** accept discipline + add a "meeting had no transcript" alert |
| G7 | Meet-only | Zoom/Teams meetings captured today would be **lost** | Keep Tactiq for those — dual-source, not a cutover |
| G8 | Up to 24 h delay | Breaks the same-day debrief flow | Watcher already polls continuously; just set expectations |
| G9 | `pageSize=50`, no paging in `get_new_transcripts` | Fine today; with per-meeting subfolders the count grows fast | Add paging when recursion lands |

---

## 4. Two integration options

### Option A — Drive scraping (evolutionary)
Point the watcher at the `Google Meet` folder; add recursion (G1), shortcut resolution
(G2), and a format-detecting parser (G4).

- ✅ Reuses the entire existing pipeline; smallest conceptual change.
- ✅ Same approval/dedup/sensitivity path, already battle-tested.
- ❌ Regex-parsing a Google Doc — brittle if Google changes layout.
- ❌ Still depends on files sitting in someone's personal My Drive (G3).

### Option B — Google Meet REST API v2 (structural)
Read `conferenceRecords.transcripts.entries` directly: **structured** entries with
speaker, text, and start/end times — no regex at all.

- ✅ Robust, structured, immune to Doc-layout changes.
- ✅ Ties naturally to the Calendar event → better meeting identity than filename dedup
  (would also fix the long-deferred "dedup by meeting identity, not filename" item).
- ❌ New scope `meetings.space.readonly` (App Access Control change).
- ❌ **Open question: whose meetings can the bot see?** Likely needs Eyal's identity
  (his token, or domain-wide delegation) — must be verified before committing.
- ❌ Google warns API entries "might not match" the Doc exactly.

**Leaning: B for the data, A for the plumbing** — use the Meet API to fetch structured
transcripts, but feed them into the *existing* processor pipeline by rendering to the
canonical `[HH:MM:SS] Speaker: text` form. That keeps one parser, one approval flow, and
makes the transcript source pluggable.

---

## 5. Phased plan

**Phase 0 — restore today's flow.** Re-point Tactiq to `CropSight Ops/Raw Transcripts`
(`1ECWm_evyWt2zjg1RP1f6UuyFsUA1XawD`). *Eyal action, 2 min.* Unblocks auto-ingest now.

**Phase 1 — shadow (no code).** Run Meet transcription manually on 2–3 real meetings
alongside Tactiq. Collect: exact filename, exact body format, where it landed, how long it
took. **This is the cheapest way to de-risk G4/G5 — do not design the parser before we
have real samples.** Also settles the Option B access question.

> **Phases 2–5 are PARKED per the 2026-08-04 decisions above.** Kept for reference in
> case the governance argument (third-party access to FOUNDERS-tier content) or a plan
> upgrade later changes the calculus. Do not start them without revisiting the header.
>
> Note on decision 3 (**automate artifacts into `CropSight Ops`**): Drive has **no
> native "move new files to folder X" rule**. Automating it needs either an Apps Script
> trigger on Eyal's account, or the watcher reading the `Google Meet` folder directly —
> which re-introduces G1 (recursion) and G2 (shortcuts). So this is *not* free, and is
> only worth doing if Gemini use goes beyond occasional manual drops.

**Phase 2 — source-agnostic ingestion.** Introduce a `TranscriptSource` seam that
normalises any source to the canonical format + metadata `{title, date, participants}`.
Tactiq becomes one implementation, Meet another. No behaviour change on day one.

**Phase 3 — watcher hardening.** Recursion (G1), shortcut resolution (G2), paging (G9),
multi-folder watching. Ship behind a flag, shadow-mode first.

**Phase 4 — dual-run.** Both sources live, dedup by meeting identity so the same meeting
captured twice doesn't double-ingest. This is where G7 stops being a problem — Meet for
Meet calls, Tactiq for Zoom/Teams.

**Phase 5 — retire Tactiq.** Only if coverage is genuinely complete. If CropSight still
meets partners on Zoom/Teams, **the honest answer may be "never fully"**.

---

## 6. Decisions needed from Eyal

1. **Business Plus upgrade?** Without it there is no automatic transcription, and the
   whole thing depends on someone remembering to press a button. This is the single
   biggest determinant of whether Gemini can actually replace Tactiq.
2. **Do we still meet on Zoom/Teams?** If yes, Tactiq stays regardless (G7) and this
   becomes an *addition*, not a migration.
3. **Where should Meet artifacts live?** They default to personal My Drive (G3). Accept,
   or add an automation to land them in `CropSight Ops`?
4. **Option A or B** — decide after Phase 1 samples, not before.

## 7. Explicitly out of scope
Recordings (video) and Gemini summary notes. We want the verbatim transcript only;
Gianluigi does its own extraction and a second summariser would fight it.
