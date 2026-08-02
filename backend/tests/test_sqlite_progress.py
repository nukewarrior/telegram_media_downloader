from __future__ import annotations

import asyncio
import os
import sqlite3
import shutil
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
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
            self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(db.execute("PRAGMA busy_timeout").fetchone()[0], main.SQLITE_BUSY_TIMEOUT_MS)
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")

    def test_business_connections_enforce_task_media_foreign_key(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            with main.connection() as db:
                db.execute(
                    """INSERT INTO task_media (task_id, message_id, filename, media_type, size_bytes, message_date)
                       VALUES (-1, 99, 'orphan.bin', 'DOCUMENT', 1, ?)""",
                    (main.now(),),
                )

    def test_progress_persists_only_after_one_second_or_completion(self) -> None:
        interval = main.DOWNLOAD_PROGRESS_MIN_INTERVAL_SECONDS
        self.assertFalse(main.should_persist_download_progress(4 * 1024 * 1024, 200 * 1024 * 1024, interval - 0.01, 0.0))
        self.assertFalse(main.should_persist_download_progress(100 * 1024 * 1024, 200 * 1024 * 1024, interval - 0.01, 0.0))
        self.assertTrue(main.should_persist_download_progress(1, 200 * 1024 * 1024, interval, 0.0))
        self.assertTrue(main.should_persist_download_progress(200 * 1024 * 1024, 200 * 1024 * 1024, 0.01, 0.0))

    def test_high_speed_progress_does_not_persist_every_four_megabytes(self) -> None:
        total_bytes = 200 * 1024 * 1024
        start_time = 100.0
        last_persisted_at = 0.0
        persisted_at: list[float] = []

        for tick in range(1, 51):
            current_time = start_time + tick * 0.04
            current_bytes = tick * 4 * 1024 * 1024
            if main.should_persist_download_progress(current_bytes, total_bytes, current_time, last_persisted_at):
                persisted_at.append(current_time)
                last_persisted_at = current_time

        self.assertLessEqual(len(persisted_at), 3)
        self.assertEqual(persisted_at[0], start_time + 0.04)
        self.assertEqual(persisted_at[-1], start_time + 2.0)
        if len(persisted_at) > 2:
            self.assertGreaterEqual(persisted_at[1] - persisted_at[0], 0.99)

    def test_concurrent_file_progress_keeps_aggregate_sum(self) -> None:
        task_id, first_media_id, second_media_id = self.create_task_with_media()
        barrier = Barrier(2)

        def update(media_id: int, downloaded_bytes: int, speed: int) -> None:
            barrier.wait(timeout=5)
            main.update_parallel_download_progress(task_id, media_id, downloaded_bytes, speed)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(update, first_media_id, 7 * 1024 * 1024, 700),
                executor.submit(update, second_media_id, 5 * 1024 * 1024, 500),
            ]
            for future in futures:
                future.result()

        with main.connection() as db:
            task = db.execute("SELECT downloaded_bytes, speed_bytes_per_second, media_revision FROM tasks WHERE id = ?", (task_id,)).fetchone()
            media = db.execute("SELECT id, downloaded_bytes, speed_bytes_per_second, revision FROM task_media WHERE task_id = ? ORDER BY id", (task_id,)).fetchall()
        self.assertEqual((task["downloaded_bytes"], task["speed_bytes_per_second"]), (12 * 1024 * 1024, 1200))
        self.assertEqual([row["downloaded_bytes"] for row in media], [7 * 1024 * 1024, 5 * 1024 * 1024])
        revisions = sorted(row["revision"] for row in media)
        self.assertEqual(revisions, [1, 2])
        self.assertEqual(task["media_revision"], max(revisions))

        changed = main.changed_task_media(task_id, 0)
        self.assertEqual({item["id"] for item in changed}, {first_media_id, second_media_id})
        self.assertEqual([item["revision"] for item in changed], revisions)
        self.assertEqual([item["id"] for item in main.changed_task_media(task_id, revisions[0])], [next(item["id"] for item in changed if item["revision"] == revisions[1])])

    def test_single_thread_revision_remains_monotonic(self) -> None:
        task_id, first_media_id, second_media_id = self.create_task_with_media()

        main.update_parallel_download_progress(task_id, first_media_id, 1, 10)
        main.update_parallel_download_progress(task_id, second_media_id, 2, 20)
        revisions = [item["revision"] for item in main.changed_task_media(task_id, 0)]

        self.assertEqual(revisions, [1, 2])

    def test_non_parallel_progress_update_assigns_a_revision_atomically(self) -> None:
        task_id, media_id, _ = self.create_task_with_media()

        main.update_download_progress(task_id, media_id, 3, 4, 5)

        with main.connection() as db:
            task = db.execute("SELECT downloaded_bytes, speed_bytes_per_second, media_revision FROM tasks WHERE id = ?", (task_id,)).fetchone()
            media = db.execute("SELECT downloaded_bytes, speed_bytes_per_second, revision FROM task_media WHERE id = ?", (media_id,)).fetchone()
        self.assertEqual((task["downloaded_bytes"], task["speed_bytes_per_second"], task["media_revision"]), (3, 5, 1))
        self.assertEqual((media["downloaded_bytes"], media["speed_bytes_per_second"], media["revision"]), (4, 5, 1))

    def test_claim_next_media_assigns_a_revision_atomically(self) -> None:
        task_id, media_id, _ = self.create_task_with_media()

        claimed = main.claim_next_media(task_id)

        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["id"], media_id)
        self.assertEqual(claimed["revision"], 1)
        with main.connection() as db:
            task = db.execute("SELECT media_revision FROM tasks WHERE id = ?", (task_id,)).fetchone()
        self.assertEqual(task["media_revision"], 1)

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
