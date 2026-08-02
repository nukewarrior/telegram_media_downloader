from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

TEST_DATA = Path(tempfile.mkdtemp(prefix="sqlite-progress-test-"))
os.environ["DATA_DIR"] = str(TEST_DATA)
os.environ["DOWNLOAD_ROOT"] = str(TEST_DATA / "downloads")

from app import main


class SQLiteProgressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        main.initialize_database()

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(TEST_DATA, ignore_errors=True)

    def setUp(self) -> None:
        with main.connection() as db:
            db.execute("DELETE FROM task_media")
            db.execute("DELETE FROM tasks")

    def create_task_with_media(self) -> tuple[int, int, int]:
        timestamp = main.now()
        with main.connection() as db:
            task_id = db.execute(
                """INSERT INTO tasks (chat_id, chat_title, filters_json, status, total_count, total_bytes, created_at, updated_at)
                   VALUES ('sqlite-test-chat', 'SQLite 测试', '{}', 'DOWNLOADING', 2, ?, ?, ?)""",
                (20 * 1024 * 1024, timestamp, timestamp),
            ).lastrowid
            media_ids = []
            for message_id in (1, 2):
                media_ids.append(db.execute(
                    """INSERT INTO task_media (task_id, message_id, filename, media_type, size_bytes, message_date)
                       VALUES (?, ?, ?, 'DOCUMENT', 10485760, ?)""",
                    (task_id, message_id, f"file-{message_id}.bin", timestamp),
                ).lastrowid)
        return task_id, media_ids[0], media_ids[1]

    def test_connections_set_busy_timeout_and_keep_wal(self) -> None:
        with main.connection() as db:
            self.assertEqual(db.execute("PRAGMA busy_timeout").fetchone()[0], main.SQLITE_BUSY_TIMEOUT_MS)
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")

    def test_progress_persists_after_one_second_or_four_megabytes_or_completion(self) -> None:
        interval = main.DOWNLOAD_PROGRESS_MIN_INTERVAL_SECONDS
        threshold = main.DOWNLOAD_PROGRESS_MIN_BYTES
        self.assertFalse(main.should_persist_download_progress(1, 100, interval - 0.01, 0.0, 0))
        self.assertTrue(main.should_persist_download_progress(1, 100, interval, 0.0, 0))
        self.assertTrue(main.should_persist_download_progress(threshold, threshold + 1, 0.01, 0.0, 0))
        self.assertTrue(main.should_persist_download_progress(100, 100, 0.01, 0.0, 100))

    def test_concurrent_file_progress_keeps_aggregate_sum(self) -> None:
        task_id, first_media_id, second_media_id = self.create_task_with_media()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(main.update_parallel_download_progress, task_id, first_media_id, 7 * 1024 * 1024, 700),
                executor.submit(main.update_parallel_download_progress, task_id, second_media_id, 5 * 1024 * 1024, 500),
            ]
            for future in futures:
                future.result()

        with main.connection() as db:
            task = db.execute("SELECT downloaded_bytes, speed_bytes_per_second FROM tasks WHERE id = ?", (task_id,)).fetchone()
            media = db.execute("SELECT downloaded_bytes, speed_bytes_per_second FROM task_media WHERE task_id = ? ORDER BY id", (task_id,)).fetchall()
        self.assertEqual((task["downloaded_bytes"], task["speed_bytes_per_second"]), (12 * 1024 * 1024, 1200))
        self.assertEqual([row["downloaded_bytes"] for row in media], [7 * 1024 * 1024, 5 * 1024 * 1024])

    def test_restart_requeues_only_interrupted_media(self) -> None:
        task_id, first_media_id, second_media_id = self.create_task_with_media()
        with main.connection() as db:
            db.execute("UPDATE task_media SET status = 'DOWNLOADING', downloaded_bytes = 3, speed_bytes_per_second = 99 WHERE id = ?", (first_media_id,))
            db.execute("UPDATE task_media SET status = 'COMPLETED', downloaded_bytes = size_bytes WHERE id = ?", (second_media_id,))

        main.initialize_database()

        with main.connection() as db:
            task = db.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            media = db.execute("SELECT status, downloaded_bytes, speed_bytes_per_second FROM task_media WHERE task_id = ? ORDER BY id", (task_id,)).fetchall()
        self.assertEqual(task["status"], "DOWNLOADING")
        self.assertEqual((media[0]["status"], media[0]["downloaded_bytes"], media[0]["speed_bytes_per_second"]), ("PENDING", 3, 0))
        self.assertEqual((media[1]["status"], media[1]["downloaded_bytes"]), ("COMPLETED", 10 * 1024 * 1024))

    def test_pause_resume_and_retry_persist_state_transitions_immediately(self) -> None:
        task_id, first_media_id, second_media_id = self.create_task_with_media()

        with patch.object(main, "start_task_worker") as start_worker:
            paused = asyncio.run(main.pause_task(task_id))
            resumed = asyncio.run(main.resume_task(task_id))
        self.assertEqual(paused["status"], "PAUSED")
        self.assertEqual(resumed["status"], "DOWNLOADING")
        start_worker.assert_called_once_with(task_id)

        with main.connection() as db:
            db.execute("UPDATE task_media SET status = 'FAILED', downloaded_bytes = 123, speed_bytes_per_second = 77 WHERE id = ?", (first_media_id,))
            db.execute("UPDATE task_media SET status = 'RETRY_WAIT', downloaded_bytes = 456, speed_bytes_per_second = 88 WHERE id = ?", (second_media_id,))

        with patch.object(main, "start_task_worker") as start_worker:
            retried = asyncio.run(main.retry_task(task_id))

        self.assertEqual(retried["status"], "DOWNLOADING")
        start_worker.assert_called_once_with(task_id)
        with main.connection() as db:
            media = db.execute("SELECT status, downloaded_bytes, speed_bytes_per_second, attempt_count, next_retry_at FROM task_media WHERE task_id = ? ORDER BY id", (task_id,)).fetchall()
        for row in media:
            self.assertEqual((row["status"], row["downloaded_bytes"], row["speed_bytes_per_second"], row["attempt_count"], row["next_retry_at"]), ("PENDING", 0, 0, 0, None))


if __name__ == "__main__":
    unittest.main()
