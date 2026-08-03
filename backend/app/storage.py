from __future__ import annotations

import asyncio
import errno
import os
import re
import secrets
import shutil
import stat
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlsplit

import httpx


class StorageError(RuntimeError):
    """A destination could not be read or written."""


@dataclass
class DirectoryCacheMetrics:
    """Per-upload WebDAV directory cache lookup counts."""

    hits: int = 0
    misses: int = 0


_LAST_UPLOAD_DIRECTORY_CACHE_METRICS: ContextVar[DirectoryCacheMetrics | None] = ContextVar(
    "last_upload_directory_cache_metrics",
    default=None,
)


def clear_last_directory_cache_metrics() -> None:
    """Forget the previous upload's metrics in the current async context."""
    _LAST_UPLOAD_DIRECTORY_CACHE_METRICS.set(None)


def get_last_directory_cache_metrics() -> DirectoryCacheMetrics | None:
    """Return the current task's most recent WebDAV upload metrics."""
    return _LAST_UPLOAD_DIRECTORY_CACHE_METRICS.get()


_DIRECTORY_STATE_INVALIDATION_STATUSES = frozenset({404, 405, 409, 410, 412, 423})
_UPLOAD_DIRECTORY_RETRY_STATUSES = frozenset({404, 409, 410, 412, 423})
_ARCHIVE_YEAR_RE = re.compile(r"\d{4}\Z")
_ARCHIVE_MONTH_RE = re.compile(r"(?:0[1-9]|1[0-2])\Z")
_WEBDAV_PROPFIND_BODY = b"""<?xml version=\"1.0\" encoding=\"utf-8\"?>
<D:propfind xmlns:D=\"DAV:\"><D:prop><D:resourcetype/></D:prop></D:propfind>"""
_MAX_WEBDAV_DIRECTORY_RESPONSE_BYTES = 512 * 1024
_MAX_WEBDAV_DIRECTORY_ENTRIES = 2048


