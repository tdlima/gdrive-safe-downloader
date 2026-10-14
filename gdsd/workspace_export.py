"""Exportação de arquivos nativos do Google Workspace (Docs, Sheets, Slides...).

Arquivos nativos não têm conteúdo binário próprio: são CONVERTIDOS pelo Google no
momento da exportação. A conversão pode perder formatação, comentários, scripts,
fórmulas específicas ou outros recursos, e o resultado pode variar entre duas
exportações do mesmo arquivo — por isso exportações nunca são retomadas por
intervalo de bytes e não possuem checksum de referência do Google.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import ExportNotSupported

GOOGLE_APPS_PREFIX = "application/vnd.google-apps."


@dataclass(frozen=True)
class ExportFormat:
    key: str
    mime: str
    extension: str
    note: str = ""


def _f(key: str, mime: str, ext: str | None = None, note: str = "") -> ExportFormat:
    return ExportFormat(key, mime, ext or key, note)


EXPORT_FORMATS: dict[str, list[ExportFormat]] = {
    "document": [
        _f("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        _f("pdf", "application/pdf"),
        _f("odt", "application/vnd.oasis.opendocument.text"),
        _f("rtf", "application/rtf"),
        _f("txt", "text/plain", note="somente texto"),
        _f("md", "text/markdown"),
        _f("epub", "application/epub+zip"),
        _f("html", "application/zip", "zip", note="HTML compactado em .zip"),
    ],
    "spreadsheet": [
        _f("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        _f("ods", "application/x-vnd.oasis.opendocument.spreadsheet"),
        _f("pdf", "application/pdf"),
        _f("csv", "text/csv", note="somente a primeira aba"),
        _f("tsv", "text/tab-separated-values", note="somente a primeira aba"),
        _f("html", "application/zip", "zip", note="HTML compactado em .zip"),
    ],
    "presentation": [
        _f("pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
        _f("odp", "application/vnd.oasis.opendocument.presentation"),
        _f("pdf", "application/pdf"),
        _f("txt", "text/plain", note="somente texto"),
    ],
    "drawing": [
        _f("pdf", "application/pdf"),
        _f("png", "image/png"),
        _f("jpg", "image/jpeg"),
        _f("svg", "image/svg+xml"),
    ],
    "script": [
        _f("json", "application/vnd.google-apps.script+json", note="projeto Apps Script"),
    ],
}

KIND_BY_MIME = {
    "application/vnd.google-apps.document": "document",
    "application/vnd.google-apps.spreadsheet": "spreadsheet",
    "application/vnd.google-apps.presentation": "presentation",
    "application/vnd.google-apps.drawing": "drawing",
    "application/vnd.google-apps.script": "script",
}

KIND_LABEL = {
    "document": "Documento Google",
    "spreadsheet": "Planilha Google",
    "presentation": "Apresentação Google",
    "drawing": "Desenho Google",
    "script": "Apps Script",
}

NOT_EXPORTABLE = {
    "application/vnd.google-apps.form": "Formulários Google não podem ser exportados pela API.",
    "application/vnd.google-apps.site": "Google Sites não podem ser exportados pela API.",
    "application/vnd.google-apps.map": "Mapas do My Maps não podem ser exportados pela API.",
    "application/vnd.google-apps.jam": "Jamboards não podem ser exportados pela API.",
    "application/vnd.google-apps.fusiontable": "Fusion Tables foram descontinuadas.",
    "application/vnd.google-apps.vid": "Vídeos do Google Vids não podem ser exportados pela API.",
}


def is_google_native(mime: str | None) -> bool:
    return bool(mime) and mime.startswith(GOOGLE_APPS_PREFIX)


def export_kind(mime: str) -> str | None:
    return KIND_BY_MIME.get(mime)


def formats_for(mime: str) -> list[ExportFormat]:
    kind = export_kind(mime)
    return list(EXPORT_FORMATS.get(kind, [])) if kind else []


def resolve_format(mime: str, requested: str | None, defaults: dict[str, str]) -> ExportFormat:
    """Escolhe o formato de exportação para ``mime``.

    ``requested`` (ex.: ``"pdf"``) tem prioridade; senão usa o padrão do tipo.
    Lança :class:`ExportNotSupported` quando não houver formato válido.
    """
    if mime in NOT_EXPORTABLE:
        raise ExportNotSupported(NOT_EXPORTABLE[mime])
    kind = export_kind(mime)
    if kind is None:
        raise ExportNotSupported(f"Tipo Google sem exportação suportada: {mime}.")
    options = EXPORT_FORMATS[kind]
    wanted = (requested or defaults.get(kind) or options[0].key).lower().lstrip(".")
    for fmt in options:
        if fmt.key == wanted:
            return fmt
    valid = ", ".join(f.key for f in options)
    raise ExportNotSupported(
        f"Formato '{wanted}' não disponível para {KIND_LABEL[kind]}. Opções: {valid}."
    )
