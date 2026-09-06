import pytest
from unittest.mock import patch, AsyncMock

from nazman.managers.nfs_manager import nfs_manager


@pytest.mark.asyncio
async def test_list_exports_empty(client):
    with patch("nazman.api.nfs.nfs_manager") as mock:
        mock.list_exports = AsyncMock(return_value=[])
        response = client.get("/api/nfs/")
        assert response.status_code == 200
        assert response.json() == []


@pytest.mark.asyncio
async def test_list_exports(client):
    with patch("nazman.api.nfs.nfs_manager") as mock:
        mock.list_exports = AsyncMock(return_value=[{
            "dataset_name": "testpool/data",
            "export_path": "/testpool/data",
            "sharenfs": "access=192.168.1.0/24,rw,no_subtree_check,all_squash,anonuid=65533,anongid=65533",
            "enabled": True,
            "paused": False,
        }])
        response = client.get("/api/nfs/")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["sharenfs"].startswith("access=192.168.1.0/24")
        assert data[0]["enabled"] is True


@pytest.mark.asyncio
async def test_list_active_exports(client):
    with patch("nazman.api.nfs.nfs_manager") as mock:
        mock.get_active_exports = AsyncMock(return_value=[
            {"path": "/testpool/data", "client": "192.168.1.0/24", "options": "rw,sync"}
        ])
        response = client.get("/api/nfs/active")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1


@pytest.mark.asyncio
async def test_create_export(client):
    with patch("nazman.api.nfs.nfs_manager") as mock:
        async def fake_set(db, dataset_name=None, client_spec=None, options=None, sharenfs=None, enabled=None):
            return {
                "dataset_name": dataset_name,
                "export_path": f"/{dataset_name}",
                "sharenfs": f"access={client_spec},rw,no_subtree_check,all_squash,anonuid=65533,anongid=65533",
                "enabled": True,
                "paused": False,
            }
        mock.set_export = AsyncMock(side_effect=fake_set)
        response = client.post("/api/nfs/", json={
            "dataset_name": "testpool/data",
            "client_spec": "192.168.1.0/24",
            "options": {"rw": True, "sync": True},
        })
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["sharenfs"].startswith("access=192.168.1.0/24")
        assert data["enabled"] is True


@pytest.mark.asyncio
async def test_update_export_enable_disable(client):
    with patch("nazman.api.nfs.nfs_manager") as mock:
        async def fake_set(db, dataset_name=None, client_spec=None, options=None, sharenfs=None, enabled=None):
            return {
                "dataset_name": dataset_name,
                "export_path": f"/{dataset_name}",
                "sharenfs": "access=192.168.1.0/24,rw,all_squash,anonuid=65533,anongid=65533",
                "enabled": False,
                "paused": True,
            }
        mock.set_export = AsyncMock(side_effect=fake_set)
        response = client.put("/api/nfs/testpool/data", json={"enabled": False})
        assert response.status_code == 200
        data = response.json()
        assert data["enabled"] is False
        assert data["paused"] is True


@pytest.mark.asyncio
async def test_delete_export(client):
    with patch("nazman.api.nfs.nfs_manager") as mock:
        mock.delete_export = AsyncMock(return_value=None)
        response = client.delete("/api/nfs/testpool/data")
        assert response.status_code == 200
        assert "removed" in response.json()["message"]


@pytest.mark.asyncio
async def test_list_active_exports_parses_real_exportfs_output():
    real_output = """/photolib1 \t192.168.32.0/20(rw,sync,wdelay,hide,nocrossmnt,secure,root_squash,no_all_squash,no_subtree_check,secure_locks,acl,anonuid=65534,anongid=65534,sec=sys,rw,secure,root_squash,no_all_squash)
/srv/nfs4 \t*(ro,sync,no_subtree_check)
"""

    async def fake_run_command(cmd, timeout=None, **kwargs):
        return real_output, "", 0

    with patch("nazman.managers.nfs_manager.run_command", side_effect=fake_run_command):
        result = await nfs_manager.get_active_exports()

    assert len(result) == 2
    assert result[0]["path"] == "/photolib1"
    assert result[0]["client"] == "192.168.32.0/20"
    assert "root_squash" in result[0]["options"]
    assert result[1]["path"] == "/srv/nfs4"
    assert result[1]["client"] == "*"


@pytest.mark.asyncio
async def test_build_sharenfs_value():
    manager = nfs_manager.__class__()
    value = manager._build_sharenfs_value(
        "192.168.1.0/24",
        {"rw": True, "sync": True, "no_subtree_check": True, "nohide": False}
    )
    assert value.startswith("access=192.168.1.0/24,rw")
    assert "all_squash" in value
    assert f"anonuid={manager.ANON_UID}" in value
    assert f"anongid={manager.ANON_GID}" in value
    assert "root_squash" not in value
    assert "rw=" not in value


