"""Backup manifest: self-describing metadata written to backup volumes.

A backup volume carries an aggregate ``nazman-backup.json`` at its root plus a
per-artifact sidecar ``*.info.json`` beside every ZFS send stream and config
bundle.  The manifest records the pool/vdev topology and dataset list needed to
rebuild a system on fresh hardware, plus which medium each dataset stream lives
on.  The sidecars make each artifact self-describing so the aggregate index can
be rebuilt (or a single stream restored) even if the index is lost.

This module is deliberately pure: it builds, reads and writes dicts and files
and never imports a manager.  ZFS/DB facts are gathered by callers.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

MANIFEST_NAME = "nazman-backup.json"
SIDECAR_SUFFIX = ".info.json"
MANIFEST_VERSION = 1
CONFIG_DIR = "config"
DATA_DIR = "data"


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def media_identity(
    fs_uuid: Optional[str] = None,
    label: Optional[str] = None,
    by_id: Optional[str] = None,
    serial: Optional[str] = None,
    size_bytes: Optional[int] = None,
    mount_point: Optional[str] = None,
) -> Dict[str, Any]:
    """Identity of the volume holding a manifest (drops unset fields)."""
    raw = {
        "fs_uuid": fs_uuid, "label": label, "by_id": by_id,
        "serial": serial, "size_bytes": size_bytes, "mount_point": mount_point,
    }
    return {k: v for k, v in raw.items() if v is not None}


def sha256_file(path: str, chunk_size: int = 1 << 20) -> str:
    """SHA-256 of a file, read in chunks (safe for multi-GB streams)."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    """Write JSON to ``path`` atomically (temp file + fsync + os.replace)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def new_manifest(media: Optional[Dict[str, Any]] = None, nazman_version: str = "") -> Dict[str, Any]:
    now = utcnow_iso()
    return {
        "manifest_version": MANIFEST_VERSION,
        "nazman_version": nazman_version,
        "created_at": now,
        "updated_at": now,
        "media": dict(media or {}),
        "config_backups": [],
        "pools": [],
        "datasets": [],
    }


def validate_manifest(data: Any) -> None:
    """Raise ValueError when ``data`` is not a supported manifest."""
    if not isinstance(data, dict):
        raise ValueError("manifest is not a JSON object")
    version = data.get("manifest_version")
    if version != MANIFEST_VERSION:
        raise ValueError(f"unsupported manifest_version: {version!r}")
    for key in ("config_backups", "pools", "datasets"):
        if not isinstance(data.get(key, []), list):
            raise ValueError(f"manifest.{key} must be a list")


def load_manifest(volume_root: str | Path) -> Optional[Dict[str, Any]]:
    """Load the aggregate manifest at a volume root, or None when absent/invalid."""
    path = Path(volume_root) / MANIFEST_NAME
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        validate_manifest(data)
        return data
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def save_manifest(volume_root: str | Path, manifest: Dict[str, Any]) -> None:
    manifest["updated_at"] = utcnow_iso()
    atomic_write_json(Path(volume_root) / MANIFEST_NAME, manifest)


def sidecar_path(stream_file: str | Path) -> Path:
    """Sidecar path for a stream/config file: ``<file>.info.json``."""
    return Path(f"{stream_file}{SIDECAR_SUFFIX}")


def write_sidecar(artifact_path: str | Path, info: Dict[str, Any]) -> None:
    atomic_write_json(sidecar_path(artifact_path), info)


def read_sidecar(artifact_path: str | Path) -> Optional[Dict[str, Any]]:
    path = sidecar_path(artifact_path)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def upsert_config_backup(manifest: Dict[str, Any], entry: Dict[str, Any]) -> None:
    """Insert/replace a config-backup entry (keyed by ``id``), newest first."""
    entries = manifest.setdefault("config_backups", [])
    entries[:] = [e for e in entries if e.get("id") != entry.get("id")]
    entries.append(entry)
    entries.sort(key=lambda e: e.get("created_at") or "", reverse=True)


