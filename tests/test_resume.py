"""Retomada após interrupção e após reiniciar o programa (novo processo simulado
por novas instâncias de History/Downloader/DriveClient sobre o mesmo banco)."""

import os
import threading

import pytest

from conftest import ACCOUNT
from gdsd import history as H
from gdsd.downloader import Downloader, NullReporter
from gdsd.drive_client import DriveClient
from gdsd.errors import AnotherInstanceRunning
from gdsd.history import History
from gdsd.resume import InstanceLock, cancel_tasks, prepare_resume, prepare_retry, recover_after_restart

DATA = os.urandom(3 * 1024 * 1024)


class StopAfter(NullReporter):
    """Simula Ctrl+C depois de N bytes."""

    def __init__(self, stop_event, limit):
        self.stop_event, self.limit, self.seen = stop_event, limit, 0

    def file_progress(self, task_id, nbytes):
        self.seen += nbytes
        if self.seen >= self.limit:
            self.stop_event.set()


def new_process(settings, drive, policy, reporter=None, stop_event=None):
    history = History(settings.db_path)
    client = DriveClient(drive.session, policy)
    dl = Downloader(client, history, settings, ACCOUNT, stop_event=stop_event, reporter=reporter)
    return history, client, dl


def test_interrupcao_e_retomada_apos_reiniciar(drive, settings, policy, dest):
    drive.add_file("big", "grande.bin", DATA)

    # --- processo 1: começa e é interrompido no meio
    stop = threading.Event()
    h1, c1, d1 = new_process(settings, drive, policy, StopAfter(stop, 1_200_000), stop)
    tid, _ = d1.plan(c1.get_file("big"), dest / "grande.bin", dest_root=dest)
    out = d1.run(tid)
    assert out.status == H.INTERRUPTED
    task = h1.get_task(tid)
    partial = task["temp_path"]
    assert os.path.exists(partial) and task["temp_path"].endswith(".gdpart")
    saved = os.path.getsize(partial)
    assert 0 < saved < len(DATA)
    assert not (dest / "grande.bin").exists()
    h1.close()

    # --- processo 2: identifica a tarefa e retoma com Range a partir do parcial
    h2, c2, d2 = new_process(settings, drive, policy)
    ids = prepare_resume(h2, ACCOUNT)
    assert ids == [tid]
    drive.requests.clear()
    result = d2.run_tasks(ids)
    assert result.count(H.COMPLETED) == 1
    assert (dest / "grande.bin").read_bytes() == DATA
    media = [r for r in drive.requests if r["kind"] == "media"]
    assert media[0]["headers"]["Range"] == f"bytes={saved}-"
    assert h2.get_task(tid)["attempts"] == 2
    h2.close()


def test_tarefa_em_andamento_de_processo_morto_vira_interrompida(drive, settings, policy, dest):
    h1, c1, d1 = new_process(settings, drive, policy)
    drive.add_file("f", "f.bin", b"abc")
    tid, _ = d1.plan(c1.get_file("f"), dest / "f.bin", dest_root=dest)
    h1.update_task(tid, status=H.RUNNING)  # processo "morreu" aqui
    h1.close()

    h2 = History(settings.db_path)
    assert recover_after_restart(h2) == 1
    assert h2.get_task(tid)["status"] == H.INTERRUPTED
    assert prepare_resume(h2, ACCOUNT) == [tid]
    h2.close()


def test_retomada_reinicia_se_remoto_mudou(drive, settings, policy, dest):
    drive.add_file("big", "grande.bin", DATA)
    stop = threading.Event()
    h1, c1, d1 = new_process(settings, drive, policy, StopAfter(stop, 1_000_000), stop)
    tid, _ = d1.plan(c1.get_file("big"), dest / "grande.bin", dest_root=dest)
    d1.run(tid)
    h1.close()

    novo = os.urandom(len(DATA))
    drive.set_content("big", novo)  # nova revisão no Drive

    h2, c2, d2 = new_process(settings, drive, policy)
    drive.requests.clear()
    d2.run_tasks(prepare_resume(h2, ACCOUNT))
    assert (dest / "grande.bin").read_bytes() == novo
    media = [r for r in drive.requests if r["kind"] == "media"]
    assert "Range" not in media[0]["headers"]
    h2.close()


def test_parcial_maior_que_o_esperado_reinicia(drive, settings, policy, dest):
    drive.add_file("f", "f.bin", b"0123456789")
    h, c, d = new_process(settings, drive, policy)
    tid, _ = d.plan(c.get_file("f"), dest / "f.bin", dest_root=dest)
    task = h.get_task(tid)
    with open(task["temp_path"], "wb") as fh:
        fh.write(b"X" * 50)
    d.run(tid)
    assert (dest / "f.bin").read_bytes() == b"0123456789"
    h.close()


