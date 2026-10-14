import sqlite3

from gdsd import history as H
from gdsd.history import History


def _task(h, **kw):
    base = dict(account_email="a@x.com", file_id="f1", file_name="relatório_2024.pdf", local_path="C:/d/r.pdf")
    base.update(kw)
    return h.create_task(**base)


def test_persistencia_apos_reabrir(settings):
    h = History(settings.db_path)
    job = h.create_job("a@x.com", "folder", "fold", "Pasta", "C:/d")
    tid = _task(h, job_id=job, parent_id="fold", expected_size=10, google_md5="abc", export_format=None)
    h.update_task(tid, status=H.COMPLETED, downloaded_bytes=10, sha256="ff", integrity_status="verified_md5")
    h.finish_job(job, "ok")
    h.close()

    h2 = History(settings.db_path)
    t = h2.get_task(tid)
    assert t["status"] == H.COMPLETED and t["sha256"] == "ff" and t["parent_id"] == "fold"
    assert t["created_at"] and t["updated_at"]
    assert h2.counts("a@x.com") == {H.COMPLETED: 1}
    h2.close()


def test_filtros(settings):
    with History(settings.db_path) as h:
        a = _task(h, status=H.ERROR, error="x")
        _task(h, file_id="f2", file_name="foto.jpg", status=H.COMPLETED)
        _task(h, file_id="f3", file_name="outra conta", account_email="b@x.com", status=H.ERROR)
        assert [t["id"] for t in h.list_tasks(statuses=[H.ERROR], account_email="a@x.com")] == [a]
        assert [t["file_name"] for t in h.list_tasks(name_like="foto")] == ["foto.jpg"]
        assert len(h.list_tasks(name_like="_")) == 1  # '_' é literal, não curinga
        assert len(h.list_tasks(limit=2)) == 2


def test_sem_colunas_de_credenciais(settings):
    History(settings.db_path).close()
    conn = sqlite3.connect(settings.db_path)
    cols = {r[1].lower() for table in ("tasks", "jobs") for r in conn.execute(f"PRAGMA table_info({table})")}
    conn.close()
    assert not any(word in c for c in cols for word in ("token", "password", "senha", "secret"))


def test_versao_do_schema(settings):
    History(settings.db_path).close()
    conn = sqlite3.connect(settings.db_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == H.SCHEMA_VERSION
    conn.close()


def test_mark_stale_running(settings):
    with History(settings.db_path) as h:
        t = _task(h, status=H.RUNNING)
        assert h.mark_stale_running() == 1
        assert h.get_task(t)["status"] == H.INTERRUPTED
