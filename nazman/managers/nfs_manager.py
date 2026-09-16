from typing import List, Dict, Any, Optional
import os
import shutil

from sqlalchemy.orm import Session

from ..utils.commands import run_zfs, run_command
from ..utils.exceptions import NfsError, ValidationError
from ..utils.validation import validate_ip_cidr
from ..utils import provisioning, zfs_query


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
    ANON_USER = provisioning.ANON_USER
    ANON_UID = provisioning.ANON_UID
    ANON_GID = provisioning.ANON_GID

    # zfs-share.service runs `zfs share -a` before nfs-server.service, so on
    # slow boots the kernel export table can be left empty (exportfs -r only
    # re-reads /etc/exports, not ZFS's etab). A drop-in re-shares after start.
    RESHARE_UNIT_DIR = "/etc/systemd/system/nfs-server.service.d"
    RESHARE_DROP_IN = "zfs-share.conf"

    # Content of the ExecStartPost unit drop-in.
    @staticmethod
    def _reshare_drop_in_content() -> str:
        return (
            "# Re-register ZFS sharenfs exports once the NFS server is up; a\n"
            "# reboot may otherwise leave the kernel export table empty.\n"
            "[Service]\n"
            "ExecStartPost=/usr/sbin/zfs share -a\n"
        )

    async def _install_reshare_hook(self) -> None:
        """Install a drop-in that re-shares ZFS exports whenever nfs-server starts."""
        await run_command(["mkdir", "-p", self.RESHARE_UNIT_DIR], timeout=30, op="system", category="nfs")
        await run_command(
            ["tee", os.path.join(self.RESHARE_UNIT_DIR, self.RESHARE_DROP_IN)],
            input=self._reshare_drop_in_content(),
            timeout=30, op="system", category="nfs",
        )
        await run_command(["systemctl", "daemon-reload"], timeout=30, op="system", category="nfs")

    # -- presence / server readiness --------------------------------------

    @staticmethod
    def is_server_present() -> bool:
        """True if the NFS kernel server (exportfs) is installed on this host."""
        import os

        return any(os.path.isfile(p) for p in ("/usr/sbin/exportfs", "/usr/bin/exportfs"))

    async def install_server(self) -> Dict[str, Any]:
        """Install the NFS kernel server on this host via apt.

        Idempotent: returns immediately when ``exportfs`` is already present.
        Installs ``nfs-kernel-server``, ensures the ``nfsd`` kernel module is
        loaded (NFSv4), enables/starts the service, and ensures the shared
        anonymous identity used by NAZMan's shares exists.
        """
        if self.is_server_present():
            return {"installed": True, "message": "The NFS kernel server is already installed."}

        if shutil.which("apt-get") is None:
            raise NfsError(
                "The NFS kernel server is not installed and apt-get is not "
                "available on this server. Install the `nfs-kernel-server` "
                "package manually."
            )

        env = {"DEBIAN_FRONTEND": "noninteractive"}

        _, stderr, rc = await run_command(
            ["apt-get", "update"], timeout=600, check=False,
            env=env, op="system", category="nfs",
        )
        if rc != 0:
            raise NfsError(f"apt-get update failed: {stderr.strip()}")

        _, stderr, rc = await run_command(
            ["apt-get", "install", "-y", "nfs-kernel-server"], timeout=600, check=False,
            env=env, op="system", category="nfs",
        )
        if rc != 0:
            raise NfsError(f"Failed to install nfs-kernel-server: {stderr.strip()}")

        # NFSv4 is served by the nfsd kernel module; load it if not present so
        # /proc/fs/nfsd is available to the running server.
        await run_command(["modprobe", "nfsd"], timeout=30, check=False, op="system", category="nfs")
        await run_command(["systemctl", "enable", "nfs-kernel-server"], timeout=60, op="system", category="nfs")
        await run_command(["systemctl", "start", "nfs-kernel-server"], timeout=60, op="system", category="nfs")

        # nfs-server runs `exportfs -r` (only /etc/exports) on start, which can
        # drop ZFS shares registered before the server was up; re-share now and
        # keep a drop-in that does so on every future start.
        await self._install_reshare_hook()
        await run_zfs("share", "-a", check=False, category="nfs")

        await self._ensure_anon_user()

        return {
            "installed": self.is_server_present(),
            "message": "NFS kernel server installed successfully." if self.is_server_present()
            else "NFS install completed but exportfs was not found.",
        }

    # -- live property access ---------------------------------------------

    @staticmethod
    def _normalize_sharenfs(value: str) -> str:
        """A bare ``-``/``on``/``off``/empty sharenfs becomes ``"off"`` if not shared."""
        v = (value or "").strip()
        if v in ("-", ""):
            return "off"
        return v

    async def read_sharenfs(self, dataset_name: str) -> str:
        stdout, _, rc = await run_zfs(
            "get", "-H", "-o", "value", "sharenfs", dataset_name, check=False, op="read",
        )
        return self._normalize_sharenfs(stdout)

    async def _dataset_exists(self, dataset_name: str) -> bool:
        return await zfs_query.dataset_exists(dataset_name)

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

    async def list_dataset_names(self, pool_name: Optional[str] = None) -> List[str]:
        """Full ZFS names of every dataset (filesystem), excluding pool roots.

        Dataset existence is derived live from ZFS: no database copy exists.
        When ``pool_name`` is given, only that pool's children are returned.
        """
        if pool_name:
            return await zfs_query.list_filesystem_names(pool_name)
        return await zfs_query.all_filesystem_names()

    async def _active_export_paths(self) -> set:
        """Set of currently active export paths from the kernel export table."""
        try:
            stdout, _, rc = await run_command(["exportfs", "-v"], timeout=30, check=False)
            if rc != 0:
                return set()
            paths = set()
            for line in stdout.splitlines():
                line = line.strip()
                if line:
                    paths.add(line.split()[0])
            return paths
        except Exception:
            return set()

    async def list_exports(self, db: Session) -> List[Dict[str, Any]]:
        """List every dataset with NFS sharing configured (sharenfs not off).

        Fully-disabled datasets are omitted so a deleted share disappears; a
        paused share (options retained, export unshared) is shown as paused.
        """
        active = await self._active_export_paths()
        rows = []
        for name in await self.list_dataset_names():
            sharenfs = await self.read_sharenfs(name)
            if sharenfs in ("", "off"):
                continue
            rows.append({
                "dataset_name": name,
                "export_path": f"/{name}",
                "sharenfs": sharenfs,
                "enabled": f"/{name}" in active,
                "paused": f"/{name}" not in active,
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
        ``all_squash`` to the shared anon uid/gid, with mount access restricted
        to ``client_spec``. ``access=`` is used instead of ``rw=`` so the export
        is limited to the named clients; a bare ``rw=@net`` would otherwise add
        an implicit wildcard entry for every other host.
        """
        tokens = [k for k, v in (options or {}).items() if v]
        # all_squash is implied by the shared-anon model; force it and pin ids.
        toks = [t for t in tokens if t not in ("all_squash", "root_squash", "no_root_squash")]
        parts = ",".join(toks)
        parts = f"{parts},{'all_squash'},anonuid={self.ANON_UID},anongid={self.ANON_GID}" if parts \
            else f"all_squash,anonuid={self.ANON_UID},anongid={self.ANON_GID}"
        return f"access={client_spec},rw,{parts}"

    async def _export_status(self, dataset_name: str, sharenfs_value: str) -> Dict[str, Any]:
        """Live status of a dataset's share: enabled (in kernel) or paused."""
        active = await self._active_export_paths()
        path = f"/{dataset_name}"
        normalized = self._normalize_sharenfs(sharenfs_value)
        return {
            "dataset_name": dataset_name,
            "export_path": path,
            "sharenfs": normalized,
            "enabled": path in active,
            "paused": path not in active and normalized != "off",
        }

    async def set_export(
        self,
        db: Session,
        dataset_name: str,
        client_spec: str = None,
        options: Dict[str, bool] = None,
        sharenfs: str = None,
        enabled: bool = None,
    ) -> Dict[str, Any]:
        """Create, update, pause or resume a dataset's NFS share.

        Creating/updating sets the ``sharenfs`` property (ZFS is the source of
        truth). Pausing (``enabled=False``) only runs ``zfs unshare`` so the
        configured options survive and the share can be resumed. A share whose
        options were discarded (deleted) cannot be resumed and must be created.
        """
        if not await self._dataset_exists(dataset_name):
            raise ValidationError(f"Dataset '{dataset_name}' not found")

        if not self.is_server_present():
            raise NfsError(
                "The NFS kernel server is not installed on this server. Run the "
                "NAZMan installer (build.sh, choosing to install NFS) or "
                "`sudo apt-get install -y nfs-kernel-server`."
            )

        if enabled is False:
            await run_zfs("unshare", dataset_name, check=False)
            return await self._export_status(dataset_name, await self.read_sharenfs(dataset_name))

        if sharenfs is not None:
            value = sharenfs.strip()
        elif client_spec is not None:
            validate_ip_cidr(client_spec)
            value = self._build_sharenfs_value(client_spec, options)
        else:
            # No new config supplied: re-share with the stored options, or
            # refuse to enable a share whose settings were discarded.
            current = await self.read_sharenfs(dataset_name)
            if current in ("", "off"):
                if enabled is True:
                    raise ValidationError(
                        "This share has no stored configuration; recreate it to enable it."
                    )
                return await self._export_status(dataset_name, current)
            await run_zfs("share", dataset_name, check=False)
            return await self._export_status(dataset_name, current)

        if value not in ("", "off"):
            await self._ensure_anon_user()
            await self._prepare_dataset_dir(dataset_name)

        await self._set_sharenfs(dataset_name, value)
        return await self._export_status(dataset_name, value)

    async def delete_export(self, db: Session, dataset_name: str) -> None:
        """Permanently remove a dataset's NFS share (sharenfs -> off)."""
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
        names.update(await self.list_dataset_names(pool.name))

        for name in names:
            try:
                await self._set_sharenfs(name, "off")
            except Exception:
                pass

    async def get_pool_export_info(self, db: Session, pool) -> Dict[str, Any]:
        """Return share status and active NFS clients for a pool's datasets."""
        dataset_names = await self.list_dataset_names(pool.name)
        export_paths = sorted({f"/{pool.name}"} | {f"/{d}" for d in dataset_names})
        active = await self._active_export_paths()

        exports = []
        for name in dataset_names:
            sharenfs = await self.read_sharenfs(name)
            if sharenfs in ("", "off"):
                continue
            path = f"/{name}"
            exports.append({
                "export_path": path,
                "dataset_name": name,
                "sharenfs": sharenfs,
                "enabled": path in active,
                "paused": path not in active,
            })
        if pool.name:
            root = await self.read_sharenfs(pool.name)
            if root not in ("", "off"):
                path = f"/{pool.name}"
                exports.append({
                    "export_path": path,
                    "dataset_name": pool.name,
                    "sharenfs": root,
                    "enabled": path in active,
                    "paused": path not in active,
                })

        active_clients = await zfs_query.showmount_clients(export_paths)

        return {"exports": exports, "active_clients": active_clients}

    # -- anon identity scaffolding (kept for the common-anon squash model) --

    async def _ensure_anon_user(self) -> None:
        """Idempotently ensure the anonymous NFS user/group exists."""
        await provisioning.ensure_anon_user()

    async def _prepare_dataset_dir(self, dataset_name: str) -> None:
        """Make the shared directory writable by the anon user."""
        try:
            await provisioning.prepare_dataset_dir(dataset_name, category="nfs")
        except Exception as e:
            raise NfsError(f"Failed to prepare dataset directory: {str(e)}")

