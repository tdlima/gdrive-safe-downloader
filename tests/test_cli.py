"""Testes da linha de comando com o Drive simulado (sem rede, sem conta real)."""

import os

import pytest

from fakes import DOC, DOCX, PDF
from gdsd import cli
from gdsd.drive_client import DriveClient
from gdsd.retry import RetryPolicy
from gdsd.ui import parse_selection

FID = "1AbCdEfGhIjKlMnOpQrStUvWxYz0"
FOLDER_ID = "1FoLdErAbCdEfGhIjKlMnOpQrSt"


@pytest.fixture
def fake_app(monkeypatch, drive, tmp_path):
    """Substitui autenticação e cliente pelo Drive simulado."""
    monkeypatch.setattr(cli.AuthManager, "stored_account", lambda self: "usuario@example.com")
    client = DriveClient(drive.session, RetryPolicy(max_retries=2, sleep=lambda s, e: None))
    monkeypatch.setattr(cli.App, "client", property(lambda self: client))
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    return drive


def test_help(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for cmd in ("login", "account", "search", "download", "folder", "resume", "retry", "history",
                "status", "logout", "cancel", "verify", "formats", "config"):
        assert cmd in out


def test_comando_desconhecido():
    with pytest.raises(SystemExit) as exc:
        cli.main(["voar"])
    assert exc.value.code == 2


def test_formats(capsys):
    assert cli.main(["formats"]) == 0
    assert "docx*" in capsys.readouterr().out


def test_link_invalido(fake_app, capsys, tmp_path):
    assert cli.main(["--no-input", "download", "https://exemplo.com/x", "--dest", str(tmp_path)]) == 2
    assert "não é do Google Drive" in capsys.readouterr().err


def test_sem_permissao(fake_app, capsys, tmp_path):
    fake_app.add_file(FID, "privado.pdf", b"x", _no_access=True)
    code = cli.main(["--no-input", "download", f"https://drive.google.com/file/d/{FID}/view",
                     "--dest", str(tmp_path)])
    assert code == 1
    assert "não tem acesso" in capsys.readouterr().err


def test_download_e_history(fake_app, capsys, tmp_path):
    fake_app.add_file(FID, "relatório.pdf", b"%PDF-conteudo")
    dest = tmp_path / "saida"
    code = cli.main(["--no-input", "download", f"https://drive.google.com/file/d/{FID}/view",
                     "--dest", str(dest)])
    assert code == 0
    assert (dest / "relatório.pdf").read_bytes() == b"%PDF-conteudo"
    capsys.readouterr()
    assert cli.main(["history"]) == 0
    out = capsys.readouterr().out
    assert "relatório.pdf" in out and "md5 ok" in out
    assert cli.main(["history", "--id", "1"]) == 0
    assert "MD5 do Google" in capsys.readouterr().out
    assert cli.main(["verify", "--all"]) == 0


def test_download_de_pasta_sugere_folder(fake_app, capsys, tmp_path):
    fake_app.add_folder(FOLDER_ID, "Pasta")
    assert cli.main(["--no-input", "download", FOLDER_ID, "--dest", str(tmp_path)]) == 1
    assert "use: python main.py folder" in capsys.readouterr().err.lower()


def test_download_google_doc_com_format(fake_app, capsys, tmp_path):
    fake_app.add_native(FID, "Ata", DOC, exports={DOCX: b"PK", PDF: b"%PDF"})
    code = cli.main(["--no-input", "download", FID, "--format", "pdf", "--dest", str(tmp_path)])
    assert code == 0 and (tmp_path / "Ata.pdf").read_bytes() == b"%PDF"
    assert "CONVERTIDO" in capsys.readouterr().out


def test_folder_e_status(fake_app, capsys, tmp_path):
    fake_app.add_folder(FOLDER_ID, "Projeto")
    fake_app.add_file("a1", "a.txt", b"a", FOLDER_ID)
    fake_app.add_file("b1", "b.txt", b"b", FOLDER_ID, _forbidden="cannotDownloadFile")
    code = cli.main(["--no-input", "folder", f"https://drive.google.com/drive/folders/{FOLDER_ID}",
                     "--dest", str(tmp_path)])
    assert code == 1  # houve um erro
    out = capsys.readouterr().out
    assert "Concluídos:    1" in out and "Com erro:      1" in out
    assert (tmp_path / "Projeto" / "a.txt").exists()
    assert cli.main(["status"]) == 0
    assert "com erro" in capsys.readouterr().out


def test_folder_dry_run(fake_app, capsys, tmp_path):
    fake_app.add_folder(FOLDER_ID, "Projeto")
    fake_app.add_file("a1", "a.txt", b"a", FOLDER_ID)
    assert cli.main(["--no-input", "folder", FOLDER_ID, "--dry-run", "--dest", str(tmp_path)]) == 0
    assert "Simulação" in capsys.readouterr().out
    assert not (tmp_path / "Projeto").exists()


def test_search_lista_resultados(fake_app, capsys):
    fake_app.add_file("x1", "Relatórios 2024.pdf", b"1")
    assert cli.main(["--no-input", "search", "relatórios"]) == 0
    out = capsys.readouterr().out
    assert "Relatórios 2024.pdf" in out and "Meu Drive" in out


def test_retry_e_resume_sem_tarefas(fake_app, capsys):
    assert cli.main(["--no-input", "retry"]) == 0
    assert cli.main(["--no-input", "resume", "--yes"]) == 0
    assert "Nenhum" in capsys.readouterr().out


def test_parse_selection():
    assert parse_selection("1,3-5", 5) == [1, 3, 4, 5]
    assert parse_selection("2, 2", 3) == [2]
    with pytest.raises(ValueError):
        parse_selection("9", 3)
    with pytest.raises(ValueError):
        parse_selection("a", 3)


def test_config_init(capsys):
    assert cli.main(["config", "--init"]) == 0
    path = os.path.join(os.environ["GDSD_HOME"], "config.toml")
    assert os.path.exists(path)
    assert cli.main(["config"]) == 0
