from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from telethon.errors import RpcCallFailError, TimedOutError

TEST_DATA = Path(tempfile.mkdtemp(prefix="download-retries-test-"))
os.environ["DATA_DIR"] = str(TEST_DATA)
os.environ["DOWNLOAD_ROOT"] = str(TEST_DATA / "downloads")

from app import main


class DownloadRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        main.DOWNLOAD_CLIENT = None
        main.DOWNLOAD_CLIENT_RESET_REQUESTED = False
        main.DOWNLOAD_RUNNING.clear()

    def tearDown(self) -> None:
        main.DOWNLOAD_CLIENT = None
        main.DOWNLOAD_CLIENT_RESET_REQUESTED = False
        main.DOWNLOAD_RUNNING.clear()

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(TEST_DATA, ignore_errors=True)

    def test_rpc_call_failure_is_recoverable_telegram_server_error(self) -> None:
        category, recoverable, message = main.classify_download_error(RpcCallFailError(None))

        self.assertEqual(category, "TELEGRAM_SERVER")
        self.assertTrue(recoverable)
        self.assertEqual(message, "Telegram 服务暂时无法处理下载请求，将自动重试")

    def test_timeout_is_recoverable_telegram_server_error(self) -> None:
        category, recoverable, message = main.classify_download_error(TimedOutError(None, "Timeout while fetching data"))

        self.assertEqual(category, "TELEGRAM_SERVER")
        self.assertTrue(recoverable)
        self.assertEqual(message, "Telegram 服务暂时无法处理下载请求，将自动重试")

    def test_client_reset_waits_for_active_downloads_then_allows_a_fresh_client(self) -> None:
        main.DOWNLOAD_CLIENT = object()
        main.DOWNLOAD_RUNNING[(1, 1)] = object()  # type: ignore[assignment]
        main.request_download_client_reset(1, 1)

        with patch.object(main, "close_download_client", new_callable=AsyncMock) as close_client:
            asyncio.run(main.reset_download_client_if_requested())
            close_client.assert_not_awaited()
            self.assertTrue(main.DOWNLOAD_CLIENT_RESET_REQUESTED)

            main.DOWNLOAD_RUNNING.clear()
            asyncio.run(main.reset_download_client_if_requested())

        close_client.assert_awaited_once()
        self.assertFalse(main.DOWNLOAD_CLIENT_RESET_REQUESTED)

    def test_failed_client_reset_clears_the_client_for_the_next_retry(self) -> None:
        main.DOWNLOAD_CLIENT = object()
        main.DOWNLOAD_CLIENT_RESET_REQUESTED = True

        with patch.object(main, "close_download_client", new_callable=AsyncMock, side_effect=RuntimeError("disconnect failed")):
            asyncio.run(main.reset_download_client_if_requested())

        self.assertIsNone(main.DOWNLOAD_CLIENT)
        self.assertFalse(main.DOWNLOAD_CLIENT_RESET_REQUESTED)

    def test_client_reset_waits_for_an_active_shared_client_lease(self) -> None:
        class FakeTelegramClient:
            def __init__(self) -> None:
                self.disconnect_calls = 0

            def is_connected(self) -> bool:
                return True

            async def is_user_authorized(self) -> bool:
                return True

            async def disconnect(self) -> None:
                self.disconnect_calls += 1

        client = FakeTelegramClient()

        async def exercise() -> None:
            entered = asyncio.Event()
            release = asyncio.Event()

            async def hold_lease() -> None:
                async with main.connected_telegram_client():
                    entered.set()
                    await release.wait()

            main.DOWNLOAD_CLIENT = client  # type: ignore[assignment]
            main.DOWNLOAD_CLIENT_RESET_REQUESTED = True
            holder = asyncio.create_task(hold_lease())
            await entered.wait()
            reset = asyncio.create_task(main.reset_download_client_if_requested())
            await asyncio.sleep(0)
            self.assertEqual(client.disconnect_calls, 0)
            release.set()
            await holder
            await reset

        asyncio.run(exercise())
        self.assertEqual(client.disconnect_calls, 1)
        self.assertIsNone(main.DOWNLOAD_CLIENT)
        self.assertFalse(main.DOWNLOAD_CLIENT_RESET_REQUESTED)

    def test_download_client_keeps_the_last_rpc_error_and_can_reconnect(self) -> None:
        created: dict[str, object] = {}

        class FakeTelegramClient:
            def __init__(self, *args: object, **kwargs: object) -> None:
                created["args"] = args
                created["kwargs"] = kwargs

            async def connect(self) -> None:
                return None

        with (
            patch.object(main, "ensure_session_storage"),
            patch.object(main, "secure_session_file"),
            patch.object(main, "get_telegram_credentials", return_value=(12345, "a" * 32)),
            patch.object(main, "TelegramClient", FakeTelegramClient),
        ):
            asyncio.run(main.open_telegram_client())

        options = created["kwargs"]
        self.assertEqual(options["device_model"], main.TELEGRAM_DEVICE_MODEL)
        self.assertEqual(options["app_version"], main.PROJECT_VERSION)
        self.assertNotIn("system_version", options)
        self.assertNotIn("lang_code", options)
        self.assertNotIn("system_lang_code", options)
        self.assertEqual(options["connection_retries"], 5)
        self.assertEqual(options["request_retries"], 5)
        self.assertTrue(options["auto_reconnect"])
        self.assertTrue(options["raise_last_call_error"])
        self.assertEqual(main.app.version, main.PROJECT_VERSION)


if __name__ == "__main__":
    unittest.main()
