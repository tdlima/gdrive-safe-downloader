"""Nomes de arquivo seguros para Windows, proteção contra path traversal e
movimentação atômica sem sobrescrita."""

from __future__ import annotations

import os
import re
import sys
import threading
import unicodedata
from pathlib import Path

from .errors import UnsafePathError

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_RESERVED = {
    "CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
    *(f"COM{i}" for i in "123456789¹²³"),
    *(f"LPT{i}" for i in "123456789¹²³"),
}
# Limite do NTFS: 255 unidades UTF-16 por componente. Deixamos margem para
# sufixos como " (12)" e nomes temporários.
MAX_COMPONENT_UNITS = 180
TEMP_SUFFIX = ".gdpart"
INVALID_SUFFIX = ".gdinvalid"


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _truncate_units(text: str, limit: int) -> str:
    while text and _utf16_len(text) > limit:
        text = text[:-1]
    return text


def split_ext(name: str) -> tuple[str, str]:
    """Separa nome e extensão; ``.bashrc`` não tem extensão."""
    stem, ext = os.path.splitext(name)
    if not stem:
        return name, ""
    if len(ext) > 16 or " " in ext:  # "Relatório v1.final versão 2" não tem extensão real
        return name, ""
    return stem, ext


def sanitize_component(name: str, fallback: str = "sem_nome") -> str:
    """Converte um nome do Drive em um nome de arquivo/pasta válido no Windows.

    - remove caracteres proibidos e de controle (inclui ``/`` e ``\\``);
    - impede ``.``/``..`` e nomes vazios;
    - evita nomes reservados (``CON``, ``NUL``, ``COM1``...);
    - remove pontos/espaços finais e limita o comprimento preservando a extensão.
    """
    text = unicodedata.normalize("NFC", name or "")
    text = _INVALID_CHARS.sub("_", text)
    text = text.strip().rstrip(". ")  # também elimina "." e ".."
    if not text:
        text = fallback

    stem, ext = split_ext(text)
    base = stem.split(".")[0].upper().rstrip(" ")
    if base in _RESERVED:
        stem = "_" + stem

    if _utf16_len(stem + ext) > MAX_COMPONENT_UNITS:
        ext = _truncate_units(ext, 16)
        stem = _truncate_units(stem, MAX_COMPONENT_UNITS - _utf16_len(ext)).rstrip(". ")
        if not stem:
            stem = fallback
    result = (stem + ext).rstrip(". ")
    return result or fallback


def ensure_extension(name: str, extension: str) -> str:
    """Acrescenta ``.extension`` se o nome ainda não terminar com ela."""
    if not extension:
        return name
    ext = "." + extension.lstrip(".")
    if name.lower().endswith(ext.lower()):
        return name
    return name + ext


def numbered_name(name: str, number: int) -> str:
    """``relatorio.pdf`` + 2 -> ``relatorio (2).pdf``."""
    stem, ext = split_ext(name)
    return f"{stem} ({number}){ext}"


def safe_join(root: Path, *components: str) -> Path:
    """Junta componentes já sanitizados a ``root`` garantindo que o resultado
    permaneça dentro de ``root`` (proteção contra path traversal)."""
    root_resolved = Path(os.path.abspath(root))
    candidate = root_resolved
    for comp in components:
        if comp in ("", ".", "..") or _INVALID_CHARS.search(comp) or os.path.isabs(comp):
            raise UnsafePathError(f"Componente de caminho inseguro: {comp!r}")
        candidate = candidate / comp
    candidate = Path(os.path.abspath(candidate))
    try:
        common = os.path.commonpath([str(root_resolved), str(candidate)])
    except ValueError as exc:  # unidades diferentes no Windows
        raise UnsafePathError("O caminho de destino sai do diretório escolhido.") from exc
    if os.path.normcase(common) != os.path.normcase(str(root_resolved)):
        raise UnsafePathError("O caminho de destino sai do diretório escolhido.")
    return candidate


def io_path(path: Path | str) -> str:
    """Retorna um caminho utilizável para E/S no Windows mesmo acima de 260 caracteres."""
    text = os.path.abspath(str(path))
    if sys.platform == "win32" and len(text) >= 240 and not text.startswith("\\\\?\\"):
        if text.startswith("\\\\"):
            return "\\\\?\\UNC\\" + text[2:]
        return "\\\\?\\" + text
    return text


def temp_path_for(final: Path, task_id: int) -> Path:
    """Arquivo parcial (.gdpart) na mesma pasta do destino final (rename atômico)."""
    stem = _truncate_units(final.name, 100)
    return final.parent / f"~gdsd-{task_id}-{stem}{TEMP_SUFFIX}"


def invalid_path_for(final: Path, task_id: int) -> Path:
    stem = _truncate_units(final.name, 100)
    return final.parent / f"~gdsd-{task_id}-{stem}{INVALID_SUFFIX}"


def exists(path: Path) -> bool:
    return os.path.lexists(io_path(path))


def move_no_overwrite(src: Path, dst: Path) -> None:
    """Move ``src`` para ``dst`` falhando com ``FileExistsError`` se ``dst`` existir.

    No Windows ``os.rename`` já é atômico e não sobrescreve. Em outros sistemas
    usamos ``link`` + ``unlink`` para obter a mesma garantia.
    """
    if sys.platform == "win32":
        os.rename(io_path(src), io_path(dst))
        return
    os.link(src, dst)
    os.unlink(src)


def move_overwrite(src: Path, dst: Path) -> None:
    os.replace(io_path(src), io_path(dst))


class NameAllocator:
    """Reserva nomes únicos por diretório, sem diferenciar maiúsculas/minúsculas
    (como o NTFS). Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._used: dict[str, set[str]] = {}

    def _key(self, directory: Path) -> str:
        return os.path.normcase(os.path.abspath(directory)).casefold()

    def reserve(self, directory: Path, name: str, check_disk: bool = False) -> str:
        """Reserva ``name`` (ou ``name (2)``, ``name (3)``...) em ``directory``."""
        with self._lock:
            used = self._used.setdefault(self._key(directory), set())
            candidate = name
            number = 1
            while candidate.casefold() in used or (check_disk and exists(directory / candidate)):
                number += 1
                candidate = numbered_name(name, number)
            used.add(candidate.casefold())
            return candidate

    def is_reserved(self, directory: Path, name: str) -> bool:
        with self._lock:
            return name.casefold() in self._used.get(self._key(directory), set())
