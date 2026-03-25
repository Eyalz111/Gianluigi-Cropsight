# Phase 10 Review Notes — Architecture & Implementation Concerns

**Date:** March 25, 2026
**Context:** Review of the Phase 10 "Polish & Ship" plan. Read alongside the Phase 10 spec. Address each point during implementation or explain why it's not needed.

---

## 1. Define Task Column Mapping as a Single Constant

The Tasks sheet column reorder (A1) is the riskiest item in Phase 10. Every function that references a column letter — `add_task()`, `find_task_row()`, `update_task_row()`, `get_all_tasks()`, the archival scheduler from 9A, the MCP `create_task` and `update_task` write paths, the Sheets sync from Phase 8 — all need to update simultaneously. Missing one means tasks silently write to wrong columns.

**Action:** Before touching any function, define the column mapping as a constant in one place (top of `services/google_sheets.py` or in `config/settings.py`):

```python
TASK_COLUMNS = {
    "priority": "A",
    "label": "B",
    "task": "C",
    "owner": "D",
    "deadline": "E",
    "status": "F",
    "category": "G",
    "source_meeting": "H",
    "created": "I",
}
```

Every function references `TASK_COLUMNS["status"]` instead of hardcoded `"F"`. This makes the reorder a single-point change, and any future reorder never requires a codebase grep. Do this as the **first step** of A1 before modifying any functions.

Do the same for the Decisions sheet if column letters are hardcoded there.

---

## 2. Add a Backup Step to the Rebuild Script

A3 clears the entire Tasks tab and rebuilds from Supabase. If the script has a bug (e.g., Supabase query returns empty due to a connection issue), the Tasks sheet is blanked with no recovery path.

**Action:** Before clearing any sheet tab, the rebuild script should:
1. Export current sheet content to a backup tab (e.g., `Tasks_Backup_20260325`)
2. Proceed with the clear + rebuild
3. Log the backup tab name so Eyal can compare before/after and catch mapping bugs visually

Apply the same backup step to the Decisions rebuild in A2. One extra API call per sheet, trivial cost, prevents the worst case.

---

## 3. Add Context to Haiku Decision Label Backfill

A2 uses Haiku to retroactively label old decisions with empty label fields. But if Haiku only sees the decision text (e.g., "Agreed to push timeline by 2 weeks"), it has no context to determine which project this belongs to.

**Action:** For each decision being labeled, pass to Haiku:
- The decision text
- The canonical project names list (from `config/projects.py`)
- The meeting title (from the linked meeting record)
- The meeting participants

The meeting title alone ("Founders Technical Review — Moldova Pilot Update") is usually enough to disambiguate. Without this context, Haiku will produce generic labels like "Timeline" instead of "Moldova Pilot."

**Also:** Run the backfill in dry-run mode first — print proposed labels to stdout without writing, let Eyal review, then run for real. Wrong labels propagate into topic threading.

Implementation: add a `--dry-run` flag to `scripts/backfill_decision_labels.py`.

---

## 4. Verify Actual Tool Count Before Writing the Prompt

The Phase 9 plan settled on 33 tools, but between Phase 9 implementation and Phase 10, the count may have drifted — a tool got split, a deprecated tool got removed, or collapsing happened differently than planned.

**Action:** Before writing B1, count the actual registered tools from code:

```bash
# Whatever pattern matches tool registration in mcp_server.py
grep -c "def tool_" services/mcp_server.py
# or count @mcp.tool() decorators, however tools are registered
```

Write the prompt from the actual registered tools, not from the plan. If the count doesn't match 33, update the prompt AND the Phase 10 spec to match reality.

---

## 5. Add Missing Tool Routing Hints to B3

The current routing hints cover basics well. Add these patterns from real usage:

```
SESSION ONBOARDING:
- Eyal starts a conversation with no context →
  get_last_session_summary() first, then get_system_context()

PEOPLE & COMPANIES:
- Eyal asks about a person or company →
  get_stakeholder_info() first, then search_memory() if record is thin

SYSTEM STATUS:
- Eyal asks "is everything working?" → get_system_health()

IMPORTANT DISTINCTION — quick_inject vs save_session_summary:
- "Remember this" / "Log this" / Eyal shares operational info →
  quick_inject() (extracts structured items into DB)
- End of a Claude.ai work session, preserving conversation context →
  save_session_summary() (saves prose summary for next session continuity)
These serve completely different purposes. Never use save_session_summary
for operational information injection.

DECISION LOOKUP:
- "What did we decide about X?" →
  get_decisions(topic=X) first, then search_memory() if no decisions found
```

The `quick_inject` vs `save_session_summary` distinction is especially important — they sound similar but do very different things. Without explicit guidance, Claude.ai will use the wrong one.

---

## 6. Add Email Distribution Check to QA Checklist (C2)

