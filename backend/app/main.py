from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import os
import re
import secrets
import sqlite3
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image as PillowImage
from PIL import ImageOps, UnidentifiedImageError
from pydantic import BaseModel, Field
from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError, SessionPasswordNeededError


DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
DB_PATH = DATA_DIR / "app.db"
DOWNLOAD_ROOT = os.getenv("DOWNLOAD_ROOT", str(DATA_DIR / "downloads"))
THUMBNAIL_ROOT = DATA_DIR / "thumbnails"
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
DOWNLOAD_RUNNING: dict[tuple[int, int], asyncio.Task[None]] = {}
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
        auto_reconnect=False,
        connection_retries=1,
    )
    try:
        await client.connect()
    except (OSError, asyncio.TimeoutError) as error:
        raise HTTPException(503, "无法连接 Telegram，请检查网络后重试") from error
    finally:
        secure_session_file()
    return client


async def close_telegram_client(client: TelegramClient) -> None:
    try:
        if client.is_connected():
            await client.disconnect()
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
              UNIQUE(task_id, message_id)
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
        task_columns = {column["name"] for column in db.execute("PRAGMA table_info(tasks)").fetchall()}
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
        # A process restart cannot safely resume an in-flight Telethon transfer.
        # Keep completed work and return only the interrupted file to the queue.
        db.execute("UPDATE task_media SET status = 'PENDING', speed_bytes_per_second = 0 WHERE status = 'DOWNLOADING'")
        if DEMO_MODE and db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0:
            seed_demo_data(db)
    reconcile_thumbnail_records()


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
        if blob["media_type"] == "PHOTO":
            await asyncio.to_thread(create_image_thumbnail, source, target)
        else:
            await create_video_thumbnail(source, target)
    except (UnidentifiedImageError, OSError, RuntimeError) as error:
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


def masked_api_id(api_id: str | None) -> str | None:
    if not api_id:
        return None
    return f"••••{api_id[-4:]}"


