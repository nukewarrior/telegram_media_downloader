from __future__ import annotations

import asyncio
import json
import os
import secrets
import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
DB_PATH = DATA_DIR / "app.db"
DOWNLOAD_ROOT = os.getenv("DOWNLOAD_ROOT", str(DATA_DIR / "downloads"))
DEMO_MODE = os.getenv("DEMO_MODE", "true").lower() == "true"
STATIC_DIR = Path(os.getenv("STATIC_DIR", "./static"))


def now() -> str:
    return datetime.now(UTC).isoformat()


def connection() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def rows(items: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(item) for item in items]


def initialize_database() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    Path(DOWNLOAD_ROOT).mkdir(parents=True, exist_ok=True)
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
            """
        )
        if DEMO_MODE and db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0:
            seed_demo_data(db)


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
        blob = db.execute(
            """INSERT INTO media_blobs (content_hash, canonical_path, thumbnail_status, size_bytes, media_type, created_at)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (f"demo-hash-{index}", canonical_path, "READY" if media_type in {"PHOTO", "VIDEO"} else "UNAVAILABLE", size, media_type, created),
        )
        db.execute(
            """INSERT INTO archive_items (blob_id, chat_id, chat_title, message_id, filename, media_type, mime_type,
            size_bytes, message_date, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (blob.lastrowid, f"demo-{title}", title, 5000 + index, filename, media_type,
             "image/jpeg" if media_type == "PHOTO" else "video/mp4" if media_type == "VIDEO" else "application/pdf",
             size, f"{month}-{18-index:02d}T09:30:00+00:00", created),
        )


class ApiCredentials(BaseModel):
    api_id: str = Field(min_length=1, max_length=32)
    api_hash: str = Field(min_length=16, max_length=128)


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
    return {
        "configured": configured,
        "accountConnected": bool(setting["account_connected"]),
        "accountName": setting["account_name"],
        "downloadRoot": DOWNLOAD_ROOT,
        "demoMode": DEMO_MODE,
    }


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    yield


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
    return {
        "apiId": masked_api_id(setting["api_id"]),
        "apiHashConfigured": bool(setting["api_hash"]),
        "accountConnected": bool(setting["account_connected"]),
        "accountName": setting["account_name"],
        "accountPhone": setting["account_phone"],
        "downloadRoot": DOWNLOAD_ROOT,
        "trustedLanWarning": "当前服务未启用应用层认证。仅应部署在可信局域网中。",
    }


@app.put("/api/settings/api")
def update_api_credentials(payload: ApiCredentials) -> dict[str, Any]:
    return save_setup(payload)


@app.post("/api/telegram/login/send-code")
def send_login_code(payload: LoginCodeRequest) -> dict[str, Any]:
    if not app_state()["configured"]:
        raise HTTPException(409, "请先配置 Telegram API 凭据")
    attempt_id = secrets.token_urlsafe(18)
    with connection() as db:
        db.execute("INSERT INTO login_attempts (id, phone, created_at) VALUES (?, ?, ?)", (attempt_id, payload.phone, now()))
    return {"attemptId": attempt_id, "passwordRequired": False, "demoHint": "演示模式下可输入任意六码验证码" if DEMO_MODE else None}


@app.post("/api/telegram/login/verify-code")
def verify_login_code(payload: LoginCodeVerify) -> dict[str, Any]:
    with connection() as db:
        attempt = db.execute("SELECT * FROM login_attempts WHERE id = ?", (payload.attempt_id,)).fetchone()
        if not attempt:
            raise HTTPException(404, "登录会话已过期")
        if not DEMO_MODE:
            raise HTTPException(501, "Telegram 网关尚未启用；请将 Telethon 网关接入此部署")
        db.execute(
            "UPDATE app_settings SET account_phone = ?, account_name = ?, account_connected = 1, updated_at = ? WHERE id = 1",
            (attempt["phone"], "已连接的 Telegram 账号", now()),
        )
        db.execute("DELETE FROM login_attempts WHERE id = ?", (payload.attempt_id,))
    return app_state()


@app.post("/api/telegram/login/verify-password")
def verify_login_password(payload: LoginPasswordVerify) -> dict[str, Any]:
    return verify_login_code(LoginCodeVerify(attempt_id=payload.attempt_id, code="password"))


@app.post("/api/telegram/logout")
def logout() -> dict[str, Any]:
    with connection() as db:
        db.execute("UPDATE app_settings SET account_connected = 0, account_name = NULL, account_phone = NULL, updated_at = ? WHERE id = 1", (now(),))
    return app_state()


@app.get("/api/chats")
def list_chats() -> list[dict[str, Any]]:
    if not app_state()["accountConnected"]:
        raise HTTPException(409, "请先连接 Telegram 账号")
    return [
        {"id": "demo-tech", "title": "科技前沿观察", "handle": "@tech_frontier_obs", "type": "CHANNEL"},
        {"id": "demo-design", "title": "设计灵感库", "handle": "@design_inspiration_cn", "type": "CHANNEL"},
        {"id": "demo-study", "title": "学习资料库", "handle": "@study_materials_zh", "type": "GROUP"},
    ]


@app.get("/api/tasks")
def list_tasks() -> list[dict[str, Any]]:
    with connection() as db:
        records = db.execute("SELECT * FROM tasks ORDER BY CASE status WHEN 'DOWNLOADING' THEN 0 WHEN 'PAUSED' THEN 1 ELSE 2 END, updated_at DESC").fetchall()
    return [{**dict(record), "filters": json.loads(record["filters_json"])} for record in records]


@app.get("/api/tasks/{task_id}")
def get_task(task_id: int) -> dict[str, Any]:
    with connection() as db:
        record = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not record:
        raise HTTPException(404, "任务不存在")
    return {**dict(record), "filters": json.loads(record["filters_json"])}


@app.post("/api/tasks/scan")
def scan_task(payload: ScanRequest) -> dict[str, Any]:
    # The scan contract is intentionally usable before the Telegram worker lands.
    seed = sum(ord(char) for char in payload.chat_id)
    total_count = 160 + seed % 870
    total_bytes = total_count * (4_000_000 + seed % 10_000_000)
    return {"chat": payload.model_dump(exclude={"filters"}), "filters": payload.filters.model_dump(), "totalCount": total_count, "totalBytes": total_bytes, "duplicateCount": seed % 24}


@app.post("/api/tasks")
def create_task(payload: ScanRequest) -> dict[str, Any]:
    preview = scan_task(payload)
    created = now()
    with connection() as db:
        cursor = db.execute(
            """INSERT INTO tasks (chat_id, chat_title, chat_handle, filters_json, status, total_count, total_bytes, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'QUEUED', ?, ?, ?, ?)""",
            (payload.chat_id, payload.chat_title, payload.chat_handle, payload.filters.model_dump_json(), preview["totalCount"], preview["totalBytes"], created, created),
        )
    return get_task(cursor.lastrowid)


@app.post("/api/tasks/{task_id}/pause")
def pause_task(task_id: int) -> dict[str, Any]:
    with connection() as db:
        db.execute("UPDATE tasks SET status = 'PAUSED', updated_at = ? WHERE id = ?", (now(), task_id))
    return get_task(task_id)


@app.post("/api/tasks/{task_id}/resume")
def resume_task(task_id: int) -> dict[str, Any]:
    with connection() as db:
        db.execute("UPDATE tasks SET status = 'DOWNLOADING', updated_at = ? WHERE id = ?", (now(), task_id))
    return get_task(task_id)


@app.post("/api/tasks/{task_id}/cancel")
def cancel_task(task_id: int) -> dict[str, Any]:
    with connection() as db:
        db.execute("UPDATE tasks SET status = 'CANCELLED', updated_at = ? WHERE id = ?", (now(), task_id))
    return get_task(task_id)


@app.post("/api/tasks/{task_id}/retry")
def retry_task(task_id: int) -> dict[str, Any]:
    with connection() as db:
        db.execute("UPDATE tasks SET status = 'QUEUED', error_message = NULL, updated_at = ? WHERE id = ?", (now(), task_id))
    return get_task(task_id)


@app.get("/api/tasks/{task_id}/events")
async def task_events(request: Request, task_id: int) -> StreamingResponse:
    async def event_stream():
        while not await request.is_disconnected():
            try:
                task = get_task(task_id)
                yield f"event: task\\ndata: {json.dumps(task)}\\n\\n"
            except HTTPException:
                yield "event: error\\ndata: {\"message\":\"任务不存在\"}\\n\\n"
                return
            await asyncio.sleep(1)
    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/api/archives/chats")
def archive_chats() -> list[dict[str, Any]]:
    with connection() as db:
        records = db.execute("SELECT chat_id AS id, chat_title AS title, COUNT(*) AS item_count FROM archive_items GROUP BY chat_id, chat_title ORDER BY title").fetchall()
    return rows(records)


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
            f"""SELECT a.*, b.canonical_path, b.thumbnail_path, b.thumbnail_status
            FROM archive_items a JOIN media_blobs b ON a.blob_id = b.id {where}
            ORDER BY a.message_date DESC""", values).fetchall()
    return rows(records)


@app.get("/api/archives/media/{item_id}")
def archive_media_detail(item_id: int) -> dict[str, Any]:
    with connection() as db:
        record = db.execute(
            """SELECT a.*, b.canonical_path, b.thumbnail_path, b.thumbnail_status FROM archive_items a
            JOIN media_blobs b ON a.blob_id = b.id WHERE a.id = ?""", (item_id,)
        ).fetchone()
    if not record:
        raise HTTPException(404, "归档项目不存在")
    return dict(record)


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
