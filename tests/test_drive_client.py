import pytest

from fakes import FOLDER
from gdsd.drive_client import DriveClient, escape_query_value
from gdsd.errors import AuthError, NotFoundOrNoAccess, PermissionDenied, TransientError
from gdsd.retry import RetryPolicy


def _setup_search(drive):
    drive.add_folder("f1", "Relatórios 2024")
    drive.add_file("a1", "Relatórios anuais.pdf", b"x", "f1")
    drive.add_file("s1", "Relatórios do parceiro.xlsx", b"y", ownedByMe=False,
                   owners=[{"emailAddress": "parceiro@example.com"}])
    drive.shared_drives = [{"id": "d1", "name": "Financeiro"}]
    drive.add_file("sd1", "Relatórios do setor.docx", b"z", "d1", driveId="d1", ownedByMe=False)
    drive.add_file("outro", "Fotos.zip", b"w")


def test_pesquisa_em_todas_as_origens(drive, client):
    _setup_search(drive)
    items, warnings = client.search("relatórios")
    ids = [i["id"] for i in items]
    assert set(ids) == {"f1", "a1", "s1", "sd1"}
    assert ids[0] == "f1"  # pastas primeiro
    # o drive compartilhado é pesquisado individualmente (corpora=drive)
    drive_calls = [r for r in drive.requests if r["kind"] == "list" and r["params"].get("corpora") == "drive"]
    assert drive_calls and drive_calls[0]["params"]["driveId"] == "d1"
    for r in drive.requests:
        if r["kind"] == "list":
            assert r["params"]["supportsAllDrives"] == "true"
            assert r["params"]["includeItemsFromAllDrives"] == "true"
    assert warnings == []


def test_pesquisa_por_escopo_e_tipo(drive, client):
    _setup_search(drive)
    items, _ = client.search("relatórios", scope="shared")
    assert [i["id"] for i in items] == ["s1"]
    items, _ = client.search("relatórios", scope="mydrive", kind="file")
    assert [i["id"] for i in items] == ["a1"]
    items, _ = client.search("relatórios", kind="folder")
    assert [i["id"] for i in items] == ["f1"]
    items, _ = client.search("relatórios", scope="drives")
    assert [i["id"] for i in items] == ["sd1"]


def test_pesquisa_limite(drive, client):
    for i in range(10):
        drive.add_file(f"x{i}", f"dados {i}.bin", b"1")
    items, warnings = client.search("dados", limit=4)
    assert len(items) == 4
    assert any("primeiros 4" in w for w in warnings)


def test_escape_aspas_na_consulta(drive, client):
    drive.add_file("q1", "Relatório d'Ávila.pdf", b"1")
    items, _ = client.search("d'Ávila")
    assert [i["id"] for i in items] == ["q1"]
    assert escape_query_value("a'b\\c") == "a\\'b\\\\c"


def test_origem(drive, client):
    _setup_search(drive)
    assert client.describe_origin(client.get_file("a1")) == "Meu Drive"
    assert client.describe_origin(client.get_file("s1")) == "Compartilhado por parceiro@example.com"
    assert client.describe_origin(client.get_file("sd1")) == "Drive compartilhado: Financeiro"


def test_get_file_404_sem_acesso(drive, client):
    with pytest.raises(NotFoundOrNoAccess):
        client.get_file("inexistente")
    drive.add_file("priv", "privado.txt", b"x", _no_access=True)
    with pytest.raises(NotFoundOrNoAccess):
        client.get_file("priv")


def test_get_file_inclui_supports_all_drives(drive, client):
    drive.add_file("a", "a.txt", b"x")
    client.get_file("a")
    assert drive.requests[-1]["params"]["supportsAllDrives"] == "true"


def test_403_permissao(drive, client):
    drive.add_file("a", "a.txt", b"x")
    drive.statuses["get:a"] = [(403, "insufficientFilePermissions")]
    with pytest.raises(PermissionDenied, match="permissão"):
        client.get_file("a")


def test_401_vira_erro_de_autenticacao(drive, client):
    drive.statuses["about"] = [401]
    with pytest.raises(AuthError):
        client.get_about()


def test_429_e_5xx_sao_repetidos(drive, client):
    drive.add_file("a", "a.txt", b"x")
    drive.statuses["get:a"] = [429, (403, "userRateLimitExceeded"), 500]  # max_retries=3
    assert client.get_file("a")["id"] == "a"
    assert drive.count("get", "a") == 4


def test_erro_temporario_persistente(drive):
    drive.add_file("a", "a.txt", b"x")
    drive.statuses["get:a"] = [503] * 10
    c = DriveClient(drive.session, RetryPolicy(max_retries=2, sleep=lambda s, e: None))
    with pytest.raises(TransientError):
        c.get_file("a")
    assert drive.count("get", "a") == 3


def test_listagem_paginada_de_pasta(drive, client):
    drive.add_folder("p", "Pasta")
    for i in range(25):
        drive.add_file(f"f{i}", f"{i}.txt", b"1", "p")
    drive.max_page_size = 10  # a API pode devolver páginas menores que pageSize
    children = list(client.list_children(client.get_file("p")))
    assert len(children) == 25
    assert drive.count("list") == 3


def test_listagem_drive_compartilhado_usa_corpora_drive(drive, client):
    drive.shared_drives = [{"id": "d1", "name": "Equipe"}]
    drive.add_folder("pf", "Projetos", "d1", driveId="d1")
    drive.add_file("x", "x.txt", b"1", "pf", driveId="d1")
    children = list(client.list_children(client.get_file("pf")))
    assert [c["id"] for c in children] == ["x"]
    call = [r for r in drive.requests if r["kind"] == "list"][-1]
    assert call["params"]["corpora"] == "drive" and call["params"]["driveId"] == "d1"


def test_pasta_compartilhada_por_terceiros(drive, client):
    drive.add_folder("ext", "Pasta do cliente", ownedByMe=False)
    drive.add_file("e1", "contrato.pdf", b"1", "ext", ownedByMe=False)
    drive.add_file("e2", "segredo.pdf", b"1", "ext", ownedByMe=False, _no_access=True)
    children = list(client.list_children(client.get_file("ext")))
    assert [c["id"] for c in children] == ["e1"]  # itens sem acesso não aparecem


def test_resource_key_enviada(drive, client):
    drive.add_file("rk", "antigo.pdf", b"1")
    drive.required_resource_keys["rk"] = "0-KEY"
    with pytest.raises(NotFoundOrNoAccess):
        client.get_file("rk")
    client.register_resource_key("rk", "0-KEY")
    assert client.get_file("rk")["id"] == "rk"


def test_export_link_de_dominio_estranho_recusado(client):
    from gdsd.errors import DriveApiError

    with pytest.raises(DriveApiError):
        client.open_url("https://evil-google.com.attacker.net/x")
    with pytest.raises(DriveApiError):
        client.open_url("http://docs.google.com/x")


def test_folder_mime_constante():
    assert FOLDER == "application/vnd.google-apps.folder"
