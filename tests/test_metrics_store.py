import os

import pytest
from unittest.mock import patch

from nazman.managers.metrics_store import MetricsStore, SYSTEM_POOL


@pytest.fixture()
def store(tmp_path):
    s = MetricsStore(os.path.join(tmp_path, "metrics.db"))
    s.connect()
    yield s
    s.close()


def test_connect_creates_schema(store):
    assert store._conn is not None
    # per-pool enabled table loads empty
    assert store.list_enabled_pools() == []


def test_set_and_get_pool_enabled(store):
    assert store.is_pool_enabled("tank") is False
    store.set_pool_enabled("tank", True)
    assert store.is_pool_enabled("tank") is True
    assert store.list_enabled_pools() == ["tank"]
    store.set_pool_enabled("tank", False)
    assert store.is_pool_enabled("tank") is False
    assert store.list_enabled_pools() == []


def test_record_system_and_disk(store):
    store.set_pool_enabled("tank", True)
    store.record(SYSTEM_POOL, "cpu", "<system>", 1000, 40.0)
    store.record("tank", "disk", "sda", 1000, 12.5)
    store.record("tank", "disk", "sdb", 1000, 88.3)
    store.flush()

    cpu = store.query_metric(SYSTEM_POOL, "cpu", "<system>", 0, 2000)
    assert cpu == [{"ts": 1000, "value": 40.0}]
    assert store.query_metric("tank", "disk", "sda", 0, 2000) == [{"ts": 1000, "value": 12.5}]
    assert store.distinct_devices("tank", "disk") == ["sda", "sdb"]


def test_disabled_pool_filtered(store):
    store.record("other", "disk", "sdc", 1000, 50.0)
    store.flush()
    # 'other' was never enabled, so nothing persisted
    assert store.query_metric("other", "disk", "sdc", 0, 2000) == []


def test_query_time_range(store):
    store.set_pool_enabled("tank", True)
    for ts, v in [(100, 1.0), (200, 2.0), (300, 3.0)]:
        store.record("tank", "disk", "sda", ts, v)
    store.flush()
    rows = store.query_metric("tank", "disk", "sda", 150, 250)
    assert rows == [{"ts": 200, "value": 2.0}]


def test_prune_removes_old(store):
    import nazman.managers.metrics_store as ms
    store.set_pool_enabled("tank", True)
    store.record("tank", "disk", "sda", 1, 5.0)  # old timestamp
    store.flush()
    store._pruned_at = None
    with patch.object(ms, "get_settings") as mock_settings:
        class S:
            metrics_log_retention_days = 0  # cutoff = now -> ts 1 removed
        mock_settings.return_value = S()
        store.prune()
    assert store.query_metric("tank", "disk", "sda", 0, 9999999) == []


def test_db_size_bytes(store):
    store.set_pool_enabled("tank", True)
    store.record("tank", "disk", "sda", 1000, 5.0)
    store.flush()
    assert store.db_size_bytes() > 0


def test_unavailable_path_no_crash(tmp_path):
    from unittest.mock import patch

    bad = MetricsStore("/nonexistent_root_xyz/metrics.db")
    # simulates unwritable -> connect marks unavailable, no raise
    with patch("nazman.managers.metrics_store.os.makedirs") as mk:
        mk.side_effect = OSError("denied")
        bad.connect()
    assert bad._unavailable is True
    assert bad.list_enabled_pools() == []
    assert bad.query_metric("tank", "disk", "sda", 0, 1) == []
    bad.close()