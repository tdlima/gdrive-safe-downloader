import os

from conftest import ACCOUNT
from fakes import DOC, DOCX, FORM, SHEET, XLSX
from gdsd import history as H
from gdsd.downloader import Downloader
from gdsd.folder_downloader import FolderDownloader, FolderOptions

EXPORTS = {"document": "docx", "spreadsheet": "xlsx", "presentation": "pptx", "drawing": "pdf"}


def run_folder(drive, client, downloader, dest, fid="root", options=None):
    fd = FolderDownloader(client, downloader, EXPORTS)
    plan = fd.plan(client.get_file(fid), dest, None, options or FolderOptions())
    result = downloader.run_tasks(plan.task_ids)
    return plan, plan.early + result.outcomes


def build_tree(drive):
    drive.add_folder("root", "Projeto")
    drive.add_file("r1", "leia-me.txt", b"raiz", "root")
    drive.add_folder("sub", "Documentos", "root")
    drive.add_file("s1", "contrato.pdf", b"pdf", "sub")
    drive.add_folder("deep", "Anexos", "sub")
    drive.add_file("d1", "foto.jpg", os.urandom(70_000), "deep")
    drive.add_native("doc", "Ata", DOC, "sub", exports={DOCX: b"PK-ata"})
    drive.add_native("sheet", "Custos", SHEET, "root", exports={XLSX: b"PK-custos"})


def test_estrutura_recursiva_reproduzida(drive, client, downloader, dest):
    build_tree(drive)
    plan, outcomes = run_folder(drive, client, downloader, dest)
    base = dest / "Projeto"
    assert (base / "leia-me.txt").read_bytes() == b"raiz"
    assert (base / "Documentos" / "contrato.pdf").read_bytes() == b"pdf"
    assert (base / "Documentos" / "Anexos" / "foto.jpg").read_bytes() == drive.contents["d1"]
    assert (base / "Documentos" / "Ata.docx").read_bytes() == b"PK-ata"
    assert (base / "Custos.xlsx").read_bytes() == b"PK-custos"
    assert plan.folders == 3 and plan.files == 5
    assert all(o.status == H.COMPLETED for o in outcomes)


def test_dry_run_nao_baixa(drive, client, downloader, dest):
    build_tree(drive)
    plan, outcomes = run_folder(drive, client, downloader, dest, options=FolderOptions(dry_run=True))
    assert plan.files == 5 and not plan.task_ids and not outcomes
    assert any("Ata.docx" in line for line in plan.dry_items)
    assert drive.count("media") == 0 and not (dest / "Projeto").exists()


def test_formato_de_exportacao_por_tipo(drive, client, downloader, dest):
    drive.add_folder("root", "P")
    drive.add_native("doc", "Ata", DOC, "root", exports={DOCX: b"x", "application/pdf": b"%PDF"})
    run_folder(drive, client, downloader, dest, options=FolderOptions(export_overrides={"document": "pdf"}))
    assert (dest / "P" / "Ata.pdf").read_bytes() == b"%PDF"


def test_nomes_duplicados_deterministicos(drive, client, downloader, dest):
    drive.add_folder("root", "P")
    drive.add_file("b", "relatório.pdf", b"segundo", "root", created="2024-05-02T00:00:00Z")
    drive.add_file("a", "relatório.pdf", b"primeiro", "root", created="2024-05-01T00:00:00Z")
    drive.add_file("c", "RELATÓRIO.pdf", b"terceiro", "root", created="2024-05-03T00:00:00Z")
    run_folder(drive, client, downloader, dest)
    base = dest / "P"
    names = sorted(p.name for p in base.iterdir())
    assert len(names) == 3
    assert (base / "relatório.pdf").read_bytes() == b"primeiro"  # o mais antigo mantém o nome
    contents = sorted((base / n).read_bytes() for n in names)
    assert contents == sorted([b"primeiro", b"segundo", b"terceiro"])


def test_reexecucao_nao_duplica_nem_rebaixa(drive, client, history, settings, dest):
    drive.add_folder("root", "P")
    drive.add_file("a", "x.txt", b"1", "root")
    drive.add_file("b", "x.txt", b"2", "root", created="2025-01-01T00:00:00Z")
    run_folder(drive, client, Downloader(client, history, settings, ACCOUNT), dest)
    _, outcomes = run_folder(drive, client, Downloader(client, history, settings, ACCOUNT), dest)
    assert {o.status for o in outcomes} == {H.SKIPPED}
    assert sorted(p.name for p in (dest / "P").iterdir()) == ["x (2).txt", "x.txt"]
    assert drive.count("media") == 2


