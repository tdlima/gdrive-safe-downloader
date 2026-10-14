import json
from types import SimpleNamespace

import pytest
from google.auth.exceptions import RefreshError

from gdsd.auth import SCOPES, AuthManager
from gdsd.errors import AuthError


class MemoryStore:
    def __init__(self, data=None):
        self.data = data

    def load(self):
        return dict(self.data) if self.data else None

    def save(self, data):
        self.data = dict(data)

    def delete(self):
        self.data = None


CLIENT = {"installed": {"client_id": "cid.apps.googleusercontent.com", "client_secret": "GOCSPX-teste",
                        "token_uri": "https://oauth2.googleapis.com/token",
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "redirect_uris": ["http://localhost"]}}


@pytest.fixture
def client_secret(settings):
    path = settings.home / "client_secret.json"
    path.write_text(json.dumps(CLIENT), encoding="utf-8")
    return path


class FakeFlow:
    def __init__(self, creds):
        self.creds = creds
        self.kwargs = None

    def run_local_server(self, **kwargs):
        self.kwargs = kwargs
        return self.creds


def fake_creds(refresh_token="1//refresh-novo", scopes=SCOPES):
    return SimpleNamespace(refresh_token=refresh_token, token="ya29.acesso", granted_scopes=scopes,
                           scopes=scopes, valid=True)


def make_auth(settings, store, flow=None, about=None, revoked=None):
    revoked = revoked if revoked is not None else []
    return AuthManager(
        settings, store=store,
        flow_factory=lambda cfg, scopes: flow,
        about_fetcher=about or (lambda creds: {"email": "usuario@example.com", "name": "Usuário"}),
        revoke=lambda token: revoked.append(token) or True,
    )


def test_login_salva_apenas_refresh_token(settings, client_secret):
    store = MemoryStore()
    flow = FakeFlow(fake_creds())
    auth = make_auth(settings, store, flow)
    account, new = auth.login()
    assert new and account.email == "usuario@example.com"
    assert store.data["refresh_token"] == "1//refresh-novo"
    assert store.data["account_email"] == "usuario@example.com"
    assert "ya29.acesso" not in json.dumps(store.data)  # access token não é persistido
    assert flow.kwargs["access_type"] == "offline"
    assert "select_account" in flow.kwargs["prompt"]
    assert flow.kwargs["port"] == 0


def test_login_sem_escopo_drive_falha(settings, client_secret):
    flow = FakeFlow(fake_creds(scopes=["openid"]))
    with pytest.raises(AuthError, match="não foi concedida"):
        make_auth(settings, MemoryStore(), flow).login()


def test_login_sem_refresh_token_falha(settings, client_secret):
    flow = FakeFlow(fake_creds(refresh_token=None))
    with pytest.raises(AuthError, match="refresh token"):
        make_auth(settings, MemoryStore(), flow).login()


def test_client_secret_tipo_web_rejeitado(settings):
    (settings.home / "client_secret.json").write_text(json.dumps({"web": {}}), encoding="utf-8")
    with pytest.raises(AuthError, match="App para computador"):
        make_auth(settings, MemoryStore(), FakeFlow(fake_creds())).login()


def test_client_secret_ausente(settings):
    with pytest.raises(AuthError, match="client_secret.json não encontrado"):
        make_auth(settings, MemoryStore(), FakeFlow(fake_creds())).login()


def test_renovacao_de_token(settings, client_secret, monkeypatch):
    calls = []

    def fake_refresh(self, request):
        calls.append(self.refresh_token)
        self.token = "ya29.novo"

    monkeypatch.setattr("google.oauth2.credentials.Credentials.refresh", fake_refresh)
    store = MemoryStore({"refresh_token": "1//salvo", "account_email": "usuario@example.com",
                         "client_id": CLIENT["installed"]["client_id"], "scopes": SCOPES})
    creds = make_auth(settings, store).credentials()
    assert calls == ["1//salvo"]
    assert creds.token == "ya29.novo"
    assert creds.client_id == CLIENT["installed"]["client_id"]


def test_refresh_token_rotacionado_e_salvo(settings, client_secret, monkeypatch):
    def fake_refresh(self, request):
        self.token = "ya29.x"
        self._refresh_token = "1//rotacionado"

    monkeypatch.setattr("google.oauth2.credentials.Credentials.refresh", fake_refresh)
    store = MemoryStore({"refresh_token": "1//antigo", "account_email": "a@b.c", "scopes": SCOPES})
    make_auth(settings, store).credentials()
    assert store.data["refresh_token"] == "1//rotacionado"


def test_token_revogado_pede_novo_login(settings, client_secret, monkeypatch):
    def fail(self, request):
        raise RefreshError("invalid_grant")

    monkeypatch.setattr("google.oauth2.credentials.Credentials.refresh", fail)
    store = MemoryStore({"refresh_token": "1//revogado", "account_email": "a@b.c", "scopes": SCOPES})
    with pytest.raises(AuthError, match="login"):
        make_auth(settings, store).credentials()


def test_sem_conta(settings, client_secret):
    with pytest.raises(AuthError, match="Nenhuma conta"):
        make_auth(settings, MemoryStore()).credentials()


def test_client_id_diferente_exige_novo_login(settings, client_secret):
    store = MemoryStore({"refresh_token": "1//x", "client_id": "outro", "scopes": SCOPES})
    with pytest.raises(AuthError, match="outro projeto"):
        make_auth(settings, store).credentials()


def test_logout_revoga(settings):
    revoked = []
    store = MemoryStore({"refresh_token": "1//x", "account_email": "a@b.c"})
    msg = make_auth(settings, store, revoked=revoked).logout()
    assert revoked == ["1//x"] and store.data is None and "revogada" in msg


def test_logout_local(settings):
    revoked = []
    store = MemoryStore({"refresh_token": "1//x", "account_email": "a@b.c"})
    make_auth(settings, store, revoked=revoked).logout(revoke=False)
    assert revoked == [] and store.data is None


def test_troca_de_conta_revoga_anterior(settings, client_secret):
    revoked = []
    store = MemoryStore({"refresh_token": "1//antigo", "account_email": "antigo@example.com"})
    auth = make_auth(settings, store, FakeFlow(fake_creds()), revoked=revoked)
    account, new = auth.login(switch=True)
    assert new and account.email == "usuario@example.com"
    assert revoked == ["1//antigo"]
    assert store.data["refresh_token"] == "1//refresh-novo"


def test_login_existente_valido_nao_abre_navegador(settings, client_secret, monkeypatch):
    monkeypatch.setattr("google.oauth2.credentials.Credentials.refresh",
                        lambda self, r: setattr(self, "token", "ya29.ok"))
    flow = FakeFlow(fake_creds())
    store = MemoryStore({"refresh_token": "1//ok", "account_email": "usuario@example.com", "scopes": SCOPES})
    account, new = make_auth(settings, store, flow).login()
    assert not new and flow.kwargs is None