class _WebDAVResponseError(StorageError):
    """A WebDAV response that callers may classify without parsing its message."""

    def __init__(self, message: str, *, status_code: int, operation: str, url: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.operation = operation
        self.url = url


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


def _single_relative_segment(value: str | Path) -> str:
    if Path(value).is_absolute():
        raise StorageError("聊天归档目录必须是安全相对路径")
    text = _relative_text(value)
    if "/" in text:
        raise StorageError("聊天归档目录必须是单个路径段")
    return text


def _local_directory_is_empty(path: Path) -> bool:
    try:
        with os.scandir(path) as entries:
            return next(entries, None) is None
    except FileNotFoundError:
        return True


def _remove_empty_local_directory(path: Path) -> bool:
    try:
        path.rmdir()
        return True
    except FileNotFoundError:
        return True
    except OSError as error:
        if error.errno in {errno.ENOTEMPTY, errno.EEXIST}:
            return False
        raise


def _cleanup_local_empty_chat_tree(root: Path) -> None:
    """Remove only an empty archive chat tree, without following symlinks."""
    if root.is_symlink() or not root.is_dir():
        return

    root_blocked = False
    with os.scandir(root) as year_entries:
        years = list(year_entries)
    for year_entry in years:
        if year_entry.is_symlink() or not year_entry.is_dir(follow_symlinks=False) or not _ARCHIVE_YEAR_RE.fullmatch(year_entry.name):
            root_blocked = True
            continue

        year_path = Path(year_entry.path)
        year_blocked = False
        with os.scandir(year_path) as month_entries:
            months = list(month_entries)
        for month_entry in months:
            if month_entry.is_symlink() or not month_entry.is_dir(follow_symlinks=False) or not _ARCHIVE_MONTH_RE.fullmatch(month_entry.name):
                year_blocked = True
                continue
            month_path = Path(month_entry.path)
            if _local_directory_is_empty(month_path):
                if not _remove_empty_local_directory(month_path):
                    year_blocked = True
            else:
                year_blocked = True

        if not year_blocked and _local_directory_is_empty(year_path):
            if not _remove_empty_local_directory(year_path):
                root_blocked = True
        else:
            root_blocked = True

    if not root_blocked and _local_directory_is_empty(root):
        _remove_empty_local_directory(root)


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


@dataclass(frozen=True)
class _WebDAVResource:
    name: str
    is_collection: bool


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
    # A destination version is immutable and is the primary identity for a
    # persistent WebDAV client.  The optional value keeps hand-built test
    # destinations and legacy rows compatible; those use configuration
    # identity as a fallback in WebDAVClientManager.
    version_id: int | None = None

    @classmethod
    def from_row(cls, row: object) -> "Destination":
        get = row.__getitem__  # type: ignore[attr-defined]
        local_root = get("local_root")
        columns = row.keys() if hasattr(row, "keys") else ()  # type: ignore[attr-defined]
        version_id = get("destination_version_id") if "destination_version_id" in columns else None
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
            version_id=int(version_id) if version_id is not None else None,
        )

    @property
    def is_local(self) -> bool:
        return self.kind == "LOCAL"

    def storage_namespace(self) -> tuple[str, ...]:
        """Identify the physical archive namespace used by in-process guards."""
        if self.is_local:
            if not self.local_root:
                raise StorageError("本地目的地缺少目录")
            return ("LOCAL", str(self.local_root.resolve()))
        return ("WEBDAV", self._service_url(), "/".join(self._remote_root_parts()))

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
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=10,
                keepalive_expiry=30.0,
            ),
        )

    def webdav_client_cache_key(self) -> tuple[object, ...]:
        """Return the immutable identity used by the persistent client cache.

        Persisted work carries a destination version, so its cache identity
        must never collapse to the mutable destination id.  Hand-built test
        destinations do not have a version id; their complete connection
        configuration is a stable fallback identity instead.
        """
        if self.is_local:
            raise StorageError("本地目的地不需要 WebDAV 客户端")
        if self.version_id is not None:
            return ("version", self.id, self.version_id)
        return (
            "configuration",
            self._service_url(),
            self.webdav_username,
            self.webdav_password or "",
            "/".join(self._remote_root_parts()),
        )

    @staticmethod
    def _response_url(response: httpx.Response) -> str | None:
        try:
            request = response.request
        except RuntimeError:
            return None
        return str(request.url) if request is not None else None

    @classmethod
    def _check_response(cls, response: httpx.Response, operation: str, *, accepted: set[int] | None = None) -> None:
        accepted_statuses = accepted or set(range(200, 300))
        if response.status_code not in accepted_statuses:
            try:
                detail = response.text.strip()[:160]
            except Exception:
                detail = ""
            suffix = f"：{detail}" if detail else ""
            if response.status_code in {401, 403}:
                raise _WebDAVResponseError(
                    f"WebDAV {operation}权限不足（HTTP {response.status_code}）{suffix}",
                    status_code=response.status_code,
                    operation=operation,
                    url=cls._response_url(response),
                )
            raise _WebDAVResponseError(
                f"WebDAV {operation}失败（HTTP {response.status_code}）{suffix}",
                status_code=response.status_code,
                operation=operation,
                url=cls._response_url(response),
            )

    @staticmethod
    async def _propfind_collection(client: httpx.AsyncClient, url: str) -> httpx.Response:
        return await client.request("PROPFIND", url, headers={"Depth": "0"})

    async def _require_collection(
        self,
        client: httpx.AsyncClient,
        url: str,
        operation: str,
        *,
        directory_cache: "WebDAVClientManager | None" = None,
    ) -> None:
        if directory_cache is not None:
            await directory_cache.require_collection(self, client, url, operation)
            return
        await self._require_collection_uncached(client, url, operation)

    async def _require_collection_uncached(self, client: httpx.AsyncClient, url: str, operation: str) -> None:
        response = await self._propfind_collection(client, url)
        if 200 <= response.status_code < 300:
            return
        if response.status_code in {401, 403}:
            self._check_response(response, operation)
        if response.status_code == 404:
            raise _WebDAVResponseError(
                f"WebDAV {operation}不存在（HTTP 404）",
                status_code=response.status_code,
                operation=operation,
                url=self._response_url(response),
            )
        self._check_response(response, operation)

    async def _ensure_collection(
        self,
        client: httpx.AsyncClient,
        url: str,
        operation: str,
        *,
        directory_cache: "WebDAVClientManager | None" = None,
    ) -> None:
        if directory_cache is not None:
            await directory_cache.ensure_collection(self, client, url, operation)
            return
        await self._ensure_collection_uncached(client, url, operation)

    async def _ensure_collection_uncached(self, client: httpx.AsyncClient, url: str, operation: str) -> None:
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
            raise _WebDAVResponseError(
                f"WebDAV {operation}创建后无法确认目录（HTTP {confirmation.status_code}）",
                status_code=confirmation.status_code,
                operation=operation,
                url=self._response_url(confirmation),
            )
        if response.status_code in {401, 403}:
            self._check_response(response, f"创建{operation}")
        self._check_response(response, f"创建{operation}")

    async def _ensure_remote_directories(
        self,
        client: httpx.AsyncClient,
        relative: str | Path,
        *,
        directory_cache: "WebDAVClientManager | None" = None,
    ) -> None:
        relative_text = _relative_text(relative)
        service_url = self._service_url()
        await self._require_collection(client, service_url, "WebDAV 服务入口", directory_cache=directory_cache)
        current_url = service_url
        for part in self._remote_root_parts():
            current_url = f"{current_url}/{quote(part, safe='-._~')}"
            await self._ensure_collection(client, current_url, "远端根目录", directory_cache=directory_cache)
        parents = relative_text.split("/")[:-1]
        for index in range(1, len(parents) + 1):
            directory = "/".join(parents[:index])
            await self._ensure_collection(client, self.remote_url(directory), "归档目录", directory_cache=directory_cache)

    def _remote_parent_url(self, relative: str) -> str:
        parent = relative.rsplit("/", 1)[0] if "/" in relative else ""
        return self.remote_url(parent) if parent else self._base_url()

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
            clear_last_directory_cache_metrics()
            target = self.local_path(relative)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(os.replace, source, target)
            except OSError as error:
                raise StorageError(f"本地目的地写入失败：{error}") from error
            return

        relative_text = _relative_text(relative)
        temporary_relative = f"{relative_text}.part"
        manager = webdav_client_manager
        cache_metrics: DirectoryCacheMetrics | None = None
        try:
            async with manager.collect_directory_cache_metrics() as collected_metrics:
                cache_metrics = collected_metrics
                client = await manager.get_client(self)
                for attempt in range(2):
                    try:
                        await self._ensure_remote_directories(client, relative_text, directory_cache=manager)
                        response = await client.put(self.remote_url(temporary_relative), content=_AsyncFileStream(source))
                        self._check_response(response, "上传文件")
                        response = await client.request(
                            "MOVE",
                            self.remote_url(temporary_relative),
                            headers={"Destination": self.remote_url(relative_text), "Overwrite": "T"},
                        )
                        self._check_response(response, "提交文件", accepted={200, 201, 204})
                        break
                    except _WebDAVResponseError as error:
                        if attempt or not self._should_retry_directory_state(error):
                            raise
                        manager.invalidate_directory(self, self._remote_parent_url(relative_text), include_ancestors=True)
        except OSError as error:
            raise StorageError(f"读取本地临时文件失败：{error}") from error
        except httpx.HTTPError as error:
            raise StorageError(f"WebDAV 上传失败：{error}") from error
        finally:
            _LAST_UPLOAD_DIRECTORY_CACHE_METRICS.set(cache_metrics)

    async def delete_file(self, relative: str | Path) -> None:
        """Delete exactly one archived regular file.

        This method is intentionally separate from the best-effort cleanup used
        by connection probes.  Archive deletion needs to surface permissions,
        transport failures, and unexpected server responses so the index can be
        kept for a retry instead of silently drifting from storage.
        """
        if Path(relative).is_absolute():
            raise StorageError("归档删除路径必须是安全相对路径")
        relative_text = _relative_text(relative)
        if relative_text.endswith(".part"):
            raise StorageError("不允许删除临时归档文件")
        if self.is_local:
            target = self.local_path(relative_text)
            try:
                info = await asyncio.to_thread(target.lstat)
                if not stat.S_ISREG(info.st_mode):
                    raise StorageError("归档位置不是普通文件")
                await asyncio.to_thread(target.unlink)
            except FileNotFoundError:
                return
            except StorageError:
                raise
            except OSError as error:
                raise StorageError(f"本地归档删除失败：{error}") from error
            return
        try:
            client = await webdav_client_manager.get_client(self)
            response = await client.request("DELETE", self.remote_url(relative_text))
            self._check_response(response, "删除归档文件", accepted=set(range(200, 300)) | {404})
        except httpx.HTTPError as error:
            raise StorageError(f"WebDAV 删除失败：{error}") from error

    @staticmethod
    def _normalize_webdav_candidate(target_url: str, candidate: str) -> tuple[str, list[str]]:
        target = urlsplit(target_url)
        parsed = urlsplit(candidate)
        if (
            parsed.scheme.casefold() != target.scheme.casefold()
            or parsed.netloc.casefold() != target.netloc.casefold()
            or parsed.query
            or parsed.fragment
        ):
            raise StorageError("WebDAV 目录响应链接超出当前存储范围")
        decoded_path = unquote(parsed.path)
        if not decoded_path.startswith("/"):
            raise StorageError("WebDAV 目录响应链接无效")
        raw_parts = decoded_path.split("/")
        if any(part == "" for part in raw_parts[1:-1]) or any(part in {".", ".."} for part in raw_parts):
            raise StorageError("WebDAV 目录响应链接包含非法路径段")
        parts = [part for part in raw_parts if part]
        normalized_path = "/" + "/".join(quote(part, safe="-._~") for part in parts)
        return f"{target.scheme}://{target.netloc}{normalized_path}".rstrip("/"), parts

    @classmethod
    def _normalize_webdav_href_variants(cls, target_url: str, href: str) -> list[tuple[str, list[str]]]:
        if not href or len(href) > 4096:
            raise StorageError("WebDAV 目录响应包含无效链接")
        raw_href = href.strip()
        href_path = unquote(urlsplit(raw_href).path)
        if any(part in {".", ".."} for part in href_path.split("/")):
            raise StorageError("WebDAV 目录响应链接包含非法路径段")
        parsed_href = urlsplit(raw_href)
        if parsed_href.scheme or parsed_href.netloc or href_path.startswith("/"):
            candidates = [urljoin(target_url.rstrip("/") + "/", raw_href)]
        else:
            candidates = [
                urljoin(target_url.rstrip("/") + "/", raw_href),
                urljoin(target_url.rsplit("/", 1)[0].rstrip("/") + "/", raw_href),
            ]
        variants: list[tuple[str, list[str]]] = []
        for candidate in candidates:
            normalized = cls._normalize_webdav_candidate(target_url, candidate)
            if normalized not in variants:
                variants.append(normalized)
        return variants

    @classmethod
    def _normalize_webdav_href(cls, target_url: str, href: str) -> tuple[str, list[str]]:
        return cls._normalize_webdav_href_variants(target_url, href)[0]

    @classmethod
    def _parse_webdav_directory_listing(
        cls,
        response: httpx.Response,
        target_url: str,
    ) -> tuple[bool, list[_WebDAVResource]] | None:
        if response.status_code == 404:
            return None
        if response.status_code != 207:
            cls._check_response(response, "读取 WebDAV 目录", accepted={207})
        if len(response.content) > _MAX_WEBDAV_DIRECTORY_RESPONSE_BYTES:
            raise StorageError("WebDAV 目录响应过大，已停止清理")
        try:
            document = ET.fromstring(response.content)
        except (ET.ParseError, ValueError) as error:
            raise StorageError("WebDAV 目录响应 XML 无法解析，已保留目录") from error

        normalized_target, target_parts = cls._normalize_webdav_href(target_url, target_url)
        target_is_collection: bool | None = None
        children: list[_WebDAVResource] = []
        seen_urls: set[str] = set()
        response_nodes = [element for element in document.iter() if _xml_local_name(element.tag) == "response"]
        if not response_nodes or len(response_nodes) > _MAX_WEBDAV_DIRECTORY_ENTRIES + 1:
            raise StorageError("WebDAV 目录响应内容不明确，已保留目录")
        for response_node in response_nodes:
            href_node = next((element for element in response_node.iter() if _xml_local_name(element.tag) == "href"), None)
            status_node = next((element for element in response_node.iter() if _xml_local_name(element.tag) == "status"), None)
            resource_type_node = next((element for element in response_node.iter() if _xml_local_name(element.tag) == "resourcetype"), None)
            if href_node is None or not (href_node.text or "").strip() or status_node is None or resource_type_node is None:
                raise StorageError("WebDAV 目录响应缺少可靠属性，已保留目录")
            status_text = (status_node.text or "").strip()
            if not re.search(r"\b2\d{2}\b", status_text):
                raise StorageError("WebDAV 目录响应包含异常资源状态，已保留目录")
            href_variants = cls._normalize_webdav_href_variants(target_url, (href_node.text or "").strip())
            normalized_url, parts = next(
                (
                    variant
                    for variant in href_variants
                    if variant[0] == normalized_target
                    or (len(variant[1]) == len(target_parts) + 1 and variant[1][: len(target_parts)] == target_parts)
                ),
                ("", []),
            )
            if not normalized_url:
                raise StorageError("WebDAV Depth:1 响应越出当前目录，已保留目录")
            if normalized_url in seen_urls:
                raise StorageError("WebDAV 目录响应包含重复资源，已保留目录")
            seen_urls.add(normalized_url)
            is_collection = any(_xml_local_name(element.tag) == "collection" for element in resource_type_node.iter())
            if normalized_url == normalized_target:
                if target_is_collection is not None:
                    raise StorageError("WebDAV 目录响应包含重复目标，已保留目录")
                target_is_collection = is_collection
                continue
            if len(parts) != len(target_parts) + 1 or parts[: len(target_parts)] != target_parts:
                raise StorageError("WebDAV Depth:1 响应越出当前目录，已保留目录")
            children.append(_WebDAVResource(name=parts[-1], is_collection=is_collection))
        if target_is_collection is None:
            raise StorageError("WebDAV 目录响应缺少目标集合，已保留目录")
        return target_is_collection, children

    async def _webdav_directory_listing(
        self,
        client: httpx.AsyncClient,
        relative: str,
    ) -> tuple[bool, list[_WebDAVResource]] | None:
        url = self.remote_url(relative)
        response = await client.request(
            "PROPFIND",
            url,
            headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
            content=_WEBDAV_PROPFIND_BODY,
        )
        return self._parse_webdav_directory_listing(response, url)

    async def _webdav_collection_exists(
        self,
        client: httpx.AsyncClient,
        relative: str,
    ) -> bool:
        url = self.remote_url(relative)
        response = await client.request("PROPFIND", url, headers={"Depth": "0"})
        if response.status_code == 404:
            return False
        self._check_response(response, "确认 WebDAV 目录删除", accepted={207})
        return True

    async def _cleanup_webdav_directory(
        self,
        client: httpx.AsyncClient,
        relative: str,
        *,
        level: int,
    ) -> None:
        listing = await self._webdav_directory_listing(client, relative)
        if listing is None:
            return
        is_collection, children = listing
        if not is_collection:
            return

        for child in children:
            expected = (
                level == 0 and child.is_collection and _ARCHIVE_YEAR_RE.fullmatch(child.name)
            ) or (
                level == 1 and child.is_collection and _ARCHIVE_MONTH_RE.fullmatch(child.name)
            )
            if expected:
                await self._cleanup_webdav_directory(client, f"{relative}/{child.name}", level=level + 1)

        # Re-list after every child operation.  WebDAV DELETE is recursive on
        # many servers, so a stale listing must never authorize a parent delete.
        refreshed = await self._webdav_directory_listing(client, relative)
        if refreshed is None:
            return
        refreshed_is_collection, refreshed_children = refreshed
        if not refreshed_is_collection or refreshed_children:
            return

        url = self.remote_url(relative)
        response = await client.request("DELETE", url)
        self._check_response(response, "删除空 WebDAV 目录", accepted=set(range(200, 300)) | {404})
        webdav_client_manager.invalidate_directory(
            self,
            url,
            include_descendants=True,
            include_ancestors=True,
        )
        if await self._webdav_collection_exists(client, relative):
            raise StorageError("WebDAV 目录删除后仍可见，已停止清理")

    async def cleanup_empty_chat_tree(self, chat_root: str | Path) -> None:
        """Best-effort removal of only an empty chat/year/month archive tree."""
        chat_root_text = _single_relative_segment(chat_root)
        if self.is_local:
            if not self.local_root:
                raise StorageError("本地目的地缺少目录")
            # Do not use local_path() here: it resolves the final segment and
            # would turn a chat-directory symlink into an external target.
            root = self.local_root.resolve() / chat_root_text
            await asyncio.to_thread(_cleanup_local_empty_chat_tree, root)
            return
        try:
            client = await webdav_client_manager.get_client(self)
            await self._cleanup_webdav_directory(client, chat_root_text, level=0)
        except httpx.HTTPError as error:
            raise StorageError(f"WebDAV 目录清理失败：{error}") from error

    @staticmethod
    def _should_retry_directory_state(error: _WebDAVResponseError) -> bool:
        return error.status_code in _UPLOAD_DIRECTORY_RETRY_STATUSES or (
            error.status_code == 405 and "目录" in error.operation
        )

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
            client = await webdav_client_manager.get_client(self)
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
        client = await webdav_client_manager.get_client(self)
        response: httpx.Response | None = None
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
            await webdav_client_manager.register_remote_stream(client, response)
            return client, response
        except Exception:
            if response is not None and not response.is_closed:
                await response.aclose()
            raise

    async def close_remote_stream(self, client: httpx.AsyncClient, response: httpx.Response) -> None:
        await webdav_client_manager.close_remote_stream(client, response)


