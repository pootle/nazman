"""Persistent per-pool metrics logging to a dedicated SQLite database.

Unlike :mod:`nazman.managers.metrics_manager` (in-memory ring buffers), this
module records samples to disk so history can be reviewed on the Performance
Monitoring page. Logging is enabled on a per-pool basis: when a pool's logging
toggle is on, that pool's disk busy% samples and the system metrics (cpu,
memory, network) are persisted.

Storage
    - ``metric_samples(pool, metric, device, ts, value)``: one row per sample.
      System metrics use the special pool key ``__system__``.
    - ``pool_metric_log(pool_name PRIMARY KEY, enabled INTEGER)``: per-pool
      logging state (survives restart).

Samples are buffered in memory and flushed in batches to keep write load low.
Old rows are pruned once a day according to ``metrics_log_retention_days``.
"""

import os
import sqlite3
import time
from collections import deque
from threading import RLock
from typing import Deque, Dict, List, Optional, Tuple

from ..config import get_settings

SYSTEM_POOL = "__system__"
BUFFER_FLUSH_COUNT = 6  # flush once every N ticks (~30s at default 5s cadence)


class MetricsStore:
    def __init__(self, db_path: Optional[str] = None) -> None:
        path = db_path or get_settings().metrics_log_path
        self._path = path
        self._lock = RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._buffer: Deque[Tuple] = deque()
        self._flush_counter = 0
        self._enabled_cache: Dict[str, bool] = {}
        self._pruned_at: Optional[float] = None
        self._unavailable = False

    # ── Connection / lifecycle ──────────────────────────────────────────

    def connect(self) -> None:
        """Open (or reopen) the SQLite database and ensure schema exists.

        If the database path cannot be opened (e.g. unwritable in dev/test),
        the store marks itself unavailable and subsequent operations degrade
        to no-ops returning empty results rather than raising.
        """
        with self._lock:
            if self._conn is not None:
                return
            if self._unavailable:
                return
            parent = os.path.dirname(self._path)
            try:
                if parent:
                    os.makedirs(parent, exist_ok=True)
                conn = sqlite3.connect(self._path, timeout=15, check_same_thread=False)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=15000")
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS metric_samples ("
                    "  pool TEXT NOT NULL,"
                    "  metric TEXT NOT NULL,"
                    "  device TEXT NOT NULL,"
                    "  ts INTEGER NOT NULL,"
                    "  value REAL NOT NULL"
                    ")"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_metric_samples "
                    "ON metric_samples (pool, metric, device, ts)"
                )
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS pool_metric_log ("
                    "  pool_name TEXT PRIMARY KEY,"
                    "  enabled INTEGER NOT NULL DEFAULT 0"
                    ")"
                )
                conn.commit()
            except (sqlite3.OperationalError, OSError):
                self._unavailable = True
                self._conn = None
                return
            self._conn = conn
            self._reload_enabled()

    def close(self) -> None:
        self.flush()
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

    # ── Enabled state (per pool) ────────────────────────────────────────

    def _reload_enabled(self) -> None:
        cached: Dict[str, bool] = {}
        if self._conn is not None:
            try:
                for row in self._conn.execute(
                    "SELECT pool_name, enabled FROM pool_metric_log"
                ):
                    cached[row[0]] = bool(row[1])
            except sqlite3.Error:
                cached = {}
        self._enabled_cache = cached

    def is_pool_enabled(self, pool_name: str) -> bool:
        return self._enabled_cache.get(pool_name, False)

    def list_enabled_pools(self) -> List[str]:
        return [p for p, e in self._enabled_cache.items() if e]

    def set_pool_enabled(self, pool_name: str, enabled: bool) -> None:
        val = 1 if enabled else 0
        with self._lock:
            self._ensure_conn()
            if self._conn is None:
                return
            self._conn.execute(
                "INSERT INTO pool_metric_log (pool_name, enabled) "
                "VALUES (?, ?) "
                "ON CONFLICT(pool_name) DO UPDATE SET enabled=excluded.enabled",
                (pool_name, val),
            )
            self._conn.commit()
            self._enabled_cache[pool_name] = bool(val)

    # ── Recording ───────────────────────────────────────────────────────

    def record(self, pool: str, metric: str, device: str, ts: int,
               value: float) -> None:
        if pool != SYSTEM_POOL and not self.is_pool_enabled(pool):
            return
        self._buffer.append((pool, metric, device, ts, float(value)))

    def tick(self) -> None:
        """Record a completed sampling tick (triggers a batch flush every N)."""
        self._flush_counter += 1
        if self._flush_counter >= BUFFER_FLUSH_COUNT:
            self._flush_counter = 0
            self.flush()

    def flush(self) -> None:
        """Persist buffered samples in one transaction."""
        if not self._buffer:
            return
        with self._lock:
            self._ensure_conn()
            if self._conn is None:
                return
            rows = list(self._buffer)
            self._buffer.clear()
            try:
                self._conn.executemany(
                    "INSERT INTO metric_samples (pool, metric, device, ts, value) "
                    "VALUES (?,?,?,?,?)",
                    rows,
                )
                self._conn.commit()
            except sqlite3.Error:
                # Return buffer on failure so nothing is lost silently.
                self._buffer.extendleft(reversed(rows))

    # ── Pruning ─────────────────────────────────────────────────────────

    def prune(self) -> None:
        try:
            retention_days = get_settings().metrics_log_retention_days
        except Exception:
            retention_days = 30
        now = time.time()
        if self._pruned_at is not None and (now - self._pruned_at) < 86400:
            return
        cutoff = int(now - retention_days * 86400)
        with self._lock:
            self._ensure_conn()
            if self._conn is None:
                self._pruned_at = now
                return
            try:
                self._conn.execute(
                    "DELETE FROM metric_samples WHERE ts < ?", (cutoff,)
                )
                self._conn.commit()
                self._pruned_at = now
            except sqlite3.Error:
                self._pruned_at = None

    # ── Queries ─────────────────────────────────────────────────────────

    def query_metric(self, pool: str, metric: str,
                     device: Optional[str], start_ts: int,
                     end_ts: int) -> List[Dict[str, float]]:
        """Return [{ts, value}] sorted ascending within [start_ts, end_ts]."""
        with self._lock:
            self._ensure_conn()
            if self._conn is None:
                return []
            q = (
                "SELECT ts, value FROM metric_samples "
                "WHERE pool=? AND metric=? AND ts>=? AND ts<=?"
            )
            params: list = [pool, metric, int(start_ts), int(end_ts)]
            if device:
                q += " AND device=?"
                params.append(device)
            q += " ORDER BY ts ASC"
            try:
                rows = self._conn.execute(q, params).fetchall()
            except sqlite3.Error:
                return []
        return [{"ts": r[0], "value": r[1]} for r in rows]

    def distinct_devices(self, pool: str, metric: str) -> List[str]:
        """Return distinct device names recorded for a pool+metric in order."""
        with self._lock:
            self._ensure_conn()
            if self._conn is None:
                return []
            try:
                rows = self._conn.execute(
                    "SELECT DISTINCT device FROM metric_samples "
                    "WHERE pool=? AND metric=? AND device!='<system>' "
                    "ORDER BY device",
                    (pool, metric),
                ).fetchall()
            except sqlite3.Error:
                return []
        return [r[0] for r in rows]

    def oldest_ts(self) -> Optional[int]:
        with self._lock:
            self._ensure_conn()
            if self._conn is None:
                return None
            try:
                row = self._conn.execute(
                    "SELECT MIN(ts) FROM metric_samples"
                ).fetchone()
            except sqlite3.Error:
                return None
        return row[0] if row and row[0] is not None else None

    def db_size_bytes(self) -> int:
        try:
            return os.path.getsize(self._path)
        except OSError:
            return 0


# Singleton instance
metrics_store = MetricsStore()