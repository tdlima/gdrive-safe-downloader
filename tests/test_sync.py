"""Sincronização: depois de baixar uma pasta, baixar somente o que foi
acrescentado (ou alterado) no Drive, no mesmo lugar da estrutura local."""

import os
import sqlite3

import pytest

from conftest import ACCOUNT
from fakes import DOC, DOCX
from gdsd import cli
from gdsd import history as H
from gdsd.downloader import Downloader
from gdsd.drive_client import DriveClient
from gdsd.folder_downloader import FolderDownloader, FolderOptions
from gdsd.history import History
from gdsd.retry import RetryPolicy

WORKSHOP = "1WoRkShOpAbCdEfGhIjKlMnOpQr"


def build_workshop(drive):
    """workshop/dia1..dia3, cada uma com aula1..aula3."""
    drive.add_folder(WORKSHOP, "workshop")
    for d in (1, 2, 3):
        drive.add_folder(f"dia{d}", f"dia{d}", WORKSHOP)
        for a in (1, 2, 3):
            drive.add_file(f"d{d}a{a}", f"aula{a}.mp4", f"video dia{d} aula{a}".encode(), f"dia{d}")


def plan_and_run(client, history, settings, dest, options=None):
    dl = Downloader(client, history, settings, ACCOUNT)
    fd = FolderDownloader(client, dl, settings.export_formats)
    plan = fd.plan(client.get_file(WORKSHOP), dest, None, options or FolderOptions())
    result = dl.run_tasks(plan.task_ids)
    return plan, plan.early + result.outcomes


def media_ids(drive):
    return [r["id"] for r in drive.requests if r["kind"] == "media"]


# ------------------------------------------------------------- núcleo
def test_exemplo_workshop_baixa_somente_aula4(drive, client, history, settings, dest):
    build_workshop(drive)
    plan, _ = plan_and_run(client, history, settings, dest)
    assert len(plan.new_files) == 9
    assert len(list((dest / "workshop").rglob("*.mp4"))) == 9

    # Futuramente, a aula4 é acrescentada na pasta dia2 do Drive
    drive.add_file("d2a4", "aula4.mp4", b"video dia2 aula4", "dia2")
    drive.requests.clear()
    plan, outcomes = plan_and_run(client, history, settings, dest)

    assert plan.new_files == ["workshop/dia2/aula4.mp4"]
    assert plan.unchanged == 9 and plan.updated_files == []
    assert media_ids(drive) == ["d2a4"]                     # só esse arquivo foi baixado
    assert (dest / "workshop" / "dia2" / "aula4.mp4").read_bytes() == b"video dia2 aula4"
    assert len(list((dest / "workshop").rglob("*.mp4"))) == 10   # nenhuma duplicata
    done = [o for o in outcomes if o.status == H.COMPLETED]
    assert [o.name for o in done] == ["aula4.mp4"]


def test_subpasta_nova_tambem_e_criada(drive, client, history, settings, dest):
    build_workshop(drive)
    plan_and_run(client, history, settings, dest)
    drive.add_folder("dia4", "dia4", WORKSHOP)
    drive.add_file("d4a1", "aula1.mp4", b"novo dia", "dia4")
    plan, _ = plan_and_run(client, history, settings, dest)
    assert plan.new_files == ["workshop/dia4/aula1.mp4"]
    assert (dest / "workshop" / "dia4" / "aula1.mp4").read_bytes() == b"novo dia"


def test_sincronizacao_sem_novidades_nao_cria_linhas_no_historico(drive, client, history, settings, dest):
    build_workshop(drive)
    plan_and_run(client, history, settings, dest)
    before = len(history.list_tasks())
    for _ in range(3):
        plan, outcomes = plan_and_run(client, history, settings, dest)
        assert plan.task_ids == [] and all(o.unchanged for o in outcomes)
    assert len(history.list_tasks()) == before


def test_dry_run_mostra_apenas_novidades(drive, client, history, settings, dest):
    build_workshop(drive)
    plan_and_run(client, history, settings, dest)
    drive.add_file("d2a4", "aula4.mp4", b"x", "dia2")
    plan, _ = plan_and_run(client, history, settings, dest, FolderOptions(dry_run=True))
    assert plan.dry_items == ["[NOVO] workshop/dia2/aula4.mp4"]
    assert not (dest / "workshop" / "dia2" / "aula4.mp4").exists()


