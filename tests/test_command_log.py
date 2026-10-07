import os
import pytest
from nazman.utils.command_log import CommandLogger, command_log
from nazman.utils.command_log_store import CommandLogStore


@pytest.fixture
def store(tmp_path):
    s = CommandLogStore(db_path=str(tmp_path / "cmd.db"))
    s.connect()
    yield s
    s.close()


@pytest.fixture
def logger(store):
    lg = CommandLogger(store=store)
    return lg


def test_record_and_get_newest_first(logger, store):
    logger.record(command="zfs list", status="success", returncode=0)
    logger.record(command="zpool status", status="success", returncode=0, duration_ms=12)
    entries = logger.get_entries()
    assert len(entries) == 2
    assert entries[0]["command"] == "zpool status"
    assert entries[1]["command"] == "zfs list"
    assert entries[0]["duration_ms"] == 12


def test_records_failure_with_stderr(logger):
    logger.record(command="zfs create", status="failed", returncode=1, stderr="cannot create dataset")
    entry = logger.get_entries()[0]
    assert entry["status"] == "failed"
    assert entry["returncode"] == 1
    assert entry["stderr"] == "cannot create dataset"


def test_persists_across_store_reopen(tmp_path, store):
    logger = CommandLogger(store=store)
    logger.record(command="zfs create tank/data", status="success")
    # Reopening a fresh store on the same file must find the entry (persistence).
    store.close()
    store2 = CommandLogStore(db_path=str(tmp_path / "cmd.db"))
    store2.connect()
    try:
        entries = store2.get_entries()
        assert any(e["command"] == "zfs create tank/data" for e in entries)
    finally:
        store2.close()


def test_limit_caps_returns(logger):
    for i in range(10):
        logger.record(command=f"cmd {i}", status="success")
    entries = logger.get_entries(limit=5)
    assert len(entries) == 5
    assert entries[0]["command"] == "cmd 9"


def test_stderr_truncated_to_200_chars(logger):
    long_stderr = "x" * 500
    logger.record(command="cmd", status="failed", returncode=1, stderr=long_stderr)
    entry = logger.get_entries()[0]
    assert len(entry["stderr"]) == 200


def test_reset_clears_buffer(logger):
    logger.record(command="cmd", status="success")
    logger.reset()
    assert logger.get_entries() == []


def test_get_entries_filter_by_ops(logger):
    logger.record(command="smartctl -a -j /dev/sda", status="success", op="read")
    logger.record(command="zfs create tank/data", status="success", op="write")
    logger.record(command="smbcontrol reload", status="success", op="system")

    writes = logger.get_entries(ops=["write"])
    assert len(writes) == 1
    assert writes[0]["command"] == "zfs create tank/data"

    reads_systems = logger.get_entries(ops=["read", "system"])
    assert len(reads_systems) == 2


def test_get_entries_untagged_grouped_as_write(logger):
    logger.record(command="zfs create tank/data", status="success")  # no op tag
    logger.record(command="smartctl -a -j /dev/sda", status="success", op="read")

    writes = logger.get_entries(ops=["write"])
    assert len(writes) == 1
    assert writes[0]["command"] == "zfs create tank/data"


def test_get_entries_filter_by_status(logger):
    logger.record(command="zfs create", status="success")
    logger.record(command="zfs destroy", status="failed")
    failed = logger.get_entries(statuses=["failed"])
    assert len(failed) == 1
    assert failed[0]["command"] == "zfs destroy"


def test_record_omits_none_fields(logger):
    logger.record(command="cmd", status="success")
    entry = logger.get_entries()[0]
    assert entry.get("returncode") is None
    assert entry.get("stderr") is None
    assert entry.get("op") is None
    assert entry.get("category") is None
    assert "ts" in entry


def test_record_stores_op_and_category(logger):
    logger.record(command="zfs create tank/data", status="success", op="write", category="zfs")
    entry = logger.get_entries()[0]
    assert entry["op"] == "write"
    assert entry["category"] == "zfs"


def test_untagged_entry_has_no_op_and_groups_as_write(logger):
    logger.record(command="smartctl -a -j /dev/sda", status="success")
    entry = logger.get_entries()[0]
    assert entry.get("op") is None
    assert logger.get_entries(ops=["write"])[0]["command"] == "smartctl -a -j /dev/sda"


def test_untagged_mutating_entry_groups_as_write(logger):
    logger.record(command="zpool create -f tank /dev/sda", status="success")
    entry = logger.get_entries()[0]
    assert entry.get("op") is None
    assert logger.get_entries(ops=["write"])[0]["command"] == "zpool create -f tank /dev/sda"


def test_explicit_op_is_preserved(logger):
    logger.record(command="zfs list", status="success", op="read", category="zfs")
    entry = logger.get_entries()[0]
    assert entry["op"] == "read"


def test_global_singleton_is_command_log():
    assert isinstance(command_log, CommandLogger)
    command_log.reset()