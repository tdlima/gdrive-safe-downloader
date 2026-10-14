"""Download recursivo de pastas preservando a estrutura de diretórios.

Etapas:
1. Mapeamento: percorre a árvore (iterativo, sem recursão Python), resolve
   atalhos, define nomes locais únicos e registra as tarefas no histórico.
2. Execução: as tarefas são baixadas em paralelo por :class:`Downloader`.

Nomes duplicados na mesma pasta do Drive são permitidos pelo Google, mas não no
Windows: são ordenados de forma determinística (nome, data de criação, ID) e
recebem sufixos ``(2)``, ``(3)``... — a mesma pasta remota gera sempre os mesmos
nomes locais, o que permite retomar e reexecutar sem duplicar arquivos.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import history as H
from .downloader import Downloader, Outcome
from .drive_client import FOLDER_MIME, SHORTCUT_MIME, DriveClient
from .errors import AuthError, DriveApiError, ExportNotSupported, GDSDError, Interrupted, UnsafePathError
from .paths import ensure_extension, safe_join, sanitize_component, split_ext
from .workspace_export import ExportFormat, export_kind, is_google_native, resolve_format

log = logging.getLogger(__name__)


@dataclass
class FolderOptions:
    export_overrides: dict[str, str] = field(default_factory=dict)
    on_conflict: str | None = None
    on_update: str = "keep-both"
    dry_run: bool = False
    max_depth: int = 100
    root_dir_name: str | None = None  # nome local da pasta raiz (sincronização)


@dataclass
class PlanResult:
    task_ids: list[int] = field(default_factory=list)
    early: list[Outcome] = field(default_factory=list)
    folders: int = 0
    files: int = 0
    known_bytes: int = 0
    unknown_sizes: int = 0
    local_root: Path | None = None
    dry_items: list[str] = field(default_factory=list)
    new_files: list[str] = field(default_factory=list)      # caminhos relativos
    updated_files: list[str] = field(default_factory=list)  # alterados no Drive
    unchanged: int = 0


@dataclass
class _Entry:
    kind: str  # "folder" | "file"
    name: str
    meta: dict[str, Any]
    export: ExportFormat | None = None
    via_shortcut: bool = False


def local_file_name(meta: dict[str, Any], export: ExportFormat | None, name: str | None = None,
                    target_name: str | None = None) -> str:
    """Nome local seguro de um arquivo, com a extensão da exportação quando houver.

    ``target_name`` (nome do arquivo de destino de um atalho) fornece a extensão
    quando o nome do atalho não a possui.
    """
    result = sanitize_component(name or meta.get("name") or meta.get("id", "arquivo"))
    if export:
        return ensure_extension(result, export.extension)
    if target_name:
        _, ext = split_ext(target_name)
        if ext:
            result = ensure_extension(result, ext)
    return result


class FolderDownloader:
    def __init__(self, client: DriveClient, downloader: Downloader, export_defaults: dict[str, str]):
        self.client = client
        self.downloader = downloader
        self.export_defaults = export_defaults
        self.reporter = downloader.reporter

    def _export_for(self, meta: dict[str, Any], options: FolderOptions) -> ExportFormat | None:
        mime = meta.get("mimeType", "")
        if not is_google_native(mime):
            return None
        kind = export_kind(mime)
        requested = options.export_overrides.get(kind) if kind else None
        return resolve_format(mime, requested, self.export_defaults)

    def root_name(self, root: dict[str, Any]) -> str:
        if root.get("driveId") and root.get("id") == root.get("driveId"):
            return sanitize_component(self.client.drive_name(root["driveId"]))
        return sanitize_component(root.get("name") or root["id"])

    def plan(self, root: dict[str, Any], dest_root: Path, job_id: int | None,
             options: FolderOptions | None = None) -> PlanResult:
        options = options or FolderOptions()
        result = PlanResult()
        root_dir_name = (sanitize_component(options.root_dir_name) if options.root_dir_name
                         else self.root_name(root))
        result.local_root = safe_join(dest_root, root_dir_name)
        visited = {root["id"]}
        # (pasta remota, componentes do caminho local relativos a dest_root, profundidade)
        stack: list[tuple[dict[str, Any], list[str], int]] = [(root, [root_dir_name], 0)]

        def finish(meta: dict[str, Any], comps: list[str], status: str, msg: str, parent: str | None) -> None:
            rel = "/".join(comps)
            if options.dry_run:
                result.dry_items.append(f"[{H.STATUS_LABELS[status].upper()}] {rel} — {msg}")
                return
            try:
                local = safe_join(dest_root, *comps)
            except UnsafePathError:
                local = dest_root
            result.early.append(self.downloader.record_final(
                meta, local, status, msg, job_id=job_id, dest_root=dest_root,
                parent_id=parent, remote_path=rel))

        while stack:
            folder, comps, depth = stack.pop()
            result.folders += 1
            self.reporter.info(f"Mapeando: {'/'.join(comps)}")
            try:
                children = list(self.client.list_children(folder))
            except (AuthError, Interrupted):
                raise  # não adianta continuar
            except GDSDError as exc:
                finish(folder, comps, H.ERROR, f"Não foi possível listar a pasta: {exc}", None)
                continue

            entries: list[_Entry] = []
            for child in children:
                mime = child.get("mimeType", "")
                if mime == SHORTCUT_MIME:
                    entry = self._resolve_shortcut(child, options, comps, finish, folder["id"])
                    if entry:
                        entries.append(entry)
                elif mime == FOLDER_MIME:
                    entries.append(_Entry("folder", sanitize_component(child.get("name", "")), child))
                else:
                    try:
                        export = self._export_for(child, options)
                    except ExportNotSupported as exc:
                        finish(child, [*comps, sanitize_component(child.get("name", ""))],
                               H.SKIPPED, str(exc), folder["id"])
                        continue
                    entries.append(_Entry("file", local_file_name(child, export), child, export))

            entries.sort(key=lambda e: (e.name.casefold(), e.meta.get("createdTime") or "", e.meta.get("id", "")))
            local_dir = safe_join(dest_root, *comps)
            for entry in entries:
                name = self.downloader.allocator.reserve(local_dir, entry.name)
                child_comps = [*comps, name]
                if entry.kind == "folder":
                    fid = entry.meta["id"]
                    if fid in visited:
                        finish(entry.meta, child_comps, H.SKIPPED,
                               "Pasta já incluída (atalho circular ou repetido).", folder["id"])
                        continue
                    if depth + 1 > options.max_depth:
                        finish(entry.meta, child_comps, H.SKIPPED,
                               f"Profundidade máxima ({options.max_depth}) atingida.", folder["id"])
                        continue
                    visited.add(fid)
                    stack.append((entry.meta, child_comps, depth + 1))
                    continue
                self._plan_file(entry, child_comps, dest_root, job_id, folder["id"], options, result)
        return result

    def _resolve_shortcut(self, shortcut: dict[str, Any], options: FolderOptions, comps: list[str],
                          finish: Any, parent_id: str) -> _Entry | None:
        details = shortcut.get("shortcutDetails") or {}
        target_id = details.get("targetId")
        label = sanitize_component(shortcut.get("name", ""))
        if not target_id:
            finish(shortcut, [*comps, label], H.ERROR, "Atalho sem destino definido.", parent_id)
            return None
        try:
            target = self.client.get_file(target_id)
        except DriveApiError as exc:
            finish(shortcut, [*comps, label], H.ERROR,
                   f"O atalho aponta para um item inacessível: {exc}", parent_id)
            return None
        if target.get("mimeType") == FOLDER_MIME:
            return _Entry("folder", label, target, via_shortcut=True)
        if target.get("mimeType") == SHORTCUT_MIME:
            finish(shortcut, [*comps, label], H.SKIPPED, "Atalho para outro atalho não é seguido.", parent_id)
            return None
        try:
            export = self._export_for(target, options)
        except ExportNotSupported as exc:
            finish(target, [*comps, label], H.SKIPPED, str(exc), parent_id)
            return None
        name = local_file_name(target, export, name=shortcut.get("name"), target_name=target.get("name"))
        return _Entry("file", name, target, export, via_shortcut=True)

    def _plan_file(self, entry: _Entry, comps: list[str], dest_root: Path, job_id: int | None,
                   parent_id: str, options: FolderOptions, result: PlanResult) -> None:
        meta = entry.meta
        rel = "/".join(comps)
        result.files += 1
        local = safe_join(dest_root, *comps)

        current, outdated = self.downloader.find_downloaded(meta, local, entry.export)
        if current is not None:
            result.unchanged += 1
            if options.dry_run:
                return
        else:
            (result.updated_files if outdated is not None else result.new_files).append(rel)
            size = meta.get("size")
            if size is not None and entry.export is None:
                result.known_bytes += int(size)
            else:
                result.unknown_sizes += 1

        if options.dry_run:
            label = "ALTERADO NO DRIVE" if outdated is not None else "NOVO"
            extra = f" (exportar como {entry.export.key})" if entry.export else ""
            via = " [via atalho]" if entry.via_shortcut else ""
            result.dry_items.append(f"[{label}] {rel}{extra}{via}")
            return
        task_id, outcome = self.downloader.plan(
            meta, local, dest_root=dest_root, export=entry.export, job_id=job_id,
            parent_id=parent_id, remote_path=rel, on_conflict=options.on_conflict,
            on_update=options.on_update,
        )
        if outcome is not None:
            result.early.append(outcome)
        elif task_id is not None:
            result.task_ids.append(task_id)
