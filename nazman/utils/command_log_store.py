"""Persistent command-log store backed by a dedicated SQLite database.

The command log records an audit of every external command NAZMan runs so
admins can review/replay activity. Entries are tagged with an ``op`` type
(read/write/system) and a ``category`` for grouping.

Unlike the in-memory ring buffer this replaces, entries survive restarts. The
store mirrors the design of ``nazman.managers.metrics_store``: a single,
thread-safe SQLite connection, graceful degradation when the DB is unwritable
(in dev/test), and incremental pruning by retention days.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from .. import config


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class CommandLogStore:
    """Thread-safe SQLite-backed command log."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._explicit_path = db_path is not None
        self._path = db_path or self._resolve_path()
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._unavailable = False
        self._pruned_at: Optional[float] = None

    @staticmethod
    def _resolve_path() -> str:
        try:
            return config.get_settings().command_log_path
        except Exception:
            return "/var/lib/nazman/command_log.db"

    # ── Connection / lifecycle ──────────────────────────────────────────

    def connect(self) -> None:
        with self._lock:
            if self._conn is not None or self._unavailable:
                return
            if not self._explicit_path:
                self._path = self._resolve_path()
            parent = os.path.dirname(self._path)
            try:
                if parent:
                    os.makedirs(parent, exist_ok=True)
                conn = sqlite3.connect(self._path, timeout=15, check_same_thread=False)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=15000")
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS command_log_entries ("
                    "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "  ts TEXT NOT NULL,"
                    "  command TEXT NOT NULL,"
                    "  status TEXT NOT NULL,"
                    "  op TEXT,"
                    "  category TEXT,"
                    "  returncode INTEGER,"
                    "  stderr TEXT,"
                    "  duration_ms INTEGER"
                    ")"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_command_log_ts "
                    "ON command_log_entries (ts)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_command_log_op "
                    "ON command_log_entries (op, status)"
                )
                conn.commit()
            except (sqlite3.OperationalError, OSError):
                self._unavailable = True
                self._conn = None
                return
            self._conn = conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except sqlite3.Error:
                    pass
                self._conn = None

    def _ensure_conn(self) -> None:
        if self._conn is None and not self._unavailable:
            self.connect()

    # ── Recording ───────────────────────────────────────────────────────

    def record(
        self,
        *,
        command: str,
        status: str,
        returncode: Optional[int] = None,
        stderr: Optional[str] = None,
        duration_ms: Optional[int] = None,
        op: Optional[str] = None,
        category: Optional[str] = None,
    ) -> None:
        with self._lock:
            self._ensure_conn()
            if self._conn is None:
                return
            try:
                self._conn.execute(
                    "INSERT INTO command_log_entries "
                    "(ts, command, status, op, category, returncode, stderr, duration_ms) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (
                        _utcnow_iso(), command, status, op, category,
                        returncode, (stderr or "")[:200] if stderr else None,
                        duration_ms,
                    ),
                )
                self._conn.commit()
            except sqlite3.Error:
                pass

    # ── Pruning ─────────────────────────────────────────────────────────

    def prune(self) -> None:
        try:
            retention_days = config.get_settings().command_log_retention_days
        except Exception:
            retention_days = 30
        now = time.time()
        if self._pruned_at is not None and (now - self._pruned_at) < 86400:
            return
        cutoff_ts = _cutoff_iso(retention_days)
        with self._lock:
            self._ensure_conn()
            if self._conn is None:
                self._pruned_at = now
                return
            try:
                self._conn.execute(
                    "DELETE FROM command_log_entries WHERE ts < ?", (cutoff_ts,)
                )
                self._conn.commit()
                self._pruned_at = now
            except sqlite3.Error:
                self._pruned_at = None

    # ── Query ───────────────────────────────────────────────────────────

    def get_entries(
        self,
        *,
        ops: Optional[Sequence[str]] = None,
        statuses: Optional[Sequence[str]] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Return entries newest-first, optionally filtered and capped.

        ``ops``/``statuses`` are SQL ``IN`` filters. When ``ops`` is provided,
        entries with no ``op`` (NULL) are treated as ``write`` so they surface
        under the change filter. Pass ``None`` for no cap.
        """
        with self._lock:
            self._ensure_conn()
            if self._conn is None:
                return []
            where = []
            params: list = []
            if ops:
                expanded = []
                for o in ops:
                    expanded.append("(op = ? COLLATE NOCASE)")
                    params.append(o)
                    if o == "write":
                        # Untagged entries group as write/Change.
                        expanded.append("(op IS NULL OR op = '')")
                where.append("(" + " OR ".join(expanded) + ")")
            if statuses:
                ph = ",".join("?" for _ in statuses)
                where.append(f"status IN ({ph})")
                params.extend(statuses)
            q = "SELECT ts, command, status, op, category, returncode, stderr, duration_ms "
            q += "FROM command_log_entries"
            if where:
                q += " WHERE " + " AND ".join(where)
            q += " ORDER BY id DESC, ts DESC"
            if limit is not None:
                q += f" LIMIT {int(limit)}"
            try:
                rows = self._conn.execute(q, params).fetchall()
            except sqlite3.Error:
                return []
        return [
            {
                "ts": r[0],
                "command": r[1],
                "status": r[2],
                "op": r[3],
                "category": r[4],
                "returncode": r[5],
                "stderr": r[6],
                "duration_ms": r[7],
            }
            for r in rows
        ]

    @property
    def raw_count(self) -> int:
        with self._lock:
            self._ensure_conn()
            if self._conn is None:
                return 0
            try:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM command_log_entries"
                ).fetchone()
            except sqlite3.Error:
                return 0
        return row[0] if row else 0

    def reset(self) -> None:
        with self._lock:
            self._ensure_conn()
            if self._conn is None:
                return
            try:
                self._conn.execute("DELETE FROM command_log_entries")
                self._conn.commit()
            except sqlite3.Error:
                pass


def _cutoff_iso(days: int) -> str:
    from datetime import timedelta
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


# Singleton instance.
command_log_store = CommandLogStore()