"""Recuperação de tarefas após reinício, retomada, nova tentativa e cancelamento.

A retomada propriamente dita (comparação do arquivo remoto e ``Range``) ocorre em
:meth:`gdsd.downloader.Downloader.run`; aqui ficam as regras sobre QUAIS tarefas
podem ser retomadas e a trava que impede duas instâncias simultâneas.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import IO, Any

from . import history as H
from .errors import AnotherInstanceRunning
from .history import History
from .paths import exists, invalid_path_for, io_path


class InstanceLock:
    """Trava de arquivo exclusiva: garante uma única instância baixando por vez.

    O sistema operacional libera a trava automaticamente se o processo morrer,
    então não há travas "presas" após falhas.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fh: IO[str] | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+", encoding="utf-8")
        try:
            if sys.platform == "win32":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            raise AnotherInstanceRunning(
                "Outra execução do programa está baixando arquivos agora. "
                "Aguarde terminar ou encerre-a antes de continuar."
            ) from exc
        self._fh = fh

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self._fh.close()
        self._fh = None

    def __enter__(self) -> "InstanceLock":
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def recover_after_restart(history: History) -> int:
    """Marca como interrompidas as tarefas que estavam 'em andamento' quando o
    programa terminou inesperadamente. Chamar somente com a trava adquirida."""
    return history.mark_stale_running()


def resumable_tasks(history: History, account_email: str) -> list[dict[str, Any]]:
    return history.list_tasks(statuses=H.RESUMABLE, account_email=account_email, newest_first=False)


def prepare_resume(history: History, account_email: str, task_ids: list[int] | None = None) -> list[int]:
    """Seleciona tarefas pendentes/interrompidas para retomar."""
    tasks = resumable_tasks(history, account_email)
    if task_ids:
        wanted = set(task_ids)
        tasks = [t for t in tasks if t["id"] in wanted]
    return [t["id"] for t in tasks]


def prepare_retry(history: History, account_email: str, task_ids: list[int] | None = None,
                  include_all_errors: bool = False) -> tuple[list[int], list[str]]:
    """Recoloca tarefas com erro (ou explicitamente indicadas) como pendentes.

    Arquivos ``.gdinvalid`` (falha de integridade) são mantidos até que a nova
    tentativa termine com sucesso.
    """
    messages: list[str] = []
    selected: list[dict[str, Any]] = []
    if task_ids:
        for tid in task_ids:
            task = history.get_task(tid)
            if task is None or task["account_email"] != account_email:
                messages.append(f"Tarefa #{tid} não encontrada para esta conta.")
            elif task["status"] == H.COMPLETED:
                messages.append(f"Tarefa #{tid} já foi concluída.")
            elif task["status"] == H.RUNNING:
                messages.append(f"Tarefa #{tid} está em andamento.")
            else:
                selected.append(task)
    elif include_all_errors:
        selected = history.list_tasks(statuses=[H.ERROR], account_email=account_email, newest_first=False)
    ids: list[int] = []
    for task in selected:
        history.update_task(task["id"], status=H.PENDING, error=None)
        ids.append(task["id"])
    return ids, messages


def cancel_tasks(history: History, account_email: str, task_ids: list[int],
                 delete_partial: bool = False) -> list[str]:
    messages: list[str] = []
    for tid in task_ids:
        task = history.get_task(tid)
        if task is None or task["account_email"] != account_email:
            messages.append(f"Tarefa #{tid} não encontrada para esta conta.")
            continue
        if task["status"] in (H.COMPLETED, H.SKIPPED, H.CANCELLED):
            messages.append(f"Tarefa #{tid} já está {H.STATUS_LABELS[task['status']]}; nada a cancelar.")
            continue
        removed = ""
        if delete_partial:
            for path in filter(None, [task.get("temp_path"),
                                      str(invalid_path_for(Path(task["local_path"]), tid))]):
                if exists(Path(path)):
                    os.remove(io_path(path))
                    removed = " (arquivo parcial removido)"
        history.update_task(tid, status=H.CANCELLED, error="Cancelada pelo usuário.")
        messages.append(f"Tarefa #{tid} cancelada{removed}: {task['file_name']}")
    return messages
