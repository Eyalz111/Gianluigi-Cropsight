"""Generate a PDF preview of a meeting summary WITHOUT distributing it.

For inspecting PDF quality/formatting before sending to the team. Runs the exact
production pipeline: the stored structured content -> Word .docx (word_generator)
-> PDF (Google Drive conversion). Writes the PDF to a local path. Creates + deletes
a temp Google Doc in the Meeting Summaries folder; touches NOTHING else (read-only
on the meeting/approval — safe to run against a pending summary).

    python scripts/preview_summary_pdf.py <meeting_id> [out.pdf]
"""
import asyncio
import sys

from services.supabase_client import supabase_client
from services.google_drive import drive_service
from services.word_generator import generate_summary_docx


async def main(meeting_id: str, out_path: str) -> None:
    meeting = supabase_client.get_meeting(meeting_id)
    if not meeting:
        print(f"meeting {meeting_id} not found")
        return

    # Prefer the pending_approval's structured content (exactly what distribution
    # feeds the docx generator); fall back to the DB rows for the meeting.
    pa = supabase_client.get_pending_approval(meeting_id) or {}
    content = pa.get("content") or {}

    decisions = content.get("decisions")
    if decisions is None:
        decisions = supabase_client.list_decisions(meeting_id=meeting_id, include_pending=True)
    tasks = content.get("tasks")
    if tasks is None:
        tasks = [t for t in supabase_client.get_tasks(status=None, include_pending=True)
                 if t.get("meeting_id") == meeting_id]
    follow_ups = content.get("follow_ups") or []
    open_questions = content.get("open_questions")
    if open_questions is None:
        open_questions = supabase_client.get_open_questions(meeting_id=meeting_id, include_pending=True)

    docx_bytes = generate_summary_docx(
        meeting_title=meeting.get("title", ""),
        meeting_date=str(meeting.get("date", ""))[:10],
        participants=meeting.get("participants", []) or [],
        duration_minutes=meeting.get("duration_minutes", 0) or 0,
        sensitivity=meeting.get("sensitivity", "founders"),
        decisions=decisions or [],
        tasks=tasks or [],
        follow_ups=follow_ups,
        open_questions=open_questions or [],
        discussion_summary=content.get("discussion_summary", "") or meeting.get("summary", ""),
        stakeholders_mentioned=content.get("stakeholders", []) or [],
    )
    print(f"generated .docx: {len(docx_bytes)} bytes "
          f"({len(decisions or [])} decisions, {len(tasks or [])} tasks)")

    pdf = await drive_service.docx_to_pdf_bytes(docx_bytes)
    if not pdf:
        print("PDF conversion returned empty — check Drive auth / MEETING_SUMMARIES_FOLDER_ID")
        return
    with open(out_path, "wb") as f:
        f.write(pdf)
    print(f"wrote PDF: {len(pdf)} bytes -> {out_path}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python scripts/preview_summary_pdf.py <meeting_id> [out.pdf]")
        sys.exit(1)
    mid = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else "preview.pdf"
    asyncio.run(main(mid, out))
