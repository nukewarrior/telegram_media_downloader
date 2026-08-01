from __future__ import annotations

import unittest
from pathlib import Path

from app import main


class DownloadPathTests(unittest.TestCase):
    def destination(self, *, chat_id: str = "-100123", chat_title: str = "项目 / 资料", filename: str = "report.pdf", message_id: int = 55, message_date: str = "2026-07-30T04:00:00+00:00") -> Path:
        return main.archive_destination(
            {"chat_id": chat_id, "chat_title": chat_title, "archive_timezone": "Asia/Shanghai"},
            {"filename": filename, "message_id": message_id, "message_date": message_date},
        )

    def test_destination_uses_chat_identity_message_date_and_unique_filename(self) -> None:
        destination = self.destination()

        self.assertEqual(
            destination,
            Path(main.DOWNLOAD_ROOT) / "项目 _ 资料__chat--100123" / "2026" / "07" / "report__msg-55.pdf",
        )
        self.assertNotIn("/55/", str(destination))
        self.assertEqual(destination.with_suffix(destination.suffix + ".part").parent, destination.parent)

    def test_staging_path_is_scoped_to_task_and_media(self) -> None:
        destination = main.Destination(
            id=7,
            name="本地",
            kind="LOCAL",
            local_root=Path(main.DOWNLOAD_ROOT),
        )
        relative = Path("chat/2026/07/report__msg-55.jpg")

        first = main.archive_stage_path({"id": 1}, {"id": 2}, destination, relative)
        second = main.archive_stage_path({"id": 3}, {"id": 4}, destination, relative)

        self.assertEqual(first.parent, main.STAGING_ROOT)
        self.assertNotEqual(first, second)
        self.assertTrue(first.name.endswith(".jpg.part"))

    def test_same_title_chats_and_same_name_files_do_not_collide(self) -> None:
        first = self.destination(chat_id="-1001", message_id=10)
        second_chat = self.destination(chat_id="-1002", message_id=10)
        second_message = self.destination(chat_id="-1001", message_id=11)

        self.assertNotEqual(first.parent, second_chat.parent)
        self.assertNotEqual(first.name, second_message.name)

    def test_message_date_is_converted_to_china_standard_time(self) -> None:
        january = self.destination(message_date="2025-12-31T16:30:00+00:00")

        self.assertEqual(january.parent.parent.name, "2026")
        self.assertEqual(january.parent.name, "01")

    def test_task_timezone_snapshot_controls_the_archive_month(self) -> None:
        destination = main.archive_destination(
            {"chat_id": "-100123", "chat_title": "项目", "archive_timezone": "America/Los_Angeles"},
            {"filename": "report.pdf", "message_id": 55, "message_date": "2026-01-01T07:30:00+00:00"},
        )

        self.assertEqual(destination.parent.parent.name, "2025")
        self.assertEqual(destination.parent.name, "12")


if __name__ == "__main__":
    unittest.main()
