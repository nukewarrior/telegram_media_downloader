from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

TEST_DATA = Path(tempfile.mkdtemp(prefix="archive-preview-test-"))
os.environ["DATA_DIR"] = str(TEST_DATA)
os.environ["DOWNLOAD_ROOT"] = str(TEST_DATA / "downloads")

from fastapi.testclient import TestClient
from PIL import Image

from app import main


class ArchivePreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        main.initialize_database()

    @classmethod
    def tearDownClass(cls) -> None:
        import shutil
        shutil.rmtree(TEST_DATA, ignore_errors=True)

    def setUp(self) -> None:
        with main.connection() as db:
            db.execute("DELETE FROM archive_items")
            db.execute("DELETE FROM media_blobs")
        for path in main.THUMBNAIL_ROOT.glob("*.jpg"):
            path.unlink()

    def add_archive(self, media_type: str, source: Path, mime_type: str) -> tuple[int, int]:
        source.parent.mkdir(parents=True, exist_ok=True)
        size = source.stat().st_size if source.is_file() else 0
        with main.connection() as db:
            blob_id = db.execute(
                """INSERT INTO media_blobs (content_hash, canonical_path, thumbnail_status, size_bytes, media_type, created_at)
                VALUES (?, ?, 'PENDING', ?, ?, ?)""",
                (f"test-{media_type}-{source.name}", str(source), size, media_type, main.now()),
            ).lastrowid
            item_id = db.execute(
                """INSERT INTO archive_items (blob_id, chat_id, chat_title, message_id, filename, media_type, mime_type,
                size_bytes, message_date, created_at) VALUES (?, 'test', '测试聊天', ?, ?, ?, ?, ?, ?, ?)""",
                (blob_id, blob_id, source.name, media_type, mime_type, size, main.now(), main.now()),
            ).lastrowid
        return blob_id, item_id

    def generate(self, blob_id: int) -> None:
        with main.connection() as db:
            blob = db.execute("SELECT * FROM media_blobs WHERE id = ?", (blob_id,)).fetchone()
        self.assertIsNotNone(blob)
        asyncio.run(main.generate_thumbnail(blob))

    def test_photo_thumbnail_and_controlled_endpoints(self) -> None:
        source = Path(main.DOWNLOAD_ROOT) / "images" / "example.png"
        source.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1200, 800), (31, 117, 147)).save(source)
        blob_id, item_id = self.add_archive("PHOTO", source, "image/png")
        self.generate(blob_id)
        with main.connection() as db:
            blob = db.execute("SELECT thumbnail_status, thumbnail_path FROM media_blobs WHERE id = ?", (blob_id,)).fetchone()
        self.assertEqual(blob["thumbnail_status"], "READY")
        self.assertTrue(Path(blob["thumbnail_path"]).is_file())
        with TestClient(main.app) as client:
            detail = client.get(f"/api/archives/media/{item_id}")
            self.assertEqual(detail.status_code, 200)
            self.assertEqual(detail.json()["thumbnail_url"], f"/api/archives/media/{item_id}/thumbnail")
            preview = client.get(f"/api/archives/media/{item_id}/thumbnail")
            self.assertEqual(preview.status_code, 200)
            self.assertEqual(preview.headers["content-type"], "image/jpeg")
            content = client.get(f"/api/archives/media/{item_id}/content", headers={"Range": "bytes=0-7"})
            self.assertEqual(content.status_code, 206)
            self.assertEqual(len(content.content), 8)
            download = client.get(f"/api/archives/media/{item_id}/download")
            self.assertIn("attachment", download.headers["content-disposition"])

    def test_video_thumbnail(self) -> None:
        source = Path(main.DOWNLOAD_ROOT) / "videos" / "example.mp4"
        source.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=#416f96:s=640x360:d=2", "-pix_fmt", "yuv420p", str(source)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        blob_id, _ = self.add_archive("VIDEO", source, "video/mp4")
        self.generate(blob_id)
        with main.connection() as db:
            blob = db.execute("SELECT thumbnail_status, thumbnail_path FROM media_blobs WHERE id = ?", (blob_id,)).fetchone()
        self.assertEqual(blob["thumbnail_status"], "READY")
        with Image.open(blob["thumbnail_path"]) as thumbnail:
            self.assertLessEqual(max(thumbnail.size), 640)

    def test_missing_source_and_outside_path_are_not_served(self) -> None:
        source = Path(main.DOWNLOAD_ROOT) / "missing.jpg"
        blob_id, item_id = self.add_archive("PHOTO", source, "image/jpeg")
        self.generate(blob_id)
        with main.connection() as db:
            status = db.execute("SELECT thumbnail_status FROM media_blobs WHERE id = ?", (blob_id,)).fetchone()[0]
            db.execute("UPDATE media_blobs SET canonical_path = '/etc/passwd' WHERE id = ?", (blob_id,))
        self.assertEqual(status, "UNAVAILABLE")
        with TestClient(main.app) as client:
            self.assertEqual(client.get(f"/api/archives/media/{item_id}/content").status_code, 404)


if __name__ == "__main__":
    unittest.main()
