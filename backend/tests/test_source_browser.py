from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
