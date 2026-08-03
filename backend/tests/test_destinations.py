from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import quote, unquote, urlsplit

import httpx

TEST_DATA = Path(tempfile.mkdtemp(prefix="destination-test-"))
os.environ["DATA_DIR"] = str(TEST_DATA)
os.environ["DOWNLOAD_ROOT"] = str(TEST_DATA / "downloads")

from fastapi.testclient import TestClient
from PIL import Image

from app import storage
from app import main
from app.storage import Destination, StorageError, WebDAVClientManager


class MockWebDAV:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.requests: list[tuple[str, str]] = []
        self.propfind_depths: list[tuple[str, str]] = []
        self.collections: set[str] = {"/dav", "/dav/archive", "/dav/archive/root"}
        self.propfind_overrides: dict[str, int] = {}
        self.propfind_sequences: dict[str, list[int]] = {}
        self.propfind_bodies: dict[str, bytes] = {}
        self.mkcol_overrides: dict[str, int] = {"/dav": 403}

    def _propfind_xml(self, path: str) -> bytes:
        resources = [path]
        resources.extend(
            sorted(
                candidate
                for candidate in set(self.collections).union(self.files)
                if candidate != path and candidate.rsplit("/", 1)[0] == path
            )
        )
        body = [b'<?xml version="1.0" encoding="utf-8"?>', b'<D:multistatus xmlns:D="DAV:">']
        for resource in resources:
            resource_type = b"<D:collection/>" if resource in self.collections else b""
            href = quote(resource, safe="/-._~").encode()
            body.append(
                b"<D:response><D:href>"
                + href
                + b"</D:href><D:propstat><D:prop><D:resourcetype>"
                + resource_type
                + b"</D:resourcetype></D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>"
            )
        body.append(b"</D:multistatus>")
        return b"".join(body)

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        path = unquote(urlsplit(str(request.url)).path)
        self.requests.append((request.method, path))
        if request.method == "PROPFIND":
            depth = request.headers.get("Depth", "0")
            self.propfind_depths.append((path, depth))
            sequence = self.propfind_sequences.get(path)
            if sequence:
                status = sequence.pop(0)
            else:
                status = self.propfind_overrides.get(path, 207 if path in self.collections else 404)
            if status != 207:
                return httpx.Response(status, request=request)
            content = self.propfind_bodies.get(path, self._propfind_xml(path))
            return httpx.Response(status, content=content, headers={"Content-Type": "application/xml"}, request=request)
        if request.method == "MKCOL":
            if path in self.mkcol_overrides:
                return httpx.Response(self.mkcol_overrides[path], request=request)
            if path in self.collections:
                return httpx.Response(405, request=request)
            parent = path.rsplit("/", 1)[0]
            if parent not in self.collections:
                return httpx.Response(409, request=request)
            self.collections.add(path)
            return httpx.Response(201, request=request)
        if request.method == "PUT":
            if path.rsplit("/", 1)[0] not in self.collections:
                return httpx.Response(409, request=request)
            self.files[path] = await request.aread()
            return httpx.Response(201, request=request)
        if request.method == "MOVE":
            source = path
            target = unquote(urlsplit(request.headers["Destination"]).path)
            if source not in self.files:
                return httpx.Response(404, request=request)
            if target.rsplit("/", 1)[0] not in self.collections:
                return httpx.Response(409, request=request)
            self.files[target] = self.files.pop(source)
            return httpx.Response(201, request=request)
        if request.method == "DELETE":
            if path in self.collections:
                children = {
                    candidate
                    for candidate in set(self.collections).union(self.files)
                    if candidate != path and candidate.rsplit("/", 1)[0] == path
                }
                if children:
                    return httpx.Response(409, request=request)
                self.collections.remove(path)
                return httpx.Response(204, request=request)
            if path not in self.files:
                return httpx.Response(404, request=request)
            self.files.pop(path)
            return httpx.Response(204, request=request)
        if request.method == "GET":
            if path not in self.files:
                return httpx.Response(404, request=request)
            content = self.files[path]
            range_header = request.headers.get("Range")
            if range_header:
                start_text, end_text = range_header.removeprefix("bytes=").split("-", 1)
                start = int(start_text)
                end = int(end_text) if end_text else len(content) - 1
                body = content[start:end + 1]
                return httpx.Response(206, headers={"Content-Length": str(len(body))}, content=body, request=request)
            return httpx.Response(200, headers={"Content-Length": str(len(content))}, content=content, request=request)
        return httpx.Response(405, request=request)


class DestinationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        main.initialize_database()
        with main.connection() as db:
            db.execute("UPDATE app_settings SET archive_timezone = 'Asia/Shanghai', updated_at = ? WHERE id = 1", (main.now(),))

    @classmethod
    def tearDownClass(cls) -> None:
        asyncio.run(storage.webdav_client_manager.close())
        shutil.rmtree(TEST_DATA, ignore_errors=True)

    def create_local_destination(self, name: str) -> int:
        with main.connection() as db:
            cursor = db.execute(
                """INSERT INTO destinations (name, kind, local_root, enabled, is_system, created_at, updated_at)
                   VALUES (?, 'LOCAL', ?, 1, 0, ?, ?)""",
                (name, str(TEST_DATA / name), main.now(), main.now()),
            )
            return int(cursor.lastrowid)

    def create_webdav_destination(self, name: str, *, enabled: bool = True, password: str = "stored-password") -> int:
        with main.connection() as db:
            cursor = db.execute(
                """INSERT INTO destinations (name, kind, webdav_url, webdav_username, webdav_password, remote_root, enabled, is_system, created_at, updated_at)
                   VALUES (?, 'WEBDAV', 'https://dav.example.test/dav', 'user', ?, 'archive/root', ?, 0, ?, ?)""",
                (name, password, int(enabled), main.now(), main.now()),
            )
            return int(cursor.lastrowid)

    def add_task_and_media(self, destination_id: int, task_id_hint: int) -> tuple[object, object]:
        with main.connection() as db:
            task_id = db.execute(
                """INSERT INTO tasks (chat_id, chat_title, archive_timezone, destination_id, filters_json, status, created_at, updated_at)
                   VALUES ('same-chat', '同一聊天', 'Asia/Shanghai', ?, '{}', 'DOWNLOADING', ?, ?)""",
                (destination_id, main.now(), main.now()),
            ).lastrowid
            media_id = db.execute(
                """INSERT INTO task_media (task_id, message_id, filename, media_type, mime_type, size_bytes, message_date)
                   VALUES (?, 42, 'same.bin', 'DOCUMENT', 'application/octet-stream', 4, ?)""",
                (task_id, "2026-07-30T04:00:00+00:00"),
            ).lastrowid
            task = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            media = db.execute("SELECT * FROM task_media WHERE id = ?", (media_id,)).fetchone()
        self.assertEqual(task_id, task_id_hint)
        return task, media

    def create_indexed_local_archive(
        self,
        destination_id: int,
        *,
        chat_id: str,
        chat_title: str,
        relative: str,
        content: bytes,
    ) -> tuple[Destination, int]:
        destination_record = main.destination_row(destination_id, include_disabled=True)
        self.assertIsNotNone(destination_record)
        version_id = main.current_destination_version_id(destination_id)
        with main.connection() as db:
            task_id = db.execute(
                """INSERT INTO tasks (chat_id, chat_title, archive_timezone, destination_id, destination_version_id, filters_json, status, created_at, updated_at)
                   VALUES (?, ?, 'Asia/Shanghai', ?, ?, '{}', 'DOWNLOADING', ?, ?)""",
                (chat_id, chat_title, destination_id, version_id, main.now(), main.now()),
            ).lastrowid
            media_id = db.execute(
                """INSERT INTO task_media (task_id, message_id, filename, media_type, mime_type, size_bytes, message_date)
                   VALUES (?, ?, 'cleanup.bin', 'DOCUMENT', 'application/octet-stream', ?, ?)""",
                (task_id, int(task_id), len(content), "2026-07-30T04:00:00+00:00"),
            ).lastrowid
            task = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            media = db.execute("SELECT * FROM task_media WHERE id = ?", (media_id,)).fetchone()
            version = db.execute("SELECT * FROM destination_versions WHERE id = ?", (version_id,)).fetchone()
        destination = main.destination_from_version(destination_record, version)
        path = destination.local_path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        main.record_archive(
            task,
            media,
            path,
            Path(relative),
            destination=destination,
            destination_version_id=version_id,
            content_hash=main.file_sha256(path),
        )
        with main.connection() as db:
            item = db.execute("SELECT id FROM archive_items WHERE chat_id = ? AND message_id = ? ORDER BY id DESC LIMIT 1", (chat_id, int(task_id))).fetchone()
        self.assertIsNotNone(item)
        return destination, int(item["id"])

    def test_legacy_local_destination_and_task_selection_are_persisted(self) -> None:
        system = main.destination_row()
        self.assertIsNotNone(system)
        self.assertEqual(Path(system["local_root"]), Path(main.DOWNLOAD_ROOT))
        destination_id = self.create_local_destination("second-local")
        payload = main.ScanRequest(chat_id="chat", chat_title="聊天", filters=main.TaskFilters(), destination_id=destination_id)
        with patch.object(main, "start_task_worker"):
            task = asyncio.run(main.create_task(payload))
        self.assertEqual(task["destination_id"], destination_id)
        self.assertEqual(task["destination"]["id"], destination_id)

    def test_destination_management_can_disable_and_reenable_a_destination(self) -> None:
        with TestClient(main.app) as client:
            created = client.post(
                "/api/destinations",
                json={"name": "managed-local", "kind": "LOCAL", "local_root": str(TEST_DATA / "managed-local")},
            )
            self.assertEqual(created.status_code, 200)
            destination = created.json()
            self.assertNotIn("webdav_password", destination)
            self.assertEqual(client.delete(f"/api/destinations/{destination['id']}").status_code, 200)
            listed = next(item for item in client.get("/api/destinations").json() if item["id"] == destination["id"])
            self.assertFalse(listed["enabled"])
            enabled = client.post(f"/api/destinations/{destination['id']}/enable")
            self.assertTrue(enabled.json()["enabled"])

    def test_archive_and_tasks_keep_the_destination_revision_used_at_creation(self) -> None:
        server = MockWebDAV()
        source = TEST_DATA / "versioned.jpg"
        Image.new("RGB", (800, 500), (75, 145, 98)).save(source, format="JPEG")
        source_bytes = source.read_bytes()
        server.files["/dav/archive/root/versioned.jpg"] = source_bytes
        destination_id = self.create_webdav_destination("versioned-dav")
        old_version_id = main.current_destination_version_id(destination_id)

        with patch.object(main, "start_task_worker"):
            old_task = asyncio.run(main.create_task(main.ScanRequest(
                chat_id="version-chat",
                chat_title="版本聊天",
                filters=main.TaskFilters(),
                destination_id=destination_id,
            )))

        with main.connection() as db:
            blob_id = db.execute(
                """INSERT INTO media_blobs (content_hash, canonical_path, thumbnail_status, size_bytes, media_type, created_at)
                   VALUES (?, 'versioned.jpg', 'PENDING', ?, 'PHOTO', ?)""",
                (main.file_sha256(source), len(source_bytes), main.now()),
            ).lastrowid
            location_id = db.execute(
                """INSERT INTO archive_locations (blob_id, destination_id, destination_version_id, canonical_path, created_at)
                   VALUES (?, ?, ?, 'versioned.jpg', ?)""",
                (blob_id, destination_id, old_version_id, main.now()),
            ).lastrowid
            item_id = db.execute(
                """INSERT INTO archive_items (blob_id, location_id, chat_id, chat_title, message_id, filename, media_type, mime_type, size_bytes, message_date, created_at)
                   VALUES (?, ?, 'version-chat', '版本聊天', 9001, 'versioned.jpg', 'PHOTO', 'image/jpeg', ?, ?, ?)""",
                (blob_id, location_id, len(source_bytes), main.now(), main.now()),
            ).lastrowid
            blob = db.execute("SELECT * FROM media_blobs WHERE id = ?", (blob_id,)).fetchone()

        updated = asyncio.run(main.update_destination(destination_id, main.DestinationUpdateSettings(
            name="versioned-dav",
            kind="WEBDAV",
            webdav_url="https://dav.example.test/new-dav",
            webdav_username="user-v2",
            remote_root="archive/new-root",
            enabled=True,
        )))
        self.assertEqual(updated["id"], destination_id)

        with patch.object(main, "start_task_worker"):
            new_task = asyncio.run(main.create_task(main.ScanRequest(
                chat_id="version-chat-new",
                chat_title="版本聊天新任务",
                filters=main.TaskFilters(),
                destination_id=destination_id,
            )))

        with main.connection() as db:
            old_task_row = db.execute("SELECT * FROM tasks WHERE id = ?", (old_task["id"],)).fetchone()
            new_task_row = db.execute("SELECT * FROM tasks WHERE id = ?", (new_task["id"],)).fetchone()
            revisions = db.execute("SELECT revision FROM destination_versions WHERE destination_id = ? ORDER BY revision", (destination_id,)).fetchall()
        self.assertEqual([revision["revision"] for revision in revisions], [1, 2])
        self.assertEqual(main.destination_for_task(old_task_row).remote_root, "archive/root")
        self.assertEqual(main.destination_for_task(new_task_row).remote_root, "archive/new-root")
        self.assertNotEqual(old_task_row["destination_version_id"], new_task_row["destination_version_id"])

        async def client_factory(_: Destination) -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(server), follow_redirects=True)

        with patch.object(Destination, "_client", client_factory):
            asyncio.run(main.generate_thumbnail(blob))
            with TestClient(main.app) as client:
                content = client.get(f"/api/archives/media/{item_id}/content")
                detail = client.get(f"/api/archives/media/{item_id}").json()
        self.assertEqual(content.status_code, 200)
        self.assertEqual(content.content, source_bytes)
        self.assertEqual(detail["destination"]["id"], destination_id)
        self.assertIn(("GET", "/dav/archive/root/versioned.jpg"), server.requests)
        self.assertNotIn(("GET", "/new-dav/archive/new-root/versioned.jpg"), server.requests)

    def test_webdav_destination_update_keeps_id_password_and_disabled_state(self) -> None:
        destination_id = self.create_webdav_destination("editable-dav", enabled=False)
        with main.connection() as db:
            task_id = db.execute(
                """INSERT INTO tasks (chat_id, chat_title, archive_timezone, destination_id, filters_json, status, created_at, updated_at)
                   VALUES ('editable-chat', '可编辑聊天', 'Asia/Shanghai', ?, '{}', 'PAUSED', ?, ?)""",
                (destination_id, main.now(), main.now()),
            ).lastrowid
            task = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()

        with TestClient(main.app) as client:
            response = client.put(
                f"/api/destinations/{destination_id}",
                json={
                    "name": "updated-dav",
                    "kind": "WEBDAV",
                    "webdav_url": "https://dav.example.test/updated-dav",
                    "webdav_username": "updated-user",
                    "remote_root": "archive/updated",
                    "enabled": False,
                },
            )

        self.assertEqual(response.status_code, 200)
        updated = response.json()
        self.assertEqual(updated["id"], destination_id)
        self.assertEqual(updated["name"], "updated-dav")
        self.assertEqual(updated["webdav_url"], "https://dav.example.test/updated-dav")
        self.assertFalse(updated["enabled"])
        self.assertTrue(updated["webdav_password_configured"])
        self.assertNotIn("webdav_password", updated)
        self.assertEqual(main.destination_for_task(task).webdav_url, "https://dav.example.test/updated-dav")
        with main.connection() as db:
            stored = db.execute("SELECT webdav_password, enabled FROM destinations WHERE id = ?", (destination_id,)).fetchone()
        self.assertEqual(stored["webdav_password"], "stored-password")
        self.assertFalse(stored["enabled"])

    def test_saved_webdav_candidate_test_uses_stored_password_without_persisting(self) -> None:
        destination_id = self.create_webdav_destination("candidate-dav")
        server = MockWebDAV()
        seen_passwords: list[str | None] = []

        async def client_factory(destination: Destination) -> httpx.AsyncClient:
            seen_passwords.append(destination.webdav_password)
            return httpx.AsyncClient(transport=httpx.MockTransport(server), follow_redirects=True)

        payload = {
            "name": "candidate-only",
            "kind": "WEBDAV",
            "webdav_url": "https://dav.example.test/dav",
            "webdav_username": "user",
            "remote_root": "archive/root",
            "enabled": True,
        }
        with patch.object(Destination, "_client", client_factory), TestClient(main.app) as client:
            current = client.post(f"/api/destinations/{destination_id}/test")
            candidate = client.post(f"/api/destinations/{destination_id}/test", json=payload)

        self.assertEqual(current.status_code, 200)
        self.assertEqual(candidate.status_code, 200)
        self.assertEqual(seen_passwords, ["stored-password", "stored-password"])
        self.assertTrue(any(method == "PUT" for method, _ in server.requests))
        with main.connection() as db:
            stored = db.execute("SELECT name, webdav_password FROM destinations WHERE id = ?", (destination_id,)).fetchone()
        self.assertEqual(stored["name"], "candidate-dav")
        self.assertEqual(stored["webdav_password"], "stored-password")

    def test_saved_webdav_candidate_test_failure_does_not_persist_changes(self) -> None:
        destination_id = self.create_webdav_destination("candidate-failure-dav")
        server = MockWebDAV()
        server.propfind_overrides["/dav/archive/root"] = 403

        async def client_factory(_: Destination) -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(server), follow_redirects=True)

        with patch.object(Destination, "_client", client_factory), TestClient(main.app) as client:
            response = client.post(
                f"/api/destinations/{destination_id}/test",
                json={
                    "name": "failed-candidate",
                    "kind": "WEBDAV",
                    "webdav_url": "https://dav.example.test/dav",
                    "webdav_username": "user",
                    "remote_root": "archive/root",
                    "enabled": True,
                },
            )

        self.assertEqual(response.status_code, 502)
        with main.connection() as db:
            stored = db.execute("SELECT name, webdav_password FROM destinations WHERE id = ?", (destination_id,)).fetchone()
        self.assertEqual(stored["name"], "candidate-failure-dav")
        self.assertEqual(stored["webdav_password"], "stored-password")

    def test_same_message_can_be_recorded_and_read_from_two_destinations(self) -> None:
        first_id = self.create_local_destination("destination-a")
        second_id = self.create_local_destination("destination-b")
        with main.connection() as db:
            task_a_id = db.execute(
                """INSERT INTO tasks (chat_id, chat_title, archive_timezone, destination_id, filters_json, status, created_at, updated_at)
                   VALUES ('same-chat', '同一聊天', 'Asia/Shanghai', ?, '{}', 'DOWNLOADING', ?, ?)""",
                (first_id, main.now(), main.now()),
            ).lastrowid
            task_b_id = db.execute(
                """INSERT INTO tasks (chat_id, chat_title, archive_timezone, destination_id, filters_json, status, created_at, updated_at)
                   VALUES ('same-chat', '同一聊天', 'Asia/Shanghai', ?, '{}', 'DOWNLOADING', ?, ?)""",
                (second_id, main.now(), main.now()),
            ).lastrowid
            media_a_id = db.execute(
                """INSERT INTO task_media (task_id, message_id, filename, media_type, mime_type, size_bytes, message_date)
                   VALUES (?, 42, 'same.bin', 'DOCUMENT', 'application/octet-stream', 4, ?)""",
                (task_a_id, "2026-07-30T04:00:00+00:00"),
            ).lastrowid
            media_b_id = db.execute(
                """INSERT INTO task_media (task_id, message_id, filename, media_type, mime_type, size_bytes, message_date)
                   VALUES (?, 42, 'same.bin', 'DOCUMENT', 'application/octet-stream', 4, ?)""",
                (task_b_id, "2026-07-30T04:00:00+00:00"),
            ).lastrowid
            task_a = db.execute("SELECT * FROM tasks WHERE id = ?", (task_a_id,)).fetchone()
            task_b = db.execute("SELECT * FROM tasks WHERE id = ?", (task_b_id,)).fetchone()
            media_a = db.execute("SELECT * FROM task_media WHERE id = ?", (media_a_id,)).fetchone()
            media_b = db.execute("SELECT * FROM task_media WHERE id = ?", (media_b_id,)).fetchone()

        relative = Path("同一聊天__chat-same-chat/2026/07/same__msg-42.bin")
        destination_a = Destination.from_row(main.destination_row(first_id, include_disabled=True))
        destination_b = Destination.from_row(main.destination_row(second_id, include_disabled=True))
        path_a = destination_a.local_path(relative)
        path_b = destination_b.local_path(relative)
        path_a.parent.mkdir(parents=True, exist_ok=True)
        path_b.parent.mkdir(parents=True, exist_ok=True)
        path_a.write_bytes(b"same")
        path_b.write_bytes(b"same")
        main.record_archive(task_a, media_a, path_a, relative)
        main.record_archive(task_b, media_b, path_b, relative)

        archived_a, _, _ = main.source_item_states("same-chat", [42], first_id)
        archived_b, _, _ = main.source_item_states("same-chat", [42], second_id)
        self.assertIn(42, archived_a)
        self.assertIn(42, archived_b)
        self.assertEqual(len(main.archive_media(destination_id=first_id)), 1)
        self.assertEqual(len(main.archive_media(destination_id=second_id)), 1)

        with TestClient(main.app) as client:
            first_item = client.get(f"/api/archives/media/{archived_a[42]}").json()
            second_item = client.get(f"/api/archives/media/{archived_b[42]}").json()
            self.assertIsNone(first_item["content_url"])
            self.assertEqual(first_item["destination"]["id"], first_id)
            self.assertEqual(second_item["destination"]["id"], second_id)

    def test_record_archive_uses_precomputed_hash_without_rescanning_and_keeps_fingerprint_guard(self) -> None:
        destination_id = self.create_local_destination("prehashed-local")
        destination = Destination.from_row(main.destination_row(destination_id, include_disabled=True))
        version_id = main.current_destination_version_id(destination_id)
        with main.connection() as db:
            task_id = db.execute(
                """INSERT INTO tasks (chat_id, chat_title, archive_timezone, destination_id, destination_version_id, filters_json, status, created_at, updated_at)
                   VALUES ('prehashed-chat', '预计算聊天', 'Asia/Shanghai', ?, ?, '{}', 'DOWNLOADING', ?, ?)""",
                (destination_id, version_id, main.now(), main.now()),
            ).lastrowid
            media_id = db.execute(
                """INSERT INTO task_media (task_id, message_id, filename, media_type, mime_type, size_bytes, message_date, status)
                   VALUES (?, 601, 'prehashed.bin', 'DOCUMENT', 'application/octet-stream', 11, ?, 'DOWNLOADING')""",
                (task_id, "2026-07-30T04:00:00+00:00"),
            ).lastrowid
            task = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            media = db.execute("SELECT * FROM task_media WHERE id = ?", (media_id,)).fetchone()

        relative = Path("prehashed.bin")
        path = destination.local_path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"precomputed")
        expected_hash = main.file_sha256(path)
        original_fingerprint = main.file_stat_fingerprint(path)
        path.write_bytes(b"changed-data")

        with patch.object(main, "file_sha256", side_effect=AssertionError("record_archive rescanned the file")) as hasher:
            with self.assertRaisesRegex(StorageError, "索引前发生变化"):
                main.record_archive(
                    task,
                    media,
                    path,
                    relative,
                    destination=destination,
                    destination_version_id=version_id,
                    content_hash=expected_hash,
                    content_fingerprint=original_fingerprint,
                )
            hasher.assert_not_called()

        path.write_bytes(b"precomputed")
        with patch.object(main, "file_sha256", side_effect=AssertionError("record_archive rescanned the file")) as hasher:
            main.record_archive(
                task,
                media,
                path,
                relative,
                destination=destination,
                destination_version_id=version_id,
                content_hash=expected_hash,
                content_fingerprint=main.file_stat_fingerprint(path),
            )
            hasher.assert_not_called()

        with main.connection() as db:
            blob = db.execute("SELECT content_hash FROM media_blobs WHERE content_hash = ?", (expected_hash,)).fetchone()
        self.assertIsNotNone(blob)

    def test_same_path_with_different_content_keeps_two_archive_items_and_matching_previews(self) -> None:
        destination_id = self.create_local_destination("collision-local")
        destination = Destination.from_row(main.destination_row(destination_id, include_disabled=True))
        version_id = main.current_destination_version_id(destination_id)
        with main.connection() as db:
            task_ids: list[int] = []
            media_ids: list[int] = []
            for message_id in (501, 502):
                task_id = db.execute(
                    """INSERT INTO tasks (chat_id, chat_title, archive_timezone, destination_id, destination_version_id, filters_json, status, created_at, updated_at)
                       VALUES ('collision-chat', '冲突聊天', 'Asia/Shanghai', ?, ?, '{}', 'DOWNLOADING', ?, ?)""",
                    (destination_id, version_id, main.now(), main.now()),
                ).lastrowid
                media_id = db.execute(
                    """INSERT INTO task_media (task_id, message_id, filename, media_type, mime_type, size_bytes, message_date)
                       VALUES (?, ?, 'same.jpg', 'PHOTO', 'image/jpeg', 0, ?)""",
                    (task_id, message_id, "2026-07-30T04:00:00+00:00"),
                ).lastrowid
                task_ids.append(int(task_id))
                media_ids.append(int(media_id))
            tasks = [db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone() for task_id in task_ids]
            media = [db.execute("SELECT * FROM task_media WHERE id = ?", (media_id,)).fetchone() for media_id in media_ids]

        relative = main.archive_relative_path(tasks[0], media[0])
        first_path = destination.local_path(relative)
        first_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (900, 600), (220, 45, 40)).save(first_path, format="JPEG")
        first_thumbnail, first_error = asyncio.run(main.prepare_archive_thumbnail(first_path, "PHOTO", task_id=tasks[0]["id"], media_id=media[0]["id"]))
        self.assertIsNone(first_error)
        main.record_archive(
            tasks[0], media[0], first_path, relative,
            destination=destination,
            destination_version_id=version_id,
            content_hash=main.file_sha256(first_path),
            prepared_thumbnail=first_thumbnail,
            thumbnail_attempted=True,
        )

        second_source = TEST_DATA / "collision-source.jpg"
        Image.new("RGB", (900, 600), (40, 70, 220)).save(second_source, format="JPEG")
        second_hash = main.file_sha256(second_source)
        with self.assertRaises(StorageError):
            main.record_archive(
                tasks[1], media[1], second_source, relative,
                destination=destination,
                destination_version_id=version_id,
                content_hash=second_hash,
            )
        second_relative = main.resolve_archive_relative_path(destination, relative, second_hash)
        self.assertNotEqual(second_relative, relative)
        self.assertIn(f"__hash-{second_hash[:12]}", second_relative.name)
        second_path = destination.local_path(second_relative)
        second_path.parent.mkdir(parents=True, exist_ok=True)
        second_path.write_bytes(second_source.read_bytes())
        second_thumbnail, second_error = asyncio.run(main.prepare_archive_thumbnail(second_path, "PHOTO", task_id=tasks[1]["id"], media_id=media[1]["id"]))
        self.assertIsNone(second_error)
        main.record_archive(
            tasks[1], media[1], second_path, second_relative,
            destination=destination,
            destination_version_id=version_id,
            content_hash=second_hash,
            prepared_thumbnail=second_thumbnail,
            thumbnail_attempted=True,
        )

        with main.connection() as db:
            records = db.execute(
                """SELECT a.id, l.canonical_path, b.content_hash
                   FROM archive_items a JOIN archive_locations l ON l.id = a.location_id
                   JOIN media_blobs b ON b.id = a.blob_id
                   WHERE l.destination_id = ? ORDER BY a.id""",
                (destination_id,),
            ).fetchall()
        self.assertEqual(len(records), 2)
        with TestClient(main.app) as client:
            for record, expected_path in zip(records, (first_path, second_path)):
                content = client.get(f"/api/archives/media/{record['id']}/content")
                thumbnail = client.get(f"/api/archives/media/{record['id']}/thumbnail")
                self.assertEqual(content.status_code, 200)
                self.assertEqual(thumbnail.status_code, 200)
                self.assertEqual(content.content, expected_path.read_bytes())
                with Image.open(io.BytesIO(thumbnail.content)) as opened:
                    pixel = opened.convert("RGB").getpixel((0, 0))
                if expected_path == first_path:
                    self.assertGreater(pixel[0], pixel[2])
                else:
                    self.assertGreater(pixel[2], pixel[0])

    def test_archive_delivery_guard_serializes_same_destination_base_path(self) -> None:
        async def exercise() -> list[str]:
            main.ARCHIVE_DELIVERY_LOCKS.clear()
            entered = asyncio.Event()
            release = asyncio.Event()
            order: list[str] = []

            async def first() -> None:
                async with main.archive_delivery_guard(901, Path("same/base.jpg")):
                    order.append("first-enter")
                    entered.set()
                    await release.wait()
                    order.append("first-exit")

            async def second() -> None:
                async with main.archive_delivery_guard(901, Path("same/base.jpg")):
                    order.append("second-enter")

            first_task = asyncio.create_task(first())
            await entered.wait()
            second_task = asyncio.create_task(second())
            await asyncio.sleep(0)
            self.assertEqual(order, ["first-enter"])
            release.set()
            await asyncio.gather(first_task, second_task)
            return order

        self.assertEqual(asyncio.run(exercise()), ["first-enter", "first-exit", "second-enter"])

    def test_destination_retry_reuses_a_completed_local_staging_file(self) -> None:
        with main.connection() as db:
            destination_id = db.execute(
                """INSERT INTO destinations (name, kind, webdav_url, remote_root, enabled, is_system, created_at, updated_at)
                   VALUES ('retry-dav', 'WEBDAV', 'https://dav.example.test/dav', 'archive', 1, 0, ?, ?)""",
                (main.now(), main.now()),
            ).lastrowid
            task_id = db.execute(
                """INSERT INTO tasks (chat_id, chat_title, archive_timezone, destination_id, filters_json, status, total_count, total_bytes, created_at, updated_at)
                   VALUES ('retry-chat', '重试聊天', 'Asia/Shanghai', ?, '{}', 'DOWNLOADING', 1, 4, ?, ?)""",
                (destination_id, main.now(), main.now()),
            ).lastrowid
            media_id = db.execute(
                """INSERT INTO task_media (task_id, message_id, filename, media_type, mime_type, size_bytes, message_date, status)
                   VALUES (?, 7, 'retry.bin', 'DOCUMENT', 'application/octet-stream', 4, ?, 'DOWNLOADING')""",
                (task_id, "2026-07-30T04:00:00+00:00"),
            ).lastrowid
            task = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            media = db.execute("SELECT * FROM task_media WHERE id = ?", (media_id,)).fetchone()

        destination = Destination.from_row(main.destination_row(destination_id, include_disabled=True))
        relative = main.archive_relative_path(task, media)
        staged = main.archive_stage_path(task, media, destination, relative)
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(b"data")
        expected_hash = main.file_sha256(staged)
        to_thread_calls: list[object] = []
        original_to_thread = asyncio.to_thread

        async def capture_to_thread(func: object, *args: object, **kwargs: object) -> object:
            to_thread_calls.append(func)
            return await original_to_thread(func, *args, **kwargs)  # type: ignore[arg-type]

        with (
            patch.object(main.asyncio, "to_thread", new=capture_to_thread),
            patch.object(main, "file_sha256", wraps=main.file_sha256) as hasher,
            patch.object(Destination, "upload_file", new_callable=AsyncMock) as upload,
            patch.object(main, "get_download_client", new_callable=AsyncMock) as telegram_client,
        ):
            asyncio.run(main.download_media_job(task_id, media_id))

        upload.assert_awaited_once()
        telegram_client.assert_not_awaited()
        hasher.assert_called_once_with(staged)
        self.assertIn(hasher, to_thread_calls)
        with main.connection() as db:
            status = db.execute("SELECT status FROM task_media WHERE id = ?", (media_id,)).fetchone()[0]
            location = db.execute("SELECT destination_id, canonical_path FROM archive_locations WHERE destination_id = ?", (destination_id,)).fetchone()
            blob = db.execute("SELECT content_hash FROM media_blobs WHERE content_hash = ?", (expected_hash,)).fetchone()
        self.assertEqual(status, "COMPLETED")
        self.assertEqual(location["destination_id"], destination_id)
        self.assertIsNotNone(blob)
        self.assertFalse(staged.exists())

    def test_preview_adoption_is_hashed_once_and_archived(self) -> None:
        destination_id = self.create_local_destination("preview-adopt-local")
        destination = Destination.from_row(main.destination_row(destination_id, include_disabled=True))
        version_id = main.current_destination_version_id(destination_id)
        preview_path = main.PREVIEW_ROOT / "adopted.bin"
        preview_path.write_bytes(b"preview-data")
        with main.connection() as db:
            preview_id = db.execute(
                """INSERT INTO preview_cache (chat_id, message_id, filename, media_type, mime_type, size_bytes, message_date, cache_path, status, expires_at, updated_at)
                   VALUES ('preview-adopt-chat', 602, 'adopted.bin', 'DOCUMENT', 'application/octet-stream', 12, ?, ?, 'READY', '2100-01-01T00:00:00+00:00', ?)""",
                (main.now(), str(preview_path), main.now()),
            ).lastrowid
            task_id = db.execute(
                """INSERT INTO tasks (chat_id, chat_title, archive_timezone, destination_id, destination_version_id, filters_json, status, total_count, total_bytes, created_at, updated_at)
                   VALUES ('preview-adopt-chat', '预览接管聊天', 'Asia/Shanghai', ?, ?, '{}', 'DOWNLOADING', 1, 12, ?, ?)""",
                (destination_id, version_id, main.now(), main.now()),
            ).lastrowid
            media_id = db.execute(
                """INSERT INTO task_media (task_id, message_id, filename, media_type, mime_type, size_bytes, message_date, status, preview_cache_id)
                   VALUES (?, 602, 'adopted.bin', 'DOCUMENT', 'application/octet-stream', 12, ?, 'DOWNLOADING', ?)""",
                (task_id, main.now(), preview_id),
            ).lastrowid
            task = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            media = db.execute("SELECT * FROM task_media WHERE id = ?", (media_id,)).fetchone()

        relative = main.archive_relative_path(task, media)
        staged = main.archive_stage_path(task, media, destination, relative)
        expected_hash = main.file_sha256(preview_path)
        with (
            patch.object(main, "file_sha256", wraps=main.file_sha256) as hasher,
            patch.object(main, "get_download_client", new_callable=AsyncMock) as telegram_client,
        ):
            asyncio.run(main.download_media_job(task_id, media_id))

        hasher.assert_called_once_with(staged)
        telegram_client.assert_not_awaited()
        final_path = destination.local_path(relative)
        self.assertEqual(final_path.read_bytes(), b"preview-data")
        self.assertFalse(preview_path.exists())
        self.assertFalse(staged.exists())
        with main.connection() as db:
            status = db.execute("SELECT status, preview_cache_id FROM task_media WHERE id = ?", (media_id,)).fetchone()
            preview = db.execute("SELECT status FROM preview_cache WHERE id = ?", (preview_id,)).fetchone()
            blob = db.execute("SELECT content_hash FROM media_blobs WHERE content_hash = ?", (expected_hash,)).fetchone()
        self.assertEqual(status["status"], "COMPLETED")
        self.assertIsNone(status["preview_cache_id"])
        self.assertEqual(preview["status"], "CONSUMED")
        self.assertIsNotNone(blob)

    def test_fresh_telegram_download_hashes_staging_once_before_archive(self) -> None:
        destination_id = self.create_local_destination("fresh-download-local")
        destination = Destination.from_row(main.destination_row(destination_id, include_disabled=True))
        version_id = main.current_destination_version_id(destination_id)
        with main.connection() as db:
            task_id = db.execute(
                """INSERT INTO tasks (chat_id, chat_title, archive_timezone, destination_id, destination_version_id, filters_json, status, total_count, total_bytes, created_at, updated_at)
                   VALUES ('-100603', '新下载聊天', 'Asia/Shanghai', ?, ?, '{}', 'DOWNLOADING', 1, 10, ?, ?)""",
                (destination_id, version_id, main.now(), main.now()),
            ).lastrowid
            media_id = db.execute(
                """INSERT INTO task_media (task_id, message_id, filename, media_type, mime_type, size_bytes, message_date, status)
                   VALUES (?, 603, 'fresh.bin', 'DOCUMENT', 'application/octet-stream', 10, ?, 'DOWNLOADING')""",
                (task_id, "2026-07-30T04:00:00+00:00"),
            ).lastrowid
            task = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            media = db.execute("SELECT * FROM task_media WHERE id = ?", (media_id,)).fetchone()

        class FakeTelegramClient:
            async def get_entity(self, chat_id: int) -> int:
                return chat_id

            async def get_messages(self, entity: int, ids: int) -> object:
                return object()

            async def download_media(self, message: object, file: str, progress_callback: object = None) -> str:
                Path(file).write_bytes(b"fresh-data")
                if progress_callback:
                    progress_callback(10, 10)  # type: ignore[operator]
                return file

        expected_hash = hashlib.sha256(b"fresh-data").hexdigest()
        fake_client = FakeTelegramClient()
        with (
            patch.object(main, "get_download_client", new_callable=AsyncMock, return_value=fake_client) as telegram_client,
            patch.object(main, "file_sha256", wraps=main.file_sha256) as hasher,
        ):
            asyncio.run(main.download_media_job(task_id, media_id))

        relative = main.archive_relative_path(task, media)
        final_path = destination.local_path(relative)
        self.assertEqual(final_path.read_bytes(), b"fresh-data")
        hasher.assert_called_once_with(main.archive_stage_path(task, media, destination, relative))
        telegram_client.assert_awaited_once()
        with main.connection() as db:
            status = db.execute("SELECT status FROM task_media WHERE id = ?", (media_id,)).fetchone()[0]
            blob = db.execute("SELECT content_hash FROM media_blobs WHERE content_hash = ?", (expected_hash,)).fetchone()
        self.assertEqual(status, "COMPLETED")
        self.assertIsNotNone(blob)

    def test_webdav_upload_failure_keeps_staging_file_for_retry(self) -> None:
        with main.connection() as db:
            destination_id = db.execute(
                """INSERT INTO destinations (name, kind, webdav_url, remote_root, enabled, is_system, created_at, updated_at)
                   VALUES ('failed-upload-dav', 'WEBDAV', 'https://dav.example.test/dav', 'archive', 1, 0, ?, ?)""",
                (main.now(), main.now()),
            ).lastrowid
            task_id = db.execute(
                """INSERT INTO tasks (chat_id, chat_title, archive_timezone, destination_id, filters_json, status, total_count, total_bytes, created_at, updated_at)
                   VALUES ('failed-upload-chat', '上传失败聊天', 'Asia/Shanghai', ?, '{}', 'DOWNLOADING', 1, 4, ?, ?)""",
                (destination_id, main.now(), main.now()),
            ).lastrowid
            media_id = db.execute(
                """INSERT INTO task_media (task_id, message_id, filename, media_type, mime_type, size_bytes, message_date, status)
                   VALUES (?, 10, 'failed.bin', 'DOCUMENT', 'application/octet-stream', 4, ?, 'DOWNLOADING')""",
                (task_id, "2026-07-30T04:00:00+00:00"),
            ).lastrowid
            task = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            media = db.execute("SELECT * FROM task_media WHERE id = ?", (media_id,)).fetchone()

        destination = Destination.from_row(main.destination_row(destination_id, include_disabled=True))
        relative = main.archive_relative_path(task, media)
        staged = main.archive_stage_path(task, media, destination, relative)
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(b"data")
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(main.JsonLogFormatter())
        main.LOGGER.addHandler(handler)
        with (
            patch.object(Destination, "upload_file", new_callable=AsyncMock, side_effect=StorageError("remote unavailable")) as upload,
            patch.object(main, "get_download_client", new_callable=AsyncMock) as telegram_client,
        ):
            try:
                asyncio.run(main.download_media_job(task_id, media_id))
            finally:
                main.LOGGER.removeHandler(handler)

        upload.assert_awaited_once()
        telegram_client.assert_not_awaited()
        with main.connection() as db:
            status = db.execute("SELECT status FROM task_media WHERE id = ?", (media_id,)).fetchone()[0]
        self.assertEqual(status, "RETRY_WAIT")
        self.assertTrue(staged.exists())
        events = [json.loads(line) for line in stream.getvalue().splitlines() if line]
        failed_upload = next(event for event in events if event["event"] == "webdav.upload_failed")
        self.assertEqual(failed_upload["status"], "failed")
        self.assertIsInstance(failed_upload["duration_ms"], int)
        self.assertEqual(failed_upload["directory_cache_metrics_status"], "unavailable")
        download_failed = next(event for event in events if event["event"] == "download.failed")
        self.assertEqual(download_failed["delivery_status"], "failed")
        self.assertIsInstance(download_failed["delivery_duration_ms"], int)

    def test_webdav_delivery_prepares_thumbnail_without_remote_readback(self) -> None:
        server = MockWebDAV()
        with main.connection() as db:
            destination_id = db.execute(
                """INSERT INTO destinations (name, kind, webdav_url, remote_root, enabled, is_system, created_at, updated_at)
                   VALUES ('pipeline-dav', 'WEBDAV', 'https://dav.example.test/dav', 'archive/root', 1, 0, ?, ?)""",
                (main.now(), main.now()),
            ).lastrowid
            task_id = db.execute(
                """INSERT INTO tasks (chat_id, chat_title, archive_timezone, destination_id, filters_json, status, total_count, created_at, updated_at)
                   VALUES ('pipeline-chat', '流水线聊天', 'Asia/Shanghai', ?, '{}', 'DOWNLOADING', 1, ?, ?)""",
                (destination_id, main.now(), main.now()),
            ).lastrowid
            media_id = db.execute(
                """INSERT INTO task_media (task_id, message_id, filename, media_type, mime_type, size_bytes, message_date, status)
                   VALUES (?, 8, 'photo.jpg', 'PHOTO', 'image/jpeg', 0, ?, 'DOWNLOADING')""",
                (task_id, "2026-07-30T04:00:00+00:00"),
            ).lastrowid
            task = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            media = db.execute("SELECT * FROM task_media WHERE id = ?", (media_id,)).fetchone()

        destination = Destination.from_row(main.destination_row(destination_id, include_disabled=True))
        relative = main.archive_relative_path(task, media)
        staged = main.archive_stage_path(task, media, destination, relative)
        staged.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1200, 800), (31, 117, 147)).save(staged, format="JPEG")
        with main.connection() as db:
            db.execute("UPDATE task_media SET size_bytes = ? WHERE id = ?", (staged.stat().st_size, media_id))
            media = db.execute("SELECT * FROM task_media WHERE id = ?", (media_id,)).fetchone()

        async def client_factory(_: Destination) -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(server), follow_redirects=True)

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(main.JsonLogFormatter())
        main.LOGGER.addHandler(handler)
        with (
            patch.object(Destination, "_client", client_factory),
            patch.object(Destination, "download_to_file", new_callable=AsyncMock) as readback,
        ):
            try:
                asyncio.run(main.download_media_job(task_id, media_id))
            finally:
                main.LOGGER.removeHandler(handler)
            requests_before_archive_reads = list(server.requests)
            with TestClient(main.app) as client:
                items = client.get("/api/archives/media", params={"destination_id": destination_id})
                self.assertEqual(items.status_code, 200)
                item_id = items.json()[0]["id"]
                self.assertEqual(client.get(f"/api/archives/media/{item_id}/thumbnail").status_code, 200)
                content = client.get(f"/api/archives/media/{item_id}/content", headers={"Range": "bytes=0-7"})
                self.assertEqual(content.status_code, 206)
                self.assertEqual(len(content.content), 8)
                self.assertIn("attachment", client.get(f"/api/archives/media/{item_id}/download").headers["content-disposition"])

        self.assertFalse(any(method == "GET" for method, _ in requests_before_archive_reads))
        self.assertEqual(sum(method == "PUT" for method, _ in requests_before_archive_reads), 1)
        self.assertEqual(sum(method == "MOVE" for method, _ in requests_before_archive_reads), 1)
        readback.assert_not_awaited()
        with main.connection() as db:
            status = db.execute("SELECT status FROM task_media WHERE id = ?", (media_id,)).fetchone()[0]
            blob = db.execute("SELECT thumbnail_status, thumbnail_path FROM media_blobs ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(status, "COMPLETED")
        self.assertEqual(blob["thumbnail_status"], "READY")
        self.assertTrue(Path(blob["thumbnail_path"]).is_file())
        self.assertFalse(staged.exists())
        events = [json.loads(line) for line in stream.getvalue().splitlines() if line]
        completed_upload = next(event for event in events if event["event"] == "webdav.upload_completed")
        self.assertEqual(completed_upload["status"], "success")
        self.assertIsInstance(completed_upload["duration_ms"], int)
        self.assertEqual(completed_upload["directory_cache_metrics_status"], "collected")
        self.assertGreaterEqual(completed_upload["directory_cache_misses"], 1)
        completed_download = next(event for event in events if event["event"] == "download.completed")
        self.assertEqual(completed_download["delivery_kind"], "webdav")
        self.assertEqual(completed_download["thumbnail_status"], "completed")
        self.assertIsInstance(completed_download["total_duration_ms"], int)

    def test_webdav_thumbnail_failure_does_not_fail_archive(self) -> None:
        server = MockWebDAV()
        with main.connection() as db:
            destination_id = db.execute(
                """INSERT INTO destinations (name, kind, webdav_url, remote_root, enabled, is_system, created_at, updated_at)
                   VALUES ('failed-thumbnail-dav', 'WEBDAV', 'https://dav.example.test/dav', 'archive/root', 1, 0, ?, ?)""",
                (main.now(), main.now()),
            ).lastrowid
            task_id = db.execute(
                """INSERT INTO tasks (chat_id, chat_title, archive_timezone, destination_id, filters_json, status, total_count, created_at, updated_at)
                   VALUES ('failed-thumbnail-chat', '失败缩略图聊天', 'Asia/Shanghai', ?, '{}', 'DOWNLOADING', 1, ?, ?)""",
                (destination_id, main.now(), main.now()),
            ).lastrowid
            media_id = db.execute(
                """INSERT INTO task_media (task_id, message_id, filename, media_type, mime_type, size_bytes, message_date, status)
                   VALUES (?, 9, 'broken.jpg', 'PHOTO', 'image/jpeg', 9, ?, 'DOWNLOADING')""",
                (task_id, "2026-07-30T04:00:00+00:00"),
            ).lastrowid
            task = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            media = db.execute("SELECT * FROM task_media WHERE id = ?", (media_id,)).fetchone()

        destination = Destination.from_row(main.destination_row(destination_id, include_disabled=True))
        relative = main.archive_relative_path(task, media)
        staged = main.archive_stage_path(task, media, destination, relative)
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(b"not-an-image")
        with main.connection() as db:
            db.execute("UPDATE task_media SET size_bytes = ? WHERE id = ?", (staged.stat().st_size, media_id))
            media = db.execute("SELECT * FROM task_media WHERE id = ?", (media_id,)).fetchone()

        async def client_factory(_: Destination) -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(server), follow_redirects=True)

        with (
            patch.object(Destination, "_client", client_factory),
            patch.object(Destination, "download_to_file", new_callable=AsyncMock) as readback,
        ):
            asyncio.run(main.download_media_job(task_id, media_id))

        self.assertFalse(any(method == "GET" for method, _ in server.requests))
        readback.assert_not_awaited()
        with main.connection() as db:
            status = db.execute("SELECT status FROM task_media WHERE id = ?", (media_id,)).fetchone()[0]
            blob = db.execute("SELECT thumbnail_status, thumbnail_error FROM media_blobs ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(status, "COMPLETED")
        self.assertEqual(blob["thumbnail_status"], "FAILED")
        self.assertTrue(blob["thumbnail_error"])
        self.assertFalse(staged.exists())

    def test_legacy_webdav_thumbnail_keeps_remote_readback_compatibility(self) -> None:
        server = MockWebDAV()
        source = TEST_DATA / "legacy-source.jpg"
        Image.new("RGB", (800, 600), (89, 107, 158)).save(source, format="JPEG")
        server.files["/dav/archive/root/legacy.jpg"] = source.read_bytes()
        with main.connection() as db:
            destination_id = db.execute(
                """INSERT INTO destinations (name, kind, webdav_url, remote_root, enabled, is_system, created_at, updated_at)
                   VALUES ('legacy-thumbnail-dav', 'WEBDAV', 'https://dav.example.test/dav', 'archive/root', 1, 0, ?, ?)""",
                (main.now(), main.now()),
            ).lastrowid
            blob_id = db.execute(
                """INSERT INTO media_blobs (content_hash, canonical_path, thumbnail_status, size_bytes, media_type, created_at)
                   VALUES ('legacy-thumbnail', 'legacy.jpg', 'PENDING', ?, 'PHOTO', ?)""",
                (source.stat().st_size, main.now()),
            ).lastrowid
            db.execute(
                """INSERT INTO archive_locations (blob_id, destination_id, canonical_path, created_at)
                   VALUES (?, ?, 'legacy.jpg', ?)""",
                (blob_id, destination_id, main.now()),
            )
            blob = db.execute("SELECT * FROM media_blobs WHERE id = ?", (blob_id,)).fetchone()

        async def client_factory(_: Destination) -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(server), follow_redirects=True)

        with patch.object(Destination, "_client", client_factory):
            asyncio.run(main.generate_thumbnail(blob))

        self.assertIn(("GET", "/dav/archive/root/legacy.jpg"), server.requests)
        with main.connection() as db:
            result = db.execute("SELECT thumbnail_status, thumbnail_path FROM media_blobs WHERE id = ?", (blob_id,)).fetchone()
        self.assertEqual(result["thumbnail_status"], "READY")
        self.assertTrue(Path(result["thumbnail_path"]).is_file())

    def test_webdav_upload_move_range_readback_and_connection_test(self) -> None:
        server = MockWebDAV()

        async def client_factory(_: Destination) -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(server), follow_redirects=True)

        destination = Destination(id=9, name="dav", kind="WEBDAV", webdav_url="https://dav.example.test/dav", remote_root="archive/root")
        source = TEST_DATA / "staging.bin"
        source.write_bytes(b"0123456789")
        relative = Path("nested/file.bin")
        with patch.object(Destination, "_client", client_factory):
            asyncio.run(destination.test_connection())
            asyncio.run(destination.upload_file(source, relative))
            target = TEST_DATA / "downloaded.bin"
            asyncio.run(destination.download_to_file(relative, target))

            async def read_range() -> bytes:
                client, response = await destination.open_remote_stream(relative, "bytes=2-5")
                try:
                    return b"".join([chunk async for chunk in response.aiter_bytes()])
                finally:
                    await destination.close_remote_stream(client, response)

            ranged = asyncio.run(read_range())

        self.assertEqual(server.files["/dav/archive/root/nested/file.bin"], b"0123456789")
        self.assertEqual(target.read_bytes(), b"0123456789")
        self.assertEqual(ranged, b"2345")
        self.assertNotIn(("MKCOL", "/dav"), server.requests)
        self.assertEqual(server.requests.count(("MKCOL", "/dav/archive/root/nested")), 1)
        self.assertTrue(any(method == "DELETE" for method, _ in server.requests))
        self.assertIn(("PUT", "/dav/archive/root/nested/file.bin.part"), server.requests)
        self.assertIn(("MOVE", "/dav/archive/root/nested/file.bin.part"), server.requests)

    def test_webdav_directory_cache_reuses_confirmations_for_one_version(self) -> None:
        server = MockWebDAV()
        created: list[int | None] = []

        async def factory(destination: Destination) -> httpx.AsyncClient:
            created.append(destination.version_id)
            return httpx.AsyncClient(transport=httpx.MockTransport(server), follow_redirects=True)

        manager = WebDAVClientManager(factory)
        destination = Destination(
            id=90,
            name="cached-dav",
            kind="WEBDAV",
            webdav_url="https://dav.example.test/dav",
            webdav_username="cache-user",
            webdav_password="cache-password",
            remote_root="archive/root",
            version_id=9001,
        )
        first = TEST_DATA / "directory-cache-a.bin"
        second = TEST_DATA / "directory-cache-b.bin"
        first.write_bytes(b"cache-a")
        second.write_bytes(b"cache-b")

        async def exercise() -> None:
            with patch.object(storage, "webdav_client_manager", manager):
                await destination.upload_file(first, "same-chat/2026/08/a.bin")
                first_metrics = storage.get_last_directory_cache_metrics()
                await destination.upload_file(second, "same-chat/2026/08/b.bin")
                second_metrics = storage.get_last_directory_cache_metrics()
                self.assertEqual(manager.cached_client_count, 1)
                self.assertEqual(manager.directory_cache_count, 6)
                self.assertIsNotNone(first_metrics)
                self.assertEqual((first_metrics.hits, first_metrics.misses), (0, 6))
                self.assertIsNotNone(second_metrics)
                self.assertEqual((second_metrics.hits, second_metrics.misses), (6, 0))
                await manager.close()

        asyncio.run(exercise())
        self.assertEqual(created, [9001])
        for path in (
            "/dav",
            "/dav/archive",
            "/dav/archive/root",
            "/dav/archive/root/same-chat",
            "/dav/archive/root/same-chat/2026",
            "/dav/archive/root/same-chat/2026/08",
        ):
            self.assertEqual(server.requests.count(("PROPFIND", path)), 1, path)
        for path in (
            "/dav/archive/root/same-chat",
            "/dav/archive/root/same-chat/2026",
            "/dav/archive/root/same-chat/2026/08",
        ):
            self.assertEqual(server.requests.count(("MKCOL", path)), 1, path)

    def test_webdav_directory_lock_deduplicates_concurrent_same_directory_creation(self) -> None:
        class YieldingMockWebDAV(MockWebDAV):
            async def __call__(self, request: httpx.Request) -> httpx.Response:
                await asyncio.sleep(0)
                return await super().__call__(request)

        server = YieldingMockWebDAV()

        async def factory(_: Destination) -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(server), follow_redirects=True)

        manager = WebDAVClientManager(factory)
        destination = Destination(
            id=90,
            name="locked-dav",
            kind="WEBDAV",
            webdav_url="https://dav.example.test/dav",
            remote_root="archive/root",
            version_id=9002,
        )
        first = TEST_DATA / "directory-lock-a.bin"
        second = TEST_DATA / "directory-lock-b.bin"
        first.write_bytes(b"lock-a")
        second.write_bytes(b"lock-b")

        async def exercise() -> None:
            with patch.object(storage, "webdav_client_manager", manager):
                await asyncio.gather(
                    destination.upload_file(first, "same-chat/2026/08/a.bin"),
                    destination.upload_file(second, "same-chat/2026/08/b.bin"),
                )
                await manager.close()

        asyncio.run(exercise())
        for path in (
            "/dav/archive/root/same-chat",
            "/dav/archive/root/same-chat/2026",
            "/dav/archive/root/same-chat/2026/08",
        ):
            self.assertEqual(server.requests.count(("PROPFIND", path)), 1, path)
            self.assertEqual(server.requests.count(("MKCOL", path)), 1, path)
        self.assertEqual(sum(method == "MOVE" for method, _ in server.requests), 2)

    def test_webdav_directory_cache_isolated_between_destination_versions(self) -> None:
        old_server = MockWebDAV()
        new_server = MockWebDAV()

        async def factory(destination: Destination) -> httpx.AsyncClient:
            server = old_server if destination.version_id == 9011 else new_server
            return httpx.AsyncClient(transport=httpx.MockTransport(server), follow_redirects=True)

        manager = WebDAVClientManager(factory)
        old = Destination(
            id=90,
            name="versioned-cache",
            kind="WEBDAV",
            webdav_url="https://dav.example.test/dav",
            remote_root="archive/root",
            version_id=9011,
        )
        new = Destination(
            id=90,
            name="versioned-cache",
            kind="WEBDAV",
            webdav_url="https://dav.example.test/dav",
            remote_root="archive/root",
            version_id=9012,
        )
        old_source = TEST_DATA / "old-cache-version.bin"
        new_source = TEST_DATA / "new-cache-version.bin"
        old_source.write_bytes(b"old-version")
        new_source.write_bytes(b"new-version")

        async def exercise() -> None:
            with patch.object(storage, "webdav_client_manager", manager):
                await old.upload_file(old_source, "same-chat/2026/08/old.bin")
                await new.upload_file(new_source, "same-chat/2026/08/new.bin")
                self.assertEqual(manager.cached_client_count, 2)
                self.assertEqual(len(manager.cached_directories(old)), 6)
                self.assertEqual(len(manager.cached_directories(new)), 6)
                await manager.close()

        asyncio.run(exercise())
        self.assertEqual(old_server.requests.count(("PROPFIND", "/dav/archive/root/same-chat/2026/08")), 1)
        self.assertEqual(new_server.requests.count(("PROPFIND", "/dav/archive/root/same-chat/2026/08")), 1)

    def test_webdav_directory_cache_reconfirms_after_remote_directory_loss(self) -> None:
        server = MockWebDAV()

        async def factory(_: Destination) -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(server), follow_redirects=True)

        manager = WebDAVClientManager(factory)
        destination = Destination(
            id=90,
            name="invalidated-cache",
            kind="WEBDAV",
            webdav_url="https://dav.example.test/dav",
            remote_root="archive/root",
            version_id=9013,
        )
        first = TEST_DATA / "invalidated-cache-a.bin"
        second = TEST_DATA / "invalidated-cache-b.bin"
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        month = "/dav/archive/root/same-chat/2026/08"

        async def exercise() -> None:
            with patch.object(storage, "webdav_client_manager", manager):
                await destination.upload_file(first, "same-chat/2026/08/a.bin")
                server.collections.remove(month)
                await destination.upload_file(second, "same-chat/2026/08/b.bin")
                await manager.close()

        asyncio.run(exercise())
        self.assertEqual(server.requests.count(("PROPFIND", month)), 2)
        self.assertEqual(server.requests.count(("MKCOL", month)), 2)
        self.assertEqual(server.files["/dav/archive/root/same-chat/2026/08/b.bin"], b"second")

    def test_webdav_client_manager_reuses_one_client_per_version_and_closes_streams_before_clients(self) -> None:
        server = MockWebDAV()
        created: list[tuple[int | None, str | None, str | None]] = []
        closed: list[httpx.AsyncClient] = []

        class CountingClient(httpx.AsyncClient):
            async def aclose(self) -> None:
                closed.append(self)
                await super().aclose()

        async def factory(destination: Destination) -> httpx.AsyncClient:
            created.append((destination.version_id, destination.webdav_url, destination.webdav_password))
            return CountingClient(transport=httpx.MockTransport(server), follow_redirects=True)

        manager = WebDAVClientManager(factory)
        destination = Destination(
            id=91,
            name="versioned-dav",
            kind="WEBDAV",
            webdav_url="https://dav.example.test/dav",
            webdav_username="user-v1",
            webdav_password="password-v1",
            remote_root="archive/root",
            version_id=1,
        )
        source_a = TEST_DATA / "pool-a.bin"
        source_b = TEST_DATA / "pool-b.bin"
        source_a.write_bytes(b"pool-a")
        source_b.write_bytes(b"pool-b")

        async def exercise() -> None:
            with patch.object(storage, "webdav_client_manager", manager):
                await destination.upload_file(source_a, "pool-a.bin")
                await destination.upload_file(source_b, "pool-b.bin")
                target = TEST_DATA / "pool-download.bin"
                await destination.download_to_file("pool-a.bin", target)
                client, response = await destination.open_remote_stream("pool-a.bin", "bytes=0-3")
                self.assertEqual(b"".join([chunk async for chunk in response.aiter_bytes()]), b"pool")
                self.assertEqual(manager.active_stream_count, 1)
                await destination.close_remote_stream(client, response)
                self.assertEqual(manager.active_stream_count, 0)
                self.assertEqual(len(created), 1)
                self.assertEqual(len(closed), 0)
                await manager.close()

        asyncio.run(exercise())
        self.assertEqual(created, [(1, "https://dav.example.test/dav", "password-v1")])
        self.assertEqual(len(closed), 1)

    def test_webdav_client_manager_isolates_destination_versions_and_passwords(self) -> None:
        created: list[tuple[int | None, str | None, str | None]] = []
        closed: list[httpx.AsyncClient] = []

        class CountingClient(httpx.AsyncClient):
            async def aclose(self) -> None:
                closed.append(self)
                await super().aclose()

        async def factory(destination: Destination) -> httpx.AsyncClient:
            created.append((destination.version_id, destination.webdav_url, destination.webdav_password))
            return CountingClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, request=request)))

        manager = WebDAVClientManager(factory)
        old = Destination(
            id=92,
            name="versioned-dav",
            kind="WEBDAV",
            webdav_url="https://dav.example.test/old-dav",
            webdav_username="old-user",
            webdav_password="old-password",
            remote_root="archive/old",
            version_id=11,
        )
        new = Destination(
            id=92,
            name="versioned-dav",
            kind="WEBDAV",
            webdav_url="https://dav.example.test/new-dav",
            webdav_username="new-user",
            webdav_password="new-password",
            remote_root="archive/new",
            version_id=12,
        )

        async def exercise() -> None:
            first = await manager.get_client(old)
            second = await manager.get_client(new)
            old_again = await manager.get_client(old)
            self.assertIs(first, old_again)
            self.assertIsNot(first, second)
            self.assertEqual(manager.cache_keys, (("version", 92, 11), ("version", 92, 12)))
            await manager.close()

        asyncio.run(exercise())
        self.assertEqual(
            created,
            [
                (11, "https://dav.example.test/old-dav", "old-password"),
                (12, "https://dav.example.test/new-dav", "new-password"),
            ],
        )
        self.assertEqual(len(closed), 2)

    def test_webdav_client_manager_uses_configuration_fallback_without_version_id(self) -> None:
        created: list[Destination] = []

        async def factory(destination: Destination) -> httpx.AsyncClient:
            created.append(destination)
            return httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, request=request)))

        manager = WebDAVClientManager(factory)
        first = Destination(
            id=101,
            name="test-dav-a",
            kind="WEBDAV",
            webdav_url="https://dav.example.test/dav/",
            webdav_username="same-user",
            webdav_password="same-password",
            remote_root="/archive/root/",
        )
        same_configuration = Destination(
            id=102,
            name="test-dav-b",
            kind="WEBDAV",
            webdav_url="https://dav.example.test/dav",
            webdav_username="same-user",
            webdav_password="same-password",
            remote_root="archive/root",
        )
        changed_password = Destination(
            id=102,
            name="test-dav-b",
            kind="WEBDAV",
            webdav_url="https://dav.example.test/dav",
            webdav_username="same-user",
            webdav_password="changed-password",
            remote_root="archive/root",
        )

        async def exercise() -> None:
            first_client = await manager.get_client(first)
            same_client = await manager.get_client(same_configuration)
            changed_client = await manager.get_client(changed_password)
            self.assertIs(first_client, same_client)
            self.assertIsNot(first_client, changed_client)
            self.assertEqual(len(created), 2)
            await manager.close()

        asyncio.run(exercise())

    def test_webdav_connection_test_uses_one_shot_client_without_manager_cache(self) -> None:
        created: list[httpx.AsyncClient] = []
        server = MockWebDAV()

        async def factory(_: Destination) -> httpx.AsyncClient:
            client = httpx.AsyncClient(transport=httpx.MockTransport(server), follow_redirects=True)
            created.append(client)
            return client

        destination = Destination(
            id=103,
            name="candidate-dav",
            kind="WEBDAV",
            webdav_url="https://dav.example.test/dav",
            webdav_username="candidate-user",
            webdav_password="candidate-password",
            remote_root="archive/root",
        )

        async def exercise() -> None:
            with patch.object(Destination, "_client", factory):
                await destination.test_connection()

        asyncio.run(exercise())
        self.assertEqual(len(created), 1)
        self.assertEqual(storage.webdav_client_manager.cached_client_count, 0)

    def test_lifespan_shutdown_closes_all_webdav_clients(self) -> None:
        closed: list[httpx.AsyncClient] = []

        class CountingClient(httpx.AsyncClient):
            async def aclose(self) -> None:
                closed.append(self)
                await super().aclose()

        async def factory(_: Destination) -> httpx.AsyncClient:
            return CountingClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, request=request)))

        manager = WebDAVClientManager(factory)
        destination = Destination(
            id=104,
            name="lifespan-dav",
            kind="WEBDAV",
            webdav_url="https://dav.example.test/dav",
            remote_root="archive/root",
            version_id=1041,
        )

        async def exercise() -> None:
            with patch.object(main, "webdav_client_manager", manager):
                async with main.lifespan(main.app):
                    client = await manager.get_client(destination)
                    self.assertFalse(client.is_closed)
                self.assertTrue(client.is_closed)

        asyncio.run(exercise())
        self.assertEqual(len(closed), 1)

    def test_webdav_mkcol_405_is_confirmed_with_propfind(self) -> None:
        server = MockWebDAV()
        directory = "/dav/archive/root/mkcol-405"
        server.collections.add(directory)
        server.propfind_sequences[directory] = [404, 207]
        server.mkcol_overrides[directory] = 405

        async def client_factory(_: Destination) -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(server), follow_redirects=True)

        destination = Destination(id=30, name="dav", kind="WEBDAV", webdav_url="https://dav.example.test/dav", remote_root="archive/root")
        source = TEST_DATA / "mkcol-405.bin"
        source.write_bytes(b"405-confirmed")
        with patch.object(Destination, "_client", client_factory):
            asyncio.run(destination.upload_file(source, Path("mkcol-405/file.bin")))
        self.assertEqual(server.files["/dav/archive/root/mkcol-405/file.bin"], b"405-confirmed")
        self.assertEqual(server.requests.count(("MKCOL", directory)), 1)
        self.assertEqual(server.requests.count(("PROPFIND", directory)), 2)

    def test_local_archive_delete_removes_empty_chat_history_but_keeps_other_chats(self) -> None:
        destination_id = self.create_local_destination("cleanup-local")
        destination, history_item = self.create_indexed_local_archive(
            destination_id,
            chat_id="cleanup-chat",
            chat_title="旧标题",
            relative="旧标题__chat-cleanup-chat/2025/12/history.bin",
            content=b"history",
        )
        _, current_item = self.create_indexed_local_archive(
            destination_id,
            chat_id="cleanup-chat",
            chat_title="新标题",
            relative="新标题__chat-cleanup-chat/2026/08/current.bin",
            content=b"current",
        )
        _, other_item = self.create_indexed_local_archive(
            destination_id,
            chat_id="other-chat",
            chat_title="其他聊天",
            relative="其他聊天__chat-other-chat/2026/08/other.bin",
            content=b"other",
        )
        history_root = destination.local_path("旧标题__chat-cleanup-chat")
        current_root = destination.local_path("新标题__chat-cleanup-chat")
        other_root = destination.local_path("其他聊天__chat-other-chat")

        self.assertEqual(asyncio.run(main.process_archive_delete_item(history_item))[0], "DELETED")
        self.assertFalse(history_root.exists())
        self.assertTrue(current_root.is_dir())
        self.assertTrue(other_root.is_dir())
        self.assertEqual(asyncio.run(main.process_archive_delete_item(current_item))[0], "DELETED")
        self.assertFalse(current_root.exists())
        self.assertTrue(other_root.is_dir())
        self.assertEqual(asyncio.run(main.process_archive_delete_item(other_item))[0], "DELETED")
        self.assertFalse(other_root.exists())
        self.assertTrue(destination.local_root.is_dir())

    def test_local_chat_tree_cleanup_preserves_unknown_part_and_symlink_resources(self) -> None:
        destination_id = self.create_local_destination("cleanup-blocked-local")
        destination_record = main.destination_row(destination_id, include_disabled=True)
        version_id = main.current_destination_version_id(destination_id)
        with main.connection() as db:
            version = db.execute("SELECT * FROM destination_versions WHERE id = ?", (version_id,)).fetchone()
        destination = main.destination_from_version(destination_record, version)
        root = destination.local_path("blocked-chat")
        month = root / "2026" / "08"
        month.mkdir(parents=True)
        (month / "orphan.part").write_bytes(b"partial")
        external = TEST_DATA / "cleanup-blocked-external"
        external.mkdir()
        symlink = month / "external-link"
        symlink.symlink_to(external, target_is_directory=True)
        root_symlink = destination.local_root.resolve() / "linked-chat"
        root_symlink.symlink_to(external, target_is_directory=True)
        try:
            asyncio.run(destination.cleanup_empty_chat_tree("blocked-chat"))
            asyncio.run(destination.cleanup_empty_chat_tree("linked-chat"))
            self.assertTrue(root.is_dir())
            self.assertTrue((month / "orphan.part").is_file())
            self.assertTrue(symlink.is_symlink())
            self.assertTrue(root_symlink.is_symlink())
            self.assertTrue(external.is_dir())
        finally:
            symlink.unlink(missing_ok=True)
            root_symlink.unlink(missing_ok=True)

    def test_archive_delete_success_is_not_changed_by_chat_tree_cleanup_warning(self) -> None:
        destination_id = self.create_local_destination("cleanup-warning-local")
        destination, item_id = self.create_indexed_local_archive(
            destination_id,
            chat_id="warning-chat",
            chat_title="警告聊天",
            relative="警告聊天__chat-warning-chat/2026/08/file.bin",
            content=b"delete-me",
        )
        path = destination.local_path("警告聊天__chat-warning-chat/2026/08/file.bin")
        with patch.object(Destination, "cleanup_empty_chat_tree", new=AsyncMock(side_effect=StorageError("cleanup unavailable"))):
            result = asyncio.run(main.process_archive_delete_item(item_id))
        self.assertEqual(result[0], "DELETED")
        self.assertFalse(path.exists())
        with main.connection() as db:
            self.assertIsNone(db.execute("SELECT id FROM archive_items WHERE id = ?", (item_id,)).fetchone())

    def test_shared_location_survives_first_logical_delete_and_cleans_after_last_reference(self) -> None:
        destination_id = self.create_local_destination("cleanup-shared-local")
        destination, first_item = self.create_indexed_local_archive(
            destination_id,
            chat_id="shared-chat-a",
            chat_title="共享聊天 A",
            relative="共享目录__chat-shared/2026/08/shared.bin",
            content=b"shared-content",
        )
        _, second_item = self.create_indexed_local_archive(
            destination_id,
            chat_id="shared-chat-b",
            chat_title="共享聊天 B",
            relative="共享目录__chat-shared/2026/08/shared.bin",
            content=b"shared-content",
        )
        root = destination.local_path("共享目录__chat-shared")
        shared_file = destination.local_path("共享目录__chat-shared/2026/08/shared.bin")

        self.assertEqual(asyncio.run(main.process_archive_delete_item(first_item))[0], "DELETED")
        self.assertTrue(shared_file.is_file())
        self.assertTrue(root.is_dir())
        self.assertEqual(asyncio.run(main.process_archive_delete_item(second_item))[0], "DELETED")
        self.assertFalse(shared_file.exists())
        self.assertFalse(root.exists())

    def test_webdav_cleanup_removes_only_empty_chat_tree_and_confirms_deletes(self) -> None:
        server = MockWebDAV()

        async def factory(_: Destination) -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(server), follow_redirects=True)

        manager = WebDAVClientManager(factory)
        destination = Destination(
            id=991,
            name="cleanup-dav",
            kind="WEBDAV",
            webdav_url="https://dav.example.test/dav",
            remote_root="archive/root",
        )
        source = TEST_DATA / "cleanup-webdav.bin"
        source.write_bytes(b"remote")

        async def exercise() -> None:
            with patch.object(storage, "webdav_client_manager", manager):
                await destination.upload_file(source, "same-chat/2026/08/file.bin")
                await destination.delete_file("same-chat/2026/08/file.bin")
                await destination.cleanup_empty_chat_tree("same-chat")
                await manager.close()

        asyncio.run(exercise())
        self.assertNotIn("/dav/archive/root/same-chat", server.collections)
        self.assertNotIn("/dav/archive/root/same-chat/2026", server.collections)
        self.assertNotIn("/dav/archive/root/same-chat/2026/08", server.collections)
        self.assertIn("/dav/archive/root", server.collections)
        self.assertTrue(any(path == "/dav/archive/root/same-chat" and depth == "1" for path, depth in server.propfind_depths))
        self.assertTrue(any(path == "/dav/archive/root/same-chat/2026/08" and depth == "0" for path, depth in server.propfind_depths))

    def test_webdav_cleanup_preserves_nonempty_and_rejects_ambiguous_listing(self) -> None:
        server = MockWebDAV()
        blocked = "/dav/archive/root/blocked-chat"
        server.collections.update({blocked, f"{blocked}/2026", f"{blocked}/2026/08"})
        server.files[f"{blocked}/2026/08/orphan.part"] = b"partial"

        async def factory(_: Destination) -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(server), follow_redirects=True)

        manager = WebDAVClientManager(factory)
        destination = Destination(
            id=992,
            name="cleanup-dav-blocked",
            kind="WEBDAV",
            webdav_url="https://dav.example.test/dav",
            remote_root="archive/root",
        )

        async def exercise() -> None:
            with patch.object(storage, "webdav_client_manager", manager):
                await destination.cleanup_empty_chat_tree("blocked-chat")
                self.assertIn(blocked, server.collections)
                server.propfind_overrides[blocked] = 403
                with self.assertRaisesRegex(StorageError, "权限不足"):
                    await destination.cleanup_empty_chat_tree("blocked-chat")
                server.propfind_overrides.pop(blocked)
                server.propfind_bodies[blocked] = b"<not-xml"
                with self.assertRaisesRegex(StorageError, "XML"):
                    await destination.cleanup_empty_chat_tree("blocked-chat")
                await manager.close()

        asyncio.run(exercise())

    def test_archive_chat_guard_serializes_same_physical_chat_namespace(self) -> None:
        destination = Destination(id=993, name="lock-local", kind="LOCAL", local_root=TEST_DATA / "lock-local")
        entered = asyncio.Event()
        release = asyncio.Event()
        active = 0
        maximum = 0

        async def worker() -> None:
            nonlocal active, maximum
            async with main.archive_chat_guard(destination, "same-chat"):
                active += 1
                maximum = max(maximum, active)
                entered.set()
                await release.wait()
                active -= 1

        async def exercise() -> None:
            first = asyncio.create_task(worker())
            await entered.wait()
            second = asyncio.create_task(worker())
            await asyncio.sleep(0)
            self.assertEqual(active, 1)
            release.set()
            await asyncio.gather(first, second)

        asyncio.run(exercise())
        self.assertEqual(maximum, 1)

    def test_webdav_permission_and_missing_service_errors_are_actionable(self) -> None:
        source = TEST_DATA / "permission-probe.bin"
        source.write_bytes(b"data")

        for status in (401, 403):
            server = MockWebDAV()
            server.propfind_overrides["/dav/archive/root"] = status

            async def client_factory(_: Destination, server: MockWebDAV = server) -> httpx.AsyncClient:
                return httpx.AsyncClient(transport=httpx.MockTransport(server), follow_redirects=True)

            destination = Destination(id=10 + status, name="dav", kind="WEBDAV", webdav_url="https://dav.example.test/dav", remote_root="archive/root")
            with patch.object(Destination, "_client", client_factory):
                with self.assertRaisesRegex(StorageError, "权限不足"):
                    asyncio.run(destination.upload_file(source, Path("nested/file.bin")))

        server = MockWebDAV()
        server.collections.remove("/dav")

        async def client_factory(_: Destination) -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(server), follow_redirects=True)

        destination = Destination(id=20, name="dav", kind="WEBDAV", webdav_url="https://dav.example.test/dav", remote_root="archive/root")
        with patch.object(Destination, "_client", client_factory):
            with self.assertRaisesRegex(StorageError, "服务入口不存在"):
                asyncio.run(destination.test_connection())

    def test_destination_connection_api_exposes_remote_write_permission_error(self) -> None:
        server = MockWebDAV()
        server.propfind_overrides["/dav/archive/root"] = 403

        async def client_factory(_: Destination) -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(server), follow_redirects=True)

        payload = {
            "name": "permission-dav",
            "kind": "WEBDAV",
            "webdav_url": "https://dav.example.test/dav",
            "webdav_username": "user",
            "webdav_password": "password",
            "remote_root": "archive/root",
        }
        with patch.object(Destination, "_client", client_factory), TestClient(main.app) as client:
            response = client.post("/api/destinations/test", json=payload)
        self.assertEqual(response.status_code, 502)
        self.assertIn("权限不足", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
