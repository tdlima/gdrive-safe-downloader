"""Configurações, diretórios da aplicação e valores padrão.

Ordem de precedência (da maior para a menor):
1. Opções de linha de comando (aplicadas em ``cli.py``).
2. Variáveis de ambiente ``GDSD_*``.
3. Arquivo ``config.toml`` no diretório da aplicação.
4. Valores padrão definidos aqui.
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError

APP_NAME = "GDriveSafeDownloader"
KEYRING_SERVICE = "gdrive-safe-downloader"

MiB = 1024 * 1024
CONFLICT_POLICIES = ("rename", "skip", "overwrite")

DEFAULT_EXPORT_FORMATS: dict[str, str] = {
    "document": "docx",
    "spreadsheet": "xlsx",
    "presentation": "pptx",
    "drawing": "pdf",
    "script": "json",
}


def app_home() -> Path:
    """Diretório de dados da aplicação (banco, logs, client_secret, config)."""
    env = os.environ.get("GDSD_HOME")
    if env:
        return Path(env).expanduser().resolve()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_NAME
    return Path.home() / ".local" / "share" / "gdrive-safe-downloader"


@dataclass
class Settings:
    """Configuração efetiva da aplicação."""

    home: Path
    download_dir: Path
    max_workers: int = 3
    chunk_size: int = 8 * MiB
    max_retries: int = 6
    backoff_base: float = 1.0
    backoff_max: float = 64.0
    requests_per_second: float = 8.0
    connect_timeout: float = 20.0
    read_timeout: float = 120.0
    on_conflict: str = "rename"
    min_free_space: int = 200 * MiB
    log_level: str = "INFO"
    export_formats: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_EXPORT_FORMATS))

    @property
    def db_path(self) -> Path:
        return self.home / "history.sqlite3"

    @property
    def log_dir(self) -> Path:
        return self.home / "logs"

    @property
    def lock_path(self) -> Path:
        return self.home / "gdsd.lock"

    @property
    def config_path(self) -> Path:
        return self.home / "config.toml"

    @property
    def stored_client_secret(self) -> Path:
        return self.home / "client_secret.json"

    @property
    def timeout(self) -> tuple[float, float]:
        return (self.connect_timeout, self.read_timeout)

    def ensure_dirs(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def validate(self) -> None:
        if not 1 <= self.max_workers <= 8:
            raise ConfigError("max_workers deve estar entre 1 e 8.")
        if not 1 * MiB <= self.chunk_size <= 64 * MiB:
            raise ConfigError("chunk_size_mb deve estar entre 1 e 64.")
        if not 0 <= self.max_retries <= 20:
            raise ConfigError("max_retries deve estar entre 0 e 20.")
        if self.on_conflict not in CONFLICT_POLICIES:
            raise ConfigError(
                f"on_conflict inválido: {self.on_conflict!r}. Use: {', '.join(CONFLICT_POLICIES)}."
            )
        if self.requests_per_second <= 0:
            raise ConfigError("requests_per_second deve ser maior que zero.")


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Erro de sintaxe em {path}: {exc}") from exc


def load_settings(home: Path | None = None) -> Settings:
    """Carrega a configuração combinando padrões, ``config.toml`` e ambiente."""
    home = home or app_home()
    settings = Settings(home=home, download_dir=Path.cwd() / "downloads")
    data = _read_toml(home / "config.toml")

    general = data.get("geral", data)
    mapping: dict[str, tuple[str, Any]] = {
        "download_dir": ("download_dir", lambda v: Path(v).expanduser()),
        "max_workers": ("max_workers", int),
        "chunk_size_mb": ("chunk_size", lambda v: int(v) * MiB),
        "max_retries": ("max_retries", int),
        "requests_per_second": ("requests_per_second", float),
        "on_conflict": ("on_conflict", str),
        "min_free_space_mb": ("min_free_space", lambda v: int(v) * MiB),
        "log_level": ("log_level", lambda v: str(v).upper()),
        "read_timeout": ("read_timeout", float),
    }
    for key, (attr, conv) in mapping.items():
        if key in general:
            try:
                setattr(settings, attr, conv(general[key]))
            except (TypeError, ValueError) as exc:
                raise ConfigError(f"Valor inválido para {key} em config.toml: {exc}") from exc

    exports = data.get("exportacao", {})
    for kind, fmt in exports.items():
        settings.export_formats[str(kind)] = str(fmt).lower()

    env_dir = os.environ.get("GDSD_DOWNLOAD_DIR")
    if env_dir:
        settings.download_dir = Path(env_dir).expanduser()
    env_workers = os.environ.get("GDSD_MAX_WORKERS")
    if env_workers:
        try:
            settings.max_workers = int(env_workers)
        except ValueError as exc:
            raise ConfigError("GDSD_MAX_WORKERS deve ser um número inteiro.") from exc

    settings.validate()
    return settings


def find_client_secret(settings: Settings, explicit: str | Path | None = None) -> Path | None:
    """Localiza o arquivo ``client_secret.json`` do OAuth.

    Ordem: caminho explícito, ``GDSD_CLIENT_SECRET``, diretório da aplicação,
    diretório atual.
    """
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    env = os.environ.get("GDSD_CLIENT_SECRET")
    if env:
        candidates.append(Path(env).expanduser())
    candidates.append(settings.stored_client_secret)
    candidates.append(Path.cwd() / "client_secret.json")
    for path in candidates:
        if path.is_file():
            return path
    return None


EXAMPLE_CONFIG = """# Configuração do GDrive Safe Downloader (todas as chaves são opcionais)
[geral]
# download_dir = "D:/Arquivos/GoogleDrive"
# max_workers = 3            # downloads simultâneos (1 a 8)
# chunk_size_mb = 8          # tamanho do bloco de gravação/progresso
# max_retries = 6            # tentativas para erros temporários
# requests_per_second = 8    # limite de chamadas à API
# on_conflict = "rename"     # rename | skip | overwrite
# min_free_space_mb = 200    # margem mínima de espaço livre
# log_level = "INFO"

[exportacao]
# document = "docx"          # docx | pdf | odt | rtf | txt | md | epub | html
# spreadsheet = "xlsx"       # xlsx | ods | pdf | csv | tsv | html
# presentation = "pptx"      # pptx | odp | pdf | txt
# drawing = "pdf"            # pdf | png | jpg | svg
"""
