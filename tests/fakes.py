"""Simulação em memória da Google Drive API v3 para os testes automatizados.

Implementa apenas o necessário: about, files.get (metadados e alt=media com Range),
files.export, files.list (subconjunto da sintaxe ``q``), drives.list/get e
exportLinks. Permite injetar falhas: códigos HTTP, quedas de conexão no meio do
stream, respostas truncadas, conteúdo corrompido e servidor que ignora Range.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from typing import Any
from urllib.parse import parse_qs, urlsplit

import requests
from requests.structures import CaseInsensitiveDict

FOLDER = "application/vnd.google-apps.folder"
SHORTCUT = "application/vnd.google-apps.shortcut"
DOC = "application/vnd.google-apps.document"
SHEET = "application/vnd.google-apps.spreadsheet"
FORM = "application/vnd.google-apps.form"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PDF = "application/pdf"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

REASONS = {429: "rateLimitExceeded", 500: "backendError", 503: "backendError", 404: "notFound", 401: "authError"}


class FakeResponse:
    def __init__(self, status: int = 200, json_data: Any = None, content: bytes = b"",
                 headers: dict[str, str] | None = None, fail_after: int | None = None,
                 truncate_at: int | None = None):
        self.status_code = status
        self._json = json_data
        self._content = content
        self.headers = CaseInsensitiveDict(headers or {})
        self.fail_after = fail_after
        self.truncate_at = truncate_at
        self.closed = False

    def json(self) -> Any:
        if self._json is None:
            raise ValueError("sem JSON")
        return self._json

    def iter_content(self, chunk_size: int = 1024):
        data = self._content
        if self.truncate_at is not None:
            data = data[: self.truncate_at]
        pos = 0
        while pos < len(data):
            end = min(len(data), pos + chunk_size)
            if self.fail_after is not None and end > self.fail_after:
                if self.fail_after > pos:
                    yield data[pos:self.fail_after]
                raise requests.exceptions.ChunkedEncodingError("conexão perdida (simulada)")
            yield data[pos:end]
            pos = end

    def close(self) -> None:
        self.closed = True


def error(status: int, reason: str | None = None, message: str = "erro simulado",
          headers: dict[str, str] | None = None) -> FakeResponse:
    reason = reason or REASONS.get(status, "error")
    return FakeResponse(status, {"error": {"code": status, "message": message,
                                           "errors": [{"reason": reason}]}}, headers=headers)


class FakeSession:
    def __init__(self, drive: "FakeDrive"):
        self.drive = drive

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        return self.drive.handle(method, url, kwargs.get("params") or {}, kwargs.get("headers") or {})


class FakeDrive:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.contents: dict[str, bytes] = {}
        self.exports: dict[tuple[str, str], bytes] = {}
        self.requests: list[dict[str, Any]] = []
        self.statuses: dict[str, list[Any]] = defaultdict(list)  # chave -> [status | (status, reason)]
        self.media_fail: dict[str, list[int]] = defaultdict(list)
        self.media_truncate: dict[str, list[int]] = defaultdict(list)
        self.export_fail: dict[str, list[int]] = defaultdict(list)
        self.corrupt: set[str] = set()
        self.export_too_large: set[str] = set()
        self.required_resource_keys: dict[str, str] = {}
        self.ignore_range = False
        self.max_page_size = 1000
        self.shared_drives: list[dict[str, str]] = []
        self.about = {"emailAddress": "usuario@example.com", "displayName": "Usuário Teste"}
        self._rev = 0
        self.on_media: Any = None  # callback(file_id, offset) chamado antes de servir conteúdo

    # ------------------------------------------------------------ montagem
    def _next_rev(self) -> str:
        self._rev += 1
        return f"rev{self._rev}"

    def add_folder(self, fid: str, name: str, parent: str | None = None, **kw: Any) -> dict[str, Any]:
        meta = {"id": fid, "name": name, "mimeType": FOLDER, "parents": [parent] if parent else [],
                "ownedByMe": True, "trashed": False, "createdTime": "2024-01-01T00:00:00Z",
                "modifiedTime": "2024-01-01T00:00:00Z", "capabilities": {"canDownload": True}}
        meta.update(kw)
        self.items[fid] = meta
        return meta

    def add_file(self, fid: str, name: str, content: bytes, parent: str | None = None, *,
                 mime: str = "application/octet-stream", md5: bool = True, sha256: bool = False,
                 created: str = "2024-01-01T00:00:00Z", **kw: Any) -> dict[str, Any]:
        meta = {"id": fid, "name": name, "mimeType": mime, "parents": [parent] if parent else [],
                "ownedByMe": True, "trashed": False, "createdTime": created,
                "capabilities": {"canDownload": True}}
        meta.update(kw)
        self.items[fid] = meta
        self.set_content(fid, content, md5=md5, sha256=sha256)
        return meta

    def set_content(self, fid: str, content: bytes, md5: bool = True, sha256: bool = False) -> None:
        meta = self.items[fid]
        self.contents[fid] = content
        meta["size"] = str(len(content))
        meta["headRevisionId"] = self._next_rev()
        meta["modifiedTime"] = f"2024-02-01T00:00:{self._rev:02d}Z"
        meta.pop("md5Checksum", None)
        meta.pop("sha256Checksum", None)
        if md5:
            meta["md5Checksum"] = hashlib.md5(content).hexdigest()
        if sha256:
            meta["sha256Checksum"] = hashlib.sha256(content).hexdigest()

    def add_native(self, fid: str, name: str, mime: str, parent: str | None = None,
                   exports: dict[str, bytes] | None = None, **kw: Any) -> dict[str, Any]:
        meta = {"id": fid, "name": name, "mimeType": mime, "parents": [parent] if parent else [],
                "ownedByMe": True, "trashed": False, "createdTime": "2024-01-01T00:00:00Z",
                "modifiedTime": "2024-03-01T00:00:00Z", "capabilities": {"canDownload": True},
                "exportLinks": {}}
        meta.update(kw)
        for emime, data in (exports or {}).items():
            self.exports[(fid, emime)] = data
            meta["exportLinks"][emime] = f"https://docs.google.com/feeds/download/{fid}?exportMime={emime}"
        self.items[fid] = meta
        return meta

    def add_shortcut(self, fid: str, name: str, target: str, parent: str | None = None) -> dict[str, Any]:
        target_mime = self.items.get(target, {}).get("mimeType", "application/octet-stream")
        meta = {"id": fid, "name": name, "mimeType": SHORTCUT, "parents": [parent] if parent else [],
                "ownedByMe": True, "trashed": False, "createdTime": "2024-01-01T00:00:00Z",
                "shortcutDetails": {"targetId": target, "targetMimeType": target_mime}}
        self.items[fid] = meta
        return meta

    def session(self) -> FakeSession:
        return FakeSession(self)

    def count(self, kind: str, fid: str | None = None) -> int:
        return sum(1 for r in self.requests if r["kind"] == kind and (fid is None or r.get("id") == fid))

    # ------------------------------------------------------------ servidor
    def _injected(self, key: str) -> FakeResponse | None:
        queue = self.statuses.get(key)
        if queue:
            item = queue.pop(0)
            status, reason = item if isinstance(item, tuple) else (item, None)
            headers = {"Retry-After": "0"} if status == 429 else None
            return error(status, reason, headers=headers)
        return None

    def _public(self, meta: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in meta.items() if not k.startswith("_")}

    def _visible(self, fid: str, headers: dict[str, str]) -> bool:
        if fid not in self.items or self.items[fid].get("_no_access"):
            return False
        needed = self.required_resource_keys.get(fid)
        if needed:
            sent = headers.get("X-Goog-Drive-Resource-Keys", "")
            return f"{fid}/{needed}" in sent.split(",")
        return True

    def handle(self, method: str, url: str, params: dict[str, Any], headers: dict[str, str]) -> FakeResponse:
        parts = urlsplit(url)
        path = parts.path
        rec: dict[str, Any] = {"method": method, "url": url, "params": dict(params), "headers": dict(headers)}
        self.requests.append(rec)

        if parts.hostname == "docs.google.com":
            m = re.match(r"/feeds/download/([^/?]+)", path)
            fid = m.group(1) if m else ""
            emime = parse_qs(parts.query).get("exportMime", [""])[0]
            rec.update(kind="exportlink", id=fid)
            data = self.exports.get((fid, emime))
            if data is None:
                return error(404)
            return FakeResponse(200, content=data, headers={"Content-Length": str(len(data))})

        if path.endswith("/about"):
            rec["kind"] = "about"
            return self._injected("about") or FakeResponse(200, {"user": self.about})

        m = re.match(r"^/drive/v3/files/([^/]+)/export$", path)
        if m:
            fid = m.group(1)
            rec.update(kind="export", id=fid)
            inj = self._injected(f"export:{fid}")
            if inj:
                return inj
            if not self._visible(fid, headers):
                return error(404)
            if fid in self.export_too_large:
                return error(403, "exportSizeLimitExceeded")
            data = self.exports.get((fid, params.get("mimeType", "")))
            if data is None:
                return error(400, "badRequest", "formato não suportado")
            fail = self.export_fail[fid].pop(0) if self.export_fail[fid] else None
            return FakeResponse(200, content=data, fail_after=fail)

        m = re.match(r"^/drive/v3/files/([^/]+)$", path)
        if m:
            fid = m.group(1)
            if params.get("alt") == "media":
                rec.update(kind="media", id=fid)
                return self._media(fid, headers)
            rec.update(kind="get", id=fid)
            inj = self._injected(f"get:{fid}")
            if inj:
                return inj
            if not self._visible(fid, headers):
                return error(404)
            return FakeResponse(200, self._public(self.items[fid]))

        if path == "/drive/v3/files":
            rec["kind"] = "list"
            return self._injected("list") or self._list(params, headers)

        if path == "/drive/v3/drives":
            rec["kind"] = "drives"
            return FakeResponse(200, {"drives": self.shared_drives})

        m = re.match(r"^/drive/v3/drives/([^/]+)$", path)
        if m:
            rec["kind"] = "drive"
            for d in self.shared_drives:
                if d["id"] == m.group(1):
                    return FakeResponse(200, d)
            return error(404)
        return error(404)

    def _media(self, fid: str, headers: dict[str, str]) -> FakeResponse:
        inj = self._injected(f"media:{fid}")
        if inj:
            return inj
        if not self._visible(fid, headers):
            return error(404)
        meta = self.items[fid]
        if meta["mimeType"].startswith("application/vnd.google-apps."):
            return error(403, "fileNotDownloadable")
        if meta.get("_forbidden"):
            return error(403, meta["_forbidden"])
        data = self.contents[fid]
        if fid in self.corrupt:
            data = bytes(b ^ 0xFF for b in data[:16]) + data[16:]
        offset = 0
        rng = headers.get("Range")
        if self.on_media:
            self.on_media(fid, rng)
            data = self.contents[fid] if fid not in self.corrupt else data
        status = 200
        resp_headers: dict[str, str] = {}
        if rng and not self.ignore_range:
            offset = int(re.match(r"bytes=(\d+)-", rng).group(1))
            if offset >= len(data):
                return error(416, "requestedRangeNotSatisfiable")
            status = 206
            resp_headers["Content-Range"] = f"bytes {offset}-{len(data) - 1}/{len(data)}"
        body = data[offset:]
        resp_headers["Content-Length"] = str(len(body))
        fail = self.media_fail[fid].pop(0) if self.media_fail[fid] else None
        trunc = self.media_truncate[fid].pop(0) if self.media_truncate[fid] else None
        return FakeResponse(status, content=body, headers=resp_headers, fail_after=fail, truncate_at=trunc)

    def _list(self, params: dict[str, Any], headers: dict[str, str]) -> FakeResponse:
        q = params.get("q", "")
        items = [m for m in self.items.values() if not m.get("_no_access")]
        parent_m = re.search(r"'((?:[^'\\]|\\.)*)' in parents", q)
        if parent_m:
            pid = parent_m.group(1)
            items = [m for m in items if pid in m.get("parents", [])]
        name_m = re.search(r"name contains '((?:[^'\\]|\\.)*)'", q)
        if name_m:
            term = name_m.group(1).replace("\\'", "'").replace("\\\\", "\\").casefold()
            items = [m for m in items if term in m["name"].casefold()]
        if "trashed = false" in q:
            items = [m for m in items if not m.get("trashed")]
        mm = re.search(r"mimeType (=|!=) '([^']+)'", q)
        if mm:
            op, mime = mm.groups()
            items = [m for m in items if (m["mimeType"] == mime) == (op == "=")]
        if "'me' in owners" in q:
            items = [m for m in items if m.get("ownedByMe")]
        if "sharedWithMe = true" in q:
            items = [m for m in items if not m.get("ownedByMe") and not m.get("driveId")]
        if params.get("corpora") == "drive":
            items = [m for m in items if m.get("driveId") == params.get("driveId")]
        elif params.get("corpora") == "user" and not parent_m:
            items = [m for m in items if not m.get("driveId")]
        size = min(int(params.get("pageSize", 100)), self.max_page_size)
        start = int(params.get("pageToken", 0) or 0)
        page = items[start:start + size]
        data: dict[str, Any] = {"files": [self._public(m) for m in page]}
        if start + size < len(items):
            data["nextPageToken"] = str(start + size)
        return FakeResponse(200, data)
