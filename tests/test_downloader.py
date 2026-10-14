import hashlib
import os
from unittest import mock

import pytest

from conftest import ACCOUNT
from fakes import DOC, DOCX, FORM, PDF
from gdsd import history as H
from gdsd import integrity
from gdsd.downloader import Downloader, same_remote_content
from gdsd.errors import ExportNotSupported
from gdsd.workspace_export import resolve_format

DATA = os.urandom(3 * 1024 * 1024 + 123)  # vários blocos de 256 KB e checkpoints de 1 MB


def plan_and_run(downloader, client, fid, dest, name=None, export=None, on_conflict=None):
    meta = client.get_file(fid)
    local = dest / (name or meta["name"])
    task_id, early = downloader.plan(meta, local, dest_root=dest, export=export, on_conflict=on_conflict)
    if early:
        return early
    return downloader.run(task_id)


def test_download_binario_verificado_md5(drive, client, downloader, dest, history):
    drive.add_file("bin", "dados.bin", DATA)
    out = plan_and_run(downloader, client, "bin", dest)
    assert out.status == H.COMPLETED
    assert (dest / "dados.bin").read_bytes() == DATA
    assert out.integrity == integrity.VERIFIED_MD5
    task = history.get_task(out.task_id)
    assert task["sha256"] == hashlib.sha256(DATA).hexdigest()
    assert task["google_md5"] == hashlib.md5(DATA).hexdigest()
    assert task["expected_size"] == len(DATA) == task["downloaded_bytes"]
    assert task["temp_path"] is None
    assert not [p for p in dest.iterdir() if p.suffix in (".gdpart", ".gdinvalid")]


def test_download_verificado_sha256_quando_disponivel(drive, client, downloader, dest):
    drive.add_file("bin", "dados.bin", DATA, sha256=True)
    out = plan_and_run(downloader, client, "bin", dest)
    assert out.integrity == integrity.VERIFIED_SHA256


def test_sem_checksum_registra_apenas_tamanho(drive, client, downloader, dest):
    drive.add_file("bin", "dados.bin", b"abc", md5=False)
    out = plan_and_run(downloader, client, "bin", dest)
    assert out.status == H.COMPLETED and out.integrity == integrity.SIZE_ONLY


def test_arquivo_vazio(drive, client, downloader, dest):
    drive.add_file("z", "vazio.txt", b"")
    out = plan_and_run(downloader, client, "z", dest)
    assert out.status == H.COMPLETED and (dest / "vazio.txt").read_bytes() == b""


def test_checksum_divergente_nao_e_declarado_integro(drive, client, downloader, dest, history):
    drive.add_file("bin", "dados.bin", DATA)
    drive.corrupt.add("bin")
    out = plan_and_run(downloader, client, "bin", dest)
    assert out.status == H.ERROR and "integridade" in out.message
    assert not (dest / "dados.bin").exists()  # nunca vira arquivo definitivo
    invalid = [p for p in dest.iterdir() if p.name.endswith(".gdinvalid")]
    assert len(invalid) == 1
    assert history.get_task(out.task_id)["integrity_status"] == integrity.FAILED


def test_tamanho_divergente_falha(drive, client, downloader, dest):
    drive.add_file("bin", "dados.bin", DATA)
    real = drive.contents["bin"]
    drive.items["bin"]["size"] = str(len(real) + 10)
    drive.items["bin"].pop("md5Checksum")
    out = plan_and_run(downloader, client, "bin", dest)
    assert out.status == H.ERROR


def test_queda_de_rede_retoma_com_range(drive, client, downloader, dest):
    drive.add_file("bin", "dados.bin", DATA)
    # duas quedas no meio do stream (posições relativas ao início de cada resposta)
    drive.media_fail["bin"] = [1_000_000, 1_000_000]
    out = plan_and_run(downloader, client, "bin", dest)
    assert out.status == H.COMPLETED
    assert (dest / "dados.bin").read_bytes() == DATA
    ranges = [r["headers"].get("Range") for r in drive.requests if r["kind"] == "media"]
    assert ranges[0] is None
    assert ranges[1].startswith("bytes=") and int(ranges[1][6:-1]) > 0
    assert len(ranges) == 3


def test_resposta_truncada_e_detectada_e_retomada(drive, client, downloader, dest):
    drive.add_file("bin", "dados.bin", DATA)
    drive.media_truncate["bin"] = [500_000]
    out = plan_and_run(downloader, client, "bin", dest)
    assert out.status == H.COMPLETED and (dest / "dados.bin").read_bytes() == DATA


def test_servidor_ignora_range_reinicia_do_zero(drive, client, downloader, dest):
    drive.add_file("bin", "dados.bin", DATA)
    drive.media_fail["bin"] = [1_000_000]
    drive.ignore_range = True
    out = plan_and_run(downloader, client, "bin", dest)
    assert out.status == H.COMPLETED and (dest / "dados.bin").read_bytes() == DATA


