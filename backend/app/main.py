from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import sqlite3
import sys
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image as PillowImage
from PIL import ImageOps, UnidentifiedImageError
from pydantic import BaseModel, Field
from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError, RpcCallFailError, SessionPasswordNeededError, TimedOutError


DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
DB_PATH = DATA_DIR / "app.db"
DOWNLOAD_ROOT = os.getenv("DOWNLOAD_ROOT", str(DATA_DIR / "downloads"))
THUMBNAIL_ROOT = DATA_DIR / "thumbnails"
PREVIEW_ROOT = DATA_DIR / "previews"
DEMO_MODE = os.getenv("DEMO_MODE", "false").lower() == "true"
STATIC_DIR = Path(os.getenv("STATIC_DIR", "./static"))
SESSION_DIR = DATA_DIR / "sessions"
SESSION_PATH = SESSION_DIR / "telegram.session"
LOGIN_ATTEMPT_TTL = timedelta(minutes=10)
TELEGRAM_LOCK = asyncio.Lock()
TASK_WORKERS: dict[int, asyncio.Task[None]] = {}
DOWNLOAD_DISPATCHER: asyncio.Task[None] | None = None
THUMBNAIL_WORKER: asyncio.Task[None] | None = None
DOWNLOAD_CLIENT: TelegramClient | None = None
DOWNLOAD_CLIENT_RESET_REQUESTED = False
DOWNLOAD_RUNNING: dict[tuple[int, int], asyncio.Task[None]] = {}
PREVIEW_RUNNING: dict[int, asyncio.Task[None]] = {}
DOWNLOAD_REQUEUED_CANCELS: set[tuple[int, int]] = set()
DOWNLOAD_WAKE = asyncio.Event()
THUMBNAIL_WAKE = asyncio.Event()
DOWNLOAD_EFFECTIVE_CONCURRENCY = 3
DOWNLOAD_FLOOD_UNTIL: datetime | None = None
DOWNLOAD_ERROR_TIMES: deque[float] = deque()
DOWNLOAD_LAST_ERROR_AT: float | None = None
DOWNLOAD_ROUND_ROBIN_OFFSET = 0
DOWNLOAD_CLIENT_LOCK = asyncio.Lock()
RETRY_DELAYS_SECONDS = (5, 30, 120)
STABILITY_WINDOW_SECONDS = 5 * 60
SCAN_CACHE_TTL = timedelta(minutes=10)
SCAN_CACHE: dict[str, tuple[datetime, list[tuple[int, str, str, str | None, int, str]]]] = {}
PREVIEW_CACHE_TTL = timedelta(hours=24)
PREVIEW_LAST_CLEANUP_AT: datetime | None = None
AVAILABLE_TIMEZONES = frozenset(available_timezones())
UNFINISHED_TASK_STATUSES = ("QUEUED", "SCANNING", "DOWNLOADING", "RETRYING", "WAITING_RATE_LIMIT", "PAUSED")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_ROOT = DATA_DIR / "logs"
LOG_FILE_PREFIX = "telegram-media-archiver-"
LOG_RETENTION_DAYS = 30
LOG_FAILURE_REPORT_INTERVAL = timedelta(minutes=5)
LOGGER = logging.getLogger("telegram_media_archiver")
SENSITIVE_LOG_KEY_PARTS = ("api_hash", "password", "code", "session", "token", "attempt_id")


def local_now() -> datetime:
    """Return the host/container local time with its UTC offset."""
    return datetime.now().astimezone()


class JsonLogFormatter(logging.Formatter):
    """Emit one self-contained JSON object per application log event."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": local_now().isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", "server.log" if record.name.startswith("uvicorn") else "application_log"),
            "message": record.getMessage(),
        }
        payload.update(getattr(record, "event_data", {}))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class DailyCompressedJsonlHandler(logging.Handler):
    """Persist JSONL by local calendar day without allowing file I/O to stop the service."""

    def __init__(self, directory: Path, fallback_handler: logging.Handler) -> None:
        super().__init__()
        self.directory = directory
        self.fallback_handler = fallback_handler
        self.stream: Any | None = None
        self.active_day: date | None = None
        self.next_retry_at: datetime | None = None
        self.last_failure_reported_at: datetime | None = None
        self._prepare_directory()

    def _path_for(self, day: date) -> Path:
        return self.directory / f"{LOG_FILE_PREFIX}{day.isoformat()}.jsonl"

    def _dated_files(self) -> list[tuple[date, Path]]:
        pattern = re.compile(rf"^{re.escape(LOG_FILE_PREFIX)}(\d{{4}}-\d{{2}}-\d{{2}})\.jsonl(?:\.gz)?$")
        try:
            paths = list(self.directory.iterdir())
        except OSError as error:
            self._report_failure("list", error)
            return []
        result: list[tuple[date, Path]] = []
        for path in paths:
            match = pattern.match(path.name)
            if not match:
                continue
            try:
                result.append((date.fromisoformat(match.group(1)), path))
            except ValueError:
                continue
        return result

    def _prepare_directory(self) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            self._maintain(local_now().date())
        except OSError as error:
            self._report_failure("initialize", error)

    def _compress(self, source: Path) -> None:
        target = source.with_suffix(source.suffix + ".gz")
        if target.is_file():
            source.unlink()
            return
        temporary = target.with_suffix(target.suffix + ".tmp")
        try:
            with source.open("rb") as input_file, gzip.open(temporary, "wb") as output_file:
                shutil.copyfileobj(input_file, output_file)
            temporary.replace(target)
            source.unlink()
        finally:
            temporary.unlink(missing_ok=True)

    def _maintain(self, current_day: date) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        for file_day, path in self._dated_files():
            if file_day < current_day and path.suffix == ".jsonl":
                try:
                    self._compress(path)
                except OSError as error:
                    self._report_failure("compress", error)
        cutoff = current_day - timedelta(days=LOG_RETENTION_DAYS - 1)
        for file_day, path in self._dated_files():
            if file_day < cutoff:
                try:
                    path.unlink()
                except OSError as error:
                    self._report_failure("cleanup", error)

    def _close_stream(self) -> None:
        if self.stream:
            try:
                self.stream.close()
            except OSError:
                pass
        self.stream = None
        self.active_day = None

    def _report_failure(self, operation: str, error: OSError) -> None:
        current_time = local_now()
        if self.last_failure_reported_at and current_time - self.last_failure_reported_at < LOG_FAILURE_REPORT_INTERVAL:
            return
        self.last_failure_reported_at = current_time
        try:
            fallback = logging.LogRecord(
                "telegram_media_archiver",
                logging.ERROR,
                __file__,
                0,
                "Persistent log storage failed; continuing with stdout only",
                (),
                None,
            )
            fallback.event = "logging.persistence_failed"
            fallback.event_data = {"operation": operation, "log_directory": str(self.directory), "error_type": type(error).__name__}
            self.fallback_handler.emit(fallback)
        except Exception:
            pass

    def _ensure_stream(self, current_day: date) -> bool:
        if self.stream and self.active_day == current_day:
            return True
        self._close_stream()
        try:
            self._maintain(current_day)
            self.stream = self._path_for(current_day).open("a", encoding="utf-8")
            self.active_day = current_day
            self.next_retry_at = None
            return True
        except OSError as error:
            self.next_retry_at = local_now() + timedelta(minutes=1)
            self._report_failure("open", error)
            return False

    def emit(self, record: logging.LogRecord) -> None:
        self.acquire()
        try:
            current_time = local_now()
            if self.next_retry_at and current_time < self.next_retry_at:
                return
            if not self._ensure_stream(current_time.date()):
                return
            try:
                self.stream.write(self.format(record) + "\n")
                self.stream.flush()
            except OSError as error:
                self._close_stream()
                self.next_retry_at = current_time + timedelta(minutes=1)
                self._report_failure("write", error)
        finally:
            self.release()

    def close(self) -> None:
        self.acquire()
        try:
            self._close_stream()
            super().close()
        finally:
            self.release()


def configure_logging() -> None:
    """Send application and Uvicorn logs to stdout and a durable local JSONL stream."""
    level = getattr(logging, LOG_LEVEL, logging.INFO)
    formatter = JsonLogFormatter()
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    persistent_handler = DailyCompressedJsonlHandler(LOG_ROOT, stdout_handler)
    persistent_handler.setFormatter(formatter)
    for logger in (LOGGER, logging.getLogger("uvicorn")):
        logger.setLevel(level)
        logger.propagate = False
        logger.handlers.clear()
        logger.addHandler(stdout_handler)
        logger.addHandler(persistent_handler)
    uvicorn_error = logging.getLogger("uvicorn.error")
    uvicorn_error.handlers.clear()
    uvicorn_error.setLevel(level)
    uvicorn_error.propagate = True
    # Access records are emitted by the FastAPI middleware below so that we can
    # omit query strings and include a duration without duplicate log lines.
    uvicorn_access = logging.getLogger("uvicorn.access")
    uvicorn_access.handlers.clear()
    uvicorn_access.disabled = True


def log_event(level: int, event: str, message: str, *, exc_info: bool = False, **context: Any) -> None:
    """Log structured operational context while rejecting known credential fields."""
    safe_context = {
        key: value
        for key, value in context.items()
        if not any(part in key.lower().replace("-", "_") for part in SENSITIVE_LOG_KEY_PARTS)
    }
    LOGGER.log(level, message, extra={"event": event, "event_data": safe_context}, exc_info=exc_info)


configure_logging()


@dataclass
class PendingLogin:
    phone: str
    phone_code_hash: str | None
    expires_at: datetime


pending_logins: dict[str, PendingLogin] = {}


def now() -> str:
    return datetime.now(UTC).isoformat()


def connection() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def rows(items: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(item) for item in items]


def ensure_session_storage() -> None:
    """Create the private directory that contains Telethon's SQLite session."""
    try:
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(SESSION_DIR, 0o700)
        if SESSION_PATH.exists():
            os.chmod(SESSION_PATH, 0o600)
    except OSError as error:
        raise RuntimeError("无法保护 Telegram 会话目录；请检查数据目录权限") from error


def secure_session_file() -> None:
    if SESSION_PATH.exists():
        try:
            os.chmod(SESSION_PATH, 0o600)
        except OSError as error:
            raise RuntimeError("无法保护 Telegram 会话文件；请检查数据目录权限") from error


def mask_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    if len(digits) <= 4:
        return "••••"
    return f"+{digits[:min(3, len(digits) - 4)]}••••{digits[-4:]}"


def normalize_phone(phone: str) -> str:
    normalized = re.sub(r"[\s()\-]", "", phone)
    if not re.fullmatch(r"\+\d{5,15}", normalized):
        raise HTTPException(400, "请输入带国家区号的手机号，例如 +8613800000000")
    return normalized


def discard_expired_attempts() -> None:
    current_time = datetime.now(UTC)
    for attempt_id, attempt in list(pending_logins.items()):
        if attempt.expires_at <= current_time:
            pending_logins.pop(attempt_id, None)


def require_pending_login(attempt_id: str) -> PendingLogin:
    discard_expired_attempts()
    attempt = pending_logins.get(attempt_id)
    if not attempt:
        raise HTTPException(400, "登录会话已过期，请重新发送验证码")
    return attempt


def get_telegram_credentials() -> tuple[int, str]:
    with connection() as db:
        setting = db.execute("SELECT api_id, api_hash FROM app_settings WHERE id = 1").fetchone()
    if not setting or not setting["api_id"] or not setting["api_hash"]:
        raise HTTPException(409, "请先配置 Telegram API 凭据")
    try:
        api_id = int(setting["api_id"])
    except (TypeError, ValueError) as error:
        raise HTTPException(400, "API ID 必须是数字") from error
    if api_id <= 0:
        raise HTTPException(400, "API ID 必须是正整数")
    return api_id, setting["api_hash"]


def mark_session_invalid() -> None:
    with connection() as db:
        db.execute(
            "UPDATE app_settings SET account_connected = 0, connection_status = 'invalid', updated_at = ? WHERE id = 1",
            (now(),),
        )
    log_event(logging.WARNING, "telegram.session_invalid", "Telegram session marked invalid")


def clear_local_session() -> None:
    # SQLite may use either rollback-journal or WAL sidecar files.  Only remove
    # the known artifacts next to this application's fixed session filename.
    for path in (SESSION_PATH, Path(f"{SESSION_PATH}-journal"), Path(f"{SESSION_PATH}-wal"), Path(f"{SESSION_PATH}-shm")):
        try:
            path.unlink(missing_ok=True)
        except OSError as error:
            raise HTTPException(500, "无法删除本地 Telegram 会话文件；请检查数据目录权限") from error


def update_connected_account(phone: str, user: Any) -> None:
    name = " ".join(part for part in [getattr(user, "first_name", None), getattr(user, "last_name", None)] if part).strip()
    name = name or getattr(user, "username", None) or "已连接的 Telegram 账号"
    with connection() as db:
        db.execute(
            """UPDATE app_settings SET account_phone = ?, account_name = ?, account_connected = 1,
               connection_status = 'connected', updated_at = ? WHERE id = 1""",
            (mask_phone(phone), name, now()),
        )


async def open_telegram_client() -> TelegramClient:
    ensure_session_storage()
    api_id, api_hash = get_telegram_credentials()
    client = TelegramClient(
        str(SESSION_PATH),
        api_id,
        api_hash,
        receive_updates=False,
        # Downloads can use a different Telegram data centre from the one used
        # for login. Keep the long-lived download client able to reconnect and
        # retain the final RPC error so retry classification is not reduced to
        # Telethon's generic "Request was unsuccessful" ValueError.
        auto_reconnect=True,
        connection_retries=5,
        request_retries=5,
        raise_last_call_error=True,
    )
    try:
        await client.connect()
        log_event(logging.DEBUG, "telegram.client_connected", "Connected temporary Telegram client")
    except (OSError, asyncio.TimeoutError) as error:
        log_event(logging.ERROR, "telegram.client_connect_failed", "Unable to connect Telegram client", exc_info=True, error_type=type(error).__name__)
        raise HTTPException(503, "无法连接 Telegram，请检查网络后重试") from error
    finally:
        secure_session_file()
    return client


async def close_telegram_client(client: TelegramClient) -> None:
    try:
        if client.is_connected():
            await client.disconnect()
            log_event(logging.DEBUG, "telegram.client_disconnected", "Disconnected temporary Telegram client")
    finally:
        secure_session_file()


def raise_telegram_error(error: Exception, *, during_authorization: bool = False) -> None:
    """Convert Telegram errors to safe messages without exposing credentials."""
    error_name = type(error).__name__
    if isinstance(error, FloodWaitError):
        seconds = max(1, int(getattr(error, "seconds", 1)))
        raise HTTPException(429, f"Telegram 要求等待 {seconds} 秒后再试", headers={"Retry-After": str(seconds)}) from error
    if error_name in {"PhoneNumberInvalidError", "PhoneNumberBannedError"}:
        raise HTTPException(400, "该手机号无法用于 Telegram 登录，请检查号码和账号状态") from error
    if error_name in {"PhoneCodeInvalidError", "PhoneCodeExpiredError", "PhoneCodeHashEmptyError", "PhoneCodeHashInvalidError"}:
        raise HTTPException(400, "验证码无效或已过期，请重新发送验证码") from error
    if error_name in {"PasswordHashInvalidError"}:
        raise HTTPException(400, "两步验证密码不正确") from error
    if error_name in {"AuthKeyUnregisteredError", "SessionRevokedError", "UserDeactivatedError", "UserDeactivatedBanError"}:
        mark_session_invalid()
        raise HTTPException(409, "Telegram 登录状态已失效，请重新连接账号") from error
    if isinstance(error, (OSError, asyncio.TimeoutError)):
        raise HTTPException(503, "无法连接 Telegram，请检查网络后重试") from error
    if isinstance(error, RPCError):
        message = "Telegram 暂时拒绝了此请求，请稍后重试" if during_authorization else "Telegram 登录状态不可用，请重新连接账号"
        raise HTTPException(400 if during_authorization else 409, message) from error
    raise HTTPException(503, "Telegram 服务暂时不可用，请稍后重试") from error


