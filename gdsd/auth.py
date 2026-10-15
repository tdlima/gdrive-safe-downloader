"""Autenticação OAuth 2.0 (fluxo "App para computador" com loopback).

- A senha do Google nunca passa pelo programa: o login acontece no navegador.
- Escopo único: ``drive.readonly`` (leitura de arquivos e metadados). O e-mail da
  conta é obtido por ``about.get`` da própria Drive API, sem escopos extras.
- Apenas o *refresh token* é persistido, no cofre de senhas do sistema via
  ``keyring`` (Windows: Gerenciador de Credenciais; Linux: Secret Service/GNOME
  Keyring/KWallet; macOS: Keychain). Em Linux sem cofre, num arquivo com
  permissão 600 (ver :class:`AutoTokenStore`). O *access token* fica só em memória.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from urllib.parse import parse_qs, urlsplit
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import requests
from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials

from .config import KEYRING_SERVICE, Settings, find_client_secret
from .errors import AuthError

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
TOKEN_URI = "https://oauth2.googleapis.com/token"
REVOKE_URI = "https://oauth2.googleapis.com/revoke"
KEYRING_USERNAME = "oauth-token"

# Evita que o oauthlib aborte quando o Google devolve os escopos em outra ordem
# ou com escopos adicionais já concedidos; a verificação é feita manualmente.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")


class TokenStore(Protocol):
    def load(self) -> dict[str, Any] | None: ...
    def save(self, data: dict[str, Any]) -> None: ...
    def delete(self) -> None: ...


class KeyringTokenStore:
    """Armazena o token no cofre do sistema (Windows: Gerenciador de Credenciais,
    protegido por DPAPI e vinculado ao usuário do Windows)."""

    def __init__(self, service: str = KEYRING_SERVICE, username: str = KEYRING_USERNAME):
        self.service = service
        self.username = username

    def _keyring(self) -> Any:
        try:
            import keyring
            from keyring.errors import NoKeyringError  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise AuthError("O pacote 'keyring' não está instalado (pip install -r requirements.txt).") from exc
        return keyring

    def load(self) -> dict[str, Any] | None:
        kr = self._keyring()
        try:
            raw = kr.get_password(self.service, self.username)
        except Exception as exc:  # noqa: BLE001 - backend indisponível
            raise AuthError(f"Não foi possível acessar o cofre de credenciais do sistema: {type(exc).__name__}") from exc
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def save(self, data: dict[str, Any]) -> None:
        kr = self._keyring()
        try:
            kr.set_password(self.service, self.username, json.dumps(data, separators=(",", ":")))
        except Exception as exc:  # noqa: BLE001
            raise AuthError(f"Não foi possível gravar no cofre de credenciais do sistema: {type(exc).__name__}") from exc

    def delete(self) -> None:
        kr = self._keyring()
        try:
            kr.delete_password(self.service, self.username)
        except Exception:  # noqa: BLE001 - já inexistente
            pass


class FileTokenStore:
    """Token em arquivo JSON legível apenas pelo próprio usuário (permissão 600,
    pasta 700). Usado em Linux sem cofre de senhas (servidores, SSH, contêineres),
    como fazem o ``gh`` e o ``gcloud``. Proteja a conta do sistema operacional."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict[str, Any] | None:
        if not self.path.is_file():
            return None
        if os.name == "posix" and self.path.stat().st_mode & 0o077:
            os.chmod(self.path, 0o600)  # corrige permissões abertas demais
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            os.chmod(self.path.parent, 0o700)
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, separators=(",", ":"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)
        if os.name == "posix":
            os.chmod(self.path, 0o600)

    def delete(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def location(self) -> str:
        return f"arquivo {self.path} (permissão 600)"


def keyring_available() -> bool:
    """True se existir um cofre de senhas utilizável (Windows, macOS, Linux com
    GNOME Keyring/KWallet via Secret Service)."""
    try:
        import keyring
        from keyring.backends import fail

        backend = keyring.get_keyring()
        if isinstance(backend, fail.Keyring) or getattr(backend, "priority", 1) <= 0:
            return False
        backend.get_password(KEYRING_SERVICE, "__teste_de_disponibilidade__")
        return True
    except Exception:  # noqa: BLE001 - D-Bus ausente, cofre bloqueado etc.
        return False


class AutoTokenStore:
    """Escolhe onde guardar o token conforme ``token_store``:

    - ``keyring``: sempre o cofre do sistema (erro se não existir);
    - ``file``: sempre o arquivo protegido;
    - ``auto``: cofre do sistema se disponível; senão, o arquivo (com aviso).
    """

    def __init__(self, mode: str, keyring_store: TokenStore, file_store: FileTokenStore,
                 available: Callable[[], bool] = keyring_available):
        self.mode = mode
        self.keyring_store = keyring_store
        self.file_store = file_store
        self._available = available
        self._use_keyring: bool | None = None

    @property
    def uses_keyring(self) -> bool:
        if self._use_keyring is None:
            if self.mode == "file":
                self._use_keyring = False
            elif self.mode == "keyring":
                self._use_keyring = True
            else:
                self._use_keyring = self._available()
        return self._use_keyring

    @property
    def is_fallback(self) -> bool:
        return self.mode == "auto" and not self.uses_keyring

    def load(self) -> dict[str, Any] | None:
        if self.uses_keyring:
            data = self.keyring_store.load()
            if data or self.mode == "keyring":
                return data
        return self.file_store.load()

    def save(self, data: dict[str, Any]) -> None:
        if self.uses_keyring:
            self.keyring_store.save(data)
            self.file_store.delete()  # não deixa cópia em arquivo para trás
        else:
            self.file_store.save(data)

    def delete(self) -> None:
        if self.uses_keyring:
            self.keyring_store.delete()
        self.file_store.delete()

    def location(self) -> str:
        if self.uses_keyring:
            return "cofre de senhas do sistema (Gerenciador de Credenciais / Secret Service / Keychain)"
        return self.file_store.location()


def make_token_store(settings: Settings) -> AutoTokenStore:
    return AutoTokenStore(settings.token_store, KeyringTokenStore(), FileTokenStore(settings.token_file))


@dataclass(frozen=True)
class AccountInfo:
    email: str
    name: str = ""


FlowFactory = Callable[[dict[str, Any], list[str]], Any]
AboutFetcher = Callable[[Credentials], dict[str, str]]


def _default_flow_factory(config: dict[str, Any], scopes: list[str]) -> Any:
    from google_auth_oauthlib.flow import InstalledAppFlow

    return InstalledAppFlow.from_client_config(config, scopes)


def _default_about(creds: Credentials) -> dict[str, str]:
    from .drive_client import DriveClient

    return DriveClient(lambda: AuthorizedSession(creds)).get_about()


def _default_revoke(token: str) -> bool:
    try:
        resp = requests.post(REVOKE_URI, data={"token": token},
                             headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=20)
        return resp.status_code == 200
    except requests.RequestException:
        return False


def load_client_config(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuthError(f"Não foi possível ler {path}: {exc}") from exc
    if "installed" not in data:
        if "web" in data:
            raise AuthError(
                "O client_secret.json é do tipo 'Aplicativo da Web'. Crie um ID do cliente OAuth "
                "do tipo 'App para computador' (Desktop app) no Google Cloud Console."
            )
        raise AuthError("Arquivo client_secret.json inválido (chave 'installed' ausente).")
    for key in ("client_id", "client_secret"):
        if not data["installed"].get(key):
            raise AuthError(f"client_secret.json sem o campo '{key}'.")
    return data


class AuthManager:
    def __init__(
        self,
        settings: Settings,
        store: TokenStore | None = None,
        flow_factory: FlowFactory | None = None,
        about_fetcher: AboutFetcher | None = None,
        revoke: Callable[[str], bool] | None = None,
        request_factory: Callable[[], Any] = Request,
        ask: Callable[[str], str] = input,
        say: Callable[[str], None] = print,
    ):
        self.settings = settings
        self.store = store or make_token_store(settings)
        self.ask = ask
        self.say = say
        self.flow_factory = flow_factory or _default_flow_factory
        self.about_fetcher = about_fetcher or _default_about
        self.revoke = revoke or _default_revoke
        self.request_factory = request_factory
        self._creds: Credentials | None = None

    # ------------------------------------------------------------ cliente
    def client_config(self, explicit: str | Path | None = None) -> tuple[dict[str, Any], Path]:
        path = find_client_secret(self.settings, explicit)
        if path is None:
            raise AuthError(
                "Arquivo client_secret.json não encontrado. Baixe-o no Google Cloud Console "
                "(veja o README, seção 'Configurar o Google Cloud') e execute:\n"
                f"  python main.py login --client-secret {os.path.join('CAMINHO', 'client_secret.json')}"
            )
        return load_client_config(path), path

    def _remember_client_secret(self, path: Path) -> None:
        target = self.settings.stored_client_secret
        if path.resolve() != target.resolve():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)

    # -------------------------------------------------------------- login
    def stored_account(self) -> str | None:
        """E-mail da conta salva, sem acessar a rede."""
        data = self.store.load()
        return data.get("account_email") if data else None

    def _manual_flow(self, flow: Any, port: int) -> Any:
        """Login sem servidor local: para Linux sem navegador ou acesso por SSH.

        O usuário abre o link em qualquer computador; o Google redireciona para
        ``http://localhost:PORTA/?code=...`` (a página não carrega, o que é
        esperado) e o usuário cola esse endereço aqui.
        """
        flow.redirect_uri = f"http://localhost:{port}/"
        url, state = flow.authorization_url(access_type="offline", prompt="consent select_account")
        self.say(
            "\n1. Abra este endereço em um navegador (pode ser em outro computador):\n"
            f"\n{url}\n\n"
            "2. Autorize o acesso. O navegador tentará abrir um endereço 'http://localhost:...'\n"
            "   e mostrará um erro de conexão — isso é esperado.\n"
            "3. Copie o endereço COMPLETO da barra do navegador e cole abaixo.\n"
        )
        pasted = (self.ask("Endereço: ") or "").strip()
        query = parse_qs(urlsplit(pasted).query)
        if "error" in query:
            raise AuthError(f"O Google recusou a autorização: {query['error'][0]}")
        code = (query.get("code") or [None])[0]
        if not code:
            raise AuthError("O endereço colado não contém o código de autorização (parâmetro 'code').")
        if (query.get("state") or [None])[0] != state:
            raise AuthError("O endereço colado não corresponde a este login (state diferente). Repita o login.")
        try:
            flow.fetch_token(code=code)
        except Exception as exc:  # noqa: BLE001 - oauthlib
            raise AuthError(f"Não foi possível concluir o login: {type(exc).__name__}: {exc}") from exc
        return flow.credentials

    def login(self, client_secret: str | Path | None = None, switch: bool = False,
              open_browser: bool = True, manual: bool = False, port: int = 0) -> tuple[AccountInfo, bool]:
        """Autentica. Retorna ``(conta, houve_novo_login)``.

        ``manual``: fluxo de copiar/colar, para máquinas sem navegador.
        ``port``: porta fixa do servidor local (útil com ``ssh -L PORTA:localhost:PORTA``).
        """
        if not switch and self.store.load():
            try:
                creds = self.credentials()
                about = self.about_fetcher(creds)
                return AccountInfo(about["email"], about.get("name", "")), False
            except AuthError:
                log.info("autorização salva inválida; iniciando novo login")

        config, path = self.client_config(client_secret)
        flow = self.flow_factory(config, SCOPES)
        try:
            if manual:
                creds = self._manual_flow(flow, port or 8765)
            else:
                creds = flow.run_local_server(
                    host="localhost",
                    port=port,
                    open_browser=open_browser,
                    authorization_prompt_message=(
                        "Abra este endereço no navegador para autorizar o acesso (somente leitura) "
                        "ao Google Drive:\n{url}\n"
                    ),
                    success_message="Autorização concluída. Você já pode fechar esta janela e voltar ao terminal.",
                    timeout_seconds=300,
                    access_type="offline",
                    prompt="consent select_account",
                )
        except AuthError:
            raise
        except Exception as exc:  # noqa: BLE001 - oauthlib/servidor local
            raise AuthError(f"O login não foi concluído: {type(exc).__name__}: {exc}") from exc
        if creds is None:
            raise AuthError("O login não foi concluído (tempo esgotado ou janela fechada).")
        granted = set(getattr(creds, "granted_scopes", None) or creds.scopes or [])
        if granted and not set(SCOPES) <= granted:
            raise AuthError(
                "A permissão de leitura do Google Drive não foi concedida. Repita o login e "
                "marque a caixa de acesso ao Drive na tela de consentimento."
            )
        if not creds.refresh_token:
            raise AuthError("O Google não retornou um refresh token. Repita: python main.py login --switch")

        about = self.about_fetcher(creds)
        if not about.get("email"):
            raise AuthError("Não foi possível identificar o e-mail da conta autenticada.")

        old = self.store.load()
        if switch and old and old.get("refresh_token") and old.get("account_email") != about["email"]:
            self.revoke(old["refresh_token"])

        self.store.save({
            "refresh_token": creds.refresh_token,
            "scopes": SCOPES,
            "account_email": about["email"],
            "account_name": about.get("name", ""),
            "client_id": config["installed"]["client_id"],
            "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        self._remember_client_secret(path)
        self._creds = creds
        return AccountInfo(about["email"], about.get("name", "")), True

    # -------------------------------------------------------- credenciais
    def credentials(self) -> Credentials:
        """Credenciais válidas, renovando o access token quando necessário."""
        if self._creds is not None and self._creds.valid:
            return self._creds
        data = self.store.load()
        if not data or not data.get("refresh_token"):
            raise AuthError("Nenhuma conta conectada. Execute: python main.py login")
        config, _ = self.client_config()
        installed = config["installed"]
        if data.get("client_id") and data["client_id"] != installed["client_id"]:
            raise AuthError(
                "O client_secret.json atual é de outro projeto OAuth. Execute: python main.py login --switch"
            )
        creds = Credentials(
            token=None,
            refresh_token=data["refresh_token"],
            token_uri=installed.get("token_uri", TOKEN_URI),
            client_id=installed["client_id"],
            client_secret=installed["client_secret"],
            scopes=data.get("scopes") or SCOPES,
        )
        try:
            creds.refresh(self.request_factory())
        except RefreshError as exc:
            raise AuthError(
                "A autorização salva expirou ou foi revogada. Execute: python main.py login"
            ) from exc
        except TransportError as exc:
            raise AuthError(f"Sem conexão com os servidores do Google: {type(exc).__name__}") from exc
        if creds.refresh_token and creds.refresh_token != data["refresh_token"]:
            self.store.save({**data, "refresh_token": creds.refresh_token})
        self._creds = creds
        return creds

    def session_factory(self) -> Callable[[], AuthorizedSession]:
        creds = self.credentials()
        return lambda: AuthorizedSession(creds)

    # ------------------------------------------------------------- logout
    def logout(self, revoke: bool = True) -> str:
        data = self.store.load()
        if not data:
            return "Nenhuma conta estava conectada."
        email = data.get("account_email", "?")
        revoked = False
        if revoke and data.get("refresh_token"):
            revoked = self.revoke(data["refresh_token"])
        self.store.delete()
        self._creds = None
        if revoke and not revoked:
            return (f"Conta {email} desconectada localmente. Não foi possível confirmar a revogação "
                    "no Google; revise em https://myaccount.google.com/permissions")
        if revoke:
            return f"Conta {email} desconectada e autorização revogada no Google."
        return f"Conta {email} desconectada deste computador (autorização mantida no Google)."