@pytest.mark.asyncio
async def test_pause_unshares_and_preserves_options():
    manager = nfs_manager.__class__()
    calls = []

    async def fake_zfs(*args, **kw):
        calls.append(list(args))
        return ("", "", 0)

    async def fake_exists(*a, **k):
        return True

    async def fake_present(*a, **k):
        return True

    async def fake_read(*a, **k):
        return "access=192.168.1.0/24,rw,all_squash,anonuid=65533,anongid=65533"

    async def fake_active(*a, **k):
        return set()

    with patch.object(manager.__class__, "_dataset_exists", fake_exists), \
         patch.object(manager.__class__, "is_server_present", fake_present), \
         patch.object(manager.__class__, "_read_sharenfs", fake_read), \
         patch.object(manager.__class__, "_active_export_paths", fake_active), \
         patch("nazman.managers.nfs_manager.run_zfs", side_effect=fake_zfs):
        result = await manager.set_export(None, "testpool/data", enabled=False)

    assert calls == [["unshare", "testpool/data"]]
    assert result["enabled"] is False
    assert result["paused"] is True
    assert "192.168.1.0/24" in result["sharenfs"]


@pytest.mark.asyncio
async def test_resume_reshare_with_stored_options():
    manager = nfs_manager.__class__()
    calls = []

    async def fake_zfs(*args, **kw):
        calls.append(list(args))
        return ("", "", 0)

    async def fake_exists(*a, **k):
        return True

    async def fake_present(*a, **k):
        return True

    async def fake_read(*a, **k):
        return "access=192.168.1.0/24,rw,all_squash,anonuid=65533,anongid=65533"

    async def fake_active(*a, **k):
        return {"/testpool/data"}

    with patch.object(manager.__class__, "_dataset_exists", fake_exists), \
         patch.object(manager.__class__, "is_server_present", fake_present), \
         patch.object(manager.__class__, "_read_sharenfs", fake_read), \
         patch.object(manager.__class__, "_active_export_paths", fake_active), \
         patch("nazman.managers.nfs_manager.run_zfs", side_effect=fake_zfs):
        result = await manager.set_export(None, "testpool/data", enabled=True)

    assert calls == [["share", "testpool/data"]]
    assert result["enabled"] is True
    assert result["paused"] is False


@pytest.mark.asyncio
async def test_resume_off_share_raises():
    manager = nfs_manager.__class__()
    from nazman.utils.exceptions import ValidationError

    async def fake_exists(*a, **k):
        return True

    async def fake_present(*a, **k):
        return True

    async def fake_read(*a, **k):
        return "off"

    with patch.object(manager.__class__, "_dataset_exists", fake_exists), \
         patch.object(manager.__class__, "is_server_present", fake_present), \
         patch.object(manager.__class__, "_read_sharenfs", fake_read):
        with pytest.raises(ValidationError, match="recreate"):
            await manager.set_export(None, "testpool/data", enabled=True)


@pytest.mark.asyncio
async def test_set_export_uses_access_and_ensures_anon():
    manager = nfs_manager.__class__()
    calls = []

    async def fake_zfs(*args, **kw):
        calls.append(list(args))
        return ("", "", 0)

    async def fake_exists(*a, **k):
        return True

    async def fake_present(*a, **k):
        return True

    async def fake_active(self=None):
        return {"/testpool/data"}

    async def fake_anon(self=None, *a, **k):
        pass

    async def fake_prep(self=None, *a, **k):
        pass

    with patch.object(manager.__class__, "_dataset_exists", fake_exists), \
         patch.object(manager.__class__, "is_server_present", fake_present), \
         patch.object(manager.__class__, "_active_export_paths", fake_active), \
         patch.object(manager.__class__, "_ensure_anon_user", fake_anon), \
         patch.object(manager.__class__, "_prepare_dataset_dir", fake_prep), \
         patch("nazman.managers.nfs_manager.run_zfs", side_effect=fake_zfs):
        result = await manager.set_export(
            None, "testpool/data", client_spec="192.168.1.0/24",
            options={"sync": True},
        )
    set_call = next(c for c in calls if c[0] == "set")
    assert set_call[1].startswith("sharenfs=access=192.168.1.0/24,rw")
    assert result["enabled"] is True


