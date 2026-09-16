"""Shared OS-level identity provisioning.

Both NFS (``all_squash``) and SMB (``force user``) rely on one shared
anonymous identity so clients' reads/writes land on the same POSIX user.
Ensuring that identity and preparing dataset directories is one routine,
kept here so the two sharing managers cannot drift apart.
"""

from .commands import run_command

ANON_USER = "nfsanon"
ANON_UID = 65533
ANON_GID = 65533


async def ensure_anon_user() -> None:
    """Idempotently ensure the shared anonymous user/group exists."""
    _, _, rc = await run_command(
        ["getent", "group", ANON_USER], timeout=10, check=False
    )
    if rc != 0:
        await run_command(
            ["groupadd", "-g", str(ANON_GID), ANON_USER], timeout=30
        )
    _, _, rc = await run_command(
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


async def prepare_dataset_dir(dataset_name: str, category: str) -> None:
    """Make a shared dataset directory group-writable by the anon user."""
    await run_command(
        ["chown", f":{ANON_USER}", f"/{dataset_name}"],
        timeout=30, op="write", category=category,
    )
    await run_command(
        ["chmod", "2775", f"/{dataset_name}"],
        timeout=30, op="write", category=category,
    )
