from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FakeDrive  # noqa: E402
from gdsd.config import MiB, Settings  # noqa: E402
from gdsd.downloader import Downloader  # noqa: E402
from gdsd.drive_client import DriveClient  # noqa: E402
from gdsd.history import History  # noqa: E402
from gdsd.retry import RetryPolicy  # noqa: E402

ACCOUNT = "usuario@example.com"


def no_sleep(seconds, stop_event=None):  # noqa: ANN001
    return None


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("GDSD_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("GDSD_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("GDSD_DOWNLOAD_DIR", raising=False)


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = Settings(home=tmp_path / "home", download_dir=tmp_path / "downloads")
    s.min_free_space = 0
    s.max_workers = 2
    s.chunk_size = 1 * MiB
    s.max_retries = 3
    s.ensure_dirs()
    return s


@pytest.fixture
def drive() -> FakeDrive:
    return FakeDrive()


@pytest.fixture
def policy() -> RetryPolicy:
    return RetryPolicy(max_retries=3, base=0.01, cap=0.02, sleep=no_sleep)


@pytest.fixture
def client(drive, policy) -> DriveClient:
    return DriveClient(drive.session, policy)


@pytest.fixture
def history(settings):
    h = History(settings.db_path)
    yield h
    h.close()


@pytest.fixture
def downloader(client, history, settings) -> Downloader:
    return Downloader(client, history, settings, ACCOUNT)


@pytest.fixture
def dest(tmp_path) -> Path:
    d = tmp_path / "dest"
    d.mkdir()
    return d
