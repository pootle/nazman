"""Disk *view* composition: joining disk, pool and backup state for the UI.

The disks page needs each disk's role (pool member / backup target / OS /
dead), partition usage, and ZFS error counters — facts that live in three
different managers. That cross-domain join belongs in a service, not in the
route handler.
"""

import asyncio
import re
from typing import Any, Dict, List

from sqlalchemy.orm import Session

from ..models.disk import Disk
from ..utils.devices import (
    get_device_entry, get_device_path, get_device_name,
    read_slot_uuids, partition_by_id, os_reserved_partition_names,
)


class DiskViewService:
    """Builds the enriched disk listings consumed by the disks API."""

    def __init__(self, disk, zfs, zfs_backup) -> None:
        self.disk = disk
        self.zfs = zfs
        self.zfs_backup = zfs_backup

    def get_disk_or_404(self, db: Session, disk_id: int) -> Disk:
        return self.disk.get_disk(db, disk_id)

    async def enrich(self, db: Session, disks: List[Disk]) -> List[Dict[str, Any]]:
        """Add partition count, free space %, role, and backup state to each disk.

        Each lookup runs once across all disks (a single ``lsblk`` read, a
        single ``zpool status``, and the stored backup-disk rows) rather than
        per disk.
        """
        results = await asyncio.gather(
            self.zfs.get_members_and_errors(),
            self.zfs_backup.list_backup_disks(db),
            self.disk.get_disk_usage(disks),
            return_exceptions=True,
        )
        members_errors, backup_result, usage_result = results
        pool_members, error_counts_raw = (
            members_errors
            if not isinstance(members_errors, BaseException)
            else ({}, {})
        )
        backup_rows = backup_result if not isinstance(backup_result, BaseException) else []
        if isinstance(usage_result, BaseException):
            raise usage_result
        usage = usage_result
        backup_map = {rec["disk_id"]: rec for rec in backup_rows}

        error_counts: Dict[int, Dict[str, int]] = {}
        for disk in disks:
            errors = self.zfs.pool_errors_for_disk(error_counts_raw, disk)
            if errors is not None:
                error_counts[disk.id] = errors

        views = []
        for disk in disks:
            pools: List[str] = []
            if disk.status == "dead":
                role, role_detail, backup_state = "dead", None, None
            elif disk.status == "removed" or not get_device_name(disk):
                role, role_detail, backup_state = "removed", None, None
            elif disk.is_os_disk:
                role, role_detail, backup_state = "system", None, None
            else:
                pool = self.zfs.pool_member_for_disk(pool_members, disk)
                pools = self.zfs.pools_for_disk(pool_members, disk)
                if pool:
                    role, role_detail, backup_state = "pool", pool, None
                elif disk.id in backup_map:
                    rec = backup_map[disk.id]
                    role, role_detail, backup_state = "backup", rec.get("label"), rec.get("status")
                else:
                    role, role_detail, backup_state = "unused", None, None

            disk_usage = usage.get(disk.id, {})
            entry = get_device_entry(disk) or {}
            views.append({
                "disk": disk,
                "device_name": entry.get("device_name") or get_device_name(disk),
                "device_path": entry.get("device_path") or get_device_path(disk),
                "partition_count": disk_usage.get("partition_count", 0),
                "free_percent": disk_usage.get("free_percent"),
                "role": role,
                "role_detail": role_detail,
                "pools": pools if role == "pool" else [],
                "backup_state": backup_state,
                "zfs_errors": error_counts.get(disk.id),
            })
        return views

    async def list_disks(self, db: Session) -> List[Dict[str, Any]]:
        disks = await self.disk.sync_disks_to_database(db)
        return await self.enrich(db, disks)

    async def get_disk(self, db: Session, disk_id: int) -> Dict[str, Any]:
        disk = self.disk.get_disk(db, disk_id)
        return (await self.enrich(db, [disk]))[0]

    async def health_detail(self, db: Session, disk_id: int) -> Dict[str, Any]:
        """SMART + ZFS integrity details for one disk (details modal payload).

        ``smart`` is null when the disk is not present or SMART is unavailable;
        ``zfs`` is null/empty for disks not owned by a pool.  ZFS event history
        is whatever ``zpool events`` still holds in its recent ring buffer.
        """
        disk = self.disk.get_disk(db, disk_id)
        view = (await self.enrich(db, [disk]))[0]

        smart = None
        if disk.status not in ("dead", "removed"):
            try:
                device_path = self.disk.live_device_path(disk, action="read SMART details")
            except Exception:
                device_path = None
            if device_path:
                smart = await self.disk.get_smart_details(device_path)

        zfs_info: Dict[str, Any] = {"pool": None, "errors": None, "events": []}
        if view["role"] == "pool":
            counts = await self.zfs.get_pool_error_counts()
            zfs_info["errors"] = self.zfs.pool_errors_for_disk(counts, disk)
            pool = view["role_detail"]
            if pool:
                events = await self.zfs.get_pool_error_events(pool)
                zfs_info["events"] = self.zfs.events_for_disk(
                    events, self.zfs.leaf_identities_for_disk(counts, disk)
                )
                zfs_info["pool"] = pool

        return {"view": view, "smart": smart, "zfs": zfs_info}

    async def partitions(self, db: Session, disk_id: int) -> Dict[str, Any]:
        """Read partitions from disk (reads GPT names, not DB)."""
        disk = self.disk.get_disk(db, disk_id)
        device_path = self.disk.live_device_path(disk, action="read partitions")
        slot_info = await read_slot_uuids([device_path])
        disk_parts = slot_info.get(device_path, {}).get("partitions", [])

        reserved_names = await os_reserved_partition_names()

        partitions = []
        for part in disk_parts:
            part_name = part["name"]
            m = re.search(r'(\d+)$', part_name)
            if not m:
                continue
            part_num = int(m.group(1))

            slot_uuid = part.get("slot_uuid")
            if not slot_uuid:
                continue
            dev_path = partition_by_id(disk.by_id, part_num) or part_name

            partitions.append({
                "number": part_num,
                "slot_uuid": slot_uuid,
                "device_path": dev_path,
                "size_bytes": part.get("size_bytes", 0),
                "reserved": part_name in reserved_names,
            })

        return {
            "disk_id": disk.id,
            "disk_name": get_device_name(disk) or disk.model or disk.serial,
            "partitions": partitions,
        }