@pytest.mark.asyncio
async def test_list_exports_excludes_off_and_flags_paused():
    manager = nfs_manager.__class__()

    async def fake_names(*a, **k):
        return ["testpool/a", "testpool/b"]

    async def fake_read(*a, **k):
        name = a[-1] if a else k.get("dataset_name")
        return {
            "testpool/a": "access=192.168.1.0/24,rw,all_squash,anonuid=65533,anongid=65533",
            "testpool/b": "off",
        }[name]

    async def fake_active(*a, **k):
        return {"/testpool/a"}

    with patch.object(manager.__class__, "_list_dataset_names", fake_names), \
         patch.object(manager.__class__, "_read_sharenfs", fake_read), \
         patch.object(manager.__class__, "_active_export_paths", fake_active):
        rows = await manager.list_exports(None)

    assert [r["dataset_name"] for r in rows] == ["testpool/a"]
    assert rows[0]["enabled"] is True
    assert rows[0]["paused"] is False


@pytest.mark.asyncio
async def test_get_presence(client):
    with patch("nazman.api.nfs.nfs_manager") as mock:
        mock.is_server_present = lambda: True
        response = client.get("/api/nfs/presence")
        assert response.status_code == 200
        assert response.json() == {"installed": True}


@pytest.mark.asyncio
async def test_install_server(client):
    with patch("nazman.api.nfs.nfs_manager") as mock:
        mock.install_server = AsyncMock(
            return_value={"installed": True, "message": "NFS kernel server installed successfully."})
        response = client.post("/api/nfs/install")
        assert response.status_code == 200
        assert response.json()["installed"] is True


@pytest.mark.asyncio
async def test_install_server_error(client):
    from nazman.utils.exceptions import NfsError
    with patch("nazman.api.nfs.nfs_manager") as mock:
        mock.install_server = AsyncMock(side_effect=NfsError("apt failed"))
        response = client.post("/api/nfs/install")
        assert response.status_code == 400
        assert "apt failed" in response.json()["detail"]


@pytest.mark.asyncio
async def test_install_server_already_installed(client):
    with patch("nazman.api.nfs.nfs_manager") as mock:
        mock.install_server = AsyncMock(
            return_value={"installed": True, "message": "The NFS kernel server is already installed."})
        response = client.post("/api/nfs/install")
        assert response.status_code == 200
        assert response.json()["installed"] is True


@pytest.mark.asyncio
async def test_nfs_install_server_runs_apt_and_systemctl():
    manager = nfs_manager.__class__()
    calls = []

    async def fake_cmd(cmd, **kw):
        calls.append(cmd)
        return ("", "", 0)

    async def fake_zfs(*args, **kw):
        calls.append(["zfs"] + list(args))
        return ("", "", 0)

    with patch.object(manager.__class__, "is_server_present", side_effect=[False, True, True]), \
         patch("nazman.managers.nfs_manager.run_command", side_effect=fake_cmd), \
         patch("nazman.managers.nfs_manager.run_zfs", side_effect=fake_zfs), \
         patch("nazman.managers.nfs_manager.shutil.which", return_value="/usr/bin/apt-get"):
        result = await manager.install_server()

    assert calls[0] == ["apt-get", "update"]
    assert calls[1] == ["apt-get", "install", "-y", "nfs-kernel-server"]
    assert ["systemctl", "enable", "nfs-kernel-server"] in calls
    assert ["systemctl", "start", "nfs-kernel-server"] in calls
    assert ["modprobe", "nfsd"] in calls
    assert ["zfs", "share", "-a"] in calls
    assert result["installed"] is True
    assert "installed successfully" in result["message"]


@pytest.mark.asyncio
async def test_nfs_install_server_installs_reshare_dropin():
    manager = nfs_manager.__class__()
    written = []

    async def fake_cmd(cmd, **kw):
        if cmd[0] == "tee":
            written.append((cmd, kw.get("input")))
        return ("", "", 0)

    async def fake_zfs(*args, **kw):
        return ("", "", 0)

    with patch.object(manager.__class__, "is_server_present", side_effect=[False, True, True]), \
         patch("nazman.managers.nfs_manager.run_command", side_effect=fake_cmd), \
         patch("nazman.managers.nfs_manager.run_zfs", side_effect=fake_zfs), \
         patch("nazman.managers.nfs_manager.shutil.which", return_value="/usr/bin/apt-get"):
        await manager.install_server()

    assert len(written) == 1
    cmd, content = written[0]
    assert cmd == ["tee", "/etc/systemd/system/nfs-server.service.d/zfs-share.conf"]
    assert "ExecStartPost=/usr/sbin/zfs share -a" in content


@pytest.mark.asyncio
async def test_nfs_install_server_no_apt():
    manager = nfs_manager.__class__()
    from nazman.utils.exceptions import NfsError
    with patch.object(manager.__class__, "is_server_present", return_value=False), \
         patch("nazman.managers.nfs_manager.shutil.which", return_value=None):
        with pytest.raises(NfsError, match="apt-get"):
            await manager.install_server()
