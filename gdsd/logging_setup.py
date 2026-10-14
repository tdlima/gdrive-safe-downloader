"""Logs estruturados (JSON por linha) com remoção de credenciais."""

from __future__ import annotations

import json
import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

_SECRET_PATTERNS = [
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"), r"\1[REMOVIDO]"),
    (re.compile(r"ya29\.[A-Za-z0-9._-]+"), "[TOKEN-REMOVIDO]"),
    (re.compile(r"1//[A-Za-z0-9._-]{20,}"), "[REFRESH-TOKEN-REMOVIDO]"),
    (re.compile(r"(?i)((?:access|refresh|id)_token|client_secret)(['\"]?\s*[:=]\s*['\"]?)[^&'\",\s}]+"),
     r"\1\2[REMOVIDO]"),
    (re.compile(r"(?i)([?&]code=)[^&\s]+"), r"\1[REMOVIDO]"),
    (re.compile(r"GOCSPX-[A-Za-z0-9_-]+"), "[CLIENT-SECRET-REMOVIDO]"),
]


def redact(text: str) -> str:
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "thread": record.threadName,
            "msg": record.getMessage(),
        }
        ctx = getattr(record, "ctx", None)
        if ctx:
            payload["ctx"] = ctx
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return redact(json.dumps(payload, ensure_ascii=False, default=str))


def setup_logging(log_dir: Path, level: str = "INFO", verbose: bool = False) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "gdsd.log"
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    file_handler = RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    file_handler.setLevel(getattr(logging, level.upper(), logging.INFO))
    file_handler.setFormatter(JsonFormatter())
    root.addHandler(file_handler)

    if verbose:
        console = logging.StreamHandler()
        console.setLevel(logging.DEBUG)
        console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        console.addFilter(lambda r: setattr(r, "msg", redact(str(r.msg))) or True)
        root.addHandler(console)

    for noisy in ("urllib3", "google_auth_oauthlib", "google.auth", "keyring"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return log_file
