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
    get_device_path, partition_by_id, read_slot_uuids, resolve_by_id,
    os_disk_names,
)
from ..utils.exceptions import BackupError, ValidationError
from ..utils.sizes import parse_size_to_bytes
from ..utils.paths import ensure_dir

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
        base = ensure_dir(Path(self.settings_backup_mount_base()) / "restore")
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
            ensure_dir(mountpoint)
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

    def _volume_id(self, candidate: Dict[str, Any]) -> str:
        """Identify one backup volume (what is physically attached)."""
        return candidate.get("fs_uuid") or f"dev:{candidate['device_name']}"

    @staticmethod
    def _manifest_set_id(manifest: Dict[str, Any]) -> Optional[str]:
        """The backup set a volume belongs to, as stamped by the writer.

        Volumes written before backup sets existed carry no stamp, so they are
        each their own set - which is exactly what they are.
        """
        for ds in manifest.get("datasets") or []:
            for run in ds.get("backups") or []:
                if run.get("set_id"):
                    return str(run["set_id"])
        return None

    @staticmethod
    def _merge_manifests(manifests: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Combine a set's volumes into one logical manifest.

        A set is a chain spread over its volumes, so the set's contents are the
        union of its volumes' - datasets matched by name, runs matched by
        stream file, so re-reading a volume twice cannot duplicate entries.
        """
        merged: Dict[str, Any] = {
            "datasets": [],
            "pools": [],
            "config_backups": [],
            "media": {},
        }
        updated = ""
        group = None
        for manifest in manifests:
            if not manifest:
                continue
            for key in ("manifest_version", "nazman_version", "created_at"):
                if merged.get(key) is None and manifest.get(key) is not None:
                    merged[key] = manifest[key]
            updated = max(updated, manifest.get("updated_at") or "")
            if group is None and manifest.get("group"):
                group = manifest["group"]
            if manifest.get("media") and not merged["media"]:
                merged["media"] = manifest["media"]
            for pool in manifest.get("pools") or []:
                if pool.get("name") and not any(
                    p.get("name") == pool.get("name") for p in merged["pools"]
                ):
                    merged["pools"].append(pool)
            for config in manifest.get("config_backups") or []:
                if not any(c.get("id") == config.get("id") for c in merged["config_backups"]):
                    merged["config_backups"].append(config)
            for ds in manifest.get("datasets") or []:
                target = next(
                    (d for d in merged["datasets"] if d.get("name") == ds.get("name")), None,
                )
                if target is None:
                    target = {
                        "name": ds.get("name"), "pool": ds.get("pool"),
                        "properties": dict(ds.get("properties") or {}),
                        "mountpoint": ds.get("mountpoint"), "backups": [],
                    }
                    merged["datasets"].append(target)
                for key in ("pool", "mountpoint"):
                    if ds.get(key):
                        target[key] = ds[key]
                if ds.get("properties"):
                    target["properties"] = dict(ds["properties"])
                runs = target.setdefault("backups", [])
                for run in ds.get("backups") or []:
                    runs[:] = [r for r in runs if r.get("stream_file") != run.get("stream_file")]
                    runs.append(run)
        merged["updated_at"] = updated or None
        if group:
            merged["group"] = group
        for ds in merged["datasets"]:
            ds["backups"].sort(key=lambda r: r.get("created_at") or "")
        return merged

    async def _scan_volumes(self, db: Session) -> List[Dict[str, Any]]:
        """Every attached volume with a readable backup manifest."""
        found = []
        for candidate in await self.list_candidates(db):
            manifest = await self._read_manifest(candidate)
            if manifest is None:
                continue
            found.append({
                "candidate": candidate,
                "manifest": manifest,
                "volume_id": self._volume_id(candidate),
                "set_id": self._manifest_set_id(manifest) or self._volume_id(candidate),
            })
        return found

    async def discover_backup_sets(self, db: Session) -> List[Dict[str, Any]]:
        """Scan all disks and return a menu of available backup sets.

        A set may span several volumes, so volumes are grouped by the set
        stamp their writer left; the menu shows one entry per set with the
        media it needs.
        """
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for volume in await self._scan_volumes(db):
            groups.setdefault(volume["set_id"], []).append(volume)

        sets = []
        for set_id, volumes in groups.items():
            merged = self._merge_manifests([v["manifest"] for v in volumes])
            summary = bm.manifest_summary(merged)
            volume_views = []
            for v in volumes:
                candidate = v["candidate"]
                volume_views.append({
                    "volume_id": v["volume_id"],
                    "device": candidate["device"],
                    "by_id": candidate.get("by_id"),
                    "fstype": candidate.get("fstype"),
                    "label": candidate.get("label"),
                    "fs_uuid": candidate.get("fs_uuid"),
                })
            sets.append({
                **summary,
                "set_id": set_id,
                "group": merged.get("group"),
                "volume_count": len(volume_views),
                "volumes": volume_views,
                # Kept for the single-volume case, where it is the set itself.
                "fs_uuid": volume_views[0]["fs_uuid"] if len(volume_views) == 1 else None,
                "label": volume_views[0].get("label"),
                "media_fs_uuids": sorted({
                    v["volume_id"] for v in volumes
                }) or summary.get("media_fs_uuids") or [],
            })
        # Within one group ring, every set carrying the newest updates is the
        # currently "latest" backup; ties (e.g. after replication) are all marked.
        newest_at: Dict[str, str] = {}
        for s in sets:
            grp = s.get("group")
            ts = s.get("updated_at") or ""
            if grp and ts and ts > newest_at.get(grp, ""):
                newest_at[grp] = ts
        for s in sets:
            grp = s.get("group")
            ts = s.get("updated_at") or ""
            s["is_latest"] = bool(grp and ts and ts == newest_at.get(grp))
        return sets

    async def _volumes_of(self, db: Session, set_id: str) -> List[Dict[str, Any]]:
        """The attached volumes that make up a set."""
        volumes = [v for v in await self._scan_volumes(db) if v["set_id"] == set_id]
        if not volumes:
            raise BackupError(f"Backup set {set_id} not found on any attached disk")
        return volumes

    async def _find_candidate(self, db: Session, set_id: str) -> Dict[str, Any]:
        for candidate in await self.list_candidates(db):
            if self._volume_id(candidate) == set_id:
                return candidate
        for volume in await self._scan_volumes(db):
            if volume["set_id"] == set_id:
                return volume["candidate"]
        raise BackupError(f"Backup set {set_id} not found on any attached disk")

    async def _manifest_for(self, db: Session, set_id: str) -> Dict[str, Any]:
        """The set's merged manifest, across every volume that belongs to it."""
        volumes = await self._volumes_of(db, set_id)
        manifest = self._merge_manifests([v["manifest"] for v in volumes])
        manifest["_volumes"] = volumes
        return manifest

    async def get_backup_set(self, db: Session, set_id: str) -> Dict[str, Any]:
        """Full detail of one backup set (pools, datasets, config, media)."""
        manifest = await self._manifest_for(db, set_id)
        volumes = manifest.pop("_volumes", [])
        summary = bm.manifest_summary(manifest)
        return {
            "set_id": set_id,
            "volume_count": len(volumes),
            "volumes": [
                {
                    "volume_id": v["volume_id"],
                    "device": v["candidate"]["device"],
                    "by_id": v["candidate"].get("by_id"),
                    "label": v["candidate"].get("label"),
                    "fs_uuid": v["candidate"].get("fs_uuid"),
                }
                for v in volumes
            ],
            "summary": summary,
            "manifest": manifest,
            "required_media": await self.required_media(db, set_id),
        }

    # ── Pool recreation ─────────────────────────────────────────────────
    async def _attached_disks(self, db: Session, excluded: set = ()) -> List[Dict[str, Any]]:
        """Attached, assignable whole disks with their labeled partitions.

        ``excluded`` holds device identities (by-id paths or base names) that
        must never be offered, e.g. the backup media of the set being restored.
        Disks already claimed by an imported pool are dropped too, mirroring
        :meth:`list_candidates`.
        """
        if self.disk is not None:
            await self.disk.sync_disks_to_database(db)
        media = {ident: "media" for ident in excluded if ident}
        pool_members = {}
        if self.zfs is not None:
            try:
                pool_members = await self.zfs.get_pool_members()
            except Exception:
                pool_members = {}
        attached = []
        for d in db.query(Disk).all():
            if d.is_os_disk:
                continue
            live_path = get_device_path(d)
            if self._in_pool(media, live_path, d.by_id) or self._in_pool(pool_members, live_path, d.by_id):
                continue
            partitions = []
            if live_path:
                base_name = live_path.removeprefix("/dev/")
                for p in (await read_slot_uuids([live_path])).get(live_path, {}).get("partitions", []):
                    if not p.get("slot_uuid"):
                        continue
                    partitions.append({
                        "number": self._partition_number(p["name"], base_name),
                        "slot_uuid": p["slot_uuid"],
                        "device_path": partition_by_id(d.by_id, self._partition_number(p["name"], base_name)) if d.by_id else None,
                        "size_bytes": p.get("size_bytes", 0),
                    })
            attached.append({
                "disk_id": d.id,
                "by_id": d.by_id,
                "serial": d.serial,
                "size_bytes": d.size_bytes,
                "model": d.model,
                "device_path": live_path,
                "present": bool(live_path),
                "is_os_disk": d.is_os_disk,
                "partitions": partitions,
            })
        return attached

    async def plan_pool_mapping(self, db: Session, set_id: str, pool_name: str) -> Dict[str, Any]:
        """Suggest which attached disk fills each recorded vdev slot.

        Matching is by-id first, then serial (a disk moved to a new controller
        keeps its serial).  Unmatched slots are returned for manual assignment.
        The set's own backup media is never offered, and a recorded partition
        slot that already exists on its matched disk is reported so the wizard
        can use it directly instead of wiping and repartitioning.
        """
        manifest = await self._manifest_for(db, set_id)
        pool = next((p for p in manifest.get("pools", []) if p.get("name") == pool_name), None)
        if pool is None:
            raise ValidationError(f"Pool '{pool_name}' not found in backup set")

        excluded = {
            ident
            for volume in manifest.get("_volumes") or []
            for ident in (
                (volume.get("candidate") or {}).get("base_by_id"),
                (volume.get("candidate") or {}).get("by_id"),
                (volume.get("candidate") or {}).get("base_name"),
                (volume.get("candidate") or {}).get("device_name"),
            )
            if ident
        }
        attached = await self._attached_disks(db, excluded)
        used: set = set()
        vdevs = []
        for vdev in pool.get("vdevs", []):
            devices = []
            for spec in vdev.get("devices", []):
                match = self._match_disk(spec, attached, used)
                if match:
                    used.add(match["disk_id"])
                matched_partition = None
                if match and spec.get("slot_uuid"):
                    matched_partition = next(
                        (p for p in match.get("partitions", []) if p["slot_uuid"] == spec["slot_uuid"]),
                        None,
                    )
                size_ok = bool(match and (not spec.get("size_bytes")
                            or match["size_bytes"] >= spec["size_bytes"]))
                if matched_partition and spec.get("partition_size_bytes"):
                    size_ok = matched_partition["size_bytes"] >= spec["partition_size_bytes"]
                devices.append({
                    "spec": spec,
                    "matched_disk_id": match["disk_id"] if match else None,
                    "match": "by_id" if match and match["by_id"] == spec.get("by_id")
                    else ("serial" if match else None),
                    "size_ok": size_ok,
                    "matched_partition": matched_partition,
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
        volumes = manifest.get("_volumes") or []
        connected = {v["volume_id"] for v in volumes}
        pools = await self.zfs.list_pool_names(db) if self.zfs is not None else []
        plan = []
        for ds in manifest.get("datasets", []):
            backups = ds.get("backups") or []
            if not backups:
                continue
            source = ds.get("name")
            leaf = source.split("/", 1)[1] if "/" in source else source
            suggested = ds.get("pool") if ds.get("pool") in pools else None
            # A set's chain can span several volumes, so the plan says which
            # ones this dataset needs and whether they are all attached.
            chain = self._chain(backups)
            media = sorted({
                b.get("media_fs_uuid") for b in chain if b.get("media_fs_uuid")
            })
            plan.append({
                "source_dataset": source,
                "leaf": leaf,
                "suggested_pool": suggested,
                "pools": pools,
                "enabled": True,
                "media_fs_uuids": media,
                "media_missing": [m for m in media if m not in connected],
                "spans_volumes": len(media) > 1,
                "chain": chain,
            })
        return plan

    # ── Media grouping ──────────────────────────────────────────────────
    async def required_media(self, db: Session, set_id: str) -> List[Dict[str, Any]]:
        """Which volumes of the set hold the streams, and are they attached?"""
        manifest = await self._manifest_for(db, set_id)
        volumes = manifest.get("_volumes") or []
        present = {v["volume_id"]: v for v in volumes}
        labels = {v["volume_id"]: v["candidate"].get("label") for v in volumes}
        groups: Dict[str, Dict[str, Any]] = {}
        for ds in manifest.get("datasets", []):
            for b in self._chain(ds.get("backups") or []):
                key = b.get("media_fs_uuid") or ""
                group = groups.setdefault(key, {
                    "media_fs_uuid": key or None,
                    "label": labels.get(key) or b.get("media_label"),
                    "datasets": [],
                    "connected": key in present if key else False,
                })
                if ds.get("name") not in group["datasets"]:
                    group["datasets"].append(ds.get("name"))
        return list(groups.values())

    async def _mount_volumes(self, db: Session, set_id: str) -> tuple:
        """Mount every volume of a set read-only, so a chain can cross them."""
        volumes = await self._volumes_of(db, set_id)
        mounted: List[tuple] = []
        try:
            for volume in volumes:
                mountpoint = await self._mount_readonly(volume["candidate"])
                if mountpoint is not None:
                    mounted.append((volume["candidate"], mountpoint))
        except Exception:
            for _candidate, mountpoint in mounted:
                await self._unmount(mountpoint)
            raise
        if not mounted:
            raise BackupError(f"Could not mount any volume of backup set {set_id}")
        return mounted

    async def _unmount_all(self, mounted: List[tuple]) -> None:
        for _candidate, mountpoint in mounted:
            await self._unmount(mountpoint)

    async def _mount_for_set(self, db: Session, set_id: str) -> tuple:
        """Mount the set's first volume (config bundles live on one volume)."""
        mounted = await self._mount_volumes(db, set_id)
        candidate, mountpoint = mounted[0]
        await self._unmount_all(mounted[1:])
        return candidate, mountpoint

    async def restore_datasets(
        self, db: Session, set_id: str, selections: List[Dict[str, Any]],
        media_fs_uuid: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Replay each selected dataset's latest chain into its target pool.

        ``selections`` entries are ``{source_dataset, target_pool, enabled}``.
        Every volume of the set is mounted, because a set's chain is spread
        across its disks in the order they filled up; each stream is read from
        whichever volume the writer put it on.  When ``media_fs_uuid`` is given
        only datasets whose chain lives entirely on that volume are restored
        (used to prompt disk-by-disk).
        """
        if self.zfs_backup is None:
            raise BackupError("ZFS backup manager unavailable")
        manifest = await self._manifest_for(db, set_id)
        manifest.pop("_volumes", None)

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
            if media_fs_uuid is not None and chain_media != {media_fs_uuid}:
                # Restricting to one volume: only what fits entirely on it.
                continue
            chosen.append((source, target, chain))

        # Parents before children, so a dataset's parent pool/dataset exists
        # before the child is received into it.
        chosen.sort(key=lambda item: item[0].count("/"))

        mounted = await self._mount_volumes(db, set_id)
        results = []
        try:
            for source, target, chain in chosen:
                results.append(
                    await self._restore_one(source, target, chain, mounted)
                )
        finally:
            await self._unmount_all(mounted)
        return {"set_id": set_id, "results": results}

    async def _restore_one(
        self, source: str, target: str, chain: List[Dict[str, Any]],
        mounted: List[tuple],
    ) -> Dict[str, Any]:
        exists = await self.zfs.dataset_exists(target) if self.zfs is not None else False
        entry: Dict[str, Any] = {"source": source, "target": target, "streams": []}
        try:
            for b in chain:
                stream = self._locate_stream(b, mounted)
                if stream is None:
                    missing = b.get("media_fs_uuid") or "its backup volume"
                    entry["status"] = "failed"
                    entry["error"] = (
                        f"Stream {b.get('stream_file')} is not connected "
                        f"(expected on {missing})"
                    )
                    return entry
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

    @staticmethod
    def _locate_stream(run: Dict[str, Any], mounted: List[tuple]) -> Optional[Path]:
        """Find a stream on whichever mounted volume of the set holds it.

        The recorded media UUID is the fast path; falling back to a scan of
        every volume means a chain still restores when a volume's identity
        changed (e.g. the media was re-imaged).
        """
        relative = run.get("stream_file") or ""
        preferred = run.get("media_fs_uuid")
        for candidate, mountpoint in mounted:
            if preferred and candidate.get("fs_uuid") != preferred:
                continue
            stream = mountpoint / relative
            if stream.exists():
                return stream
        for _candidate, mountpoint in mounted:
            stream = mountpoint / relative
            if stream.exists():
                return stream
        return None

    # ── Configuration restore ───────────────────────────────────────────
    async def restore_configuration(self, db: Session, set_id: str, config_id: str) -> Dict[str, Any]:
        manifest = await self._manifest_for(db, set_id)
        manifest.pop("_volumes", None)
        entry = next((e for e in manifest.get("config_backups", []) if e.get("id") == config_id), None)
        if entry is None:
            raise ValidationError(f"Config backup {config_id} not found in set")
        if self.backup is None:
            raise BackupError("Backup manager unavailable")

        # Each volume carries its own bundle, so the one being restored may be
        # on any volume of the set.
        mounted = await self._mount_volumes(db, set_id)
        try:
            relative = entry.get("path") or ""
            bundle = None
            for _candidate, mountpoint in mounted:
                if (mountpoint / relative).exists():
                    bundle = mountpoint / relative
                    break
            if bundle is None:
                raise BackupError(
                    f"Config backup {config_id} is not on any connected volume of this set"
                )
            await self.backup.restore_configuration_bundle(db, bundle)
        finally:
            await self._unmount_all(mounted)
        return {"set_id": set_id, "config_id": config_id, "restored": True}

    # ── Post-restore adoption (independent options) ─────────────────────
    async def adopt_media(self, db: Session, set_id: str) -> Dict[str, Any]:
        """Re-register the set's backup volumes as declared backup disks.

        A set's volumes form one chain, so adopting the set adopts all of its
        attached volumes; the ones still missing are reported rather than
        silently skipped, because the chain cannot be used without them.
        """
        volumes = await self._volumes_of(db, set_id)
        adopted: List[int] = []
        already: List[int] = []
        missing: List[str] = []
        for volume in volumes:
            candidate = volume["candidate"]
            if not candidate.get("fs_uuid"):
                missing.append(f"{candidate.get('device')} (no filesystem UUID)")
                continue
            existing = db.query(BackupDisk).filter(
                BackupDisk.fs_uuid == candidate["fs_uuid"],
            ).first()
            if existing:
                already.append(existing.id)
                continue
            base_by_id = candidate.get("base_by_id")
            disk = db.query(Disk).filter(Disk.by_id == base_by_id).first() if base_by_id else None
            if disk is None:
                missing.append(f"{candidate.get('label') or candidate.get('device')} "
                               "(not matched to an attached disk)")
                continue

            from ..config import get_settings
            mount_base = Path(get_settings().backup_mount_base) / candidate["fs_uuid"]
            mount_point = ensure_dir(mount_base)
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
            adopted.append(rec.id)
        return {
            "adopted": bool(adopted),
            "backup_disk_ids": adopted,
            "already_declared": already,
            "missing": missing,
            "message": (
                f"Declared {len(adopted)} volume(s); {len(already)} already declared"
                + (f"; {len(missing)} could not be adopted" if missing else "")
            ),
        }

    async def rebuild_schedules(self, db: Session, set_id: str) -> Dict[str, Any]:
        """Reconcile the restored backup groups into scheduler jobs.

        A restored database's groups keep their crons, so the jobs are rebuilt
        from them rather than from the media that was just read.
        """
        from ..models.backup_zfs import BackupGroup
        from .backup_group_service import BackupGroupService

        groups = db.query(BackupGroup).count()
        service = BackupGroupService(
            zfs_backup=self.zfs_backup, scheduler=self.scheduler,
        )
        await service.sync_scheduled_tasks(db)
        return {"set_id": set_id, "backup_disks": len(db.query(BackupDisk).all()),
                "backup_groups": groups, "schedules_synced": True}
