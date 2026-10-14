"""Verificação de integridade: tamanho, SHA-256 local e checksums do Google.

Níveis de verificação (do mais forte para o mais fraco):

``verified_sha256``  SHA-256 local == ``sha256Checksum`` informado pelo Google.
``verified_md5``     MD5 local == ``md5Checksum`` informado pelo Google.
``size_only``        Apenas o tamanho confere; conteúdo não verificável externamente.
``unverifiable``     Sem referência do Google (ex.: arquivos exportados/convertidos).
``failed``           Tamanho ou checksum divergente: o arquivo NÃO é considerado íntegro.

O SHA-256 local é sempre registrado como identificador do arquivo gravado, mas
sozinho não prova que ele é idêntico ao original.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from pathlib import Path

from .errors import Interrupted
from .paths import io_path

VERIFIED_SHA256 = "verified_sha256"
VERIFIED_MD5 = "verified_md5"
SIZE_ONLY = "size_only"
UNVERIFIABLE = "unverifiable"
FAILED = "failed"

LABELS = {
    VERIFIED_SHA256: "íntegro (SHA-256 confere com o Google)",
    VERIFIED_MD5: "íntegro (MD5 confere com o Google)",
    SIZE_ONLY: "tamanho confere; conteúdo não verificável externamente",
    UNVERIFIABLE: "sem referência do Google para comparar",
    FAILED: "FALHA na verificação",
}


@dataclass(frozen=True)
class FileHashes:
    size: int
    sha256: str
    md5: str


@dataclass(frozen=True)
class IntegrityResult:
    status: str
    detail: str
    hashes: FileHashes

    @property
    def ok(self) -> bool:
        return self.status != FAILED


def hash_file(path: Path, block_size: int = 1024 * 1024, stop_event: threading.Event | None = None) -> FileHashes:
    """Calcula SHA-256 e MD5 em uma única leitura, em blocos."""
    sha = hashlib.sha256()
    md5 = hashlib.md5(usedforsecurity=False)
    size = 0
    with open(io_path(path), "rb") as fh:
        while True:
            if stop_event is not None and stop_event.is_set():
                raise Interrupted("Verificação interrompida.")
            block = fh.read(block_size)
            if not block:
                break
            sha.update(block)
            md5.update(block)
            size += len(block)
    return FileHashes(size, sha.hexdigest(), md5.hexdigest())


def verify_file(
    path: Path,
    expected_size: int | None,
    google_md5: str | None,
    google_sha256: str | None,
    stop_event: threading.Event | None = None,
) -> IntegrityResult:
    """Compara o arquivo local com as referências disponíveis do Google."""
    hashes = hash_file(path, stop_event=stop_event)
    if expected_size is not None and hashes.size != expected_size:
        return IntegrityResult(
            FAILED, f"tamanho divergente: esperado {expected_size} bytes, obtido {hashes.size}", hashes
        )
    if google_sha256:
        if hashes.sha256.lower() == google_sha256.lower():
            return IntegrityResult(VERIFIED_SHA256, LABELS[VERIFIED_SHA256], hashes)
        return IntegrityResult(FAILED, "SHA-256 divergente do informado pelo Google", hashes)
    if google_md5:
        if hashes.md5.lower() == google_md5.lower():
            return IntegrityResult(VERIFIED_MD5, LABELS[VERIFIED_MD5], hashes)
        return IntegrityResult(FAILED, "MD5 divergente do informado pelo Google", hashes)
    if expected_size is not None:
        return IntegrityResult(SIZE_ONLY, LABELS[SIZE_ONLY], hashes)
    return IntegrityResult(UNVERIFIABLE, LABELS[UNVERIFIABLE], hashes)


def check_against_record(path: Path, recorded_sha256: str) -> tuple[bool, FileHashes]:
    """Confere um arquivo já baixado com o SHA-256 registrado no histórico
    (detecta alteração ou corrupção local posterior)."""
    hashes = hash_file(path)
    return hashes.sha256 == recorded_sha256, hashes
