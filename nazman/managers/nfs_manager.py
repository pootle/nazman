from typing import List, Dict, Any, Optional

from sqlalchemy.orm import Session

from ..utils.commands import run_zfs, run_command, run_zpool
from ..utils.exceptions import NfsError, ValidationError
from ..utils.validation import validate_ip_cidr


class NfsManager:
    """Manages ZFS-native NFS sharing via the ``sharenfs`` property.

    ZFS is the single source of truth: each dataset carries a ``sharenfs``
    property (``on`` | ``off`` | options string) and ZFS maintains the kernel
    export table itself via ``exportfs``. No NFS export state is persisted in
    the database, so there is no DB copy to drift from the live system.
    Datasets are identified by their ZFS name (``pool/name``); the pool's
    root dataset (``sharenfs`` on the pool itself) is handled separately.
    """

    # Dedicated identity anonymous NFS clients are squashed to, so clients with
    # arbitrary local UIDs get consistent read/write access.
    ANON_USER = "nfsanon"
    ANON_UID = 65533
    ANON_GID = 65533

    # -- presence / server readiness --------------------------------------

    @staticmethod
    def is_server_present() -> bool:
        """True if the NFS kernel server (exportfs) is installed on this host."""
        import os

        return any(os.path.isfile(p) for p in ("/usr/sbin/exportfs", "/usr/bin/exportfs"))

    # -- live property access ---------------------------------------------

    @staticmethod
    def _normalize_sharenfs(value: str) -> str:
        """A bare ``-``/``on``/``off``/empty sharenfs becomes ``"off"`` if not shared."""
        v = (value or "").strip()
        if v in ("-", ""):
            return "off"
        return v

    async def _read_sharenfs(self, dataset_name: str) -> str:
        stdout, _, rc = await run_zfs(
            "get", "-H", "-o", "value", "sharenfs", dataset_name, check=False, op="read",
        )
        return self._normalize_sharenfs(stdout)

    async def _dataset_exists(self, dataset_name: str) -> bool:
        stdout, _, rc = await run_zfs(
            "list", "-H", "-o", "name", dataset_name, check=False, op="read",
        )
        return rc == 0 and dataset_name in stdout.split()

    async def _set_sharenfs(self, dataset_name: str, value: str) -> None:
        """Set the sharenfs property and sync the kernel export accordingly.

        ``value`` is ``"off"`` to disable, otherwise an options string.
        ZFS re-shares/unshares the dataset when the property changes; share/
        unshare are also issued explicitly so the kernel table is current even
        if the daemon state was stale.
        """
        val = value.strip()
        stdout, stderr, rc = await run_zfs(
            "set", f"sharenfs={val}", dataset_name, check=False
        )
        if rc != 0:
            raise NfsError(f"Failed to set sharenfs on {dataset_name}: {stderr}")

        if val == "off" or val == "":
            await run_zfs("unshare", dataset_name, check=False)
        else:
            await run_zfs("share", dataset_name, check=False)

    # -- listing -----------------------------------------------------------

    async def _list_dataset_names(self, pool_name: Optional[str] = None) -> List[str]:
        """Full ZFS names of every dataset (filesystem), excluding pool roots.

        Dataset existence is derived live from ZFS: no database copy exists.
        When ``pool_name`` is given, only that pool's children are returned.
        """
        if pool_name:
            roots = [pool_name]
        else:
            stdout, _, rc = await run_zpool("list", "-H", "-o", "name", check=False, op="read")
            if rc != 0:
                return []
            roots = [line.strip() for line in stdout.splitlines() if line.strip()]

        names: List[str] = []
        for root in roots:
            stdout, _, rc = await run_zfs(
                "list", "-H", "-o", "name", "-t", "filesystem", "-r", root, check=False, op="read",
            )
            if rc != 0:
                continue
            for line in stdout.splitlines():
                name = line.strip()
                if name and name != root:
                    names.append(name)
        return names

    async def list_exports(self, db: Session) -> List[Dict[str, Any]]:
        """List every dataset with its live sharenfs value."""
        rows = []
        for name in await self._list_dataset_names():
            sharenfs = await self._read_sharenfs(name)
            rows.append({
                "dataset_name": name,
                "export_path": f"/{name}",
                "sharenfs": sharenfs,
                "enabled": sharenfs not in ("off", ""),
            })
        return rows

    async def get_active_exports(self) -> List[Dict[str, Any]]:
        """List currently active NFS exports from the kernel export table."""
        try:
            stdout, stderr, returncode = await run_command(
                ["exportfs", "-v"], timeout=30, check=False
            )
            if returncode != 0:
                raise NfsError(f"Failed to list exports: {stderr}")

            exports = []
            for line in stdout.strip().split('\n'):
                if not line:
                    continue
                parts = line.strip().split()
                if len(parts) >= 2:
                    path = parts[0]
                    client_spec = parts[1]
                    options = ""
                    if '(' in client_spec and ')' in client_spec:
                        client_ip = client_spec.split('(')[0]
                        options = client_spec.split('(')[1].rstrip(')')
                    else:
                        client_ip = client_spec
                    exports.append({
                        "path": path,
                        "client": client_ip,
                        "options": options,
                    })
            return exports
        except Exception as e:
            if isinstance(e, NfsError):
                raise
            raise NfsError(f"Error getting active exports: {str(e)}")

    # -- CRUD --------------------------------------------------------------

    def _build_sharenfs_value(self, client_spec: str, options: Dict[str, bool]) -> str:
        """Build a ZFS sharenfs option string for the common-anon squash model.

        Standard options (rw/sync/async/no_subtree_check) plus a universal
        ``all_squash`` to the shared anon uid/gid, restricted to ``client_spec``.
        """
        tokens = [k for k, v in (options or {}).items() if v]
        # all_squash is implied by the shared-anon model; force it and pin ids.
        toks = [t for t in tokens if t not in ("all_squash", "root_squash", "no_root_squash")]
        parts = ",".join(toks)
        parts = f"{parts},{'all_squash'},anonuid={self.ANON_UID},anongid={self.ANON_GID}" if parts \
            else f"all_squash,anonuid={self.ANON_UID},anongid={self.ANON_GID}"
        return f"rw={client_spec},{parts}"

    async def set_export(
        self,
        db: Session,
        dataset_name: str,
        client_spec: str = None,
        options: Dict[str, bool] = None,
        sharenfs: str = None,
        enabled: bool = None,
    ) -> Dict[str, Any]:
        """Create or update a dataset's NFS share via the sharenfs property."""
        if not await self._dataset_exists(dataset_name):
            raise ValidationError(f"Dataset '{dataset_name}' not found")

        if not self.is_server_present():
            raise NfsError(
                "The NFS kernel server is not installed on this server. Run the "
                "NAZMan installer (build.sh, choosing to install NFS) or "
                "`sudo apt-get install -y nfs-kernel-server`."
            )

        if sharenfs is not None:
            value = sharenfs.strip()
        elif enabled is not None and enabled is False:
            value = "off"
        elif client_spec is not None:
            validate_ip_cidr(client_spec)
            value = self._build_sharenfs_value(client_spec, options)
        else:
            # No new sharing info provided: preserve the current value.
            current = await self._read_sharenfs(dataset_name)
            if enabled is True and current == "off":
                raise ValidationError(
                    "Provide client_spec and options to enable a disabled share"
                )
            value = current

        if value not in ("", "off"):
            await self._ensure_anon_user()
            await self._prepare_dataset_dir(dataset_name)

        await self._set_sharenfs(dataset_name, value)
        return {
            "dataset_name": dataset_name,
            "export_path": f"/{dataset_name}",
            "sharenfs": self._normalize_sharenfs(value),
            "enabled": value not in ("off", ""),
        }

    async def delete_export(self, db: Session, dataset_name: str) -> None:
        """Disable (unshare) a dataset's NFS share."""
        if not await self._dataset_exists(dataset_name):
            raise ValidationError(f"Dataset '{dataset_name}' not found")
        await self._set_sharenfs(dataset_name, "off")

    # -- pool lifecycle ----------------------------------------------------

    async def unexport_pool(self, db: Session, pool) -> None:
        """Disable NFS sharing for every dataset (and the pool root) in a pool.

        Releases NFS references (``zfs unshare``) so that ``zpool destroy`` is
        not blocked by a busy export. No separate /etc/exports handling is
        needed because ZFS owns the export table for sharenfs datasets.
        """
        names = set()
        if pool.name:
            names.add(pool.name)
        names.update(await self._list_dataset_names(pool.name))

        for name in names:
            try:
                await self._set_sharenfs(name, "off")
            except Exception:
                pass

    async def get_pool_export_info(self, db: Session, pool) -> Dict[str, Any]:
        """Return share status and active NFS clients for a pool's datasets."""
        dataset_names = await self._list_dataset_names(pool.name)
        export_paths = sorted({f"/{pool.name}"} | {f"/{d}" for d in dataset_names})

        exports = []
        for name in dataset_names:
            sharenfs = await self._read_sharenfs(name)
            exports.append({
                "export_path": f"/{name}",
                "dataset_name": name,
                "sharenfs": sharenfs,
                "enabled": sharenfs not in ("off", ""),
            })
        if pool.name and not any(e["dataset_name"] == pool.name for e in exports):
            root = await self._read_sharenfs(pool.name)
            if root not in ("off", ""):
                exports.append({
                    "export_path": f"/{pool.name}",
                    "dataset_name": pool.name,
                    "sharenfs": root,
                    "enabled": True,
                })

        active_clients = []
        try:
            stdout, _, rc = await run_command(
                ["showmount", "-a"], timeout=15, check=False
            )
            if rc == 0:
                for line in stdout.splitlines():
                    line = line.strip()
                    if not line or line.startswith("All mount") or ":" not in line:
                        continue
                    client, path = line.rsplit(":", 1)
                    path = path.strip()
                    if any(path == p or path.startswith(p + "/") for p in export_paths):
                        active_clients.append({"client": client.strip(), "path": path})
        except Exception:
            pass

        return {"exports": exports, "active_clients": active_clients}

    # -- anon identity scaffolding (kept for the common-anon squash model) --

    async def _ensure_anon_user(self) -> None:
        """Idempotently ensure the anonymous NFS user/group exists."""
        stdout, _, rc = await run_command(
            ["getent", "group", self.ANON_USER], timeout=10, check=False
        )
        if rc != 0:
            await run_command(
                ["groupadd", "-g", str(self.ANON_GID), self.ANON_USER], timeout=30
            )
        stdout, _, rc = await run_command(
            ["getent", "passwd", self.ANON_USER], timeout=10, check=False
        )
        if rc != 0:
            await run_command(
                [
                    "useradd", "-r", "-g", str(self.ANON_GID),
                    "-u", str(self.ANON_UID), "-M",
                    "-s", "/usr/sbin/nologin",
                    "-d", "/var/lib/nfs", self.ANON_USER,
                ],
                timeout=30,
            )

    async def _prepare_dataset_dir(self, dataset_name: str) -> None:
        """Make the shared directory writable by the anon user."""
        try:
            await run_command(["chown", f":{self.ANON_USER}", f"/{dataset_name}"], timeout=30, op="write", category="nfs")
            await run_command(["chmod", "2775", f"/{dataset_name}"], timeout=30, op="write", category="nfs")
        except Exception as e:
            raise NfsError(f"Failed to prepare dataset directory: {str(e)}")


# Singleton instance
nfs_manager = NfsManager()