def test_atalhos_para_arquivo_e_pasta(drive, client, downloader, dest):
    drive.add_folder("root", "P")
    drive.add_folder("outra", "Pasta Externa")
    drive.add_file("o1", "dentro.txt", b"externo", "outra")
    drive.add_file("alvo", "planilha.csv", b"a,b", None)
    drive.add_shortcut("sc1", "Atalho para planilha", "alvo", "root")
    drive.add_shortcut("sc2", "Atalho pasta", "outra", "root")
    _, outcomes = run_folder(drive, client, downloader, dest)
    assert (dest / "P" / "Atalho para planilha.csv").read_bytes() == b"a,b"
    assert (dest / "P" / "Atalho pasta" / "dentro.txt").read_bytes() == b"externo"


def test_atalho_circular_nao_entra_em_loop(drive, client, downloader, dest):
    drive.add_folder("root", "P")
    drive.add_folder("sub", "S", "root")
    drive.add_shortcut("loop", "volta", "root", "sub")
    drive.add_file("f", "a.txt", b"1", "sub")
    plan, outcomes = run_folder(drive, client, downloader, dest)
    assert (dest / "P" / "S" / "a.txt").exists()
    assert any(o.status == H.SKIPPED and "circular" in o.message for o in outcomes)


def test_atalho_sem_acesso_registra_erro_e_continua(drive, client, downloader, dest):
    drive.add_folder("root", "P")
    drive.add_file("secreto", "secreto.txt", b"x", _no_access=True)
    drive.add_shortcut("sc", "Atalho secreto", "secreto", "root")
    drive.add_file("ok", "ok.txt", b"ok", "root")
    _, outcomes = run_folder(drive, client, downloader, dest)
    statuses = {o.name: o.status for o in outcomes}
    assert statuses["Atalho secreto"] == H.ERROR
    assert statuses["ok.txt"] == H.COMPLETED


def test_erro_em_um_arquivo_nao_interrompe_os_demais(drive, client, downloader, dest):
    drive.add_folder("root", "P")
    drive.add_file("bom1", "a.txt", b"a", "root")
    drive.add_file("ruim", "b.txt", b"b", "root", _forbidden="cannotDownloadFile")
    drive.add_file("bom2", "c.txt", b"c", "root")
    drive.add_native("form", "Pesquisa", FORM, "root")
    _, outcomes = run_folder(drive, client, downloader, dest)
    by_name = {o.name: o.status for o in outcomes}
    assert by_name == {"a.txt": H.COMPLETED, "b.txt": H.ERROR, "c.txt": H.COMPLETED, "Pesquisa": H.SKIPPED}


def test_nomes_maliciosos_ficam_dentro_do_destino(drive, client, downloader, dest):
    drive.add_folder("root", "..")
    drive.add_folder("evil", "../../fora", "root")
    drive.add_file("e1", "..\\..\\system.ini", b"x", "evil")
    drive.add_file("e2", "CON", b"y", "root")
    drive.add_file("e3", "a:b|c?.txt", b"z", "root")
    run_folder(drive, client, downloader, dest)
    dest_real = os.path.realpath(dest)
    files = [os.path.join(dp, f) for dp, _, fs in os.walk(dest) for f in fs]
    assert len(files) == 3
    for f in files:
        assert os.path.realpath(f).startswith(dest_real + os.sep)
    assert not (dest.parent / "fora").exists()


def test_drive_compartilhado_raiz(drive, client, downloader, dest):
    drive.shared_drives = [{"id": "0Adrive", "name": "Financeiro"}]
    drive.add_folder("0Adrive", "Drive", driveId="0Adrive")
    drive.add_file("x", "balanço.xlsx", b"1", "0Adrive", driveId="0Adrive")
    run_folder(drive, client, downloader, dest, fid="0Adrive")
    assert (dest / "Financeiro" / "balanço.xlsx").read_bytes() == b"1"


def test_pasta_sem_permissao_de_listagem(drive, client, downloader, dest):
    drive.add_folder("root", "P")
    drive.add_folder("sub", "Bloqueada", "root")
    drive.add_file("ok", "ok.txt", b"1", "root")
    original = client.list_children

    def list_children(folder):
        if folder["id"] == "sub":
            from gdsd.errors import PermissionDenied
            raise PermissionDenied("sem acesso", 403, "insufficientFilePermissions")
        return original(folder)

    client.list_children = list_children
    _, outcomes = run_folder(drive, client, downloader, dest)
    assert any(o.status == H.ERROR and "listar" in o.message for o in outcomes)
    assert (dest / "P" / "ok.txt").exists()
