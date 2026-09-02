"""Command log (persistent, SQLite-backed).

This module exposes the app-facing command logger: a thin wrapper over
``nazman.utils.command_log_store`` that (a) keeps the historical ``record`` /
``get_entries`` / ``reset`` API, (b) tags entries (``op`` type + ``category``)
and (c) persists them to a dedicated SQLite database so they survive restarts.

Entries are tagged:
    - ``op``: read (inspect-only), write (changes storage/data), or system
      (changes host service/config/package state). Mutually exclusive.
    - ``category``: a coarser grouping (zfs, zpool, smartctl, disk, systemd,
      package, smb, nfs, backup, ...).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from .command_log_store import command_log_store
from .command_log_store import CommandLogStore


class CommandLogger:
    """Persistent, thread-safe command log (SQLite-backed)."""

    def __init__(self, store: Optional["CommandLogStore"] = None) -> None:
        # Default to the global store; tests inject an isolated store.
        self._store = store or command_log_store

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
        self._store.record(
            command=command,
            status=status,
            returncode=returncode,
            stderr=stderr,
            duration_ms=duration_ms,
            op=op,
            category=category,
        )

    # ── Query ───────────────────────────────────────────────────────────

    def get_entries(
        self,
        *,
        ops: Optional[Sequence[str]] = None,
        statuses: Optional[Sequence[str]] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Return entries newest-first, optionally filtered and capped.

        ``ops``: allowed op values (read/write/system) to include.
        ``statuses``: allowed status values (success/failed/timeout/error).
        ``limit``: maximum number of entries to return (None for unlimited).
        """
        return self._store.get_entries(ops=ops, statuses=statuses, limit=limit)

    # ── Maintenance ─────────────────────────────────────────────────────

    def reset(self) -> None:
        self._store.reset()

    def prune(self) -> None:
        self._store.prune()

    def close(self) -> None:
        self._store.close()


# Singleton instance.
command_log = CommandLogger()