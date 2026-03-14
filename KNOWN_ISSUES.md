# Known Issues — Gianluigi v0.5

Bugs and limitations discovered during live testing (Feb 25 – Mar 13, 2026).
Issues marked **FIXED** have been resolved. Open issues should be addressed in v1.0.

---

## Open Issues

### Meeting Prep Quality
- **Too much noise:** Prep docs pull in loosely related context, making them long and unfocused. The RAG search returns quantity over relevance for prep generation.
- **Wrong context for meeting type:** A BD meeting prep might include unrelated product/legal context. No filtering by meeting category or attendee relevance.
- **Timing issues:** Prep generation triggers based on calendar proximity, but sometimes fires too early or too late depending on how far ahead the meeting was created.

### Multi-Turn Conversation Memory
- **Data handling errors:** Conversation memory (in-memory, TTL 30min) occasionally produces errors during long edit cycles in the approval flow.
- **Formatting drift:** After multiple edit rounds, the summary formatting can degrade — sections get reordered or structural elements (headers, bullet points) get lost.

### Telegram UX
- **Polling vs webhook:** Using `run_polling()` on Cloud Run with `min-instances=1`. Works but not ideal — a cold start means missed messages until the instance is warm.
- **Long messages truncated:** Telegram has a 4096-char limit per message. Long approval previews or search results sometimes get cut off without a clean split.

### Email Watcher
- **5-minute polling delay:** Not real-time. If Eyal replies to an approval email, it takes up to 5 minutes to be processed.
- **Reply text extraction fragile:** `_extract_reply_text()` uses marker-based splitting (`\nOn `, `\n>`, etc.) which can fail on non-standard email clients or forwarded chains.

### Document Ingestion
- **No OCR:** Scanned PDFs produce empty text extraction. Only text-based PDFs work.
- **No image processing:** Charts, diagrams, and images in PPTX/DOCX are ignored.
- **Basic chunking:** Fixed-size character chunking doesn't respect document structure (sections, headings).

---

## Fixed Issues (v0.5)

### Tasks Going to Wrong Sheets Tab (Fixed Mar 13)
- **Symptom:** Tasks appeared in the Commitments tab instead of the Tasks tab, without formatting.
- **Root cause:** `_append_rows()` used `range="A:I"` without a tab name. When the Commitments tab existed, Google Sheets API sometimes resolved the unqualified range to it.
- **Additional:** `apply_edits()` stripped `category` and `status` fields from tasks during editing.
- **Fix:** Added `tab_name` parameter to `_append_rows()`/`_append_row()`, all task callers pass `tab_name="Tasks"`, first sheet renamed to "Tasks" in `format_task_tracker()`. Category/status preserved through edit round-trip.

### Email Approval Route Incomplete (Fixed Mar 13)
- **Symptom:** Email replies to approval requests were logged but never processed. Replying "approve" by email had no effect.
- **Root cause:** `_handle_approval_reply()` only logged the action. Comment said "actual approval handling happens through meeting_id which we'd need to extract from the subject or thread."
- **Fix:** Added `[ref:{meeting_id[:8]}]` tag to approval email subjects. Rewrote handler to extract ref, look up pending approval, and call `process_response()`. Sends Telegram confirmation after processing.

### Edit Count Message Inaccurate (Fixed Mar 13)
- **Symptom:** "Applied 1 edit(s)" when 2 edits were requested.
- **Root cause:** `len(edits)` counted LLM-parsed instructions, not actual changes applied. The LLM might merge two edits into one instruction or split one into many.
- **Fix:** Replaced with generic "Edits applied successfully. A new approval request has been sent."

---

## Fixed Issues (Pre-v0.5)

These were found and fixed during earlier live testing sessions. Listed for reference to avoid regression.

### Pipeline & Extraction
- `EMBEDDING_MODEL` typo in `.env` (`smal` not `small`)
- `_serialize_datetime` crashed on unparseable dates from Claude
- Transcript parser only matched `[MM:SS]` format, not Tactiq's actual unbracketed `MM:SS Speaker:` format
- Participant extraction regex didn't handle lowercase names
- PostgREST ambiguity on `open_questions` table (two FKs to `meetings`)
- `create_task_mentions_batch` failed on FK violations when tasks were deduplicated — added resilience

### Supabase Client
- All methods are SYNC but were being `await`ed in 3 schedulers — caused runtime errors
- `ts_rank()` returns `real` not `double precision` — needed `::FLOAT` cast in RPC
- Search threshold was too high (0.7) — lowered to 0.4 for better recall

### Telegram Bot
- Approval messages sent to group chat instead of Eyal's personal DM
- Message formatting broken — sending raw Markdown in HTML parse mode
- `head -50` pipe on `python main.py` left orphan process causing 409 Conflict on restart
- `start()` returned immediately — `asyncio.wait(FIRST_COMPLETED)` shut down all services

### Gmail
- `invalid_scope` error — `gmail.modify` scope not in initial OAuth token
- Sender verification fallback missing — non-standard From headers caused crashes

### Google Sheets
- `Sheet1!` prefix caused errors — omit tab prefix to default to first tab
- `sheetId` assumed to be 0 — added `_get_first_sheet_id()` helper
- Sheets category names didn't match Claude extraction output — updated to BD & Sales, Legal & Compliance, Strategy & Research

### Calendar
- Calendar filter expected `{email: ...}` dicts but watcher passed string names
- `EYAL_TELEGRAM_ID` vs `TELEGRAM_EYAL_CHAT_ID` naming inconsistency — added fallback

### Configuration
- pydantic-settings does NOT export .env values to `os.environ` — OpenAI embeddings couldn't find API key
- `OPENAI_API_KEY` fallback needed when `EMBEDDING_API_KEY` not set
- Module-level constant capture in schedulers broke test mockability — moved to runtime resolution in `__init__`
