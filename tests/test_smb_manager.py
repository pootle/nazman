import os
import pytest
from unittest.mock import patch, AsyncMock

from nazman.managers.smb_manager import (
    SmbManager, smb_manager, _MARKER_BEGIN, _MARKER_END, ANON_USER,
)


def _write_conf(tmp_path, content="[global]\n\tworkgroup = WORKGROUP\n\tsecurity = user\n"):
    conf = tmp_path / "smb.conf"
    conf.write_text(content, encoding="utf-8")
    os.environ["NASMAN_SMB_CONF"] = str(conf)
    return str(conf)


@pytest.fixture()
def manager(tmp_path):
    _write_conf(tmp_path)
    yield smb_manager
    os.environ.pop("NASMAN_SMB_CONF", None)


def test_share_name_for():
    assert SmbManager.share_name_for("pool/data") == "data"
    assert SmbManager.share_name_for("pool/My Dataset") == "my_dataset"
    assert SmbManager.share_name_for("pool/root") == "root"


def test_add_share_rewrites_region_and_preserves_global(manager, tmp_path):
    conf = manager.conf_path()
    manager._rewrite_region(conf, set_block=manager._render_share_block("pool/data", False, True))

    shares = manager.list_shares(None)
    assert len(shares) == 1
    assert shares[0]["dataset_name"] == "pool/data"
    assert shares[0]["enabled"] is True
    assert shares[0]["read_only"] is False

    text = open(conf, encoding="utf-8").read()
    assert "workgroup = WORKGROUP" in text
    assert "security = user" in text
    assert text.count(_MARKER_BEGIN) == 1


def test_update_share_preserves_other_shares(manager, tmp_path):
    conf = manager.conf_path()
    manager._rewrite_region(conf, set_block=manager._render_share_block("pool/data", False, True))
    manager._rewrite_region(conf, set_block=manager._render_share_block("pool/docs", False, True))
    manager._rewrite_region(conf, set_block=manager._render_share_block("pool/data", True, True))

    shares = {s["dataset_name"]: s for s in manager.list_shares(None)}
    assert set(shares) == {"pool/data", "pool/docs"}
    assert shares["pool/data"]["read_only"] is True
    assert shares["pool/data"]["enabled"] is True


def test_disable_share(manager, tmp_path):
    conf = manager.conf_path()
    manager._rewrite_region(conf, set_block=manager._render_share_block("pool/data", False, False))
    shares = manager.list_shares(None)
    assert shares[0]["enabled"] is False
    assert shares[0]["dataset_name"] == "pool/data"


def test_delete_share(manager, tmp_path):
    conf = manager.conf_path()
    manager._rewrite_region(conf, set_block=manager._render_share_block("pool/data", False, True))
    manager._rewrite_region(conf, set_block=manager._render_share_block("pool/docs", False, True))
    manager._rewrite_region(conf, remove_dataset="pool/data")

    shares = manager.list_shares(None)
    assert [s["dataset_name"] for s in shares] == ["pool/docs"]


@pytest.mark.asyncio
async def test_set_share_requires_samba_installed(tmp_path):
    conf = _write_conf(tmp_path)
    manager = SmbManager()
    with patch.object(SmbManager, "is_server_present", return_value=False), \
         patch("nazman.managers.smb_manager.run_zfs",
               AsyncMock(side_effect=lambda *a, **k: ("pool/data", "", 0))):
        from nazman.utils.exceptions import SmbError
        with pytest.raises(SmbError, match="Samba is not installed"):
            await manager.set_share(None, "pool/data")


@pytest.mark.asyncio
async def test_set_share_unknown_dataset(tmp_path):
    _write_conf(tmp_path)
    manager = SmbManager()
    with patch("nazman.managers.smb_manager.run_zfs",
               AsyncMock(side_effect=lambda *a, **k: ("", "", 1))):
        from nazman.utils.exceptions import ValidationError
        with pytest.raises(ValidationError, match="not found"):
            await manager.set_share(None, "nope/missing")


