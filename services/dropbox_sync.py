"""
Dropbox → Google Drive sync service — Phase 13 B1.

Mirrors files from a Dropbox folder to a Google Drive folder.
Uses hash-based change detection and conflict flagging.

Prerequisites:
- Install dropbox SDK: pip install dropbox
- Create Dropbox app at https://www.dropbox.com/developers/apps
- Obtain refresh token via OAuth flow
- Set DROPBOX_APP_KEY, DROPBOX_REFRESH_TOKEN in env

Usage:
    from services.dropbox_sync import DropboxSyncService
    sync = DropboxSyncService()
    result = await sync.sync_folder()
"""

import hashlib
import logging
from typing import Any

from config.settings import settings
from services.supabase_client import supabase_client

logger = logging.getLogger(__name__)


class DropboxSyncService:
    """Sync files from Dropbox to Google Drive with change detection."""

    def __init__(self):
        self._dbx = None

    def _get_client(self):
        """Lazy-initialize Dropbox client."""
        if self._dbx is not None:
            return self._dbx

        if not settings.DROPBOX_APP_KEY or not settings.DROPBOX_REFRESH_TOKEN:
            raise RuntimeError("Dropbox credentials not configured")

        try:
            import dropbox
            self._dbx = dropbox.Dropbox(
                oauth2_refresh_token=settings.DROPBOX_REFRESH_TOKEN,
                app_key=settings.DROPBOX_APP_KEY,
            )
            return self._dbx
        except ImportError:
            raise RuntimeError("dropbox SDK not installed — run: pip install dropbox")

    async def sync_folder(self) -> dict:
        """
        Sync configured Dropbox folder to Drive mirror folder.

        Returns:
            Dict with sync results: {synced, skipped, conflicts, errors}
        """
        folder_path = settings.DROPBOX_SYNC_FOLDER
        drive_folder_id = settings.DROPBOX_MIRROR_DRIVE_FOLDER_ID

        if not folder_path or not drive_folder_id:
            logger.warning("Dropbox sync folder or Drive mirror not configured")
            return {"synced": 0, "skipped": 0, "conflicts": 0, "errors": 0}

        try:
            dbx = self._get_client()
        except RuntimeError as e:
            logger.error(f"Dropbox client init failed: {e}")
            return {"synced": 0, "skipped": 0, "conflicts": 0, "errors": [str(e)]}

        result = {"synced": 0, "skipped": 0, "conflicts": 0, "errors": []}

        try:
            # List files in Dropbox folder
            entries = self._list_folder(dbx, folder_path)

            for entry in entries:
                try:
                    sync_result = await self._sync_file(dbx, entry, drive_folder_id)
                    if sync_result == "synced":
                        result["synced"] += 1
                    elif sync_result == "skipped":
                        result["skipped"] += 1
                    elif sync_result == "conflict":
                        result["conflicts"] += 1
                except Exception as e:
                    logger.error(f"Error syncing {entry.get('name', '?')}: {e}")
                    result["errors"].append(str(e))

        except Exception as e:
            logger.error(f"Dropbox folder listing failed: {e}")
            result["errors"].append(str(e))

        logger.info(
            f"Dropbox sync complete: {result['synced']} synced, "
            f"{result['skipped']} skipped, {result['conflicts']} conflicts"
        )
        return result

    def _list_folder(self, dbx, folder_path: str) -> list[dict]:
        """List files in a Dropbox folder."""
        import dropbox

        entries = []
        result = dbx.files_list_folder(folder_path)

        for entry in result.entries:
            if isinstance(entry, dropbox.files.FileMetadata):
                entries.append({
                    "id": entry.id,
                    "name": entry.name,
                    "path": entry.path_display,
                    "content_hash": entry.content_hash,
                    "size": entry.size,
                    "modified": entry.server_modified.isoformat() if entry.server_modified else None,
                })

        # Handle pagination
        while result.has_more:
            result = dbx.files_list_folder_continue(result.cursor)
            for entry in result.entries:
                if isinstance(entry, dropbox.files.FileMetadata):
                    entries.append({
                        "id": entry.id,
                        "name": entry.name,
                        "path": entry.path_display,
                        "content_hash": entry.content_hash,
                        "size": entry.size,
                        "modified": entry.server_modified.isoformat() if entry.server_modified else None,
                    })

        return entries

    async def _sync_file(
        self, dbx, entry: dict, drive_folder_id: str
    ) -> str:
        """
        Sync a single file from Dropbox to Drive.

        Returns: 'synced', 'skipped', or 'conflict'
        """
        dropbox_id = entry["id"]
        content_hash = entry.get("content_hash", "")

        # Check if already synced with same hash
        existing = self._get_sync_record(dropbox_id)
        if existing and existing.get("content_hash") == content_hash:
            return "skipped"

        # Download from Dropbox
        _, response = dbx.files_download(entry["path"])
        file_bytes = response.content

        # Upload to Drive
        from services.google_drive import drive_service
        import mimetypes

        mime = mimetypes.guess_type(entry["name"])[0] or "application/octet-stream"
        drive_result = await drive_service._upload_bytes_file(
            data=file_bytes,
            filename=entry["name"],
            folder_id=drive_folder_id,
            mime_type=mime,
        )
        drive_file_id = drive_result.get("id")

        # Update sync tracking
        self._upsert_sync_record(
            dropbox_file_id=dropbox_id,
            drive_file_id=drive_file_id,
            dropbox_path=entry["path"],
            drive_path=entry["name"],
            content_hash=content_hash,
        )

        logger.info(f"Synced: {entry['name']} → Drive ({drive_file_id})")
        return "synced"

    def _get_sync_record(self, dropbox_file_id: str) -> dict | None:
        """Get sync tracking record for a Dropbox file."""
        try:
            result = supabase_client.client.table("dropbox_drive_sync").select(
                "*"
            ).eq("dropbox_file_id", dropbox_file_id).limit(1).execute()
            return result.data[0] if result.data else None
        except Exception:
            return None

    def _upsert_sync_record(
        self,
        dropbox_file_id: str,
        drive_file_id: str | None,
        dropbox_path: str,
        drive_path: str | None,
        content_hash: str | None,
    ) -> None:
        """Create or update sync tracking record."""
        try:
            supabase_client.client.table("dropbox_drive_sync").upsert({
                "dropbox_file_id": dropbox_file_id,
                "drive_file_id": drive_file_id,
                "dropbox_path": dropbox_path,
                "drive_path": drive_path,
                "content_hash": content_hash,
                "sync_status": "synced",
            }, on_conflict="dropbox_file_id").execute()
        except Exception as e:
            logger.warning(f"Could not upsert sync record for {dropbox_file_id}: {e}")


# Singleton — only instantiate if credentials are configured
dropbox_sync_service = DropboxSyncService()
