import asyncio

import pytest
from unittest.mock import patch, MagicMock

from nazman.managers.metrics_manager import MetricsManager


@pytest.mark.asyncio
async def test_register_and_sample():
    mgr = MetricsManager()
    mgr.register("cpu", lambda: 42.0, size=5)
    mgr.sample()
    series = mgr.get_series("cpu")
    assert len(series) == 1
    assert series[0]["value"] == 42.0
    assert "ts" in series[0]


@pytest.mark.asyncio
async def test_ring_buffer_caps_at_size():
    mgr = MetricsManager()
    mgr.register("cpu", lambda: 1.0, size=3)
    for _ in range(5):
        mgr.sample()
    series = mgr.get_series("cpu")
    assert len(series) == 3


@pytest.mark.asyncio
async def test_collector_exception_skipped():
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("fail")
        return 5.0

    mgr = MetricsManager()
    mgr.register("tricky", boom, size=5)
    mgr.sample()
    mgr.sample()
    series = mgr.get_series("tricky")
    assert len(series) == 1
    assert series[0]["value"] == 5.0


@pytest.mark.asyncio
async def test_start_stop_records_in_background():
    mgr = MetricsManager()
    mgr.register("cpu", lambda: 10.0, size=5)
    mgr._interval = 0.01
    await mgr.start()
    try:
        await asyncio.sleep(0.05)
        series = mgr.get_series("cpu")
        assert len(series) >= 2
    finally:
        await mgr.stop()
    assert not mgr._started


@pytest.mark.asyncio
async def test_get_metrics_returns_all_series():
    mgr = MetricsManager()
    mgr.register("a", lambda: 1.0, size=5)
    mgr.register("b", lambda: 2.0, size=5)
    mgr.sample()
    all_series = mgr.get_metrics()
    assert set(all_series.keys()) == {"a", "b"}
    assert all_series["a"][0]["value"] == 1.0
    assert all_series["b"][0]["value"] == 2.0


@pytest.mark.asyncio
async def test_metrics_manager_pushes_multi_collector():
    """A collector with collect(push) writes one or more named series."""
    class Multi:
        def refresh(self):
            pass
        def collect(self, push):
            push("x", 1.0)
            push("y", 2.0)
    mgr = MetricsManager()
    mgr.register("m", Multi(), size=5)
    mgr.sample()
    assert mgr.get_series("x")[-1]["value"] == 1.0
    assert mgr.get_series("y")[-1]["value"] == 2.0


@pytest.mark.asyncio
async def test_network_collector_computes_percent():
    from nazman.managers.metrics_manager import _NetworkCollector

    fake_ifaces = MagicMock()
    fake_ifaces.net_io_counters.return_value = {
        "eth0": MagicMock(bytes_recv=100_000_000, bytes_sent=50_000_000),
    }

    def fake_speed(iface):
        return "1000"  # 1000 Mb/s link

    with patch("nazman.managers.metrics_manager._NetworkCollector._link_speed_mbps",
               side_effect=lambda: 1000), \
         patch("nazman.managers.metrics_manager.psutil", fake_ifaces), \
         patch("nazman.managers.metrics_manager.time") as mock_time:
        mock_time.time.side_effect = [1000.0, 1000.5]  # 0.5s between reads
        net = _NetworkCollector()
        net._iface = "eth0"
        assert net() == 0.0  # first call primes (stores 150M total)
        # total 150M -> 170M = 20MB over 0.5s = 320 Mbit/s = 32% of 1000Mb/s
        mock_time.time.side_effect = [1000.5, 1001.0]
        fake_ifaces.net_io_counters.return_value = {
            "eth0": MagicMock(bytes_recv=120_000_000, bytes_sent=50_000_000),
        }
        val = net()
        assert 30.0 < val < 34.0


@pytest.mark.asyncio
async def test_network_collector_clamps_at_100():
    from nazman.managers.metrics_manager import _NetworkCollector
    net = _NetworkCollector()
    net._iface = "eth0"
    with patch("nazman.managers.metrics_manager.psutil") as p, \
         patch("nazman.managers.metrics_manager.time") as mock_time, \
         patch("nazman.managers.metrics_manager._NetworkCollector._link_speed_mbps",
               return_value=100):
        mock_time.time.side_effect = [0.0, 1.0]
        p.net_io_counters.return_value = {"eth0": MagicMock(bytes_recv=0, bytes_sent=0)}
        assert net() == 0.0  # prime
        mock_time.time.side_effect = [1.0, 2.0]
        p.net_io_counters.return_value = {"eth0": MagicMock(bytes_recv=(10**15), bytes_sent=(10**15))}
        assert net() == 100.0


@pytest.mark.asyncio
async def test_disk_collector_records_per_disk_series(tmp_path):
    from nazman.managers.metrics_manager import _DiskCollector
    collector = _DiskCollector()
    series = {}
    with patch("nazman.managers.metrics_manager._scan_disk_devices", return_value=["sda", "sdb"]), \
         patch("nazman.managers.metrics_manager._read_io_ticks",
               side_effect=[0, 0, 1000, 0]) as read_ticks, \
         patch("nazman.managers.metrics_manager.time") as mock_time:
        mock_time.time.return_value = 100.0
        collector.refresh()  # reads [0,0] to prime prevs for sda,sdb
        collector.collect(lambda n, v: series.__setitem__(n, v))  # primes last_ts, records nothing
        # advance 0.1s and move sda to 1000ms busy, sdb stays 0
        mock_time.time.return_value = 100.1
        collector.collect(lambda n, v: series.__setitem__(n, v))
    assert series["disk_sda"] == 100.0  # (1000-0)ms / 100ms
    assert series["disk_sdb"] == 0.0


@pytest.mark.asyncio
async def test_normalize_base_name():
    from nazman.managers.metrics_manager import normalize_base_name
    assert normalize_base_name("sda") == "sda"
    assert normalize_base_name("sda1") == "sda"
    assert normalize_base_name("nvme0n1p2") == "nvme0n1"
    assert normalize_base_name("mmcblk0p1") == "mmcblk0"
    assert normalize_base_name("/dev/sdb2") == "sdb"


def test_normalize_base_name_resolves_by_id_alias():
    """Bare by-id alias (like zpool leaf names) resolves to the base kernel name."""
    import os
    from unittest.mock import patch
    from nazman.managers.metrics_manager import normalize_base_name

    alias = "ata-WDC_WD80EFBX-part15"
    # Simulate /dev/disk/by-id/<alias> existing and resolving to /dev/sdc1
    with patch("os.path.isabs",
               side_effect=lambda p: True), \
         patch("os.path.exists",
               side_effect=lambda p: p == f"/dev/disk/by-id/{alias}"), \
         patch("os.path.realpath",
               side_effect=lambda p: "/dev/sdc1" if p == f"/dev/disk/by-id/{alias}" else p):
        assert normalize_base_name(alias) == "sdc"