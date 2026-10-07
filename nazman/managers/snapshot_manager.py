from typing import List, Optional, Dict, Any
from sqlalchemy.orm import Session

from ..utils.commands import run_zfs
from ..utils.exceptions import DatasetError
from ..utils.validation import validate_dataset_name, validate_snapshot_component, validate_snapshot_name


class SnapshotManager:
    """Manages ZFS snapshot operations."""

    async def create_snapshot(
        self,
        db: Session,
        dataset_name: str,
        snapshot_name: str
    ) -> Dict[str, Any]:
        """Create a snapshot of a dataset (no DB persistence)."""
        validate_dataset_name(dataset_name)
        validate_snapshot_component(snapshot_name)
        list_out, _, list_rc = await run_zfs(
            "list", "-H", "-o", "name", dataset_name, check=False, op="read"
        )
        if list_rc != 0 or dataset_name not in list_out.split():
            raise DatasetError(f"Dataset '{dataset_name}' not found")

        full_snapshot_name = f"{dataset_name}@{snapshot_name}"

        stdout, stderr, returncode = await run_zfs(
            "snapshot", full_snapshot_name,
            timeout=300, check=False
        )

        if returncode != 0:
            raise DatasetError(f"Failed to create snapshot: {stderr}")

        return {
            "name": full_snapshot_name,
            "dataset_name": dataset_name,
            "snapshot_name": snapshot_name,
        }

    async def list_snapshots(self, db: Session, dataset_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """List snapshots from ZFS (no DB)."""
        try:
            cmd = ["list", "-H", "-o", "name,used,referenced,creation", "-t", "snapshot"]
            if dataset_name:
                cmd.extend(["-r", dataset_name])

            stdout, stderr, returncode = await run_zfs(*cmd, check=False, op="read")

            if returncode != 0:
                raise DatasetError(f"Failed to list snapshots: {stderr}")

            snapshots = []
            for line in stdout.strip().split('\n'):
                if not line:
                    continue

                parts = line.split('\t')
                if len(parts) >= 3:
                    full_name = parts[0]
                    if '@' in full_name:
                        ds_name, snap_name = full_name.split('@', 1)
                        snapshots.append({
                            "name": full_name,
                            "dataset_name": ds_name,
                            "snapshot_name": snap_name,
                            "used": parts[1] if parts[1] != '-' else None,
                            "referenced": parts[2] if parts[2] != '-' else None,
                            "creation": parts[3] if len(parts) > 3 and parts[3] != '-' else None,
                        })

            return snapshots

        except Exception as e:
            if isinstance(e, DatasetError):
                raise
            raise DatasetError(f"Error listing snapshots: {str(e)}")

    async def destroy_snapshot(self, db: Session, snapshot_name: str) -> None:
        """Destroy a snapshot (DESTRUCTIVE). No DB record to remove."""
        validate_snapshot_name(snapshot_name)
        stdout, stderr, returncode = await run_zfs(
            "destroy", snapshot_name,
            timeout=300, check=False
        )

        if returncode != 0:
            raise DatasetError(f"Failed to destroy snapshot: {stderr}")