WebDAVClientFactory = Callable[[Destination], Awaitable[httpx.AsyncClient]]


class WebDAVClientManager:
    """Own persistent WebDAV clients and the response streams using them.

    The manager is deliberately separate from the immutable Destination
    value object.  It owns client construction, bounded connection pools and
    shutdown.  A client is cached for the lifetime of one application event
    loop and one destination-version/configuration identity.
    """

    def __init__(self, client_factory: WebDAVClientFactory | None = None) -> None:
        self._client_factory = client_factory
        self._clients: dict[tuple[object, ...], httpx.AsyncClient] = {}
        self._directory_cache: dict[tuple[object, ...], set[str]] = {}
        self._directory_locks: dict[tuple[tuple[object, ...], str], asyncio.Lock] = {}
        self._active_directory_cache_metrics: ContextVar[DirectoryCacheMetrics | None] = ContextVar(
            f"active_directory_cache_metrics_{id(self)}",
            default=None,
        )
        self._active_streams: dict[int, tuple[httpx.AsyncClient, httpx.Response]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock: asyncio.Lock | None = None
        self._closed = False

    @property
    def cached_client_count(self) -> int:
        return len(self._clients)

    @property
    def active_stream_count(self) -> int:
        return len(self._active_streams)

    @property
    def cache_keys(self) -> tuple[tuple[object, ...], ...]:
        return tuple(self._clients)

    @property
    def directory_cache_count(self) -> int:
        return sum(len(entries) for entries in self._directory_cache.values())

    @property
    def directory_lock_count(self) -> int:
        return len(self._directory_locks)

    def cached_directories(self, destination: Destination) -> tuple[str, ...]:
        """Return cached directory URLs for one destination namespace."""
        namespace = destination.webdav_client_cache_key()
        return tuple(sorted(self._directory_cache.get(namespace, set())))

    def is_directory_cached(self, destination: Destination, url: str) -> bool:
        namespace = destination.webdav_client_cache_key()
        return self._canonical_directory_url(url) in self._directory_cache.get(namespace, set())

    @staticmethod
    def _canonical_directory_url(url: str) -> str:
        return url.rstrip("/")

    @staticmethod
    def _is_directory_state_invalidating(status_code: int) -> bool:
        return status_code in _DIRECTORY_STATE_INVALIDATION_STATUSES

    def _directory_namespace(self, destination: Destination) -> tuple[object, ...]:
        return destination.webdav_client_cache_key()

    def _directory_lock(self, namespace: tuple[object, ...], url: str) -> asyncio.Lock:
        key = (namespace, url)
        lock = self._directory_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._directory_locks[key] = lock
        return lock

    @asynccontextmanager
    async def collect_directory_cache_metrics(self) -> AsyncIterator[DirectoryCacheMetrics]:
        """Collect cache lookups for one upload without logging each lookup."""
        metrics = DirectoryCacheMetrics()
        token = self._active_directory_cache_metrics.set(metrics)
        try:
            yield metrics
        finally:
            self._active_directory_cache_metrics.reset(token)

    def _record_directory_cache_hit(self) -> None:
        metrics = self._active_directory_cache_metrics.get()
        if metrics is not None:
            metrics.hits += 1

    def _record_directory_cache_miss(self) -> None:
        metrics = self._active_directory_cache_metrics.get()
        if metrics is not None:
            metrics.misses += 1

    def invalidate_directory(
        self,
        destination: Destination,
        url: str,
        *,
        include_descendants: bool = True,
        include_ancestors: bool = False,
    ) -> None:
        """Forget a directory and related paths without discarding its lock."""
        namespace = self._directory_namespace(destination)
        canonical_url = self._canonical_directory_url(url)
        entries = self._directory_cache.get(namespace)
        if not entries:
            return

        prefixes: list[str] = []
        if include_descendants:
            prefixes.append(f"{canonical_url}/")
        if include_ancestors:
            parts = urlsplit(canonical_url)
            path = parts.path.rstrip("/")
            while path and path != "/":
                path = path.rsplit("/", 1)[0] or "/"
                prefixes.append(f"{parts.scheme}://{parts.netloc}{path}".rstrip("/"))

        entries.discard(canonical_url)
        for entry in tuple(entries):
            if any(entry.startswith(prefix) for prefix in prefixes):
                entries.discard(entry)
        if not entries:
            self._directory_cache.pop(namespace, None)

    async def require_collection(
        self,
        destination: Destination,
        client: httpx.AsyncClient,
        url: str,
        operation: str,
    ) -> None:
        await self._ensure_cached_collection(destination, client, url, operation, require=True)

    async def ensure_collection(
        self,
        destination: Destination,
        client: httpx.AsyncClient,
        url: str,
        operation: str,
    ) -> None:
        await self._ensure_cached_collection(destination, client, url, operation, require=False)

    async def _ensure_cached_collection(
        self,
        destination: Destination,
        client: httpx.AsyncClient,
        url: str,
        operation: str,
        *,
        require: bool,
    ) -> None:
        await self._ensure_loop()
        canonical_url = self._canonical_directory_url(url)
        namespace = self._directory_namespace(destination)
        entries = self._directory_cache.setdefault(namespace, set())
        if canonical_url in entries:
            self._record_directory_cache_hit()
            return

        self._record_directory_cache_miss()

        lock = self._directory_lock(namespace, canonical_url)
        async with lock:
            if canonical_url in self._directory_cache.get(namespace, set()):
                self._record_directory_cache_hit()
                return
            try:
                if require:
                    await destination._require_collection_uncached(client, canonical_url, operation)
                else:
                    await destination._ensure_collection_uncached(client, canonical_url, operation)
            except _WebDAVResponseError as error:
                if self._is_directory_state_invalidating(error.status_code):
                    self.invalidate_directory(destination, canonical_url, include_ancestors=True)
                raise
            self._directory_cache.setdefault(namespace, set()).add(canonical_url)

    async def _close_resources(
        self,
        streams: list[tuple[httpx.AsyncClient, httpx.Response]],
        clients: list[httpx.AsyncClient],
    ) -> None:
        # Responses must be closed before their owning clients so a streamed
        # connection is returned/closed before the pool itself disappears.
        seen_responses: set[int] = set()
        for _, response in streams:
            if id(response) in seen_responses:
                continue
            seen_responses.add(id(response))
            try:
                await response.aclose()
            except Exception:
                pass

        seen_clients: set[int] = set()
        for client in clients:
            if id(client) in seen_clients:
                continue
            seen_clients.add(id(client))
            try:
                await client.aclose()
            except Exception:
                pass

    async def _ensure_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is loop:
            if self._lock is None:
                self._lock = asyncio.Lock()
            return

        stale_streams = list(self._active_streams.values())
        stale_clients = list(self._clients.values())
        self._active_streams.clear()
        self._clients.clear()
        self._directory_cache.clear()
        self._directory_locks.clear()
        self._loop = loop
        self._lock = asyncio.Lock()
        self._closed = False
        await self._close_resources(stale_streams, stale_clients)

    async def start(self) -> None:
        """Bind the manager to the current application event loop."""
        await self._ensure_loop()
        self._closed = False

    async def get_client(self, destination: Destination) -> httpx.AsyncClient:
        if destination.is_local:
            raise StorageError("本地目的地不需要 WebDAV 客户端")
        await self._ensure_loop()
        assert self._lock is not None
        key = destination.webdav_client_cache_key()
        async with self._lock:
            if self._closed:
                raise StorageError("WebDAV 客户端管理器已关闭")
            client = self._clients.get(key)
            if client is not None and not client.is_closed:
                return client
            if client is not None:
                self._clients.pop(key, None)
            if self._client_factory is None:
                client = await destination._client()
            else:
                client = await self._client_factory(destination)
            self._clients[key] = client
            return client

    async def get(self, destination: Destination) -> httpx.AsyncClient:
        """Short alias for callers that treat the manager as a client pool."""
        return await self.get_client(destination)

    async def register_remote_stream(self, client: httpx.AsyncClient, response: httpx.Response) -> None:
        await self._ensure_loop()
        assert self._lock is not None
        async with self._lock:
            if self._closed:
                should_close = True
            else:
                self._active_streams[id(response)] = (client, response)
                should_close = False
        if should_close:
            await response.aclose()
            raise StorageError("WebDAV 客户端管理器已关闭")

    async def close_remote_stream(self, client: httpx.AsyncClient, response: httpx.Response) -> None:
        try:
            await response.aclose()
        finally:
            if self._lock is not None:
                async with self._lock:
                    self._active_streams.pop(id(response), None)

    async def close(self) -> None:
        """Close every active response first, then every cached client."""
        await self._ensure_loop()
        assert self._lock is not None
        async with self._lock:
            self._closed = True
            streams = list(self._active_streams.values())
            clients = list(self._clients.values())
            self._active_streams.clear()
            self._clients.clear()
            self._directory_cache.clear()
            self._directory_locks.clear()
        await self._close_resources(streams, clients)


# Keep both spellings available for callers/tests while using the common
# WebDAV spelling in the application code.
WebDavClientManager = WebDAVClientManager
webdav_client_manager = WebDAVClientManager()
