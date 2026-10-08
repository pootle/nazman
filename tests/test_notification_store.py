import pytest

from nazman.utils.notification_store import NotificationStore


def _store(tmp_path):
    store = NotificationStore(db_path=str(tmp_path / "notifications.db"))
    store.connect()
    return store


def test_add_and_list_newest_first(tmp_path):
    store = _store(tmp_path)
    store.add(message="first")
    store.add(message="second", level="error", title="Oops", source="backup")
    entries = store.list(limit=10)
    assert [e["message"] for e in entries] == ["second", "first"]
    assert entries[0]["level"] == "error"
    assert entries[0]["title"] == "Oops"
    assert entries[0]["source"] == "backup"
    assert entries[0]["read"] is False


def test_invalid_level_falls_back_to_info(tmp_path):
    store = _store(tmp_path)
    store.add(message="x", level="bogus")
    assert store.list(limit=1)[0]["level"] == "info"


def test_ring_cap_keeps_only_newest(tmp_path):
    store = _store(tmp_path)
    store._size = 3
    for i in range(5):
        store.add(message=f"m{i}")
    entries = store.list(limit=10)
    assert [e["message"] for e in entries] == ["m4", "m3", "m2"]


def test_since_id_returns_only_newer(tmp_path):
    store = _store(tmp_path)
    first = store.add(message="first")
    store.add(message="second")
    newer = store.list(since_id=first)
    assert [e["message"] for e in newer] == ["second"]
    assert store.max_id() >= first


def test_unread_count_and_mark_all_read(tmp_path):
    store = _store(tmp_path)
    store.add(message="a")
    store.add(message="b")
    assert store.unread_count() == 2
    store.mark_read()
    assert store.unread_count() == 0


def test_mark_read_specific_ids(tmp_path):
    store = _store(tmp_path)
    a = store.add(message="a")
    store.add(message="b")
    store.mark_read([a])
    assert store.unread_count() == 1
    assert store.list(unread_only=True)[0]["message"] == "b"


def test_clear_removes_entries(tmp_path):
    store = _store(tmp_path)
    store.add(message="a")
    store.clear()
    assert store.list() == []
    assert store.unread_count() == 0


def test_metadata_defaults(tmp_path):
    store = _store(tmp_path)
    store.add(message="m")
    entry = store.list()[0]
    assert entry["duration_ms"] is None
    assert entry["bytes"] is None
    assert entry["source"] is None


def test_unavailable_store_degrades(tmp_path):
    store = NotificationStore(db_path="/proc/definitely/not/writable/n.db")
    store.connect()
    assert store.available is False
    assert store.add(message="x") is None
    assert store.list() == []