def test_arquivo_apagado_localmente_e_baixado_de_novo(drive, client, history, settings, dest):
    build_workshop(drive)
    plan_and_run(client, history, settings, dest)
    os.remove(dest / "workshop" / "dia1" / "aula2.mp4")
    plan, _ = plan_and_run(client, history, settings, dest)
    assert plan.new_files == ["workshop/dia1/aula2.mp4"]
    assert (dest / "workshop" / "dia1" / "aula2.mp4").exists()


def test_documento_google_editado_e_exportado_novamente(drive, client, history, settings, dest):
    drive.add_folder(WORKSHOP, "workshop")
    drive.add_native("doc", "Apostila", DOC, WORKSHOP, exports={DOCX: b"v1"})
    plan_and_run(client, history, settings, dest)
    drive.exports[("doc", DOCX)] = b"v2"
    drive.items["doc"]["modifiedTime"] = "2024-09-09T00:00:00Z"
    plan, _ = plan_and_run(client, history, settings, dest, FolderOptions(on_update="replace"))
    assert plan.updated_files == ["workshop/Apostila.docx"]
    assert (dest / "workshop" / "Apostila.docx").read_bytes() == b"v2"


# ------------------------------------------------- arquivos alterados no Drive
def _change_aula1(drive):
    drive.set_content("d1a1", b"aula1 versao corrigida")


def test_alterado_keep_both_padrao(drive, client, history, settings, dest):
    build_workshop(drive)
    plan_and_run(client, history, settings, dest)
    _change_aula1(drive)
    plan, _ = plan_and_run(client, history, settings, dest)
    assert plan.updated_files == ["workshop/dia1/aula1.mp4"]
    d1 = dest / "workshop" / "dia1"
    assert (d1 / "aula1.mp4").read_bytes() == b"video dia1 aula1"          # original preservado
    assert (d1 / "aula1 (2).mp4").read_bytes() == b"aula1 versao corrigida"
    plan, _ = plan_and_run(client, history, settings, dest)                # não repete
    assert plan.task_ids == [] and plan.updated_files == []


def test_alterado_replace_substitui_copia_intacta(drive, client, history, settings, dest):
    build_workshop(drive)
    plan_and_run(client, history, settings, dest)
    _change_aula1(drive)
    plan_and_run(client, history, settings, dest, FolderOptions(on_update="replace"))
    d1 = dest / "workshop" / "dia1"
    assert (d1 / "aula1.mp4").read_bytes() == b"aula1 versao corrigida"
    assert not (d1 / "aula1 (2).mp4").exists()


def test_alterado_replace_preserva_copia_editada_no_computador(drive, client, history, settings, dest):
    build_workshop(drive)
    plan_and_run(client, history, settings, dest)
    local = dest / "workshop" / "dia1" / "aula1.mp4"
    local.write_bytes(b"minhas anotacoes!")  # mesmo tamanho? não importa: o SHA-256 difere
    _change_aula1(drive)
    plan_and_run(client, history, settings, dest, FolderOptions(on_update="replace"))
    assert local.read_bytes() == b"minhas anotacoes!"
    assert (local.parent / "aula1 (2).mp4").read_bytes() == b"aula1 versao corrigida"


def test_alterado_skip_mantem_versao_local(drive, client, history, settings, dest):
    build_workshop(drive)
    plan_and_run(client, history, settings, dest)
    _change_aula1(drive)
    drive.requests.clear()
    _, outcomes = plan_and_run(client, history, settings, dest, FolderOptions(on_update="skip"))
    assert media_ids(drive) == []
    assert any(o.status == H.SKIPPED and "alterado no Drive" in o.message for o in outcomes)


# ------------------------------------------------------------- CLI
@pytest.fixture
def cli_drive(monkeypatch, drive):
    monkeypatch.setattr(cli.AuthManager, "stored_account", lambda self: ACCOUNT)
    c = DriveClient(drive.session, RetryPolicy(max_retries=2, sleep=lambda s, e: None))
    monkeypatch.setattr(cli.App, "client", property(lambda self: c))
    return drive