def initialize_database() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    Path(DOWNLOAD_ROOT).mkdir(parents=True, exist_ok=True)
    THUMBNAIL_ROOT.mkdir(parents=True, exist_ok=True)
    PREVIEW_ROOT.mkdir(parents=True, exist_ok=True)
    ensure_session_storage()
    with connection() as db:
        db.executescript(
            """
            PRAGMA journal_mode = WAL;
            PRAGMA foreign_keys = ON;
            CREATE TABLE IF NOT EXISTS app_settings (
              id INTEGER PRIMARY KEY CHECK (id = 1),
              api_id TEXT,
              api_hash TEXT,
              account_phone TEXT,
              account_name TEXT,
              account_connected INTEGER NOT NULL DEFAULT 0,
              connection_status TEXT NOT NULL DEFAULT 'disconnected',
              archive_timezone TEXT,
              download_concurrency_max INTEGER NOT NULL DEFAULT 3,
              updated_at TEXT NOT NULL
            );
            INSERT OR IGNORE INTO app_settings (id, updated_at) VALUES (1, CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS login_attempts (
              id TEXT PRIMARY KEY,
              phone TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tasks (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              chat_id TEXT NOT NULL,
              chat_title TEXT NOT NULL,
              chat_handle TEXT,
              archive_timezone TEXT,
              filters_json TEXT NOT NULL,
              status TEXT NOT NULL,
              total_count INTEGER NOT NULL DEFAULT 0,
              completed_count INTEGER NOT NULL DEFAULT 0,
              failed_count INTEGER NOT NULL DEFAULT 0,
              total_bytes INTEGER NOT NULL DEFAULT 0,
              downloaded_bytes INTEGER NOT NULL DEFAULT 0,
              current_file TEXT,
              speed_bytes_per_second INTEGER NOT NULL DEFAULT 0,
              media_revision INTEGER NOT NULL DEFAULT 0,
              download_wait_until TEXT,
              error_message TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS media_blobs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              content_hash TEXT UNIQUE,
              canonical_path TEXT NOT NULL,
              thumbnail_path TEXT,
              thumbnail_status TEXT NOT NULL DEFAULT 'PENDING',
              thumbnail_error TEXT,
              size_bytes INTEGER NOT NULL,
              media_type TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS archive_items (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              blob_id INTEGER REFERENCES media_blobs(id),
              chat_id TEXT NOT NULL,
              chat_title TEXT NOT NULL,
              message_id INTEGER NOT NULL,
              filename TEXT NOT NULL,
              media_type TEXT NOT NULL,
              mime_type TEXT,
              size_bytes INTEGER NOT NULL,
              message_date TEXT NOT NULL,
              duplicate_of INTEGER REFERENCES archive_items(id),
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS task_media (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
              message_id INTEGER NOT NULL,
              filename TEXT NOT NULL,
              media_type TEXT NOT NULL,
              mime_type TEXT,
              size_bytes INTEGER NOT NULL,
              message_date TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'PENDING',
              error_message TEXT,
              downloaded_bytes INTEGER NOT NULL DEFAULT 0,
              speed_bytes_per_second INTEGER NOT NULL DEFAULT 0,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              revision INTEGER NOT NULL DEFAULT 0,
              attempt_count INTEGER NOT NULL DEFAULT 0,
              next_retry_at TEXT,
              failure_category TEXT,
              preview_cache_id INTEGER,
              UNIQUE(task_id, message_id)
            );
            CREATE TABLE IF NOT EXISTS preview_cache (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              chat_id TEXT NOT NULL,
              message_id INTEGER NOT NULL,
              filename TEXT NOT NULL,
              media_type TEXT NOT NULL,
              mime_type TEXT,
              size_bytes INTEGER NOT NULL,
              message_date TEXT NOT NULL,
              cache_path TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'PENDING',
              downloaded_bytes INTEGER NOT NULL DEFAULT 0,
              error_message TEXT,
              expires_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(chat_id, message_id)
            );
            """
        )
        setting_columns = {column["name"] for column in db.execute("PRAGMA table_info(app_settings)").fetchall()}
        if "connection_status" not in setting_columns:
            db.execute("ALTER TABLE app_settings ADD COLUMN connection_status TEXT NOT NULL DEFAULT 'disconnected'")
            # Older versions only had a simulated connected flag.  It cannot be
            # trusted as a real Telethon session, so require reconnect once.
            db.execute("UPDATE app_settings SET connection_status = CASE WHEN account_connected = 1 THEN 'invalid' ELSE 'disconnected' END")
        if "download_concurrency_max" not in setting_columns:
            db.execute("ALTER TABLE app_settings ADD COLUMN download_concurrency_max INTEGER NOT NULL DEFAULT 3")
        if "archive_timezone" not in setting_columns:
            db.execute("ALTER TABLE app_settings ADD COLUMN archive_timezone TEXT")
        task_columns = {column["name"] for column in db.execute("PRAGMA table_info(tasks)").fetchall()}
        if "archive_timezone" not in task_columns:
            db.execute("ALTER TABLE tasks ADD COLUMN archive_timezone TEXT")
        if "media_revision" not in task_columns:
            db.execute("ALTER TABLE tasks ADD COLUMN media_revision INTEGER NOT NULL DEFAULT 0")
        if "download_wait_until" not in task_columns:
            db.execute("ALTER TABLE tasks ADD COLUMN download_wait_until TEXT")
        task_media_columns = {column["name"] for column in db.execute("PRAGMA table_info(task_media)").fetchall()}
        blob_columns = {column["name"] for column in db.execute("PRAGMA table_info(media_blobs)").fetchall()}
        if "thumbnail_error" not in blob_columns:
            db.execute("ALTER TABLE media_blobs ADD COLUMN thumbnail_error TEXT")
        if "downloaded_bytes" not in task_media_columns:
            db.execute("ALTER TABLE task_media ADD COLUMN downloaded_bytes INTEGER NOT NULL DEFAULT 0")
        if "speed_bytes_per_second" not in task_media_columns:
            db.execute("ALTER TABLE task_media ADD COLUMN speed_bytes_per_second INTEGER NOT NULL DEFAULT 0")
        if "updated_at" not in task_media_columns:
            db.execute("ALTER TABLE task_media ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''")
            db.execute("UPDATE task_media SET updated_at = ? WHERE updated_at = ''", (now(),))
        if "revision" not in task_media_columns:
            db.execute("ALTER TABLE task_media ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
        if "attempt_count" not in task_media_columns:
            db.execute("ALTER TABLE task_media ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0")
        if "next_retry_at" not in task_media_columns:
            db.execute("ALTER TABLE task_media ADD COLUMN next_retry_at TEXT")
        if "failure_category" not in task_media_columns:
            db.execute("ALTER TABLE task_media ADD COLUMN failure_category TEXT")
        if "preview_cache_id" not in task_media_columns:
            db.execute("ALTER TABLE task_media ADD COLUMN preview_cache_id INTEGER")
        # A process restart cannot safely resume an in-flight Telethon transfer.
        # Keep completed work and return only the interrupted file to the queue.
        db.execute("UPDATE task_media SET status = 'PENDING', speed_bytes_per_second = 0 WHERE status = 'DOWNLOADING'")
        if DEMO_MODE and db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0:
            seed_demo_data(db)
    reconcile_thumbnail_records()
    cleanup_preview_cache()
    log_event(logging.INFO, "database.initialized", "Database and runtime directories are ready", data_dir=str(DATA_DIR), download_root=str(DOWNLOAD_ROOT), demo_mode=DEMO_MODE)


def seed_demo_data(db: sqlite3.Connection) -> None:
    created = now()
    active_task = (
        "科技前沿观察", "@tech_frontier_obs", '{"mediaTypes":["PHOTO","VIDEO"],"dateStart":"2025-01-01","dateEnd":"2026-07-30","minSizeMb":0,"maxSizeMb":0}',
        "DOWNLOADING", 2016, 1248, 0, 29_600_000_000, 18_400_000_000,
        "2026-07-30_14-35-01_5120.jpg", 8_700_000,
    )
    db.execute(
        """INSERT INTO tasks (chat_title, chat_handle, filters_json, status, total_count, completed_count,
           failed_count, total_bytes, downloaded_bytes, current_file, speed_bytes_per_second, chat_id, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'demo-tech', ?, ?)""",
        active_task + (created, created),
    )
    for title, handle, status, count, size in [
        ("设计灵感库", "@design_inspiration_cn", "COMPLETED", 3421, 9_700_000_000),
        ("经典电影收藏", "@movie_classic_zh", "COMPLETED", 1892, 22_100_000_000),
        ("学习资料库", "@study_materials_zh", "PAUSED", 5672, 13_200_000_000),
    ]:
        db.execute(
            """INSERT INTO tasks (chat_id, chat_title, chat_handle, filters_json, status, total_count, completed_count,
               total_bytes, downloaded_bytes, created_at, updated_at)
               VALUES (?, ?, ?, '{}', ?, ?, ?, ?, ?, ?, ?)""",
            (f"demo-{title}", title, handle, status, count, count if status == "COMPLETED" else count // 2,
             size, size if status == "COMPLETED" else size // 2, created, created),
        )
    for index, (title, media_type, filename, month, size) in enumerate([
        ("科技前沿观察", "PHOTO", "quantum-lab_5120.jpg", "2026-07", 2_400_000),
        ("科技前沿观察", "VIDEO", "launch-notes_5118.mp4", "2026-07", 182_000_000),
        ("设计灵感库", "PHOTO", "editorial-grid_1208.jpg", "2026-06", 3_600_000),
        ("经典电影收藏", "VIDEO", "scene-study_224.mp4", "2026-06", 756_000_000),
        ("学习资料库", "DOCUMENT", "asyncio-notes.pdf", "2026-05", 1_800_000),
    ]):
        canonical_path = str(Path(DOWNLOAD_ROOT) / title / month / filename)
        thumbnail_path: str | None = None
        thumbnail_status = "UNAVAILABLE"
        if media_type in {"PHOTO", "VIDEO"}:
            thumbnail_path = str(create_demo_thumbnail(index, media_type))
            thumbnail_status = "READY"
        blob = db.execute(
            """INSERT INTO media_blobs (content_hash, canonical_path, thumbnail_path, thumbnail_status, size_bytes, media_type, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (f"demo-hash-{index}", canonical_path, thumbnail_path, thumbnail_status, size, media_type, created),
        )
        db.execute(
            """INSERT INTO archive_items (blob_id, chat_id, chat_title, message_id, filename, media_type, mime_type,
            size_bytes, message_date, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (blob.lastrowid, f"demo-{title}", title, 5000 + index, filename, media_type,
             "image/jpeg" if media_type == "PHOTO" else "video/mp4" if media_type == "VIDEO" else "application/pdf",
             size, f"{month}-{18-index:02d}T09:30:00+00:00", created),
        )


def create_demo_thumbnail(index: int, media_type: str) -> Path:
    """Create a real, local image so demo archive cards never claim a missing preview is ready."""
    target = THUMBNAIL_ROOT / f"demo-{index}.jpg"
    if target.is_file():
        return target
    palettes = [(45, 127, 153), (89, 107, 158), (185, 119, 82), (64, 131, 115)]
    background = palettes[index % len(palettes)]
    image = PillowImage.new("RGB", (640, 400), background)
    # A simple two-tone composition makes the demo visually distinguishable without pretending
    # to be a downloaded user image.
    accent = tuple(min(255, component + 55) for component in background)
    for offset in range(0, 640, 80):
        image.paste(accent, (offset, 0, offset + 38, 400))
    image.save(target, format="JPEG", quality=82, optimize=True)
    return target


def path_in_root(path_value: str | Path, root: str | Path) -> Path | None:
    """Resolve a persisted path without allowing it to escape its dedicated data directory."""
    try:
        path = Path(path_value).resolve()
        path.relative_to(Path(root).resolve())
    except (OSError, ValueError):
        return None
    return path


def reconcile_thumbnail_records() -> None:
    """Repair old rows that predate real thumbnail generation or were interrupted mid-job."""
    with connection() as db:
        blobs = db.execute("SELECT id, canonical_path, thumbnail_path, thumbnail_status, media_type FROM media_blobs").fetchall()
        for blob in blobs:
            if blob["media_type"] not in {"PHOTO", "VIDEO"}:
                if blob["thumbnail_status"] != "UNAVAILABLE":
                    db.execute("UPDATE media_blobs SET thumbnail_status = 'UNAVAILABLE', thumbnail_error = NULL WHERE id = ?", (blob["id"],))
                continue
            thumbnail = path_in_root(blob["thumbnail_path"], THUMBNAIL_ROOT) if blob["thumbnail_path"] else None
            if blob["thumbnail_status"] == "READY" and thumbnail and thumbnail.is_file():
                continue
            source = path_in_root(blob["canonical_path"], DOWNLOAD_ROOT)
            if source and source.is_file():
                db.execute(
                    "UPDATE media_blobs SET thumbnail_path = NULL, thumbnail_status = 'PENDING', thumbnail_error = NULL WHERE id = ?",
                    (blob["id"],),
                )
            elif not thumbnail or not thumbnail.is_file():
                db.execute(
                    "UPDATE media_blobs SET thumbnail_path = NULL, thumbnail_status = 'UNAVAILABLE', thumbnail_error = '原始归档文件不存在' WHERE id = ?",
                    (blob["id"],),
                )


def wake_thumbnail_worker() -> None:
    THUMBNAIL_WAKE.set()


def claim_next_thumbnail() -> sqlite3.Row | None:
    with connection() as db:
        blob = db.execute(
            """SELECT * FROM media_blobs WHERE thumbnail_status = 'PENDING' AND media_type IN ('PHOTO', 'VIDEO')
               ORDER BY id LIMIT 1"""
        ).fetchone()
        if not blob:
            return None
        claimed = db.execute(
            """UPDATE media_blobs SET thumbnail_status = 'PROCESSING', thumbnail_error = NULL
               WHERE id = ? AND thumbnail_status = 'PENDING'""",
            (blob["id"],),
        ).rowcount
        return blob if claimed else None


def set_thumbnail_result(blob_id: int, status: str, *, path: Path | None = None, error: str | None = None) -> None:
    with connection() as db:
        db.execute(
            "UPDATE media_blobs SET thumbnail_path = ?, thumbnail_status = ?, thumbnail_error = ? WHERE id = ?",
            (str(path) if path else None, status, error, blob_id),
        )
    level = logging.INFO if status == "READY" else logging.WARNING
    log_event(level, "thumbnail.state_changed", "Thumbnail state changed", blob_id=blob_id, status=status, thumbnail_path=str(path) if path else None, error=error)


def create_image_thumbnail(source: Path, target: Path) -> None:
    temporary = target.with_suffix(".tmp")
    try:
        with PillowImage.open(source) as opened:
            image = ImageOps.exif_transpose(opened)
            if image.mode not in {"RGB", "L"}:
                image = image.convert("RGB")
            image.thumbnail((640, 640), PillowImage.Resampling.LANCZOS)
            image.save(temporary, format="JPEG", quality=85, optimize=True)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


async def create_video_thumbnail(source: Path, target: Path) -> None:
    temporary = target.with_name(f".{target.stem}.tmp.jpg")

    async def run_at(seconds: str | None) -> tuple[int, str]:
        command = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
        if seconds is not None:
            command.extend(["-ss", seconds])
        command.extend(["-i", str(source), "-frames:v", "1", "-vf", "scale=640:640:force_original_aspect_ratio=decrease", "-q:v", "3", str(temporary)])
        process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, stderr = await process.communicate()
        return process.returncode, stderr.decode("utf-8", "replace").strip()

    try:
        code, error = await run_at("1")
        if code != 0 or not temporary.is_file():
            temporary.unlink(missing_ok=True)
            code, error = await run_at(None)
        if code != 0 or not temporary.is_file():
            raise RuntimeError(error or "FFmpeg 未能抽取视频帧")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


async def generate_thumbnail(blob: sqlite3.Row) -> None:
    source = path_in_root(blob["canonical_path"], DOWNLOAD_ROOT)
    if not source or not source.is_file():
        set_thumbnail_result(blob["id"], "UNAVAILABLE", error="原始归档文件不存在")
        return
    target = THUMBNAIL_ROOT / f"{blob['id']}.jpg"
    try:
        log_event(logging.INFO, "thumbnail.started", "Thumbnail generation started", blob_id=blob["id"], media_type=blob["media_type"], source_path=str(source), target_path=str(target))
        if blob["media_type"] == "PHOTO":
            await asyncio.to_thread(create_image_thumbnail, source, target)
        else:
            await create_video_thumbnail(source, target)
    except (UnidentifiedImageError, OSError, RuntimeError) as error:
        log_event(logging.ERROR, "thumbnail.failed", "Thumbnail generation failed", exc_info=True, blob_id=blob["id"], media_type=blob["media_type"], source_path=str(source), error_type=type(error).__name__)
        set_thumbnail_result(blob["id"], "FAILED", error=str(error)[:500])
        return
    set_thumbnail_result(blob["id"], "READY", path=target)


async def thumbnail_worker() -> None:
    """Generate one preview at a time so archive maintenance never competes with Telegram downloads."""
    while True:
        blob = claim_next_thumbnail()
        if blob:
            try:
                await generate_thumbnail(blob)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log_event(logging.ERROR, "thumbnail.worker_failed", "Thumbnail worker failed", exc_info=True, blob_id=blob["id"], error_type=type(error).__name__)
                set_thumbnail_result(blob["id"], "FAILED", error=str(error)[:500])
            continue
        THUMBNAIL_WAKE.clear()
        try:
            await asyncio.wait_for(THUMBNAIL_WAKE.wait(), timeout=1)
        except asyncio.TimeoutError:
            pass


class ApiCredentials(BaseModel):
    api_id: str = Field(min_length=1, max_length=32)
    api_hash: str = Field(min_length=16, max_length=128)


class DownloadConcurrencySettings(BaseModel):
    max_concurrency: int = Field(ge=1, le=5)


class ArchiveTimezoneSettings(BaseModel):
    archive_timezone: str = Field(min_length=1, max_length=64)


class LoginCodeRequest(BaseModel):
    phone: str = Field(min_length=5, max_length=32)


class LoginCodeVerify(BaseModel):
    attempt_id: str
    code: str = Field(min_length=3, max_length=12)


class LoginPasswordVerify(BaseModel):
    attempt_id: str
    password: str = Field(min_length=1, max_length=128)


class TaskFilters(BaseModel):
    media_types: list[Literal["PHOTO", "VIDEO", "AUDIO", "DOCUMENT"]] = ["PHOTO", "VIDEO", "DOCUMENT"]
    date_start: str | None = None
    date_end: str | None = None
    min_size_mb: int = Field(default=0, ge=0)
    max_size_mb: int = Field(default=0, ge=0)


class ScanRequest(BaseModel):
    chat_id: str
    chat_title: str
    chat_handle: str | None = None
    filters: TaskFilters


class ArchiveQuery(BaseModel):
    chat_id: str | None = None
    media_type: str | None = None
    month: str | None = None


class SelectionTaskRequest(BaseModel):
    chat_id: str
    chat_title: str = Field(min_length=1, max_length=255)
    chat_handle: str | None = None
    message_ids: list[int] = Field(min_length=1, max_length=500)


class PreviewRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=180)
    media_type: Literal["PHOTO", "VIDEO", "AUDIO", "DOCUMENT"]
    mime_type: str | None = Field(default=None, max_length=255)
    size_bytes: int = Field(ge=0)
    message_date: str


def masked_api_id(api_id: str | None) -> str | None:
    if not api_id:
        return None
    return f"••••{api_id[-4:]}"


def validated_timezone(value: str) -> str:
    timezone_name = value.strip()
    if timezone_name not in AVAILABLE_TIMEZONES:
        raise HTTPException(422, "请选择有效的 IANA 时区")
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as error:
        raise HTTPException(422, "当前服务不支持该时区") from error
    return timezone_name


def app_state() -> dict[str, Any]:
    with connection() as db:
        setting = db.execute("SELECT * FROM app_settings WHERE id = 1").fetchone()
    api_configured = bool(setting["api_id"] and setting["api_hash"])
    archive_timezone = setting["archive_timezone"]
    configured = api_configured and bool(archive_timezone)
    stored_status = setting["connection_status"]
    connection_status = "unconfigured" if not configured else stored_status
    connected = configured and stored_status == "connected" and bool(setting["account_connected"])
    return {
        "configured": configured,
        "apiConfigured": api_configured,
        "archiveTimezone": archive_timezone,
        "accountConnected": connected,
        "accountName": setting["account_name"],
        "connectionStatus": connection_status,
        "downloadRoot": DOWNLOAD_ROOT,
        "demoMode": DEMO_MODE,
    }


def download_runtime_state() -> dict[str, Any]:
    with connection() as db:
        setting = db.execute("SELECT download_concurrency_max FROM app_settings WHERE id = 1").fetchone()
    return {
        "maxConcurrency": setting["download_concurrency_max"],
        "effectiveConcurrency": min(DOWNLOAD_EFFECTIVE_CONCURRENCY, setting["download_concurrency_max"]),
        "activeDownloads": len(DOWNLOAD_RUNNING) + len(PREVIEW_RUNNING),
        "waitUntil": DOWNLOAD_FLOOD_UNTIL.isoformat() if DOWNLOAD_FLOOD_UNTIL else None,
    }


def restore_download_runtime() -> None:
    """Rebuild a persisted FloodWait window after a service restart."""
    global DOWNLOAD_FLOOD_UNTIL, DOWNLOAD_EFFECTIVE_CONCURRENCY
    current_time = datetime.now(UTC)
    with connection() as db:
        records = db.execute("SELECT download_wait_until FROM tasks WHERE status = 'WAITING_RATE_LIMIT'").fetchall()
        waits = []
        for record in records:
            if not record["download_wait_until"]:
                continue
            try:
                wait_until = datetime.fromisoformat(record["download_wait_until"])
                waits.append(wait_until if wait_until.tzinfo else wait_until.replace(tzinfo=UTC))
            except ValueError:
                continue
        future_waits = [wait_until for wait_until in waits if wait_until > current_time]
        if future_waits:
            DOWNLOAD_FLOOD_UNTIL = max(future_waits)
            DOWNLOAD_EFFECTIVE_CONCURRENCY = 1
            log_event(logging.WARNING, "download.rate_limit_restored", "Restored persisted Telegram rate-limit pause", wait_until=DOWNLOAD_FLOOD_UNTIL.isoformat(), effective_concurrency=DOWNLOAD_EFFECTIVE_CONCURRENCY)
        else:
            resumed = db.execute("UPDATE tasks SET status = 'DOWNLOADING', download_wait_until = NULL, updated_at = ? WHERE status = 'WAITING_RATE_LIMIT'", (now(),)).rowcount
            if resumed:
                log_event(logging.INFO, "download.rate_limit_recovered", "Expired persisted rate-limit pause was cleared", task_count=resumed)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global DOWNLOAD_DISPATCHER, THUMBNAIL_WORKER, DOWNLOAD_WAKE, THUMBNAIL_WAKE
    # TestClient and development reloads may create a new event loop in the same
    # process.  These wake-up events must belong to the lifespan that owns the
    # dispatcher workers, rather than a previous loop.
    DOWNLOAD_WAKE = asyncio.Event()
    THUMBNAIL_WAKE = asyncio.Event()
    initialize_database()
    restore_download_runtime()
    log_event(logging.INFO, "service.started", "Telegram media archiver started", log_level=LOG_LEVEL, demo_mode=DEMO_MODE)
    DOWNLOAD_DISPATCHER = asyncio.create_task(download_dispatcher(), name="telegram-download-dispatcher")
    THUMBNAIL_WORKER = asyncio.create_task(thumbnail_worker(), name="archive-thumbnail-worker")
    wake_thumbnail_worker()
    if DEMO_MODE or app_state()["accountConnected"]:
        start_pending_task_workers()
    yield
    log_event(logging.INFO, "service.stopping", "Telegram media archiver is stopping", active_downloads=len(DOWNLOAD_RUNNING), task_workers=len(TASK_WORKERS))
    if DOWNLOAD_DISPATCHER:
        DOWNLOAD_DISPATCHER.cancel()
        await asyncio.gather(DOWNLOAD_DISPATCHER, return_exceptions=True)
    if THUMBNAIL_WORKER:
        THUMBNAIL_WORKER.cancel()
        await asyncio.gather(THUMBNAIL_WORKER, return_exceptions=True)
    for worker in tuple(DOWNLOAD_RUNNING.values()):
        worker.cancel()
    for worker in tuple(PREVIEW_RUNNING.values()):
        worker.cancel()
    await asyncio.gather(*tuple(DOWNLOAD_RUNNING.values()), *tuple(PREVIEW_RUNNING.values()), return_exceptions=True)
    await close_download_client()
    for worker in tuple(TASK_WORKERS.values()):
        worker.cancel()
    await asyncio.gather(*tuple(TASK_WORKERS.values()), return_exceptions=True)
    log_event(logging.INFO, "service.stopped", "Telegram media archiver stopped")


app = FastAPI(title="Telegram 媒体归档器", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "http://localhost:5173").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def structured_access_log(request: Request, call_next: Any) -> Any:
    """Log request metadata without persisting query strings or request bodies."""
    started_at = asyncio.get_running_loop().time()
    client = request.client.host if request.client else None
    try:
        response = await call_next(request)
    except Exception as error:
        log_event(
            logging.ERROR,
            "http.request_failed",
            "HTTP request failed before a response was produced",
            exc_info=True,
            method=request.method,
            path=request.url.path,
            client_address=client,
            duration_ms=round((asyncio.get_running_loop().time() - started_at) * 1000),
            error_type=type(error).__name__,
        )
        raise
    log_event(
        logging.INFO,
        "http.request_completed",
        "HTTP request completed",
        method=request.method,
        path=request.url.path,
        client_address=client,
        status_code=response.status_code,
        duration_ms=round((asyncio.get_running_loop().time() - started_at) * 1000),
    )
    return response


@app.get("/api/app-state")
def get_app_state() -> dict[str, Any]:
    return app_state()


@app.put("/api/setup")
def save_setup(payload: ApiCredentials) -> dict[str, Any]:
    with connection() as db:
        db.execute("UPDATE app_settings SET api_id = ?, api_hash = ?, updated_at = ? WHERE id = 1", (payload.api_id, payload.api_hash, now()))
    log_event(logging.INFO, "settings.api_credentials_updated", "Telegram API credentials were updated")
    return app_state()


@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    with connection() as db:
        setting = db.execute("SELECT * FROM app_settings WHERE id = 1").fetchone()
    configured = bool(setting["api_id"] and setting["api_hash"] and setting["archive_timezone"])
    connected = configured and setting["connection_status"] == "connected" and bool(setting["account_connected"])
    return {
        "apiId": masked_api_id(setting["api_id"]),
        "apiHashConfigured": bool(setting["api_hash"]),
        "archiveTimezone": setting["archive_timezone"],
        "accountConnected": connected,
        "accountName": setting["account_name"],
        "accountPhone": setting["account_phone"],
        "connectionStatus": "unconfigured" if not configured else setting["connection_status"],
        "downloadRoot": DOWNLOAD_ROOT,
        "trustedLanWarning": "当前服务未启用应用层认证。任何可访问此 HTTP 地址的人都可操作已连接账号和查看归档；仅应部署在可信局域网，绝不可暴露公网。",
        "download": download_runtime_state(),
    }


@app.put("/api/settings/api")
def update_api_credentials(payload: ApiCredentials) -> dict[str, Any]:
    return save_setup(payload)


@app.get("/api/timezones")
def list_timezones() -> dict[str, list[str]]:
    return {"timezones": sorted(AVAILABLE_TIMEZONES)}


@app.put("/api/settings/archive-timezone")
def update_archive_timezone(payload: ArchiveTimezoneSettings) -> dict[str, Any]:
    timezone_name = validated_timezone(payload.archive_timezone)
    placeholders = ", ".join("?" for _ in UNFINISHED_TASK_STATUSES)
    with connection() as db:
        db.execute("UPDATE app_settings SET archive_timezone = ?, updated_at = ? WHERE id = 1", (timezone_name, now()))
        backfilled = db.execute(
            f"UPDATE tasks SET archive_timezone = ? WHERE archive_timezone IS NULL AND status IN ({placeholders})",
            (timezone_name, *UNFINISHED_TASK_STATUSES),
        ).rowcount
    log_event(logging.INFO, "settings.archive_timezone_updated", "Archive timezone was updated", archive_timezone=timezone_name, backfilled_task_count=backfilled)
    return app_state()


@app.put("/api/settings/download-concurrency")
async def update_download_concurrency(payload: DownloadConcurrencySettings) -> dict[str, Any]:
    global DOWNLOAD_EFFECTIVE_CONCURRENCY
    with connection() as db:
        previous = db.execute("SELECT download_concurrency_max FROM app_settings WHERE id = 1").fetchone()[0]
        db.execute("UPDATE app_settings SET download_concurrency_max = ?, updated_at = ? WHERE id = 1", (payload.max_concurrency, now()))
    if payload.max_concurrency < previous:
        DOWNLOAD_EFFECTIVE_CONCURRENCY = min(DOWNLOAD_EFFECTIVE_CONCURRENCY, payload.max_concurrency)
    elif payload.max_concurrency > previous and not DOWNLOAD_FLOOD_UNTIL and DOWNLOAD_LAST_ERROR_AT is None:
        # This is a user setting change rather than an adaptive recovery.  Do not
        # make a healthy pool wait five minutes after someone raises its ceiling.
        DOWNLOAD_EFFECTIVE_CONCURRENCY = payload.max_concurrency
    cancelled = release_excess_downloads(DOWNLOAD_EFFECTIVE_CONCURRENCY)
    if cancelled:
        await asyncio.gather(*cancelled, return_exceptions=True)
    DOWNLOAD_WAKE.set()
    log_event(logging.INFO, "settings.download_concurrency_updated", "Download concurrency setting was updated", previous_max_concurrency=previous, max_concurrency=payload.max_concurrency, effective_concurrency=DOWNLOAD_EFFECTIVE_CONCURRENCY, requeued_download_count=len(cancelled))
    return download_runtime_state()


@app.post("/api/telegram/login/send-code")
async def send_login_code(payload: LoginCodeRequest) -> dict[str, Any]:
    phone = normalize_phone(payload.phone)
    get_telegram_credentials()
    attempt_id = secrets.token_urlsafe(24)

    if DEMO_MODE:
        pending_logins[attempt_id] = PendingLogin(phone=phone, phone_code_hash=None, expires_at=datetime.now(UTC) + LOGIN_ATTEMPT_TTL)
        log_event(logging.INFO, "telegram.login_code_requested", "Demo Telegram login code requested", demo_mode=True)
        return {"attemptId": attempt_id, "passwordRequired": False, "demoHint": "演示模式：可输入任意六码验证码。"}

    async with TELEGRAM_LOCK:
        client: TelegramClient | None = None
        try:
            client = await open_telegram_client()
            sent_code = await client.send_code_request(phone)
            pending_logins[attempt_id] = PendingLogin(
                phone=phone,
                phone_code_hash=sent_code.phone_code_hash,
                expires_at=datetime.now(UTC) + LOGIN_ATTEMPT_TTL,
            )
            log_event(logging.INFO, "telegram.login_code_requested", "Telegram login code requested", demo_mode=False)
        except HTTPException:
            raise
        except Exception as error:
            log_event(logging.ERROR, "telegram.login_code_failed", "Telegram login code request failed", exc_info=True, error_type=type(error).__name__)
            raise_telegram_error(error, during_authorization=True)
        finally:
            if client:
                await close_telegram_client(client)
    return {"attemptId": attempt_id, "passwordRequired": False, "demoHint": None}


@app.post("/api/telegram/login/verify-code")
async def verify_login_code(payload: LoginCodeVerify) -> dict[str, Any]:
    attempt = require_pending_login(payload.attempt_id)
    code = payload.code.strip()
    if not code:
        raise HTTPException(400, "请输入验证码")

    if DEMO_MODE:
        update_connected_account(attempt.phone, user=object())
        pending_logins.pop(payload.attempt_id, None)
        start_pending_task_workers()
        log_event(logging.INFO, "telegram.login_succeeded", "Demo Telegram login completed", demo_mode=True)
        return {**app_state(), "accountPhone": mask_phone(attempt.phone), "passwordRequired": False}

    async with TELEGRAM_LOCK:
        client: TelegramClient | None = None
        try:
            client = await open_telegram_client()
            try:
                await client.sign_in(phone=attempt.phone, code=code, phone_code_hash=attempt.phone_code_hash)
            except SessionPasswordNeededError:
                log_event(logging.INFO, "telegram.password_required", "Telegram login requires two-step verification")
                return {"passwordRequired": True, "attemptId": payload.attempt_id}
            user = await client.get_me()
            update_connected_account(attempt.phone, user)
            pending_logins.pop(payload.attempt_id, None)
            log_event(logging.INFO, "telegram.login_succeeded", "Telegram login completed", account_name=getattr(user, "username", None) or getattr(user, "first_name", None))
        except SessionPasswordNeededError:
            log_event(logging.INFO, "telegram.password_required", "Telegram login requires two-step verification")
            return {"passwordRequired": True, "attemptId": payload.attempt_id}
        except HTTPException:
            raise
        except Exception as error:
            log_event(logging.ERROR, "telegram.login_failed", "Telegram login verification failed", exc_info=True, error_type=type(error).__name__)
            raise_telegram_error(error, during_authorization=True)
        finally:
            if client:
                await close_telegram_client(client)
    start_pending_task_workers()
    return {**app_state(), "accountPhone": mask_phone(attempt.phone), "passwordRequired": False}


@app.post("/api/telegram/login/verify-password")
async def verify_login_password(payload: LoginPasswordVerify) -> dict[str, Any]:
    attempt = require_pending_login(payload.attempt_id)

    if DEMO_MODE:
        update_connected_account(attempt.phone, user=object())
        pending_logins.pop(payload.attempt_id, None)
        start_pending_task_workers()
        log_event(logging.INFO, "telegram.login_succeeded", "Demo Telegram two-step login completed", demo_mode=True)
        return {**app_state(), "accountPhone": mask_phone(attempt.phone), "passwordRequired": False}

    async with TELEGRAM_LOCK:
        client: TelegramClient | None = None
        try:
            client = await open_telegram_client()
            user = await client.sign_in(password=payload.password)
            update_connected_account(attempt.phone, user)
            pending_logins.pop(payload.attempt_id, None)
            log_event(logging.INFO, "telegram.login_succeeded", "Telegram two-step login completed", account_name=getattr(user, "username", None) or getattr(user, "first_name", None))
        except HTTPException:
            raise
        except Exception as error:
            log_event(logging.ERROR, "telegram.login_failed", "Telegram two-step login failed", exc_info=True, error_type=type(error).__name__)
            raise_telegram_error(error, during_authorization=True)
        finally:
            if client:
                await close_telegram_client(client)
    start_pending_task_workers()
    return {**app_state(), "accountPhone": mask_phone(attempt.phone), "passwordRequired": False}


@app.post("/api/telegram/logout")
async def logout() -> dict[str, Any]:
    remote_logout_warning: str | None = None
    if not DEMO_MODE and SESSION_PATH.exists():
        try:
            async with connected_telegram_client() as client:
                await client.log_out()
        except Exception as error:
            # The local session still has to be removed: the user explicitly
            # requested to remove this server's access to their account.
            remote_logout_warning = "本地会话已清除，但 Telegram 远端注销未能确认。"
            log_event(logging.ERROR, "telegram.remote_logout_failed", "Telegram remote logout could not be confirmed", exc_info=True, error_type=type(error).__name__)
    await close_download_client()
    clear_local_session()
    pending_logins.clear()
    with connection() as db:
        db.execute(
            """UPDATE app_settings SET account_connected = 0, account_name = NULL, account_phone = NULL,
               connection_status = 'disconnected', updated_at = ? WHERE id = 1""",
            (now(),),
        )
    log_event(logging.INFO, "telegram.logout_completed", "Telegram local session was cleared", remote_logout_confirmed=remote_logout_warning is None)
    return {**app_state(), "warning": remote_logout_warning}


@app.get("/api/chats")
async def list_chats() -> list[dict[str, Any]]:
    if not app_state()["accountConnected"]:
        raise HTTPException(409, "Telegram 尚未连接或登录状态已失效，请重新连接账号")
    if DEMO_MODE:
        chats = [
            {"id": "demo-tech", "title": "科技前沿观察", "handle": "@tech_frontier_obs", "type": "CHANNEL"},
            {"id": "demo-design", "title": "设计灵感库", "handle": "@design_inspiration_cn", "type": "CHANNEL"},
            {"id": "demo-study", "title": "学习资料库", "handle": "@study_materials_zh", "type": "GROUP"},
        ]
        log_event(logging.INFO, "telegram.chats_listed", "Demo Telegram chats listed", chat_count=len(chats), demo_mode=True)
        return chats

    try:
        async with connected_telegram_client() as client:
            chats: list[dict[str, Any]] = []
            async for dialog in client.iter_dialogs():
                entity = dialog.entity
                chats.append({
                    "id": str(dialog.id),
                    "title": dialog.name or "未命名聊天",
                    "handle": f"@{entity.username}" if getattr(entity, "username", None) else None,
                    "type": "CHANNEL" if dialog.is_channel else "GROUP" if dialog.is_group else "PRIVATE",
                })
            log_event(logging.INFO, "telegram.chats_listed", "Telegram chats listed", chat_count=len(chats), demo_mode=False)
            return chats
    except HTTPException:
        raise
    except Exception as error:
        log_event(logging.ERROR, "telegram.chats_list_failed", "Telegram chat listing failed", exc_info=True, error_type=type(error).__name__)
        raise_telegram_error(error)


@app.get("/api/tasks")
def list_tasks() -> list[dict[str, Any]]:
    with connection() as db:
        records = db.execute("SELECT * FROM tasks ORDER BY CASE status WHEN 'DOWNLOADING' THEN 0 WHEN 'PAUSED' THEN 1 ELSE 2 END, updated_at DESC").fetchall()
    return [{**dict(record), "filters": json.loads(record["filters_json"])} for record in records]


@app.get("/api/tasks/{task_id}")
def get_task(task_id: int) -> dict[str, Any]:
    with connection() as db:
        record = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        active_media = db.execute("SELECT * FROM task_media WHERE task_id = ? AND status = 'DOWNLOADING' ORDER BY updated_at", (task_id,)).fetchall()
    if not record:
        raise HTTPException(404, "任务不存在")
    return {
        **dict(record),
        "filters": json.loads(record["filters_json"]),
        "activeMedia": [media_payload(item) for item in active_media],
        "downloadRuntime": download_runtime_state(),
    }


@app.get("/api/tasks/{task_id}/media")
def list_task_media(task_id: int, page: int = 1, page_size: int = 50) -> dict[str, Any]:
    if page < 1 or not 1 <= page_size <= 50:
        raise HTTPException(400, "分页参数无效")
    with connection() as db:
        task = db.execute("SELECT media_revision FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            raise HTTPException(404, "任务不存在")
        total = db.execute("SELECT COUNT(*) FROM task_media WHERE task_id = ?", (task_id,)).fetchone()[0]
        records = db.execute(
            """SELECT * FROM task_media WHERE task_id = ?
               ORDER BY CASE status WHEN 'DOWNLOADING' THEN 0 WHEN 'FAILED' THEN 1 WHEN 'COMPLETED' THEN 2 ELSE 3 END,
               CASE WHEN status = 'COMPLETED' THEN updated_at END DESC, message_id ASC
               LIMIT ? OFFSET ?""",
            (task_id, page_size, (page - 1) * page_size),
        ).fetchall()
    return {"items": [media_payload(record) for record in records], "page": page, "pageSize": page_size, "total": total, "mediaRevision": task["media_revision"]}


def changed_task_media(task_id: int, after_revision: int) -> list[dict[str, Any]]:
    with connection() as db:
        records = db.execute(
            "SELECT * FROM task_media WHERE task_id = ? AND revision > ? ORDER BY revision ASC",
            (task_id, after_revision),
        ).fetchall()
    return [media_payload(record) for record in records]


def matching_media(message: Any, filters: TaskFilters) -> tuple[str, int, str | None] | None:
    if not getattr(message, "media", None):
        return None
    media_type = "PHOTO" if getattr(message, "photo", None) else "VIDEO" if getattr(message, "video", None) else "AUDIO" if getattr(message, "audio", None) else "DOCUMENT" if getattr(message, "document", None) else None
    if not media_type or media_type not in filters.media_types:
        return None
    file = getattr(message, "file", None)
    size = int(getattr(file, "size", 0) or 0)
    message_date = getattr(message, "date", None)
    if not message_date or size < filters.min_size_mb * 1_000_000 or (filters.max_size_mb and size > filters.max_size_mb * 1_000_000):
        return None
    if filters.date_start and message_date.date() < date.fromisoformat(filters.date_start):
        return None
    if filters.date_end and message_date.date() > date.fromisoformat(filters.date_end):
        return None
    return media_type, size, getattr(file, "mime_type", None)


def safe_filename(value: str) -> str:
    return (re.sub(r"[\\\\/:*?\"<>|\x00-\x1f]", "_", value).strip(". ") or "telegram-media")[:180]


def filename_for(message: Any, media_type: str) -> str:
    supplied = getattr(getattr(message, "file", None), "name", None)
    if supplied:
        return safe_filename(supplied)
    return f"message-{message.id}" + {"PHOTO": ".jpg", "VIDEO": ".mp4", "AUDIO": ".audio", "DOCUMENT": ".bin"}[media_type]


def archived_filename(filename: str, message_id: int) -> str:
    """Make the on-disk name unique while leaving the logical filename unchanged."""
    sanitized = safe_filename(filename)
    suffix = Path(sanitized).suffix
    stem = sanitized[:-len(suffix)] if suffix else sanitized
    return f"{stem}__msg-{message_id}{suffix}"


def archive_destination(task: Any, media: Any) -> Path:
    """Build the stable on-disk location for a downloaded Telegram message."""
    timezone_name = task["archive_timezone"]
    if not timezone_name:
        raise RuntimeError("任务缺少归档时区，无法开始下载")
    message_date = datetime.fromisoformat(str(media["message_date"]))
    if message_date.tzinfo is None:
        message_date = message_date.replace(tzinfo=UTC)
    archive_date = message_date.astimezone(ZoneInfo(timezone_name))
    chat_directory = f"{safe_filename(str(task['chat_title']))}__chat-{task['chat_id']}"
    return (
        Path(DOWNLOAD_ROOT)
        / chat_directory
        / f"{archive_date:%Y}"
        / f"{archive_date:%m}"
        / archived_filename(str(media["filename"]), int(media["message_id"]))
    )


def scan_cache_key(payload: ScanRequest) -> str:
    return json.dumps(payload.model_dump(), ensure_ascii=False, sort_keys=True)


def take_cached_scan(payload: ScanRequest) -> list[tuple[int, str, str, str | None, int, str]] | None:
    current_time = datetime.now(UTC)
    for key, (expires_at, _) in list(SCAN_CACHE.items()):
        if expires_at <= current_time:
            SCAN_CACHE.pop(key, None)
    cached = SCAN_CACHE.pop(scan_cache_key(payload), None)
    return cached[1] if cached and cached[0] > current_time else None


async def scan_messages(payload: ScanRequest) -> list[tuple[int, str, str, str | None, int, str]]:
    started_at = asyncio.get_running_loop().time()
    log_event(logging.INFO, "scan.started", "Telegram media scan started", chat_id=payload.chat_id, chat_title=payload.chat_title, filters=payload.filters.model_dump(), demo_mode=DEMO_MODE)
    if DEMO_MODE:
        seed = sum(ord(char) for char in payload.chat_id)
        count, size = 160 + seed % 870, 4_000_000 + seed % 10_000_000
        result = [(index, f"demo-{index}.bin", "DOCUMENT", "application/octet-stream", size, now()) for index in range(count)]
        log_event(logging.INFO, "scan.completed", "Demo Telegram media scan completed", chat_id=payload.chat_id, chat_title=payload.chat_title, matched_count=len(result), total_bytes=sum(item[4] for item in result), duration_ms=round((asyncio.get_running_loop().time() - started_at) * 1000))
        return result
    if not app_state()["accountConnected"]:
        raise HTTPException(409, "Telegram 尚未连接或登录状态已失效，请重新连接账号")
    try:
        start_date = date.fromisoformat(payload.filters.date_start) if payload.filters.date_start else None
        end_date = date.fromisoformat(payload.filters.date_end) if payload.filters.date_end else None
    except ValueError as error:
        raise HTTPException(400, "日期格式无效") from error
    result: list[tuple[int, str, str, str | None, int, str]] = []
    try:
        async with connected_telegram_client() as client:
            entity = await client.get_entity(int(payload.chat_id))
            # Telegram returns newest messages first by default.  Starting at the
            # selected end date and stopping at the start date avoids traversing a
            # channel's entire history for a narrow date range.
            end_offset = datetime.combine(end_date + timedelta(days=1), datetime.min.time(), tzinfo=UTC) if end_date else None
            async for message in client.iter_messages(entity, offset_date=end_offset):
                if start_date and message.date.date() < start_date:
                    break
                match = matching_media(message, payload.filters)
                if match:
                    media_type, size, mime_type = match
                    result.append((message.id, filename_for(message, media_type), media_type, mime_type, size, message.date.isoformat()))
    except HTTPException:
        raise
    except Exception as error:
        log_event(logging.ERROR, "scan.failed", "Telegram media scan failed", exc_info=True, chat_id=payload.chat_id, chat_title=payload.chat_title, error_type=type(error).__name__)
        raise_telegram_error(error)
    log_event(logging.INFO, "scan.completed", "Telegram media scan completed", chat_id=payload.chat_id, chat_title=payload.chat_title, matched_count=len(result), total_bytes=sum(item[4] for item in result), duration_ms=round((asyncio.get_running_loop().time() - started_at) * 1000))
    return result


def source_cursor(message_id: int) -> str:
    return base64.urlsafe_b64encode(str(message_id).encode()).decode().rstrip("=")


def parse_source_cursor(value: str | None) -> int | None:
    if not value:
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        result = int(base64.urlsafe_b64decode(padded).decode())
    except (ValueError, UnicodeDecodeError, base64.binascii.Error) as error:
        raise HTTPException(400, "媒体分页游标无效") from error
    if result <= 0:
        raise HTTPException(400, "媒体分页游标无效")
    return result


def source_item_states(chat_id: str, message_ids: list[int]) -> tuple[dict[int, int], set[int], dict[int, sqlite3.Row]]:
    if not message_ids:
        return {}, set(), {}
    placeholders = ", ".join("?" for _ in message_ids)
    with connection() as db:
        archived = db.execute(
            f"SELECT message_id, id FROM archive_items WHERE chat_id = ? AND message_id IN ({placeholders})",
            (chat_id, *message_ids),
        ).fetchall()
        queued = db.execute(
            f"""SELECT DISTINCT m.message_id FROM task_media m JOIN tasks t ON t.id = m.task_id
                WHERE t.chat_id = ? AND m.message_id IN ({placeholders})
                AND t.status IN ('QUEUED', 'SCANNING', 'DOWNLOADING', 'RETRYING', 'WAITING_RATE_LIMIT', 'PAUSED')""",
            (chat_id, *message_ids),
        ).fetchall()
        previews = db.execute(
            f"SELECT * FROM preview_cache WHERE chat_id = ? AND message_id IN ({placeholders})",
            (chat_id, *message_ids),
        ).fetchall()
    return ({record["message_id"]: record["id"] for record in archived}, {record["message_id"] for record in queued}, {record["message_id"]: record for record in previews})


def preview_payload(record: sqlite3.Row) -> dict[str, Any]:
    payload = dict(record)
    ready = record["status"] == "READY" and path_in_root(record["cache_path"], PREVIEW_ROOT)
    payload["content_url"] = f"/api/previews/{record['id']}/content" if ready else None
    return payload


def source_media_payload(item: tuple[int, str, str, str | None, int, str], archive_ids: dict[int, int], queued_ids: set[int], previews: dict[int, sqlite3.Row]) -> dict[str, Any]:
    message_id, filename, media_type, mime_type, size_bytes, message_date = item
    preview = previews.get(message_id)
    preview_data = preview_payload(preview) if preview else None
    return {
        "message_id": message_id,
        "filename": filename,
        "media_type": media_type,
        "mime_type": mime_type,
        "size_bytes": size_bytes,
        "message_date": message_date,
        "archived": message_id in archive_ids,
        "archive_id": archive_ids.get(message_id),
        "queued": message_id in queued_ids,
        "thumbnail_status": "READY" if preview_data and preview_data["content_url"] and media_type in {"PHOTO", "VIDEO"} else (preview["status"] if preview else "UNAVAILABLE"),
        "thumbnail_url": preview_data["content_url"] if preview_data and media_type in {"PHOTO", "VIDEO"} else None,
        "preview": preview_data,
    }


def demo_source_media(chat_id: str, offset: int | None, media_types: set[str]) -> list[tuple[int, str, str, str | None, int, str]]:
    upper = offset - 1 if offset else 240
    result = []
    for message_id in range(upper, max(0, upper - 120), -1):
        media_type = ("PHOTO", "VIDEO", "DOCUMENT", "PHOTO")[message_id % 4]
        if media_type not in media_types:
            continue
        extension = {"PHOTO": "jpg", "VIDEO": "mp4", "DOCUMENT": "pdf"}[media_type]
        result.append((message_id, f"demo-{message_id}.{extension}", media_type, {"PHOTO": "image/jpeg", "VIDEO": "video/mp4", "DOCUMENT": "application/pdf"}[media_type], (message_id % 17 + 1) * 1_000_000, (datetime.now(UTC) - timedelta(hours=message_id)).isoformat()))
    return result


@app.get("/api/sources/{chat_id}/media")
async def browse_source_media(chat_id: str, cursor: str | None = None, media_type: str | None = None, date_start: str | None = None, date_end: str | None = None, page_size: int = 30) -> dict[str, Any]:
    if not 1 <= page_size <= 50:
        raise HTTPException(400, "每页媒体数量必须在 1 到 50 之间")
    allowed_types = {"PHOTO", "VIDEO", "AUDIO", "DOCUMENT"}
    requested_types = {media_type} if media_type else allowed_types
    if not requested_types <= allowed_types:
        raise HTTPException(400, "媒体类型无效")
    try:
        if date_start:
            date.fromisoformat(date_start)
        if date_end:
            date.fromisoformat(date_end)
        filters = TaskFilters(media_types=list(requested_types), date_start=date_start, date_end=date_end)
        offset = parse_source_cursor(cursor)
    except ValueError as error:
        raise HTTPException(400, "日期格式无效") from error
    matched: list[tuple[int, str, str, str | None, int, str]] = []
    if DEMO_MODE:
        matched = demo_source_media(chat_id, offset, requested_types)
    else:
        if not app_state()["accountConnected"]:
            raise HTTPException(409, "Telegram 尚未连接或登录状态已失效，请重新连接账号")
        try:
            async with connected_telegram_client() as client:
                entity = await client.get_entity(int(chat_id))
                async for message in client.iter_messages(entity, offset_id=offset or 0):
                    match = matching_media(message, filters)
                    if match:
                        kind, size, mime_type = match
                        matched.append((message.id, filename_for(message, kind), kind, mime_type, size, message.date.isoformat()))
                    if len(matched) >= page_size + 1:
                        break
        except HTTPException:
            raise
        except Exception as error:
            log_event(logging.ERROR, "source_media.list_failed", "Unable to list source media", exc_info=True, chat_id=chat_id, error_type=type(error).__name__)
            raise_telegram_error(error)
    visible, overflow = matched[:page_size], matched[page_size:]
    archive_ids, queued_ids, previews = source_item_states(chat_id, [item[0] for item in visible])
    return {
        "items": [source_media_payload(item, archive_ids, queued_ids, previews) for item in visible],
        "next_cursor": source_cursor(visible[-1][0]) if overflow and visible else None,
    }


@app.post("/api/tasks/scan")
async def scan_task(payload: ScanRequest) -> dict[str, Any]:
    matched = await scan_messages(payload)
    SCAN_CACHE[scan_cache_key(payload)] = (datetime.now(UTC) + SCAN_CACHE_TTL, matched)
    log_event(logging.INFO, "scan.cached", "Scan result cached for task creation", chat_id=payload.chat_id, chat_title=payload.chat_title, matched_count=len(matched), cache_ttl_seconds=int(SCAN_CACHE_TTL.total_seconds()))
    return {"chat": payload.model_dump(exclude={"filters"}), "filters": payload.filters.model_dump(), "totalCount": len(matched), "totalBytes": sum(item[4] for item in matched), "duplicateCount": 0}


def update_task(task_id: int, **values: Any) -> None:
    values["updated_at"] = now()
    assignments = ", ".join(f"{key} = ?" for key in values)
    with connection() as db:
        previous = db.execute("SELECT status, chat_id, chat_title FROM tasks WHERE id = ?", (task_id,)).fetchone()
        updated = db.execute(f"UPDATE tasks SET {assignments} WHERE id = ?", (*values.values(), task_id)).rowcount
    if updated and previous and "status" in values and values["status"] != previous["status"]:
        log_event(logging.INFO, "task.state_changed", "Task state changed", task_id=task_id, chat_id=previous["chat_id"], chat_title=previous["chat_title"], previous_status=previous["status"], status=values["status"])


def media_payload(record: sqlite3.Row) -> dict[str, Any]:
    item = dict(record)
    downloaded = item["size_bytes"] if item["status"] == "COMPLETED" else min(item["downloaded_bytes"], item["size_bytes"])
    return {
        **item,
        "downloaded_bytes": downloaded,
        "percent": min(100, int(downloaded / item["size_bytes"] * 100)) if item["size_bytes"] else 0,
    }


def update_media(task_id: int, media_id: int, **values: Any) -> int:
    """Persist one file's state and assign a task-local, monotonic revision."""
    timestamp = now()
    with connection() as db:
        task = db.execute("SELECT media_revision FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            raise HTTPException(404, "任务不存在")
        media = db.execute("SELECT status, filename, message_id, media_type, size_bytes FROM task_media WHERE id = ? AND task_id = ?", (media_id, task_id)).fetchone()
        revision = task["media_revision"] + 1
        values.update(updated_at=timestamp, revision=revision)
        assignments = ", ".join(f"{key} = ?" for key in values)
        updated = db.execute(f"UPDATE task_media SET {assignments} WHERE id = ? AND task_id = ?", (*values.values(), media_id, task_id)).rowcount
        db.execute("UPDATE tasks SET media_revision = ?, updated_at = ? WHERE id = ?", (revision, timestamp, task_id))
    if updated and media and "status" in values and values["status"] != media["status"]:
        log_event(logging.INFO, "media.state_changed", "Task media state changed", task_id=task_id, media_id=media_id, message_id=media["message_id"], filename=media["filename"], media_type=media["media_type"], size_bytes=media["size_bytes"], previous_status=media["status"], status=values["status"], failure_category=values.get("failure_category"), retry_at=values.get("next_retry_at"))
    return revision


def update_download_progress(task_id: int, media_id: int, task_downloaded_bytes: int, file_downloaded_bytes: int, speed_bytes_per_second: int) -> None:
    """Update aggregate and current-file progress in one SQLite transaction."""
    timestamp = now()
    with connection() as db:
        task = db.execute("SELECT total_bytes, media_revision FROM tasks WHERE id = ?", (task_id,)).fetchone()
        media = db.execute("SELECT size_bytes FROM task_media WHERE id = ? AND task_id = ?", (media_id, task_id)).fetchone()
        if not task or not media:
            return
        revision = task["media_revision"] + 1
        file_bytes = min(max(0, file_downloaded_bytes), media["size_bytes"])
        db.execute(
            """UPDATE task_media SET status = 'DOWNLOADING', downloaded_bytes = ?, speed_bytes_per_second = ?,
               updated_at = ?, revision = ? WHERE id = ? AND task_id = ?""",
            (file_bytes, speed_bytes_per_second, timestamp, revision, media_id, task_id),
        )
        db.execute(
            """UPDATE tasks SET downloaded_bytes = ?, speed_bytes_per_second = ?, media_revision = ?, updated_at = ?
               WHERE id = ?""",
            (min(task_downloaded_bytes, task["total_bytes"]), speed_bytes_per_second, revision, timestamp, task_id),
        )


def update_parallel_download_progress(task_id: int, media_id: int, file_downloaded_bytes: int, speed_bytes_per_second: int) -> None:
    """Recalculate aggregate task progress so concurrent files cannot overwrite it."""
    timestamp = now()
    with connection() as db:
        task = db.execute("SELECT total_bytes, media_revision FROM tasks WHERE id = ?", (task_id,)).fetchone()
        media = db.execute("SELECT size_bytes FROM task_media WHERE id = ? AND task_id = ?", (media_id, task_id)).fetchone()
        if not task or not media:
            return
        revision = task["media_revision"] + 1
        file_bytes = min(max(0, file_downloaded_bytes), media["size_bytes"])
        db.execute("""UPDATE task_media SET status = 'DOWNLOADING', downloaded_bytes = ?, speed_bytes_per_second = ?,
                   updated_at = ?, revision = ? WHERE id = ? AND task_id = ?""",
                   (file_bytes, speed_bytes_per_second, timestamp, revision, media_id, task_id))
        totals = db.execute("""SELECT COALESCE(SUM(downloaded_bytes), 0) AS bytes,
                   COALESCE(SUM(speed_bytes_per_second), 0) AS speed FROM task_media WHERE task_id = ?""", (task_id,)).fetchone()
        db.execute("""UPDATE tasks SET downloaded_bytes = ?, speed_bytes_per_second = ?, media_revision = ?, updated_at = ?
                   WHERE id = ?""", (min(totals["bytes"], task["total_bytes"]), totals["speed"], revision, timestamp, task_id))


def configured_download_concurrency() -> int:
    with connection() as db:
        return db.execute("SELECT download_concurrency_max FROM app_settings WHERE id = 1").fetchone()[0]


def notify_download_dispatcher() -> None:
    DOWNLOAD_WAKE.set()


async def get_download_client() -> TelegramClient:
    """Return the sole long-lived client that owns the on-disk Telethon session."""
    global DOWNLOAD_CLIENT
    async with DOWNLOAD_CLIENT_LOCK:
        if DOWNLOAD_CLIENT and DOWNLOAD_CLIENT.is_connected():
            log_event(logging.DEBUG, "download.client_reused", "Reusing connected Telegram download client")
            return DOWNLOAD_CLIENT
        DOWNLOAD_CLIENT = await open_telegram_client()
        if not await DOWNLOAD_CLIENT.is_user_authorized():
            await close_telegram_client(DOWNLOAD_CLIENT)
            DOWNLOAD_CLIENT = None
            mark_session_invalid()
            raise RuntimeError("Telegram 登录状态已失效")
        log_event(logging.INFO, "download.client_opened", "Telegram download client opened")
        return DOWNLOAD_CLIENT


@asynccontextmanager
async def connected_telegram_client():
    """Lease the shared authenticated client for a non-download Telegram operation.

    Telethon's SQLiteSession keeps a live SQLite connection and writes session
    state during reconnects.  Holding this lock for the whole operation prevents
    a client reset from closing that one client mid-request, and—more
    importantly—prevents the old per-request clients from opening the same
    session file alongside it.
    """
    async with TELEGRAM_LOCK:
        client = await get_download_client()
        if not await client.is_user_authorized():
            mark_session_invalid()
            raise HTTPException(409, "Telegram 登录状态已失效，请重新连接账号")
        yield client


async def close_download_client() -> None:
    global DOWNLOAD_CLIENT
    # Keep this order aligned with connected_telegram_client so a reset cannot
    # close the shared session while a browse/scan/request lease is in flight.
    async with TELEGRAM_LOCK:
        async with DOWNLOAD_CLIENT_LOCK:
            if DOWNLOAD_CLIENT:
                await close_telegram_client(DOWNLOAD_CLIENT)
                DOWNLOAD_CLIENT = None
                log_event(logging.INFO, "download.client_closed", "Telegram download client closed")


def request_download_client_reset(task_id: int, media_id: int) -> None:
    """Stop dispatching new work until the shared client can be safely replaced."""
    global DOWNLOAD_CLIENT_RESET_REQUESTED
    if DOWNLOAD_CLIENT_RESET_REQUESTED:
        return
    DOWNLOAD_CLIENT_RESET_REQUESTED = True
    log_event(
        logging.WARNING,
        "download.client_reset_requested",
        "Telegram download client will be rebuilt after active downloads finish",
        task_id=task_id,
        media_id=media_id,
        active_download_count=len(DOWNLOAD_RUNNING) + len(PREVIEW_RUNNING),
    )
    notify_download_dispatcher()


async def reset_download_client_if_requested() -> None:
    """Close the shared client only after no worker can still be using it."""
    global DOWNLOAD_CLIENT, DOWNLOAD_CLIENT_RESET_REQUESTED
    if not DOWNLOAD_CLIENT_RESET_REQUESTED or DOWNLOAD_RUNNING or PREVIEW_RUNNING:
        return
    try:
        await close_download_client()
        log_event(logging.INFO, "download.client_reset_completed", "Telegram download client reset completed")
    except Exception as error:
        # A failed disconnect must not block the next retry from constructing a
        # fresh client. close_download_client clears the reference only after
        # its disconnect call, so clear it explicitly on this recovery path.
        async with DOWNLOAD_CLIENT_LOCK:
            DOWNLOAD_CLIENT = None
        log_event(logging.WARNING, "download.client_reset_failed", "Telegram download client reset failed; a new client will be opened for retry", exc_info=True, error_type=type(error).__name__)
    finally:
        DOWNLOAD_CLIENT_RESET_REQUESTED = False
        notify_download_dispatcher()


def claim_next_media(task_id: int) -> sqlite3.Row | None:
    timestamp = now()
    with connection() as db:
        task = db.execute("SELECT media_revision, chat_id, chat_title FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            return None
        media = db.execute("""SELECT * FROM task_media WHERE task_id = ? AND
            (status = 'PENDING' OR (status = 'RETRY_WAIT' AND (next_retry_at IS NULL OR next_retry_at <= ?)))
            ORDER BY CASE status WHEN 'PENDING' THEN 0 ELSE 1 END, message_id LIMIT 1""", (task_id, timestamp)).fetchone()
        if not media:
            return None
        revision = task["media_revision"] + 1
        claimed = db.execute("""UPDATE task_media SET status = 'DOWNLOADING', speed_bytes_per_second = 0,
            next_retry_at = NULL, updated_at = ?, revision = ? WHERE id = ? AND task_id = ? AND status = ?""",
            (timestamp, revision, media["id"], task_id, media["status"])).rowcount
        if not claimed:
            return None
        db.execute("UPDATE tasks SET status = 'DOWNLOADING', media_revision = ?, download_wait_until = NULL, updated_at = ? WHERE id = ?", (revision, timestamp, task_id))
        claimed_media = db.execute("SELECT * FROM task_media WHERE id = ?", (media["id"],)).fetchone()
    if claimed_media:
        log_event(logging.INFO, "media.claimed", "Media claimed by download dispatcher", task_id=task_id, media_id=claimed_media["id"], chat_id=task["chat_id"], chat_title=task["chat_title"], message_id=claimed_media["message_id"], filename=claimed_media["filename"], media_type=claimed_media["media_type"], size_bytes=claimed_media["size_bytes"], previous_status=media["status"], status="DOWNLOADING")
    return claimed_media


def eligible_task_ids() -> list[int]:
    timestamp = now()
    with connection() as db:
        records = db.execute("""SELECT DISTINCT t.id FROM tasks t JOIN task_media m ON m.task_id = t.id
            WHERE t.status IN ('DOWNLOADING', 'RETRYING') AND
            (m.status = 'PENDING' OR (m.status = 'RETRY_WAIT' AND (m.next_retry_at IS NULL OR m.next_retry_at <= ?)))
            ORDER BY t.id""", (timestamp,)).fetchall()
    return [record["id"] for record in records]


def finalize_task_status(task_id: int) -> None:
    with connection() as db:
        counts = {record["status"]: record["count"] for record in db.execute(
            "SELECT status, COUNT(*) AS count FROM task_media WHERE task_id = ? GROUP BY status", (task_id,)).fetchall()}
        completed = counts.get("COMPLETED", 0)
        failed = counts.get("FAILED", 0)
        active = counts.get("DOWNLOADING", 0)
        pending = counts.get("PENDING", 0)
        retrying = counts.get("RETRY_WAIT", 0)
        task = db.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not task or task["status"] in {"PAUSED", "CANCELLED", "FAILED", "WAITING_RATE_LIMIT"}:
        return
    if active or pending:
        update_task(task_id, status="DOWNLOADING", completed_count=completed, failed_count=failed)
    elif retrying:
        update_task(task_id, status="RETRYING", completed_count=completed, failed_count=failed, speed_bytes_per_second=0)
    else:
        update_task(task_id, status="PARTIAL_FAILED" if failed else "COMPLETED", completed_count=completed, failed_count=failed, current_file=None, speed_bytes_per_second=0)


def classify_download_error(error: Exception) -> tuple[str, bool, str]:
    name = type(error).__name__
    if name in {"AuthKeyUnregisteredError", "SessionRevokedError", "UserDeactivatedError", "UserDeactivatedBanError"}:
        return "AUTH", False, "Telegram 登录已失效，请重新连接账号后重试"
    if name in {"FileReferenceExpiredError", "FileReferenceInvalidError", "FileIdInvalidError", "LocationInvalidError", "MessageIdInvalidError", "ChannelPrivateError", "ChannelInvalidError"} or "消息已不可用" in str(error):
        return "UNAVAILABLE", False, "文件不可用、已删除或当前账号无访问权限"
    if isinstance(error, (RpcCallFailError, TimedOutError)):
        return "TELEGRAM_SERVER", True, "Telegram 服务暂时无法处理下载请求，将自动重试"
    if isinstance(error, (OSError, asyncio.TimeoutError)) or isinstance(error, RPCError):
        return "NETWORK", True, "网络或 Telegram 服务暂时不可用，将自动重试"
    return "UNKNOWN", True, "下载发生临时错误，将自动重试"


def register_recoverable_error() -> None:
    global DOWNLOAD_EFFECTIVE_CONCURRENCY, DOWNLOAD_LAST_ERROR_AT
    current = asyncio.get_running_loop().time()
    DOWNLOAD_ERROR_TIMES.append(current)
    while DOWNLOAD_ERROR_TIMES and DOWNLOAD_ERROR_TIMES[0] < current - 60:
        DOWNLOAD_ERROR_TIMES.popleft()
    previous = DOWNLOAD_EFFECTIVE_CONCURRENCY
    if len(DOWNLOAD_ERROR_TIMES) >= 2:
        DOWNLOAD_EFFECTIVE_CONCURRENCY = max(1, DOWNLOAD_EFFECTIVE_CONCURRENCY - 1)
        DOWNLOAD_ERROR_TIMES.clear()
    if DOWNLOAD_EFFECTIVE_CONCURRENCY != previous:
        log_event(logging.WARNING, "download.concurrency_reduced", "Download concurrency reduced after repeated recoverable errors", previous_effective_concurrency=previous, effective_concurrency=DOWNLOAD_EFFECTIVE_CONCURRENCY)
    DOWNLOAD_LAST_ERROR_AT = current


def register_flood_wait(seconds: int) -> None:
    global DOWNLOAD_EFFECTIVE_CONCURRENCY, DOWNLOAD_FLOOD_UNTIL, DOWNLOAD_LAST_ERROR_AT
    wait_until = datetime.now(UTC) + timedelta(seconds=max(1, seconds))
    DOWNLOAD_EFFECTIVE_CONCURRENCY = 1
    DOWNLOAD_FLOOD_UNTIL = max(DOWNLOAD_FLOOD_UNTIL, wait_until) if DOWNLOAD_FLOOD_UNTIL else wait_until
    DOWNLOAD_LAST_ERROR_AT = asyncio.get_running_loop().time()
    with connection() as db:
        affected = db.execute("UPDATE tasks SET status = 'WAITING_RATE_LIMIT', download_wait_until = ?, speed_bytes_per_second = 0, updated_at = ? WHERE status IN ('DOWNLOADING', 'RETRYING')", (DOWNLOAD_FLOOD_UNTIL.isoformat(), now())).rowcount
    log_event(logging.WARNING, "download.rate_limited", "Telegram rate limit paused all downloads", wait_seconds=seconds, wait_until=DOWNLOAD_FLOOD_UNTIL.isoformat(), task_count=affected, effective_concurrency=DOWNLOAD_EFFECTIVE_CONCURRENCY)


def cancel_task_downloads(task_id: int) -> None:
    """Interrupt active transfers for an explicit pause/cancel action."""
    cancelled = 0
    for (running_task_id, _), worker in tuple(DOWNLOAD_RUNNING.items()):
        if running_task_id == task_id:
            worker.cancel()
            cancelled += 1
    if cancelled:
        log_event(logging.INFO, "download.cancelled_for_task", "Active downloads cancelled for task action", task_id=task_id, cancelled_download_count=cancelled)


def release_excess_downloads(limit: int) -> list[asyncio.Task[None]]:
    """Immediately honor a lower user-selected global concurrency limit."""
    workers: list[asyncio.Task[None]] = []
    for key, worker in tuple(DOWNLOAD_RUNNING.items())[max(0, limit):]:
        DOWNLOAD_REQUEUED_CANCELS.add(key)
        worker.cancel()
        workers.append(worker)
    return workers


def stop_all_downloads_for_auth_failure(message: str) -> None:
    """A revoked shared session must prevent every task from taking new work."""
    with connection() as db:
        db.execute("""UPDATE tasks SET status = 'FAILED', current_file = NULL, speed_bytes_per_second = 0,
                   error_message = ?, updated_at = ? WHERE status IN ('DOWNLOADING', 'RETRYING', 'WAITING_RATE_LIMIT')""", (message, now()))
    for worker in tuple(DOWNLOAD_RUNNING.values()):
        worker.cancel()
    for worker in tuple(PREVIEW_RUNNING.values()):
        worker.cancel()
    log_event(logging.ERROR, "download.stopped_for_auth_failure", "All downloads stopped because Telegram authorization failed", active_download_count=len(DOWNLOAD_RUNNING))


def preview_part_path(record: sqlite3.Row) -> Path | None:
    cached = path_in_root(record["cache_path"], PREVIEW_ROOT)
    return cached.with_suffix(cached.suffix + ".part") if cached else None


def cleanup_preview_cache() -> None:
    """Remove expired preview data that has not been adopted by a download task."""
    try:
        with connection() as db:
            records = db.execute(
                """SELECT p.* FROM preview_cache p WHERE p.expires_at <= ?
                   AND NOT EXISTS (SELECT 1 FROM task_media m WHERE m.preview_cache_id = p.id)""",
                (now(),),
            ).fetchall()
            db.executemany("DELETE FROM preview_cache WHERE id = ?", [(record["id"],) for record in records])
        for record in records:
            cached = path_in_root(record["cache_path"], PREVIEW_ROOT)
            part = preview_part_path(record)
            if cached:
                cached.unlink(missing_ok=True)
            if part:
                part.unlink(missing_ok=True)
        if records:
            log_event(logging.INFO, "preview.cache_cleaned", "Expired preview cache removed", cache_count=len(records))
    except (OSError, sqlite3.Error) as error:
        log_event(logging.WARNING, "preview.cache_cleanup_failed", "Unable to clean expired preview cache", exc_info=True, error_type=type(error).__name__)


def update_preview_cache(cache_id: int, **values: Any) -> None:
    values["updated_at"] = now()
    assignments = ", ".join(f"{key} = ?" for key in values)
    with connection() as db:
        db.execute(f"UPDATE preview_cache SET {assignments} WHERE id = ?", (*values.values(), cache_id))


def claim_next_preview() -> sqlite3.Row | None:
    with connection() as db:
        record = db.execute("SELECT * FROM preview_cache WHERE status = 'PENDING' ORDER BY updated_at LIMIT 1").fetchone()
        if not record:
            return None
        claimed = db.execute("UPDATE preview_cache SET status = 'DOWNLOADING', error_message = NULL, updated_at = ? WHERE id = ? AND status = 'PENDING'", (now(), record["id"])).rowcount
        return db.execute("SELECT * FROM preview_cache WHERE id = ?", (record["id"],)).fetchone() if claimed else None


async def preview_download_job(cache_id: int) -> None:
    try:
        with connection() as db:
            record = db.execute("SELECT * FROM preview_cache WHERE id = ?", (cache_id,)).fetchone()
        if not record or record["status"] != "DOWNLOADING":
            return
        cached = path_in_root(record["cache_path"], PREVIEW_ROOT)
        part = preview_part_path(record)
        if not cached or not part:
            raise RuntimeError("预览缓存路径无效")
        cached.parent.mkdir(parents=True, exist_ok=True)
        if DEMO_MODE:
            remaining = max(0, record["size_bytes"] - record["downloaded_bytes"])
            with part.open("ab") as output:
                output.truncate(record["downloaded_bytes"] + remaining)
            os.replace(part, cached)
        else:
            client = await get_download_client()
            async with TELEGRAM_LOCK:
                entity = await client.get_entity(int(record["chat_id"]))
                message = await client.get_messages(entity, ids=record["message_id"])
            if not message:
                raise RuntimeError("消息已不可用")
            started = asyncio.get_running_loop().time()
            last_persisted = 0.0
            base_bytes = part.stat().st_size if part.is_file() else 0

            def progress(current: int, total: int) -> None:
                nonlocal last_persisted
                current_time = asyncio.get_running_loop().time()
                if current != total and current_time - last_persisted < 1:
                    return
                last_persisted = current_time
                update_preview_cache(cache_id, downloaded_bytes=min(record["size_bytes"], base_bytes + current))

            result = await client.download_media(message, file=str(part), progress_callback=progress)
            if not result:
                raise RuntimeError("Telegram 未返回预览文件")
            os.replace(part, cached)
        update_preview_cache(cache_id, status="READY", downloaded_bytes=record["size_bytes"], error_message=None, expires_at=(datetime.now(UTC) + PREVIEW_CACHE_TTL).isoformat())
        log_event(logging.INFO, "preview.ready", "Source media preview ready", preview_id=cache_id, chat_id=record["chat_id"], message_id=record["message_id"], size_bytes=record["size_bytes"])
    except asyncio.CancelledError:
        with connection() as db:
            record = db.execute("SELECT * FROM preview_cache WHERE id = ?", (cache_id,)).fetchone()
        part = preview_part_path(record) if record else None
        update_preview_cache(cache_id, status="PENDING", downloaded_bytes=part.stat().st_size if part and part.is_file() else 0)
        raise
    except FloodWaitError as error:
        register_flood_wait(max(1, int(getattr(error, "seconds", 1))))
        update_preview_cache(cache_id, status="PENDING", error_message="Telegram 限流，预览将在等待结束后继续")
    except Exception as error:
        category, recoverable, message = classify_download_error(error)
        update_preview_cache(cache_id, status="FAILED", error_message=message)
        log_event(logging.WARNING, "preview.failed", "Source media preview failed", exc_info=True, preview_id=cache_id, error_type=type(error).__name__, failure_category=category)
    finally:
        PREVIEW_RUNNING.pop(cache_id, None)
        await reset_download_client_if_requested()
        notify_download_dispatcher()


async def download_media_job(task_id: int, media_id: int) -> None:
    try:
        with connection() as db:
            task = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            media = db.execute("SELECT * FROM task_media WHERE id = ? AND task_id = ?", (media_id, task_id)).fetchone()
        if not task or not media or current_task_status(task_id) not in {"DOWNLOADING", "RETRYING"}:
            return
        log_event(logging.INFO, "download.started", "Telegram media download started", task_id=task_id, media_id=media_id, chat_id=task["chat_id"], chat_title=task["chat_title"], message_id=media["message_id"], filename=media["filename"], media_type=media["media_type"], size_bytes=media["size_bytes"])
        destination = archive_destination(task, media)
        destination.parent.mkdir(parents=True, exist_ok=True)
        part = destination.with_suffix(destination.suffix + ".part")
        if media["preview_cache_id"]:
            with connection() as db:
                preview = db.execute("SELECT * FROM preview_cache WHERE id = ?", (media["preview_cache_id"],)).fetchone()
            preview_file = path_in_root(preview["cache_path"], PREVIEW_ROOT) if preview else None
            preview_part = preview_part_path(preview) if preview else None
            if preview and preview["status"] == "READY" and preview_file and preview_file.is_file():
                os.replace(preview_file, destination)
                update_preview_cache(preview["id"], status="CONSUMED", expires_at=now())
                with connection() as db:
                    db.execute("UPDATE task_media SET preview_cache_id = NULL WHERE id = ?", (media_id,))
                record_archive(task, media, destination)
                update_media(task_id, media_id, status="COMPLETED", downloaded_bytes=media["size_bytes"], speed_bytes_per_second=0, error_message=None, failure_category=None)
                log_event(logging.INFO, "download.preview_adopted", "Completed preview adopted without another Telegram download", task_id=task_id, media_id=media_id, preview_id=preview["id"])
                return
            if preview_part and preview_part.is_file() and not part.exists():
                os.replace(preview_part, part)
                update_preview_cache(preview["id"], status="CONSUMED", expires_at=now())
                with connection() as db:
                    db.execute("UPDATE task_media SET preview_cache_id = NULL WHERE id = ?", (media_id,))
        client = await get_download_client()
        async with TELEGRAM_LOCK:
            entity = await client.get_entity(int(task["chat_id"]))
            message = await client.get_messages(entity, ids=media["message_id"])
        if not message:
            raise RuntimeError("消息已不可用")
        started_at = asyncio.get_running_loop().time()
        last_persisted_at = 0.0

        def progress(current: int, total: int) -> None:
            nonlocal last_persisted_at
            current_time = asyncio.get_running_loop().time()
            if current != total and current_time - last_persisted_at < 1:
                return
            last_persisted_at = current_time
            update_parallel_download_progress(task_id, media_id, current, round(current / max(current_time - started_at, 0.001)))

        result = await client.download_media(message, file=str(part), progress_callback=progress)
        if not result:
            raise RuntimeError("Telegram 未返回下载文件")
        os.replace(part, destination)
        record_archive(task, media, destination)
        update_media(task_id, media_id, status="COMPLETED", downloaded_bytes=media["size_bytes"], speed_bytes_per_second=0, error_message=None, failure_category=None)
        log_event(logging.INFO, "download.completed", "Telegram media download completed", task_id=task_id, media_id=media_id, filename=media["filename"], archive_path=str(destination), size_bytes=media["size_bytes"])
    except asyncio.CancelledError:
        status = current_task_status(task_id)
        key = (task_id, media_id)
        if key in DOWNLOAD_REQUEUED_CANCELS:
            DOWNLOAD_REQUEUED_CANCELS.discard(key)
            update_media(task_id, media_id, status="PENDING", speed_bytes_per_second=0, error_message=None)
        elif status == "PAUSED":
            update_media(task_id, media_id, status="PENDING", speed_bytes_per_second=0, error_message=None)
        elif status == "CANCELLED":
            update_media(task_id, media_id, status="PENDING", speed_bytes_per_second=0)
        log_event(logging.INFO, "download.cancelled", "Telegram media download cancelled", task_id=task_id, media_id=media_id, task_status=status)
        raise
    except FloodWaitError as error:
        seconds = max(1, int(getattr(error, "seconds", 1)))
        register_flood_wait(seconds)
        update_media(task_id, media_id, status="RETRY_WAIT", speed_bytes_per_second=0, next_retry_at=DOWNLOAD_FLOOD_UNTIL.isoformat() if DOWNLOAD_FLOOD_UNTIL else now(), failure_category="RATE_LIMIT", error_message=f"Telegram 限流，约 {seconds} 秒后自动继续")
    except Exception as error:
        if type(error).__name__ in {"FloodPremiumWaitError", "FloodWaitError"}:
            seconds = max(1, int(getattr(error, "seconds", 1)))
            register_flood_wait(seconds)
            update_media(task_id, media_id, status="RETRY_WAIT", speed_bytes_per_second=0, next_retry_at=DOWNLOAD_FLOOD_UNTIL.isoformat() if DOWNLOAD_FLOOD_UNTIL else now(), failure_category="RATE_LIMIT", error_message=f"Telegram 限流，约 {seconds} 秒后自动继续")
            return
        category, recoverable, message = classify_download_error(error)
        log_event(logging.ERROR if not recoverable else logging.WARNING, "download.failed", "Telegram media download failed", exc_info=True, task_id=task_id, media_id=media_id, error_type=type(error).__name__, failure_category=category, recoverable=recoverable)
        if isinstance(error, TimedOutError):
            request_download_client_reset(task_id, media_id)
        if category == "AUTH":
            mark_session_invalid()
            update_media(task_id, media_id, status="RETRY_WAIT", speed_bytes_per_second=0, failure_category=category, error_message=message)
            stop_all_downloads_for_auth_failure(message)
        elif recoverable:
            with connection() as db:
                record = db.execute("SELECT attempt_count FROM task_media WHERE id = ?", (media_id,)).fetchone()
            attempts = (record["attempt_count"] if record else 0) + 1
            register_recoverable_error()
            if attempts > len(RETRY_DELAYS_SECONDS):
                update_media(task_id, media_id, status="FAILED", attempt_count=attempts, speed_bytes_per_second=0, failure_category=category, error_message="自动重试 3 次后仍失败，可手动重试")
            else:
                retry_at = datetime.now(UTC) + timedelta(seconds=RETRY_DELAYS_SECONDS[attempts - 1])
                update_media(task_id, media_id, status="RETRY_WAIT", attempt_count=attempts, speed_bytes_per_second=0, next_retry_at=retry_at.isoformat(), failure_category=category, error_message=f"{message}（第 {attempts}/3 次）")
        else:
            update_media(task_id, media_id, status="FAILED", speed_bytes_per_second=0, failure_category=category, error_message=message)
    finally:
        DOWNLOAD_RUNNING.pop((task_id, media_id), None)
        await reset_download_client_if_requested()
        finalize_task_status(task_id)
        notify_download_dispatcher()


async def download_dispatcher() -> None:
    global DOWNLOAD_EFFECTIVE_CONCURRENCY, DOWNLOAD_FLOOD_UNTIL, DOWNLOAD_LAST_ERROR_AT, DOWNLOAD_ROUND_ROBIN_OFFSET, PREVIEW_LAST_CLEANUP_AT
    while True:
        try:
            max_concurrency = configured_download_concurrency()
            DOWNLOAD_EFFECTIVE_CONCURRENCY = min(DOWNLOAD_EFFECTIVE_CONCURRENCY, max_concurrency)
            current_time = datetime.now(UTC)
            if PREVIEW_LAST_CLEANUP_AT is None or current_time - PREVIEW_LAST_CLEANUP_AT >= timedelta(minutes=1):
                cleanup_preview_cache()
                PREVIEW_LAST_CLEANUP_AT = current_time
            if DOWNLOAD_FLOOD_UNTIL and current_time >= DOWNLOAD_FLOOD_UNTIL:
                DOWNLOAD_FLOOD_UNTIL = None
                with connection() as db:
                    resumed = db.execute("UPDATE tasks SET status = 'DOWNLOADING', download_wait_until = NULL, updated_at = ? WHERE status = 'WAITING_RATE_LIMIT'", (now(),)).rowcount
                DOWNLOAD_LAST_ERROR_AT = asyncio.get_running_loop().time()
                log_event(logging.INFO, "download.rate_limit_recovered", "Telegram rate-limit pause ended", task_count=resumed)
            if not DOWNLOAD_FLOOD_UNTIL and DOWNLOAD_LAST_ERROR_AT is not None:
                monotonic_now = asyncio.get_running_loop().time()
                if monotonic_now - DOWNLOAD_LAST_ERROR_AT >= STABILITY_WINDOW_SECONDS and DOWNLOAD_EFFECTIVE_CONCURRENCY < max_concurrency:
                    previous = DOWNLOAD_EFFECTIVE_CONCURRENCY
                    DOWNLOAD_EFFECTIVE_CONCURRENCY += 1
                    DOWNLOAD_LAST_ERROR_AT = monotonic_now
                    log_event(logging.INFO, "download.concurrency_recovered", "Download concurrency increased after stability window", previous_effective_concurrency=previous, effective_concurrency=DOWNLOAD_EFFECTIVE_CONCURRENCY)
            slots = max(0, DOWNLOAD_EFFECTIVE_CONCURRENCY - len(DOWNLOAD_RUNNING) - len(PREVIEW_RUNNING))
            if slots and not DOWNLOAD_FLOOD_UNTIL and not DOWNLOAD_CLIENT_RESET_REQUESTED and (DEMO_MODE or app_state()["accountConnected"]):
                # A clicked preview should receive the next available slot, but it
                # never interrupts a task that is already writing an archive file.
                while slots:
                    preview = claim_next_preview()
                    if not preview:
                        break
                    PREVIEW_RUNNING[preview["id"]] = asyncio.create_task(preview_download_job(preview["id"]), name=f"telegram-preview-{preview['id']}")
                    slots -= 1
                task_ids = eligible_task_ids()
                if task_ids:
                    offset = DOWNLOAD_ROUND_ROBIN_OFFSET % len(task_ids)
                    ordered = task_ids[offset:] + task_ids[:offset]
                    for task_id in ordered[:slots]:
                        media = claim_next_media(task_id)
                        if media:
                            key = (task_id, media["id"])
                            runner = download_demo_media_job if DEMO_MODE else download_media_job
                            DOWNLOAD_RUNNING[key] = asyncio.create_task(runner(*key), name=f"telegram-media-{task_id}-{media['id']}")
                    DOWNLOAD_ROUND_ROBIN_OFFSET += 1
            DOWNLOAD_WAKE.clear()
            try:
                await asyncio.wait_for(DOWNLOAD_WAKE.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                pass
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Keep the dispatcher alive; individual jobs expose their own error state.
            log_event(logging.ERROR, "download.dispatcher_failed", "Download dispatcher iteration failed", exc_info=True, error_type=type(error).__name__)
            await asyncio.sleep(1)


def current_task_status(task_id: int) -> str | None:
    with connection() as db:
        task = db.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return task["status"] if task else None


def task_request(task: sqlite3.Row) -> ScanRequest:
    return ScanRequest(chat_id=task["chat_id"], chat_title=task["chat_title"], chat_handle=task["chat_handle"], filters=TaskFilters(**json.loads(task["filters_json"])))


def record_archive(task: sqlite3.Row, media: sqlite3.Row, path: Path) -> None:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with connection() as db:
        existing = db.execute("SELECT id FROM media_blobs WHERE content_hash = ?", (digest,)).fetchone()
        if existing:
            blob_id = existing["id"]
        else:
            blob_id = db.execute("""INSERT INTO media_blobs (content_hash, canonical_path, thumbnail_status, size_bytes, media_type, created_at)
                VALUES (?, ?, 'PENDING', ?, ?, ?)""", (digest, str(path), media["size_bytes"], media["media_type"], now())).lastrowid
        db.execute("""INSERT INTO archive_items (blob_id, chat_id, chat_title, message_id, filename, media_type, mime_type, size_bytes, message_date, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (blob_id, task["chat_id"], task["chat_title"], media["message_id"], media["filename"], media["media_type"], media["mime_type"], media["size_bytes"], media["message_date"], now()))
    wake_thumbnail_worker()
    log_event(logging.INFO, "archive.recorded", "Downloaded media recorded in archive", task_id=task["id"], media_id=media["id"], blob_id=blob_id, chat_id=task["chat_id"], chat_title=task["chat_title"], message_id=media["message_id"], filename=media["filename"], archive_path=str(path), duplicate_blob=bool(existing))


async def download_demo_media_job(task_id: int, media_id: int) -> None:
    """Use the same pool in demo mode so concurrency controls remain observable."""
    try:
        while current_task_status(task_id) in {"DOWNLOADING", "RETRYING"}:
            with connection() as db:
                media = db.execute("SELECT * FROM task_media WHERE id = ? AND task_id = ?", (media_id, task_id)).fetchone()
            if not media or media["status"] != "DOWNLOADING":
                return
            transferred = min(1_000_000, media["size_bytes"] - media["downloaded_bytes"])
            downloaded = media["downloaded_bytes"] + transferred
            update_parallel_download_progress(task_id, media_id, downloaded, 1_000_000)
            if downloaded >= media["size_bytes"]:
                update_media(task_id, media_id, status="COMPLETED", downloaded_bytes=media["size_bytes"], speed_bytes_per_second=0, error_message=None, failure_category=None)
                log_event(logging.INFO, "download.completed", "Demo media download completed", task_id=task_id, media_id=media_id, filename=media["filename"], size_bytes=media["size_bytes"], demo_mode=True)
                return
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        status = current_task_status(task_id)
        key = (task_id, media_id)
        if key in DOWNLOAD_REQUEUED_CANCELS:
            DOWNLOAD_REQUEUED_CANCELS.discard(key)
            update_media(task_id, media_id, status="PENDING", speed_bytes_per_second=0, error_message=None)
        elif status in {"PAUSED", "CANCELLED"}:
            update_media(task_id, media_id, status="PENDING", speed_bytes_per_second=0)
        raise
    finally:
        DOWNLOAD_RUNNING.pop((task_id, media_id), None)
        finalize_task_status(task_id)
        notify_download_dispatcher()


async def run_task_worker(task_id: int) -> None:
    try:
        if DEMO_MODE:
            with connection() as db:
                task = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
                planned = db.execute("SELECT COUNT(*) FROM task_media WHERE task_id = ?", (task_id,)).fetchone()[0]
            if not task or task["status"] in {"PAUSED", "CANCELLED", "COMPLETED"}:
                return
            if not planned:
                matched = await scan_messages(task_request(task))
                with connection() as db:
                    db.executemany("""INSERT INTO task_media (task_id, message_id, filename, media_type, mime_type, size_bytes, message_date)
                        VALUES (?, ?, ?, ?, ?, ?, ?)""", [(task_id, *item) for item in matched])
                update_task(task_id, total_count=len(matched), total_bytes=sum(item[4] for item in matched), completed_count=0, downloaded_bytes=0)
            update_task(task_id, status="DOWNLOADING")
            if not planned and not matched:
                finalize_task_status(task_id)
            notify_download_dispatcher()
            return
        with connection() as db:
            task = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            planned = db.execute("SELECT COUNT(*) FROM task_media WHERE task_id = ?", (task_id,)).fetchone()[0]
        if not task or task["status"] in {"PAUSED", "CANCELLED", "COMPLETED"}:
            return
        if not planned:
            update_task(task_id, status="SCANNING", current_file=None, speed_bytes_per_second=0)
            payload = task_request(task)
            matched = take_cached_scan(payload)
            if matched is not None:
                log_event(logging.INFO, "scan.cache_hit", "Task worker reused cached scan result", task_id=task_id, chat_id=task["chat_id"], chat_title=task["chat_title"], matched_count=len(matched))
            else:
                matched = await scan_messages(payload)
            with connection() as db:
                db.executemany("""INSERT INTO task_media (task_id, message_id, filename, media_type, mime_type, size_bytes, message_date)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""", [(task_id, *item) for item in matched])
            update_task(task_id, status="DOWNLOADING", total_count=len(matched), total_bytes=sum(item[4] for item in matched), completed_count=0, downloaded_bytes=0, failed_count=0)
            if not matched:
                finalize_task_status(task_id)
        else:
            update_task(task_id, status="DOWNLOADING", current_file=None, speed_bytes_per_second=0)
        notify_download_dispatcher()
        return
    except asyncio.CancelledError:
        log_event(logging.INFO, "task.worker_cancelled", "Task worker cancelled", task_id=task_id)
        raise
    except Exception as error:
        log_event(logging.ERROR, "task.worker_failed", "Task worker failed while preparing download list", exc_info=True, task_id=task_id, error_type=type(error).__name__)
        update_task(task_id, status="FAILED", current_file=None, speed_bytes_per_second=0, error_message=f"创建下载清单失败：{str(error) or '请检查 Telegram 连接后重试'}")
    finally:
        TASK_WORKERS.pop(task_id, None)
        log_event(logging.DEBUG, "task.worker_stopped", "Task worker stopped", task_id=task_id)


def start_task_worker(task_id: int) -> None:
    worker = TASK_WORKERS.get(task_id)
    if not worker or worker.done():
        TASK_WORKERS[task_id] = asyncio.create_task(run_task_worker(task_id), name=f"telegram-download-{task_id}")
        log_event(logging.INFO, "task.worker_started", "Task worker started", task_id=task_id)


def start_pending_task_workers() -> None:
    with connection() as db:
        task_ids = [record["id"] for record in db.execute("""SELECT id FROM tasks
            WHERE status IN ('QUEUED', 'DOWNLOADING', 'SCANNING', 'RETRYING', 'WAITING_RATE_LIMIT')""").fetchall()]
    for task_id in task_ids:
        start_task_worker(task_id)
    if task_ids:
        log_event(logging.INFO, "task.workers_restored", "Pending task workers restored", task_count=len(task_ids))


@app.post("/api/tasks")
async def create_task(payload: ScanRequest) -> dict[str, Any]:
    created = now()
    with connection() as db:
        setting = db.execute("SELECT archive_timezone FROM app_settings WHERE id = 1").fetchone()
        if not setting or not setting["archive_timezone"]:
            raise HTTPException(409, "请先完成归档时区设置")
        cursor = db.execute("""INSERT INTO tasks (chat_id, chat_title, chat_handle, archive_timezone, filters_json, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'QUEUED', ?, ?)""", (payload.chat_id, payload.chat_title, payload.chat_handle, setting["archive_timezone"], payload.filters.model_dump_json(), created, created))
    log_event(logging.INFO, "task.created", "Archive task created", task_id=cursor.lastrowid, chat_id=payload.chat_id, chat_title=payload.chat_title, chat_handle=payload.chat_handle, archive_timezone=setting["archive_timezone"], filters=payload.filters.model_dump())
    start_task_worker(cursor.lastrowid)
    return get_task(cursor.lastrowid)


async def selected_source_messages(payload: SelectionTaskRequest) -> list[tuple[int, str, str, str | None, int, str]]:
    requested_ids = list(dict.fromkeys(payload.message_ids))
    if DEMO_MODE:
        available = {item[0]: item for item in demo_source_media(payload.chat_id, None, {"PHOTO", "VIDEO", "DOCUMENT", "AUDIO"})}
        return [available[item_id] for item_id in requested_ids if item_id in available]
    if not app_state()["accountConnected"]:
        raise HTTPException(409, "Telegram 尚未连接或登录状态已失效，请重新连接账号")
    try:
        async with connected_telegram_client() as client:
            entity = await client.get_entity(int(payload.chat_id))
            messages = await client.get_messages(entity, ids=requested_ids)
            if not isinstance(messages, list):
                messages = [messages]
            result = []
            for message in messages:
                match = matching_media(message, TaskFilters(media_types=["PHOTO", "VIDEO", "AUDIO", "DOCUMENT"])) if message else None
                if match:
                    media_type, size, mime_type = match
                    result.append((message.id, filename_for(message, media_type), media_type, mime_type, size, message.date.isoformat()))
            return result
    except HTTPException:
        raise
    except Exception as error:
        log_event(logging.ERROR, "source_media.selection_validation_failed", "Unable to validate selected Telegram messages", exc_info=True, chat_id=payload.chat_id, error_type=type(error).__name__)
        raise_telegram_error(error)


@app.post("/api/tasks/selection")
async def create_selection_task(payload: SelectionTaskRequest) -> dict[str, Any]:
    selected = await selected_source_messages(payload)
    if not selected:
        raise HTTPException(409, "所选文件已不可用，或不再包含可下载媒体")
    archive_ids, queued_ids, previews = source_item_states(payload.chat_id, [item[0] for item in selected])
    accepted = [item for item in selected if item[0] not in archive_ids and item[0] not in queued_ids]
    if not accepted:
        raise HTTPException(409, "所选文件已归档或已加入下载队列")
    preview_workers: list[asyncio.Task[None]] = []
    for item in accepted:
        preview = previews.get(item[0])
        if preview and preview["status"] == "DOWNLOADING":
            worker = PREVIEW_RUNNING.get(preview["id"])
            if worker:
                worker.cancel()
                preview_workers.append(worker)
    if preview_workers:
        await asyncio.gather(*preview_workers, return_exceptions=True)
    created = now()
    with connection() as db:
        setting = db.execute("SELECT archive_timezone FROM app_settings WHERE id = 1").fetchone()
        if not setting or not setting["archive_timezone"]:
            raise HTTPException(409, "请先完成归档时区设置")
        cursor = db.execute(
            """INSERT INTO tasks (chat_id, chat_title, chat_handle, archive_timezone, filters_json, status, total_count, total_bytes, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'QUEUED', ?, ?, ?, ?)""",
            (payload.chat_id, payload.chat_title, payload.chat_handle, setting["archive_timezone"], json.dumps({"mode": "selection", "message_ids": [item[0] for item in accepted]}, ensure_ascii=False), len(accepted), sum(item[4] for item in accepted), created, created),
        )
        task_id = cursor.lastrowid
        db.executemany(
            """INSERT INTO task_media (task_id, message_id, filename, media_type, mime_type, size_bytes, message_date, preview_cache_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [(task_id, *item, previews[item[0]]["id"] if item[0] in previews else None) for item in accepted],
        )
    log_event(logging.INFO, "task.selection_created", "Selected media task created", task_id=task_id, chat_id=payload.chat_id, chat_title=payload.chat_title, media_count=len(accepted), skipped_archived=len(selected) - len(accepted))
    start_task_worker(task_id)
    return get_task(task_id)


@app.post("/api/sources/{chat_id}/media/{message_id}/preview")
async def start_source_preview(chat_id: str, message_id: int, payload: PreviewRequest) -> dict[str, Any]:
    if message_id <= 0:
        raise HTTPException(400, "消息编号无效")
    cleanup_preview_cache()
    with connection() as db:
        record = db.execute("SELECT * FROM preview_cache WHERE chat_id = ? AND message_id = ?", (chat_id, message_id)).fetchone()
        if record:
            if record["status"] == "FAILED":
                db.execute("UPDATE preview_cache SET status = 'PENDING', error_message = NULL, expires_at = ?, updated_at = ? WHERE id = ?", ((datetime.now(UTC) + PREVIEW_CACHE_TTL).isoformat(), now(), record["id"]))
                record = db.execute("SELECT * FROM preview_cache WHERE id = ?", (record["id"],)).fetchone()
            else:
                db.execute("UPDATE preview_cache SET expires_at = ?, updated_at = ? WHERE id = ?", ((datetime.now(UTC) + PREVIEW_CACHE_TTL).isoformat(), now(), record["id"]))
                record = db.execute("SELECT * FROM preview_cache WHERE id = ?", (record["id"],)).fetchone()
        else:
            temporary_path = PREVIEW_ROOT / f"pending-{secrets.token_hex(8)}"
            cache_id = db.execute(
                """INSERT INTO preview_cache (chat_id, message_id, filename, media_type, mime_type, size_bytes, message_date, cache_path, expires_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (chat_id, message_id, safe_filename(payload.filename), payload.media_type, payload.mime_type, payload.size_bytes, payload.message_date, str(temporary_path), (datetime.now(UTC) + PREVIEW_CACHE_TTL).isoformat(), now()),
            ).lastrowid
            extension = Path(safe_filename(payload.filename)).suffix or ".bin"
            final_path = PREVIEW_ROOT / f"preview-{cache_id}{extension}"
            db.execute("UPDATE preview_cache SET cache_path = ? WHERE id = ?", (str(final_path), cache_id))
            record = db.execute("SELECT * FROM preview_cache WHERE id = ?", (cache_id,)).fetchone()
    notify_download_dispatcher()
    return preview_payload(record)


@app.get("/api/previews/{cache_id}")
def get_source_preview(cache_id: int) -> dict[str, Any]:
    with connection() as db:
        record = db.execute("SELECT * FROM preview_cache WHERE id = ?", (cache_id,)).fetchone()
    if not record:
        raise HTTPException(404, "预览缓存不存在或已过期")
    return preview_payload(record)


@app.delete("/api/previews/{cache_id}")
async def stop_source_preview(cache_id: int) -> dict[str, Any]:
    worker = PREVIEW_RUNNING.get(cache_id)
    if worker:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
    with connection() as db:
        record = db.execute("SELECT * FROM preview_cache WHERE id = ?", (cache_id,)).fetchone()
    if not record:
        raise HTTPException(404, "预览缓存不存在或已过期")
    return preview_payload(record)


@app.get("/api/previews/{cache_id}/content")
def source_preview_content(cache_id: int) -> FileResponse:
    with connection() as db:
        record = db.execute("SELECT * FROM preview_cache WHERE id = ?", (cache_id,)).fetchone()
    if not record or record["status"] != "READY":
        raise HTTPException(409, "预览文件尚未准备完成")
    file_path = path_in_root(record["cache_path"], PREVIEW_ROOT)
    if not file_path or not file_path.is_file():
        raise HTTPException(404, "预览缓存文件不存在")
    return FileResponse(file_path, media_type=record["mime_type"] or mimetypes.guess_type(record["filename"])[0] or "application/octet-stream", filename=record["filename"], content_disposition_type="inline", headers={"Cache-Control": "private, max-age=86400"})


@app.post("/api/tasks/{task_id}/pause")
async def pause_task(task_id: int) -> dict[str, Any]:
    update_task(task_id, status="PAUSED")
    cancel_task_downloads(task_id)
    notify_download_dispatcher()
    return get_task(task_id)


@app.post("/api/tasks/{task_id}/resume")
async def resume_task(task_id: int) -> dict[str, Any]:
    update_task(task_id, status="DOWNLOADING")
    start_task_worker(task_id)
    notify_download_dispatcher()
    return get_task(task_id)


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: int) -> dict[str, Any]:
    update_task(task_id, status="CANCELLED")
    cancel_task_downloads(task_id)
    notify_download_dispatcher()
    return get_task(task_id)


@app.post("/api/tasks/{task_id}/retry")
async def retry_task(task_id: int) -> dict[str, Any]:
    with connection() as db:
        failed_ids = [record["id"] for record in db.execute("SELECT id FROM task_media WHERE task_id = ? AND status IN ('FAILED', 'RETRY_WAIT')", (task_id,)).fetchall()]
    for media_id in failed_ids:
        update_media(task_id, media_id, status="PENDING", downloaded_bytes=0, speed_bytes_per_second=0, attempt_count=0, next_retry_at=None, failure_category=None, error_message=None)
    update_task(task_id, status="DOWNLOADING", failed_count=0, error_message=None)
    log_event(logging.INFO, "task.retry_requested", "Failed task media queued for retry", task_id=task_id, media_count=len(failed_ids))
    start_task_worker(task_id)
    notify_download_dispatcher()
    return get_task(task_id)


@app.get("/api/tasks/{task_id}/events")
async def task_events(request: Request, task_id: int, after_revision: int = 0) -> StreamingResponse:
    async def event_stream():
        last_revision = max(0, after_revision)
        while not await request.is_disconnected():
            try:
                task = get_task(task_id)
                yield f"event: task\ndata: {json.dumps(task)}\n\n"
                for media in changed_task_media(task_id, last_revision):
                    last_revision = max(last_revision, media["revision"])
                    yield f"event: media\ndata: {json.dumps(media)}\n\n"
            except HTTPException:
                yield "event: error\ndata: {\"message\":\"任务不存在\"}\n\n"
                return
            await asyncio.sleep(1)
    return StreamingResponse(event_stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/archives/chats")
def archive_chats() -> list[dict[str, Any]]:
    with connection() as db:
        records = db.execute("SELECT chat_id AS id, chat_title AS title, COUNT(*) AS item_count FROM archive_items GROUP BY chat_id, chat_title ORDER BY title").fetchall()
    return rows(records)


def archive_record(item_id: int) -> sqlite3.Row:
    with connection() as db:
        record = db.execute(
            """SELECT a.*, b.canonical_path, b.thumbnail_path, b.thumbnail_status, b.thumbnail_error FROM archive_items a
            JOIN media_blobs b ON a.blob_id = b.id WHERE a.id = ?""",
            (item_id,),
        ).fetchone()
    if not record:
        raise HTTPException(404, "归档项目不存在")
    return record


def archive_payload(record: sqlite3.Row) -> dict[str, Any]:
    payload = dict(record)
    item_id = record["id"]
    payload["thumbnail_url"] = f"/api/archives/media/{item_id}/thumbnail" if record["thumbnail_status"] == "READY" else None
    payload["content_url"] = f"/api/archives/media/{item_id}/content" if record["media_type"] in {"PHOTO", "VIDEO"} else None
    payload["download_url"] = f"/api/archives/media/{item_id}/download"
    return payload


def archive_file_response(item_id: int, kind: Literal["thumbnail", "content", "download"]) -> FileResponse:
    record = archive_record(item_id)
    if kind == "thumbnail":
        if record["thumbnail_status"] != "READY" or not record["thumbnail_path"]:
            raise HTTPException(404, "缩略图暂不可用")
        file_path = path_in_root(record["thumbnail_path"], THUMBNAIL_ROOT)
        media_type = "image/jpeg"
        filename = f"{record['filename']}.jpg"
    else:
        file_path = path_in_root(record["canonical_path"], DOWNLOAD_ROOT)
        media_type = record["mime_type"] or mimetypes.guess_type(record["filename"])[0] or "application/octet-stream"
        filename = record["filename"]
    if not file_path or not file_path.is_file():
        raise HTTPException(404, "归档文件不存在")
    return FileResponse(
        file_path,
        media_type=media_type,
        filename=filename,
        content_disposition_type="attachment" if kind == "download" else "inline",
        headers={"Cache-Control": "private, max-age=86400"},
    )


@app.get("/api/archives/media")
def archive_media(chat_id: str | None = None, media_type: str | None = None, month: str | None = None) -> list[dict[str, Any]]:
    clauses, values = [], []
    if chat_id:
        clauses.append("a.chat_id = ?")
        values.append(chat_id)
    if media_type:
        clauses.append("a.media_type = ?")
        values.append(media_type)
    if month:
        clauses.append("substr(a.message_date, 1, 7) = ?")
        values.append(month)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with connection() as db:
        records = db.execute(
            f"""SELECT a.*, b.canonical_path, b.thumbnail_path, b.thumbnail_status, b.thumbnail_error
            FROM archive_items a JOIN media_blobs b ON a.blob_id = b.id {where}
            ORDER BY a.message_date DESC""", values).fetchall()
    return [archive_payload(record) for record in records]


@app.get("/api/archives/media/{item_id}/thumbnail")
def archive_thumbnail(item_id: int) -> FileResponse:
    return archive_file_response(item_id, "thumbnail")


@app.get("/api/archives/media/{item_id}/content")
def archive_content(item_id: int) -> FileResponse:
    return archive_file_response(item_id, "content")


@app.get("/api/archives/media/{item_id}/download")
def archive_download(item_id: int) -> FileResponse:
    return archive_file_response(item_id, "download")


@app.get("/api/archives/media/{item_id}")
def archive_media_detail(item_id: int) -> dict[str, Any]:
    return archive_payload(archive_record(item_id))


if STATIC_DIR.exists():
    assets_dir = STATIC_DIR / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def frontend_application(full_path: str) -> FileResponse:
        target = STATIC_DIR / full_path
        if full_path and target.is_file():
            return FileResponse(target)
        return FileResponse(STATIC_DIR / "index.html")
