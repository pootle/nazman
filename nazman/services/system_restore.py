"""System restore composition: discover backup volumes and rebuild a NAS.

On fresh hardware there is no database of declared backup disks, so this
service works directly from the volumes: it enumerates candidate partitions,
mounts each read-only, reads the aggregate manifest/sidecars, and drives pool
and dataset restoration.  Cross-domain orchestration lives here rather than in
a route handler; the ZFS/disk/config work is delegated to the injected
managers.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from ..models.backup_zfs import BackupDisk
from ..models.disk import Disk
from ..utils import backup_manifest as bm
from ..utils.commands import run_command
from ..utils.devices import (
    get_device_path, partition_by_id, resolve_by_id, os_disk_names,
)
from ..utils.exceptions import BackupError, ValidationError
from ..utils.sizes import parse_size_to_bytes

logger = logging.getLogger(__name__)

# Filesystems that never hold a nazman backup volume.
_SKIP_FSTYPES = {
    "", "swap", "linux_raid_member", "zfs_member", "LVM2_member", "crypto_LUKS",
}


class SystemRestoreService:
    def __init__(self, disk=None, zfs=None, zfs_backup=None, backup=None, scheduler=None):
        self.disk = disk
        self.zfs = zfs
        self.zfs_backup = zfs_backup
        self.backup = backup
        self.scheduler = scheduler

    # ── Volume discovery ────────────────────────────────────────────────
    async def _block_devices(self) -> List[Dict[str, Any]]:
        try:
            stdout, _, rc = await run_command(
                ["lsblk", "-J", "-b", "-o", "NAME,TYPE,FSTYPE,UUID,LABEL,PARTLABEL,SIZE,PKNAME"],
                timeout=30, check=False, op="read", category="disk",
            )
        except Exception:
            return []
        if rc != 0 or not stdout.strip():
            return []
        try:
            return json.loads(stdout).get("blockdevices", [])
        except ValueError:
            return []

    @staticmethod
    def _in_pool(pool_members: Dict[str, str], *identities: Optional[str]) -> bool:
        """True if any device identity (whole disk or ``-partN``) is a pool member."""
        bases = {i.rsplit("/", 1)[-1] for i in identities if i}
        if not bases:
            return False
        for key in pool_members:
            kb = key.rsplit("/", 1)[-1]
            if kb in bases:
                return True
            for base in bases:
                if kb.startswith(f"{base}-part") or base.startswith(f"{kb}-part"):
                    return True
        return False

    async def list_candidates(self, db: Session) -> List[Dict[str, Any]]:
        """Mountable, non-OS, non-pool devices that may hold a backup volume."""
        devices = await self._block_devices()
        os_disks = await os_disk_names()
        pool_members = {}
        if self.zfs is not None:
            try:
                pool_members = await self.zfs.get_pool_members()
            except Exception:
                pool_members = {}

        candidates: List[Dict[str, Any]] = []

        def add(dev: Dict[str, Any], parent: Optional[Dict[str, Any]] = None) -> None:
            fstype = (dev.get("fstype") or "").lower()
            if fstype in _SKIP_FSTYPES:
                return
            name = dev.get("name") or ""
            if name in os_disks or (parent and parent.get("name") in os_disks):
                return
            base_name = (parent or dev).get("name") or name
            base_by_id = resolve_by_id(base_name)
            if dev.get("type") == "part":
                partition_number = self._partition_number(name, base_name)
                by_id = partition_by_id(base_by_id, partition_number)
            else:
                by_id = base_by_id
                partition_number = None
            if self._in_pool(pool_members, name, base_name, by_id, base_by_id):
                return
            candidates.append({
                "device": f"/dev/{name}",
                "device_name": name,
                "by_id": by_id,
                "fstype": fstype,
                "fs_uuid": dev.get("uuid"),
                "label": dev.get("label"),
                "partlabel": dev.get("partlabel"),
                "size_bytes": self._size(dev.get("size")),
                "base_by_id": base_by_id,
                "base_name": base_name,
                "partition_number": partition_number,
            })

        for dev in devices:
            if dev.get("type") == "disk":
                if dev.get("fstype"):
                    add(dev)
                for child in dev.get("children", []):
                    if child.get("type") == "part":
                        add(child, parent=dev)
        return candidates

    @staticmethod
    def _partition_number(name: str, parent_name: str) -> int:
        suffix = name[len(parent_name):] if name.startswith(parent_name) else name
        digits = "".join(ch for ch in suffix if ch.isdigit())
        return int(digits) if digits else 1

    @staticmethod
    def _size(value: Any) -> int:
        if isinstance(value, (int, float)):
            return int(value)
        try:
            return parse_size_to_bytes(str(value))
        except Exception:
            return 0

    def _scratch_root(self) -> Path:
        base = Path(self.settings_backup_mount_base()) / "restore"
        base.mkdir(parents=True, exist_ok=True)
        return base

    def settings_backup_mount_base(self) -> str:
        from ..config import get_settings
        return get_settings().backup_mount_base

    async def _mount_readonly(self, candidate: Dict[str, Any]) -> Optional[Path]:
        """Mount a candidate read-only under the scratch root (or None on failure)."""
        key = candidate.get("fs_uuid") or candidate["device_name"]
        mountpoint = self._scratch_root() / str(key)
        try:
            if Path(mountpoint).is_mount():
                return mountpoint
            mountpoint.mkdir(parents=True, exist_ok=True)
            _, _, rc = await run_command(
                ["mount", "-o", "ro", candidate["device"], str(mountpoint)],
                timeout=60, check=False, op="write", category="disk",
            )
            if rc != 0 or not Path(mountpoint).is_mount():
                return None
            return mountpoint
        except Exception:
            return None

    async def _unmount(self, mountpoint: Path) -> None:
        try:
            if mountpoint and Path(mountpoint).is_mount():
                await run_command(
                    ["umount", str(mountpoint)], timeout=60, check=False,
                    op="write", category="disk",
                )
        except Exception:
            pass

    async def _read_manifest(self, candidate: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        mountpoint = await self._mount_readonly(candidate)
        if mountpoint is None:
            return None
        try:
            manifest = bm.scan_volume(mountpoint)
            if manifest is None:
                return None
            has_content = (
                manifest.get("config_backups") or manifest.get("pools")
                or manifest.get("datasets")
            )
            if not has_content:
                return None
            manifest["_volume_root"] = str(mountpoint)
            return manifest
        finally:
            await self._unmount(mountpoint)

    def _set_id(self, candidate: Dict[str, Any]) -> str:
        return candidate.get("fs_uuid") or f"dev:{candidate['device_name']}"

    async def discover_backup_sets(self, db: Session) -> List[Dict[str, Any]]:
        """Scan all disks and return a menu of available backup info sets."""
        sets = []
        for candidate in await self.list_candidates(db):
            manifest = await self._read_manifest(candidate)
            if manifest is None:
                continue
            summary = bm.manifest_summary(manifest)
            summary.update({
                "set_id": self._set_id(candidate),
                "device": candidate["device"],
                "by_id": candidate.get("by_id"),
                "fstype": candidate.get("fstype"),
                "label": candidate.get("label"),
                "fs_uuid": candidate.get("fs_uuid"),
                "volume_root": manifest.get("_volume_root"),
            })
            sets.append(summary)
        return sets

    async def _find_candidate(self, db: Session, set_id: str) -> Dict[str, Any]:
        for candidate in await self.list_candidates(db):
            if self._set_id(candidate) == set_id:
                return candidate
        raise BackupError(f"Backup set {set_id} not found on any attached disk")

    async def _manifest_for(self, db: Session, set_id: str) -> Dict[str, Any]:
        candidate = await self._find_candidate(db, set_id)
        manifest = await self._read_manifest(candidate)
        if manifest is None:
            raise BackupError(f"Backup set {set_id} could not be read")
        manifest["_candidate"] = candidate
        return manifest

    async def get_backup_set(self, db: Session, set_id: str) -> Dict[str, Any]:
        """Full detail of one backup set (pools, datasets, config, media)."""
        manifest = await self._manifest_for(db, set_id)
        candidate = manifest.pop("_candidate", {})
        manifest.pop("_volume_root", None)
        return {
            "set_id": set_id,
            "candidate": candidate,
            "summary": bm.manifest_summary(manifest),
            "manifest": manifest,
            "required_media": await self.required_media(db, set_id),
        }

    # ── Pool recreation ─────────────────────────────────────────────────
    async def _attached_disks(self, db: Session) -> List[Dict[str, Any]]:
        if self.disk is not None:
            await self.disk.sync_disks_to_database(db)
        return [
            {
                "disk_id": d.id,
                "by_id": d.by_id,
                "serial": d.serial,
                "size_bytes": d.size_bytes,
                "model": d.model,
                "device_path": get_device_path(d),
                "present": bool(get_device_path(d)),
                "is_os_disk": d.is_os_disk,
            }
            for d in db.query(Disk).all()
            if not d.is_os_disk
        ]

    async def plan_pool_mapping(self, db: Session, set_id: str, pool_name: str) -> Dict[str, Any]:
        """Suggest which attached disk fills each recorded vdev slot.

        Matching is by-id first, then serial (a disk moved to a new controller
        keeps its serial).  Unmatched slots are returned for manual assignment.
        """
        manifest = await self._manifest_for(db, set_id)
        pool = next((p for p in manifest.get("pools", []) if p.get("name") == pool_name), None)
        if pool is None:
            raise ValidationError(f"Pool '{pool_name}' not found in backup set")

        attached = await self._attached_disks(db)
        used: set = set()
        vdevs = []
        for vdev in pool.get("vdevs", []):
            devices = []
            for spec in vdev.get("devices", []):
                match = self._match_disk(spec, attached, used)
                if match:
                    used.add(match["disk_id"])
                devices.append({
                    "spec": spec,
                    "matched_disk_id": match["disk_id"] if match else None,
                    "match": "by_id" if match and match["by_id"] == spec.get("by_id")
                    else ("serial" if match else None),
                    "size_ok": bool(match and (not spec.get("size_bytes")
                                or match["size_bytes"] >= spec["size_bytes"])),
                })
            vdevs.append({
                "role": vdev.get("role"), "topology": vdev.get("topology"),
                "ashift": vdev.get("ashift"), "devices": devices,
            })

        return {
            "pool": pool_name,
            "ashift": pool.get("ashift"),
            "vdevs": vdevs,
            "available_disks": [d for d in attached if d["disk_id"] not in used],
            "attached_disks": attached,
        }

    @staticmethod
    def _match_disk(spec: Dict[str, Any], attached: List[Dict[str, Any]], used: set) -> Optional[Dict[str, Any]]:
        by_id = spec.get("by_id")
        serial = spec.get("serial")
        for d in attached:
            if d["disk_id"] in used:
                continue
            if by_id and d.get("by_id") == by_id:
                return d
        for d in attached:
            if d["disk_id"] in used:
                continue
            if serial and d.get("serial") == serial:
                return d
        return None

    async def create_pool_from_backup(
        self, db: Session, set_id: str, pool_name: str, vdevs: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Recreate a pool from an operator-confirmed vdev/device mapping."""
        if self.zfs is None:
            raise BackupError("ZFS manager unavailable")
        existing = await self.zfs.list_pool_names(db)
        if pool_name in existing:
            raise ValidationError(f"Pool '{pool_name}' already exists")
        for vdev in vdevs:
            if not vdev.get("devices"):
                raise ValidationError(f"Vdev '{vdev.get('role')}' has no devices assigned")
        return await self.zfs.create_pool(db, pool_name, vdevs)

    # ── Dataset restore plan ────────────────────────────────────────────
    def _chain(self, backups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Latest full plus the incrementals that follow it (creation order)."""
        ordered = sorted(backups, key=lambda b: b.get("created_at") or "")
        last_full = None
        for i, b in enumerate(ordered):
            if b.get("type") == "full":
                last_full = i
        if last_full is None:
            return ordered
        return ordered[last_full:]

    async def restore_plan(self, db: Session, set_id: str) -> List[Dict[str, Any]]:
        """Datasets to restore, with a suggested target pool (default on)."""
        manifest = await self._manifest_for(db, set_id)
        pools = await self.zfs.list_pool_names(db) if self.zfs is not None else []
        plan = []
        for ds in manifest.get("datasets", []):
            backups = ds.get("backups") or []
            if not backups:
                continue
            source = ds.get("name")
            leaf = source.split("/", 1)[1] if "/" in source else source
            suggested = ds.get("pool") if ds.get("pool") in pools else None
            media = sorted({
                b.get("media_fs_uuid") for b in backups if b.get("media_fs_uuid")
            })
            plan.append({
                "source_dataset": source,
                "leaf": leaf,
                "suggested_pool": suggested,
                "pools": pools,
                "enabled": True,
                "media_fs_uuids": media,
                "chain": self._chain(backups),
            })
        return plan

    # ── Media grouping ──────────────────────────────────────────────────
    async def required_media(self, db: Session, set_id: str) -> List[Dict[str, Any]]:
        manifest = await self._manifest_for(db, set_id)
        candidates = await self.list_candidates(db)
        present = {self._set_id(c): c for c in candidates}
        groups: Dict[str, Dict[str, Any]] = {}
        for ds in manifest.get("datasets", []):
            for b in self._chain(ds.get("backups") or []):
                key = b.get("media_fs_uuid") or ""
                group = groups.setdefault(key, {
                    "media_fs_uuid": key or None,
                    "label": b.get("media_label"),
                    "datasets": [],
                    "connected": key in present if key else False,
                })
                if ds.get("name") not in group["datasets"]:
                    group["datasets"].append(ds.get("name"))
        return list(groups.values())

    async def _mount_for_set(self, db: Session, set_id: str) -> tuple:
        candidate = await self._find_candidate(db, set_id)
        mountpoint = await self._mount_readonly(candidate)
        if mountpoint is None:
            raise BackupError(f"Could not mount backup volume {set_id}")
        return candidate, mountpoint

    async def restore_datasets(
        self, db: Session, set_id: str, selections: List[Dict[str, Any]],
        media_fs_uuid: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Replay each selected dataset's latest chain into its target pool.

        ``selections`` entries are ``{source_dataset, target_pool, enabled}``.
        When ``media_fs_uuid`` is given only datasets whose chain lives entirely
        on that medium are restored (used to prompt disk-by-disk).
        """
        if self.zfs_backup is None:
            raise BackupError("ZFS backup manager unavailable")
        manifest = await self._manifest_for(db, set_id)
        manifest.pop("_candidate", None)
        manifest.pop("_volume_root", None)

        by_name = {d.get("name"): d for d in manifest.get("datasets", [])}
        chosen = []
        for sel in selections:
            if not sel.get("enabled", True):
                continue
            source = sel.get("source_dataset")
            target_pool = sel.get("target_pool")
            ds = by_name.get(source)
            if ds is None or not target_pool:
                continue
            chain = self._chain(ds.get("backups") or [])
            if not chain:
                continue
            leaf = source.split("/", 1)[1] if "/" in source else source
            target = f"{target_pool}/{leaf}"
            chain_media = {(b.get("media_fs_uuid") or None) for b in chain}
            if len(chain_media) > 1:
                chosen.append((source, target, chain, None,
                               "chain spans multiple backup media"))
                continue
            media = next(iter(chain_media))
            if media_fs_uuid is not None and media != media_fs_uuid:
                continue
            chosen.append((source, target, chain, media, None))

        chosen.sort(key=lambda item: item[0].count("/"))

        results = []
        groups: Dict[Optional[str], List[tuple]] = {}
        for source, target, chain, media, err in chosen:
            if err:
                results.append({"source": source, "target": target, "status": "failed", "error": err})
            else:
                groups.setdefault(media, []).append((source, target, chain))

        # Each group's streams live on one medium; mount it for the replay.
        for media, items in groups.items():
            volume_root = await self._mount_media(db, set_id, media)
            if volume_root is None:
                for source, target, _chain in items:
                    results.append({
                        "source": source, "target": target, "status": "failed",
                        "error": "Backup medium is not connected",
                    })
                continue
            try:
                for source, target, chain in items:
                    results.append(await self._restore_one(source, target, chain, volume_root))
            finally:
                await self._unmount(volume_root)
        return {"set_id": set_id, "results": results}

    async def _mount_media(self, db: Session, set_id: str, media_uuid: Optional[str]) -> Optional[Path]:
        """Mount the volume holding a dataset chain's streams.

        Falls back to the selected set's own volume when no media UUID was
        recorded; otherwise the matching candidate is mounted read-only.
        """
        if not media_uuid or media_uuid == set_id:
            _candidate, mountpoint = await self._mount_for_set(db, set_id)
            return mountpoint
        for candidate in await self.list_candidates(db):
            if self._set_id(candidate) == media_uuid:
                return await self._mount_readonly(candidate)
        return None

    async def _restore_one(
        self, source: str, target: str, chain: List[Dict[str, Any]], volume_root: Path,
    ) -> Dict[str, Any]:
        exists = await self.zfs.dataset_exists(target) if self.zfs is not None else False
        entry: Dict[str, Any] = {"source": source, "target": target, "streams": []}
        try:
            for b in chain:
                stream = volume_root / (b.get("stream_file") or "")
                result = await self.zfs_backup.receive_stream(
                    str(stream), target, force=exists or b.get("type") == "incremental",
                )
                entry["streams"].append(result)
                exists = True
            entry["status"] = "success"
        except Exception as e:
            entry["status"] = "failed"
            entry["error"] = str(e)
        return entry

    # ── Configuration restore ───────────────────────────────────────────
    async def restore_configuration(self, db: Session, set_id: str, config_id: str) -> Dict[str, Any]:
        manifest = await self._manifest_for(db, set_id)
        manifest.pop("_candidate", None)
        manifest.pop("_volume_root", None)
        entry = next((e for e in manifest.get("config_backups", []) if e.get("id") == config_id), None)
        if entry is None:
            raise ValidationError(f"Config backup {config_id} not found in set")
        if self.backup is None:
            raise BackupError("Backup manager unavailable")

        _candidate, volume_root = await self._mount_for_set(db, set_id)
        try:
            bundle = volume_root / (entry.get("path") or "")
            await self.backup.restore_configuration_bundle(db, bundle)
        finally:
            await self._unmount(volume_root)
        return {"set_id": set_id, "config_id": config_id, "restored": True}

    # ── Post-restore adoption (independent options) ─────────────────────
    async def adopt_media(self, db: Session, set_id: str) -> Dict[str, Any]:
        """Re-register the backup volume as a declared backup disk."""
        candidate = await self._find_candidate(db, set_id)
        if not candidate.get("fs_uuid"):
            raise ValidationError("Backup volume has no filesystem UUID; cannot adopt")
        existing = db.query(BackupDisk).filter(BackupDisk.fs_uuid == candidate["fs_uuid"]).first()
        if existing:
            return {"adopted": False, "backup_disk_id": existing.id, "message": "Already declared"}

        base_by_id = candidate.get("base_by_id")
        disk = db.query(Disk).filter(Disk.by_id == base_by_id).first() if base_by_id else None
        if disk is None:
            raise ValidationError("Could not match the backup volume to an attached disk")

        from ..config import get_settings
        mount_base = Path(get_settings().backup_mount_base)
        mount_point = mount_base / candidate["fs_uuid"]
        mount_point.mkdir(parents=True, exist_ok=True)
        await run_command(
            ["mount", candidate["device"], str(mount_point)],
            timeout=60, check=False, op="write", category="disk",
        )
        rec = BackupDisk(
            disk_id=disk.id,
            slot_uuid=None,
            partition_number=candidate.get("partition_number") or 1,
            label=candidate.get("label"),
            fs_type=candidate.get("fstype") or "ext4",
            mount_point=str(mount_point),
            fs_uuid=candidate["fs_uuid"],
        )
        db.add(rec)
        db.commit()
        db.refresh(rec)
        return {"adopted": True, "backup_disk_id": rec.id, "mount_point": str(mount_point)}

    async def rebuild_schedules(self, db: Session, set_id: str) -> Dict[str, Any]:
        """Reconcile restored backup schedules into scheduler jobs."""
        if self.zfs_backup is None:
            raise BackupError("ZFS backup manager unavailable")
        await self.zfs_backup.sync_scheduled_tasks(db)
        count = len(db.query(BackupDisk).all())
        return {"set_id": set_id, "backup_disks": count, "schedules_synced": True}
