from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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
        with main.connection() as db:
            db.execute("DELETE FROM task_media")
            db.execute("DELETE FROM tasks")
            db.execute("DELETE FROM archive_items")
            db.execute("DELETE FROM media_blobs")
            db.execute("DELETE FROM preview_cache")
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
            chats = await main.list_chats()
            self.assertEqual(chats[0]["id"], "123")
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


if __name__ == "__main__":
    unittest.main()
