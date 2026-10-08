from unittest.mock import patch

import pytest

from nazman.utils.notification_store import NotificationStore


@pytest.fixture()
def journal(tmp_path):
    store = NotificationStore(db_path=str(tmp_path / "n.db"))
    store.connect()
    with patch("nazman.api.notifications.notification_store", store):
        yield store
    store.close()


def test_create_and_list(client, journal):
    resp = client.post("/api/notifications", json={
        "message": "Backup finished", "level": "success",
        "title": "Backup", "source": "ui",
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"]

    data = client.get("/api/notifications").json()
    assert data["entries"][0]["message"] == "Backup finished"
    assert data["entries"][0]["level"] == "success"
    assert data["unread"] == 1
    assert data["max_id"] >= 1


def test_mark_read_and_clear(client, journal):
    client.post("/api/notifications", json={"message": "a"})
    client.post("/api/notifications", json={"message": "b"})
    assert client.get("/api/notifications").json()["unread"] == 2

    assert client.post("/api/notifications/read", json={}).json()["unread"] == 0
    assert client.get("/api/notifications").json()["unread"] == 0

    assert client.delete("/api/notifications").json()["unread"] == 0
    assert client.get("/api/notifications").json()["entries"] == []


def test_mark_specific_ids_read(client, journal):
    first = client.post("/api/notifications", json={"message": "a"}).json()["id"]
    client.post("/api/notifications", json={"message": "b"})
    assert client.post("/api/notifications/read", json={"ids": [first]}).json()["unread"] == 1
    unread = client.get("/api/notifications?unread_only=true").json()["entries"]
    assert [e["message"] for e in unread] == ["b"]


def test_since_id_filters(client, journal):
    client.post("/api/notifications", json={"message": "a"})
    marker = client.get("/api/notifications").json()["max_id"]
    client.post("/api/notifications", json={"message": "b"})
    entries = client.get(f"/api/notifications?since_id={marker}").json()["entries"]
    assert [e["message"] for e in entries] == ["b"]