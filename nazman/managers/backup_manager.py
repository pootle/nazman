from typing import List, Optional, Dict, Any
from datetime import datetime, timezone
from pathlib import Path
import logging
import shutil
from sqlalchemy.orm import Session

from ..models.backup_zfs import BackupDisk
from ..config import get_settings
from ..utils import backup_manifest as bm
from ..utils.commands import run_command, run_zpool
from ..utils.exceptions import BackupError

logger = logging.getLogger(__name__)


class BackupManager:
    """Captures configuration bundles onto backup volumes (no git).

    A "configuration bundle" is a full ``nazman.db`` snapshot plus the host
    config files, pool exports and partition tables, written under
    ``<volume>/config/<timestamp>/``.  Each capture is recorded in the volume's
    aggregate manifest and in a ``config.info.json`` sidecar so a fresh install
    can discover and restore it.  Old bundles are pruned to a retention count.
    """

    def __init__(self, zfs=None):
        self.settings = get_settings()
        self.zfs = zfs

    # ── Config capture ──────────────────────────────────────────────────
    async def capture_config_bundle(
        self,
        db: Session,
        volume_root: str | Path,
        media: Optional[Dict[str, Any]] = None,
        message: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Snapshot the configuration onto one volume and update its manifest."""
        volume_root = Path(volume_root)
        volume_root.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        bundle = volume_root / bm.CONFIG_DIR / ts
        bundle.mkdir(parents=True, exist_ok=True)

        # Consistent DB snapshot (a raw copy of a WAL DB can be torn).
        db_path = Path(self.settings.database_path)
        backup_db_path = bundle / "nazman.db"
        if db_path.exists():
            await self._snapshot_db(db_path, backup_db_path)

        # Host configuration files.
        config_files = ["/etc/exports", "/etc/default/nfs-kernel-server"]
        config_dir = bundle / "system-config"
        config_dir.mkdir(exist_ok=True)
        copied = []
        for config_file in config_files:
            if Path(config_file).exists():
                dest = config_dir / Path(config_file).name
                shutil.copy2(config_file, dest)
                copied.append(str(dest.relative_to(volume_root)))

        await self._export_pool_configs(bundle)
        await self._export_partition_tables(bundle)

        manifest = bm.scan_volume(
            volume_root,
            media=media or bm.media_identity(mount_point=str(volume_root)),
            nazman_version=self.settings.app_version,
        )
        await self._merge_live_specs(db, manifest)

        entry = {
            "id": ts,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "message": message or f"Configuration backup - {ts}",
            "path": f"{bm.CONFIG_DIR}/{ts}",
            "db_file": f"{bm.CONFIG_DIR}/{ts}/nazman.db",
            "system_config": copied,
        }
        bm.upsert_config_backup(manifest, entry)
        bm.save_manifest(volume_root, manifest)
        bm.write_sidecar(bundle / "config", {"kind": "config", "config": entry})

        self._prune_config_bundles(volume_root, manifest)
        return entry

    async def _merge_live_specs(self, db: Session, manifest: Dict[str, Any]) -> None:
        """Fold current pool/dataset topology into a manifest (best effort)."""
        if self.zfs is None:
            return
        try:
            bm.merge_pools(manifest, await self.zfs.get_pool_recreate_specs(db))
            known = {d.get("name") for d in manifest.get("datasets", [])}
            for ds in await self.zfs.get_dataset_recreate_specs(db):
                if ds["name"] not in known:
                    manifest.setdefault("datasets", []).append({
                        "name": ds["name"], "pool": ds["pool"],
                        "properties": ds["properties"], "mountpoint": ds.get("mountpoint"),
                        "backups": [],
                    })
        except Exception as e:
            logger.warning("failed to collect live specs for manifest: %s", e)

    def _prune_config_bundles(self, volume_root: Path, manifest: Dict[str, Any]) -> None:
        """Keep only the newest N config bundles on a volume."""
        keep = int(getattr(self.settings, "backup_config_retention", 5) or 5)
        config_root = volume_root / bm.CONFIG_DIR
        if not config_root.exists():
            return
        bundles = sorted(
            (p for p in config_root.iterdir() if p.is_dir()),
            key=lambda p: p.name, reverse=True,
        )
        removed = set()
        for old in bundles[keep:]:
            shutil.rmtree(old, ignore_errors=True)
            removed.add(old.name)
        if removed:
            manifest["config_backups"] = [
                e for e in manifest.get("config_backups", []) if e.get("id") not in removed
            ]
            bm.save_manifest(volume_root, manifest)

    # ── Restore ─────────────────────────────────────────────────────────
    async def restore_configuration_bundle(
        self, db: Session, bundle_path: str | Path, restore_db: bool = True
    ) -> bool:
        """Restore DB and host config files from a config bundle directory."""
        bundle = Path(bundle_path)
        if not bundle.is_dir():
            raise BackupError(f"Config bundle not found: {bundle_path}")

        backup_db = bundle / "nazman.db"
        if restore_db and backup_db.exists():
            shutil.copy2(backup_db, Path(self.settings.database_path))

        config_dir = bundle / "system-config"
        if config_dir.exists():
            for config_file in config_dir.iterdir():
                if config_file.is_file():
                    shutil.copy2(config_file, f"/etc/{config_file.name}")

        exports_file = config_dir / "exports"
        if exports_file.exists():
            await self._apply_exports(exports_file)
        return True

    async def restore_configuration(self, db: Session, commit_hash: str) -> bool:
        """Restore the config bundle whose id (or commit hash) is ``commit_hash``."""
        for volume_root in self._all_volume_roots(db):
            bundle = volume_root / bm.CONFIG_DIR / commit_hash
            if bundle.is_dir():
                return await self.restore_configuration_bundle(db, bundle)
        raise BackupError(f"Config backup {commit_hash} not found")

    def _all_volume_roots(self, db: Session) -> List[Path]:
        """Mount points of every declared backup disk (no local fallback)."""
        return [Path(rec.mount_point) for rec in db.query(BackupDisk).all()]

    def find_config_bundles(self, db: Session) -> List[Dict[str, Any]]:
        """Every config bundle across all declared volumes, newest first."""
        out = []
        for root in self._all_volume_roots(db):
            manifest = bm.load_manifest(root)
            if manifest:
                for entry in manifest.get("config_backups", []):
                    out.append({**entry, "volume_root": str(root)})
        out.sort(key=lambda e: e.get("created_at") or "", reverse=True)
        return out

    # ── Helpers ─────────────────────────────────────────────────────────
    async def _snapshot_db(self, db_path: Path, dest: Path) -> None:
        """Consistent SQLite snapshot via ``sqlite3 .backup`` (falls back to copy)."""
        _, _, rc = await self._run_command(
            ["sqlite3", str(db_path), f".backup {str(dest)}"]
        )
        if rc != 0:
            shutil.copy2(db_path, dest)

    async def _export_pool_configs(self, bundle: Path) -> None:
        """Export ZFS pool status/properties JSON into a bundle."""
        try:
            stdout, stderr, returncode = await run_zpool(
                "list", "-H", "-o", "name", check=False, op="read",
            )
            if returncode != 0:
                return
            config_dir = bundle / "pool-configs"
            config_dir.mkdir(exist_ok=True)
            for pool_name in stdout.strip().split('\n'):
                if not pool_name:
                    continue
                out, _, rc = await run_zpool("status", "-j", pool_name, check=False, op="read")
                if rc == 0:
                    (config_dir / f"{pool_name}.json").write_text(out)
                out, _, rc = await run_zpool("get", "-j", "all", pool_name, check=False, op="read")
                if rc == 0:
                    (config_dir / f"{pool_name}-props.json").write_text(out)
        except Exception as e:
            logger.warning("Failed to export pool configs: %s", e)

    async def _export_partition_tables(self, bundle: Path) -> None:
        """Export sfdisk partition tables for all disks into a bundle."""
        try:
            stdout, stderr, returncode = await self._run_command(
                ["lsblk", "-d", "-n", "-o", "NAME"]
            )
            if returncode != 0:
                return
            config_dir = bundle / "partition-tables"
            config_dir.mkdir(exist_ok=True)
            for disk in stdout.strip().split('\n'):
                disk = disk.strip()
                if not disk:
                    continue
                out, _, rc = await self._run_command(["sfdisk", "-d", f"/dev/{disk}"])
                if rc == 0:
                    (config_dir / f"{disk}.sfdisk").write_text(out)
        except Exception as e:
            logger.warning("Failed to export partition tables: %s", e)

    async def _apply_exports(self, exports_file: Path) -> None:
        try:
            shutil.copy2(exports_file, "/etc/exports")
            await self._run_command(["exportfs", "-ra"], op="system", category="nfs")
        except Exception as e:
            raise BackupError(f"Failed to apply exports: {str(e)}")

    @staticmethod
    async def _run_command(cmd: list, **kwargs) -> tuple:
        try:
            return await run_command(cmd, timeout=60, check=False, **kwargs)
        except Exception as e:
            return ("", str(e), -1)
