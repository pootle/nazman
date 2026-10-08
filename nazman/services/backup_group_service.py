"""Backup group orchestration: groups, sets, disks, and the rotation cycle.

A backup group owns a fixed list of datasets and a cycle of backup sets; each
set is a chain of one or more disks used in order.  A trigger (cron or the
Full/Incremental buttons) writes every dataset in the group to the group's
active set, then moves the group to the next set so the following trigger
starts there.

This is a service because it spans several managers: it asks
``ZfsBackupManager`` to mount and write datasets, ``BackupManager`` to capture
the configuration bundle, the ``SchedulerManager`` for cron, and the
``AlertManager`` to tell the user when a set has run out of usable media.
None of those managers import each other, and none import this.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from ..models.backup_zfs import (
    BackupDisk, BackupGroup, BackupGroupDataset, BackupRun,
    BackupSession, BackupSet,
)
from ..models.scheduler import ScheduledTask, TaskType
from ..utils.exceptions import (
    NAZManError, NotFoundError, ValidationError,
)
from ..utils.notification_store import notification_store
from ..utils.timing import elapsed_ms as _elapsed_ms
from ..utils.validation import validate_dataset_name, validate_schedule

logger = logging.getLogger(__name__)

# Statuses a session can end in.
SESSION_RUNNING = "running"
SESSION_SUCCESS = "success"
SESSION_PARTIAL = "partial"
SESSION_FAILED = "failed"
SESSION_NEEDS_DISK = "needs_disk"


class BackupGroupService:
    """Backup groups: dataset membership, set/disk layout, and the run cycle."""

    def __init__(self, zfs_backup=None, backup=None, scheduler=None, alerter=None):
        self.zfs_backup = zfs_backup
        self.backup = backup
        self.scheduler = scheduler
        self.alerter = alerter
        self._group_locks: Dict[int, asyncio.Lock] = {}
        self._session_tasks: set = set()

    # ── Lookups ─────────────────────────────────────────────────────────
    def get_group(self, db: Session, group_id: int) -> BackupGroup:
        group = db.query(BackupGroup).filter(BackupGroup.id == group_id).first()
        if not group:
            raise NotFoundError(f"Backup group {group_id} not found")
        return group

    def get_set(self, db: Session, set_id: int) -> BackupSet:
        bset = db.query(BackupSet).filter(BackupSet.id == set_id).first()
        if not bset:
            raise NotFoundError(f"Backup set {set_id} not found")
        return bset

    def get_disk(self, db: Session, backup_disk_id: int) -> BackupDisk:
        rec = db.query(BackupDisk).filter(BackupDisk.id == backup_disk_id).first()
        if not rec:
            raise BackupDiskNotFoundError("Backup disk not found")
        return rec

    def group_datasets(self, db: Session, group_id: int) -> List[str]:
        """The group's dataset list, in the order it was declared."""
        return [
            row.dataset_name
            for row in (
                db.query(BackupGroupDataset)
                .filter(BackupGroupDataset.group_id == group_id)
                .order_by(BackupGroupDataset.id)
                .all()
            )
        ]

    def group_sets(self, db: Session, group_id: int) -> List[BackupSet]:
        """The group's sets in cycle order."""
        return (
            db.query(BackupSet)
            .filter(BackupSet.group_id == group_id)
            .order_by(BackupSet.position)
            .all()
        )

    # ── Group CRUD ──────────────────────────────────────────────────────
    @staticmethod
    def _check_crons(full_cron: Optional[str], incremental_cron: Optional[str]) -> None:
        """Both crons are optional (a group can be manual-only), but a cron
        that is set has to parse."""
        for label, cron in (("full", full_cron), ("incremental", incremental_cron)):
            if cron:
                try:
                    validate_schedule(cron)
                except ValidationError as e:
                    raise ValidationError(f"Invalid {label} schedule: {e}")

    async def create_group(
        self, db: Session, name: str, dataset_names: Optional[List[str]] = None,
        full_cron: Optional[str] = None, incremental_cron: Optional[str] = None,
        enabled: bool = True, copies: int = 1, recycle_full_disks: bool = False,
    ) -> BackupGroup:
        """Create a backup group and its dataset list.

        Sets are added separately, which is what the setup flow wants: name the
        group and its datasets first, then allocate media to its sets.
        """
        name = (name or "").strip()
        if not name:
            raise ValidationError("Group name is required")
        if len(name) > 100:
            raise ValidationError("Group name must be 100 characters or fewer")
        if db.query(BackupGroup).filter(BackupGroup.name == name).first():
            raise ValidationError(f"A backup group named '{name}' already exists")
        self._check_crons(full_cron, incremental_cron)
        if copies < 1:
            raise ValidationError("copies must be at least 1")

        group = BackupGroup(
            name=name, full_cron=full_cron,
            incremental_cron=incremental_cron, enabled=enabled,
            copies=copies, recycle_full_disks=recycle_full_disks,
        )
        db.add(group)
        db.commit()
        db.refresh(group)
        for dataset in dataset_names or []:
            self._link_dataset(db, group, dataset)
        db.commit()
        await self.sync_scheduled_tasks(db)
        return group

    async def update_group(
        self, db: Session, group_id: int, name: Optional[str] = None,
        full_cron: Optional[str] = None, incremental_cron: Optional[str] = None,
        enabled: Optional[bool] = None, copies: Optional[int] = None,
        recycle_full_disks: Optional[bool] = None,
    ) -> BackupGroup:
        group = self.get_group(db, group_id)
        if name is not None:
            name = name.strip()
            if not name:
                raise ValidationError("Group name is required")
            other = db.query(BackupGroup).filter(
                BackupGroup.name == name, BackupGroup.id != group_id,
            ).first()
            if other:
                raise ValidationError(f"A backup group named '{name}' already exists")
            group.name = name
        if full_cron is not None or incremental_cron is not None:
            self._check_crons(
                full_cron if full_cron is not None else group.full_cron,
                incremental_cron if incremental_cron is not None else group.incremental_cron,
            )
        if full_cron is not None:
            group.full_cron = full_cron or None
        if incremental_cron is not None:
            group.incremental_cron = incremental_cron or None
        if enabled is not None:
            group.enabled = enabled
        if copies is not None:
            if copies < 1:
                raise ValidationError("copies must be at least 1")
            group.copies = copies
        if recycle_full_disks is not None:
            group.recycle_full_disks = recycle_full_disks
        db.commit()
        await self.sync_scheduled_tasks(db)
        return group

    async def delete_group(self, db: Session, group_id: int) -> Dict[str, Any]:
        """Delete a group and its sets.

        Declared disks survive: a disk only loses its set membership, so the
        media is still described (and still restorable) and can be filed under
        a new group's set.  The group must not be mid-session, or the rotation
        pointer it owns would be pulled out from under a running write.
        """
        group = self.get_group(db, group_id)
        if await self.is_session_running(db, group_id):
            raise ValidationError("A backup is running for this group; wait for it to finish")
        for bset in self.group_sets(db, group_id):
            db.query(BackupDisk).filter(BackupDisk.backup_set_id == bset.id).update(
                {BackupDisk.backup_set_id: None}, synchronize_session=False,
            )
        db.delete(group)
        db.commit()
        await self.sync_scheduled_tasks(db)
        return {"deleted": group_id}

    # ── Datasets ────────────────────────────────────────────────────────
    def _link_dataset(self, db: Session, group: BackupGroup, dataset_name: str) -> BackupGroupDataset:
        name = validate_dataset_name(dataset_name)
        existing = db.query(BackupGroupDataset).filter(
            BackupGroupDataset.group_id == group.id,
            BackupGroupDataset.dataset_name == name,
        ).first()
        if existing:
            return existing
        row = BackupGroupDataset(group_id=group.id, dataset_name=name)
        db.add(row)
        return row

    async def add_dataset(self, db: Session, group_id: int, dataset_name: str) -> Dict[str, Any]:
        group = self.get_group(db, group_id)
        self._link_dataset(db, group, dataset_name)
        db.commit()
        return {"group_id": group_id, "datasets": self.group_datasets(db, group_id)}

    async def remove_dataset(self, db: Session, group_id: int, dataset_name: str) -> Dict[str, Any]:
        self.get_group(db, group_id)
        name = validate_dataset_name(dataset_name)
        db.query(BackupGroupDataset).filter(
            BackupGroupDataset.group_id == group_id,
            BackupGroupDataset.dataset_name == name,
        ).delete()
        db.commit()
        return {"group_id": group_id, "datasets": self.group_datasets(db, group_id)}

    # ── Sets ────────────────────────────────────────────────────────────
    async def create_set(
        self, db: Session, group_id: int, label: Optional[str] = None,
        position: Optional[int] = None,
    ) -> BackupSet:
        """Add a rotation slot to a group, at the end of the cycle by default."""
        group = self.get_group(db, group_id)
        existing = self.group_sets(db, group_id)
        if position is None:
            position = (existing[-1].position + 1) if existing else 0
        position = int(position)
        if position < 0:
            raise ValidationError("Set position cannot be negative")
        if any(s.position == position for s in existing):
            raise ValidationError(f"Set position {position} is already used in this group")
        bset = BackupSet(group_id=group.id, position=position, label=(label or None))
        db.add(bset)
        db.commit()
        self._renumber(db, group_id)
        if group.active_set_id is None:
            group.active_set_id = bset.id
            db.commit()
        return self.get_set(db, bset.id)

    async def update_set(
        self, db: Session, set_id: int, label: Optional[str] = None,
        position: Optional[int] = None,
    ) -> BackupSet:
        bset = self.get_set(db, set_id)
        if label is not None:
            bset.label = label.strip() or None
        if position is not None:
            position = int(position)
            if position < 0:
                raise ValidationError("Set position cannot be negative")
            if any(s.position == position for s in self.group_sets(db, bset.group_id) if s.id != bset.id):
                raise ValidationError(f"Set position {position} is already used in this group")
            bset.position = position
        db.commit()
        self._renumber(db, bset.group_id)
        return self.get_set(db, set_id)

    async def delete_set(self, db: Session, set_id: int) -> Dict[str, Any]:
        """Remove a rotation slot, keeping its disks and their data.

        The set's runs, sessions and chain go with it: they only mean anything
        relative to a set.  The disks stay declared and unassigned so they can
        be filed under a new set, and their media is still restorable.
        """
        bset = self.get_set(db, set_id)
        group_id = bset.group_id
        if await self.is_session_running(db, group_id):
            raise ValidationError("A backup is running for this group; wait for it to finish")
        group = self.get_group(db, group_id)
        db.query(BackupRun).filter(BackupRun.backup_set_id == set_id).delete()
        db.query(BackupDisk).filter(BackupDisk.backup_set_id == set_id).update(
            {BackupDisk.backup_set_id: None}, synchronize_session=False,
        )
        db.delete(bset)
        db.commit()
        self._renumber(db, group_id)
        group = self.get_group(db, group_id)
        remaining = self.group_sets(db, group_id)
        if group.active_set_id == set_id or group.active_set_id is None:
            group.active_set_id = remaining[0].id if remaining else None
        group.needs_disk = False
        db.commit()
        return {"deleted": set_id, "active_set_id": group.active_set_id}

    async def activate_set(self, db: Session, set_id: int) -> Dict[str, Any]:
        """Make this the set the group's next trigger will write to."""
        bset = self.get_set(db, set_id)
        group = self.get_group(db, bset.group_id)
        group.active_set_id = bset.id
        group.needs_disk = False
        db.commit()
        return {"group_id": group.id, "active_set_id": group.active_set_id}

    @staticmethod
    def _renumber(db: Session, group_id: int) -> None:
        """Make a group's set positions a dense 0..n-1 sequence in order."""
        sets = (
            db.query(BackupSet)
            .filter(BackupSet.group_id == group_id)
            .order_by(BackupSet.position, BackupSet.id)
            .all()
        )
        for index, bset in enumerate(sets):
            if bset.position != index:
                bset.position = index
        db.commit()

    # ── Disks within a set ──────────────────────────────────────────────
    async def attach_disk(self, db: Session, set_id: int, backup_disk_id: int) -> Dict[str, Any]:
        """File an already-declared disk into a set, activating it if needed.

        A disk holds one chain, so it can only belong to one set; moving it
        between sets is allowed but breaks the set it leaves, which the caller
        is warned about rather than blocked from.
        """
        bset = self.get_set(db, set_id)
        rec = self.get_disk(db, backup_disk_id)
        previous = None
        if rec.backup_set_id is not None and rec.backup_set_id != bset.id:
            previous = db.query(BackupSet).filter(
                BackupSet.id == rec.backup_set_id,
            ).first()
            if previous is not None and previous.last_used_at is not None:
                logger.warning(
                    "disk %s moved out of used set %s; that set's chain is now broken",
                    backup_disk_id, previous.id,
                )
        rec.backup_set_id = bset.id
        if bset.active_disk_id is None:
            bset.active_disk_id = rec.id
        db.commit()
        # Realign only after the move is committed, so the set it left no
        # longer counts this disk among its own.
        if previous is not None:
            self._realign_set(db, previous)
        return await self.describe_set(db, bset.id)

    async def detach_disk(self, db: Session, set_id: int, backup_disk_id: int) -> Dict[str, Any]:
        """Remove a disk from a set (and from NAZMan's declared targets).

        Datasets whose chain still points at a snapshot on this disk cannot be
        restored from the remaining media, so the response says so instead of
        letting it pass silently.
        """
        bset = self.get_set(db, set_id)
        rec = db.query(BackupDisk).filter(
            BackupDisk.id == backup_disk_id, BackupDisk.backup_set_id == bset.id,
        ).first()
        if not rec:
            raise NotFoundError("That disk is not in this backup set")
        if await self.is_session_running(db, bset.group_id):
            raise ValidationError("A backup is running for this group; wait for it to finish")
        broken = self.chain_dependencies(db, bset.id, backup_disk_id)
        rec.backup_set_id = None
        db.commit()
        await self._deregister_disk(db, backup_disk_id)
        return {
            "set_id": bset.id,
            "removed": backup_disk_id,
            "broken_datasets": broken,
            "message": (
                "Disk removed. These datasets can no longer be restored from this "
                "set, because their chain referenced snapshots on it: "
                + ", ".join(broken)
            ) if broken else "Disk removed from the set",
        }

    async def _deregister_disk(self, db: Session, backup_disk_id: int) -> None:
        """Undeclare a disk (data on the medium is left alone)."""
        if self.zfs_backup is None:
            return
        await self.zfs_backup.deregister_backup_disk(db, backup_disk_id)

    def chain_dependencies(self, db: Session, set_id: int, backup_disk_id: int) -> List[str]:
        """Datasets in the set whose chain references snapshots on this disk."""
        runs = (
            db.query(BackupRun)
            .filter(BackupRun.backup_set_id == set_id)
            .order_by(BackupRun.id)
            .all()
        )
        streams_on_disk: set = set()
        chain: List[str] = []
        for run in runs:
            if run.backup_disk_id == backup_disk_id and run.status == "success":
                streams_on_disk.add(run.snapshot)
                if run.dataset_name not in chain:
                    chain.append(run.dataset_name)
        for run in runs:
            if run.status != "success" or run.backup_disk_id == backup_disk_id:
                continue
            if run.base_snapshot in streams_on_disk or run.full_anchor in streams_on_disk:
                if run.dataset_name not in chain:
                    chain.append(run.dataset_name)
        return chain

    async def advance_disk(self, db: Session, set_id: int) -> Dict[str, Any]:
        """Move the set to its next disk by hand (e.g. after swapping media)."""
        bset = self.get_set(db, set_id)
        if await self.is_session_running(db, bset.group_id):
            raise ValidationError("A backup is running for this group; wait for it to finish")
        disks = self.set_disks(db, set_id)
        if not disks:
            raise ValidationError("This backup set has no disks")
        order = [d.id for d in disks]
        if bset.active_disk_id in order:
            index = (order.index(bset.active_disk_id) + 1) % len(order)
        else:
            index = 0
        bset.active_disk_id = order[index]
        db.commit()
        return await self.describe_set(db, set_id)

    async def set_active_disk(self, db: Session, set_id: int, backup_disk_id: int) -> Dict[str, Any]:
        """Choose a specific disk in the set as the one the next run writes to."""
        bset = self.get_set(db, set_id)
        if await self.is_session_running(db, bset.group_id):
            raise ValidationError("A backup is running for this group; wait for it to finish")
        rec = db.query(BackupDisk).filter(
            BackupDisk.id == backup_disk_id,
            BackupDisk.backup_set_id == bset.id,
        ).first()
        if not rec:
            raise NotFoundError("That disk is not in this backup set")
        bset.active_disk_id = rec.id
        db.commit()
        return await self.describe_set(db, bset.id)

    def set_disks(self, db: Session, set_id: int) -> List[BackupDisk]:
        """A set's disks in the order they are used."""
        return (
            db.query(BackupDisk)
            .filter(BackupDisk.backup_set_id == set_id)
            .order_by(BackupDisk.id)
            .all()
        )

    def _realign_set(self, db: Session, bset: Optional[BackupSet]) -> None:
        """After losing a disk, point the set at whatever is left (or nowhere)."""
        if bset is None:
            return
        remaining = self.set_disks(db, bset.id)
        if bset.active_disk_id is not None and not any(d.id == bset.active_disk_id for d in remaining):
            bset.active_disk_id = remaining[0].id if remaining else None
        if not remaining and bset.group is not None:
            bset.group.needs_disk = True
        db.commit()

    # ── Presentation ────────────────────────────────────────────────────
    async def list_groups(self, db: Session) -> List[Dict[str, Any]]:
        """Every group with its datasets, sets, and the live state of each disk."""
        out = []
        for group in db.query(BackupGroup).order_by(BackupGroup.id).all():
            out.append(await self.describe_group(db, group.id))
        return out

    async def describe_group(self, db: Session, group_id: int) -> Dict[str, Any]:
        group = self.get_group(db, group_id)
        sets = self.group_sets(db, group_id)
        set_views = [await self.describe_set(db, bset.id) for bset in sets]
        datasets = self.group_datasets(db, group_id)
        last = (
            db.query(BackupSession)
            .filter(BackupSession.group_id == group_id)
            .order_by(BackupSession.id.desc())
            .first()
        )
        active = next((s for s in set_views if s["id"] == group.active_set_id), None)
        return {
            "id": group.id,
            "name": group.name,
            "full_cron": group.full_cron,
            "incremental_cron": group.incremental_cron,
            "enabled": bool(group.enabled),
            "needs_disk": bool(group.needs_disk),
            "copies": group.copies or 1,
            "recycle_full_disks": bool(group.recycle_full_disks),
            "active_set_id": group.active_set_id,
            "last_session_at": group.last_session_at,
            "datasets": datasets,
            "dataset_count": len(datasets),
            "sets": set_views,
            "set_count": len(set_views),
            "active_set": active,
            "ready": bool(datasets) and bool(sets) and all(
                s["disk_count"] for s in set_views
            ),
            "blocking_reason": self._blocking_reason(group, datasets, sets),
            "last_session": self._session_view(last) if last else None,
        }

    @staticmethod
    def _blocking_reason(group: BackupGroup, datasets: List[str], sets: List[BackupSet]) -> Optional[str]:
        if not datasets:
            return "Add at least one dataset to this group"
        if not sets:
            return "Add a backup set to this group"
        return None

    async def describe_set(self, db: Session, set_id: int) -> Dict[str, Any]:
        """One set with each of its disks and its live capacity/availability."""
        bset = self.get_set(db, set_id)
        disks = self.set_disks(db, set_id)
        disk_views = []
        for rec in disks:
            view = {"id": rec.id, "label": rec.label, "fs_uuid": rec.fs_uuid,
                    "mount_point": rec.mount_point, "is_active": rec.id == bset.active_disk_id}
            if self.zfs_backup is not None:
                view.update(await self.zfs_backup.serialize_now(rec))
            disk_views.append(view)
        total = sum(d.get("total_bytes") or 0 for d in disk_views)
        free = sum(d.get("free_bytes") or 0 for d in disk_views)
        return {
            "id": bset.id,
            "group_id": bset.group_id,
            "position": bset.position,
            "label": bset.label,
            "active_disk_id": bset.active_disk_id,
            "last_used_at": bset.last_used_at,
            "disks": disk_views,
            "disk_count": len(disk_views),
            "total_bytes": total,
            "free_bytes": free,
            "is_active": bset.id == bset.group.active_set_id if bset.group else False,
        }

    @staticmethod
    def _session_view(session: Optional[BackupSession]) -> Optional[Dict[str, Any]]:
        if session is None:
            return None
        return {
            "id": session.id,
            "status": session.status,
            "phase": session.phase,
            "trigger": session.trigger,
            "started_at": session.started_at,
            "completed_at": session.completed_at,
            "datasets_done": session.datasets_done,
            "datasets_total": session.datasets_total,
            "bytes_written": session.bytes_written,
            "error": session.error,
        }

    # ── Session history ─────────────────────────────────────────────────
    async def list_sessions(self, db: Session, group_id: Optional[int] = None, limit: int = 100) -> List[Dict[str, Any]]:
        """Recent sessions, newest first, with their per-dataset runs."""
        query = db.query(BackupSession)
        if group_id is not None:
            query = query.filter(BackupSession.group_id == group_id)
        sessions = query.order_by(BackupSession.id.desc()).limit(limit).all()
        out = []
        for session in sessions:
            view = self._session_view(session)
            runs = sorted(session.runs, key=lambda r: r.id)
            current = next((r for r in runs if r.status == "running"), None)
            expected = None
            pct = None
            if current is not None:
                expected = await self._expected_bytes(db, current)
                if expected:
                    size = current.size_bytes or 0
                    pct = min(99, int(100 * size / expected))
            finished = sum(
                v or 0 for v in (
                    session.datasets_done, session.datasets_skipped, session.datasets_failed,
                )
            )
            if session.datasets_total:
                if current is not None and expected:
                    run_fraction = min(1.0, (current.size_bytes or 0) / expected)
                    progress = int(100 * min(1.0, (finished + run_fraction) / session.datasets_total))
                else:
                    progress = int(100 * min(1.0, finished / session.datasets_total))
            else:
                progress = 0
            if current is not None:
                progress = min(99, progress)
            view.update({
                "group_id": session.group_id,
                "group_name": session.group.name if session.group else None,
                "backup_set_id": session.backup_set_id,
                "set_position": session.backup_set.position if session.backup_set else None,
                "backup_disk_id": session.backup_disk_id,
                "disk_label": session.backup_disk.label if session.backup_disk else None,
                "datasets_skipped": session.datasets_skipped,
                "datasets_failed": session.datasets_failed,
                "notes": session.notes,
                "progress_pct": progress,
                "current_dataset": current.dataset_name if current else None,
                "current_run": {
                    "dataset_name": current.dataset_name,
                    "backup_type": current.backup_type,
                    "phase": current.phase,
                    "size_bytes": current.size_bytes,
                    "estimated_bytes": current.estimated_bytes,
                    "expected_bytes": expected,
                    "pct": pct,
                } if current else None,
                "runs": [
                    {
                        "id": r.id,
                        "dataset_name": r.dataset_name,
                        "backup_type": r.backup_type,
                        "promoted_from": r.promoted_from,
                        "status": r.status,
                        "phase": r.phase,
                        "size_bytes": r.size_bytes,
                        "estimated_bytes": r.estimated_bytes,
                        "expected_bytes": expected if current is not None and r is current else None,
                        "pct": pct if current is not None and r is current else None,
                        "changed_bytes": r.changed_bytes,
                        "snapshot": r.snapshot,
                        "error": r.error,
                        "started_at": r.started_at,
                        "completed_at": r.completed_at,
                    }
                    for r in runs
                ],
            })
            out.append(view)
        return out

    async def _expected_bytes(self, db: Session, run: BackupRun) -> Optional[int]:
        """Compressed-size comparator for a running run's progress bar.

        Prefer the last successful stream of the same dataset and type (its
        on-disk stream size matches ``size_bytes``).  With no history of
        that type, scale the capacity estimate by this dataset's observed
        compression ratio.  For a dataset that has never backed up, fall back to
        its live ZFS ``used`` bytes (what a full send mines from).  Returns None
        only when no denominator can be established, which the UI shows as
        bytes-without-a-percentage instead of a wrong one.
        """
        prior = (
            db.query(BackupRun)
            .filter(
                BackupRun.dataset_name == run.dataset_name,
                BackupRun.backup_type == run.backup_type,
                BackupRun.status == "success",
                BackupRun.id != run.id,
            )
            .order_by(BackupRun.id.desc())
            .first()
        )
        if prior and prior.size_bytes:
            return prior.size_bytes
        if run.estimated_bytes:
            any_prior = (
                db.query(BackupRun)
                .filter(
                    BackupRun.dataset_name == run.dataset_name,
                    BackupRun.status == "success",
                    BackupRun.id != run.id,
                    BackupRun.estimated_bytes.isnot(None),
                    BackupRun.estimated_bytes > 0,
                )
                .order_by(BackupRun.id.desc())
                .first()
            )
            if any_prior and any_prior.size_bytes:
                ratio = any_prior.size_bytes / any_prior.estimated_bytes
                return int(run.estimated_bytes * ratio)
        if self.zfs_backup is not None:
            try:
                used = await self.zfs_backup.estimate_full_size(run.dataset_name)
            except Exception:
                used = 0
            if used:
                return used
        return None

    async def is_session_running(self, db: Session, group_id: int) -> bool:
        return (
            db.query(BackupSession)
            .filter(BackupSession.group_id == group_id, BackupSession.status == SESSION_RUNNING)
            .first()
        ) is not None

    def _lock(self, group_id: int) -> asyncio.Lock:
        lock = self._group_locks.get(group_id)
        if lock is None:
            lock = self._group_locks[group_id] = asyncio.Lock()
        return lock

    # ── Startup recovery ────────────────────────────────────────────────
    async def recover_orphaned_backups(self, db: Session) -> None:
        """Fail backups a previous process left in 'running' state.

        A crash or reboot kills any in-flight session and run, but their rows
        survive in the database; without this the UI would show a backup that
        can never finish.  Runs once at app startup, when nothing else can be
        genuinely running.  Snapshot left by an interrupted send is destroyed,
        matching the normal failure path.  Terminal rows ('success', 'partial',
        'failed', 'needs_disk') are never touched.
        """
        stale_runs = db.query(BackupRun).filter(BackupRun.status == SESSION_RUNNING).all()
        if stale_runs:
            logger.warning("recovering %d orphaned backup run(s)", len(stale_runs))
            for run in stale_runs:
                if run.snapshot and self.zfs_backup is not None:
                    await self.zfs_backup._discard_snapshot(run.snapshot)
                run.status = SESSION_FAILED
                run.phase = None
                run.error = "Interrupted by a restart; the backup did not complete."
                run.completed_at = datetime.now(timezone.utc)
            db.commit()

        stale_sessions = db.query(BackupSession).filter(
            BackupSession.status == SESSION_RUNNING).all()
        if stale_sessions:
            logger.warning("recovering %d orphaned backup session(s)", len(stale_sessions))
            for session in stale_sessions:
                session.status = SESSION_FAILED
                session.phase = None
                session.error = "Interrupted by a restart; the backup did not complete."
                session.completed_at = datetime.now(timezone.utc)
            db.commit()

    # ── Cron reconciliation ─────────────────────────────────────────────
    async def sync_scheduled_tasks(self, db: Session) -> None:
        """Reconcile each group's crons into ScheduledTask (ZFS_BACKUP) jobs.

        Runs on scheduler start so configured groups survive restarts, and after
        every change that alters a group's cron, set or dataset list.
        """
        if self.scheduler is None:
            return
        desired: Dict[str, Dict[str, Any]] = {}
        for group in db.query(BackupGroup).all():
            if not group.enabled or not self.group_datasets(db, group.id):
                continue
            for key, backup_type, cron in (
                ("full", "full", group.full_cron),
                ("incremental", "incremental", group.incremental_cron),
            ):
                if not cron:
                    continue
                desired[f"backup-group-{group.id}-{key}"] = {
                    "group_id": group.id, "type": backup_type, "cron": cron,
                    "name": f"Backup group '{group.name}' ({key})",
                }

        existing = {
            t.name: t for t in db.query(ScheduledTask).filter(
                ScheduledTask.task_type == TaskType.ZFS_BACKUP.value,
            ).all()
        }
        for name, cfg in desired.items():
            config = {"group_id": cfg["group_id"], "type": cfg["type"]}
            task = existing.get(name)
            try:
                if task is None:
                    await self.scheduler.create_task(
                        db, name=name, task_type=TaskType.ZFS_BACKUP,
                        target=cfg["name"], schedule=cfg["cron"], config=config,
                    )
                elif task.schedule != cfg["cron"] or task.config != config:
                    await self.scheduler.update_task(
                        db, task.id, schedule=cfg["cron"], config=config,
                    )
            except Exception as e:
                logger.error("could not reconcile backup job %s: %s", name, e)
        for name, task in existing.items():
            if name not in desired:
                await self.scheduler.delete_task(db, task.id)

    # ── The rotation cycle ──────────────────────────────────────────────
    async def start_session(
        self, db: Session, group_id: int, backup_type: str = "full",
    ) -> BackupSession:
        """Create a session row and run it in the background.

        Returns immediately so the UI shows a ``running`` entry; the trigger
        reasons about the active set and disk once the task starts.
        """
        self._check_trigger(backup_type)
        group = self.get_group(db, group_id)
        datasets = self.group_datasets(db, group_id)
        if not datasets:
            raise ValidationError("This backup group has no datasets")
        if not self.group_sets(db, group_id):
            raise ValidationError("This backup group has no backup sets")
        if await self.is_session_running(db, group_id):
            raise ValidationError("A backup is already running for this group")

        session = BackupSession(
            group_id=group.id, trigger=backup_type,
            status=SESSION_RUNNING, phase="resolving", datasets_total=len(datasets),
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        task = asyncio.create_task(self._session_worker(session.id))
        self._session_tasks.add(task)
        task.add_done_callback(self._session_tasks.discard)
        return session

    async def _session_worker(self, session_id: int) -> None:
        try:
            from ..database import get_db_context
            with get_db_context() as db:
                session = db.query(BackupSession).filter(BackupSession.id == session_id).first()
                if not session:
                    return
                await self.run_session(db, session.id)
        except Exception as e:
            logger.error("background backup session %s failed: %s", session_id, e, exc_info=True)

    async def run_session(self, db: Session, session_id: int) -> Dict[str, Any]:
        """Execute a session: write every dataset, then rotate to the next set."""
        session = db.query(BackupSession).filter(BackupSession.id == session_id).first()
        if not session:
            raise NotFoundError("Backup session not found")
        async with self._lock(session.group_id):
            return await self._execute_session(db, session)

    @staticmethod
    def _check_trigger(backup_type: str) -> None:
        if backup_type not in ("full", "incremental"):
            raise ValidationError("backup_type must be 'full' or 'incremental'")

    async def _execute_session(self, db: Session, session: BackupSession) -> Dict[str, Any]:
        group = self.get_group(db, session.group_id)
        datasets = self.group_datasets(db, group.id)
        sets = self.group_sets(db, group.id)
        if not sets:
            return await self._finish(
                db, session, SESSION_FAILED, error="This backup group has no backup sets",
            )

        bset = self._active_set(group, sets)
        target_sets = self._target_sets(group, sets, bset)
        session.backup_set_id = bset.id
        session.phase = "resolving"
        db.commit()

        notes: List[str] = []
        if len(target_sets) > 1:
            notes.append(
                "Backup written to each of these sets: "
                + ", ".join(self._set_name(s) for s in target_sets)
            )

        done = skipped = failed = 0
        total_bytes = 0
        last_rec: Optional[BackupDisk] = None
        last_recs: List[BackupDisk] = []
        for cur in target_sets:
            out = await self._write_one_set(db, session, group, cur, datasets, notes)
            done += out["done"]
            skipped += out["skipped"]
            failed += out["failed"]
            total_bytes += out["total_bytes"]
            if out["last_rec"] is not None:
                last_rec = out["last_rec"]
                last_recs.append(last_rec)
            if out["stopped"]:
                group.needs_disk = True
                db.commit()
                await self._notify_no_disk(group, cur, dataset=out.get("dataset"))
                return await self._finish(
                    db, session, SESSION_NEEDS_DISK,
                    error=out["error"], notes=notes,
                    done=done, skipped=skipped, failed=failed,
                    bytes_written=total_bytes, stopped=True,
                )

        if failed and (done or skipped):
            status = SESSION_PARTIAL
        elif failed:
            status = SESSION_FAILED
        else:
            status = SESSION_SUCCESS

        # A successful session rotates the group by the number of sets written
        # so the next trigger starts after them, wrapping at the end of the cycle.
        rotated_to = None
        if status in (SESSION_SUCCESS, SESSION_PARTIAL):
            now = datetime.now(timezone.utc)
            for cur in target_sets:
                cur.last_used_at = now
            rotated_to = self._rotate(group, sets, bset, steps=len(target_sets))
            group.needs_disk = False
            group.last_session_at = now
            db.commit()
            for rec in last_recs:
                await self._capture_config(db, rec)

        result = await self._finish(
            db, session, status, notes=notes, done=done, skipped=skipped,
            failed=failed, bytes_written=total_bytes,
        )
        await self._notify_finished(group, bset, last_rec, result, rotated_to)
        return result

    async def _write_one_set(
        self, db: Session, session: BackupSession, group: BackupGroup,
        bset: BackupSet, datasets: List[str], notes: List[str],
    ) -> Dict[str, Any]:
        """Write every dataset of the group to one set's active disk.

        Returns per-set counters and ``stopped``/``error`` when this set had to
        block the whole session (its disk unusable, or no disk with room for a
        dataset and recycling not possible).
        """
        # The first write to a set starts its chain, so it is always a full of
        # every dataset in the group plus the configuration bundle.
        first_visit = bset.last_used_at is None
        trigger = "full" if first_visit else session.trigger
        rec = await self._active_disk(db, bset)
        if rec is None:
            return {
                "done": 0, "skipped": 0, "failed": 0, "total_bytes": 0,
                "stopped": True, "dataset": None, "last_rec": None,
                "error": (
                    f"No usable disk in backup set {self._set_name(bset)}. "
                    "Add a disk to the set, advance to another set, or plug one in."
                ),
            }

        if first_visit and session.trigger == "incremental":
            notes.append(
                f"{self._set_name(bset)}: not used before, so the first backup of "
                "every dataset is a full"
            )

        session.backup_disk_id = rec.id
        session.phase = "snapshotting"
        db.commit()

        last_rec = rec
        done = skipped = failed = 0
        total_bytes = 0
        for dataset in datasets:
            if self.zfs_backup is not None and not await self.zfs_backup._dataset_exists(dataset):
                failed += 1
                await self._record_run(
                    db, session, dataset, "full", rec, status="failed",
                    error="Dataset not found in ZFS",
                )
                self._update_progress(db, session, done, skipped, failed, total_bytes)
                continue

            session.phase = "sending"
            db.commit()
            run = self._new_run(db, session, dataset, trigger, rec, backup_set_id=bset.id)
            result = await self.zfs_backup.backup_dataset(db, rec, run)
            status = result.get("status")

            if status == "needs_space":
                moved = await self._advance_for_space(db, bset, rec, dataset, result)
                recycled = False
                if moved is None and group.recycle_full_disks \
                        and not self._disk_written_this_session(db, session, rec):
                    recycled_result = await self._recycle_for_space(
                        db, rec, run, notes,
                    )
                    if recycled_result is not None:
                        moved = rec
                        result = recycled_result
                        status = result.get("status")
                        recycled = True
                if moved is None:
                    return {
                        "done": done, "skipped": skipped, "failed": failed + 1,
                        "total_bytes": total_bytes, "stopped": True, "dataset": dataset,
                        "last_rec": last_rec,
                        "error": (
                            f"Backup set {self._set_name(bset)} has no disk with room for "
                            f"{dataset}. Add a disk to the set or move the group to another set."
                        ),
                    }
                if not recycled:
                    rec, last_rec = moved, moved
                    session.backup_disk_id = rec.id
                    notes.append(
                        f"Advanced to disk '{rec.label or rec.fs_uuid}' for {dataset}: "
                        "the previous disk was full"
                    )
                    # Same run, same snapshot: only the target disk changes.
                    run.backup_disk_id = rec.id
                    run.status = "running"
                    run.error = None
                    run.phase = "pending"
                    db.commit()
                    result = await self.zfs_backup.backup_dataset(db, rec, run)
                    status = result.get("status")
                else:
                    last_rec = rec

            if status == "success":
                done += 1
                total_bytes += result["run"].size_bytes or 0
                if result["run"].promoted_from:
                    notes.append(
                        f"{dataset}: incremental promoted to full (no base snapshot in this set)"
                    )
                    await self._notify_promoted(group, bset, dataset)
            elif status == "skipped":
                skipped += 1
            else:
                failed += 1
            self._update_progress(db, session, done, skipped, failed, total_bytes)

        return {
            "done": done, "skipped": skipped, "failed": failed,
            "total_bytes": total_bytes, "stopped": False, "dataset": None,
            "last_rec": last_rec,
        }

    async def _recycle_for_space(
        self, db: Session, rec: BackupDisk, run: BackupRun, notes: List[str],
    ) -> Optional[Dict[str, Any]]:
        """Wipe a full disk in place and re-run the pending dataset against it.

        Returns the new ``backup_dataset`` result, or None when recycling could
        not proceed (offline disk, format error, still no room).  The disk keeps
        its slot in the set; all of its prior stream records are deleted, so the
        rewritten dataset starts a fresh full chain.
        """
        try:
            if self.zfs_backup is not None:
                await self.zfs_backup.recycle_disk(db, rec.id)
        except Exception as e:
            logger.warning("recycle of backup disk %s failed: %s", rec.id, e)
            return None
        notes.append(
            f"Disk '{rec.label or rec.fs_uuid}' was full and has been recycled "
            "(wiped and reformatted); its chain restarts as fulls"
        )
        run.backup_disk_id = rec.id
        run.status = "running"
        run.error = None
        run.phase = "pending"
        db.commit()
        new_result = await self.zfs_backup.backup_dataset(db, rec, run)
        if new_result.get("status") == "needs_space":
            return None
        return new_result

    @staticmethod
    def _disk_written_this_session(
        db: Session, session: BackupSession, rec: BackupDisk,
    ) -> bool:
        """True when this session already stored a stream on ``rec``.

        Recycling destroys everything already on the disk, so it is only allowed
        before the session has written to it - otherwise the freshly written
        datasets would be wiped mid-session.
        """
        return db.query(BackupRun).filter(
            BackupRun.session_id == session.id,
            BackupRun.backup_disk_id == rec.id,
            BackupRun.status == "success",
        ).count() > 0

    def _target_sets(
        self, group: BackupGroup, sets: List[BackupSet], active: BackupSet,
    ) -> List[BackupSet]:
        """The ``copies`` consecutive sets (from ``active``) this session targets."""
        copies = max(1, min(group.copies or 1, len(sets)))
        order = [s for s in sets]
        try:
            start = order.index(active)
        except ValueError:
            start = 0
        return [order[(start + i) % len(order)] for i in range(copies)]

    # -- session helpers --------------------------------------------------
    def _active_set(self, group: BackupGroup, sets: List[BackupSet]) -> BackupSet:
        for bset in sets:
            if bset.id == group.active_set_id:
                return bset
        group.active_set_id = sets[0].id
        return sets[0]

    def _rotate(
        self, group: BackupGroup, sets: List[BackupSet], current: BackupSet,
        steps: int = 1,
    ) -> Optional[int]:
        """Move the group ``steps`` sets past ``current``, wrapping the cycle."""
        order = [s.id for s in sets]
        if current.id not in order:
            return None
        steps = (steps or 1) % len(order)
        nxt = order[(order.index(current.id) + steps) % len(order)]
        group.active_set_id = nxt
        return nxt

    @staticmethod
    def _set_name(bset: BackupSet) -> str:
        return bset.label or f"set {bset.position + 1}"

    async def _active_disk(self, db: Session, bset: BackupSet) -> Optional[BackupDisk]:
        """The set's current disk, or the first one that will mount."""
        disks = self.set_disks(db, bset.id)
        if not disks:
            return None
        order = [d.id for d in disks]
        ordered = disks
        if bset.active_disk_id in order:
            index = order.index(bset.active_disk_id)
            ordered = disks[index:] + disks[:index]
        for rec in ordered:
            try:
                if self.zfs_backup is not None:
                    await self.zfs_backup.mount_backup_disk(db, rec.id)
                bset.active_disk_id = rec.id
                db.commit()
                return rec
            except Exception as e:
                logger.info(
                    "disk %s in backup set %s is not usable (%s)",
                    rec.id, bset.id, e,
                )
        return None

    async def _advance_for_space(
        self, db: Session, bset: BackupSet, rec: BackupDisk,
        dataset: str, result: Dict[str, Any],
    ) -> Optional[BackupDisk]:
        """Find the next disk in the set that has room for ``dataset``.

        The set is one chain, so the new disk continues from the same base
        rather than starting a fresh full - which is what keeps a set's media
        usable as a unit.  Returns None when no disk in the set can take it, so
        the caller can block the group and tell the user to add media.
        """
        needed = int(result.get("needed_bytes") or 0)
        if self.zfs_backup is not None:
            await self.zfs_backup._restore_idle_state(rec)
        for candidate in self.set_disks(db, bset.id):
            if candidate.id == rec.id:
                continue
            try:
                if self.zfs_backup is not None:
                    await self.zfs_backup.mount_backup_disk(db, candidate.id)
                    if not self.zfs_backup.fits(candidate, needed):
                        await self.zfs_backup._restore_idle_state(candidate)
                        continue
            except Exception as e:
                logger.info(
                    "disk %s in backup set %s could not take %s (%s)",
                    candidate.id, bset.id, dataset, e,
                )
                continue
            bset.active_disk_id = candidate.id
            db.commit()
            return candidate
        return None

    def _new_run(
        self, db: Session, session: BackupSession, dataset: str,
        backup_type: str, rec: BackupDisk,
        backup_set_id: Optional[int] = None,
    ) -> BackupRun:
        run = BackupRun(
            session_id=session.id, group_id=session.group_id,
            backup_set_id=backup_set_id or session.backup_set_id, dataset_name=dataset,
            backup_disk_id=rec.id, backup_type=backup_type,
            status="running", phase="pending",
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        return run

    async def _record_run(
        self, db: Session, session: BackupSession, dataset: str, backup_type: str,
        rec: BackupDisk, status: str, error: Optional[str] = None,
    ) -> BackupRun:
        run = self._new_run(db, session, dataset, backup_type, rec)
        run.status = status
        run.error = error
        run.phase = None
        run.completed_at = datetime.now(timezone.utc)
        db.commit()
        return run

    @staticmethod
    def _update_progress(
        db: Session, session: BackupSession, done: int, skipped: int,
        failed: int, total_bytes: int,
    ) -> None:
        """Publish per-dataset progress while the session runs.

        Without this the counters only reach the row in ``_finish``, so a live
        session reads 0/N until the whole run completes.
        """
        session.datasets_done = done
        session.datasets_skipped = skipped
        session.datasets_failed = failed
        session.bytes_written = total_bytes
        db.commit()

    async def _finish(
        self, db: Session, session: BackupSession, status: str,
        error: Optional[str] = None, notes: Optional[List[str]] = None,
        done: int = 0, skipped: int = 0, failed: int = 0, bytes_written: int = 0,
        stopped: bool = False,
    ) -> Dict[str, Any]:
        session.status = status
        session.error = error
        if notes:
            session.notes = "; ".join(notes)[:1000]
        session.datasets_done = done
        session.datasets_skipped = skipped
        session.datasets_failed = failed
        session.bytes_written = bytes_written
        session.phase = None
        session.completed_at = datetime.now(timezone.utc)
        db.commit()
        logger.info(
            "backup session %s (%s) finished: %s", session.id, session.trigger, status,
        )
        self._log_backup_finished(session, status)
        return {"session_id": session.id, "status": status, "stopped": stopped}

    @staticmethod
    def _log_backup_finished(session: BackupSession, status: str) -> None:
        """Journal a finished session with its elapsed time and data volume."""
        group_name = session.group.name if session.group else f"group {session.group_id}"
        duration_ms = _elapsed_ms(session.started_at, session.completed_at)
        level = {
            SESSION_SUCCESS: "success",
            SESSION_PARTIAL: "warning",
            SESSION_NEEDS_DISK: "warning",
        }.get(status, "error")
        detail = f"{session.datasets_done or 0}/{session.datasets_total or 0} datasets"
        if session.datasets_failed:
            detail += f", {session.datasets_failed} failed"
        message = f"Backup {group_name} {status.replace('_', ' ')}: {detail}"
        try:
            notification_store.add(
                level=level,
                title="Backup finished",
                message=message,
                source="backup",
                duration_ms=duration_ms,
                bytes=session.bytes_written or None,
            )
        except Exception:
            logger.warning("failed to journal backup session %s", session.id, exc_info=True)

    async def _capture_config(self, db: Session, rec: Optional[BackupDisk]) -> None:
        """Snapshot the configuration onto the disk the session finished on.

        Each volume carries its own bundle so it can rebuild the system on its
        own; a set therefore holds a bundle per disk it has ever used.
        """
        if rec is None or self.backup is None:
            return
        try:
            await self.backup.capture_config_bundle(
                db, rec.mount_point, media=self.zfs_backup._media_identity(rec),
            )
        except Exception as e:
            logger.warning("config capture on volume %s failed: %s", rec.mount_point, e)

    # ── Notifications ───────────────────────────────────────────────────
    async def _notify(self, event_key: str, message: str, severity: str, force: bool = False) -> None:
        if self.alerter is None:
            return
        try:
            await self.alerter.notify(event_key, message, severity=severity, force=force)
        except Exception:
            logger.warning("could not send backup alert %s", event_key, exc_info=True)

    async def _notify_no_disk(
        self, group: BackupGroup, bset: BackupSet, dataset: Optional[str] = None,
    ) -> None:
        detail = f" for {dataset}" if dataset else ""
        await self._notify(
            f"backup:group:{group.id}:needs_disk",
            f"Backup group '{group.name}' has no usable disk in "
            f"{self._set_name(bset)}{detail}. Add a disk to that set or move the "
            f"group to another set.",
            "error",
        )

    async def _notify_promoted(self, group: BackupGroup, bset: BackupSet, dataset: str) -> None:
        await self._notify(
            f"backup:set:{bset.id}:promoted",
            f"Backup group '{group.name}': {dataset} was promoted from an incremental "
            f"to a full backup because {self._set_name(bset)} had no base snapshot.",
            "info", force=True,
        )

    async def _notify_finished(
        self, group: BackupGroup, bset: BackupSet, rec: Optional[BackupDisk],
        result: Dict[str, Any], rotated_to: Optional[int],
    ) -> None:
        status = result.get("status")
        if status == SESSION_NEEDS_DISK:
            return
        if status == SESSION_FAILED:
            await self._notify(
                f"backup:group:{group.id}:failed",
                f"Backup group '{group.name}' failed: every dataset in the group "
                f"errored on {self._set_name(bset)}.",
                "error",
            )
            return
        message = (
            f"Backup group '{group.name}' {result.get('status', 'finished')}: "
            f"{result.get('done', 0)}/{result.get('total', 0)} datasets on "
            f"{self._set_name(bset)}"
            f"{' (' + rec.label + ')' if rec is not None and rec.label else ''}, "
            f"{result.get('bytes_written', 0)} bytes"
        )
        if rotated_to is not None:
            message += f". Next backup starts on set {rotated_to}"
        await self._notify(
            f"backup:group:{group.id}:complete",
            message,
            "warning" if status == SESSION_PARTIAL else "info",
            force=True,
        )
