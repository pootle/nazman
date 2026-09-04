"""Samba (SMB) share management.

Unlike NFS (which is ZFS-native via ``sharenfs``), SMB is served by Samba and
Samba's source of truth is ``/etc/samba/smb.conf``. NAZMan therefore manages the
share definitions directly: each dataset gets a ``[<share>]`` section, and the
manager reads/writes only the region of the file it owns so unrelated user or
system config is never clobbered.

Access model mirrors the NFS "shared anonymous identity" philosophy: every SMB
share is guest-readable/writable, with reads/writes forced to a single shared
authoritative user/group (the existing ``nfsanon`` UID/GID 65533) so POSIX
permissions stay consistent across SMB and NFS clients.
"""

import os
import re
import shutil
from typing import List, Dict, Any, Optional

from sqlalchemy.orm import Session

from ..utils.commands import run_command, run_zfs
from ..utils.exceptions import SmbError, ValidationError

SMB_CONF_PATH = "/etc/samba/smb.conf"

# Marker lines that fence the region NAZMan owns inside smb.conf.
# Everything between these markers is rewritten on every write.
_MARKER_BEGIN = "# ===== NAZMan managed shares (do not edit) ====="
_MARKER_END = "# ===== end NAZMan managed shares ====="

# Shared identity used by NFS; reuse it for SMB so NFS and SMB clients write as
# the same user/group, keeping permission semantics consistent.
ANON_USER = "nfsanon"
ANON_UID = 65533
ANON_GID = 65533


def _normalize_dataset_name(name: str) -> str:
    return name.strip().strip("/")


