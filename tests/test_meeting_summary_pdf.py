"""Meeting-summary PDF distribution (2026-07-12): PDF replaces the Word doc as the
email attachment + Drive archive, converted Google-natively from the .docx.

- gmail.send_meeting_summary attaches the PDF when pdf_bytes is given (falls back
  to the .docx otherwise).
- google_drive.docx_to_pdf_bytes converts via a temp Google Doc and cleans it up.
"""
import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _decode_raw(execute_send_mock) -> str:
    raw_b64 = execute_send_mock.call_args.args[0]["raw"]
    return base64.urlsafe_b64decode(raw_b64).decode("utf-8", "ignore")


class TestEmailAttachment:
    async def test_pdf_attached_when_pdf_bytes_present(self):
        from services.gmail import GmailService
        g = GmailService.__new__(GmailService)
        g.sender_email = "bot@x.com"
        g._execute_send = AsyncMock(return_value={"id": "m1"})
        ok = await g.send_meeting_summary(
            recipients=["a@x.com"], meeting_title="Board Meeting", summary_content="s",
            drive_link="http://d", meeting_date="2026-07-12",
            pdf_bytes=b"%PDF-1.4 fake", docx_bytes=b"docx fake",
        )
        assert ok is True
        raw = _decode_raw(g._execute_send)
        assert "application/pdf" in raw and ".pdf" in raw
        assert "wordprocessingml" not in raw   # the .docx is NOT attached when a PDF exists

    async def test_falls_back_to_docx_when_no_pdf(self):
        from services.gmail import GmailService
        g = GmailService.__new__(GmailService)
        g.sender_email = "bot@x.com"
        g._execute_send = AsyncMock(return_value={"id": "m1"})
        await g.send_meeting_summary(
            recipients=["a@x.com"], meeting_title="M", summary_content="s",
            drive_link="http://d", meeting_date="2026-07-12",
            docx_bytes=b"docx fake",   # no pdf_bytes
        )
        raw = _decode_raw(g._execute_send)
        assert "wordprocessingml" in raw   # the .docx is attached as fallback


class TestDocxToPdf:
    def _svc(self):
        from services.google_drive import GoogleDriveService
        with patch.object(GoogleDriveService, "__init__", lambda self: None):
            svc = GoogleDriveService()
        svc._service = MagicMock()
        return svc

    async def test_converts_exports_pdf_and_cleans_up(self):
        svc = self._svc()
        f = svc._service.files.return_value
        f.create.return_value.execute.return_value = {"id": "doc1"}
        f.export_media.return_value.execute.return_value = b"%PDF-1.4 real"
        f.delete.return_value.execute.return_value = None
        out = await svc.docx_to_pdf_bytes(b"docx content")
        assert out == b"%PDF-1.4 real"
        assert f.export_media.call_args.kwargs.get("mimeType") == "application/pdf"
        f.delete.assert_called_once()   # temp Google Doc removed

    async def test_empty_input_returns_empty(self):
        svc = self._svc()
        assert await svc.docx_to_pdf_bytes(b"") == b""

    async def test_export_failure_returns_empty_and_cleans_up(self):
        svc = self._svc()
        f = svc._service.files.return_value
        f.create.return_value.execute.return_value = {"id": "doc1"}
        f.export_media.return_value.execute.side_effect = RuntimeError("export boom")
        out = await svc.docx_to_pdf_bytes(b"docx")
        assert out == b""
        f.delete.assert_called_once()   # temp Doc still cleaned up on failure
