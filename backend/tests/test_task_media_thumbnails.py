from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

TEST_DATA = Path(tempfile.mkdtemp(prefix="task-media-thumbnail-test-"))
os.environ["DATA_DIR"] = str(TEST_DATA)
os.environ["DOWNLOAD_ROOT"] = str(TEST_DATA / "downloads")

from fastapi.testclient import TestClient
from PIL import Image

from app import main


class TaskMediaThumbnailTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        main.initialize_database()

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(TEST_DATA, ignore_errors=True)

    def setUp(self) -> None:
        with main.connection() as db:
            db.execute("DELETE FROM archive_items")
            db.execute("DELETE FROM archive_locations")
            db.execute("DELETE FROM media_blobs")
            db.execute("DELETE FROM task_media")
            db.execute("DELETE FROM tasks")
        for thumbnail in main.THUMBNAIL_ROOT.glob("task-media-*.jpg"):
            thumbnail.unlink()

    def create_task_media(self) -> tuple[int, dict[str, int]]:
        timestamp = main.now()
        destination = main.destination_row(None, include_disabled=True)
        self.assertIsNotNone(destination)
        with main.connection() as db:
            task_id = db.execute(
                """INSERT INTO tasks (chat_id, chat_title, destination_id, filters_json, status, total_count,
                   completed_count, total_bytes, created_at, updated_at)
                   VALUES ('thumbnail-chat', '缩略图测试', ?, '{}', 'COMPLETED', 5, 4, 50, ?, ?)""",
                (destination["id"], timestamp, timestamp),
            ).lastrowid
            media_ids: dict[str, int] = {}
            for message_id, filename, media_type, status in [
                (101, "photo.jpg", "PHOTO", "COMPLETED"),
                (102, "video.mp4", "VIDEO", "COMPLETED"),
                (103, "document.pdf", "DOCUMENT", "COMPLETED"),
                (104, "pending.jpg", "PHOTO", "DOWNLOADING"),
                (105, "video-ready.mp4", "VIDEO", "COMPLETED"),
            ]:
                media_ids[media_type + str(message_id)] = db.execute(
                    """INSERT INTO task_media (task_id, message_id, filename, media_type, mime_type, size_bytes,
                       message_date, status, downloaded_bytes)
                       VALUES (?, ?, ?, ?, ?, 10, ?, ?, ?)""",
                    (task_id, message_id, filename, media_type, "image/jpeg" if media_type == "PHOTO" else "video/mp4" if media_type == "VIDEO" else None, timestamp, status, 10 if status == "COMPLETED" else 3),
                ).lastrowid
            for message_id, filename, media_type, thumbnail_status in [
                (101, "photo.jpg", "PHOTO", "READY"),
                (102, "video.mp4", "VIDEO", "FAILED"),
                (103, "document.pdf", "DOCUMENT", "UNAVAILABLE"),
                (105, "video-ready.mp4", "VIDEO", "READY"),
            ]:
                blob_id = db.execute(
                    """INSERT INTO media_blobs (content_hash, canonical_path, thumbnail_path, thumbnail_status,
                       size_bytes, media_type, created_at) VALUES (?, ?, ?, ?, 10, ?, ?)""",
                    (f"task-media-hash-{message_id}", str(TEST_DATA / filename), str(main.THUMBNAIL_ROOT / f"task-media-{message_id}.jpg") if thumbnail_status == "READY" else None, thumbnail_status, media_type, timestamp),
                ).lastrowid
                location_id = db.execute(
                    """INSERT INTO archive_locations (blob_id, destination_id, canonical_path, created_at)
                       VALUES (?, ?, ?, ?)""",
                    (blob_id, destination["id"], f"thumbnail/{filename}", timestamp),
                ).lastrowid
                db.execute(
                    """INSERT INTO archive_items (blob_id, location_id, chat_id, chat_title, message_id, filename,
                       media_type, mime_type, size_bytes, message_date, created_at)
                       VALUES (?, ?, 'thumbnail-chat', '缩略图测试', ?, ?, ?, ?, 10, ?, ?)""",
                    (blob_id, location_id, message_id, filename, media_type, "image/jpeg" if media_type == "PHOTO" else "video/mp4" if media_type == "VIDEO" else "application/pdf", timestamp, timestamp),
                )
        for message_id in (101, 105):
            Image.new("RGB", (8, 8), (31, 117, 147)).save(main.THUMBNAIL_ROOT / f"task-media-{message_id}.jpg", format="JPEG")
        return int(task_id), media_ids

    def test_task_media_reuses_ready_archive_thumbnail_and_falls_back_safely(self) -> None:
        task_id, _ = self.create_task_media()

        with TestClient(main.app) as client:
            response = client.get(f"/api/tasks/{task_id}/media")

        self.assertEqual(response.status_code, 200)
        items = {item["message_id"]: item for item in response.json()["items"]}
        self.assertIn("thumbnail_url", items[101])
        self.assertIn("/api/archives/media/", items[101]["thumbnail_url"])
        self.assertIn("?v=task-media-hash-101", items[101]["thumbnail_url"])
        self.assertIn("/api/archives/media/", items[101]["content_url"])
        self.assertIn("/content?v=task-media-hash-101", items[101]["content_url"])
        self.assertIn("/api/archives/media/", items[101]["download_url"])
        self.assertIn("/download?v=task-media-hash-101", items[101]["download_url"])
        self.assertIn("/api/archives/media/", items[105]["thumbnail_url"])
        self.assertIn("/content?v=task-media-hash-105", items[105]["content_url"])
        self.assertIn("/download?v=task-media-hash-105", items[105]["download_url"])
        self.assertIsNone(items[102]["thumbnail_url"])
        self.assertIsNone(items[102]["content_url"])
        self.assertIsNone(items[102]["download_url"])
        self.assertIsNone(items[103]["thumbnail_url"])
        self.assertIsNone(items[103]["content_url"])
        self.assertIsNone(items[103]["download_url"])
        self.assertIsNone(items[104]["thumbnail_url"])
        self.assertIsNone(items[104]["content_url"])
        self.assertIsNone(items[104]["download_url"])
        self.assertNotIn("archive_item_id", items[101])

        with TestClient(main.app) as client:
            thumbnail = client.get(items[101]["thumbnail_url"])
        self.assertEqual(thumbnail.status_code, 200)
        self.assertEqual(thumbnail.headers["content-type"], "image/jpeg")

    def test_changed_task_media_keeps_thumbnail_url_in_sse_payload(self) -> None:
        task_id, media_ids = self.create_task_media()
        main.update_media(task_id, media_ids["PHOTO101"], status="COMPLETED", downloaded_bytes=10)

        changed = main.changed_task_media(task_id, 0)

        photo = next(item for item in changed if item["message_id"] == 101)
        self.assertIn("/api/archives/media/", photo["thumbnail_url"])
        self.assertIn("/content?v=task-media-hash-101", photo["content_url"])
        self.assertIn("/download?v=task-media-hash-101", photo["download_url"])


if __name__ == "__main__":
    unittest.main()
