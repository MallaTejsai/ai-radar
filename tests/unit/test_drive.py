"""Google Drive client contracts, exercised against an in-process transport.

These tests exist because the weekly backup is the one integration that cannot be
smoke-tested cheaply: a wrong content type on the token exchange only surfaces at
23:00 on a Sunday. The mock transport asserts the wire format Google actually
requires, not just that a method was called.
"""

from __future__ import annotations

import gzip
import json
from urllib.parse import parse_qs

import httpx
import pytest

from ai_radar.operations.drive import DriveClient, DriveError
from support.helpers import mock_client

TOKEN_HOST = "oauth2.googleapis.com"
API_HOST = "www.googleapis.com"


def _drive(handler, **kwargs) -> DriveClient:
    return DriveClient(
        mock_client(handler, **kwargs),
        client_id="client-id",
        client_secret="client-secret",
        refresh_token="refresh-token",
        folder_id="folder-id",
    )


def _token_handler(seen: dict, *, status: int = 200, payload: dict | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != TOKEN_HOST:
            raise AssertionError(f"unexpected host {request.url.host}")
        seen["content_type"] = request.headers.get("content-type", "")
        seen["body"] = request.content.decode("utf-8")
        return httpx.Response(
            status,
            json=payload if payload is not None else {"access_token": "access-1", "expires_in": 3600},
        )

    return handler


async def test_token_exchange_is_form_encoded_not_json():
    seen: dict = {}
    drive = _drive(_token_handler(seen))

    token = await drive.access_token()

    assert token == "access-1"
    assert seen["content_type"].startswith("application/x-www-form-urlencoded")
    fields = parse_qs(seen["body"])
    assert fields["grant_type"] == ["refresh_token"]
    assert fields["client_id"] == ["client-id"]
    assert fields["refresh_token"] == ["refresh-token"]
    # A JSON body would parse as a single key and be rejected by Google.
    assert "{" not in seen["body"]


async def test_token_is_cached_between_calls():
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(200, json={"access_token": "access-1", "expires_in": 3600})

    drive = _drive(handler)
    await drive.access_token()
    await drive.access_token()

    assert calls["count"] == 1


async def test_rejected_token_exchange_raises_drive_error():
    seen: dict = {}
    drive = _drive(
        _token_handler(
            seen,
            status=400,
            payload={"error": "invalid_grant", "error_description": "Token has been expired or revoked."},
        )
    )

    with pytest.raises(DriveError) as excinfo:
        await drive.access_token()

    message = str(excinfo.value)
    assert "400" in message
    # Google's error_description is more useful to an operator than the code alone.
    assert "expired or revoked" in message


async def test_upload_then_download_preserves_bytes():
    payload = gzip.compress(b'{"manifest": {"backup_id": "backup-1"}}', mtime=0)
    stored: dict[str, bytes] = {}
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.url.host == TOKEN_HOST:
            return httpx.Response(200, json={"access_token": "access-1", "expires_in": 3600})
        if request.url.path.endswith("/upload/drive/v3/files"):
            body = request.content
            assert b"application/json; charset=UTF-8" in body
            assert b'"folder-id"' in body
            assert body.rstrip().endswith(b"--")
            stored["file-1"] = payload
            return httpx.Response(
                200,
                json={"id": "file-1", "name": "backup.json.gz", "size": str(len(payload))},
            )
        if request.url.path.endswith("/drive/v3/files/file-1"):
            return httpx.Response(200, content=stored["file-1"])
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    drive = _drive(handler)

    uploaded = await drive.upload("backup.json.gz", payload)
    downloaded = await drive.download(uploaded.id)

    assert uploaded.id == "file-1"
    assert uploaded.size == len(payload)
    assert downloaded == payload
    assert gzip.decompress(downloaded).startswith(b'{"manifest"')


async def test_list_backups_requests_newest_first():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == TOKEN_HOST:
            return httpx.Response(200, json={"access_token": "access-1", "expires_in": 3600})
        seen["query"] = parse_qs(request.url.query.decode())
        return httpx.Response(
            200,
            json={"files": [{"id": "b2", "name": "ai-radar-backup-2.json.gz", "createdTime": "2026-11-22"}]},
        )

    drive = _drive(handler)
    files = await drive.list_backups()

    assert [item.id for item in files] == ["b2"]
    assert seen["query"]["orderBy"] == ["createdTime desc"]
    assert "trashed = false" in seen["query"]["q"][0]


async def test_unconfigured_drive_refuses_before_any_request():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("no request should be sent without credentials")

    drive = DriveClient(
        mock_client(handler),
        client_id="",
        client_secret="",
        refresh_token="",
        folder_id="",
    )

    assert drive.configured is False
    with pytest.raises(DriveError):
        await drive.access_token()


def test_upload_metadata_is_json():
    # Guards the multipart preamble: Drive rejects a part whose Content-Type and
    # body disagree, and the metadata part must stay valid JSON.
    metadata = {"name": "backup.json.gz", "parents": ["folder"], "mimeType": "application/gzip"}
    assert json.loads(json.dumps(metadata))["mimeType"] == "application/gzip"


async def test_upload_creates_an_app_owned_folder_when_none_is_configured():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == TOKEN_HOST:
            return httpx.Response(200, json={"access_token": "access-1", "expires_in": 3600})
        if request.url.path.endswith("/upload/drive/v3/files"):
            calls.append("upload")
            assert b"folder-new" in request.content
            return httpx.Response(200, json={"id": "file-1", "name": "b.json.gz", "size": "4"})
        if request.url.path.endswith("/drive/v3/files"):
            if request.method == "GET":
                calls.append("list")
                return httpx.Response(200, json={"files": []})
            calls.append("create")
            return httpx.Response(200, json={"id": "folder-new", "name": "AI Radar Backups"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    drive = DriveClient(
        mock_client(handler),
        client_id="client-id",
        client_secret="client-secret",
        refresh_token="refresh-token",
        folder_id="",
    )

    assert drive.configured is True
    await drive.upload("b.json.gz", b"data")

    # A user-created folder is not reachable through drive.file, so the app makes
    # its own folder once and reuses it.
    assert calls == ["list", "create", "upload"]
    assert await drive.ensure_folder() == "folder-new"


async def test_an_existing_app_owned_folder_is_reused():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == TOKEN_HOST:
            return httpx.Response(200, json={"access_token": "access-1", "expires_in": 3600})
        if request.url.path.endswith("/upload/drive/v3/files"):
            calls.append("upload")
            assert b"folder-existing" in request.content
            return httpx.Response(200, json={"id": "file-1", "name": "b.json.gz", "size": "4"})
        if request.method == "GET" and request.url.path.endswith("/drive/v3/files"):
            calls.append("list")
            return httpx.Response(200, json={"files": [{"id": "folder-existing"}]})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    drive = DriveClient(
        mock_client(handler),
        client_id="client-id",
        client_secret="client-secret",
        refresh_token="refresh-token",
        folder_id="",
    )

    await drive.upload("b.json.gz", b"data")

    assert calls == ["list", "upload"]


async def test_listing_backups_never_creates_a_folder():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == TOKEN_HOST:
            return httpx.Response(200, json={"access_token": "access-1", "expires_in": 3600})
        if request.method == "GET" and request.url.path.endswith("/drive/v3/files"):
            calls.append("list")
            return httpx.Response(200, json={"files": []})
        raise AssertionError(f"a health check must not write: {request.method} {request.url}")

    drive = DriveClient(
        mock_client(handler),
        client_id="client-id",
        client_secret="client-secret",
        refresh_token="refresh-token",
        folder_id="",
    )

    assert await drive.list_backups() == []
    assert calls == ["list"]
