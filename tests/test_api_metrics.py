import os
import time

import pytest
from unittest.mock import patch

from nazman.managers.metrics_store import MetricsStore, SYSTEM_POOL
from nazman.models.pool import Pool


@pytest.fixture()
def metric_store(tmp_path):
    """Swap the global metrics store singleton for an isolated temp-backed one."""
    store = MetricsStore(os.path.join(tmp_path, "metrics.db"))
    store.connect()
    store.set_pool_enabled("tank", True)
    now = int(time.time())
    store.record(SYSTEM_POOL, "cpu", "<system>", now, 40.0)
    store.record(SYSTEM_POOL, "memory", "<system>", now, 60.0)
    store.record("tank", "disk", "sda", now, 12.5)
    store.record("tank", "disk", "sdb", now, 88.3)
    store.flush()
    with patch("nazman.managers.metrics_store.metrics_store", store):
        yield store
    store.close()


def test_history_system_metric(client, db_session, metric_store):
    db_session.add(Pool(name="tank"))
    db_session.commit()

    resp = client.get("/api/monitoring/history?pool=tank&metric=cpu&days=30")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["metric"] == "cpu"
    assert len(data["series"]) == 1
    assert data["series"][0]["value"] == 40.0


def test_history_disk_metric(client, db_session, metric_store):
    db_session.add(Pool(name="tank"))
    db_session.commit()

    resp = client.get("/api/monitoring/history?pool=tank&metric=disk&days=30")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["metric"] == "disk"
    assert data["devices"] == ["sda", "sdb"]
    assert data["series"]["sda"][0]["value"] == 12.5
    assert data["series"]["sdb"][0]["value"] == 88.3


def test_history_invalid_metric_400(client):
    resp = client.get("/api/monitoring/history?pool=tank&metric=bogus&days=7")
    assert resp.status_code == 422  # FastAPI query constaint


def test_logging_toggle(client, db_session, metric_store):
    db_session.add(Pool(name="tank"))
    db_session.commit()

    state = client.get("/api/monitoring/logging")
    assert state.status_code == 200
    assert state.json()["enabled"].get("tank") is True

    resp = client.post("/api/monitoring/logging?pool=tank&enabled=false")
    assert resp.status_code == 200
    assert resp.json()["enabled"].get("tank") is False

    resp = client.post("/api/monitoring/logging?pool=tank&enabled=true")
    assert resp.status_code == 200
    assert resp.json()["enabled"].get("tank") is True


def test_logging_invalid_pool_400(client, metric_store):
    resp = client.post("/api/monitoring/logging?pool=bad/name&enabled=true")
    assert resp.status_code == 400


def test_summary(client, db_session, metric_store):
    db_session.add(Pool(name="tank"))
    db_session.commit()

    resp = client.get("/api/monitoring/summary")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "cpu" in data
    assert "disks" in data
    assert "logging" in data
    assert data["logging"]["enabled"].get("tank") is True