def test_parcial_corrompido_falha_verificacao_e_rebaixa(drive, settings, policy, dest):
    drive.add_file("f", "f.bin", DATA)
    h, c, d = new_process(settings, drive, policy)
    tid, _ = d.plan(c.get_file("f"), dest / "f.bin", dest_root=dest)
    task = h.get_task(tid)
    with open(task["temp_path"], "wb") as fh:
        fh.write(b"lixo" * 1000)  # parcial "plausível" mas com conteúdo errado
    out = d.run(tid)
    assert out.status == H.COMPLETED  # detectou pela verificação e baixou do zero
    assert (dest / "f.bin").read_bytes() == DATA
    h.close()


def test_folder_reexecutado_reaproveita_tarefa_interrompida(drive, settings, policy, dest):
    from gdsd.folder_downloader import FolderDownloader

    drive.add_folder("root", "P")
    drive.add_file("big", "grande.bin", DATA, "root")
    stop = threading.Event()
    h1, c1, d1 = new_process(settings, drive, policy, StopAfter(stop, 1_000_000), stop)
    plan = FolderDownloader(c1, d1, {}).plan(c1.get_file("root"), dest, None)
    d1.run_tasks(plan.task_ids)
    h1.close()

    h2, c2, d2 = new_process(settings, drive, policy)
    plan2 = FolderDownloader(c2, d2, {}).plan(c2.get_file("root"), dest, None)
    assert plan2.task_ids == plan.task_ids  # mesma tarefa, não duplica
    drive.requests.clear()
    d2.run_tasks(plan2.task_ids)
    assert (dest / "P" / "grande.bin").read_bytes() == DATA
    assert [r for r in drive.requests if r["kind"] == "media"][0]["headers"].get("Range")
    h2.close()


def test_retry_de_tarefas_com_erro(drive, settings, policy, dest):
    drive.add_file("f", "f.bin", b"abc")
    drive.statuses["media:f"] = [(403, "downloadQuotaExceeded")]
    h, c, d = new_process(settings, drive, policy)
    tid, _ = d.plan(c.get_file("f"), dest / "f.bin", dest_root=dest)
    assert d.run(tid).status == H.ERROR
    ids, _ = prepare_retry(h, ACCOUNT, include_all_errors=True)
    assert ids == [tid] and h.get_task(tid)["status"] == H.PENDING
    assert d.run_tasks(ids).count(H.COMPLETED) == 1
    h.close()


def test_retry_recusa_tarefa_concluida(drive, settings, policy, dest):
    drive.add_file("f", "f.bin", b"abc")
    h, c, d = new_process(settings, drive, policy)
    tid, _ = d.plan(c.get_file("f"), dest / "f.bin", dest_root=dest)
    d.run(tid)
    ids, msgs = prepare_retry(h, ACCOUNT, [tid])
    assert ids == [] and "concluída" in msgs[0]
    h.close()


def test_cancelar_remove_parcial(drive, settings, policy, dest):
    drive.add_file("big", "grande.bin", DATA)
    stop = threading.Event()
    h, c, d = new_process(settings, drive, policy, StopAfter(stop, 500_000), stop)
    tid, _ = d.plan(c.get_file("big"), dest / "grande.bin", dest_root=dest)
    d.run(tid)
    partial = h.get_task(tid)["temp_path"]
    assert os.path.exists(partial)
    msgs = cancel_tasks(h, ACCOUNT, [tid], delete_partial=True)
    assert "cancelada" in msgs[0] and not os.path.exists(partial)
    assert h.get_task(tid)["status"] == H.CANCELLED
    assert prepare_resume(h, ACCOUNT) == []
    h.close()


def test_interrupcao_em_lote_marca_todas_como_interrompidas(drive, settings, policy, dest):
    stop = threading.Event()
    h, c, d = new_process(settings, drive, policy, StopAfter(stop, 300_000), stop)
    ids = []
    for i in range(3):
        drive.add_file(f"f{i}", f"{i}.bin", DATA)
        ids.append(d.plan(c.get_file(f"f{i}"), dest / f"{i}.bin", dest_root=dest)[0])
    result = d.run_tasks(ids, max_workers=1)
    assert result.interrupted
    assert all(h.get_task(t)["status"] == H.INTERRUPTED for t in ids)
    assert sorted(prepare_resume(h, ACCOUNT)) == sorted(ids)
    h.close()


def test_trava_de_instancia_unica(settings):
    a = InstanceLock(settings.lock_path)
    a.acquire()
    b = InstanceLock(settings.lock_path)
    with pytest.raises(AnotherInstanceRunning):
        b.acquire()
    a.release()
    b.acquire()
    b.release()