def test_arquivo_remoto_muda_durante_retomada(drive, client, downloader, dest):
    drive.add_file("bin", "dados.bin", DATA)
    novo = os.urandom(len(DATA))
    drive.media_fail["bin"] = [1_000_000]
    original_get = client.get_file
    calls = {"n": 0}

    def get_file(fid):
        calls["n"] += 1
        if calls["n"] == 3:  # após a falha, antes de retomar
            drive.set_content("bin", novo)
        return original_get(fid)

    with mock.patch.object(client, "get_file", side_effect=get_file):
        out = plan_and_run(downloader, client, "bin", dest)
    assert out.status == H.COMPLETED
    assert (dest / "dados.bin").read_bytes() == novo  # nunca mistura versões
    media = [r for r in drive.requests if r["kind"] == "media"]
    assert media[-1]["headers"].get("Range") is None


def test_erros_temporarios_esgotados(drive, client, downloader, dest, history):
    drive.add_file("bin", "dados.bin", DATA)
    drive.media_fail["bin"] = [100] * 10
    out = plan_and_run(downloader, client, "bin", dest)
    assert out.status == H.ERROR
    task = history.get_task(out.task_id)
    assert task["status"] == H.ERROR and task["downloaded_bytes"] > 0  # parcial mantido para retry
    assert os.path.exists(task["temp_path"])


def test_429_no_download_e_repetido(drive, client, downloader, dest):
    drive.add_file("bin", "dados.bin", b"conteudo")
    drive.statuses["media:bin"] = [429, 503]
    out = plan_and_run(downloader, client, "bin", dest)
    assert out.status == H.COMPLETED


def test_download_desativado_pelo_proprietario(drive, client, downloader, dest):
    drive.add_file("bin", "dados.bin", b"x", capabilities={"canDownload": False})
    out = plan_and_run(downloader, client, "bin", dest)
    assert out.status == H.ERROR and "desativado" in out.message
    assert drive.count("media") == 0  # nenhuma tentativa de contornar


def test_arquivo_sinalizado_como_abusivo(drive, client, downloader, dest):
    drive.add_file("bin", "virus.exe", b"x", _forbidden="cannotDownloadAbusiveFile")
    out = plan_and_run(downloader, client, "bin", dest)
    assert out.status == H.ERROR and "nocivo" in out.message


# ---------------------------------------------------------------- conflitos
def test_conflito_rename(drive, client, downloader, dest):
    (dest / "dados.bin").write_bytes(b"meu arquivo local")
    drive.add_file("bin", "dados.bin", b"remoto")
    out = plan_and_run(downloader, client, "bin", dest, on_conflict="rename")
    assert out.status == H.COMPLETED
    assert (dest / "dados.bin").read_bytes() == b"meu arquivo local"
    assert (dest / "dados (2).bin").read_bytes() == b"remoto"


def test_conflito_skip(drive, client, downloader, dest):
    (dest / "dados.bin").write_bytes(b"local")
    drive.add_file("bin", "dados.bin", b"remoto")
    out = plan_and_run(downloader, client, "bin", dest, on_conflict="skip")
    assert out.status == H.SKIPPED and (dest / "dados.bin").read_bytes() == b"local"
    assert drive.count("media") == 0


def test_conflito_overwrite_explicito(drive, client, downloader, dest):
    (dest / "dados.bin").write_bytes(b"local")
    drive.add_file("bin", "dados.bin", b"remoto")
    out = plan_and_run(downloader, client, "bin", dest, on_conflict="overwrite")
    assert out.status == H.COMPLETED and (dest / "dados.bin").read_bytes() == b"remoto"


def test_arquivo_criado_durante_download_nao_e_sobrescrito(drive, client, downloader, dest):
    drive.add_file("bin", "dados.bin", b"remoto")
    meta = client.get_file("bin")
    tid, _ = downloader.plan(meta, dest / "dados.bin", dest_root=dest)
    (dest / "dados.bin").write_bytes(b"criado por outro programa")
    out = downloader.run(tid)
    assert out.status == H.COMPLETED
    assert (dest / "dados.bin").read_bytes() == b"criado por outro programa"
    assert (dest / "dados (2).bin").read_bytes() == b"remoto"


def test_reexecucao_ignora_arquivo_inalterado(drive, client, downloader, dest):
    drive.add_file("bin", "dados.bin", b"remoto")
    assert plan_and_run(downloader, client, "bin", dest).status == H.COMPLETED
    again = plan_and_run(downloader, client, "bin", dest)
    assert again.status == H.SKIPPED and "inalterado" in again.message
    assert drive.count("media") == 1


