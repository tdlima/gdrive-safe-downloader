"""Suporte a Linux: armazenamento do token sem cofre de senhas, login sem
navegador, pastas de dados XDG e gravação em sistemas de arquivos sem hard links.

Os testes marcados com ``posix_only`` só rodam em Linux/macOS (no GitHub Actions,
o job ubuntu-latest executa todos eles)."""

import errno
import json
import os
import stat
import sys
from types import SimpleNamespace

import pytest

from gdsd import config, paths
from gdsd.auth import SCOPES, AutoTokenStore, AuthManager, FileTokenStore
from gdsd.errors import AuthError

posix_only = pytest.mark.skipif(os.name != "posix", reason="comportamento específico de Linux/macOS")


class MemoryStore:
    def __init__(self, data=None):
        self.data = data

    def load(self):
        return dict(self.data) if self.data else None

    def save(self, data):
        self.data = dict(data)

    def delete(self):
        self.data = None


# --------------------------------------------------------- token em arquivo
def test_file_token_store_ida_e_volta(tmp_path):
    store = FileTokenStore(tmp_path / "dados" / "token.json")
    assert store.load() is None
    store.save({"refresh_token": "1//abc", "account_email": "a@b.c"})
    assert store.load()["account_email"] == "a@b.c"
    assert not list((tmp_path / "dados").glob("*.tmp"))
    store.delete()
    assert store.load() is None
    store.delete()  # idempotente


@posix_only
def test_file_token_store_permissoes_restritas(tmp_path):
    store = FileTokenStore(tmp_path / "dados" / "token.json")
    store.save({"refresh_token": "1//abc"})
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(store.path.parent).st_mode) == 0o700
    os.chmod(store.path, 0o644)            # alguém afrouxou a permissão
    store.load()
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600


def test_auto_sem_cofre_usa_arquivo(tmp_path):
    file_store = FileTokenStore(tmp_path / "token.json")
    store = AutoTokenStore("auto", MemoryStore(), file_store, available=lambda: False)
    store.save({"refresh_token": "1//x"})
    assert store.is_fallback and "permissão 600" in store.location()
    assert json.loads((tmp_path / "token.json").read_text())["refresh_token"] == "1//x"
    assert store.load()["refresh_token"] == "1//x"


def test_auto_com_cofre_nao_deixa_copia_em_arquivo(tmp_path):
    file_store = FileTokenStore(tmp_path / "token.json")
    file_store.save({"refresh_token": "1//antigo"})       # login anterior sem cofre
    keyring_store = MemoryStore()
    store = AutoTokenStore("auto", keyring_store, file_store, available=lambda: True)
    assert store.load()["refresh_token"] == "1//antigo"    # ainda encontra o antigo
    store.save({"refresh_token": "1//novo"})
    assert keyring_store.data["refresh_token"] == "1//novo"
    assert not (tmp_path / "token.json").exists()
    assert not store.is_fallback


def test_modos_explicitos(tmp_path):
    kr = MemoryStore()
    forced_file = AutoTokenStore("file", kr, FileTokenStore(tmp_path / "t.json"), available=lambda: True)
    forced_file.save({"refresh_token": "1//f"})
    assert kr.data is None and (tmp_path / "t.json").exists()
    forced_kr = AutoTokenStore("keyring", kr, FileTokenStore(tmp_path / "u.json"), available=lambda: False)
    forced_kr.save({"refresh_token": "1//k"})
    assert kr.data["refresh_token"] == "1//k" and not (tmp_path / "u.json").exists()


def test_logout_apaga_os_dois_locais(tmp_path):
    kr = MemoryStore({"refresh_token": "1//k"})
    fs = FileTokenStore(tmp_path / "t.json")
    fs.save({"refresh_token": "1//f"})
    AutoTokenStore("auto", kr, fs, available=lambda: True).delete()
    assert kr.data is None and not (tmp_path / "t.json").exists()


