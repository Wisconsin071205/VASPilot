"""Small, dependency-free WebDAV client for the synchronization feature.

The client speaks only the DAV verbs the sync engine needs (PROPFIND, GET,
PUT, MKCOL, DELETE) over ``urllib``.  It is deliberately independent of any
third-party WebDAV package so the core application keeps working when only
the standard library is installed.

Only Basic authentication is supported here.  USTC ``pan.ustc.edu.cn`` is a
Seafile deployment whose ``/seafdav`` endpoint accepts the account's login
name and password over HTTPS.  Credentials are never written to logs.
"""

from __future__ import annotations

import base64
import re
import ssl
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.parse import quote, unquote, urlparse, urlunparse

from ..core.errors import RemoteError, ValidationError

DAV_NS = "DAV:"
_BAD_SEGMENT = re.compile(r"[\x00-\x1f\x7f\\]")
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


def normalize_url(value: str) -> str:
    """Return a canonical http(s) base URL without a trailing slash.

    A bare host/path (as users often paste from a portal) gets ``https://``.
    Credentials embedded in the URL are rejected: the password belongs in the
    dedicated field and in the encrypted local vault only.
    """
    text = str(value or "").strip()
    if not text:
        raise ValidationError("WebDAV 地址不能为空")
    if not _SCHEME_RE.match(text):
        text = "https://" + text
    parsed = urlparse(text)
    if parsed.scheme.lower() not in ("http", "https"):
        raise ValidationError("WebDAV 地址必须使用 http:// 或 https://")
    if not parsed.netloc:
        raise ValidationError("WebDAV 地址缺少主机名")
    if parsed.username or parsed.password:
        raise ValidationError("请不要把用户名或密码写进 WebDAV 地址，"
                              "请使用独立的登录字段")
    path = parsed.path.rstrip("/")
    # A cached query/fragment is never part of a WebDAV collection URL.
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", "", ""))


def validate_remote_dir(value: str) -> str:
    """Validate a relative remote directory such as ``RSCI-sync/backup``."""
    text = str(value or "").strip().strip("/")
    if not text:
        return "RSCI-sync"
    if len(text) > 240:
        raise ValidationError("WebDAV 远端目录名过长")
    parts = text.split("/")
    if len(parts) > 6:
        raise ValidationError("WebDAV 远端目录最多支持 6 层")
    for part in parts:
        if not part or part in (".", "..") or _BAD_SEGMENT.search(part):
            raise ValidationError("WebDAV 远端目录包含非法路径片段")
        if len(part) > 80:
            raise ValidationError("WebDAV 远端目录片段过长")
    return "/".join(parts)


def encode_path(value: str) -> str:
    """Quote each path segment, preserving separators."""
    return "/".join(quote(part, safe="") for part in
                    str(value).replace("\\", "/").split("/") if part)


@dataclass
class RemoteEntry:
    rel: str                 # path relative to the configured remote root
    href: str                # absolute path on the server
    is_dir: bool = False
    size: int = 0
    last_modified: str = ""
    mtime: float = 0.0
    etag: str = ""

    @property
    def token(self) -> str:
        """A change marker that works even when a server omits ETag."""
        if self.etag:
            return "etag:" + self.etag
        if self.last_modified or self.size:
            return f"meta:{self.size}:{self.mtime:.3f}:{self.last_modified}"
        return ""


class WebDAVError(RemoteError):
    """A DAV request failed with a useful HTTP status."""

    def __init__(self, message: str, *, status: int = 0,
                 detail: dict | None = None) -> None:
        super().__init__(message, detail=detail)
        self.status = int(status or 0)


