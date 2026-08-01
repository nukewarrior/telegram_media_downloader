from __future__ import annotations

import asyncio
import os
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx


class StorageError(RuntimeError):
    """A destination could not be read or written."""


class _AsyncFileStream(httpx.AsyncByteStream):
    """Read a local staging file without blocking the event loop or loading it all into memory."""

    def __init__(self, path: Path, chunk_size: int = 1024 * 1024) -> None:
        self.path = path
        self.chunk_size = chunk_size

    async def __aiter__(self):
        handle = await asyncio.to_thread(self.path.open, "rb")
        try:
            while True:
                chunk = await asyncio.to_thread(handle.read, self.chunk_size)
                if not chunk:
                    break
                yield chunk
        finally:
            await asyncio.to_thread(handle.close)


def _relative_text(relative: str | Path) -> str:
    value = str(relative).replace(os.sep, "/").strip("/")
    if not value or value in {".", ".."} or any(part in {"", ".", ".."} for part in value.split("/")):
        raise StorageError("归档相对路径无效")
    return value


def _safe_local_path(root: Path, relative: str | Path) -> Path:
    candidate = (root / _relative_text(relative)).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as error:
        raise StorageError("归档路径超出目的地目录") from error
    return candidate


@dataclass(frozen=True)
class Destination:
    id: int
    name: str
    kind: str
    local_root: Path | None = None
    webdav_url: str | None = None
    webdav_username: str | None = None
    webdav_password: str | None = None
    remote_root: str = ""
    enabled: bool = True
    is_system: bool = False

    @classmethod
    def from_row(cls, row: object) -> "Destination":
        get = row.__getitem__  # type: ignore[attr-defined]
        local_root = get("local_root")
        return cls(
            id=int(get("id")),
            name=str(get("name")),
            kind=str(get("kind")),
            local_root=Path(local_root) if local_root else None,
            webdav_url=get("webdav_url"),
            webdav_username=get("webdav_username"),
            webdav_password=get("webdav_password"),
            remote_root=str(get("remote_root") or ""),
            enabled=bool(get("enabled")),
            is_system=bool(get("is_system")),
        )

    @property
    def is_local(self) -> bool:
        return self.kind == "LOCAL"

    def local_path(self, relative: str | Path) -> Path:
        if not self.is_local or not self.local_root:
            raise StorageError("该目的地不是本地目录")
        return _safe_local_path(self.local_root, relative)

    def resolve_local_location(self, persisted_path: str | Path) -> Path:
        """Resolve both new relative locations and legacy absolute paths."""
        if not self.is_local or not self.local_root:
            raise StorageError("该目的地不是本地目录")
        candidate = Path(persisted_path)
        if not candidate.is_absolute():
            return self.local_path(candidate)
        candidate = candidate.resolve()
        try:
            candidate.relative_to(self.local_root.resolve())
        except ValueError as error:
            raise StorageError("归档路径超出目的地目录") from error
        return candidate

    def _service_url(self) -> str:
        if self.is_local or not self.webdav_url:
            raise StorageError("该目的地不是 WebDAV")
        parsed = urlsplit(self.webdav_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
            raise StorageError("WebDAV URL 无效，必须包含 http 或 https 地址")
        return self.webdav_url.rstrip("/")

    def _remote_root_parts(self) -> list[str]:
        root = self.remote_root.strip("/")
        if not root:
            return []
        try:
            return _relative_text(root).split("/")
        except StorageError as error:
            raise StorageError("WebDAV 根路径无效") from error

    def _base_url(self) -> str:
        base = self._service_url()
        root_parts = self._remote_root_parts()
        if root_parts:
            encoded_root = "/".join(quote(part, safe="-._~") for part in root_parts)
            base = f"{base}/{encoded_root}"
        return base

    def remote_url(self, relative: str | Path) -> str:
        relative_text = _relative_text(relative)
        encoded = "/".join(quote(part, safe="-._~") for part in relative_text.split("/"))
        return f"{self._base_url()}/{encoded}"

    def auth(self) -> httpx.BasicAuth | None:
        if self.webdav_username is None:
            return None
        return httpx.BasicAuth(self.webdav_username, self.webdav_password or "")

    async def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            auth=self.auth(),
            follow_redirects=True,
            timeout=httpx.Timeout(60.0, connect=15.0),
        )

    @staticmethod
    def _check_response(response: httpx.Response, operation: str, *, accepted: set[int] | None = None) -> None:
        accepted_statuses = accepted or set(range(200, 300))
        if response.status_code not in accepted_statuses:
            try:
                detail = response.text.strip()[:160]
            except Exception:
                detail = ""
            suffix = f"：{detail}" if detail else ""
            if response.status_code in {401, 403}:
                raise StorageError(f"WebDAV {operation}权限不足（HTTP {response.status_code}）{suffix}")
            raise StorageError(f"WebDAV {operation}失败（HTTP {response.status_code}）{suffix}")

    @staticmethod
    async def _propfind_collection(client: httpx.AsyncClient, url: str) -> httpx.Response:
        return await client.request("PROPFIND", url, headers={"Depth": "0"})

    async def _require_collection(self, client: httpx.AsyncClient, url: str, operation: str) -> None:
        response = await self._propfind_collection(client, url)
        if 200 <= response.status_code < 300:
            return
        if response.status_code in {401, 403}:
            self._check_response(response, operation)
        if response.status_code == 404:
            raise StorageError(f"WebDAV {operation}不存在（HTTP 404）")
        self._check_response(response, operation)

    async def _ensure_collection(self, client: httpx.AsyncClient, url: str, operation: str) -> None:
        response = await self._propfind_collection(client, url)
        if 200 <= response.status_code < 300:
            return
        if response.status_code in {401, 403}:
            self._check_response(response, operation)
        if response.status_code != 404:
            self._check_response(response, operation)

        response = await client.request("MKCOL", url)
        if 200 <= response.status_code < 300:
            return
        if response.status_code == 405:
            confirmation = await self._propfind_collection(client, url)
            if 200 <= confirmation.status_code < 300:
                return
            if confirmation.status_code in {401, 403}:
                self._check_response(confirmation, operation)
            raise StorageError(f"WebDAV {operation}创建后无法确认目录（HTTP {confirmation.status_code}）")
        if response.status_code in {401, 403}:
            self._check_response(response, f"创建{operation}")
        self._check_response(response, f"创建{operation}")

    async def _ensure_remote_directories(self, client: httpx.AsyncClient, relative: str | Path) -> None:
        relative_text = _relative_text(relative)
        service_url = self._service_url()
        await self._require_collection(client, service_url, "WebDAV 服务入口")
        current_url = service_url
        for part in self._remote_root_parts():
            current_url = f"{current_url}/{quote(part, safe='-._~')}"
            await self._ensure_collection(client, current_url, "远端根目录")
        parents = relative_text.split("/")[:-1]
        for index in range(1, len(parents) + 1):
            directory = "/".join(parents[:index])
            await self._ensure_collection(client, self.remote_url(directory), "归档目录")

    async def _best_effort_delete(self, client: httpx.AsyncClient, relative: str) -> None:
        try:
            response = await client.request("DELETE", self.remote_url(relative))
            if response.status_code not in set(range(200, 300)) | {404}:
                return
        except httpx.HTTPError:
            return

    async def test_connection(self) -> None:
        if self.is_local:
            if not self.local_root:
                raise StorageError("本地目的地缺少目录")
            try:
                self.local_root.mkdir(parents=True, exist_ok=True)
                probe = self.local_root / ".telegram-media-archiver-write-test"
                await asyncio.to_thread(probe.write_bytes, b"ok")
                await asyncio.to_thread(probe.unlink, missing_ok=True)
            except OSError as error:
                raise StorageError(f"本地目的地不可写：{error}") from error
            return
        try:
            async with await self._client() as client:
                probe_relative = f".telegram-media-archiver-probe-{secrets.token_hex(8)}"
                temporary_relative = f"{probe_relative}.part"
                await self._ensure_remote_directories(client, probe_relative)
                try:
                    response = await client.put(self.remote_url(temporary_relative), content=b"telegram-media-archiver-probe")
                    self._check_response(response, "上传连接测试文件")
                    response = await client.request(
                        "MOVE",
                        self.remote_url(temporary_relative),
                        headers={"Destination": self.remote_url(probe_relative), "Overwrite": "T"},
                    )
                    self._check_response(response, "提交连接测试文件")
                    response = await client.request("DELETE", self.remote_url(probe_relative))
                    self._check_response(response, "清理连接测试文件")
                except Exception:
                    await self._best_effort_delete(client, probe_relative)
                    await self._best_effort_delete(client, temporary_relative)
                    raise
        except httpx.HTTPError as error:
            raise StorageError(f"无法连接 WebDAV：{error}") from error

    async def upload_file(self, source: Path, relative: str | Path) -> None:
        if self.is_local:
            target = self.local_path(relative)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(os.replace, source, target)
            except OSError as error:
                raise StorageError(f"本地目的地写入失败：{error}") from error
            return

        relative_text = _relative_text(relative)
        temporary_relative = f"{relative_text}.part"
        try:
            async with await self._client() as client:
                await self._ensure_remote_directories(client, relative_text)
                response = await client.put(self.remote_url(temporary_relative), content=_AsyncFileStream(source))
                self._check_response(response, "上传文件")
                response = await client.request(
                    "MOVE",
                    self.remote_url(temporary_relative),
                    headers={"Destination": self.remote_url(relative_text), "Overwrite": "T"},
                )
                self._check_response(response, "提交文件", accepted={200, 201, 204})
        except OSError as error:
            raise StorageError(f"读取本地临时文件失败：{error}") from error
        except httpx.HTTPError as error:
            raise StorageError(f"WebDAV 上传失败：{error}") from error

    async def download_to_file(self, relative: str | Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if self.is_local:
            source = self.local_path(relative)
            if not source.is_file():
                raise StorageError("归档文件不存在")
            try:
                await asyncio.to_thread(shutil.copyfile, source, target)
            except OSError as error:
                raise StorageError(f"读取本地归档失败：{error}") from error
            return
        try:
            async with await self._client() as client:
                async with client.stream("GET", self.remote_url(relative)) as response:
                    self._check_response(response, "下载文件")
                    with target.open("wb") as handle:
                        async for chunk in response.aiter_bytes():
                            handle.write(chunk)
        except OSError as error:
            raise StorageError(f"写入本地临时文件失败：{error}") from error
        except httpx.HTTPError as error:
            raise StorageError(f"WebDAV 下载失败：{error}") from error

    async def open_remote_stream(self, relative: str | Path, range_header: str | None) -> tuple[httpx.AsyncClient, httpx.Response]:
        if self.is_local:
            raise StorageError("本地目的地不需要远程流")
        client = await self._client()
        try:
            request = client.build_request("GET", self.remote_url(relative), headers={"Range": range_header} if range_header else None)
            response = await client.send(request, stream=True)
            if response.status_code >= 400:
                try:
                    self._check_response(response, "读取文件")
                finally:
                    await response.aclose()
            if range_header and response.status_code != 206:
                await response.aclose()
                raise StorageError("WebDAV 未按要求返回范围内容")
            return client, response
        except Exception:
            await client.aclose()
            raise

    async def close_remote_stream(self, client: httpx.AsyncClient, response: httpx.Response) -> None:
        await response.aclose()
        await client.aclose()
