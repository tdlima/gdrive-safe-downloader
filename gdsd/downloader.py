"""Download de arquivos individuais: streaming em blocos, retomada por intervalo
de bytes, verificação de integridade e gravação atômica.

Ciclo de vida de um arquivo:

1. ``plan()`` registra (ou reaproveita) uma tarefa no histórico e decide conflitos.
2. ``run()`` baixa para ``~gdsd-<id>-<nome>.gdpart`` na pasta de destino.
3. O arquivo parcial é verificado (tamanho + checksum do Google quando houver).
4. Só então é movido para o nome definitivo, sem sobrescrever nada que exista
   (a menos que ``--on-conflict overwrite`` tenha sido pedido explicitamente).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import threading
from collections.abc import Iterable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from . import history as H
from . import integrity
from .config import Settings
from .drive_client import FOLDER_MIME, SHORTCUT_MIME, DriveClient
from .errors import (
    DownloadNotAllowed,
    ExportNotSupported,
    ExportSizeLimitExceeded,
    GDSDError,
    InsufficientSpaceError,
    IntegrityError,
    Interrupted,
    RangeNotSatisfiable,
    RemoteChangedError,
    TransientError,
)
from .history import History
from .paths import (
    NameAllocator,
    exists,
    invalid_path_for,
    io_path,
    move_no_overwrite,
    move_overwrite,
    temp_path_for,
)
from .retry import NETWORK_ERRORS, RetryPolicy
from .workspace_export import ExportFormat, is_google_native, resolve_format

log = logging.getLogger(__name__)

READ_BLOCK = 256 * 1024
_CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+|\*)")


@dataclass
class Outcome:
    task_id: int | None
    status: str
    name: str
    local_path: str | None = None
    message: str = ""
    integrity: str | None = None
    size: int | None = None
    unchanged: bool = False  # já estava baixado e atualizado (nenhuma ação)


UPDATE_POLICIES = ("keep-both", "replace", "skip")


@dataclass
class RunResult:
    outcomes: list[Outcome] = field(default_factory=list)
    interrupted: bool = False

    def count(self, status: str) -> int:
        return sum(1 for o in self.outcomes if o.status == status)


class Reporter(Protocol):
    def file_started(self, task_id: int, name: str, total: int | None, offset: int, converting: bool) -> None: ...
    def file_progress(self, task_id: int, nbytes: int) -> None: ...
    def file_retry(self, task_id: int, name: str, attempt: int, delay: float, error: str) -> None: ...
    def file_finished(self, outcome: Outcome) -> None: ...
    def info(self, message: str) -> None: ...


class NullReporter:
    def file_started(self, *a: Any, **k: Any) -> None: ...
    def file_progress(self, *a: Any, **k: Any) -> None: ...
    def file_retry(self, *a: Any, **k: Any) -> None: ...
    def file_finished(self, *a: Any, **k: Any) -> None: ...
    def info(self, *a: Any, **k: Any) -> None: ...


def remote_fingerprint(meta: dict[str, Any]) -> dict[str, Any]:
    """Campos que identificam a versão do conteúdo remoto."""
    size = meta.get("size")
    return {
        "expected_size": int(size) if size is not None else None,
        "google_md5": meta.get("md5Checksum"),
        "google_sha256": meta.get("sha256Checksum"),
        "remote_modified_time": meta.get("modifiedTime"),
        "remote_revision": meta.get("headRevisionId"),
    }


def same_remote_content(recorded: dict[str, Any], meta: dict[str, Any]) -> bool:
    """True somente se houver prova de que o conteúdo remoto não mudou.

    Exige ao menos uma referência forte (revisão ou checksum) presente nos dois
    lados e que todas as referências disponíveis coincidam.
    """
    fresh = remote_fingerprint(meta)
    strong = False
    for key in ("remote_revision", "google_md5", "google_sha256"):
        a, b = recorded.get(key), fresh.get(key)
        if a and b:
            if a != b:
                return False
            strong = True
        elif a or b:
            return False
    for key in ("expected_size", "remote_modified_time"):
        a, b = recorded.get(key), fresh.get(key)
        if a is not None and b is not None and a != b:
            return False
    return strong


class Downloader:
    def __init__(
        self,
        client: DriveClient,
        history: History,
        settings: Settings,
        account_email: str,
        *,
        stop_event: threading.Event | None = None,
        reporter: Reporter | None = None,
        allocator: NameAllocator | None = None,
        retry_policy: RetryPolicy | None = None,
    ):
        self.client = client
        self.history = history
        self.settings = settings
        self.account = account_email
        self.stop_event = stop_event or threading.Event()
        self.reporter = reporter or NullReporter()
        self.allocator = allocator or NameAllocator()
        self.policy = retry_policy or client.retry_policy

    # ================================================================ plano
    def record_final(self, meta: dict[str, Any], local_path: Path, status: str, message: str,
                     *, job_id: int | None, dest_root: Path, parent_id: str | None = None,
                     remote_path: str | None = None) -> Outcome:
        """Registra no histórico um item que não será baixado (ignorado ou erro)."""
        task_id = self.history.create_task(
            job_id=job_id, account_email=self.account, file_id=meta.get("id", "?"),
            file_name=meta.get("name", "?"), mime_type=meta.get("mimeType"), parent_id=parent_id,
            drive_id=meta.get("driveId"), remote_path=remote_path, dest_root=str(dest_root),
            local_path=str(local_path), status=status, error=message,
        )
        outcome = Outcome(task_id, status, meta.get("name", "?"), str(local_path), message)
        self.reporter.file_finished(outcome)
        return outcome

    def plan(
        self,
        meta: dict[str, Any],
        local_path: Path,
        *,
        dest_root: Path,
        export: ExportFormat | None = None,
        job_id: int | None = None,
        parent_id: str | None = None,
        remote_path: str | None = None,
        on_conflict: str | None = None,
        on_update: str = "keep-both",
    ) -> tuple[int | None, Outcome | None]:
        """Prepara a tarefa de download. Retorna ``(task_id, None)`` para baixar
        ou ``(None, outcome)`` quando o item foi ignorado/recusado já no plano.

        ``on_update`` decide o que fazer quando uma versão anterior deste mesmo
        arquivo já foi baixada para esta pasta e o arquivo mudou no Drive:
        ``keep-both`` (padrão) baixa a nova versão como ``nome (2).ext``;
        ``replace`` substitui a cópia local — somente se ela não foi alterada no
        computador (SHA-256 igual ao registrado); ``skip`` mantém a versão local.
        """
        policy = on_conflict or self.settings.on_conflict
        mime = meta.get("mimeType", "")
        if mime in (FOLDER_MIME, SHORTCUT_MIME):
            raise ValueError("plan() recebe apenas arquivos")
        if is_google_native(mime) and export is None:
            raise ExportNotSupported("Arquivo nativo do Google sem formato de exportação definido.")
        common = dict(job_id=job_id, dest_root=dest_root, parent_id=parent_id, remote_path=remote_path)

        if (meta.get("capabilities") or {}).get("canDownload") is False:
            return None, self.record_final(
                meta, local_path, H.ERROR,
                "Download desativado pelo proprietário/administrador para esta conta.", **common)

        fp = remote_fingerprint(meta) if export is None else {
            "expected_size": None, "google_md5": None, "google_sha256": None,
            "remote_modified_time": meta.get("modifiedTime"), "remote_revision": None,
        }
        fields: dict[str, Any] = dict(
            job_id=job_id, account_email=self.account, file_id=meta["id"], file_name=meta.get("name", ""),
            mime_type=mime, parent_id=parent_id, drive_id=meta.get("driveId"),
            resource_key=meta.get("resourceKey"), remote_path=remote_path, dest_root=str(dest_root),
            export_format=export.key if export else None, export_mime=export.mime if export else None,
            on_conflict=policy, **fp,
        )

        # 1) Já baixado nesta mesma pasta (inclusive se foi renomeado por conflito)?
        #    Nenhuma linha nova é criada no histórico: sincronizações repetidas de
        #    pastas grandes não devem inflar o banco.
        current, outdated = self.find_downloaded(meta, local_path, export)
        if current is not None:
            outcome = Outcome(current["id"], H.SKIPPED, meta.get("name", "?"), current["local_path"],
                              "Já baixado e inalterado.", current.get("integrity_status"),
                              current.get("downloaded_bytes"), unchanged=True)
            self.reporter.file_finished(outcome)
            return None, outcome

        # 1b) Uma versão anterior foi baixada aqui, mas o arquivo mudou no Drive.
        if outdated is not None:
            old_path = Path(outdated["local_path"])
            if on_update == "skip":
                return None, self.record_final(
                    meta, old_path, H.SKIPPED,
                    "Arquivo alterado no Drive; versão local mantida (--on-update skip).", **common)
            if on_update == "replace":
                if self._locally_unmodified(outdated):
                    local_path, policy = old_path, "overwrite"
                    fields["on_conflict"] = policy
                    self.reporter.info(f"'{meta.get('name')}' mudou no Drive; a cópia local será substituída.")
                else:
                    self.reporter.info(
                        f"'{meta.get('name')}' mudou no Drive, mas a cópia local foi alterada no "
                        "computador; ela será mantida e a nova versão salva com outro nome.")

        # 2) Tarefa incompleta anterior para o mesmo destino: reaproveita para retomar.
        previous = self.history.find_reusable_task(self.account, meta["id"], str(local_path))
        if previous and previous["status"] in (*H.RESUMABLE, H.ERROR):
            # O fingerprint registrado NÃO é atualizado aqui: run() compara-o com
            # os metadados atuais antes de anexar dados ao arquivo parcial.
            keep = {k: v for k, v in fields.items() if k not in fp}
            self.history.update_task(previous["id"], status=H.PENDING, **keep)
            return previous["id"], None

        if exists(local_path):
            if policy == "skip":
                return None, self.record_final(
                    meta, local_path, H.SKIPPED,
                    "Já existe um arquivo local com este nome (--on-conflict skip).", **common)
            if policy == "rename":
                new_name = self.allocator.reserve(local_path.parent, local_path.name, check_disk=True)
                local_path = local_path.parent / new_name
            # overwrite: substituição explícita feita somente na finalização

        task_id = self.history.create_task(local_path=str(local_path), status=H.PENDING, **fields)
        self.history.update_task(task_id, temp_path=str(temp_path_for(local_path, task_id)))
        return task_id, None

    def find_downloaded(self, meta: dict[str, Any], local_path: Path,
                        export: ExportFormat | None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Procura downloads concluídos deste arquivo remoto na pasta de ``local_path``.

        Retorna ``(atual, desatualizado)``: ``atual`` é uma tarefa cujo arquivo
        local existe e corresponde à versão remota de agora; ``desatualizado`` é a
        tarefa mais recente cujo arquivo local existe, mas o Drive tem versão nova.
        """
        export_key = export.key if export else None
        outdated: dict[str, Any] | None = None
        for done in self.history.completed_for_file(self.account, meta["id"]):
            done_path = Path(done["local_path"])
            if os.path.normcase(str(done_path.parent)) != os.path.normcase(str(local_path.parent)):
                continue
            if done.get("export_format") != export_key:
                continue
            if not exists(done_path) or os.path.getsize(io_path(done_path)) != done.get("downloaded_bytes"):
                continue
            unchanged = (
                done.get("remote_modified_time") == meta.get("modifiedTime")
                if export else same_remote_content(done, meta)
            )
            if unchanged:
                return done, None
            if outdated is None:
                outdated = done
        return None, outdated

    def _locally_unmodified(self, task: dict[str, Any]) -> bool:
        if not task.get("sha256"):
            return False
        try:
            return integrity.check_against_record(Path(task["local_path"]), task["sha256"])[0]
        except OSError:
            return False

    # ============================================================== execução
    def run(self, task_id: int) -> Outcome:
        task = self.history.get_task(task_id)
        if task is None:
            return Outcome(task_id, H.ERROR, "?", message="Tarefa inexistente.")
        name = task["file_name"]
        if task["status"] in (H.COMPLETED, H.CANCELLED, H.SKIPPED):
            return Outcome(task_id, task["status"], name, task["local_path"], "Nada a fazer.")
        if self.stop_event.is_set():
            self.history.update_task(task_id, status=H.INTERRUPTED)
            return Outcome(task_id, H.INTERRUPTED, name, task["local_path"], "Não iniciado.")

        self.history.update_task(
            task_id, status=H.RUNNING, attempts=(task["attempts"] or 0) + 1,
            last_attempt_at=H.now_iso(), error=None,
        )
        try:
            outcome = self._run(task)
        except Interrupted:
            self.history.update_task(task_id, status=H.INTERRUPTED, error="Interrompido; o arquivo parcial foi mantido.")
            outcome = Outcome(task_id, H.INTERRUPTED, name, task["local_path"], "Interrompido.")
        except GDSDError as exc:
            self.history.update_task(task_id, status=H.ERROR, error=str(exc))
            outcome = Outcome(task_id, H.ERROR, name, task["local_path"], str(exc))
        except Exception as exc:  # noqa: BLE001 - nunca derrubar o lote inteiro
            log.exception("erro inesperado", extra={"ctx": {"task": task_id}})
            msg = f"Erro inesperado: {type(exc).__name__}: {exc}"
            self.history.update_task(task_id, status=H.ERROR, error=msg)
            outcome = Outcome(task_id, H.ERROR, name, task["local_path"], msg)
        self.reporter.file_finished(outcome)
        return outcome

    def _run(self, task: dict[str, Any]) -> Outcome:
        task_id = task["id"]
        final = Path(task["local_path"])
        temp = Path(task["temp_path"]) if task.get("temp_path") else temp_path_for(final, task_id)
        os.makedirs(io_path(final.parent), exist_ok=True)
        self.client.register_resource_key(task["file_id"], task.get("resource_key"))

        meta = self.client.get_file(task["file_id"])
        if meta.get("trashed"):
            raise GDSDError("O arquivo foi movido para a lixeira no Google Drive.")
        if (meta.get("capabilities") or {}).get("canDownload") is False:
            raise DownloadNotAllowed("Download desativado pelo proprietário/administrador para esta conta.")
        mime = meta.get("mimeType", "")
        if mime in (FOLDER_MIME, SHORTCUT_MIME):
            raise GDSDError("Esta tarefa refere-se a uma pasta ou atalho; use o comando 'folder'.")
        if is_google_native(mime) and not task.get("export_mime"):
            fmt = resolve_format(mime, None, self.settings.export_formats)
            self.history.update_task(task_id, export_format=fmt.key, export_mime=fmt.mime)
            task = {**task, "export_format": fmt.key, "export_mime": fmt.mime}

        is_export = bool(task.get("export_mime"))
        offset = 0
        resumed = False
        expected: int | None = None
        if is_export:
            if exists(temp):
                os.remove(io_path(temp))  # exportações nunca são retomadas
            self.history.update_task(task_id, remote_modified_time=meta.get("modifiedTime"),
                                     temp_path=str(temp), downloaded_bytes=0)
        else:
            fp = remote_fingerprint(meta)
            expected = fp["expected_size"]
            if exists(temp):
                part = os.path.getsize(io_path(temp))
                if same_remote_content(task, meta) and (expected is None or part <= expected):
                    offset, resumed = part, part > 0
                    if resumed:
                        self.reporter.info(f"Retomando '{task['file_name']}' a partir de {part} bytes.")
                else:
                    self.reporter.info(
                        f"'{task['file_name']}': não é seguro continuar o arquivo parcial "
                        "(conteúdo remoto alterado ou sem referência); reiniciando do zero.")
                    os.remove(io_path(temp))
            self.history.update_task(task_id, temp_path=str(temp), downloaded_bytes=offset, **fp)

        self._check_space(final.parent, None if expected is None else expected - offset)

        for full_restart in (False, True):
            meta = self._transfer(task, meta, temp, offset, is_export)
            fp = remote_fingerprint(meta)
            result = integrity.verify_file(
                temp,
                None if is_export else fp["expected_size"],
                None if is_export else fp["google_md5"],
                None if is_export else fp["google_sha256"],
                stop_event=self.stop_event,
            )
            if result.ok:
                break
            if resumed and not full_restart:
                # A retomada pode ter combinado dados incompatíveis: uma nova
                # tentativa completa, do zero, antes de declarar falha.
                self.reporter.info(f"'{task['file_name']}': verificação falhou após retomada; baixando do zero.")
                os.remove(io_path(temp))
                offset, resumed = 0, False
                continue
            bad = invalid_path_for(final, task_id)
            move_overwrite(temp, bad)
            self.history.update_task(
                task_id, integrity_status=integrity.FAILED, integrity_detail=result.detail,
                sha256=result.hashes.sha256, downloaded_bytes=result.hashes.size,
            )
            raise IntegrityError(
                f"Verificação de integridade falhou ({result.detail}). "
                f"Arquivo inválido mantido como '{bad.name}'. Use 'retry' para baixar novamente."
            )

        if is_export:
            detail = "Arquivo convertido pelo Google: não há checksum de referência para exportações."
        else:
            detail = result.detail
        target = self._finalize(temp, final, task.get("on_conflict") or self.settings.on_conflict)
        bad = invalid_path_for(final, task_id)
        if exists(bad):
            os.remove(io_path(bad))
        self.history.update_task(
            task_id, status=H.COMPLETED, local_path=str(target), temp_path=None,
            sha256=result.hashes.sha256, downloaded_bytes=result.hashes.size,
            integrity_status=result.status, integrity_detail=detail, error=None,
        )
        msg = integrity.LABELS[result.status]
        if target != final:
            msg += f" — salvo como '{target.name}' para não sobrescrever arquivo existente"
        return Outcome(task_id, H.COMPLETED, task["file_name"], str(target), msg, result.status, result.hashes.size)

    # ------------------------------------------------------------ transfer
    def _transfer(self, task: dict[str, Any], meta: dict[str, Any], temp: Path, offset: int,
                  is_export: bool) -> dict[str, Any]:
        """Executa a transferência com retentativas. Retorna os metadados vigentes."""
        task_id = task["id"]
        attempt = 0
        while True:
            try:
                if is_export:
                    self._download_export(task, meta, temp)
                else:
                    self._download_media(task, temp, offset, remote_fingerprint(meta)["expected_size"])
                return meta
            except (TransientError, RemoteChangedError, *NETWORK_ERRORS) as exc:
                attempt += 1
                if attempt > self.policy.max_retries:
                    if isinstance(exc, GDSDError):
                        raise
                    raise TransientError(
                        f"Falha de rede persistente após {attempt} tentativas ({type(exc).__name__})."
                    ) from exc
                delay = self.policy.delay(attempt, getattr(exc, "retry_after", None))
                reason = str(exc) or type(exc).__name__
                self.reporter.file_retry(task_id, task["file_name"], attempt, delay, reason)
                self.history.update_task(task_id, error=f"Tentativa {attempt} falhou: {reason}")
                log.warning("falha na transferência", extra={"ctx": {
                    "task": task_id, "attempt": attempt, "error": type(exc).__name__}})
                self.policy.wait(delay, self.stop_event)
                if is_export:
                    continue  # recomeça a exportação do zero
                fresh = self.client.get_file(task["file_id"])
                if isinstance(exc, RemoteChangedError) or not same_remote_content(remote_fingerprint(meta), fresh):
                    self.reporter.info(f"'{task['file_name']}': conteúdo remoto mudou; reiniciando do zero.")
                    if exists(temp):
                        os.remove(io_path(temp))
                    offset = 0
                    meta = fresh
                    self.history.update_task(task_id, downloaded_bytes=0, **remote_fingerprint(fresh))
                else:
                    offset = os.path.getsize(io_path(temp)) if exists(temp) else 0

    def _download_media(self, task: dict[str, Any], temp: Path, offset: int, expected: int | None) -> None:
        if expected is not None and offset == expected and offset > 0:
            return  # já completo; segue para verificação
        try:
            resp = self.client.open_media(task["file_id"], offset)
        except RangeNotSatisfiable:
            raise RemoteChangedError("Intervalo de retomada recusado pelo servidor.") from None
        mode = "wb"
        if offset > 0:
            if resp.status_code == 206:
                match = _CONTENT_RANGE.match(resp.headers.get("Content-Range", ""))
                total_ok = (match and (match.group(3) == "*" or expected is None
                                       or int(match.group(3)) == expected))
                if not match or int(match.group(1)) != offset or not total_ok:
                    resp.close()
                    raise RemoteChangedError("Resposta de intervalo inconsistente.")
                mode = "ab"
            else:
                self.reporter.info(f"'{task['file_name']}': o servidor não aceitou retomada; reiniciando do zero.")
                offset = 0
        self._write_stream(task, resp, temp, mode, offset, expected, converting=False)

    def _download_export(self, task: dict[str, Any], meta: dict[str, Any], temp: Path) -> None:
        mime = task["export_mime"]
        try:
            resp = self.client.open_export(task["file_id"], mime)
        except ExportSizeLimitExceeded:
            link = (meta.get("exportLinks") or {}).get(mime)
            if not link:
                raise ExportNotSupported(
                    "O arquivo excede o limite de 10 MB de exportação da API e o Google não "
                    f"ofereceu link alternativo para {task['export_format']}."
                ) from None
            self.reporter.info(f"'{task['file_name']}': arquivo grande; usando link de exportação alternativo.")
            resp = self.client.open_url(link)
        self._write_stream(task, resp, temp, "wb", 0, None, converting=True)

    def _write_stream(self, task: dict[str, Any], resp: Any, temp: Path, mode: str, offset: int,
                      expected: int | None, converting: bool) -> None:
        task_id = task["id"]
        written = offset
        received = 0
        checkpoint = offset
        self.reporter.file_started(task_id, task["file_name"], expected, offset, converting)
        try:
            with open(io_path(temp), mode) as fh:
                try:
                    for block in resp.iter_content(chunk_size=READ_BLOCK):
                        if self.stop_event.is_set():
                            raise Interrupted("Download interrompido.")
                        if not block:
                            continue
                        fh.write(block)
                        written += len(block)
                        received += len(block)
                        if expected is not None and written > expected:
                            raise RemoteChangedError("Recebidos mais bytes que o tamanho esperado.")
                        self.reporter.file_progress(task_id, len(block))
                        if written - checkpoint >= self.settings.chunk_size:
                            fh.flush()
                            os.fsync(fh.fileno())
                            self.history.update_task(task_id, downloaded_bytes=written)
                            checkpoint = written
                finally:
                    fh.flush()
                    os.fsync(fh.fileno())
                    self.history.update_task(task_id, downloaded_bytes=written)
        finally:
            resp.close()
        length = resp.headers.get("Content-Length") if getattr(resp, "headers", None) else None
        if length is not None and length.isdigit() and received != int(length):
            raise TransientError(
                f"Conexão encerrada antes do fim ({received} de {length} bytes recebidos)."
            )

    # ------------------------------------------------------------ auxiliares
    def _check_space(self, directory: Path, needed: int | None) -> None:
        probe = Path(os.path.abspath(directory))
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        try:
            free = shutil.disk_usage(io_path(probe)).free
        except OSError:
            return  # não foi possível consultar; segue sem a verificação
        required = (needed or 0) + self.settings.min_free_space
        if free < required:
            raise InsufficientSpaceError(
                f"Espaço insuficiente em {probe}: livre {human_size(free)}, "
                f"necessário {human_size(needed or 0)} + margem de {human_size(self.settings.min_free_space)}."
            )

    def _finalize(self, temp: Path, final: Path, policy: str) -> Path:
        if exists(final) and policy == "overwrite":
            move_overwrite(temp, final)
            return final
        target = final
        while True:
            if not exists(target):
                try:
                    move_no_overwrite(temp, target)
                    return target
                except FileExistsError:
                    pass
            target = final.parent / self.allocator.reserve(final.parent, final.name, check_disk=True)

    # ======================================================== execução em lote
    def run_tasks(self, task_ids: Iterable[int], max_workers: int | None = None) -> RunResult:
        """Executa tarefas em paralelo (limitado). Ctrl+C interrompe com segurança."""
        ids = list(task_ids)
        result = RunResult()
        if not ids:
            return result
        workers = max(1, min(max_workers or self.settings.max_workers, len(ids)))
        executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="download")
        futures = {executor.submit(self.run, tid): tid for tid in ids}
        pending = set(futures)
        try:
            while pending:
                done, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
                for fut in done:
                    result.outcomes.append(fut.result())
        except KeyboardInterrupt:
            result.interrupted = True
            self.stop_event.set()
            self.reporter.info("Interrompendo com segurança... os arquivos parciais serão mantidos.")
            for fut in pending:
                fut.cancel()
            for fut in pending:
                tid = futures[fut]
                if fut.cancelled():
                    self.history.update_task(tid, status=H.INTERRUPTED, error="Não iniciado (interrompido).")
                    task = self.history.get_task(tid) or {}
                    result.outcomes.append(Outcome(tid, H.INTERRUPTED, task.get("file_name", "?"),
                                                   task.get("local_path"), "Não iniciado."))
                else:
                    try:
                        result.outcomes.append(fut.result())
                    except Exception:  # noqa: BLE001
                        pass
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
        if self.stop_event.is_set():
            result.interrupted = True
        return result


def human_size(num: int | float | None) -> str:
    if num is None:
        return "?"
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"
