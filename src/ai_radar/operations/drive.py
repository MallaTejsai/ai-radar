"""Google Drive client using an OAuth2 refresh token.

Why OAuth2 rather than a service account: a service account has its own (empty)
storage quota, so it cannot own files in a personal Drive - the upload fails with
``storageQuotaExceeded``. A refresh token issued to the user, with the narrow
``drive.file`` scope, works unattended from GitHub Actions and can only touch
files this app created.

No Google SDK is used: the refresh exchange and the multipart upload are two
plain HTTP calls, which keeps the dependency surface small and the behaviour
testable with a mock transport.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode

from ..utils.logging import get_logger
from ..utils.net import FetchError, SafeHttpClient

logger = get_logger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
FILES_URL = "https://www.googleapis.com/drive/v3/files"


class DriveError(RuntimeError):
    pass


@dataclass(slots=True)
class DriveFile:
    id: str
    name: str
    size: int | None = None
    md5: str | None = None
    created_time: str | None = None
    app_properties: dict[str, str] | None = None


class DriveClient:
    def __init__(
        self,
        client: SafeHttpClient,
        *,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        folder_id: str = "",
    ) -> None:
        self._client = client
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._folder_id = folder_id.strip()
        self._resolved_folder: str | None = None
        self._token: str | None = None
        self._expires_at: datetime | None = None

    @property
    def configured(self) -> bool:
        return all([self._client_id, self._client_secret, self._refresh_token])

    async def access_token(self) -> str:
        if self._token and self._expires_at and datetime.now(timezone.utc) < self._expires_at:
            return self._token
        if not self.configured:
            raise DriveError("Google Drive credentials are not configured")

        body = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "refresh_token": self._refresh_token,
            "grant_type": "refresh_token",
        }
        # Google's token endpoint accepts application/x-www-form-urlencoded only.
        # Sending the same mapping as JSON is rejected with 400 invalid_request, so
        # this call deliberately bypasses the JSON helper.
        encoded = urlencode(body).encode("utf-8")
        try:
            status, raw, _ = await self._client.send_raw(
                "POST",
                TOKEN_URL,
                content=encoded,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=60.0,
                max_bytes=100_000,
            )
        except FetchError as exc:
            raise DriveError(f"token exchange failed: {exc}") from exc

        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            data = None

        if status != 200 or not isinstance(data, dict):
            detail = ""
            if isinstance(data, dict):
                detail = str(data.get("error_description") or data.get("error") or "")
            raise DriveError(f"token exchange rejected ({status}) {detail}".strip())

        token = data.get("access_token")
        if not token:
            raise DriveError("token exchange returned no access_token")
        expires_in = int(data.get("expires_in") or 3600)
        self._token = str(token)
        # Refresh a minute early so a slow upload cannot race the expiry.
        self._expires_at = datetime.now(timezone.utc) + timedelta(seconds=max(60, expires_in - 60))
        return self._token

    async def ensure_folder(
        self, name: str = "AI Radar Backups", *, create: bool = True
    ) -> str | None:
        """Return the folder to write into, optionally creating it.

        ``drive.file`` grants access to files the app created or the user opened
        with it. A folder the user created in Drive is *not* accessible through
        that scope, so uploading into an arbitrary folder id fails with 404. When
        no folder id is configured the app therefore creates its own folder,
        which it can read and write for as long as the grant exists.

        ``create=False`` makes this read-only, so a health check cannot leave a
        folder behind in the user's Drive.
        """
        if self._folder_id:
            return self._folder_id
        if self._resolved_folder:
            return self._resolved_folder

        token = await self.access_token()
        query = (
            f"name = '{name}' and mimeType = 'application/vnd.google-apps.folder' "
            "and trashed = false"
        )
        url = f"{FILES_URL}?q={quote(query)}&fields=files(id,name)&pageSize=10"
        try:
            response = await self._client.get(url, headers={"Authorization": f"Bearer {token}"})
        except FetchError as exc:
            raise DriveError(f"folder lookup failed: {exc}") from exc
        if response.status != 200:
            raise DriveError(f"folder lookup rejected ({response.status})")
        try:
            payload = json.loads(response.text or "{}")
        except json.JSONDecodeError as exc:
            raise DriveError("folder lookup returned invalid JSON") from exc
        for item in payload.get("files") or []:
            if item.get("id"):
                self._resolved_folder = str(item["id"])
                logger.info("drive_folder_reused", folder_id=self._resolved_folder)
                return self._resolved_folder

        if not create:
            return None

        try:
            created = await self._client.post_json(
                f"{FILES_URL}?fields=id,name",
                payload={"name": name, "mimeType": "application/vnd.google-apps.folder"},
                headers={"Authorization": f"Bearer {token}"},
            )
        except FetchError as exc:
            raise DriveError(f"folder creation failed: {exc}") from exc
        if created.status != 200 or not isinstance(created.data, dict) or not created.data.get("id"):
            raise DriveError(f"folder creation rejected ({created.status})")
        self._resolved_folder = str(created.data["id"])
        logger.info("drive_folder_created", folder_id=self._resolved_folder)
        return self._resolved_folder

    async def upload(
        self,
        name: str,
        data: bytes,
        *,
        mime_type: str = "application/gzip",
        app_properties: dict[str, str] | None = None,
    ) -> DriveFile:
        token = await self.access_token()
        folder = await self.ensure_folder()
        if folder is None:  # pragma: no cover - create=True cannot return None
            raise DriveError("could not resolve a backup folder")
        boundary = "ai-radar-boundary-7f3c1d"
        metadata: dict[str, object] = {
            "name": name,
            "parents": [folder],
            "mimeType": mime_type,
        }
        if app_properties:
            # The checksum has to live outside the compressed payload: it cannot
            # be embedded in bytes it is computed over. Drive appProperties keeps
            # it with the file, so a restore into a fresh database can still
            # prove integrity after the old database is gone.
            metadata["appProperties"] = app_properties
        head = (
            f"--{boundary}\r\n"
            "Content-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(metadata)}\r\n"
            f"--{boundary}\r\n"
            f"Content-Type: {mime_type}\r\n\r\n"
        ).encode("utf-8")
        tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
        body = head + data + tail

        url = (
            f"{UPLOAD_URL}?uploadType=multipart"
            "&fields=id,name,size,md5Checksum,createdTime,appProperties"
        )
        try:
            status, raw, _ = await self._client.send_raw(
                "POST",
                url,
                content=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": f"multipart/related; boundary={boundary}",
                },
                timeout=180.0,
                max_bytes=1_000_000,
            )
        except FetchError as exc:
            raise DriveError(f"upload failed: {exc}") from exc

        if status != 200:
            raise DriveError(f"upload rejected ({status}): {raw[:200]!r}")
        payload = json.loads(raw.decode("utf-8") or "{}")
        return DriveFile(
            id=str(payload.get("id")),
            name=str(payload.get("name") or name),
            size=int(payload["size"]) if str(payload.get("size", "")).isdigit() else None,
            md5=payload.get("md5Checksum"),
            created_time=payload.get("createdTime"),
            app_properties=payload.get("appProperties") or None,
        )

    async def download(self, file_id: str) -> bytes:
        """Download raw bytes. Backups are gzip, so this must never decode text."""
        token = await self.access_token()
        url = f"{FILES_URL}/{quote(file_id)}?alt=media"
        try:
            status, body, _ = await self._client.send_raw(
                "GET",
                url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=180.0,
                max_bytes=64 * 1024 * 1024,
            )
        except FetchError as exc:
            raise DriveError(f"download failed: {exc}") from exc
        if status != 200:
            raise DriveError(f"download rejected ({status})")
        return body

    async def metadata(self, file_id: str) -> DriveFile:
        token = await self.access_token()
        url = (
            f"{FILES_URL}/{quote(file_id)}"
            "?fields=id,name,size,md5Checksum,createdTime,appProperties"
        )
        try:
            response = await self._client.get(url, headers={"Authorization": f"Bearer {token}"})
        except FetchError as exc:
            raise DriveError(f"metadata request failed: {exc}") from exc
        if response.status != 200:
            raise DriveError(f"metadata request rejected ({response.status})")
        payload = json.loads(response.text or "{}")
        return DriveFile(
            id=str(payload.get("id")),
            name=str(payload.get("name")),
            size=int(payload["size"]) if str(payload.get("size", "")).isdigit() else None,
            md5=payload.get("md5Checksum"),
            created_time=payload.get("createdTime"),
            app_properties=payload.get("appProperties") or None,
        )

    async def list_backups(self, name_prefix: str = "ai-radar-backup-") -> list[DriveFile]:
        token = await self.access_token()
        folder = await self.ensure_folder(create=False)
        if folder is None:
            # Nothing has ever been uploaded; listing must not create anything.
            return []
        query = (
            f"'{folder}' in parents and trashed = false "
            f"and name contains '{name_prefix}'"
        )
        url = (
            f"{FILES_URL}?q={quote(query)}"
            "&fields=files(id,name,size,md5Checksum,createdTime,appProperties)"
            "&orderBy=createdTime desc&pageSize=100"
        )
        try:
            response = await self._client.get(url, headers={"Authorization": f"Bearer {token}"})
        except FetchError as exc:
            raise DriveError(f"list failed: {exc}") from exc
        if response.status != 200:
            raise DriveError(f"list rejected ({response.status})")
        payload = json.loads(response.text or "{}")
        return [
            DriveFile(
                id=str(item.get("id")),
                name=str(item.get("name")),
                size=int(item["size"]) if str(item.get("size", "")).isdigit() else None,
                md5=item.get("md5Checksum"),
                created_time=item.get("createdTime"),
                app_properties=item.get("appProperties") or None,
            )
            for item in payload.get("files", [])
        ]

    async def delete(self, file_id: str) -> bool:
        token = await self.access_token()
        url = f"{FILES_URL}/{quote(file_id)}"
        try:
            status, _, _ = await self._client.send_raw(
                "DELETE", url, headers={"Authorization": f"Bearer {token}"}, max_bytes=10_000
            )
        except FetchError as exc:
            raise DriveError(f"delete failed: {exc}") from exc
        return status in (200, 204)
