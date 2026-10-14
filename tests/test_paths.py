import os
import sys

import pytest

from gdsd.errors import UnsafePathError
from gdsd.paths import (
    NameAllocator,
    ensure_extension,
    io_path,
    move_no_overwrite,
    numbered_name,
    safe_join,
    sanitize_component,
)


@pytest.mark.parametrize("raw,expected", [
    ("relatório.pdf", "relatório.pdf"),
    ("a/b\\c.txt", "a_b_c.txt"),
    ('x<>:"|?*.doc', "x_______.doc"),
    ("..", "sem_nome"),
    (".", "sem_nome"),
    ("", "sem_nome"),
    ("   ", "sem_nome"),
    ("nome. ", "nome"),
    ("CON", "_CON"),
    ("con.txt", "_con.txt"),
    ("NUL.tar.gz", "_NUL.tar.gz"),
    ("COM1", "_COM1"),
    ("LPT9.log", "_LPT9.log"),
    ("CONSOLE.txt", "CONSOLE.txt"),
    ("tab\there", "tab_here"),
])
def test_sanitize(raw, expected):
    assert sanitize_component(raw) == expected


def test_traversal_vira_nome_comum():
    name = sanitize_component("../../Windows/System32/evil.dll")
    assert "/" not in name and "\\" not in name
    assert name not in ("..", ".")


def test_nome_longo_preserva_extensao():
    name = sanitize_component("a" * 400 + ".pdf")
    assert name.endswith(".pdf")
    assert len(name.encode("utf-16-le")) // 2 <= 180


def test_nome_longo_com_emoji():
    name = sanitize_component("😀" * 200 + ".txt")
    assert name.endswith(".txt")
    assert len(name.encode("utf-16-le")) // 2 <= 180


def test_safe_join_dentro(tmp_path):
    p = safe_join(tmp_path, "pasta", "arquivo.txt")
    assert p == (tmp_path / "pasta" / "arquivo.txt").resolve() or str(p).startswith(str(tmp_path))


@pytest.mark.parametrize("comps", [
    ("..", "x"),
    ("a", "..", "..", "x"),
    ("a/../../x",),
    ("C:\\Windows",),
    ("/etc/passwd",),
    ("",),
])
def test_safe_join_rejeita(tmp_path, comps):
    with pytest.raises(UnsafePathError):
        safe_join(tmp_path, *comps)


def test_ensure_extension():
    assert ensure_extension("Relatório", "docx") == "Relatório.docx"
    assert ensure_extension("Relatório.DOCX", "docx") == "Relatório.DOCX"
    assert ensure_extension("Planilha.xlsx", "csv") == "Planilha.xlsx.csv"


def test_numbered_name():
    assert numbered_name("a.pdf", 2) == "a (2).pdf"
    assert numbered_name("sem_extensao", 3) == "sem_extensao (3)"


def test_allocator_case_insensitive(tmp_path):
    alloc = NameAllocator()
    assert alloc.reserve(tmp_path, "Foto.JPG") == "Foto.JPG"
    assert alloc.reserve(tmp_path, "foto.jpg") == "foto (2).jpg"
    assert alloc.reserve(tmp_path, "FOTO.jpg") == "FOTO (3).jpg"
    assert alloc.reserve(tmp_path / "outra", "foto.jpg") == "foto.jpg"


def test_allocator_considera_disco(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    alloc = NameAllocator()
    assert alloc.reserve(tmp_path, "a.txt", check_disk=True) == "a (2).txt"


def test_move_no_overwrite(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.write_text("novo")
    dst.write_text("original")
    with pytest.raises(FileExistsError):
        move_no_overwrite(src, dst)
    assert dst.read_text() == "original"


@pytest.mark.skipif(sys.platform != "win32", reason="prefixo de caminho longo só no Windows")
def test_io_path_longo(tmp_path):
    long = tmp_path.joinpath(*["d" * 50] * 6, "arquivo.txt")
    path = io_path(long)
    assert path.startswith("\\\\?\\")
    os.makedirs(io_path(long.parent), exist_ok=True)
    with open(path, "w") as fh:
        fh.write("ok")
    assert open(path).read() == "ok"
