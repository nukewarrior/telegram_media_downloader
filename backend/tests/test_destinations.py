from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import unquote, urlsplit

import httpx

TEST_DATA = Path(tempfile.mkdtemp(prefix="destination-test-"))
os.environ["DATA_DIR"] = str(TEST_DATA)
os.environ["DOWNLOAD_ROOT"] = str(TEST_DATA / "downloads")

from fastapi.testclient import TestClient

from app import main
from app.storage import Destination, StorageError


class MockWebDAV:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.requests: list[tuple[str, str]] = []
        self.collections: set[str] = {"/dav", "/dav/archive", "/dav/archive/root"}
        self.propfind_overrides: dict[str, int] = {}
        self.propfind_sequences: dict[str, list[int]] = {}
        self.mkcol_overrides: dict[str, int] = {"/dav": 403}

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        path = unquote(urlsplit(str(request.url)).path)
        self.requests.append((request.method, path))
        if request.method == "PROPFIND":
            sequence = self.propfind_sequences.get(path)
            if sequence:
                status = sequence.pop(0)
            else:
                status = self.propfind_overrides.get(path, 207 if path in self.collections else 404)
            return httpx.Response(status, request=request)
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
        shutil.rmtree(TEST_DATA, ignore_errors=True)

    def create_local_destination(self, name: str) -> int:
        with main.connection() as db:
            cursor = db.execute(
                """INSERT INTO destinations (name, kind, local_root, enabled, is_system, created_at, updated_at)
                   VALUES (?, 'LOCAL', ?, 1, 0, ?, ?)""",
                (name, str(TEST_DATA / name), main.now(), main.now()),
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
        with (
            patch.object(Destination, "upload_file", new_callable=AsyncMock) as upload,
            patch.object(main, "get_download_client", new_callable=AsyncMock) as telegram_client,
        ):
            asyncio.run(main.download_media_job(task_id, media_id))

        upload.assert_awaited_once()
        telegram_client.assert_not_awaited()
        with main.connection() as db:
            status = db.execute("SELECT status FROM task_media WHERE id = ?", (media_id,)).fetchone()[0]
            location = db.execute("SELECT destination_id, canonical_path FROM archive_locations WHERE destination_id = ?", (destination_id,)).fetchone()
        self.assertEqual(status, "COMPLETED")
        self.assertEqual(location["destination_id"], destination_id)
        self.assertFalse(staged.exists())

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
