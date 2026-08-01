from __future__ import annotations

import asyncio
import gzip
import io
import json
import logging
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import Response
TEST_DATA = Path(tempfile.mkdtemp(prefix="logging-test-"))
os.environ["DATA_DIR"] = str(TEST_DATA)
os.environ["DOWNLOAD_ROOT"] = str(TEST_DATA / "downloads")

from app import main


class StructuredLoggingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        main.initialize_database()

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(TEST_DATA, ignore_errors=True)

    def setUp(self) -> None:
        self.stream = io.StringIO()
        self.handler = logging.StreamHandler(self.stream)
        self.handler.setFormatter(main.JsonLogFormatter())
        main.LOGGER.addHandler(self.handler)
        main.LOGGER.setLevel(logging.DEBUG)
        with main.connection() as db:
            db.execute("DELETE FROM task_media")
            db.execute("DELETE FROM tasks")

    def tearDown(self) -> None:
        main.LOGGER.removeHandler(self.handler)
        main.LOGGER.setLevel(getattr(logging, main.LOG_LEVEL, logging.INFO))

    def events(self) -> list[dict[str, object]]:
        return [json.loads(line) for line in self.stream.getvalue().splitlines() if line]

    def current_log_path(self) -> Path:
        return main.LOG_ROOT / f"{main.LOG_FILE_PREFIX}{main.local_now().date().isoformat()}.jsonl"

    def test_json_events_have_required_fields_and_exclude_credentials(self) -> None:
        main.log_event(logging.INFO, "test.event", "Structured event", task_id=7, api_hash="must-not-appear", password="must-not-appear")

        event = self.events()[0]
        self.assertEqual(event["event"], "test.event")
        self.assertEqual(event["level"], "INFO")
        self.assertEqual(event["logger"], "telegram_media_archiver")
        self.assertEqual(event["task_id"], 7)
        self.assertIn("timestamp", event)
        self.assertNotIn("api_hash", event)
        self.assertNotIn("password", event)
        self.assertNotIn("must-not-appear", self.stream.getvalue())
        self.assertIn("test.event", self.current_log_path().read_text())

    def test_error_events_include_traceback(self) -> None:
        try:
            raise RuntimeError("expected logging test failure")
        except RuntimeError:
            main.log_event(logging.ERROR, "test.failed", "Expected failure", exc_info=True)

        event = self.events()[0]
        self.assertEqual(event["event"], "test.failed")
        self.assertIn("RuntimeError: expected logging test failure", str(event["exception"]))

    def test_state_changes_are_logged_once_per_transition(self) -> None:
        with main.connection() as db:
            task_id = db.execute(
                """INSERT INTO tasks (chat_id, chat_title, filters_json, status, created_at, updated_at)
                VALUES ('chat-1', '日志测试聊天', '{}', 'QUEUED', ?, ?)""",
                (main.now(), main.now()),
            ).lastrowid
            media_id = db.execute(
                """INSERT INTO task_media (task_id, message_id, filename, media_type, size_bytes, message_date)
                VALUES (?, 42, 'log-test.jpg', 'PHOTO', 123, ?)""",
                (task_id, main.now()),
            ).lastrowid

        main.update_task(task_id, status="SCANNING")
        main.update_task(task_id, status="SCANNING")
        main.update_media(task_id, media_id, status="DOWNLOADING")
        main.update_media(task_id, media_id, status="DOWNLOADING")

        events = self.events()
        task_events = [event for event in events if event["event"] == "task.state_changed"]
        media_events = [event for event in events if event["event"] == "media.state_changed"]
        self.assertEqual(len(task_events), 1)
        self.assertEqual(len(media_events), 1)
        self.assertEqual(task_events[0]["previous_status"], "QUEUED")
        self.assertEqual(media_events[0]["filename"], "log-test.jpg")

    def test_rate_limit_and_thumbnail_failure_emit_operational_events(self) -> None:
        async def register_rate_limit() -> None:
            main.register_flood_wait(5)

        asyncio.run(register_rate_limit())
        main.set_thumbnail_result(99, "FAILED", error="expected thumbnail failure")

        events = self.events()
        self.assertTrue(any(event["event"] == "download.rate_limited" for event in events))
        self.assertTrue(any(event["event"] == "thumbnail.state_changed" and event["status"] == "FAILED" for event in events))

    def test_http_access_event_uses_path_without_query_string(self) -> None:
        request = Request({
            "type": "http",
            "method": "GET",
            "path": "/api/app-state",
            "query_string": b"api_hash=must-not-persist",
            "headers": [],
            "client": ("127.0.0.1", 9000),
        })

        async def call_next(_: Request) -> Response:
            return Response(status_code=200)

        response = asyncio.run(main.structured_access_log(request, call_next))

        self.assertEqual(response.status_code, 200)
        access_event = next(event for event in self.events() if event["event"] == "http.request_completed")
        self.assertEqual(access_event["path"], "/api/app-state")
        self.assertNotIn("must-not-persist", self.stream.getvalue())

    def test_uvicorn_errors_are_written_as_json(self) -> None:
        logging.getLogger("uvicorn.error").info("test uvicorn event")

        records = [json.loads(line) for line in self.current_log_path().read_text().splitlines()]
        event = next(record for record in reversed(records) if record["message"] == "test uvicorn event")
        self.assertEqual(event["logger"], "uvicorn.error")
        self.assertEqual(event["event"], "server.log")

    def test_cryptg_unavailable_does_not_block_startup(self) -> None:
        with patch.object(main, "CRYPTG_AVAILABLE", False):
            with TestClient(main.app):
                pass

        event = next(event for event in self.events() if event["event"] == "telegram.crypto_acceleration")
        self.assertFalse(event["cryptg_available"])


