import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from tests.conftest import mock_run_command, mock_run_zpool
from nazman.utils.command_log import command_log


@pytest.fixture(autouse=True)
def _reset_command_log():
    command_log.reset()
    yield
    command_log.reset()


@pytest.mark.asyncio
async def test_health_check(client):
    response = client.get("/api/system/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert "timestamp" in data


@pytest.mark.asyncio
async def test_get_system_status(client):
    with patch("nazman.api.system.psutil") as mock_psutil:
        mock_psutil.cpu_percent.return_value = 25.0
        mock_psutil.virtual_memory.return_value = MagicMock(
            total=8_000_000_000,
            available=4_000_000_000,
            percent=50.0,
        )
        mock_psutil.disk_usage.return_value = MagicMock(
            total=100_000_000_000,
            used=50_000_000_000,
            free=50_000_000_000,
            percent=50.0,
        )

        with patch("nazman.api.system.zfs_manager") as mock_zfs:
            mock_zfs.list_pools = AsyncMock(return_value=[])
            with patch("nazman.api.system.disk_manager") as mock_disk:
                mock_disk.sync_disks_to_database = AsyncMock(return_value=[])

                response = client.get("/api/system/status")
                assert response.status_code == 200
                data = response.json()
                assert "system" in data
                assert "storage" in data
                assert data["system"]["cpu_percent"] == 25.0
                assert data["storage"]["pool_count"] == 0


@pytest.mark.asyncio
async def test_get_system_metrics(client):
    fake_cpu = [{"ts": 1000.0, "value": 10.0}, {"ts": 1005.0, "value": 42.0}]
    fake_mem = [{"ts": 1000.0, "value": 20.0}, {"ts": 1005.0, "value": 50.0}]

    with patch("nazman.api.system.metrics_manager") as mock_metrics, \
         patch("nazman.api.system.psutil") as mock_psutil:
        mock_metrics.get_series.side_effect = lambda name: fake_cpu if name == "cpu" else fake_mem
        mock_psutil.virtual_memory.return_value = MagicMock(
            total=8_000_000_000,
            used=4_000_000_000,
            percent=50.0,
        )

        response = client.get("/api/system/metrics")
        assert response.status_code == 200
        data = response.json()
        assert data["history"]["cpu"] == fake_cpu
        assert data["history"]["memory"] == fake_mem
        assert data["memory"]["total"] == 8_000_000_000
        assert data["memory"]["used"] == 4_000_000_000
        assert data["memory"]["percent"] == 50.0


@pytest.mark.asyncio
async def test_get_system_metrics_includes_net_disks_pools(client, db_session):
    from nazman.models.pool import Pool
    pool = Pool(name="testpool")
    db_session.add(pool)
    db_session.commit()

    fake_cpu = [{"ts": 1.0, "value": 10.0}]
    fake_mem = [{"ts": 1.0, "value": 20.0}]
    fake_net = [{"ts": 1.0, "value": 30.0}]
    fake_disk_series = {"sda": [{"ts": 1.0, "value": 40.0}]}

    with patch("nazman.api.system.metrics_manager") as mock_metrics, \
         patch("nazman.api.system.psutil") as mock_psutil, \
         patch("nazman.api.system.get_disk_series_names") as mock_disk_names, \
         patch("nazman.api.system.list_network_interfaces") as mock_ifaces, \
         patch("nazman.api.system.get_selected_network_interface") as mock_sel, \
         patch("nazman.api.system.zfs_manager") as mock_zfs:
        mock_metrics.get_series.side_effect = lambda name: {
            "cpu": fake_cpu, "memory": fake_mem, "net": fake_net,
            "disk_sda": fake_disk_series["sda"],
        }.get(name, [])
        mock_disk_names.return_value = {"sda": "disk_sda"}
        mock_ifaces.return_value = [{"name": "eth0", "speed_mbps": 1000, "up": True}]
        mock_sel.return_value = "eth0"
        mock_zfs.get_pool_status = AsyncMock(return_value={
            "data_vdevs": [{"children": [{"name": "sda1", "state": "ONLINE", "path": "", "size": ""}]}],
        })
        mock_psutil.virtual_memory.return_value = MagicMock(
            total=8_000_000_000, used=4_000_000_000, percent=50.0,
        )

        response = client.get("/api/system/metrics")
        assert response.status_code == 200
        data = response.json()
        assert data["net"] == fake_net
        assert data["disks"]["sda"] == fake_disk_series["sda"]
        assert data["pools"]["testpool"] == ["sda"]
        assert data["interfaces"][0]["name"] == "eth0"
        assert data["selected_interface"] == "eth0"


@pytest.mark.asyncio
async def test_get_system_metrics_pool_disks_priority_and_cap(client, db_session):
    """Pool disk mapping must prioritise data > special > log/cache and cap at 4."""
    from nazman.models.pool import Pool
    pool = Pool(name="bigpool")
    db_session.add(pool)
    db_session.commit()

    base_names = [f"sd{chr(97 + i)}" for i in range(6)]  # sda..sdf
    disk_series = {b: [{"ts": 1.0, "value": float(i)}] for i, b in enumerate(base_names)}

    def fake_metrics_get(name):
        if not name.startswith("disk_"):
            return []
        b = name[len("disk_"):]
        return disk_series.get(b, [])

    with patch("nazman.api.system.metrics_manager") as mock_metrics, \
         patch("nazman.api.system.psutil") as mock_psutil, \
         patch("nazman.api.system.get_disk_series_names") as mock_disk_names, \
         patch("nazman.api.system.list_network_interfaces") as mock_ifaces, \
         patch("nazman.api.system.get_selected_network_interface") as mock_sel, \
         patch("nazman.api.system.zfs_manager") as mock_zfs:
        mock_metrics.get_series.side_effect = fake_metrics_get
        mock_disk_names.return_value = {b: f"disk_{b}" for b in base_names}
        mock_ifaces.return_value = []
        mock_sel.return_value = None
        mock_zfs.get_pool_status = AsyncMock(return_value={
            "data_vdevs": [{"children": [
                {"name": "sda", "state": "ONLINE", "path": "", "size": ""},
                {"name": "sdb", "state": "ONLINE", "path": "", "size": ""},
            ]}],
            "special_vdevs": [{"children": [
                {"name": "sdc", "state": "ONLINE", "path": "", "size": ""},
                {"name": "sdd", "state": "ONLINE", "path": "", "size": ""},
            ]}],
            "log_vdevs": [{"children": [
                {"name": "sde", "state": "ONLINE", "path": "", "size": ""},
            ]}],
            "cache_vdevs": [{"children": [
                {"name": "sdf", "state": "ONLINE", "path": "", "size": ""},
            ]}],
        })
        mock_psutil.virtual_memory.return_value = MagicMock(
            total=8_000_000_000, used=4_000_000_000, percent=50.0,
        )

        response = client.get("/api/system/metrics")
        assert response.status_code == 200
        data = response.json()
        # data first (sda, sdb), then special (sdc, sdd), capped at 4 — log/cache excluded.
        assert data["pools"]["bigpool"] == ["sda", "sdb", "sdc", "sdd"]


@pytest.mark.asyncio
async def test_get_system_metrics_pool_disks_dedupe(client, db_session):
    """The same disk appearing in data and special vdevs must be listed only once."""
    from nazman.models.pool import Pool
    pool = Pool(name="dedupool")
    db_session.add(pool)
    db_session.commit()

    def fake_metrics_get(name):
        return []

    with patch("nazman.api.system.metrics_manager") as mock_metrics, \
         patch("nazman.api.system.psutil") as mock_psutil, \
         patch("nazman.api.system.get_disk_series_names") as mock_disk_names, \
         patch("nazman.api.system.list_network_interfaces") as mock_ifaces, \
         patch("nazman.api.system.get_selected_network_interface") as mock_sel, \
         patch("nazman.api.system.zfs_manager") as mock_zfs:
        mock_metrics.get_series.side_effect = fake_metrics_get
        mock_disk_names.return_value = {"sda": "disk_sda", "sdb": "disk_sdb"}
        mock_ifaces.return_value = []
        mock_sel.return_value = None
        mock_zfs.get_pool_status = AsyncMock(return_value={
            "data_vdevs": [{"children": [{"name": "sda", "state": "ONLINE", "path": "", "size": ""}]}],
            "special_vdevs": [{"children": [{"name": "sda", "state": "ONLINE", "path": "", "size": ""}]}],
            "log_vdevs": [], "cache_vdevs": [],
        })
        mock_psutil.virtual_memory.return_value = MagicMock(
            total=8_000_000_000, used=4_000_000_000, percent=50.0,
        )

        response = client.get("/api/system/metrics")
        assert response.status_code == 200
        data = response.json()
        assert data["pools"]["dedupool"] == ["sda"]


@pytest.mark.asyncio
async def test_get_command_log(client):
    command_log.record(command="zfs list", status="success", returncode=0)
    command_log.record(command="zpool status", status="failed", returncode=1, stderr="boom")

    response = client.get("/api/system/command-log")
    assert response.status_code == 200
    data = response.json()
    assert data["size"] == 25
    assert len(data["entries"]) == 2
    assert data["entries"][0]["command"] == "zpool status"
    assert data["entries"][0]["status"] == "failed"
    assert data["entries"][1]["command"] == "zfs list"


@pytest.mark.asyncio
async def test_get_command_log_empty(client):
    response = client.get("/api/system/command-log")
    assert response.status_code == 200
    assert response.json()["entries"] == []


@pytest.mark.asyncio
async def test_get_command_log_filter_type(client):
    command_log.record(command="smartctl -a -j /dev/sda", status="success", op="read", category="smartctl")
    command_log.record(command="zfs create tank/data", status="success", op="write", category="zfs")
    command_log.record(command="smbcontrol all reload-config", status="success", op="system", category="smb")

    response = client.get("/api/system/command-log", params={"type": "write"})
    assert response.status_code == 200
    data = response.json()
    assert len(data["entries"]) == 1
    assert data["entries"][0]["command"] == "zfs create tank/data"


@pytest.mark.asyncio
async def test_get_command_log_filter_multiple_types(client):
    command_log.record(command="smartctl -a -j /dev/sda", status="success", op="read")
    command_log.record(command="zfs create tank/data", status="success", op="write")
    command_log.record(command="smbcontrol reload", status="success", op="system")

    response = client.get("/api/system/command-log", params={"type": "read,system"})
    assert response.status_code == 200
    entries = response.json()["entries"]
    assert len(entries) == 2
    commands = {e["command"] for e in entries}
    assert commands == {"smartctl -a -j /dev/sda", "smbcontrol reload"}


@pytest.mark.asyncio
async def test_get_command_log_untagged_grouped_as_write(client):
    command_log.record(command="zfs create tank/data", status="success")  # no op tag
    command_log.record(command="smartctl -a -j /dev/sda", status="success", op="read")

    # Untagged entry appears under the write/Change filter.
    response = client.get("/api/system/command-log", params={"type": "write"})
    assert response.status_code == 200
    entries = response.json()["entries"]
    assert len(entries) == 1
    assert entries[0]["command"] == "zfs create tank/data"

    # And is hidden under the read filter.
    response = client.get("/api/system/command-log", params={"type": "read"})
    assert len(response.json()["entries"]) == 1
    assert response.json()["entries"][0]["command"] == "smartctl -a -j /dev/sda"


@pytest.mark.asyncio
async def test_get_command_log_filter_status(client):
    command_log.record(command="zfs create tank/data", status="success", op="write")
    command_log.record(command="zfs destroy tank/data", status="failed", op="write")

    response = client.get("/api/system/command-log", params={"status": "failed"})
    assert response.status_code == 200
    entries = response.json()["entries"]
    assert len(entries) == 1
    assert entries[0]["command"] == "zfs destroy tank/data"


@pytest.mark.asyncio
async def test_get_command_log_invalid_type_400(client):
    response = client.get("/api/system/command-log", params={"type": "bogus"})
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_get_command_log_invalid_status_400(client):
    response = client.get("/api/system/command-log", params={"status": "nope"})
    assert response.status_code == 400
