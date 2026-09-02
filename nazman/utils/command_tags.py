"""Command-log entry tags.

Every command-log entry carries exactly one ``op`` type and, optionally, a
coarser ``category`` for finer grouping.

Type semantics:
    read    - inspect-only command; changes nothing (smartctl, lsblk, zpool status)
    write   - creates/mutates/destroys storage or data (zpool create, zfs snapshot)
    system  - changes host service/config/package state (systemctl, apt-get, smbcontrol)

``category`` is orthogonal to ``op`` and groups entries by domain, e.g. zfs,
zpool, smartctl, disk, systemd, package, smb, nfs, backup.
"""

from __future__ import annotations

from typing import FrozenSet

# op types (strings to keep the in-memory log JSON-serializable and simple).
OP_READ: str = "read"
OP_WRITE: str = "write"
OP_SYSTEM: str = "system"

# Valid op types accepted by the filtering API.
VALID_OPS: FrozenSet[str] = frozenset({OP_READ, OP_WRITE, OP_SYSTEM})

# Valid outcome statuses (recorded by the command wrappers) accepted by the
# filtering API.
VALID_STATUSES: FrozenSet[str] = frozenset({
    "success",
    "failed",
    "timeout",
    "error",
})