class SmbManager:
    """Manages Samba shares via a NAZMan-owned region in smb.conf."""

    # -- server readiness ---------------------------------------------------

    @staticmethod
    def is_server_present() -> bool:
        """True if Samba's ``smbd`` binary is installed on this host."""
        return any(
            os.path.isfile(p)
            for p in ("/usr/sbin/smbd", "/usr/bin/smbd")
        )

    async def install_server(self) -> Dict[str, Any]:
        """Install Samba on this host via apt (Debian/Ubuntu only).

        Idempotent: returns immediately when ``smbd`` is already present.
        Installs the ``samba`` package, then enables and starts the ``smbd``
        and ``nmbd`` services, and ensures the shared anonymous identity used
        by NAZMan's shares exists.
        """
        if self.is_server_present():
            return {"installed": True, "message": "Samba is already installed."}

        if shutil.which("apt-get") is None:
            raise SmbError(
                "Samba is not installed and apt-get is not available on this "
                "server. Install the `samba` package manually."
            )

        env = {"DEBIAN_FRONTEND": "noninteractive"}

        _, stderr, rc = await run_command(
            ["apt-get", "update"], timeout=600, check=False,
            env=env, op="system", category="smb",
        )
        if rc != 0:
            raise SmbError(f"apt-get update failed: {stderr.strip()}")

        _, stderr, rc = await run_command(
            ["apt-get", "install", "-y", "samba"], timeout=600, check=False,
            env=env, op="system", category="smb",
        )
        if rc != 0:
            raise SmbError(f"Failed to install samba: {stderr.strip()}")

        await run_command(["systemctl", "enable", "smbd"], timeout=60, op="system", category="smb")
        await run_command(["systemctl", "enable", "nmbd"], timeout=60, op="system", category="smb")
        await run_command(["systemctl", "start", "smbd"], timeout=60, op="system", category="smb")
        await run_command(["systemctl", "start", "nmbd"], timeout=60, op="system", category="smb")

        await self._ensure_anon_user()

        return {
            "installed": self.is_server_present(),
            "message": "Samba installed successfully." if self.is_server_present()
            else "Samba install completed but smbd was not found.",
        }

    # -- dataset helpers ---------------------------------------------------

    @staticmethod
    async def _dataset_exists(dataset_name: str) -> bool:
        stdout, _, rc = await run_zfs(
            "list", "-H", "-o", "name", dataset_name, check=False, op="read",
        )
        return rc == 0 and dataset_name in stdout.split()

    @staticmethod
    def share_name_for(dataset_name: str) -> str:
        """Derive an smb.conf share name from a dataset name (its basename)."""
        base = _normalize_dataset_name(dataset_name).rsplit("/", 1)[-1].lower()
        name = re.sub(r"[^a-z0-9_]", "_", base)
        return name.strip("_") or "share"

    # -- config path (test override) ---------------------------------------

    def conf_path(self) -> str:
        return os.environ.get("NASMAN_SMB_CONF", SMB_CONF_PATH)

    # -- smb.conf parsing ---------------------------------------------------

    @staticmethod
    def _read_conf(path: str) -> str:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return ""

    @staticmethod
    def _split_sections(text: str) -> List[Dict[str, Any]]:
        """Parse smb.conf into a list of ``{name, options:[(k,v),...]}``."""
        sections: List[Dict[str, Any]] = []
        current: Optional[Dict[str, Any]] = None
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith(";"):
                continue
            m = re.match(r"^\[([^\]]+)\]$", line)
            if m:
                current = {"name": m.group(1), "options": []}
                sections.append(current)
                continue
            if current is not None and "=" in line:
                key, _, value = line.partition("=")
                current["options"].append((key.strip().lower(), value.strip()))
        return sections

    @staticmethod
    def _section_to_meta(section: Dict[str, Any]) -> Dict[str, Any]:
        opts = dict(section["options"])
        dataset_name = _normalize_dataset_name(opts.get("path", ""))
        enabled = opts.get("enabled", "yes").lower() != "no" and \
            opts.get("guest ok", "no").lower() == "yes"
        return {
            "name": section["name"],
            "dataset_name": dataset_name,
            "share_path": f"/{dataset_name}",
            "share_name": section["name"],
            "read_only": opts.get("read only", "no").lower() == "yes",
            "guest_ok": opts.get("guest ok", "no").lower() == "yes",
            "enabled": enabled,
        }

    def _managed_shares(self, path: str) -> List[Dict[str, Any]]:
        """Parse only the NAZMan-owned region into share metadata."""
        lines = self._read_conf(path).splitlines()
        start = end = None
        for i, line in enumerate(lines):
            if line.strip() == _MARKER_BEGIN:
                start = i
            elif line.strip() == _MARKER_END:
                end = i
        if start is None or end is None or end <= start:
            return []
        region = "\n".join(lines[start:end])
        return [self._section_to_meta(s) for s in self._split_sections(region)]

    def list_shares(self, db: Session) -> List[Dict[str, Any]]:
        """List every dataset with an active NAZMan-managed SMB share."""
        return self._managed_shares(self.conf_path())

    # -- rendering ----------------------------------------------------------

    @staticmethod
    def _render_share_block(dataset_name: str, read_only: bool, enabled: bool) -> str:
        share_name = SmbManager.share_name_for(dataset_name)
        return (
            f"[{share_name}]\n"
            f"\tpath = /{_normalize_dataset_name(dataset_name)}\n"
            f"\tread only = {'yes' if read_only else 'no'}\n"
            f"\tguest ok = yes\n"
            f"\tpublic = yes\n"
            f"\tbrowseable = yes\n"
            f"\tenabled = {'yes' if enabled else 'no'}\n"
            f"\tforce user = {ANON_USER}\n"
            f"\tforce group = {ANON_USER}\n"
        )

    @staticmethod
    def _block_dataset(block: str) -> Optional[str]:
        """Extract the dataset from a rendered block's ``path =`` line."""
        for line in block.splitlines():
            line = line.strip()
            if line.lower().startswith("path ="):
                return _normalize_dataset_name(line.partition("=")[2])
        return None

    def _rewrite_region(self, path: str,
                        set_block: Optional[str] = None,
                        remove_dataset: Optional[str] = None) -> None:
        """Rewrite the NAZMan-owned region atomically.

        Rebuilds the region from the currently managed shares, optionally
        inserting/replacing ``set_block`` (identified by its ``path``) and/or
        dropping ``remove_dataset``. Unrelated config outside the region is
        preserved byte-for-byte.
        """
        lines = self._read_conf(path).splitlines()
        if not lines:
            lines = []

        start = end = None
        for i, line in enumerate(lines):
            if line.strip() == _MARKER_BEGIN:
                start = i
            elif line.strip() == _MARKER_END:
                end = i

        replace_ds = self._block_dataset(set_block) if set_block else None

        # Existing managed datasets (excluding any we are replacing/removing).
        keep_datasets: List[str] = []
        if start is not None and end is not None and end > start + 1:
            for meta in self._managed_shares(path):
                ds = meta["dataset_name"]
                if not ds:
                    continue
                if replace_ds is not None and ds == replace_ds:
                    continue
                if remove_dataset is not None and ds == remove_dataset:
                    continue
                keep_datasets.append(ds)

        blocks = [self._render_share_block(ds, False, True) for ds in keep_datasets]
        if set_block is not None:
            blocks.append(set_block)

        region = _MARKER_BEGIN + "\n" + "".join(blocks) + _MARKER_END + "\n"

        head = "\n".join(lines[:start]) if start is not None else ""
        tail_lines = lines[end + 1:] if end is not None else lines
        tail = "\n".join(tail_lines) if tail_lines else ""

        if head:
            head += "\n"
        if tail:
            tail = "\n" + tail

        new_text = head + region + tail
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(new_text)
        except OSError as e:
            raise SmbError(f"Failed to write {path}: {e}")

    # -- CRUD --------------------------------------------------------------

    async def set_share(self, db: Session, dataset_name: str,
                        read_only: bool = False, enabled: bool = True) -> Dict[str, Any]:
        """Create or update an SMB share for a dataset."""
        if not await self._dataset_exists(dataset_name):
            raise ValidationError(f"Dataset '{dataset_name}' not found")

        if not self.is_server_present():
            raise SmbError(
                "Samba is not installed on this server. Run the NAZMan installer "
                "(build.sh, choosing to install Samba) or `sudo apt-get install -y samba`."
            )

        await self._ensure_anon_user()

        if enabled:
            try:
                await run_command(["chown", f":{ANON_USER}", f"/{dataset_name}"], timeout=30, op="write", category="smb")
                await run_command(["chmod", "2775", f"/{dataset_name}"], timeout=30, op="write", category="smb")
            except Exception as e:
                raise SmbError(f"Failed to prepare dataset directory: {e}")

        self._rewrite_region(
            self.conf_path(),
            set_block=self._render_share_block(dataset_name, read_only, enabled),
        )
        await self._reload()

        return {
            "dataset_name": dataset_name,
            "share_name": self.share_name_for(dataset_name),
            "share_path": f"/{dataset_name}",
            "read_only": read_only,
            "enabled": enabled,
        }

    async def delete_share(self, db: Session, dataset_name: str) -> None:
        """Remove the NAZMan-managed SMB share for a dataset."""
        if not self._dataset_exists(dataset_name):
            raise ValidationError(f"Dataset '{dataset_name}' not found")
        self._rewrite_region(self.conf_path(), remove_dataset=_normalize_dataset_name(dataset_name))
        await self._reload()

    async def _reload(self) -> None:
        """Validate smb.conf with testparm, then reload Samba config."""
        conf = self.conf_path()
        try:
            _, stderr, rc = await run_command(
                ["testparm", "-s", conf], timeout=30, check=False
            )
            if rc != 0:
                raise SmbError(f"smb.conf failed validation: {stderr}")
        except SmbError:
            raise
        except Exception as e:
            raise SmbError(f"Failed to validate smb.conf: {e}")

        try:
            await run_command(
                ["smbcontrol", "all", "reload-config"],
                timeout=30,
                op="system",
                category="smb",
            )
        except Exception:
            raise SmbError("Samba config validated, but the running smbd could not be reloaded.")

    # -- pool lifecycle ----------------------------------------------------

    def get_pool_share_info(self, db: Session, pool) -> Dict[str, Any]:
        """Return SMB share info for all datasets belonging to a pool."""
        pool_name = pool.name if isinstance(pool.name, str) else str(pool.name)
        shares = self.list_shares(db)
        pool_shares = [
            s for s in shares
            if s["dataset_name"] == pool_name or s["dataset_name"].startswith(pool_name + "/")
        ]
        return {"shares": pool_shares, "active": bool(pool_shares)}

    async def unshare_pool(self, db: Session, pool) -> None:
        """Remove NAZMan-managed SMB shares for every dataset in a pool."""
        pool_name = pool.name if isinstance(pool.name, str) else str(pool.name)
        dataset_names = []
        stdout, _, rc = await run_zfs(
            "list", "-H", "-o", "name", "-t", "filesystem", "-r", pool_name,
            check=False, op="read",
        )
        if rc == 0:
            for line in stdout.splitlines():
                name = line.strip()
                if name and name != pool_name:
                    dataset_names.append(name)
        for name in dataset_names:
            self._rewrite_region(self.conf_path(), remove_dataset=name)
        if dataset_names:
            await self._reload()

    # -- anon identity scaffolding ----------------------------------------

    @staticmethod
    async def _ensure_anon_user() -> None:
        stdout, _, rc = await run_command(
            ["getent", "group", ANON_USER], timeout=10, check=False
        )
        if rc != 0:
            await run_command(["groupadd", "-g", str(ANON_GID), ANON_USER], timeout=30)
        stdout, _, rc = await run_command(
            ["getent", "passwd", ANON_USER], timeout=10, check=False
        )
        if rc != 0:
            await run_command(
                [
                    "useradd", "-r", "-g", str(ANON_GID),
                    "-u", str(ANON_UID), "-M",
                    "-s", "/usr/sbin/nologin",
                    "-d", "/var/lib/nfs", ANON_USER,
                ],
                timeout=30,
            )


# Singleton instance
smb_manager = SmbManager()