"""Persistent notification journal backed by a dedicated SQLite database.

Every fly-in toast, task completion and alert is recorded here so it can be
reviewed later - even when the browser that would have shown the toast was
closed.  The store mirrors the design of ``nazman.utils.command_log_store``:
a single, thread-safe SQLite connection, graceful degradation when the DB is
unwritable (dev/test), and lazy path resolution so tests can redirect it.

The journal is *circular*: only the newest ``notification_log_size`` entries
are retained (older rows are deleted on insert), with an optional day-based
retention prune on top.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, List, Optional

from .. import config

VALID_LEVELS = ("info", "success", "warning", "error")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cutoff_iso(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


class NotificationStore:
    """Thread-safe SQLite-backed notification journal (newest-first)."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._explicit_path = db_path is not None
        self._path = db_path or self._resolve_path()
        self._size = self._resolve_size()
        self._retention_days = self._resolve_retention()
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._unavailable = False
        self._pruned_at: Optional[float] = None

    @staticmethod
    def _resolve_path() -> str:
        try:
            return config.get_settings().notification_log_path
        except Exception:
            return "/var/lib/nazman/notifications.db"

    @staticmethod
    def _resolve_size() -> int:
        try:
            return int(config.get_settings().notification_log_size)
        except Exception:
            return 500

    @staticmethod
    def _resolve_retention() -> int:
        try:
            return int(config.get_settings().notification_log_retention_days)
        except Exception:
            return 90

    # ── Connection / lifecycle ──────────────────────────────────────────

    def connect(self) -> None:
        with self._lock:
            if self._conn is not None or self._unavailable:
                return
            if not self._explicit_path:
                self._path = self._resolve_path()
            self._size = self._resolve_size()
            self._retention_days = self._resolve_retention()
            parent = os.path.dirname(self._path)
            try:
                if parent:
                    os.makedirs(parent, exist_ok=True)
                conn = sqlite3.connect(self._path, timeout=15, check_same_thread=False)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=15000")
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS notifications ("
                    "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "  ts TEXT NOT NULL,"
                    "  level TEXT NOT NULL,"
                    "  title TEXT,"
                    "  message TEXT NOT NULL,"
                    "  source TEXT,"
                    "  duration_ms INTEGER,"
                    "  bytes INTEGER,"
                    "  read INTEGER NOT NULL DEFAULT 0"
                    ")"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_notifications_read "
                    "ON notifications (read, id)"
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

    @property
    def available(self) -> bool:
        return self._conn is not None

    # ── Recording ───────────────────────────────────────────────────────

    def add(
        self,
        *,
        message: str,
        level: str = "info",
        title: Optional[str] = None,
        source: Optional[str] = None,
        duration_ms: Optional[int] = None,
        bytes: Optional[int] = None,
    ) -> Optional[int]:
        """Append an entry, trimming the journal back to its circular size."""
        if level not in VALID_LEVELS:
            level = "info"
        with self._lock:
            self._ensure_conn()
            if self._conn is None:
                return None
            try:
                cur = self._conn.execute(
                    "INSERT INTO notifications "
                    "(ts, level, title, message, source, duration_ms, bytes, read) "
                    "VALUES (?,?,?,?,?,?,?,0)",
                    (_utcnow_iso(), level, title, message, source,
                     duration_ms, bytes),
                )
                new_id = cur.lastrowid
                self._conn.execute(
                    "DELETE FROM notifications WHERE id <= ?",
                    (new_id - self._size,),
                )
                self._conn.commit()
                return new_id
            except sqlite3.Error:
                return None

    def list(
        self,
        *,
        limit: int = 100,
        since_id: Optional[int] = None,
        unread_only: bool = False,
    ) -> List[Dict[str, Any]]:
        """Newest-first entries, optionally only those newer than ``since_id``."""
        with self._lock:
            self._ensure_conn()
            if self._conn is None:
                return []
            where = []
            params: List[Any] = []
            if since_id is not None:
                where.append("id > ?")
                params.append(since_id)
            if unread_only:
                where.append("read = 0")
            sql = "SELECT id, ts, level, title, message, source, duration_ms, bytes, read FROM notifications"
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY id DESC LIMIT ?"
            params.append(max(1, int(limit)))
            try:
                rows = self._conn.execute(sql, params).fetchall()
            except sqlite3.Error:
                return []
            return [
                {
                    "id": r[0], "ts": r[1], "level": r[2], "title": r[3],
                    "message": r[4], "source": r[5], "duration_ms": r[6],
                    "bytes": r[7], "read": bool(r[8]),
                }
                for r in rows
            ]

    def unread_count(self) -> int:
        with self._lock:
            self._ensure_conn()
            if self._conn is None:
                return 0
            try:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM notifications WHERE read = 0"
                ).fetchone()
            except sqlite3.Error:
                return 0
            return int(row[0]) if row else 0

    def max_id(self) -> int:
        with self._lock:
            self._ensure_conn()
            if self._conn is None:
                return 0
            try:
                row = self._conn.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM notifications"
                ).fetchone()
            except sqlite3.Error:
                return 0
            return int(row[0]) if row else 0

    # ── Mutation ────────────────────────────────────────────────────────

    def mark_read(self, ids: Optional[Iterable[int]] = None) -> None:
        with self._lock:
            self._ensure_conn()
            if self._conn is None:
                return
            try:
                if ids is None:
                    self._conn.execute("UPDATE notifications SET read = 1")
                else:
                    id_list = list(ids)
                    if not id_list:
                        return
                    placeholders = ",".join("?" for _ in id_list)
                    self._conn.execute(
                        f"UPDATE notifications SET read = 1 WHERE id IN ({placeholders})",
                        id_list,
                    )
                self._conn.commit()
            except sqlite3.Error:
                pass

    def clear(self) -> None:
        with self._lock:
            self._ensure_conn()
            if self._conn is None:
                return
            try:
                self._conn.execute("DELETE FROM notifications")
                self._conn.commit()
            except sqlite3.Error:
                pass

    def prune(self) -> None:
        now = time.time()
        if self._pruned_at is not None and (now - self._pruned_at) < 86400:
            return
        cutoff_ts = _cutoff_iso(self._retention_days)
        with self._lock:
            self._ensure_conn()
            if self._conn is None:
                self._pruned_at = now
                return
            try:
                self._conn.execute(
                    "DELETE FROM notifications WHERE ts < ?", (cutoff_ts,),
                )
                self._conn.commit()
            except sqlite3.Error:
                pass
            self._pruned_at = now

    def reset(self) -> None:
        """Drop all entries (test helper)."""
        with self._lock:
            self._ensure_conn()
            if self._conn is None:
                return
            try:
                self._conn.execute("DELETE FROM notifications")
                self._conn.commit()
            except sqlite3.Error:
                pass


# Singleton instance (mirrors command_log_store / metrics_store).
notification_store = NotificationStore()