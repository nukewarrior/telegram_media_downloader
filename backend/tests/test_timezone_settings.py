from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

TEST_DATA = Path(tempfile.mkdtemp(prefix="timezone-settings-test-"))
os.environ["DATA_DIR"] = str(TEST_DATA)
os.environ["DOWNLOAD_ROOT"] = str(TEST_DATA / "downloads")

from fastapi.testclient import TestClient

from app import main


class TimezoneSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        main.initialize_database()

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(TEST_DATA, ignore_errors=True)

    def setUp(self) -> None:
        with main.connection() as db:
            db.execute("DELETE FROM tasks")
            db.execute("""UPDATE app_settings SET api_id = NULL, api_hash = NULL, archive_timezone = NULL,
                       account_connected = 0, connection_status = 'disconnected', updated_at = ? WHERE id = 1""", (main.now(),))

    def test_timezone_setup_validates_state_and_backfills_unfinished_tasks(self) -> None:
        with main.connection() as db:
            task_id = db.execute("""INSERT INTO tasks (chat_id, chat_title, filters_json, status, created_at, updated_at)
                VALUES ('chat', '旧任务', '{}', 'QUEUED', ?, ?)""", (main.now(), main.now())).lastrowid
            setting_columns = {row["name"] for row in db.execute("PRAGMA table_info(app_settings)").fetchall()}
            task_columns = {row["name"] for row in db.execute("PRAGMA table_info(tasks)").fetchall()}
        self.assertIn("archive_timezone", setting_columns)
        self.assertIn("archive_timezone", task_columns)

        with TestClient(main.app) as client:
            state = client.get("/api/app-state").json()
            self.assertFalse(state["configured"])
            self.assertFalse(state["apiConfigured"])
            self.assertIsNone(state["archiveTimezone"])

            configured = client.put("/api/setup", json={"api_id": "123456", "api_hash": "a" * 32}).json()
            self.assertTrue(configured["apiConfigured"])
            self.assertFalse(configured["configured"])
            self.assertIn("Asia/Shanghai", client.get("/api/timezones").json()["timezones"])
            self.assertEqual(client.put("/api/settings/archive-timezone", json={"archive_timezone": "UTC+08:00"}).status_code, 422)

            updated = client.put("/api/settings/archive-timezone", json={"archive_timezone": "Asia/Shanghai"})
            self.assertEqual(updated.status_code, 200)
            self.assertTrue(updated.json()["configured"])

        with main.connection() as db:
            self.assertEqual(db.execute("SELECT archive_timezone FROM tasks WHERE id = ?", (task_id,)).fetchone()[0], "Asia/Shanghai")

        payload = main.ScanRequest(chat_id="new-chat", chat_title="新任务", filters=main.TaskFilters())
        with patch.object(main, "start_task_worker"):
            first_task = asyncio.run(main.create_task(payload))
        main.update_archive_timezone(main.ArchiveTimezoneSettings(archive_timezone="America/Los_Angeles"))
        with patch.object(main, "start_task_worker"):
            second_task = asyncio.run(main.create_task(payload))
        with main.connection() as db:
            self.assertEqual(db.execute("SELECT archive_timezone FROM tasks WHERE id = ?", (first_task["id"],)).fetchone()[0], "Asia/Shanghai")
            self.assertEqual(db.execute("SELECT archive_timezone FROM tasks WHERE id = ?", (second_task["id"],)).fetchone()[0], "America/Los_Angeles")


if __name__ == "__main__":
    unittest.main()