def app_state() -> dict[str, Any]:
    with connection() as db:
        setting = db.execute("SELECT * FROM app_settings WHERE id = 1").fetchone()
    configured = bool(setting["api_id"] and setting["api_hash"])
    stored_status = setting["connection_status"]
    connection_status = "unconfigured" if not configured else stored_status
    connected = configured and stored_status == "connected" and bool(setting["account_connected"])
    return {
        "configured": configured,
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
        "activeDownloads": len(DOWNLOAD_RUNNING),
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
        else:
            db.execute("UPDATE tasks SET status = 'DOWNLOADING', download_wait_until = NULL, updated_at = ? WHERE status = 'WAITING_RATE_LIMIT'", (now(),))


@asynccontextmanager
async def lifespan(_: FastAPI):
    global DOWNLOAD_DISPATCHER, THUMBNAIL_WORKER
    initialize_database()
    restore_download_runtime()
    DOWNLOAD_DISPATCHER = asyncio.create_task(download_dispatcher(), name="telegram-download-dispatcher")
    THUMBNAIL_WORKER = asyncio.create_task(thumbnail_worker(), name="archive-thumbnail-worker")
    wake_thumbnail_worker()
    with connection() as db:
        task_ids = [record["id"] for record in db.execute("SELECT id FROM tasks WHERE status IN ('QUEUED', 'DOWNLOADING', 'SCANNING', 'RETRYING', 'WAITING_RATE_LIMIT')").fetchall()]
    for task_id in task_ids:
        start_task_worker(task_id)
    yield
    if DOWNLOAD_DISPATCHER:
        DOWNLOAD_DISPATCHER.cancel()
        await asyncio.gather(DOWNLOAD_DISPATCHER, return_exceptions=True)
    if THUMBNAIL_WORKER:
        THUMBNAIL_WORKER.cancel()
        await asyncio.gather(THUMBNAIL_WORKER, return_exceptions=True)
    for worker in tuple(DOWNLOAD_RUNNING.values()):
        worker.cancel()
    await asyncio.gather(*tuple(DOWNLOAD_RUNNING.values()), return_exceptions=True)
    await close_download_client()
    for worker in tuple(TASK_WORKERS.values()):
        worker.cancel()
    await asyncio.gather(*tuple(TASK_WORKERS.values()), return_exceptions=True)


app = FastAPI(title="Telegram 媒体归档器", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "http://localhost:5173").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/app-state")
def get_app_state() -> dict[str, Any]:
    return app_state()


@app.put("/api/setup")
def save_setup(payload: ApiCredentials) -> dict[str, Any]:
    with connection() as db:
        db.execute("UPDATE app_settings SET api_id = ?, api_hash = ?, updated_at = ? WHERE id = 1", (payload.api_id, payload.api_hash, now()))
    return app_state()


@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    with connection() as db:
        setting = db.execute("SELECT * FROM app_settings WHERE id = 1").fetchone()
    configured = bool(setting["api_id"] and setting["api_hash"])
    connected = configured and setting["connection_status"] == "connected" and bool(setting["account_connected"])
    return {
        "apiId": masked_api_id(setting["api_id"]),
        "apiHashConfigured": bool(setting["api_hash"]),
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
    return download_runtime_state()


@app.post("/api/telegram/login/send-code")
async def send_login_code(payload: LoginCodeRequest) -> dict[str, Any]:
    phone = normalize_phone(payload.phone)
    get_telegram_credentials()
    attempt_id = secrets.token_urlsafe(24)

    if DEMO_MODE:
        pending_logins[attempt_id] = PendingLogin(phone=phone, phone_code_hash=None, expires_at=datetime.now(UTC) + LOGIN_ATTEMPT_TTL)
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
        except HTTPException:
            raise
        except Exception as error:
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
        return {**app_state(), "accountPhone": mask_phone(attempt.phone), "passwordRequired": False}

    async with TELEGRAM_LOCK:
        client: TelegramClient | None = None
        try:
            client = await open_telegram_client()
            try:
                await client.sign_in(phone=attempt.phone, code=code, phone_code_hash=attempt.phone_code_hash)
            except SessionPasswordNeededError:
                return {"passwordRequired": True, "attemptId": payload.attempt_id}
            user = await client.get_me()
            update_connected_account(attempt.phone, user)
            pending_logins.pop(payload.attempt_id, None)
        except SessionPasswordNeededError:
            return {"passwordRequired": True, "attemptId": payload.attempt_id}
        except HTTPException:
            raise
        except Exception as error:
            raise_telegram_error(error, during_authorization=True)
        finally:
            if client:
                await close_telegram_client(client)
    return {**app_state(), "accountPhone": mask_phone(attempt.phone), "passwordRequired": False}


@app.post("/api/telegram/login/verify-password")
async def verify_login_password(payload: LoginPasswordVerify) -> dict[str, Any]:
    attempt = require_pending_login(payload.attempt_id)

    if DEMO_MODE:
        update_connected_account(attempt.phone, user=object())
        pending_logins.pop(payload.attempt_id, None)
        return {**app_state(), "accountPhone": mask_phone(attempt.phone), "passwordRequired": False}

    async with TELEGRAM_LOCK:
        client: TelegramClient | None = None
        try:
            client = await open_telegram_client()
            user = await client.sign_in(password=payload.password)
            update_connected_account(attempt.phone, user)
            pending_logins.pop(payload.attempt_id, None)
        except HTTPException:
            raise
        except Exception as error:
            raise_telegram_error(error, during_authorization=True)
        finally:
            if client:
                await close_telegram_client(client)
    return {**app_state(), "accountPhone": mask_phone(attempt.phone), "passwordRequired": False}


@app.post("/api/telegram/logout")
async def logout() -> dict[str, Any]:
    await close_download_client()
    remote_logout_warning: str | None = None
    if not DEMO_MODE and SESSION_PATH.exists():
        async with TELEGRAM_LOCK:
            client: TelegramClient | None = None
            try:
                client = await open_telegram_client()
                if await client.is_user_authorized():
                    await client.log_out()
            except Exception:
                # The local session still has to be removed: the user explicitly
                # requested to remove this server's access to their account.
                remote_logout_warning = "本地会话已清除，但 Telegram 远端注销未能确认。"
            finally:
                if client:
                    await close_telegram_client(client)
    clear_local_session()
    pending_logins.clear()
    with connection() as db:
        db.execute(
            """UPDATE app_settings SET account_connected = 0, account_name = NULL, account_phone = NULL,
               connection_status = 'disconnected', updated_at = ? WHERE id = 1""",
            (now(),),
        )
    return {**app_state(), "warning": remote_logout_warning}


@app.get("/api/chats")
async def list_chats() -> list[dict[str, Any]]:
    if not app_state()["accountConnected"]:
        raise HTTPException(409, "Telegram 尚未连接或登录状态已失效，请重新连接账号")
    if DEMO_MODE:
        return [
            {"id": "demo-tech", "title": "科技前沿观察", "handle": "@tech_frontier_obs", "type": "CHANNEL"},
            {"id": "demo-design", "title": "设计灵感库", "handle": "@design_inspiration_cn", "type": "CHANNEL"},
            {"id": "demo-study", "title": "学习资料库", "handle": "@study_materials_zh", "type": "GROUP"},
        ]

    async with TELEGRAM_LOCK:
        client: TelegramClient | None = None
        try:
            client = await open_telegram_client()
            if not await client.is_user_authorized():
                mark_session_invalid()
                raise HTTPException(409, "Telegram 登录状态已失效，请重新连接账号")
            chats: list[dict[str, Any]] = []
            async for dialog in client.iter_dialogs():
                entity = dialog.entity
                chats.append({
                    "id": str(dialog.id),
                    "title": dialog.name or "未命名聊天",
                    "handle": f"@{entity.username}" if getattr(entity, "username", None) else None,
                    "type": "CHANNEL" if dialog.is_channel else "GROUP" if dialog.is_group else "PRIVATE",
                })
            return chats
        except HTTPException:
            raise
        except Exception as error:
            raise_telegram_error(error)
        finally:
            if client:
                await close_telegram_client(client)


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
    if DEMO_MODE:
        seed = sum(ord(char) for char in payload.chat_id)
        count, size = 160 + seed % 870, 4_000_000 + seed % 10_000_000
        return [(index, f"demo-{index}.bin", "DOCUMENT", "application/octet-stream", size, now()) for index in range(count)]
    if not app_state()["accountConnected"]:
        raise HTTPException(409, "Telegram 尚未连接或登录状态已失效，请重新连接账号")
    try:
        start_date = date.fromisoformat(payload.filters.date_start) if payload.filters.date_start else None
        end_date = date.fromisoformat(payload.filters.date_end) if payload.filters.date_end else None
    except ValueError as error:
        raise HTTPException(400, "日期格式无效") from error
    result: list[tuple[int, str, str, str | None, int, str]] = []
    async with TELEGRAM_LOCK:
        client: TelegramClient | None = None
        try:
            client = await open_telegram_client()
            if not await client.is_user_authorized():
                mark_session_invalid()
                raise HTTPException(409, "Telegram 登录状态已失效，请重新连接账号")
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
            raise_telegram_error(error)
        finally:
            if client:
                await close_telegram_client(client)
    return result


@app.post("/api/tasks/scan")
async def scan_task(payload: ScanRequest) -> dict[str, Any]:
    matched = await scan_messages(payload)
    SCAN_CACHE[scan_cache_key(payload)] = (datetime.now(UTC) + SCAN_CACHE_TTL, matched)
    return {"chat": payload.model_dump(exclude={"filters"}), "filters": payload.filters.model_dump(), "totalCount": len(matched), "totalBytes": sum(item[4] for item in matched), "duplicateCount": 0}


def update_task(task_id: int, **values: Any) -> None:
    values["updated_at"] = now()
    assignments = ", ".join(f"{key} = ?" for key in values)
    with connection() as db:
        db.execute(f"UPDATE tasks SET {assignments} WHERE id = ?", (*values.values(), task_id))


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
        revision = task["media_revision"] + 1
        values.update(updated_at=timestamp, revision=revision)
        assignments = ", ".join(f"{key} = ?" for key in values)
        db.execute(f"UPDATE task_media SET {assignments} WHERE id = ? AND task_id = ?", (*values.values(), media_id, task_id))
        db.execute("UPDATE tasks SET media_revision = ?, updated_at = ? WHERE id = ?", (revision, timestamp, task_id))
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
    global DOWNLOAD_CLIENT
    async with DOWNLOAD_CLIENT_LOCK:
        if DOWNLOAD_CLIENT and DOWNLOAD_CLIENT.is_connected():
            return DOWNLOAD_CLIENT
        async with TELEGRAM_LOCK:
            DOWNLOAD_CLIENT = await open_telegram_client()
            if not await DOWNLOAD_CLIENT.is_user_authorized():
                await close_telegram_client(DOWNLOAD_CLIENT)
                DOWNLOAD_CLIENT = None
                mark_session_invalid()
                raise RuntimeError("Telegram 登录状态已失效")
        return DOWNLOAD_CLIENT


async def close_download_client() -> None:
    global DOWNLOAD_CLIENT
    async with DOWNLOAD_CLIENT_LOCK:
        if DOWNLOAD_CLIENT:
            async with TELEGRAM_LOCK:
                await close_telegram_client(DOWNLOAD_CLIENT)
            DOWNLOAD_CLIENT = None


def claim_next_media(task_id: int) -> sqlite3.Row | None:
    timestamp = now()
    with connection() as db:
        task = db.execute("SELECT media_revision FROM tasks WHERE id = ?", (task_id,)).fetchone()
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
        return db.execute("SELECT * FROM task_media WHERE id = ?", (media["id"],)).fetchone()


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
    if isinstance(error, (OSError, asyncio.TimeoutError)) or isinstance(error, RPCError):
        return "NETWORK", True, "网络或 Telegram 服务暂时不可用，将自动重试"
    return "UNKNOWN", True, "下载发生临时错误，将自动重试"


def register_recoverable_error() -> None:
    global DOWNLOAD_EFFECTIVE_CONCURRENCY, DOWNLOAD_LAST_ERROR_AT
    current = asyncio.get_running_loop().time()
    DOWNLOAD_ERROR_TIMES.append(current)
    while DOWNLOAD_ERROR_TIMES and DOWNLOAD_ERROR_TIMES[0] < current - 60:
        DOWNLOAD_ERROR_TIMES.popleft()
    if len(DOWNLOAD_ERROR_TIMES) >= 2:
        DOWNLOAD_EFFECTIVE_CONCURRENCY = max(1, DOWNLOAD_EFFECTIVE_CONCURRENCY - 1)
        DOWNLOAD_ERROR_TIMES.clear()
    DOWNLOAD_LAST_ERROR_AT = current


def register_flood_wait(seconds: int) -> None:
    global DOWNLOAD_EFFECTIVE_CONCURRENCY, DOWNLOAD_FLOOD_UNTIL, DOWNLOAD_LAST_ERROR_AT
    wait_until = datetime.now(UTC) + timedelta(seconds=max(1, seconds))
    DOWNLOAD_EFFECTIVE_CONCURRENCY = 1
    DOWNLOAD_FLOOD_UNTIL = max(DOWNLOAD_FLOOD_UNTIL, wait_until) if DOWNLOAD_FLOOD_UNTIL else wait_until
    DOWNLOAD_LAST_ERROR_AT = asyncio.get_running_loop().time()
    with connection() as db:
        db.execute("UPDATE tasks SET status = 'WAITING_RATE_LIMIT', download_wait_until = ?, speed_bytes_per_second = 0, updated_at = ? WHERE status IN ('DOWNLOADING', 'RETRYING')", (DOWNLOAD_FLOOD_UNTIL.isoformat(), now()))


def cancel_task_downloads(task_id: int) -> None:
    """Interrupt active transfers for an explicit pause/cancel action."""
    for (running_task_id, _), worker in tuple(DOWNLOAD_RUNNING.items()):
        if running_task_id == task_id:
            worker.cancel()


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


async def download_media_job(task_id: int, media_id: int) -> None:
    try:
        with connection() as db:
            task = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            media = db.execute("SELECT * FROM task_media WHERE id = ? AND task_id = ?", (media_id, task_id)).fetchone()
        if not task or not media or current_task_status(task_id) not in {"DOWNLOADING", "RETRYING"}:
            return
        client = await get_download_client()
        async with TELEGRAM_LOCK:
            entity = await client.get_entity(int(task["chat_id"]))
            message = await client.get_messages(entity, ids=media["message_id"])
        if not message:
            raise RuntimeError("消息已不可用")
        destination_dir = Path(DOWNLOAD_ROOT) / str(task_id) / safe_filename(task["chat_title"])
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / media["filename"]
        part = destination.with_suffix(destination.suffix + ".part")
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
                update_media(task_id, media_id, status="RETRY_WAIT", attempt_count=attempts, speed_bytes_per_second=0, next_retry_at=retry_at.isoformat(), failure_category=category, error_message=f"{message}，将在 {RETRY_DELAYS_SECONDS[attempts - 1]} 秒后自动重试（第 {attempts}/3 次）")
        else:
            update_media(task_id, media_id, status="FAILED", speed_bytes_per_second=0, failure_category=category, error_message=message)
    finally:
        DOWNLOAD_RUNNING.pop((task_id, media_id), None)
        finalize_task_status(task_id)
        notify_download_dispatcher()


async def download_dispatcher() -> None:
    global DOWNLOAD_EFFECTIVE_CONCURRENCY, DOWNLOAD_FLOOD_UNTIL, DOWNLOAD_LAST_ERROR_AT, DOWNLOAD_ROUND_ROBIN_OFFSET
    while True:
        try:
            max_concurrency = configured_download_concurrency()
            DOWNLOAD_EFFECTIVE_CONCURRENCY = min(DOWNLOAD_EFFECTIVE_CONCURRENCY, max_concurrency)
            current_time = datetime.now(UTC)
            if DOWNLOAD_FLOOD_UNTIL and current_time >= DOWNLOAD_FLOOD_UNTIL:
                DOWNLOAD_FLOOD_UNTIL = None
                with connection() as db:
                    db.execute("UPDATE tasks SET status = 'DOWNLOADING', download_wait_until = NULL, updated_at = ? WHERE status = 'WAITING_RATE_LIMIT'", (now(),))
                DOWNLOAD_LAST_ERROR_AT = asyncio.get_running_loop().time()
            if not DOWNLOAD_FLOOD_UNTIL and DOWNLOAD_LAST_ERROR_AT is not None:
                monotonic_now = asyncio.get_running_loop().time()
                if monotonic_now - DOWNLOAD_LAST_ERROR_AT >= STABILITY_WINDOW_SECONDS and DOWNLOAD_EFFECTIVE_CONCURRENCY < max_concurrency:
                    DOWNLOAD_EFFECTIVE_CONCURRENCY += 1
                    DOWNLOAD_LAST_ERROR_AT = monotonic_now
            slots = max(0, DOWNLOAD_EFFECTIVE_CONCURRENCY - len(DOWNLOAD_RUNNING))
            if slots and not DOWNLOAD_FLOOD_UNTIL and (DEMO_MODE or app_state()["accountConnected"]):
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
        except Exception:
            # Keep the dispatcher alive; individual jobs expose their own error state.
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
            matched = take_cached_scan(payload) or await scan_messages(payload)
            with connection() as db:
                db.executemany("""INSERT INTO task_media (task_id, message_id, filename, media_type, mime_type, size_bytes, message_date)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""", [(task_id, *item) for item in matched])
            update_task(task_id, status="DOWNLOADING", total_count=len(matched), total_bytes=sum(item[4] for item in matched), completed_count=0, downloaded_bytes=0, failed_count=0)
            if not matched:
                finalize_task_status(task_id)
        notify_download_dispatcher()
        return
    except asyncio.CancelledError:
        raise
    except Exception as error:
        update_task(task_id, status="FAILED", current_file=None, speed_bytes_per_second=0, error_message=f"创建下载清单失败：{str(error) or '请检查 Telegram 连接后重试'}")
    finally:
        TASK_WORKERS.pop(task_id, None)


def start_task_worker(task_id: int) -> None:
    worker = TASK_WORKERS.get(task_id)
    if not worker or worker.done():
        TASK_WORKERS[task_id] = asyncio.create_task(run_task_worker(task_id), name=f"telegram-download-{task_id}")


@app.post("/api/tasks")
async def create_task(payload: ScanRequest) -> dict[str, Any]:
    created = now()
    with connection() as db:
        cursor = db.execute("""INSERT INTO tasks (chat_id, chat_title, chat_handle, filters_json, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'QUEUED', ?, ?)""", (payload.chat_id, payload.chat_title, payload.chat_handle, payload.filters.model_dump_json(), created, created))
    start_task_worker(cursor.lastrowid)
    return get_task(cursor.lastrowid)


@app.post("/api/tasks/{task_id}/pause")
async def pause_task(task_id: int) -> dict[str, Any]:
    with connection() as db:
        db.execute("UPDATE tasks SET status = 'PAUSED', updated_at = ? WHERE id = ?", (now(), task_id))
    cancel_task_downloads(task_id)
    notify_download_dispatcher()
    return get_task(task_id)


@app.post("/api/tasks/{task_id}/resume")
async def resume_task(task_id: int) -> dict[str, Any]:
    with connection() as db:
        db.execute("UPDATE tasks SET status = 'DOWNLOADING', updated_at = ? WHERE id = ?", (now(), task_id))
    start_task_worker(task_id)
    notify_download_dispatcher()
    return get_task(task_id)


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: int) -> dict[str, Any]:
    with connection() as db:
        db.execute("UPDATE tasks SET status = 'CANCELLED', updated_at = ? WHERE id = ?", (now(), task_id))
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