class WebDAVClient:
    def __init__(self, base_url: str, username: str, password: str, *,
                 timeout: float = 30.0, verify_tls: bool = True) -> None:
        self.base_url = normalize_url(base_url)
        self.username = str(username or "")
        self._password = str(password or "")
        self.timeout = max(5.0, min(float(timeout or 30.0), 180.0))
        self.verify_tls = bool(verify_tls)
        parsed = urlparse(self.base_url)
        self.base_path = parsed.path.rstrip("/")
        self._auth = ""
        if self.username or self._password:
            token = base64.b64encode(
                f"{self.username}:{self._password}".encode("utf-8")).decode("ascii")
            self._auth = "Basic " + token

    # -- URL helpers ---------------------------------------------------------
    def url_for(self, rel: str = "") -> str:
        rel = str(rel or "").strip("/")
        if rel:
            return self.base_url + "/" + encode_path(rel)
        return self.base_url + "/"

    def _request(self, method: str, rel: str = "", *, data: bytes | None = None,
                 headers: dict | None = None, retries: int = 2):
        url = self.url_for(rel)
        request_headers = {
            "User-Agent": "RSCI-WebDAV-Sync/1",
            "Accept": "*/*",
            **(headers or {}),
        }
        if self._auth:
            request_headers["Authorization"] = self._auth
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            request = urllib.request.Request(
                url, data=data, method=method, headers=request_headers)
            context = None
            if url.lower().startswith("https://"):
                context = (ssl.create_default_context() if self.verify_tls
                           else ssl._create_unverified_context())
            try:
                with urllib.request.urlopen(
                        request, timeout=self.timeout, context=context) as response:
                    return response.status, dict(response.headers), response.read()
            except urllib.error.HTTPError as exc:
                body = b""
                try:
                    body = exc.read()[:4096]
                except Exception:
                    pass
                status = int(exc.code)
                # Authentication and ordinary client mistakes are final; a
                # transient 5xx/429 is retried once or twice.
                if status in (401, 403):
                    raise WebDAVError(
                        "WebDAV 认证失败或没有权限，请检查用户名和密码"
                        if status == 401 else
                        "WebDAV 账号没有访问该目录的权限",
                        status=status) from exc
                if status == 404:
                    raise WebDAVError("WebDAV 路径不存在", status=404,
                                      detail={"body": body.decode("utf-8", "replace")}) from exc
                if status not in (429, 500, 502, 503, 504) or attempt >= retries:
                    raise WebDAVError(
                        f"WebDAV 请求失败（HTTP {status}）", status=status,
                        detail={"body": body.decode("utf-8", "replace")}) from exc
                last_error = exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt >= retries:
                    break
            time.sleep(min(4.0, 0.8 * (2 ** attempt)))
        raise WebDAVError(
            "无法连接 WebDAV 服务，请检查地址、网络/VPN 和账号密码",
            detail={"reason": str(last_error or "")}) from last_error

    # -- DAV operations ------------------------------------------------------
    def propfind(self, rel: str = "", *, depth: str = "0") -> list[RemoteEntry]:
        body = ("<?xml version=\"1.0\" encoding=\"utf-8\"?>"
                "<d:propfind xmlns:d=\"DAV:\"><d:prop>"
                "<d:displayname/><d:resourcetype/><d:getcontentlength/>"
                "<d:getlastmodified/><d:getetag/>"
                "</d:prop></d:propfind>").encode("utf-8")
        status, _, payload = self._request(
            "PROPFIND", rel, data=body,
            headers={"Depth": depth, "Content-Type": "application/xml; charset=utf-8"})
        if status not in (200, 207):
            raise WebDAVError(f"WebDAV PROPFIND 返回 HTTP {status}", status=status)
        try:
            root = ET.fromstring(payload)
        except ET.ParseError as exc:
            raise WebDAVError("WebDAV 返回了无法解析的目录响应") from exc
        entries: list[RemoteEntry] = []
        for response in root.findall(f"{{{DAV_NS}}}response"):
            href = (response.findtext(f"{{{DAV_NS}}}href") or "").strip()
            if not href:
                continue
            prop = _first_prop(response)
            if prop is None:
                continue
            resource_type = prop.find(f"{{{DAV_NS}}}resourcetype")
            is_dir = (resource_type is not None and
                      resource_type.find(f"{{{DAV_NS}}}collection") is not None)
            modified = (prop.findtext(f"{{{DAV_NS}}}getlastmodified") or "").strip()
            try:
                size = int((prop.findtext(f"{{{DAV_NS}}}getcontentlength") or "0").strip() or 0)
            except ValueError:
                size = 0
            entries.append(RemoteEntry(
                rel="",
                href=unquote(urlparse(href).path or href),
                is_dir=is_dir,
                size=max(0, size),
                last_modified=modified,
                mtime=_parse_http_date(modified),
                etag=(prop.findtext(f"{{{DAV_NS}}}getetag") or "").strip().strip('"'),
            ))
        return entries

    def stat(self, rel: str) -> RemoteEntry | None:
        try:
            entries = self.propfind(rel, depth="0")
        except WebDAVError as exc:
            if exc.status == 404:
                return None
            raise
        return entries[0] if entries else None

    def list_dir(self, rel: str = "") -> list[RemoteEntry]:
        """List immediate children; the self entry is removed."""
        try:
            entries = self.propfind(rel, depth="1")
        except WebDAVError as exc:
            if exc.status == 404:
                return []
            raise
        own = self._absolute_path(rel)
        out = []
        for entry in entries:
            if entry.href.rstrip("/") == own.rstrip("/"):
                continue
            child = self._relative(entry.href)
            if child is None or child == rel.strip("/"):
                continue
            entry.rel = child
            out.append(entry)
        return out

    def list_tree(self, rel: str = "") -> dict[str, RemoteEntry]:
        """Recursively list a collection (breadth-first, Depth: 1)."""
        root = str(rel or "").strip("/")
        found: dict[str, RemoteEntry] = {}
        pending = [root]
        seen_dirs = set()
        while pending:
            current = pending.pop(0)
            if current in seen_dirs:
                continue
            seen_dirs.add(current)
            for entry in self.list_dir(current):
                found[entry.rel] = entry
                if entry.is_dir:
                    pending.append(entry.rel)
        return found

    def ensure_dir(self, rel: str) -> None:
        current = ""
        for part in str(rel or "").strip("/").split("/"):
            if not part:
                continue
            current = f"{current}/{part}" if current else part
            if self.stat(current) is not None:
                continue
            try:
                self._request("MKCOL", current, retries=0)
            except WebDAVError as exc:
                # 405 = already there (a concurrent device or an older
                # server that does not report collections consistently).
                if exc.status == 405:
                    continue
                if exc.status == 409:
                    raise WebDAVError(
                        "无法创建 WebDAV 远端目录：父目录不存在或账号无写权限",
                        status=409) from exc
                if exc.status == 403 and "/" not in current:
                    raise WebDAVError(
                        "WebDAV 根目录不允许直接创建文件夹。Seafile 的 "
                        "/seafdav 根目录是资料库列表，请把远端目录改为已有"
                        "资料库下的子目录，例如 webdav/RSCI-sync",
                        status=403) from exc
                raise

    def get(self, rel: str) -> bytes:
        _, _, payload = self._request("GET", rel)
        return payload

    def put(self, rel: str, data: bytes, *, content_type: str = "application/octet-stream") -> None:
        self._request("PUT", rel, data=data, headers={"Content-Type": content_type})

    def delete(self, rel: str) -> None:
        try:
            self._request("DELETE", rel, retries=0)
        except WebDAVError as exc:
            if exc.status != 404:
                raise

    def test(self) -> dict:
        """Check authentication and whether the configured root is usable."""
        entry = self.stat("")
        if entry is None:
            raise WebDAVError("WebDAV 根目录不可枚举", status=404)
        return {"reachable": True, "is_collection": entry.is_dir,
                "writable": None, "url": self.base_url}

    def _absolute_path(self, rel: str) -> str:
        suffix = "/" + str(rel or "").strip("/") if str(rel or "").strip("/") else ""
        return (self.base_path + suffix).rstrip("/") + "/"

    def _relative(self, href: str) -> str | None:
        path = unquote(urlparse(href).path or "").rstrip("/")
        base = self.base_path.rstrip("/")
        if base:
            if path == base:
                return ""
            if not path.startswith(base + "/"):
                return None
            path = path[len(base) + 1:]
        return path.strip("/")


def _first_prop(response) -> ET.Element | None:
    for propstat in response.findall(f"{{{DAV_NS}}}propstat"):
        status = propstat.findtext(f"{{{DAV_NS}}}status") or ""
        if "200" not in status:
            continue
        prop = propstat.find(f"{{{DAV_NS}}}prop")
        if prop is not None:
            return prop
    return response.find(f"{{{DAV_NS}}}prop")


def _parse_http_date(value: str) -> float:
    try:
        return parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0
