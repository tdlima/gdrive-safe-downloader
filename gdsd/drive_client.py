"""Cliente REST da Google Drive API v3 (pesquisa, metadados, listagem e streams).

Decisão: usamos ``google.auth.transport.requests.AuthorizedSession`` (biblioteca
oficial ``google-auth``) em vez de ``google-api-python-client``, porque este último
depende de ``httplib2``, que não é thread-safe, e não oferece controle direto de
requisições com intervalo de bytes. Cada thread recebe sua própria sessão.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator
from typing import Any, Protocol
from urllib.parse import urlsplit

from .errors import (
    AuthError,
    DriveApiError,
    ExportSizeLimitExceeded,
    NotFoundOrNoAccess,
    PermissionDenied,
    RangeNotSatisfiable,
    TransientError,
)
from .retry import RateLimiter, RetryPolicy, call_with_retry

log = logging.getLogger(__name__)

API = "https://www.googleapis.com/drive/v3"
FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"

FILE_FIELDS = (
    "id,name,mimeType,size,md5Checksum,sha256Checksum,modifiedTime,createdTime,"
    "headRevisionId,parents,driveId,ownedByMe,owners(displayName,emailAddress),"
    "shortcutDetails,capabilities(canDownload,canListChildren),exportLinks,trashed,"
    "resourceKey,sharedWithMeTime"
)

RATE_LIMIT_REASONS = {"userRateLimitExceeded", "rateLimitExceeded", "sharingRateLimitExceeded"}
PERMISSION_MESSAGES = {
    "insufficientFilePermissions": "A conta não tem permissão suficiente para este arquivo.",
    "cannotDownloadFile": "O proprietário ou o administrador desativou o download deste arquivo.",
    "cannotDownloadAbusiveFile": (
        "O Google sinalizou este arquivo como potencialmente nocivo (malware/spam); "
        "o download não é feito automaticamente."
    ),
    "downloadQuotaExceeded": (
        "A cota de downloads deste arquivo foi excedida no Google. Tente novamente em algumas horas."
    ),
    "domainPolicy": "Uma política do domínio (Google Workspace) impede o acesso.",
    "appNotAuthorizedToFile": "Este aplicativo não está autorizado a acessar o arquivo.",
    "fileNotDownloadable": "Arquivos nativos do Google precisam ser exportados, não baixados.",
    "cannotExportFile": "O Google não permite exportar este arquivo.",
    "exportSizeLimitExceeded": "O arquivo excede o limite de exportação direta da API (10 MB).",
    "accessNotConfigured": "A Google Drive API não está ativada no projeto do Google Cloud.",
    "dailyLimitExceeded": "O limite diário da API foi atingido. Tente amanhã.",
}


class SessionLike(Protocol):
    def request(self, method: str, url: str, **kwargs: Any) -> Any: ...


def escape_query_value(value: str) -> str:
    """Escapa valores em consultas ``q`` da Drive API."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _parse_error(resp: Any) -> tuple[str, str]:
    reason, message = "", ""
    try:
        data = resp.json()
        err = data.get("error", {}) if isinstance(data, dict) else {}
        if isinstance(err, dict):
            message = str(err.get("message", ""))
            errors = err.get("errors") or []
            if errors and isinstance(errors[0], dict):
                reason = str(errors[0].get("reason", ""))
        elif isinstance(err, str):
            reason = err
    except Exception:  # noqa: BLE001 - corpo não-JSON
        pass
    return reason, message


def _retry_after(resp: Any) -> float | None:
    value = resp.headers.get("Retry-After") if getattr(resp, "headers", None) else None
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def raise_for_response(resp: Any, context: str = "") -> None:
    """Converte respostas HTTP de erro em exceções da aplicação."""
    status = resp.status_code
    if status < 400:
        return
    reason, message = _parse_error(resp)
    where = f" ({context})" if context else ""
    if status == 401:
        raise AuthError(
            "A autorização expirou ou foi revogada. Execute: python main.py login"
        )
    if status == 404:
        raise NotFoundOrNoAccess(
            f"Recurso não encontrado ou a conta conectada não tem acesso a ele{where}.",
            status, reason or "notFound",
        )
    if status == 416:
        raise RangeNotSatisfiable("Intervalo de bytes inválido.", status, reason)
    if status == 429 or status >= 500 or (status == 403 and reason in RATE_LIMIT_REASONS):
        raise TransientError(
            f"Erro temporário da API (HTTP {status}{', ' + reason if reason else ''}){where}.",
            status, reason, _retry_after(resp),
        )
    if status == 403:
        text = PERMISSION_MESSAGES.get(reason) or f"Acesso negado pela API{': ' + message if message else ''}."
        cls = ExportSizeLimitExceeded if reason == "exportSizeLimitExceeded" else PermissionDenied
        raise cls(text + where, status, reason)
    raise DriveApiError(
        f"Erro da API (HTTP {status}{', ' + reason if reason else ''}){where}: {message or 'sem detalhes'}",
        status, reason,
    )