def test_cli_sync_de_ponta_a_ponta(cli_drive, capsys, tmp_path):
    build_workshop(cli_drive)
    dest = tmp_path / "Cursos"
    assert cli.main(["--no-input", "folder", WORKSHOP, "--dest", str(dest)]) == 0
    capsys.readouterr()

    assert cli.main(["--no-input", "sync"]) == 0
    assert "Nenhuma novidade" in capsys.readouterr().out

    cli_drive.add_file("d2a4", "aula4.mp4", b"video dia2 aula4", "dia2")
    assert cli.main(["--no-input", "sync", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "[NOVO] workshop/dia2/aula4.mp4" in out
    assert not (dest / "workshop" / "dia2" / "aula4.mp4").exists()

    cli_drive.requests.clear()
    assert cli.main(["--no-input", "sync"]) == 0     # sem link e sem --dest: lembra de tudo
    out = capsys.readouterr().out
    assert "1 novo(s)" in out and "Já atualizados: 9" in out
    assert (dest / "workshop" / "dia2" / "aula4.mp4").read_bytes() == b"video dia2 aula4"
    assert media_ids(cli_drive) == ["d2a4"]

    assert cli.main(["sync", "--list"]) == 0
    assert "workshop" in capsys.readouterr().out


def test_cli_pasta_renomeada_no_drive_continua_na_mesma_pasta_local(cli_drive, capsys, tmp_path):
    build_workshop(cli_drive)
    dest = tmp_path / "Cursos"
    cli.main(["--no-input", "folder", WORKSHOP, "--dest", str(dest)])
    cli_drive.items[WORKSHOP]["name"] = "Workshop 2026 (final)"
    cli_drive.add_file("d3a4", "aula4.mp4", b"z", "dia3")
    assert cli.main(["--no-input", "sync", WORKSHOP]) == 0
    assert (dest / "workshop" / "dia3" / "aula4.mp4").exists()
    assert not (dest / "Workshop 2026 (final)").exists()


def test_cli_sync_lembra_formato_de_exportacao(cli_drive, capsys, tmp_path):
    cli_drive.add_folder(WORKSHOP, "workshop")
    cli_drive.add_native("doc1", "Apostila 1", DOC, WORKSHOP, exports={"application/pdf": b"%PDF1"})
    dest = tmp_path / "Cursos"
    cli.main(["--no-input", "folder", WORKSHOP, "--dest", str(dest), "--doc-format", "pdf"])
    cli_drive.add_native("doc2", "Apostila 2", DOC, WORKSHOP, exports={"application/pdf": b"%PDF2"})
    assert cli.main(["--no-input", "sync"]) == 0
    assert (dest / "workshop" / "Apostila 2.pdf").read_bytes() == b"%PDF2"


def test_cli_sync_remove_registro_sem_apagar_arquivos(cli_drive, capsys, tmp_path):
    build_workshop(cli_drive)
    dest = tmp_path / "Cursos"
    cli.main(["--no-input", "folder", WORKSHOP, "--dest", str(dest)])
    assert cli.main(["sync", "--remove", "1"]) == 0
    assert (dest / "workshop" / "dia1" / "aula1.mp4").exists()
    capsys.readouterr()
    assert cli.main(["--no-input", "sync"]) == 0
    assert "Nenhuma pasta registrada" in capsys.readouterr().out


def test_cli_pasta_inacessivel_nao_impede_as_outras(cli_drive, capsys, tmp_path):
    build_workshop(cli_drive)
    cli_drive.add_folder("1OuTrApAsTaAbCdEfGhIjKlMn", "outra")
    cli_drive.add_file("o1", "x.txt", b"x", "1OuTrApAsTaAbCdEfGhIjKlMn")
    dest = tmp_path / "Cursos"
    cli.main(["--no-input", "folder", "1OuTrApAsTaAbCdEfGhIjKlMn", "--dest", str(dest)])
    cli.main(["--no-input", "folder", WORKSHOP, "--dest", str(dest)])
    cli_drive.items["1OuTrApAsTaAbCdEfGhIjKlMn"]["_no_access"] = True  # acesso removido
    cli_drive.add_file("d1a4", "aula4.mp4", b"n", "dia1")
    capsys.readouterr()
    assert cli.main(["--no-input", "sync"]) == 1
    assert "Erro ao sincronizar 'outra'" in capsys.readouterr().err
    assert (dest / "workshop" / "dia1" / "aula4.mp4").exists()


# ---------------------------------------------------------- migração
def test_banco_da_versao_1_registra_pastas_ja_baixadas(settings, tmp_path):
    conn = sqlite3.connect(settings.db_path)
    conn.executescript(H._SCHEMA)
    conn.execute("INSERT INTO jobs(created_at, account_email, kind, source_id, source_name, dest_root)"
                 " VALUES ('2026-01-01', ?, 'folder', ?, 'workshop', ?)", (ACCOUNT, WORKSHOP, str(tmp_path)))
    conn.execute("INSERT INTO tasks(job_id, created_at, updated_at, account_email, file_id, file_name,"
                 " local_path, status) VALUES (1, 'x', 'x', ?, 'f', 'aula1.mp4', ?, 'completed')",
                 (ACCOUNT, str(tmp_path / "workshop" / "dia1" / "aula1.mp4")))
    conn.execute("PRAGMA user_version=1")
    conn.commit()
    conn.close()

    with History(settings.db_path) as h:
        records = h.list_synced_folders(ACCOUNT)
    assert len(records) == 1
    assert records[0]["folder_id"] == WORKSHOP and records[0]["local_root"] == "workshop"


# ------------------------------------------------- pasta movida no computador
def _download_and_move(cli_drive, tmp_path, new_name="workshop"):
    import shutil

    build_workshop(cli_drive)
    old_dest = tmp_path / "Cursos"
    assert cli.main(["--no-input", "folder", WORKSHOP, "--dest", str(old_dest)]) == 0
    new_parent = tmp_path / "HD externo" / "Backup"
    new_parent.mkdir(parents=True)
    shutil.move(str(old_dest / "workshop"), str(new_parent / new_name))
    return new_parent / new_name


def test_sync_com_pasta_movida_avisa_e_nao_rebaixa(cli_drive, capsys, tmp_path):
    _download_and_move(cli_drive, tmp_path)
    capsys.readouterr()
    cli_drive.requests.clear()
    assert cli.main(["--no-input", "sync"]) == 1
    err = capsys.readouterr().err
    assert "pasta local não foi encontrada" in err and "relocate 1" in err
    assert media_ids(cli_drive) == []                       # nada foi baixado de novo
    assert not (tmp_path / "Cursos" / "workshop").exists()   # nem recriado no local antigo


def test_relocate_e_sync_baixa_somente_o_novo_no_novo_local(cli_drive, capsys, tmp_path):
    new_root = _download_and_move(cli_drive, tmp_path)
    assert cli.main(["--no-input", "relocate", "1", str(new_root)]) == 0
    out = capsys.readouterr().out
    assert "encontrados no novo local: 9 de 9" in out

    cli_drive.add_file("d2a4", "aula4.mp4", b"video dia2 aula4", "dia2")
    cli_drive.requests.clear()
    assert cli.main(["--no-input", "sync"]) == 0
    assert media_ids(cli_drive) == ["d2a4"]
    assert (new_root / "dia2" / "aula4.mp4").read_bytes() == b"video dia2 aula4"
    assert not (tmp_path / "Cursos" / "workshop").exists()
    assert cli.main(["--no-input", "verify", "--all"]) == 0  # caminhos do histórico atualizados


def test_relocate_aceita_pasta_renomeada_e_link(cli_drive, capsys, tmp_path):
    new_root = _download_and_move(cli_drive, tmp_path, new_name="Workshop 2026")
    link = f"https://drive.google.com/drive/folders/{WORKSHOP}"
    assert cli.main(["--no-input", "relocate", link, str(new_root)]) == 0
    cli_drive.add_file("d1a4", "aula4.mp4", b"x", "dia1")
    cli_drive.requests.clear()
    assert cli.main(["--no-input", "sync"]) == 0
    assert media_ids(cli_drive) == ["d1a4"]
    assert (new_root / "dia1" / "aula4.mp4").exists()


def test_relocate_recusa_caminho_errado(cli_drive, capsys, tmp_path):
    _download_and_move(cli_drive, tmp_path)
    wrong = tmp_path / "HD externo"          # pasta pai, não a própria pasta
    assert cli.main(["--no-input", "relocate", "1", str(wrong)]) == 1
    assert "Nenhum dos arquivos" in capsys.readouterr().err
    assert cli.main(["--no-input", "relocate", "1", str(tmp_path / "nao-existe")]) == 1


def test_relocate_registro_inexistente(cli_drive, capsys, tmp_path):
    assert cli.main(["--no-input", "relocate", "7", str(tmp_path)]) == 1
    assert "não encontrado" in capsys.readouterr().err