@pytest.mark.asyncio
async def test_set_share_end_to_end(tmp_path):
    conf = _write_conf(tmp_path)
    manager = smb_manager.__class__()
    os.environ["NASMAN_SMB_CONF"] = conf

    def fake_run_zfs(*args, **kwargs):
        if args and args[0] == "list":
            return ("pool/data\n", "", 0)
        return ("", "", 0)

    async def fake_cmd(cmd, timeout=None, check=None, **kw):
        # getent for the anon user present; chown/chmod/testparm/smbcontrol succeed.
        if cmd and cmd[0] == "getent":
            return (ANON_USER + "\n", "", 0)
        return ("", "", 0)

    with patch.object(SmbManager, "is_server_present", return_value=True), \
         patch("nazman.managers.smb_manager.run_zfs", side_effect=fake_run_zfs), \
         patch("nazman.managers.smb_manager.run_command", side_effect=fake_cmd):
        result = await manager.set_share(None, "pool/data", read_only=False, enabled=True)

    assert result["dataset_name"] == "pool/data"
    assert result["share_name"] == "data"
    assert result["enabled"] is True

    shares = manager.list_shares(None)
    assert len(shares) == 1
    assert shares[0]["dataset_name"] == "pool/data"
    os.environ.pop("NASMAN_SMB_CONF", None)


@pytest.mark.asyncio
async def test_install_server_already_installed(tmp_path):
    _write_conf(tmp_path)
    manager = SmbManager()
    with patch.object(SmbManager, "is_server_present", return_value=True):
        result = await manager.install_server()
    assert result["installed"] is True
    assert "already installed" in result["message"]


@pytest.mark.asyncio
async def test_install_server_runs_apt_and_systemctl(tmp_path):
    _write_conf(tmp_path)
    manager = SmbManager()
    calls = []

    async def fake_cmd(cmd, **kw):
        calls.append(cmd)
        return ("", "", 0)

    with patch.object(SmbManager, "is_server_present", side_effect=[False, True, True]), \
         patch("nazman.managers.smb_manager.run_command", side_effect=fake_cmd), \
         patch("nazman.managers.smb_manager.shutil.which", return_value="/usr/bin/apt-get"):
        result = await manager.install_server()

    assert calls[0] == ["apt-get", "update"]
    assert calls[1] == ["apt-get", "install", "-y", "samba"]
    assert ["systemctl", "enable", "smbd"] in calls
    assert ["systemctl", "start", "nmbd"] in calls
    assert result["installed"] is True
    assert "installed successfully" in result["message"]


@pytest.mark.asyncio
async def test_install_server_no_apt(tmp_path):
    _write_conf(tmp_path)
    manager = SmbManager()
    from nazman.utils.exceptions import SmbError
    with patch.object(SmbManager, "is_server_present", return_value=False), \
         patch("nazman.managers.smb_manager.shutil.which", return_value=None):
        with pytest.raises(SmbError, match="apt-get"):
            await manager.install_server()


@pytest.mark.asyncio
async def test_install_server_apt_failure(tmp_path):
    _write_conf(tmp_path)
    manager = SmbManager()
    from nazman.utils.exceptions import SmbError

    async def fake_cmd(cmd, **kw):
        if cmd and cmd[0] == "apt-get" and cmd[1] == "install":
            return ("", "apt error", 100)
        return ("", "", 0)

    with patch.object(SmbManager, "is_server_present", side_effect=[False, False]), \
         patch("nazman.managers.smb_manager.run_command", side_effect=fake_cmd), \
         patch("nazman.managers.smb_manager.shutil.which", return_value="/usr/bin/apt-get"):
        with pytest.raises(SmbError, match="Failed to install samba"):
            await manager.install_server()


@pytest.mark.asyncio
async def test_reload_validation_failure(tmp_path):
    _write_conf(tmp_path)
    manager = smb_manager.__class__()
    os.environ["NASMAN_SMB_CONF"] = _write_conf(tmp_path)
    from nazman.utils.exceptions import SmbError

    async def fake_cmd(cmd, timeout=None, check=None, **kw):
        if cmd and cmd[0] == "testparm":
            return ("", "bad option", 1)
        return ("", "", 0)

    with patch("nazman.managers.smb_manager.run_command", side_effect=fake_cmd):
        with pytest.raises(SmbError, match="validation"):
            await manager._reload()