class DriveClient:
    """Operações de leitura na Drive API com retentativas e limite de taxa."""

    def __init__(
        self,
        session_factory: Callable[[], SessionLike],
        retry_policy: RetryPolicy | None = None,
        rate_limiter: RateLimiter | None = None,
        timeout: tuple[float, float] = (20.0, 120.0),
        stop_event: threading.Event | None = None,
    ):
        self._session_factory = session_factory
        self._local = threading.local()
        self.retry_policy = retry_policy or RetryPolicy()
        self.rate_limiter = rate_limiter
        self.timeout = timeout
        self.stop_event = stop_event
        self._resource_keys: dict[str, str] = {}
        self._keys_lock = threading.Lock()
        self._drive_names: dict[str, str] = {}

    # ------------------------------------------------------------------ base
    @property
    def session(self) -> SessionLike:
        sess = getattr(self._local, "session", None)
        if sess is None:
            sess = self._session_factory()
            self._local.session = sess
        return sess

    def register_resource_key(self, file_id: str, key: str | None) -> None:
        if key:
            with self._keys_lock:
                self._resource_keys[file_id] = key

    def _register_from(self, item: dict[str, Any]) -> None:
        self.register_resource_key(item.get("id", ""), item.get("resourceKey"))
        target = (item.get("shortcutDetails") or {})
        if target.get("targetResourceKey"):
            self.register_resource_key(target.get("targetId", ""), target["targetResourceKey"])

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = dict(extra or {})
        with self._keys_lock:
            if self._resource_keys:
                # A API aceita várias chaves; enviamos as conhecidas (limite prático de 100).
                pairs = list(self._resource_keys.items())[-100:]
                headers["X-Goog-Drive-Resource-Keys"] = ",".join(f"{i}/{k}" for i, k in pairs)
        return headers

    def _send(self, method: str, url: str, *, params: dict[str, Any] | None = None,
              headers: dict[str, str] | None = None, stream: bool = False, context: str = "") -> Any:
        if self.rate_limiter:
            self.rate_limiter.acquire()
        resp = self.session.request(
            method, url, params=params, headers=self._headers(headers),
            stream=stream, timeout=self.timeout,
        )
        if resp.status_code >= 400:
            try:
                raise_for_response(resp, context)
            finally:
                resp.close()
        return resp

    def _json(self, url: str, params: dict[str, Any] | None = None, context: str = "") -> dict[str, Any]:
        def do() -> dict[str, Any]:
            resp = self._send("GET", url, params=params, context=context)
            try:
                return resp.json()
            finally:
                resp.close()

        return call_with_retry(do, self.retry_policy, stop_event=self.stop_event, description=context or url)

    # ------------------------------------------------------------- consultas
    def get_about(self) -> dict[str, str]:
        data = self._json(f"{API}/about", {"fields": "user(displayName,emailAddress)"}, "about")
        user = data.get("user", {})
        return {"email": user.get("emailAddress", ""), "name": user.get("displayName", "")}

    def get_file(self, file_id: str) -> dict[str, Any]:
        """Metadados de um arquivo/pasta (inclui drives compartilhados)."""
        data = self._json(
            f"{API}/files/{file_id}",
            {"fields": FILE_FIELDS, "supportsAllDrives": "true"},
            f"arquivo {file_id}",
        )
        self._register_from(data)
        return data

    def _list(self, params: dict[str, Any], context: str) -> Iterator[tuple[dict[str, Any], bool]]:
        page_token: str | None = None
        while True:
            query = dict(params)
            if page_token:
                query["pageToken"] = page_token
            data = self._json(f"{API}/files", query, context)
            incomplete = bool(data.get("incompleteSearch"))
            for item in data.get("files", []):
                self._register_from(item)
                yield item, incomplete
            page_token = data.get("nextPageToken")
            if not page_token:
                return

    def list_children(self, folder: dict[str, Any]) -> Iterator[dict[str, Any]]:
        """Itens diretos de uma pasta, de qualquer origem (Meu Drive, compartilhada
        ou drive compartilhado). Itens sem acesso simplesmente não são retornados."""
        params: dict[str, Any] = {
            "q": f"'{escape_query_value(folder['id'])}' in parents and trashed = false",
            "fields": f"nextPageToken,incompleteSearch,files({FILE_FIELDS})",
            "pageSize": 1000,
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
            "orderBy": "folder,name,createdTime",
        }
        if folder.get("driveId"):
            params["corpora"] = "drive"
            params["driveId"] = folder["driveId"]
        else:
            params["corpora"] = "user"
        for item, _ in self._list(params, f"pasta {folder.get('name', folder['id'])}"):
            yield item

    def list_shared_drives(self) -> list[dict[str, Any]]:
        drives: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {"pageSize": 100, "fields": "nextPageToken,drives(id,name)"}
            if page_token:
                params["pageToken"] = page_token
            data = self._json(f"{API}/drives", params, "drives compartilhados")
            for drive in data.get("drives", []):
                self._drive_names[drive["id"]] = drive.get("name", drive["id"])
                drives.append(drive)
            page_token = data.get("nextPageToken")
            if not page_token:
                return drives

    def drive_name(self, drive_id: str) -> str:
        if drive_id not in self._drive_names:
            try:
                data = self._json(f"{API}/drives/{drive_id}", {"fields": "id,name"}, "drive compartilhado")
                self._drive_names[drive_id] = data.get("name", drive_id)
            except DriveApiError:
                self._drive_names[drive_id] = drive_id
        return self._drive_names[drive_id]

    def search(
        self,
        term: str,
        kind: str = "all",
        scope: str = "all",
        limit: int = 50,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Pesquisa por nome. Retorna (itens, avisos).

        ``scope``: ``all`` (Meu Drive + compartilhados comigo + drives compartilhados),
        ``mydrive``, ``shared`` (compartilhados comigo) ou ``drives``.
        A pesquisa em drives compartilhados é feita drive a drive (``corpora=drive``),
        pois ``corpora=allDrives`` pode retornar resultados incompletos.
        """
        clauses = [f"name contains '{escape_query_value(term)}'", "trashed = false"]
        if kind == "folder":
            clauses.append(f"mimeType = '{FOLDER_MIME}'")
        elif kind == "file":
            clauses.append(f"mimeType != '{FOLDER_MIME}'")
        base_q = " and ".join(clauses)
        fields = f"nextPageToken,incompleteSearch,files({FILE_FIELDS})"
        common = {"fields": fields, "pageSize": min(max(limit, 1), 1000),
                  "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}

        searches: list[tuple[dict[str, Any], str]] = []
        if scope in ("all", "mydrive", "shared"):
            q = base_q
            if scope == "mydrive":
                q += " and 'me' in owners"
            elif scope == "shared":
                q += " and sharedWithMe = true"
            searches.append(({**common, "q": q, "corpora": "user"}, "pesquisa"))
        warnings: list[str] = []
        if scope in ("all", "drives"):
            try:
                for drive in self.list_shared_drives():
                    searches.append((
                        {**common, "q": base_q, "corpora": "drive", "driveId": drive["id"]},
                        f"pesquisa no drive {drive.get('name')}",
                    ))
            except PermissionDenied as exc:
                warnings.append(f"Não foi possível listar drives compartilhados: {exc}")

        results: dict[str, dict[str, Any]] = {}
        for params, ctx in searches:
            for item, incomplete in self._list(params, ctx):
                if incomplete and "incompleta" not in " ".join(warnings):
                    warnings.append("A API indicou que a pesquisa pode estar incompleta.")
                results.setdefault(item["id"], item)
                if len(results) >= limit:
                    break
            if len(results) >= limit:
                warnings.append(f"Exibindo apenas os primeiros {limit} resultados (use --limit).")
                break
        items = sorted(results.values(), key=lambda i: (i.get("mimeType") != FOLDER_MIME, i.get("name", "").casefold()))
        return items, warnings

    def describe_origin(self, item: dict[str, Any]) -> str:
        if item.get("driveId"):
            return f"Drive compartilhado: {self.drive_name(item['driveId'])}"
        if item.get("ownedByMe"):
            return "Meu Drive"
        owners = item.get("owners") or []
        who = owners[0].get("emailAddress") or owners[0].get("displayName") if owners else None
        return f"Compartilhado por {who}" if who else "Compartilhado comigo"

    # --------------------------------------------------------------- streams
    def open_media(self, file_id: str, offset: int = 0) -> Any:
        """Abre o conteúdo binário (``alt=media``), opcionalmente a partir de ``offset``.
        Sem retentativa: o chamador controla a retomada."""
        headers = {"Range": f"bytes={offset}-"} if offset > 0 else {}
        return self._send(
            "GET", f"{API}/files/{file_id}",
            params={"alt": "media", "supportsAllDrives": "true"},
            headers=headers, stream=True, context=f"download {file_id}",
        )

    def open_export(self, file_id: str, mime_type: str) -> Any:
        return self._send(
            "GET", f"{API}/files/{file_id}/export",
            params={"mimeType": mime_type}, stream=True, context=f"exportação {file_id}",
        )

    def open_url(self, url: str) -> Any:
        """Abre um ``exportLinks`` (usado quando a exportação excede 10 MB)."""
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        trusted = ("google.com", "googleapis.com", "googleusercontent.com")
        if parts.scheme != "https" or not any(host == d or host.endswith("." + d) for d in trusted):
            raise DriveApiError("Link de exportação inesperado recusado por segurança.")
        return self._send("GET", url, stream=True, context="exportação (exportLinks)")
