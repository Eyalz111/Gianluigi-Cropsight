"""
Google Drive API integration.

This module handles all Google Drive operations:
- Reading files from the Raw Transcripts folder (Tactiq exports)
- Reading files from the Documents folder (team uploads)
- Writing summary documents to Meeting Summaries folder
- Writing prep documents to Meeting Prep folder
- Writing weekly digests to Weekly Digests folder
- Watching for new files (polling)

Google Drive folder structure:
    CropSight Ops/
    ├── Raw Transcripts/      ← Input (Tactiq auto-exports)
    ├── Meeting Summaries/    ← Output (approved summaries)
    ├── Meeting Prep/         ← Output (prep documents)
    ├── Weekly Digests/       ← Output (weekly summaries)
    └── Documents/            ← Input (team uploads for ingestion)

Usage:
    from services.google_drive import drive_service

    # Check for new transcripts
    new_files = await drive_service.get_new_transcripts()

    # Check for new documents
    new_docs = await drive_service.get_new_documents()

    # Save a meeting summary
    await drive_service.save_meeting_summary(content, filename)
"""

import io
import logging
from typing import Any

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload

from config.settings import settings

logger = logging.getLogger(__name__)


class GoogleDriveService:
    """
    Service for Google Drive API operations.

    Uses OAuth2 credentials for authentication.
    """

    def __init__(self):
        """
        Initialize the Google Drive service with credentials.
        """
        self._service = None
        self._credentials: Credentials | None = None
        # Track processed transcript files to avoid reprocessing
        self._processed_file_ids: set[str] = set()
        # Track processed document files to avoid reprocessing
        self._processed_doc_ids: set[str] = set()

    @property
    def service(self):
        """
        Lazy initialization of Drive API service.

        Uses OAuth2 credentials from settings.
        """
        if self._service is None:
            self._service = self._build_service()
        return self._service

    def _build_service(self):
        """Build the Google Drive API service with OAuth2 credentials."""
        if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_CLIENT_SECRET:
            raise RuntimeError("Google OAuth credentials not configured")

        if not settings.GOOGLE_REFRESH_TOKEN:
            raise RuntimeError(
                "Google refresh token not configured. "
                "Run the OAuth flow to obtain a refresh token."
            )

        # Create credentials from refresh token
        self._credentials = Credentials(
            token=None,
            refresh_token=settings.GOOGLE_REFRESH_TOKEN,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=settings.GOOGLE_CLIENT_ID,
            client_secret=settings.GOOGLE_CLIENT_SECRET,
            scopes=[
                "https://www.googleapis.com/auth/drive.file",
                "https://www.googleapis.com/auth/drive.readonly",
            ],
        )

        # Refresh the token if needed
        if self._credentials.expired or not self._credentials.token:
            self._credentials.refresh(Request())

        return build("drive", "v3", credentials=self._credentials)

    async def authenticate(self) -> bool:
        """
        Authenticate with Google Drive API using OAuth2.

        Returns:
            True if authentication successful, False otherwise.
        """
        try:
            # Force service initialization to verify auth
            _ = self.service
            logger.info("Google Drive API authentication successful")
            return True
        except Exception as e:
            logger.error(f"Google Drive API authentication failed: {e}")
            return False

    # =========================================================================
    # Reading Files
    # =========================================================================

    async def get_new_transcripts(self) -> list[dict]:
        """
        Check for new transcript files in the Raw Transcripts folder.

        Uses the _processed_file_ids set for deduplication rather than
        relying on local clock time (which may be offset from Google's
        server time and cause files to be missed).

        Returns:
            List of file metadata dicts (id, name, createdTime).
        """
        if not settings.RAW_TRANSCRIPTS_FOLDER_ID:
            logger.warning("RAW_TRANSCRIPTS_FOLDER_ID not configured")
            return []

        try:
            # Build query — no time filter; rely on _processed_file_ids
            # for deduplication. This avoids clock-skew issues where
            # the local computer time differs from Google's server time.
            query_parts = [
                f"'{settings.RAW_TRANSCRIPTS_FOLDER_ID}' in parents",
                "trashed = false",
            ]

            query = " and ".join(query_parts)

            results = self.service.files().list(
                q=query,
                spaces="drive",
                fields="files(id, name, createdTime, mimeType, webViewLink)",
                orderBy="createdTime desc",
                pageSize=50,
            ).execute()

            files = results.get("files", [])

            # Filter out already processed files
            new_files = [
                f for f in files
                if f["id"] not in self._processed_file_ids
            ]

            logger.info(f"Found {len(new_files)} new transcript files")
            return new_files

        except Exception as e:
            logger.error(f"Error checking for new transcripts: {e}")
            return []

    async def get_new_documents(self) -> list[dict]:
        """
        Check for new files in the Documents folder.

        Returns files not yet processed. Uses _processed_doc_ids for
        deduplication rather than local clock time (avoids clock-skew).

        Returns:
            List of file metadata dicts (id, name, createdTime, mimeType).
        """
        if not settings.DOCUMENTS_FOLDER_ID:
            logger.warning("DOCUMENTS_FOLDER_ID not configured")
            return []

        try:
            # No time filter — rely on _processed_doc_ids for dedup
            query_parts = [
                f"'{settings.DOCUMENTS_FOLDER_ID}' in parents",
                "trashed = false",
            ]

            query = " and ".join(query_parts)

            results = self.service.files().list(
                q=query,
                spaces="drive",
                fields="files(id, name, createdTime, mimeType, webViewLink, size)",
                orderBy="createdTime desc",
                pageSize=50,
            ).execute()

            files = results.get("files", [])

            # Filter out already processed documents
            new_files = [
                f for f in files
                if f["id"] not in self._processed_doc_ids
            ]

            logger.info(f"Found {len(new_files)} new document files")
            return new_files

        except Exception as e:
            logger.error(f"Error checking for new documents: {e}")
            return []

    async def download_file_bytes(self, file_id: str) -> bytes:
        """
        Download a file's raw bytes.

        Used for binary files like PDFs and .docx that need
        format-specific extraction before text processing.

        Args:
            file_id: Google Drive file ID.

        Returns:
            Raw file bytes.
        """
        try:
            request = self.service.files().get_media(fileId=file_id)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                status, done = downloader.next_chunk()

            raw_bytes = fh.getvalue()
            logger.info(f"Downloaded file bytes {file_id}: {len(raw_bytes)} bytes")
            return raw_bytes

        except Exception as e:
            logger.error(f"Error downloading file bytes {file_id}: {e}")
            return b""

    def mark_document_processed(self, file_id: str) -> None:
        """
        Mark a document file as processed so it won't be returned again.

        Args:
            file_id: Google Drive file ID.
        """
        self._processed_doc_ids.add(file_id)

    async def download_file(self, file_id: str) -> str:
        """
        Download a file's content as text.

        Args:
            file_id: Google Drive file ID.

        Returns:
            File content as string.
        """
        try:
            # Get file metadata to check type
            metadata = await self.get_file_metadata(file_id)
            mime_type = metadata.get("mimeType", "")

            # Handle Google Docs - export as plain text
            if mime_type == "application/vnd.google-apps.document":
                request = self.service.files().export_media(
                    fileId=file_id,
                    mimeType="text/plain"
                )
            else:
                # Regular file - download content
                request = self.service.files().get_media(fileId=file_id)

            # Download content
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                status, done = downloader.next_chunk()

            content = fh.getvalue().decode("utf-8", errors="ignore")

            # Mark as processed
            self._processed_file_ids.add(file_id)

            logger.info(f"Downloaded file {file_id}: {len(content)} chars")
            return content

        except Exception as e:
            logger.error(f"Error downloading file {file_id}: {e}")
            return ""

    async def get_file_metadata(self, file_id: str) -> dict:
        """
        Get metadata for a file (name, createdTime, mimeType, etc.).

        Args:
            file_id: Google Drive file ID.

        Returns:
            File metadata dict.
        """
        try:
            file = self.service.files().get(
                fileId=file_id,
                fields="id, name, mimeType, createdTime, modifiedTime, webViewLink, size"
            ).execute()
            return file
        except Exception as e:
            logger.error(f"Error getting file metadata: {e}")
            return {}

    # =========================================================================
    # Writing Files
    # =========================================================================

    async def save_meeting_summary(
        self,
        content: str,
        filename: str
    ) -> dict:
        """
        Save a meeting summary to the Meeting Summaries folder.

        Args:
            content: The markdown content of the summary.
            filename: Name for the file (e.g., "2026-02-22 - MVP Focus.md").

        Returns:
            File metadata including the new file ID and webViewLink.
        """
        if not settings.MEETING_SUMMARIES_FOLDER_ID:
            logger.warning("MEETING_SUMMARIES_FOLDER_ID not configured")
            return {}

        return await self._upload_text_file(
            content=content,
            filename=filename,
            folder_id=settings.MEETING_SUMMARIES_FOLDER_ID,
            mime_type="text/markdown"
        )

    async def save_meeting_prep(
        self,
        content: str,
        filename: str
    ) -> dict:
        """
        Save a meeting prep document to the Meeting Prep folder.

        Args:
            content: The markdown content of the prep document.
            filename: Name for the file (e.g., "2026-02-27 - Prep - Accuracy.md").

        Returns:
            File metadata including the new file ID and webViewLink.
        """
        if not settings.MEETING_PREP_FOLDER_ID:
            logger.warning("MEETING_PREP_FOLDER_ID not configured")
            return {}

        return await self._upload_text_file(
            content=content,
            filename=filename,
            folder_id=settings.MEETING_PREP_FOLDER_ID,
            mime_type="text/markdown"
        )

    async def save_weekly_digest(
        self,
        content: str,
        filename: str
    ) -> dict:
        """
        Save a weekly digest to the Weekly Digests folder.

        Args:
            content: The markdown content of the digest.
            filename: Name for the file (e.g., "Week of 2026-02-17.md").

        Returns:
            File metadata including the new file ID and webViewLink.
        """
        if not settings.WEEKLY_DIGESTS_FOLDER_ID:
            logger.warning("WEEKLY_DIGESTS_FOLDER_ID not configured")
            return {}

        return await self._upload_text_file(
            content=content,
            filename=filename,
            folder_id=settings.WEEKLY_DIGESTS_FOLDER_ID,
            mime_type="text/markdown"
        )

    async def _upload_text_file(
        self,
        content: str,
        filename: str,
        folder_id: str,
        mime_type: str = "text/plain"
    ) -> dict:
        """
        Upload a text file to a specified folder.

        Args:
            content: Text content of the file.
            filename: Name for the file.
            folder_id: Target folder ID.
            mime_type: MIME type of the content.

        Returns:
            File metadata including the new file ID and webViewLink.
        """
        try:
            file_metadata = {
                "name": filename,
                "parents": [folder_id],
            }

            # Create media upload
            media = MediaIoBaseUpload(
                io.BytesIO(content.encode("utf-8")),
                mimetype=mime_type,
                resumable=True
            )

            file = self.service.files().create(
                body=file_metadata,
                media_body=media,
                fields="id, name, webViewLink, createdTime"
            ).execute()

            logger.info(f"Uploaded file: {filename} ({file.get('id')})")
            return file

        except Exception as e:
            logger.error(f"Error uploading file {filename}: {e}")
            return {}

    async def _upload_bytes_file(
        self,
        data: bytes,
        filename: str,
        folder_id: str,
        mime_type: str,
    ) -> dict:
        """
        Upload a binary file to a specified folder.

        Args:
            data: Raw bytes of the file.
            filename: Name for the file.
            folder_id: Target folder ID.
            mime_type: MIME type of the content.

        Returns:
            File metadata including the new file ID and webViewLink.
        """
        try:
            file_metadata = {
                "name": filename,
                "parents": [folder_id],
            }

            media = MediaIoBaseUpload(
                io.BytesIO(data),
                mimetype=mime_type,
                resumable=True,
            )

            file = self.service.files().create(
                body=file_metadata,
                media_body=media,
                fields="id, name, webViewLink, createdTime",
            ).execute()

            logger.info(f"Uploaded file: {filename} ({file.get('id')})")
            return file

        except Exception as e:
            logger.error(f"Error uploading file {filename}: {e}")
            return {}

    async def save_meeting_summary_docx(
        self,
        data: bytes,
        filename: str,
    ) -> dict:
        """
        Save a Word document meeting summary to the Meeting Summaries folder.

        Args:
            data: Raw bytes of the .docx file.
            filename: Name for the file (e.g., "2026-03-01 - Strategy.docx").

        Returns:
            File metadata including the new file ID and webViewLink.
        """
        if not settings.MEETING_SUMMARIES_FOLDER_ID:
            logger.warning("MEETING_SUMMARIES_FOLDER_ID not configured")
            return {}

        return await self._upload_bytes_file(
            data=data,
            filename=filename,
            folder_id=settings.MEETING_SUMMARIES_FOLDER_ID,
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

    async def update_file(self, file_id: str, content: str) -> dict:
        """
        Update an existing file's content.

        Args:
            file_id: Google Drive file ID.
            content: New content for the file.

        Returns:
            Updated file metadata.
        """
        try:
            media = MediaIoBaseUpload(
                io.BytesIO(content.encode("utf-8")),
                mimetype="text/plain",
                resumable=True
            )

            file = self.service.files().update(
                fileId=file_id,
                media_body=media,
                fields="id, name, webViewLink, modifiedTime"
            ).execute()

            logger.info(f"Updated file: {file_id}")
            return file

        except Exception as e:
            logger.error(f"Error updating file {file_id}: {e}")
            return {}

    # =========================================================================
    # File Management
    # =========================================================================

    async def list_files_in_folder(
        self,
        folder_id: str,
        max_results: int = 100
    ) -> list[dict]:
        """
        List all files in a folder.

        Args:
            folder_id: Google Drive folder ID.
            max_results: Maximum number of files to return.

        Returns:
            List of file metadata dicts.
        """
        try:
            query = f"'{folder_id}' in parents and trashed = false"

            results = self.service.files().list(
                q=query,
                spaces="drive",
                fields="files(id, name, mimeType, createdTime, modifiedTime, webViewLink)",
                orderBy="modifiedTime desc",
                pageSize=max_results,
            ).execute()

            return results.get("files", [])

        except Exception as e:
            logger.error(f"Error listing files in folder {folder_id}: {e}")
            return []

    async def get_file_link(self, file_id: str) -> str:
        """
        Get the web view link for a file.

        Args:
            file_id: Google Drive file ID.

        Returns:
            URL to view the file in Google Drive.
        """
        metadata = await self.get_file_metadata(file_id)
        return metadata.get("webViewLink", "")

    # =========================================================================
    # Watcher Methods
    # =========================================================================

    def mark_file_processed(self, file_id: str) -> None:
        """
        Mark a file as processed so it won't be returned again.

        Args:
            file_id: Google Drive file ID.
        """
        self._processed_file_ids.add(file_id)

    def reset_processed_files(self) -> None:
        """Clear the list of processed files (transcripts and documents)."""
        self._processed_file_ids.clear()
        self._processed_doc_ids.clear()


# Singleton instance
drive_service = GoogleDriveService()