def upsert_dataset_backup(
    manifest: Dict[str, Any], dataset: Dict[str, Any], run: Dict[str, Any]
) -> None:
    """Record one dataset stream, merging the dataset spec and run by identity.

    The dataset is keyed by ``name``; runs are keyed by ``stream_file`` so a
    re-run of the same snapshot replaces rather than duplicates the entry.
    """
    datasets = manifest.setdefault("datasets", [])
    existing = next((d for d in datasets if d.get("name") == dataset.get("name")), None)
    if existing is None:
        existing = {
            "name": dataset.get("name"),
            "pool": dataset.get("pool"),
            "properties": dict(dataset.get("properties") or {}),
            "mountpoint": dataset.get("mountpoint"),
            "backups": [],
        }
        datasets.append(existing)
    else:
        for key in ("pool", "mountpoint"):
            if dataset.get(key):
                existing[key] = dataset[key]
        if dataset.get("properties"):
            existing["properties"] = dict(dataset["properties"])
    runs = existing.setdefault("backups", [])
    runs[:] = [r for r in runs if r.get("stream_file") != run.get("stream_file")]
    runs.append(run)
    runs.sort(key=lambda r: r.get("created_at") or "")


def merge_pools(manifest: Dict[str, Any], pools: List[Dict[str, Any]]) -> None:
    """Replace the manifest's pool topology with the latest snapshot."""
    manifest["pools"] = list(pools or [])


def _merge_dataset_sidecar(manifest: Dict[str, Any], info: Dict[str, Any]) -> None:
    dataset = info.get("dataset") or {}
    run = info.get("run") or {}
    if dataset.get("name") and run.get("stream_file"):
        upsert_dataset_backup(manifest, dataset, run)


def _merge_config_sidecar(manifest: Dict[str, Any], info: Dict[str, Any]) -> None:
    entry = info.get("config") or {}
    if entry.get("id"):
        upsert_config_backup(manifest, entry)


def build_from_sidecars(
    volume_root: str | Path, media: Optional[Dict[str, Any]] = None, nazman_version: str = ""
) -> Dict[str, Any]:
    """Rebuild a manifest by scanning every sidecar under a volume root."""
    root = Path(volume_root)
    manifest = new_manifest(media=media, nazman_version=nazman_version)
    if not root.exists():
        return manifest
    for sidecar in sorted(root.rglob(f"*{SIDECAR_SUFFIX}")):
        if sidecar.name == f"{MANIFEST_NAME}{SIDECAR_SUFFIX}":
            continue
        try:
            info = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        kind = info.get("kind")
        if kind == "dataset":
            _merge_dataset_sidecar(manifest, info)
        elif kind == "config":
            _merge_config_sidecar(manifest, info)
    return manifest


def scan_volume(
    volume_root: str | Path,
    media: Optional[Dict[str, Any]] = None,
    nazman_version: str = "",
) -> Dict[str, Any]:
    """Return a volume's manifest, reconstructing it from sidecars when needed.

    The aggregate manifest is authoritative when present; sidecars are then
    folded in so an artifact written by an older code path (or a partially
    updated index) is still discoverable.
    """
    manifest = load_manifest(volume_root)
    if manifest is None:
        manifest = build_from_sidecars(volume_root, media=media, nazman_version=nazman_version)
        if media:
            manifest["media"] = dict(media)
        return manifest
    if media and not manifest.get("media"):
        manifest["media"] = dict(media)
    if nazman_version and not manifest.get("nazman_version"):
        manifest["nazman_version"] = nazman_version
    # Fold in any sidecars missing from the index (best effort).
    root = Path(volume_root)
    if root.exists():
        for sidecar in sorted(root.rglob(f"*{SIDECAR_SUFFIX}")):
            if sidecar.name == f"{MANIFEST_NAME}{SIDECAR_SUFFIX}":
                continue
            try:
                info = json.loads(sidecar.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if info.get("kind") == "dataset":
                _merge_dataset_sidecar(manifest, info)
            elif info.get("kind") == "config":
                _merge_config_sidecar(manifest, info)
    return manifest


def manifest_summary(manifest: Dict[str, Any]) -> Dict[str, Any]:
    """Compact view of a manifest for the backup-set menu."""
    media = manifest.get("media") or {}
    datasets = manifest.get("datasets") or []
    pools = manifest.get("pools") or []
    config_backups = manifest.get("config_backups") or []
    media_uuids = sorted({
        r.get("media_fs_uuid") or (r.get("media") or {}).get("fs_uuid")
        for d in datasets for r in (d.get("backups") or [])
        if r.get("media_fs_uuid") or (r.get("media") or {}).get("fs_uuid")
    })
    return {
        "manifest_version": manifest.get("manifest_version"),
        "nazman_version": manifest.get("nazman_version"),
        "created_at": manifest.get("created_at"),
        "updated_at": manifest.get("updated_at"),
        "media": media,
        "pool_names": [p.get("name") for p in pools if p.get("name")],
        "dataset_count": len(datasets),
        "config_backup_count": len(config_backups),
        "media_fs_uuids": media_uuids,
    }