# ---------------------------------------------------------------- exportação
def test_exporta_google_docs(drive, client, downloader, dest, history):
    drive.add_native("doc", "Ata", DOC, exports={DOCX: b"PK-docx", PDF: b"%PDF"})
    fmt = resolve_format(DOC, None, {"document": "docx"})
    out = plan_and_run(downloader, client, "doc", dest, name="Ata.docx", export=fmt)
    assert out.status == H.COMPLETED
    assert (dest / "Ata.docx").read_bytes() == b"PK-docx"
    assert out.integrity == integrity.UNVERIFIABLE
    task = history.get_task(out.task_id)
    assert task["export_format"] == "docx" and task["sha256"] == hashlib.sha256(b"PK-docx").hexdigest()
    assert "convertido" in task["integrity_detail"]


def test_exporta_pdf(drive, client, downloader, dest):
    drive.add_native("doc", "Ata", DOC, exports={DOCX: b"PK", PDF: b"%PDF-1.7"})
    fmt = resolve_format(DOC, "pdf", {})
    out = plan_and_run(downloader, client, "doc", dest, name="Ata.pdf", export=fmt)
    assert (dest / "Ata.pdf").read_bytes() == b"%PDF-1.7" and out.status == H.COMPLETED


def test_exportacao_grande_usa_export_links(drive, client, downloader, dest):
    drive.add_native("doc", "Grande", DOC, exports={DOCX: b"PK-grande"})
    drive.export_too_large.add("doc")
    out = plan_and_run(downloader, client, "doc", dest, name="Grande.docx",
                       export=resolve_format(DOC, "docx", {}))
    assert out.status == H.COMPLETED and (dest / "Grande.docx").read_bytes() == b"PK-grande"
    assert drive.count("exportlink") == 1


def test_exportacao_interrompida_reinicia_sem_range(drive, client, downloader, dest):
    drive.add_native("doc", "Ata", DOC, exports={DOCX: b"A" * 600_000})
    drive.export_fail["doc"] = [300_000]
    out = plan_and_run(downloader, client, "doc", dest, name="Ata.docx", export=resolve_format(DOC, "docx", {}))
    assert out.status == H.COMPLETED and (dest / "Ata.docx").read_bytes() == b"A" * 600_000
    assert all("Range" not in r["headers"] for r in drive.requests if r["kind"] == "export")
    assert drive.count("export") == 2


def test_formato_invalido_e_tipos_nao_exportaveis():
    with pytest.raises(ExportNotSupported, match="Opções"):
        resolve_format(DOC, "xlsx", {})
    with pytest.raises(ExportNotSupported, match="Formulários"):
        resolve_format(FORM, None, {})


def test_nativo_sem_formato_e_recusado_no_plano(drive, client, downloader, dest):
    drive.add_native("doc", "Ata", DOC, exports={DOCX: b"x"})
    with pytest.raises(ExportNotSupported):
        downloader.plan(client.get_file("doc"), dest / "Ata", dest_root=dest)


# ---------------------------------------------------------------- outros
def test_espaco_insuficiente(drive, client, downloader, dest, settings):
    drive.add_file("bin", "dados.bin", DATA)
    fake_usage = mock.Mock(free=10)
    with mock.patch("gdsd.downloader.shutil.disk_usage", return_value=fake_usage):
        out = plan_and_run(downloader, client, "bin", dest)
    assert out.status == H.ERROR and "Espaço insuficiente" in out.message
    assert drive.count("media") == 0


def test_same_remote_content_exige_referencia_forte():
    rec = {"expected_size": 10, "remote_modified_time": "t", "remote_revision": None,
           "google_md5": None, "google_sha256": None}
    meta = {"size": "10", "modifiedTime": "t"}
    assert not same_remote_content(rec, meta)  # só tamanho/data não bastam
    rec2 = {**rec, "remote_revision": "r1", "google_md5": "m"}
    assert same_remote_content(rec2, {**meta, "headRevisionId": "r1", "md5Checksum": "m"})
    assert not same_remote_content(rec2, {**meta, "headRevisionId": "r2", "md5Checksum": "m"})


def test_run_tasks_paralelo(drive, client, history, settings, dest):
    dl = Downloader(client, history, settings, ACCOUNT)
    ids = []
    for i in range(6):
        drive.add_file(f"f{i}", f"arquivo{i}.bin", os.urandom(50_000))
        tid, _ = dl.plan(client.get_file(f"f{i}"), dest / f"arquivo{i}.bin", dest_root=dest)
        ids.append(tid)
    result = dl.run_tasks(ids, max_workers=3)
    assert result.count(H.COMPLETED) == 6 and not result.interrupted
    for i in range(6):
        assert (dest / f"arquivo{i}.bin").read_bytes() == drive.contents[f"f{i}"]
