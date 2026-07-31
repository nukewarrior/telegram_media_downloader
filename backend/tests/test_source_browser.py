from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from telethon.client.downloads import DownloadMethods
from telethon.tl import types as telegram_types

TEST_DATA = Path(tempfile.mkdtemp(prefix="source-browser-test-"))
os.environ["DATA_DIR"] = str(TEST_DATA)
os.environ["DOWNLOAD_ROOT"] = str(TEST_DATA / "downloads")
os.environ["DEMO_MODE"] = "true"

from app import main


class SourceBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.previous_demo_mode = main.DEMO_MODE
        main.DEMO_MODE = True
        main.initialize_database()

    @classmethod
    def tearDownClass(cls) -> None:
        main.DEMO_MODE = cls.previous_demo_mode
        shutil.rmtree(TEST_DATA, ignore_errors=True)

    def setUp(self) -> None:
        main.PREVIEW_RUNNING.clear()
        main.SOURCE_THUMBNAIL_RUNNING.clear()
        with main.connection() as db:
            db.execute("DELETE FROM task_media")
            db.execute("DELETE FROM tasks")
            db.execute("DELETE FROM archive_items")
            db.execute("DELETE FROM media_blobs")
            db.execute("DELETE FROM preview_cache")
            db.execute("DELETE FROM source_media_pages")
            db.execute("DELETE FROM source_media_cache")
            db.execute("DELETE FROM source_thumbnail_cache")
            db.execute("DELETE FROM chat_cache_items")
            db.execute("UPDATE chat_cache_state SET refreshed_at = NULL, last_attempt_at = NULL, last_error = NULL WHERE id = 1")
            db.execute("UPDATE app_settings SET archive_timezone = 'Asia/Shanghai', updated_at = ? WHERE id = 1", (main.now(),))

    def test_source_media_is_cursor_paginated_and_marks_archive_state(self) -> None:
        first = asyncio.run(main.browse_source_media("demo-tech", page_size=3))
        self.assertEqual(len(first["items"]), 3)
        self.assertIsNotNone(first["next_cursor"])
        ids = [item["message_id"] for item in first["items"]]
        self.assertEqual(ids, sorted(ids, reverse=True))
        with main.connection() as db:
            blob_id = db.execute("""INSERT INTO media_blobs (content_hash, canonical_path, thumbnail_status, size_bytes, media_type, created_at)
                VALUES ('source-browser', ?, 'UNAVAILABLE', 1, 'PHOTO', ?)""", (str(Path(main.DOWNLOAD_ROOT) / "placeholder"), main.now())).lastrowid
            db.execute("""INSERT INTO archive_items (blob_id, chat_id, chat_title, message_id, filename, media_type, size_bytes, message_date, created_at)
                VALUES (?, 'demo-tech', 'Demo', ?, 'old.jpg', 'PHOTO', 1, ?, ?)""", (blob_id, ids[0], main.now(), main.now()))
        refreshed = asyncio.run(main.browse_source_media("demo-tech", page_size=3))
        self.assertTrue(refreshed["items"][0]["archived"])
        second = asyncio.run(main.browse_source_media("demo-tech", cursor=first["next_cursor"], page_size=3))
        self.assertLess(second["items"][0]["message_id"], ids[-1])

    def test_source_media_page_is_reused_without_a_second_telegram_read(self) -> None:
        first = asyncio.run(main.browse_source_media("demo-tech", page_size=3, refresh=True))
        self.assertEqual(first["cacheStatus"], "REFRESHED")
        with patch.object(main, "demo_source_media", side_effect=AssertionError("cache miss")):
            cached = asyncio.run(main.browse_source_media("demo-tech", page_size=3))
        self.assertEqual(cached["cacheStatus"], "HIT")
        self.assertEqual([item["message_id"] for item in cached["items"]], [item["message_id"] for item in first["items"]])

    def test_source_thumbnail_lru_keeps_media_index(self) -> None:
        thumbnail = main.SOURCE_THUMBNAIL_ROOT / "evict.jpg"
        thumbnail.write_bytes(b"thumbnail")
        with main.connection() as db:
            db.execute("UPDATE app_settings SET source_cache_max_bytes = 1 WHERE id = 1")
            db.execute("""INSERT INTO source_media_cache (chat_id, message_id, filename, media_type, size_bytes, message_date, last_seen_at, accessed_at)
                          VALUES ('demo-tech', 1, 'x.jpg', 'PHOTO', 1, ?, ?, ?)""", (main.now(), main.now(), main.now()))
            thumbnail_id = db.execute("""INSERT INTO source_thumbnail_cache (chat_id, message_id, cache_path, status, size_bytes, accessed_at, updated_at)
                                        VALUES ('demo-tech', 1, ?, 'READY', 9, ?, ?)""", (str(thumbnail), main.now(), main.now())).lastrowid
        main.cleanup_source_thumbnail_cache()
        with main.connection() as db:
            status = db.execute("SELECT status FROM source_thumbnail_cache WHERE id = ?", (thumbnail_id,)).fetchone()[0]
            indexed = db.execute("SELECT message_id FROM source_media_cache WHERE chat_id = 'demo-tech' AND message_id = 1").fetchone()
            db.execute("UPDATE app_settings SET source_cache_max_bytes = ? WHERE id = 1", (main.SOURCE_CACHE_DEFAULT_MAX_BYTES,))
        self.assertEqual(status, "PENDING")
        self.assertIsNotNone(indexed)
        self.assertFalse(thumbnail.exists())

    def test_source_thumbnail_is_served_only_from_its_cache_root(self) -> None:
        thumbnail = main.SOURCE_THUMBNAIL_ROOT / "served.jpg"
        thumbnail.write_bytes(b"thumbnail")
        with main.connection() as db:
            cache_id = db.execute("""INSERT INTO source_thumbnail_cache (chat_id, message_id, cache_path, status, size_bytes, accessed_at, updated_at)
                                    VALUES ('demo-tech', 2, ?, 'READY', 9, ?, ?)""", (str(thumbnail), main.now(), main.now())).lastrowid
        with TestClient(main.app) as client:
            response = client.get(f"/api/source-thumbnails/{cache_id}/content")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/jpeg")

    def test_largest_static_telegram_thumbnail_excludes_video_sizes(self) -> None:
        small = telegram_types.PhotoSize(type="m", w=160, h=90, size=1_000)
        largest = telegram_types.PhotoSizeProgressive(type="x", w=640, h=360, sizes=[1_000, 2_000])
        video = telegram_types.VideoSize(type="v", w=1280, h=720, size=4_000)
        message = SimpleNamespace(photo=SimpleNamespace(sizes=[small, video, largest]))
        selected = main.static_telegram_thumbnail(message)
        self.assertIs(selected, largest)
        self.assertIs(DownloadMethods._get_thumb([small, largest, video], selected.type), largest)

    def _source_thumbnail_record(self, message_id: int) -> int:
        placeholder = main.SOURCE_THUMBNAIL_ROOT / f"test-pending-{message_id}"
        with main.connection() as db:
            return db.execute(
                """INSERT INTO source_thumbnail_cache (chat_id, message_id, cache_path, status, quality_version, accessed_at, updated_at)
                   VALUES ('91000', ?, ?, 'DOWNLOADING', ?, ?, ?)""",
                (message_id, str(placeholder), main.SOURCE_THUMBNAIL_QUALITY_VERSION, main.now(), main.now()),
            ).lastrowid

    def test_source_thumbnail_marks_telegram_no_result_unavailable_without_retry(self) -> None:
        progressive = telegram_types.PhotoSizeProgressive(type="x", w=1280, h=720, sizes=[1_000, 2_000])
        message = SimpleNamespace(photo=SimpleNamespace(sizes=[progressive]))

        class NoThumbnailClient:
            async def get_entity(self, chat_id: int) -> int:
                return chat_id

            async def get_messages(self, _: int, *, ids: int) -> object:
                return message

            async def download_media(self, *_: object, **__: object) -> None:
                return None

        cache_id = self._source_thumbnail_record(91_001)
        previous_demo_mode = main.DEMO_MODE
        main.DEMO_MODE = False
        try:
            with (
                patch.object(main, "get_download_client", new_callable=AsyncMock, return_value=NoThumbnailClient()),
                patch.object(main, "log_event") as log_event,
            ):
                asyncio.run(main.source_thumbnail_job(cache_id))
        finally:
            main.DEMO_MODE = previous_demo_mode

        with main.connection() as db:
            record = db.execute("SELECT status, error_message FROM source_thumbnail_cache WHERE id = ?", (cache_id,)).fetchone()
        self.assertEqual(record["status"], "UNAVAILABLE")
        self.assertEqual(record["error_message"], "Telegram 未返回可用静态缩略图")
        self.assertTrue(any(call.args[1] == "source_thumbnail.unavailable" for call in log_event.call_args_list))
        self.assertFalse(any(call.args[1] == "source_thumbnail.failed" for call in log_event.call_args_list))

        main.cache_source_page("91000", "{}", None, [(91_001, "x.jpg", "PHOTO", "image/jpeg", 1, main.now())], None)
        with main.connection() as db:
            status = db.execute("SELECT status FROM source_thumbnail_cache WHERE id = ?", (cache_id,)).fetchone()[0]
        self.assertEqual(status, "UNAVAILABLE")

    def test_source_thumbnail_downloads_progressive_type_and_generates_jpeg(self) -> None:
        progressive = telegram_types.PhotoSizeProgressive(type="x", w=1280, h=720, sizes=[1_000, 2_000])
        message = SimpleNamespace(photo=SimpleNamespace(sizes=[progressive]))

        class ThumbnailClient:
            thumb: str | None = None

            async def get_entity(self, chat_id: int) -> int:
                return chat_id

            async def get_messages(self, _: int, *, ids: int) -> object:
                return message

            async def download_media(self, _: object, *, file: str, thumb: str) -> str:
                self.thumb = thumb
                main.PillowImage.new("RGB", (1280, 720), (55, 116, 161)).save(file, format="JPEG")
                return file

        client = ThumbnailClient()
        cache_id = self._source_thumbnail_record(91_002)
        previous_demo_mode = main.DEMO_MODE
        main.DEMO_MODE = False
        try:
            with patch.object(main, "get_download_client", new_callable=AsyncMock, return_value=client):
                asyncio.run(main.source_thumbnail_job(cache_id))
        finally:
            main.DEMO_MODE = previous_demo_mode

        with main.connection() as db:
            record = db.execute("SELECT status, cache_path FROM source_thumbnail_cache WHERE id = ?", (cache_id,)).fetchone()
        self.assertEqual(client.thumb, "x")
        self.assertEqual(record["status"], "READY")
        with main.PillowImage.open(record["cache_path"]) as thumbnail:
            self.assertEqual(thumbnail.format, "JPEG")

    def test_legacy_source_thumbnail_is_invalidated_for_quality_upgrade(self) -> None:
        thumbnail = main.SOURCE_THUMBNAIL_ROOT / "legacy.jpg"
        thumbnail.write_bytes(b"legacy")
        with main.connection() as db:
            cache_id = db.execute("""INSERT INTO source_thumbnail_cache (chat_id, message_id, cache_path, status, size_bytes, quality_version, quality_origin, accessed_at, updated_at)
                                    VALUES ('demo-tech', 3, ?, 'READY', 6, 1, 'TELEGRAM', ?, ?)""", (str(thumbnail), main.now(), main.now())).lastrowid
        main.reconcile_source_thumbnail_records()
        with main.connection() as db:
            record = db.execute("SELECT status, quality_version FROM source_thumbnail_cache WHERE id = ?", (cache_id,)).fetchone()
        self.assertEqual(record["status"], "PENDING")
        self.assertEqual(record["quality_version"], main.SOURCE_THUMBNAIL_QUALITY_VERSION)
        self.assertFalse(thumbnail.exists())

    def test_preview_promotion_creates_a_durable_cover_and_prevents_downgrade(self) -> None:
        preview_path = main.PREVIEW_ROOT / "promoted.png"
        main.PillowImage.new("RGB", (1200, 800), (55, 116, 161)).save(preview_path)
        with main.connection() as db:
            preview_id = db.execute("""INSERT INTO preview_cache (chat_id, message_id, filename, media_type, size_bytes, message_date, cache_path, expires_at, updated_at)
                                       VALUES ('demo-tech', 4, 'promoted.png', 'PHOTO', ?, ?, ?, ?, ?)""",
                                    (preview_path.stat().st_size, main.now(), str(preview_path), "2100-01-01T00:00:00+00:00", main.now())).lastrowid
            preview = db.execute("SELECT * FROM preview_cache WHERE id = ?", (preview_id,)).fetchone()
        asyncio.run(main.promote_preview_thumbnail(preview, preview_path))
        with main.connection() as db:
            cover = db.execute("SELECT * FROM source_thumbnail_cache WHERE chat_id = 'demo-tech' AND message_id = 4").fetchone()
        self.assertEqual(cover["quality_origin"], "PREVIEW")
        self.assertEqual(cover["status"], "READY")
        self.assertTrue(Path(cover["cache_path"]).is_file())
        telegram_path = main.SOURCE_THUMBNAIL_ROOT / "late.jpg"
        telegram_path.write_bytes(b"late")
        self.assertFalse(main.set_telegram_thumbnail_result(cover["id"], path=telegram_path, size_bytes=4))
        with main.connection() as db:
            retained = db.execute("SELECT cache_path, quality_origin FROM source_thumbnail_cache WHERE id = ?", (cover["id"],)).fetchone()
        self.assertEqual(retained["quality_origin"], "PREVIEW")
        self.assertEqual(retained["cache_path"], cover["cache_path"])

    def test_video_preview_promotion_extracts_a_durable_cover(self) -> None:
        preview_path = main.PREVIEW_ROOT / "promoted.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=#416f96:s=640x360:d=2", "-pix_fmt", "yuv420p", str(preview_path)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        with main.connection() as db:
            preview_id = db.execute("""INSERT INTO preview_cache (chat_id, message_id, filename, media_type, size_bytes, message_date, cache_path, expires_at, updated_at)
                                       VALUES ('demo-tech', 5, 'promoted.mp4', 'VIDEO', ?, ?, ?, ?, ?)""",
                                    (preview_path.stat().st_size, main.now(), str(preview_path), "2100-01-01T00:00:00+00:00", main.now())).lastrowid
            preview = db.execute("SELECT * FROM preview_cache WHERE id = ?", (preview_id,)).fetchone()
        asyncio.run(main.promote_preview_thumbnail(preview, preview_path))
        with main.connection() as db:
            cover = db.execute("SELECT * FROM source_thumbnail_cache WHERE chat_id = 'demo-tech' AND message_id = 5").fetchone()
        self.assertEqual(cover["quality_origin"], "PREVIEW")
        with main.PillowImage.open(cover["cache_path"]) as image:
            self.assertLessEqual(max(image.size), 640)

    def test_exact_selection_creates_only_selected_media_and_reuses_preview_reference(self) -> None:
        page = asyncio.run(main.browse_source_media("demo-tech", page_size=3))
        chosen = page["items"][1]
        preview = asyncio.run(main.start_source_preview("demo-tech", chosen["message_id"], main.PreviewRequest(
            filename=chosen["filename"], media_type=chosen["media_type"], mime_type=chosen["mime_type"], size_bytes=chosen["size_bytes"], message_date=chosen["message_date"],
        )))
        request = main.SelectionTaskRequest(chat_id="demo-tech", chat_title="科技前沿观察", message_ids=[chosen["message_id"]])
        with patch.object(main, "start_task_worker"):
            task = asyncio.run(main.create_selection_task(request))
        self.assertEqual(task["total_count"], 1)
        with main.connection() as db:
            media = db.execute("SELECT message_id, preview_cache_id FROM task_media WHERE task_id = ?", (task["id"],)).fetchone()
        self.assertEqual(media["message_id"], chosen["message_id"])
        self.assertEqual(media["preview_cache_id"], preview["id"])

    def test_expired_unadopted_preview_cache_is_removed(self) -> None:
        with main.connection() as db:
            cache_id = db.execute("""INSERT INTO preview_cache (chat_id, message_id, filename, media_type, size_bytes, message_date, cache_path, expires_at, updated_at)
                VALUES ('demo-tech', 9999, 'old.jpg', 'PHOTO', 1, ?, ?, ?, ?)""", (main.now(), str(main.PREVIEW_ROOT / "old.jpg"), "2000-01-01T00:00:00+00:00", main.now())).lastrowid
        (main.PREVIEW_ROOT / "old.jpg").write_bytes(b"x")
        main.cleanup_preview_cache()
        with main.connection() as db:
            self.assertIsNone(db.execute("SELECT id FROM preview_cache WHERE id = ?", (cache_id,)).fetchone())
        self.assertFalse((main.PREVIEW_ROOT / "old.jpg").exists())

    def test_authenticated_read_operations_reuse_the_shared_client(self) -> None:
        message = SimpleNamespace(id=42, date=datetime.now(UTC))

        class SharedClient:
            async def is_user_authorized(self) -> bool:
                return True

            async def get_entity(self, chat_id: int) -> int:
                return chat_id

            def iter_dialogs(self):
                async def dialogs():
                    yield SimpleNamespace(
                        id=123,
                        name="Shared source",
                        is_channel=True,
                        is_group=False,
                        entity=SimpleNamespace(username="shared_source"),
                    )
                return dialogs()

            def iter_messages(self, *_: object, **__: object):
                async def messages():
                    yield message
                return messages()

            async def get_messages(self, *_: object, **__: object) -> list[object]:
                return [message]

        shared_client = SharedClient()

        async def exercise() -> None:
            snapshot = await main.list_chats()
            self.assertEqual(snapshot["chats"][0]["id"], "123")
            scanned = await main.scan_messages(main.ScanRequest(
                chat_id="123", chat_title="Shared source", filters=main.TaskFilters(media_types=["PHOTO"]),
            ))
            self.assertEqual(scanned[0][0], 42)
            page = await main.browse_source_media("123", media_type="PHOTO")
            self.assertEqual(page["items"][0]["message_id"], 42)
            selected = await main.selected_source_messages(main.SelectionTaskRequest(
                chat_id="123", chat_title="Shared source", message_ids=[42],
            ))
            self.assertEqual(selected[0][0], 42)

        previous_demo_mode = main.DEMO_MODE
        main.DEMO_MODE = False
        try:
            with (
                patch.object(main, "app_state", return_value={"accountConnected": True}),
                patch.object(main, "get_download_client", new_callable=AsyncMock, return_value=shared_client) as get_client,
                patch.object(main, "open_telegram_client") as open_client,
                patch.object(main, "close_telegram_client") as close_client,
                patch.object(main, "matching_media", return_value=("PHOTO", 128, "image/jpeg")),
                patch.object(main, "filename_for", return_value="shared.jpg"),
            ):
                asyncio.run(exercise())
        finally:
            main.DEMO_MODE = previous_demo_mode

        self.assertEqual(get_client.await_count, 4)
        open_client.assert_not_called()
        close_client.assert_not_called()

    def test_chat_list_uses_persisted_snapshot_until_explicit_refresh(self) -> None:
        async def exercise() -> None:
            with patch.object(main, "app_state", return_value={"accountConnected": True}):
                first = await main.list_chats()
                self.assertEqual(len(first["chats"]), 3)
                with patch.object(main, "refresh_chat_cache", new_callable=AsyncMock) as refresh:
                    second = await main.list_chats()
                refresh.assert_not_awaited()
            self.assertEqual(first["refreshedAt"], second["refreshedAt"])

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