The manual QA tests Sheets, Claude.ai tools, Telegram, and Hebrew search — but doesn't test the email output that Phase 9D polished. After the "clean prose excerpt" email changes, verify the format actually works.

**Action:** Add step 8 to C2:

```
8. Drop a transcript → approve → verify email arrives with:
   - Clean HTML formatting (no raw markdown artifacts)
   - TLDR section present
   - Decision labels visible
   - "See attached" with actual .docx attachment
   - No broken formatting in Gmail / Outlook web view
```

---

## 7. Check Import Chains When Removing Deprecated Code (D1)

Removing commitment methods from 4+ files is straightforward, but if any module *imports* a deleted function, it fails at startup — not at test time.

**Action:** After removing all deprecated functions, verify each modified module loads:

```bash
python -c "from processors.weekly_review import *"
python -c "from processors.weekly_review_session import *"
python -c "from processors.cross_reference import *"
python -c "from services.google_sheets import *"
```

Run these BEFORE the full test suite. An import error crashes the entire module, which may cause tests to be skipped rather than fail — hiding the problem.

Also: grep the entire codebase for any remaining references to the removed function names:

```bash
grep -rn "commitment" --include="*.py" | grep -v "test_" | grep -v "__pycache__"
```

Any hit outside of test files is a potential runtime error.

---

## 8. Specify Data Validation Removal Targets (D3)

The plan says "remove data validation entirely (recommended)" but doesn't specify which columns currently have dropdowns. Claude Code needs to know what to remove.

**Action:** Identify and document which columns have `setDataValidation` calls:
- Likely: Status column (dropdown with pending/in_progress/done/overdue)
- Possibly: Category column, Priority column

Search for `DataValidation` or `setDataValidation` in `services/google_sheets.py` and list all instances. Remove them all — conditional color formatting (which you're keeping) gives the same visual cues without the validation errors when data doesn't match.

---

## 9. Add Canonical Project Names to the Claude.ai Prompt

B1 documents all 33 tools. B2 adds workflow guidance. B3 adds routing hints. But none of them tell Claude.ai about the canonical project names from `config/projects.py`. When Eyal says "Moldova" in Claude.ai, Claude should normalize to "Moldova Pilot" and call `get_topic_thread("Moldova Pilot")`, not `search_memory("Moldova")`.

**Action:** Add a section to the project prompt (B1 or B2):

```
CROPSIGHT PROJECTS (canonical names — use these in tool calls):
- Moldova Pilot — wheat yield PoC, Gagauzia region, first client
- Pre-Seed Fundraising — IIA Tnufa program + next funding round
- SatYield Accuracy Model — core ML product, 85-91% accuracy
- Operational Tooling — Gianluigi system development
- Coffee/Cocoa Expansion — post-wheat crop expansion target

When Eyal mentions any variation of these (e.g., "Moldova", "Gagauzia",
"the PoC", "fundraising", "the model"), use the canonical name for
get_topic_thread(), get_decisions(), and search_memory() calls.
```

This closes the loop between the extraction prompt (normalizes during processing) and the Claude.ai prompt (normalizes during conversation).

---

## 10. Create a Synthetic Smoke Test Transcript

After Sheets rebuilding, column reordering, and email formatting changes, the most comprehensive test is dropping a transcript through the full pipeline. But real transcripts contain sensitive CropSight content and require actual meetings to generate.

**Action:** Create `tests/fixtures/phase10_smoke_test_transcript.txt` — a synthetic 10-minute "CropSight Weekly Sync" with:
- 2 decisions (one about Moldova Pilot, one about Pre-Seed Fundraising — tests canonical labels)
- 3 tasks (assigned to different team members — tests owner column)
- 1 open question (tests open question extraction)
- Mentions of a known stakeholder (tests stakeholder matching)
- A reference to a previous decision (tests supersession detection from 9A)

This gives Claude Code something to test the full pipeline against during implementation without waiting for a real meeting, and it's reusable for future phase QA. Use it as the transcript for C2 step 1.

---

## Summary: Implementation Checklist

**Before starting A1:**
1. Define `TASK_COLUMNS` constant (point 1) — do this first, everything depends on it
2. Verify actual MCP tool count from code (point 4)

**During Track A:**
3. Add backup step to rebuild script (point 2)
4. Add context + dry-run to Haiku backfill (point 3)
5. Specify and remove data validation targets (point 8)

**During Track B:**
6. Add missing routing hints (point 5)
7. Add canonical project names to prompt (point 9)

**During Track C:**
8. Add email format check to QA checklist (point 6)
9. Create synthetic smoke test transcript (point 10)

**During Track D:**
10. Verify import chains after deprecated code removal (point 7)

---

*Generated from Claude.ai architecture review session, March 25, 2026*
*Send to Claude Code before starting Phase 10 implementation*