class DailyCompressedJsonlHandlerTests(unittest.TestCase):
    def handler(self, directory: Path, stream: io.StringIO) -> main.DailyCompressedJsonlHandler:
        fallback = logging.StreamHandler(stream)
        fallback.setFormatter(main.JsonLogFormatter())
        handler = main.DailyCompressedJsonlHandler(directory, fallback)
        handler.setFormatter(main.JsonLogFormatter())
        return handler

    def record(self, message: str = "test record") -> logging.LogRecord:
        record = logging.LogRecord("test", logging.INFO, __file__, 1, message, (), None)
        record.event = "test.event"
        record.event_data = {}
        return record

    def test_cross_day_rotation_compresses_previous_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="daily-log-test-") as temporary:
            directory = Path(temporary)
            handler = self.handler(directory, io.StringIO())
            first_day = main.date(2026, 7, 29)
            second_day = main.date(2026, 7, 30)
            with patch.object(main, "local_now", return_value=main.datetime(2026, 7, 29, 23, 59, tzinfo=main.UTC)):
                handler.emit(self.record("first day"))
            with patch.object(main, "local_now", return_value=main.datetime(2026, 7, 30, 0, 1, tzinfo=main.UTC)):
                handler.emit(self.record("second day"))
            handler.close()

            compressed = directory / f"{main.LOG_FILE_PREFIX}{first_day.isoformat()}.jsonl.gz"
            active = directory / f"{main.LOG_FILE_PREFIX}{second_day.isoformat()}.jsonl"
            self.assertTrue(compressed.is_file())
            self.assertTrue(active.is_file())
            with gzip.open(compressed, "rt", encoding="utf-8") as file:
                self.assertIn("first day", file.read())
            self.assertIn("second day", active.read_text())

    def test_startup_maintenance_compresses_and_keeps_thirty_dates(self) -> None:
        with tempfile.TemporaryDirectory(prefix="daily-log-retention-test-") as temporary:
            directory = Path(temporary)
            current_day = main.date(2026, 7, 30)
            handler = self.handler(directory, io.StringIO())
            for offset in range(31):
                day = current_day - main.timedelta(days=offset)
                (directory / f"{main.LOG_FILE_PREFIX}{day.isoformat()}.jsonl").write_text(day.isoformat())
            handler._maintain(current_day)
            handler.close()

            files = sorted(directory.glob(f"{main.LOG_FILE_PREFIX}*.jsonl*"))
            self.assertEqual(len(files), 30)
            self.assertFalse((directory / f"{main.LOG_FILE_PREFIX}2026-06-30.jsonl").exists())
            self.assertTrue((directory / f"{main.LOG_FILE_PREFIX}2026-07-01.jsonl.gz").is_file())

    def test_file_failure_keeps_stdout_fallback(self) -> None:
        with tempfile.TemporaryDirectory(prefix="daily-log-failure-test-") as temporary:
            stream = io.StringIO()
            blocked_path = Path(temporary) / "not-a-directory"
            blocked_path.write_text("block directory creation")
            handler = self.handler(blocked_path, stream)
            handler.emit(self.record())

            fallback_events = [json.loads(line) for line in stream.getvalue().splitlines()]
            self.assertTrue(any(event["event"] == "logging.persistence_failed" for event in fallback_events))


class RunScriptLogLevelTests(unittest.TestCase):
    def test_help_and_invalid_log_level(self) -> None:
        root = Path(__file__).resolve().parents[2]
        help_result = subprocess.run([str(root / "run.sh"), "--help"], text=True, capture_output=True, check=True)
        self.assertIn("--log-level LEVEL", help_result.stdout)
        self.assertIn("Defaults to INFO", help_result.stdout)

        invalid_result = subprocess.run([str(root / "run.sh"), "--log-level", "verbose", "start"], text=True, capture_output=True)
        self.assertEqual(invalid_result.returncode, 2)
        self.assertIn("unsupported log level", invalid_result.stderr)

    def test_default_and_explicit_levels_are_forwarded_without_running_docker(self) -> None:
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory(prefix="run-script-log-level-test-") as temp_dir:
            temporary = Path(temp_dir)
            fake_bin = temporary / "bin"
            fake_bin.mkdir()
            fake_docker = fake_bin / "docker"
            fake_docker.write_text("#!/usr/bin/env bash\nexit 0\n")
            fake_docker.chmod(0o755)
            env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"}

            default = subprocess.run([str(root / "run.sh"), "-d", str(temporary / "default-data"), "start"], text=True, capture_output=True, check=True, env=env)
            debug = subprocess.run([str(root / "run.sh"), "-d", str(temporary / "debug-data"), "--log-level", "debug", "start"], text=True, capture_output=True, check=True, env=env)

        self.assertIn("Using application log level: INFO", default.stdout)
        self.assertIn("Using application log level: DEBUG", debug.stdout)


if __name__ == "__main__":
    unittest.main()