def test_token_store_invalido_na_configuracao(tmp_path, monkeypatch):
    monkeypatch.setenv("GDSD_TOKEN_STORE", "papel")
    with pytest.raises(Exception, match="token_store"):
        config.load_settings(tmp_path)


# ------------------------------------------------- login sem navegador (SSH)
class ManualFlow:
    def __init__(self):
        self.redirect_uri = None
        self.fetched = None
        self.credentials = SimpleNamespace(refresh_token="1//manual", granted_scopes=SCOPES, scopes=SCOPES)

    def authorization_url(self, **kwargs):
        self.kwargs = kwargs
        return "https://accounts.google.com/o/oauth2/auth?x=1", "estado123"

    def fetch_token(self, code):
        self.fetched = code


@pytest.fixture
def secret(settings):
    data = {"installed": {"client_id": "cid", "client_secret": "s", "token_uri": "https://oauth2.googleapis.com/token"}}
    (settings.home / "client_secret.json").write_text(json.dumps(data), encoding="utf-8")


def manual_auth(settings, flow, pasted, said):
    return AuthManager(settings, store=MemoryStore(), flow_factory=lambda c, s: flow,
                       about_fetcher=lambda creds: {"email": "a@b.c"}, revoke=lambda t: True,
                       ask=lambda prompt: pasted, say=said.append)


def test_login_manual_copiar_e_colar(settings, secret):
    flow, said = ManualFlow(), []
    pasted = "http://localhost:8765/?state=estado123&code=4/0AbcDEF&scope=drive.readonly"
    account, new = manual_auth(settings, flow, pasted, said).login(manual=True)
    assert new and account.email == "a@b.c"
    assert flow.redirect_uri == "http://localhost:8765/"
    assert flow.fetched == "4/0AbcDEF"
    assert flow.kwargs["access_type"] == "offline"
    assert "accounts.google.com" in said[0]


def test_login_manual_porta_personalizada(settings, secret):
    flow = ManualFlow()
    manual_auth(settings, flow, "http://localhost:9000/?state=estado123&code=x", []).login(manual=True, port=9000)
    assert flow.redirect_uri == "http://localhost:9000/"


@pytest.mark.parametrize("pasted,msg", [
    ("http://localhost:8765/?state=outro&code=x", "state"),
    ("http://localhost:8765/?error=access_denied&state=estado123", "recusou"),
    ("http://localhost:8765/?state=estado123", "código"),
    ("", "código"),
])
def test_login_manual_rejeita_endereco_invalido(settings, secret, pasted, msg):
    with pytest.raises(AuthError, match=msg):
        manual_auth(settings, ManualFlow(), pasted, []).login(manual=True)


# ---------------------------------------------------------- pastas e arquivos
def test_pasta_de_dados_linux_segue_xdg(monkeypatch, tmp_path):
    monkeypatch.delenv("GDSD_HOME", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert config.app_home() == tmp_path / "xdg" / "gdrive-safe-downloader"
    monkeypatch.delenv("XDG_DATA_HOME")
    assert config.app_home().parts[-3:] == (".local", "share", "gdrive-safe-downloader")


@posix_only
def test_mover_sem_hard_link_pendrive_fat(monkeypatch, tmp_path):
    def no_link(src, dst):
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(paths.os, "link", no_link)
    src, dst = tmp_path / "a.gdpart", tmp_path / "a.bin"
    src.write_bytes(b"dados")
    paths.move_no_overwrite(src, dst)
    assert dst.read_bytes() == b"dados" and not src.exists()

    src.write_bytes(b"novo")
    with pytest.raises(FileExistsError):
        paths.move_no_overwrite(src, dst)
    assert dst.read_bytes() == b"dados"


def test_nomes_continuam_compativeis_com_windows_no_linux():
    # Mesmo no Linux, os nomes são sanitizados para o padrão do Windows: a pasta
    # baixada pode ser copiada para um pendrive ou um PC com Windows sem erros.
    assert paths.sanitize_component('aula: parte 1?.mp4') == "aula_ parte 1_.mp4"
    assert paths.sanitize_component("CON") == "_CON"
