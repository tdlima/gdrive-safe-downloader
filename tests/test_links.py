import pytest

from gdsd.errors import InvalidLinkError
from gdsd.links import parse_drive_reference

FID = "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"


@pytest.mark.parametrize("link,kind", [
    (f"https://drive.google.com/file/d/{FID}/view?usp=sharing", "file"),
    (f"https://drive.google.com/file/u/1/d/{FID}/view", "file"),
    (f"https://drive.google.com/drive/folders/{FID}", "folder"),
    (f"https://drive.google.com/drive/u/0/folders/{FID}?usp=drive_link", "folder"),
    (f"https://drive.google.com/open?id={FID}", "unknown"),
    (f"https://drive.google.com/uc?id={FID}&export=download", "unknown"),
    (f"https://drive.usercontent.google.com/download?id={FID}", "unknown"),
    (f"https://docs.google.com/document/d/{FID}/edit", "file"),
    (f"https://docs.google.com/spreadsheets/d/{FID}/edit#gid=0", "file"),
    (f"https://docs.google.com/presentation/u/2/d/{FID}/edit", "file"),
    (f"https://docs.google.com/a/empresa.com/document/d/{FID}/edit", "file"),
    (f"drive.google.com/file/d/{FID}/view", "file"),
    (f"  \"https://drive.google.com/file/d/{FID}/view\"  ", "file"),
])
def test_links_validos(link, kind):
    ref = parse_drive_reference(link)
    assert ref.file_id == FID
    assert ref.kind_hint == kind


def test_id_puro():
    assert parse_drive_reference(FID).file_id == FID


def test_resource_key_preservada():
    ref = parse_drive_reference(f"https://drive.google.com/file/d/{FID}/view?resourcekey=0-abcDEF")
    assert ref.resource_key == "0-abcDEF"


@pytest.mark.parametrize("link", [
    "",
    "   ",
    "abc",                                                    # curto demais
    "https://evil.example.com/file/d/" + FID,                 # domínio errado
    "https://drive.google.com.evil.com/file/d/" + FID,        # domínio disfarçado
    "ftp://drive.google.com/file/d/" + FID,
    "https://drive.google.com/drive/my-drive",                # seção, não recurso
    "https://drive.google.com/drive/shared-with-me",
    "https://docs.google.com/document/d/e/2PACX-1vabcdefghijkl/pub",  # publicado na web
    "https://drive.google.com/file/d/../../etc/view",
    "https://drive.google.com/",
    "id com espaço inválido",
])
def test_links_invalidos(link):
    with pytest.raises(InvalidLinkError):
        parse_drive_reference(link)
