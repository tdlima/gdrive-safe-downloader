"""Histórico persistente em SQLite (tarefas de download e execuções).

Nenhuma senha ou token é gravado neste banco.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2

PENDING = "pending"
RUNNING = "running"
COMPLETED = "completed"
INTERRUPTED = "interrupted"
CANCELLED = "cancelled"
ERROR = "error"
SKIPPED = "skipped"

STATUS_LABELS = {
    PENDING: "pendente",
    RUNNING: "em andamento",
    COMPLETED: "concluída",
    INTERRUPTED: "interrompida",
    CANCELLED: "cancelada",
    ERROR: "com erro",
    SKIPPED: "ignorada",
}
RESUMABLE = (PENDING, RUNNING, INTERRUPTED)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    account_email TEXT NOT NULL,
    kind TEXT NOT NULL,                 -- file | folder
    source_id TEXT NOT NULL,
    source_name TEXT,
    dest_root TEXT NOT NULL,
    summary TEXT
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER REFERENCES jobs(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_attempt_at TEXT,
    account_email TEXT NOT NULL,
    file_id TEXT NOT NULL,
    file_name TEXT NOT NULL,
    mime_type TEXT,
    parent_id TEXT,
    drive_id TEXT,
    resource_key TEXT,
    remote_path TEXT,
    dest_root TEXT,
    local_path TEXT NOT NULL,
    temp_path TEXT,
    expected_size INTEGER,
    downloaded_bytes INTEGER NOT NULL DEFAULT 0,
    sha256 TEXT,
    google_md5 TEXT,
    google_sha256 TEXT,
    integrity_status TEXT,
    integrity_detail TEXT,
    export_format TEXT,
    export_mime TEXT,
    remote_modified_time TEXT,
    remote_revision TEXT,
    on_conflict TEXT NOT NULL DEFAULT 'rename',
    status TEXT NOT NULL,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_file ON tasks(file_id, local_path);
CREATE INDEX IF NOT EXISTS idx_tasks_account ON tasks(account_email);
"""

_SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS synced_folders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_email TEXT NOT NULL,
    folder_id TEXT NOT NULL,
    resource_key TEXT,
    remote_name TEXT,
    dest_root TEXT NOT NULL,            -- pasta escolhida com --dest
    local_root TEXT NOT NULL,           -- nome da pasta criada dentro de dest_root
    options TEXT,                       -- JSON: formatos de exportação, conflitos
    created_at TEXT NOT NULL,
    last_sync_at TEXT,
    last_summary TEXT,
    UNIQUE(account_email, folder_id, dest_root)
);
"""

TASK_COLUMNS = {
    "job_id", "account_email", "file_id", "file_name", "mime_type", "parent_id", "drive_id",
    "resource_key", "remote_path", "dest_root", "local_path", "temp_path", "expected_size",
    "downloaded_bytes", "sha256", "google_md5", "google_sha256", "integrity_status",
    "integrity_detail", "export_format", "export_mime", "remote_modified_time", "remote_revision",
    "on_conflict", "status", "error", "attempts", "last_attempt_at",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class History:
    """Acesso ao banco SQLite. Uma conexão protegida por lock (thread-safe)."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._migrate()

    def _migrate(self) -> None:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError("Banco de histórico criado por uma versão mais nova do programa.")
        if version < 1:
            self._conn.executescript(_SCHEMA)
        if version < 2:
            self._conn.executescript(_SCHEMA_V2)
            self._backfill_synced_folders()
        if version < SCHEMA_VERSION:
            self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self._conn.commit()

    def _backfill_synced_folders(self) -> None:
        """Registra para sincronização as pastas baixadas antes da versão 2."""
        jobs = self._conn.execute(
            "SELECT * FROM jobs WHERE kind='folder' ORDER BY id").fetchall()
        for job in jobs:
            row = self._conn.execute(
                "SELECT local_path FROM tasks WHERE job_id=? LIMIT 1", (job["id"],)).fetchone()
            if not row:
                continue
            try:
                rel = Path(row["local_path"]).relative_to(Path(job["dest_root"]))
            except ValueError:
                continue
            if not rel.parts:
                continue
            self._conn.execute(
                "INSERT OR IGNORE INTO synced_folders(account_email, folder_id, remote_name, dest_root,"
                " local_root, options, created_at, last_sync_at) VALUES (?,?,?,?,?,?,?,?)",
                (job["account_email"], job["source_id"], job["source_name"], job["dest_root"],
                 rel.parts[0], "{}", job["created_at"], job["finished_at"] or job["created_at"]),
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "History":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ jobs
    def create_job(self, account_email: str, kind: str, source_id: str, source_name: str, dest_root: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO jobs(created_at, account_email, kind, source_id, source_name, dest_root)"
                " VALUES (?,?,?,?,?,?)",
                (now_iso(), account_email, kind, source_id, source_name, dest_root),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def finish_job(self, job_id: int, summary: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE jobs SET finished_at=?, summary=? WHERE id=?", (now_iso(), summary, job_id))
            self._conn.commit()

    # -------------------------------------------------------- sincronização
    def register_synced_folder(self, account_email: str, folder_id: str, remote_name: str,
                               dest_root: str, local_root: str, options: dict[str, Any],
                               resource_key: str | None = None) -> int:
        """Cria ou atualiza o registro de uma pasta baixada (para ``sync``)."""
        ts = now_iso()
        with self._lock:
            self._conn.execute(
                "INSERT INTO synced_folders(account_email, folder_id, resource_key, remote_name, dest_root,"
                " local_root, options, created_at) VALUES (?,?,?,?,?,?,?,?)"
                " ON CONFLICT(account_email, folder_id, dest_root) DO UPDATE SET"
                " remote_name=excluded.remote_name, options=excluded.options,"
                " resource_key=COALESCE(excluded.resource_key, resource_key)",
                (account_email, folder_id, resource_key, remote_name, dest_root, local_root,
                 json.dumps(options, ensure_ascii=False), ts),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT id FROM synced_folders WHERE account_email=? AND folder_id=? AND dest_root=?",
                (account_email, folder_id, dest_root)).fetchone()
            return int(row["id"])

    def _synced(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        try:
            data["options"] = json.loads(data.get("options") or "{}")
        except json.JSONDecodeError:
            data["options"] = {}
        return data

    def list_synced_folders(self, account_email: str | None = None,
                            folder_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM synced_folders"
        clauses, params = [], []
        if account_email:
            clauses.append("account_email=?")
            params.append(account_email)
        if folder_id:
            clauses.append("folder_id=?")
            params.append(folder_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        with self._lock:
            return [self._synced(r) for r in self._conn.execute(sql, params).fetchall()]

    def get_synced_folder(self, sync_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM synced_folders WHERE id=?", (sync_id,)).fetchone()
        return self._synced(row) if row else None

    def mark_synced(self, sync_id: int, summary: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE synced_folders SET last_sync_at=?, last_summary=? WHERE id=?",
                               (now_iso(), summary, sync_id))
            self._conn.commit()

    def remove_synced_folder(self, sync_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM synced_folders WHERE id=?", (sync_id,))
            self._conn.commit()
            return cur.rowcount > 0

    # ----------------------------------------------------------------- tasks
    def create_task(self, **fields: Any) -> int:
        unknown = set(fields) - TASK_COLUMNS
        if unknown:
            raise ValueError(f"Campos desconhecidos: {unknown}")
        fields.setdefault("status", PENDING)
        ts = now_iso()
        cols = ["created_at", "updated_at", *fields.keys()]
        values = [ts, ts, *fields.values()]
        with self._lock:
            cur = self._conn.execute(
                f"INSERT INTO tasks({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", values
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def update_task(self, task_id: int, **fields: Any) -> None:
        unknown = set(fields) - TASK_COLUMNS
        if unknown:
            raise ValueError(f"Campos desconhecidos: {unknown}")
        fields["updated_at"] = now_iso()
        assignments = ",".join(f"{k}=?" for k in fields)
        with self._lock:
            self._conn.execute(f"UPDATE tasks SET {assignments} WHERE id=?", [*fields.values(), task_id])
            self._conn.commit()

    def get_task(self, task_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def list_tasks(
        self,
        statuses: Iterable[str] | None = None,
        account_email: str | None = None,
        name_like: str | None = None,
        job_id: int | None = None,
        limit: int | None = None,
        newest_first: bool = True,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if statuses:
            statuses = list(statuses)
            clauses.append(f"status IN ({','.join('?' * len(statuses))})")
            params.extend(statuses)
        if account_email:
            clauses.append("account_email = ?")
            params.append(account_email)
        if name_like:
            clauses.append("file_name LIKE ? ESCAPE '\\'")
            escaped = name_like.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.append(f"%{escaped}%")
        if job_id is not None:
            clauses.append("job_id = ?")
            params.append(job_id)
        sql = "SELECT * FROM tasks"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id " + ("DESC" if newest_first else "ASC")
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def find_reusable_task(self, account_email: str, file_id: str, local_path: str) -> dict[str, Any] | None:
        """Tarefa anterior do mesmo arquivo remoto para o mesmo destino local."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE account_email=? AND file_id=? AND local_path=? "
                "AND status != ? ORDER BY id DESC LIMIT 1",
                (account_email, file_id, local_path, CANCELLED),
            ).fetchone()
        return dict(row) if row else None

    def completed_for_file(self, account_email: str, file_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE account_email=? AND file_id=? AND status=? ORDER BY id DESC",
                (account_email, file_id, COMPLETED),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_stale_running(self) -> int:
        """Tarefas 'em andamento' de uma execução que terminou inesperadamente
        passam a 'interrompida'. Só deve ser chamado com o lock de instância."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tasks SET status=?, updated_at=?, error=COALESCE(error, ?) WHERE status=?",
                (INTERRUPTED, now_iso(), "Execução encerrada inesperadamente.", RUNNING),
            )
            self._conn.commit()
            return cur.rowcount

    def counts(self, account_email: str | None = None) -> dict[str, int]:
        sql = "SELECT status, COUNT(*) FROM tasks"
        params: list[Any] = []
        if account_email:
            sql += " WHERE account_email=?"
            params.append(account_email)
        sql += " GROUP BY status"
        with self._lock:
            return {r[0]: r[1] for r in self._conn.execute(sql, params).fetchall()}
