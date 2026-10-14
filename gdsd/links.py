"""Extração e validação de IDs a partir de links do Google Drive.

Conhecer um link NÃO concede acesso: o ID extraído aqui ainda é consultado na
API com a conta autenticada, que precisa ter permissão sobre o recurso.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import parse_qs, urlsplit

from .errors import InvalidLinkError

ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,128}$")
ALLOWED_HOSTS = {"drive.google.com", "docs.google.com", "drive.usercontent.google.com"}

Kind = Literal["file", "folder", "unknown"]

_PATH_PATTERNS: list[tuple[re.Pattern[str], Kind]] = [
    (re.compile(r"^/drive/(?:u/\d+/)?folders/([A-Za-z0-9_-]+)"), "folder"),
    (re.compile(r"^/(?:a/[^/]+/)?file/(?:u/\d+/)?d/([A-Za-z0-9_-]+)"), "file"),
    (
        re.compile(
            r"^/(?:a/[^/]+/)?(?:document|spreadsheets|presentation|drawings|forms|jamboard)"
            r"/(?:u/\d+/)?d/([A-Za-z0-9_-]+)"
        ),
        "file",
    ),
]
_PUBLISHED_RE = re.compile(r"^/(?:document|spreadsheets|presentation|forms)/d/e/")
_SECTION_RE = re.compile(
    r"^/drive/(?:u/\d+/)?(?:my-drive|home|shared-with-me|recent|starred|trash|shared-drives|computers)"
)


@dataclass(frozen=True)
class DriveRef:
    """Referência a um recurso do Drive extraída de um link ou ID."""

    file_id: str
    resource_key: str | None = None
    kind_hint: Kind = "unknown"


def _validate_id(candidate: str) -> str:
    if not ID_RE.fullmatch(candidate):
        raise InvalidLinkError(f"ID do Google Drive inválido: {candidate!r}.")
    return candidate


def parse_drive_reference(text: str) -> DriveRef:
    """Converte um link de compartilhamento ou ID em :class:`DriveRef`.

    Aceita, entre outros:
    ``https://drive.google.com/file/d/ID/view``, ``.../drive/folders/ID``,
    ``.../open?id=ID``, ``.../uc?id=ID``, ``https://docs.google.com/document/d/ID/edit``
    e o ID puro. O parâmetro ``resourcekey`` é preservado (necessário para alguns
    arquivos antigos compartilhados por link).
    """
    raw = (text or "").strip().strip('"').strip("'")
    if not raw:
        raise InvalidLinkError("Informe um link ou ID do Google Drive.")

    if "/" not in raw and "." not in raw and "?" not in raw:
        return DriveRef(_validate_id(raw))

    url = raw if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw) else "https://" + raw
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise InvalidLinkError("O link deve usar http ou https.")
    host = (parts.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise InvalidLinkError(
            f"O domínio {host or '(vazio)'!r} não é do Google Drive. "
            "Use um link de drive.google.com ou docs.google.com."
        )

    query = parse_qs(parts.query)
    resource_key = (query.get("resourcekey") or [None])[0]
    path = parts.path or "/"

    if _PUBLISHED_RE.match(path):
        raise InvalidLinkError(
            "Este é um link de 'Publicar na Web', que não contém o ID do arquivo. "
            "Use o link de compartilhamento do arquivo."
        )
    if _SECTION_RE.match(path):
        raise InvalidLinkError(
            "O link aponta para uma seção do Drive, não para um arquivo ou pasta específica."
        )

    for pattern, kind in _PATH_PATTERNS:
        match = pattern.match(path)
        if match:
            return DriveRef(_validate_id(match.group(1)), resource_key, kind)

    for key in ("id", "ids"):
        values = query.get(key)
        if values:
            first = values[0].split(",")[0]
            return DriveRef(_validate_id(first), resource_key, "unknown")

    raise InvalidLinkError("Não foi possível identificar o ID do arquivo ou pasta neste link.")
