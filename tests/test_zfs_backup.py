import gzip
import hashlib
import json
import os
import pytest
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

from nazman.models.disk import Disk
from nazman.models.pool import Pool
from nazman.models.backup_zfs import (
    BackupDisk, BackupGroup, BackupGroupDataset, BackupRun,
    BackupSession, BackupSet,
)
from nazman.managers.zfs_backup_manager import ZfsBackupManager
from nazman.managers.zfs_manager import ZfsManager
from nazman.managers.scheduler import SchedulerManager
from nazman.services.backup_group_service import BackupGroupService

zfs_manager = ZfsManager()
scheduler_manager = SchedulerManager()
zfs_backup_manager = ZfsBackupManager(zfs=zfs_manager)
backup_groups = BackupGroupService(
    zfs_backup=zfs_backup_manager, scheduler=scheduler_manager,
)
from nazman.utils.exceptions import ValidationError, BackupError, CommandError
from nazman.wiring import get_zfs_backup_manager
from tests.conftest import override_manager


def _mk_pool(db_session, name):
    pool = Pool(name=name)
    db_session.add(pool)
    db_session.flush()
    return pool


def _mk_group(db_session, tmp_path, name="Weekly", datasets=("tank/media",),
              disk_count=1, positions=None, disks_per_set=1):
    """A group with ``datasets``, ``positions`` sets, and ``disks_per_set``
    declared disks in each.

    Returns ``(group, sets, disks)`` - the shape most engine tests need to
    drive a session end to end.  ``positions`` defaults to one set per disk so
    a single-disk group is the common case.
    """
    group = BackupGroup(name=name)
    db_session.add(group)
    db_session.flush()
    for dataset in datasets:
        db_session.add(BackupGroupDataset(group_id=group.id, dataset_name=dataset))
    if positions is None:
        positions = max(1, disk_count // disks_per_set)
    sets = []
    for i in range(positions):
        bset = BackupSet(group_id=group.id, position=i, label=f"set {i + 1}")
        db_session.add(bset)
        db_session.flush()
        sets.append(bset)
    disks = []
    n = 0
    for bset in sets:
        for slot in range(disks_per_set):
            mount = tmp_path / f"vol{n}"
            mount.mkdir(parents=True, exist_ok=True)
            rec = BackupDisk(
                disk_id=900 + n, mount_point=str(mount), fs_uuid=f"UUU{n}",
                unmount_after_backup=True,
            )
            db_session.add(rec)
            db_session.flush()
            rec.backup_set_id = bset.id
            if slot == 0:
                bset.active_disk_id = rec.id
            disks.append(rec)
            n += 1
    if sets:
        group.active_set_id = sets[0].id
    db_session.commit()
    return group, sets, disks


def _fake_write(stages, stdout_path=None, content="STREAMSIM"):
    """Simulate a backup write pipeline, new or legacy shape.

    New pipelines tee the stream to a file and pipe into sha256sum, so the
    ``tee`` stage lands the bytes and the returned stdout is a real digest.
    """
    tee = next((s for s in stages if s and s[0] == "tee"), None)
    if tee is not None:
        with open(tee[1], "w") as fh:
            fh.write(content)
        digest = hashlib.sha256(content.encode()).hexdigest()
        return (f"{digest}  -\n", "", 0)
    with open(stdout_path, "w") as fh:
        fh.write(content)
    return ("", "", 0)


def _stream_fakes(pipe_assert=None):
    """zfs/pipeline fakes: snapshots work, streams land on disk."""
    def make(pipe_check=None):
        async def fake_run_zfs(*args, **kwargs):
            cmd = list(args)
            if cmd and cmd[0] == "snapshot":
                return ("", "", 0)
            if cmd and cmd[0] == "destroy":
                return ("", "", 0)
            if cmd and cmd[0] == "get":
                return ("123456", "", 0)
            if cmd and cmd[0] == "list":
                if "-t" in cmd and "snapshot" in cmd:
                    return ("", "", 0)
                return ("tank/media", "", 0)
            return ("", "", 0)

        async def fake_pipeline(stages, stdout_path=None, **kwargs):
            if pipe_check:
                pipe_check(stages)
            return _fake_write(stages, stdout_path)
        return fake_run_zfs, fake_pipeline
    return make(pipe_assert)


@pytest.mark.asyncio
async def test_group_full_session_writes_successful_runs(db_session, tmp_path, monkeypatch):
    group, sets, disks = _mk_group(db_session, tmp_path, disk_count=1)

    mounted = {d.id: True for d in disks}
    cmds = []

    def fake_is_mount(self):
        return mounted.get(_vol_id(self), False)

    def _vol_id(path):
        for rec in disks:
            if str(rec.mount_point) == str(path):
                return rec.id
        return None

    monkeypatch.setattr(Path, "is_mount", fake_is_mount)

    async def fake_run_command(cmd, **kwargs):
        cmds.append(cmd)
        if cmd[0] == "mount":
            mounted[_cmd_vol(cmd, disks)] = True
        if cmd[0] == "umount":
            mounted[_cmd_vol(cmd, disks)] = False
        return ("", "", 0)

    fake_zfs, fake_pipe = _stream_fakes()
    with patch("nazman.managers.zfs_backup_manager.run_zfs", side_effect=fake_zfs), \
            patch("nazman.utils.zfs_query.run_zfs", side_effect=fake_zfs), \
         patch("nazman.managers.zfs_backup_manager.run_pipeline", side_effect=fake_pipe), \
         patch("nazman.managers.zfs_backup_manager.run_command", side_effect=fake_run_command), \
         patch("os.path.exists", return_value=True), \
         patch.object(ZfsBackupManager, "_fs_uuid", new=AsyncMock(return_value="AAA")), \
         patch.object(ZfsBackupManager, "_dataset_exists", new=AsyncMock(return_value=True)):
        session = await backup_groups.start_session(db_session, group.id, "full")
        result = await backup_groups.run_session(db_session, session.id)

    assert result["status"] == "success"
    run = db_session.query(BackupRun).order_by(BackupRun.id.desc()).first()
    assert run.status == "success"
    assert run.backup_type == "full"
    assert run.dataset_name == "tank/media"
    assert run.group_id == group.id
    assert run.backup_set_id == sets[0].id
    assert run.snapshot.startswith("tank/media@backup-")
    assert run.size_bytes == len("STREAMSIM")
    # The stream's digest is mined from the sha256sum pipeline stage (on the
    # fly), so it must match the file actually landed on disk.
    assert run.sha256 == hashlib.sha256(b"STREAMSIM").hexdigest()
    assert Path(run.stream_file).read_text() == "STREAMSIM"

    # A set's first visit is always a full, even when asked for incremental.
    refreshed = db_session.get(BackupGroup, group.id)
    assert refreshed.last_session_at is not None

    # Self-describing manifest + sidecar stamped with the set it belongs to.
    from nazman.utils import backup_manifest as bm
    manifest = bm.load_manifest(disks[0].mount_point)
    assert manifest is not None
    run_entry = manifest["datasets"][0]["backups"][0]
    assert run_entry["sha256"] == run.sha256
    assert run_entry["set_id"] == sets[0].id
    assert run_entry["group_id"] == group.id
    assert manifest["group"] == "Weekly"


def _cmd_vol(cmd, disks):
    for rec in disks:
        if str(rec.mount_point) == str(cmd[1]):
            return rec.id
    return None


def _mk_progress_session(db_session, group, sets, disks, *,
                         current_size, current_estimated, current_type="full",
                         prior_type=None, prior_size=100, prior_estimated=200,
                         datasets_done=1, datasets_total=2) -> BackupSession:
    """A running session whose current run is mid-send, for view tests."""
    session = BackupSession(
        group_id=group.id, backup_set_id=sets[0].id, backup_disk_id=disks[0].id,
        trigger="full", status="running", phase="sending",
        datasets_total=datasets_total, datasets_done=datasets_done,
        bytes_written=0,
    )
    db_session.add(session)
    db_session.commit()
    if prior_estimated is not None:
        prior = BackupRun(
            session_id=session.id, group_id=group.id, backup_set_id=sets[0].id,
            dataset_name="tank/docs", backup_disk_id=disks[0].id,
            backup_type=prior_type or current_type, status="success", phase=None,
            size_bytes=prior_size, estimated_bytes=prior_estimated,
            completed_at=datetime.now(timezone.utc),
        )
        db_session.add(prior)
    current = BackupRun(
        session_id=session.id, group_id=group.id, backup_set_id=sets[0].id,
        dataset_name="tank/docs", backup_disk_id=disks[0].id,
        backup_type=current_type, status="running", phase="sending",
        size_bytes=current_size, estimated_bytes=current_estimated,
        started_at=datetime.now(timezone.utc),
    )
    db_session.add(current)
    db_session.commit()
    return session


@pytest.mark.asyncio
async def test_group_session_publishes_live_progress(db_session, tmp_path, monkeypatch):
    """The running session's row shows 1/2 and the phase mid-run, not 0/N."""
    group, sets, disks = _mk_group(
        db_session, tmp_path, disk_count=1, datasets=("tank/media", "tank/docs"),
    )
    monkeypatch.setattr(Path, "is_mount", lambda self: True)

    async def fake_run_command(cmd, **kwargs):
        return ("", "", 0)

    fake_zfs, fake_pipe = _stream_fakes()
    real = ZfsBackupManager.backup_dataset
    views_while_running = []

    async def spy(self, db, rec, run):
        if not views_while_running and run.dataset_name == "tank/docs":
            live = db_session.query(BackupSession).filter_by(id=session.id).first()
            assert live.datasets_done == 1
            assert live.datasets_skipped == 0
            assert live.datasets_failed == 0
            assert live.phase == "sending"
            # A real UI poll is a fresh connection.  Reload so the sticky
            # expire_on_commit=False identity map sees the committed runs.
            db_session.expire_all()
            running = next(
                s for s in await backup_groups.list_sessions(db_session, group_id=group.id)
                if s["status"] == "running"
            )
            views_while_running.append(running)
        return await real(self, db, rec, run)

    with patch("nazman.managers.zfs_backup_manager.run_zfs", side_effect=fake_zfs), \
            patch("nazman.utils.zfs_query.run_zfs", side_effect=fake_zfs), \
         patch("nazman.managers.zfs_backup_manager.run_pipeline", side_effect=fake_pipe), \
         patch("nazman.managers.zfs_backup_manager.run_command", side_effect=fake_run_command), \
         patch("os.path.exists", return_value=True), \
         patch.object(ZfsBackupManager, "_fs_uuid", new=AsyncMock(return_value="AAA")), \
         patch.object(ZfsBackupManager, "_dataset_exists", new=AsyncMock(return_value=True)), \
         patch.object(ZfsBackupManager, "backup_dataset", new=spy):
        session = await backup_groups.start_session(db_session, group.id, "full")
        result = await backup_groups.run_session(db_session, session.id)

    assert result["status"] == "success"
    running = views_while_running[0]
    assert running["phase"] == "sending"
    assert running["current_dataset"] == "tank/docs"
    # Second dataset is in flight but has no history: progress is based on the
    # live ZFS footprint (fake "get" returns 123456), nothing written yet.
    assert running["progress_pct"] == 50
    assert running["current_run"]["expected_bytes"] == 123456
    assert running["current_run"]["pct"] == 0

    sessions = await backup_groups.list_sessions(db_session, group_id=group.id)
    done_view = sessions[0]
    assert done_view["progress_pct"] == 100
    assert done_view["current_run"] is None
    runs = db_session.query(BackupRun).filter_by(session_id=session.id).all()
    assert all(r.estimated_bytes and r.estimated_bytes > 0 for r in runs)


@pytest.mark.asyncio
async def test_session_view_progress_uses_history_denominator(db_session, tmp_path):
    """A running run's % compares written bytes to the last same-type stream."""
    group, sets, disks = _mk_group(db_session, tmp_path, disk_count=1)
    _mk_progress_session(
        db_session, group, sets, disks,
        current_size=50, current_estimated=400, prior_size=100, prior_estimated=200,
    )

    sessions = await backup_groups.list_sessions(db_session, group_id=group.id)
    view = sessions[0]
    assert view["phase"] == "sending"
    assert view["current_dataset"] == "tank/docs"
    cur = view["current_run"]
    assert cur["size_bytes"] == 50
    assert cur["estimated_bytes"] == 400
    assert cur["expected_bytes"] == 100  # last successful full of this dataset
    assert cur["pct"] == 50
    assert view["progress_pct"] == 75  # (1 done + 0.5) / 2

    for r in view["runs"]:
        if r["estimated_bytes"] == 200:
            # Completed, older runs carry no middle-of-send denominator.
            assert r["expected_bytes"] is None and r["pct"] is None
        else:
            assert r["expected_bytes"] == 100 and r["pct"] == 50


@pytest.mark.asyncio
async def test_session_view_progress_uses_ratio_when_type_has_no_history(db_session, tmp_path):
    """Without a same-type stream, estimate x the dataset's compression ratio."""
    group, sets, disks = _mk_group(db_session, tmp_path, disk_count=1)
    _mk_progress_session(
        db_session, group, sets, disks,
        current_size=50, current_estimated=400,
        current_type="full", prior_type="incremental",
        prior_size=100, prior_estimated=200,
    )

    view = (await backup_groups.list_sessions(db_session, group_id=group.id))[0]
    cur = view["current_run"]
    assert cur["expected_bytes"] == 200  # 400 x (100/200)
    assert cur["pct"] == 25
    assert view["progress_pct"] == 62


@pytest.mark.asyncio
async def test_session_view_progress_caps_at_99_while_running(db_session, tmp_path):
    group, sets, disks = _mk_group(db_session, tmp_path, disk_count=1)
    _mk_progress_session(
        db_session, group, sets, disks,
        current_size=500, current_estimated=400, prior_size=100, prior_estimated=200,
    )

    view = (await backup_groups.list_sessions(db_session, group_id=group.id))[0]
    assert view["current_run"]["pct"] == 99
    assert view["progress_pct"] == 99


@pytest.mark.asyncio
async def test_session_view_progress_reverts_to_coarse_without_history(db_session, tmp_path, monkeypatch):
    """With no prior run and no measurable ZFS footprint, show bytes only."""
    monkeypatch.setattr(
        ZfsBackupManager, "estimate_full_size", AsyncMock(return_value=0),
    )
    group, sets, disks = _mk_group(db_session, tmp_path, disk_count=1)
    _mk_progress_session(
        db_session, group, sets, disks,
        current_size=50, current_estimated=None, prior_estimated=None,
    )

    view = (await backup_groups.list_sessions(db_session, group_id=group.id))[0]
    cur = view["current_run"]
    assert cur["estimated_bytes"] is None
    assert cur["expected_bytes"] is None
    assert cur["pct"] is None
    assert view["progress_pct"] == 50  # coarse: (1 done + 0) / 2


@pytest.mark.asyncio
async def test_session_view_progress_falls_back_to_dataset_used_on_first_run(db_session, tmp_path, monkeypatch):
    """First ever run of a dataset: progress estimates against live ZFS ``used``."""
    monkeypatch.setattr(
        ZfsBackupManager, "estimate_full_size", AsyncMock(return_value=200),
    )
    group, sets, disks = _mk_group(db_session, tmp_path, disk_count=1)
    _mk_progress_session(
        db_session, group, sets, disks,
        current_size=50, current_estimated=400, prior_estimated=None,
    )

    view = (await backup_groups.list_sessions(db_session, group_id=group.id))[0]
    cur = view["current_run"]
    assert cur["expected_bytes"] == 200  # live ZFS used
    assert cur["pct"] == 25
    assert view["progress_pct"] == 62  # (1 done + 0.25) / 2


@pytest.mark.asyncio
async def test_group_complete_key_is_stable_across_sessions(db_session, tmp_path, monkeypatch):
    """Completion alerts fire on backup:group:<id>:complete, so rules can match
    the latest outcome with one key instead of hunting session ids."""
    group, sets, disks = _mk_group(db_session, tmp_path, disk_count=1)
    mounted = {d.id: True for d in disks}
    monkeypatch.setattr(Path, "is_mount", lambda self: True)

    async def fake_run_command(cmd, **kwargs):
        if cmd[0] == "mount":
            mounted[_cmd_vol(cmd, disks)] = True
        if cmd[0] == "umount":
            mounted[_cmd_vol(cmd, disks)] = False
        return ("", "", 0)

    fake_zfs, fake_pipe = _stream_fakes()
    keys = []
    with patch("nazman.managers.zfs_backup_manager.run_zfs", side_effect=fake_zfs), \
            patch("nazman.utils.zfs_query.run_zfs", side_effect=fake_zfs), \
         patch("nazman.managers.zfs_backup_manager.run_pipeline", side_effect=fake_pipe), \
         patch("nazman.managers.zfs_backup_manager.run_command", side_effect=fake_run_command), \
         patch("os.path.exists", return_value=True), \
         patch.object(ZfsBackupManager, "_fs_uuid", new=AsyncMock(return_value="AAA")), \
         patch.object(ZfsBackupManager, "_dataset_exists", new=AsyncMock(return_value=True)), \
         patch.object(BackupGroupService, "_notify", new=AsyncMock()) as notify:
        for _ in range(2):
            session = await backup_groups.start_session(db_session, group.id, "full")
            result = await backup_groups.run_session(db_session, session.id)
            assert result["status"] in ("success", "partial")
            keys.append([c.args[0] for c in notify.call_args_list])
            notify.reset_mock()

    complete_key = f"backup:group:{group.id}:complete"
    assert complete_key in keys[0]
    assert complete_key in keys[1]
    # The key must not carry a session id, or it would change every run.
    assert not any(k.startswith(f"backup:group:{group.id}:session:") for k in keys[0] + keys[1])


@pytest.mark.asyncio
async def test_group_session_rotation_advances_set(db_session, tmp_path, monkeypatch):
    group, sets, disks = _mk_group(
        db_session, tmp_path, disk_count=2, positions=2,
    )
    monkeypatch.setattr(Path, "is_mount", lambda self: True)
    fake_zfs, fake_pipe = _stream_fakes()
    with patch("nazman.managers.zfs_backup_manager.run_zfs", side_effect=fake_zfs), \
            patch("nazman.utils.zfs_query.run_zfs", side_effect=fake_zfs), \
         patch("nazman.managers.zfs_backup_manager.run_pipeline", side_effect=fake_pipe), \
         patch("nazman.managers.zfs_backup_manager.run_command", new=AsyncMock()), \
         patch("os.path.exists", return_value=True), \
         patch.object(ZfsBackupManager, "_fs_uuid", new=AsyncMock(return_value="AAA")), \
         patch.object(ZfsBackupManager, "_dataset_exists", new=AsyncMock(return_value=True)), \
         patch.object(ZfsBackupManager, "_record_manifest", new=AsyncMock()):
        s1 = await backup_groups.start_session(db_session, group.id, "full")
        await backup_groups.run_session(db_session, s1.id)
        assert db_session.get(BackupGroup, group.id).active_set_id == sets[1].id
        # Second session uses set 2; the first is marked used.
        s2 = await backup_groups.start_session(db_session, group.id, "full")
        await backup_groups.run_session(db_session, s2.id)
        assert db_session.get(BackupGroup, group.id).active_set_id == sets[0].id
    assert db_session.get(BackupSet, sets[0].id).last_used_at is not None
    assert db_session.get(BackupSet, sets[1].id).last_used_at is not None


@pytest.mark.asyncio
async def test_group_session_first_visit_of_set_forces_full(db_session, tmp_path, monkeypatch):
    group, sets, disks = _mk_group(db_session, tmp_path, disk_count=1)
    monkeypatch.setattr(Path, "is_mount", lambda self: True)
    seen = []

    def check(stages):
        seen.append(stages[0])

    fake_zfs, fake_pipe = _stream_fakes(check)
    with patch("nazman.managers.zfs_backup_manager.run_zfs", side_effect=fake_zfs), \
            patch("nazman.utils.zfs_query.run_zfs", side_effect=fake_zfs), \
         patch("nazman.managers.zfs_backup_manager.run_pipeline", side_effect=fake_pipe), \
         patch("nazman.managers.zfs_backup_manager.run_command", new=AsyncMock()), \
         patch("os.path.exists", return_value=True), \
         patch.object(ZfsBackupManager, "_fs_uuid", new=AsyncMock(return_value="AAA")), \
         patch.object(ZfsBackupManager, "_dataset_exists", new=AsyncMock(return_value=True)), \
         patch.object(ZfsBackupManager, "_record_manifest", new=AsyncMock()):
        session = await backup_groups.start_session(db_session, group.id, "incremental")
        await backup_groups.run_session(db_session, session.id)

    # -i is never used on a set's first write: the chain must start full.
    assert seen and "-i" not in seen[0]
    run = db_session.query(BackupRun).order_by(BackupRun.id.desc()).first()
    assert run.backup_type == "full"
    assert run.promoted_from is None


@pytest.mark.asyncio
async def test_group_incremental_skips_when_no_changes(db_session, tmp_path, monkeypatch):
    group, sets, disks = _mk_group(db_session, tmp_path, disk_count=1)
    db_session.get(BackupSet, sets[0].id).last_used_at = datetime.now(timezone.utc)
    db_session.commit()
    monkeypatch.setattr(Path, "is_mount", lambda self: True)

    async def fake_run_zfs(*args, **kwargs):
        cmd = list(args)
        if cmd and cmd[0] == "diff":
            return ("", "", 0)  # no differences
        if cmd and cmd[0] == "list":
            if "-t" in cmd and "snapshot" in cmd:
                return ("", "", 0)
            return ("tank/media", "", 0)
        return ("", "", 0)

    async def fake_pipeline(stages, stdout_path=None, **kwargs):
        raise AssertionError("pipeline must not run for an unchanged dataset")

    with patch("nazman.managers.zfs_backup_manager.run_zfs", side_effect=fake_run_zfs), \
            patch("nazman.utils.zfs_query.run_zfs", side_effect=fake_run_zfs), \
         patch("nazman.managers.zfs_backup_manager.run_pipeline", side_effect=fake_pipeline), \
         patch("nazman.managers.zfs_backup_manager.run_command", new=AsyncMock()), \
         patch.object(ZfsBackupManager, "_dataset_exists", new=AsyncMock(return_value=True)), \
         patch.object(ZfsBackupManager, "set_anchor", new=AsyncMock(return_value="tank/media@backup-old")):
        session = await backup_groups.start_session(db_session, group.id, "incremental")
        result = await backup_groups.run_session(db_session, session.id)

    assert result["status"] == "success"
    session_row = db_session.get(BackupSession, session.id)
    assert session_row.datasets_skipped == 1
    run = db_session.query(BackupRun).order_by(BackupRun.id.desc()).first()
    assert run.status == "skipped"


@pytest.mark.asyncio
async def test_group_incremental_promotes_to_full_without_base(db_session, tmp_path, monkeypatch):
    group, sets, disks = _mk_group(db_session, tmp_path, disk_count=1)
    db_session.get(BackupSet, sets[0].id).last_used_at = datetime.now(timezone.utc)
    db_session.commit()
    monkeypatch.setattr(Path, "is_mount", lambda self: True)
    fake_zfs, fake_pipe = _stream_fakes()
    with patch("nazman.managers.zfs_backup_manager.run_zfs", side_effect=fake_zfs), \
            patch("nazman.utils.zfs_query.run_zfs", side_effect=fake_zfs), \
         patch("nazman.managers.zfs_backup_manager.run_pipeline", side_effect=fake_pipe), \
         patch("nazman.managers.zfs_backup_manager.run_command", new=AsyncMock()), \
         patch("os.path.exists", return_value=True), \
         patch.object(ZfsBackupManager, "_fs_uuid", new=AsyncMock(return_value="AAA")), \
         patch.object(ZfsBackupManager, "_dataset_exists", new=AsyncMock(return_value=True)), \
         patch.object(ZfsBackupManager, "set_anchor", new=AsyncMock(return_value=None)), \
         patch.object(ZfsBackupManager, "_record_manifest", new=AsyncMock()), \
         patch.object(BackupGroupService, "_notify", new=AsyncMock()) as notify:
        session = await backup_groups.start_session(db_session, group.id, "incremental")
        await backup_groups.run_session(db_session, session.id)

    run = db_session.query(BackupRun).order_by(BackupRun.id.desc()).first()
    assert run.promoted_from == "incremental"
    assert run.backup_type == "full"
    # Surfaced to the user in the session notes and over Telegram.
    session_row = db_session.get(BackupSession, session.id)
    assert "promoted to full" in session_row.notes
    keys = [c.args[0] for c in notify.call_args_list]
    assert f"backup:set:{sets[0].id}:promoted" in keys


@pytest.mark.asyncio
async def test_group_advances_to_next_disk_when_full(db_session, tmp_path, monkeypatch):
    group, sets, disks = _mk_group(db_session, tmp_path, disks_per_set=2)
    monkeypatch.setattr(Path, "is_mount", lambda self: True)

    free_by_vol = {str(tmp_path / "vol0"): 0, str(tmp_path / "vol1"): 10 << 30}

    async def fake_run_zfs(*args, **kwargs):
        cmd = list(args)
        if cmd and cmd[0] == "list":
            if "-t" in cmd and "snapshot" in cmd:
                return ("", "", 0)
            return ("tank/media", "", 0)
        return ("", "", 0)

    async def fake_pipeline(stages, stdout_path=None, **kwargs):
        return _fake_write(stages, stdout_path)

    with patch("nazman.managers.zfs_backup_manager.run_zfs", side_effect=fake_run_zfs), \
            patch("nazman.utils.zfs_query.run_zfs", side_effect=fake_run_zfs), \
         patch("nazman.managers.zfs_backup_manager.run_pipeline", side_effect=fake_pipeline), \
         patch("nazman.managers.zfs_backup_manager.run_command", new=AsyncMock()), \
         patch("nazman.managers.zfs_backup_manager.os.statvfs",
               side_effect=lambda p: type("S", (), {
                   "f_frsize": 1, "f_bavail": free_by_vol[str(p)],
               })()), \
         patch.object(ZfsBackupManager, "_fs_uuid", new=AsyncMock(return_value="AAA")), \
         patch.object(ZfsBackupManager, "_dataset_exists", new=AsyncMock(return_value=True)), \
         patch.object(ZfsBackupManager, "mount_backup_disk", new=AsyncMock()), \
         patch.object(ZfsBackupManager, "estimate_needed", new=AsyncMock(return_value=1 << 20)), \
         patch.object(ZfsBackupManager, "_record_manifest", new=AsyncMock()):
        session = await backup_groups.start_session(db_session, group.id, "full")
        result = await backup_groups.run_session(db_session, session.id)

    assert result["status"] == "success"
    # The write landed on the second volume, and the set now points at it.
    run = db_session.query(BackupRun).order_by(BackupRun.id.desc()).first()
    assert run.backup_disk_id == disks[1].id
    assert str(disks[1].mount_point) in str(run.stream_file)
    assert db_session.get(BackupSet, sets[0].id).active_disk_id == disks[1].id


@pytest.mark.asyncio
async def test_group_blocks_when_no_disk_has_room(db_session, tmp_path, monkeypatch):
    group, sets, disks = _mk_group(db_session, tmp_path, disk_count=1)
    monkeypatch.setattr(Path, "is_mount", lambda self: True)

    async def fake_run_zfs(*args, **kwargs):
        cmd = list(args)
        if cmd and cmd[0] == "list":
            if "-t" in cmd and "snapshot" in cmd:
                return ("", "", 0)
            return ("tank/media", "", 0)
        return ("", "", 0)

    with patch("nazman.managers.zfs_backup_manager.run_zfs", side_effect=fake_run_zfs), \
            patch("nazman.utils.zfs_query.run_zfs", side_effect=fake_run_zfs), \
         patch("nazman.managers.zfs_backup_manager.run_command", new=AsyncMock()), \
         patch("nazman.managers.zfs_backup_manager.os.statvfs",
               side_effect=lambda p: type("S", (), {"f_frsize": 1, "f_bavail": 0})()), \
         patch.object(ZfsBackupManager, "_dataset_exists", new=AsyncMock(return_value=True)), \
         patch.object(ZfsBackupManager, "mount_backup_disk", new=AsyncMock()), \
         patch.object(ZfsBackupManager, "estimate_needed", new=AsyncMock(return_value=1 << 20)), \
         patch.object(BackupGroupService, "_notify", new=AsyncMock()) as notify:
        session = await backup_groups.start_session(db_session, group.id, "full")
        result = await backup_groups.run_session(db_session, session.id)

    assert result["status"] == "needs_disk"
    session_row = db_session.get(BackupSession, session.id)
    assert "no disk with room" in session_row.error
    assert db_session.get(BackupGroup, group.id).needs_disk is True
    keys = [c.args[0] for c in notify.call_args_list]
    assert f"backup:group:{group.id}:needs_disk" in keys


@pytest.mark.asyncio
async def test_set_active_disk_picks_the_writer(db_session, tmp_path):
    group, sets, disks = _mk_group(db_session, tmp_path, disks_per_set=2)

    desc = await backup_groups.set_active_disk(db_session, sets[0].id, disks[1].id)

    assert db_session.get(BackupSet, sets[0].id).active_disk_id == disks[1].id
    assert desc["active_disk_id"] == disks[1].id
    labels = {d["id"]: d["is_active"] for d in desc["disks"]}
    assert labels == {disks[0].id: False, disks[1].id: True}


@pytest.mark.asyncio
async def test_set_active_disk_rejects_disk_from_another_set(db_session, tmp_path):
    group, sets, disks = _mk_group(db_session, tmp_path, disk_count=2, positions=2)
    with pytest.raises(Exception) as exc:
        await backup_groups.set_active_disk(db_session, sets[0].id, disks[1].id)
    assert "not in this backup set" in str(exc.value)
    assert db_session.get(BackupSet, sets[0].id).active_disk_id == disks[0].id


@pytest.mark.asyncio
async def test_set_active_disk_blocks_during_running_session(db_session, tmp_path):
    group, sets, disks = _mk_group(db_session, tmp_path, disks_per_set=2)
    session = BackupSession(
        group_id=group.id, trigger="manual", status="running",
    )
    db_session.add(session)
    db_session.commit()

    with pytest.raises(Exception) as exc:
        await backup_groups.set_active_disk(db_session, sets[0].id, disks[1].id)
    assert "backup is running" in str(exc.value)
    assert db_session.get(BackupSet, sets[0].id).active_disk_id == disks[0].id


@pytest.mark.asyncio
async def test_group_needs_disk_when_set_has_no_media(db_session, tmp_path, monkeypatch):
    group = BackupGroup(name="Empty")
    db_session.add(group)
    db_session.flush()
    db_session.add(BackupGroupDataset(group_id=group.id, dataset_name="tank/media"))
    bset = BackupSet(group_id=group.id, position=0, label="set 1")
    db_session.add(bset)
    db_session.flush()
    group.active_set_id = bset.id
    db_session.commit()

    with patch.object(BackupGroupService, "_notify", new=AsyncMock()) as notify:
        session = await backup_groups.start_session(db_session, group.id, "full")
        result = await backup_groups.run_session(db_session, session.id)

    assert result["status"] == "needs_disk"
    assert "No usable disk" in db_session.get(BackupSession, session.id).error
    keys = [c.args[0] for c in notify.call_args_list]
    assert f"backup:group:{group.id}:needs_disk" in keys


@pytest.mark.asyncio
async def test_group_rejects_session_when_not_ready(db_session):
    group = BackupGroup(name="Bare")
    db_session.add(group)
    db_session.commit()
    with pytest.raises(ValidationError, match="no datasets"):
        await backup_groups.start_session(db_session, group.id, "full")


@pytest.mark.asyncio
async def test_group_backup_dataset_capacity_insufficient_reports_needs_space(
    db_session, tmp_path, monkeypatch,
):
    """The engine reports needs_space rather than failing the dataset itself."""
    group, sets, disks = _mk_group(db_session, tmp_path, disk_count=1)
    monkeypatch.setattr(Path, "is_mount", lambda self: str(self) == str(tmp_path / "vol0"))

    class FakeStatvfs:
        f_frsize = 1024
        f_bavail = 1  # needed (used * margin) far exceeds this

    async def fake_run_zfs(*args, **kwargs):
        cmd = list(args)
        if cmd and cmd[0] == "list":
            if "-t" in cmd and "snapshot" in cmd:
                return ("", "", 0)
            return ("tank/media", "", 0)
        return ("", "", 0)

    run = BackupRun(
        group_id=group.id, backup_set_id=sets[0].id, dataset_name="tank/media",
        backup_disk_id=disks[0].id, backup_type="full", status="running", phase="pending",
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)

    with patch("nazman.managers.zfs_backup_manager.run_zfs", side_effect=fake_run_zfs), \
            patch("nazman.utils.zfs_query.run_zfs", side_effect=fake_run_zfs), \
         patch.object(ZfsBackupManager, "mount_backup_disk", new=AsyncMock()), \
         patch("nazman.managers.zfs_backup_manager.os.statvfs", return_value=FakeStatvfs()), \
         patch.object(ZfsBackupManager, "estimate_needed", new=AsyncMock(return_value=1 << 20)), \
         patch("nazman.managers.zfs_backup_manager.run_pipeline", new=AsyncMock()):
        result = await zfs_backup_manager.backup_dataset(db_session, disks[0], run)

    assert result["status"] == "needs_space"
    assert result["needed_bytes"] > 0
    # The snapshot taken before the space check is kept for the retry.
    assert run.snapshot is not None


@pytest.mark.asyncio
async def test_estimate_capacity(db_session):
    with patch.object(zfs_backup_manager, "estimate_full_size", new=AsyncMock(return_value=1000)):
        needed = await zfs_backup_manager.estimate_needed("tank/media")
    assert needed == int(1000 * zfs_backup_manager.settings.backup_full_margin)


@pytest.mark.asyncio
async def test_api_list_backup_disks_empty(client, db_session):
    response = client.get("/api/backup-zfs/disks")
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_api_list_backup_runs(client, db_session):
    response = client.get("/api/backup-zfs/runs")
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_api_start_group_backup_returns_202(client, db_session, tmp_path):
    """Starting a backup is a group operation, and returns immediately."""
    group, sets, disks = _mk_group(db_session, tmp_path)

    with patch("nazman.wiring.get_container") as mock_container:
        mock_container.return_value.backup_groups.start_session = AsyncMock(
            side_effect=lambda db, gid, t: BackupSession(
                id=99, group_id=gid, trigger=t, status="running",
            ),
        )
        response = client.post(f"/api/backup-groups/{group.id}/backup",
                               json={"backup_type": "incremental"})

    assert response.status_code == 202, response.text
    data = response.json()
    assert data["session_id"] == 99
    assert data["group_id"] == group.id
    assert data["trigger"] == "incremental"


@pytest.mark.asyncio
async def test_api_used_disks_empty(client, db_session):
    with patch.object(ZfsManager, "get_pool_members", new=AsyncMock(return_value={})):
        response = client.get("/api/backup-zfs/disks/used")
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_api_used_disks_lists_whole_and_partition(client, db_session):
    whole_disk = Disk(
        by_id="/dev/disk/by-id/usb-CAND", model="USB", serial="CAND1",
        size_bytes=10**11, disk_type="hdd", is_os_disk=False,
    )
    db_session.add(whole_disk)
    db_session.commit()
    part_disk = Disk(
        by_id="/dev/disk/by-id/usb-OTHER", model="USB", serial="OTH1",
        size_bytes=10**11, disk_type="hdd", is_os_disk=False,
    )
    db_session.add(part_disk)
    db_session.commit()
    db_session.add_all([
        BackupDisk(disk_id=whole_disk.id, mount_point="/tmp/mnt1", fs_uuid="UUU1"),
        BackupDisk(disk_id=part_disk.id, slot_uuid="slot-abc",
                   mount_point="/tmp/mnt2", fs_uuid="UUU2"),
    ])
    db_session.commit()

    with patch.object(ZfsManager, "get_pool_members", new=AsyncMock(return_value={})):
        response = client.get("/api/backup-zfs/disks/used")
    assert response.status_code == 200, response.text
    rows = response.json()
    assert {"disk_id": whole_disk.id, "slot_uuid": None} in rows
    assert {"disk_id": part_disk.id, "slot_uuid": "slot-abc"} in rows


@pytest.mark.asyncio
async def test_api_used_disks_includes_pool_members(client, db_session):
    part_disk = Disk(
        by_id="/dev/disk/by-id/sata-PART", model="HDD", serial="PT1",
        size_bytes=10**11, disk_type="hdd", is_os_disk=False,
    )
    db_session.add(part_disk)
    db_session.commit()
    whole_disk = Disk(
        by_id="/dev/disk/by-id/sata-WHOLE", model="HDD", serial="WH1",
        size_bytes=10**12, disk_type="hdd", is_os_disk=False,
    )
    db_session.add(whole_disk)
    db_session.commit()

    members = {
        "/dev/disk/by-id/sata-PART-part2": "poolA",
        "/dev/disk/by-id/sata-WHOLE": "poolB",
    }
    with patch.object(ZfsManager, "get_pool_members", new=AsyncMock(return_value=members)), \
         patch("nazman.managers.zfs_backup_manager.get_device_path", return_value="/dev/sdc"):
        response = client.get("/api/backup-zfs/disks/used")

    assert response.status_code == 200, response.text
    rows = response.json()
    # A partition member claims the whole disk just like a whole-disk member.
    assert {"disk_id": part_disk.id, "slot_uuid": None} in rows
    assert {"disk_id": whole_disk.id, "slot_uuid": None} in rows


async def _add_declare_disk(db, by_id="/dev/disk/by-id/ata-X"):
    disk = Disk(by_id=by_id, model="HDD", serial="X1", size_bytes=10**11,
                disk_type="hdd", is_os_disk=False)
    db.add(disk)
    db.commit()
    return disk


@pytest.mark.asyncio
async def test_declare_whole_disk_wipes_and_formats_part1(db_session, tmp_path, monkeypatch):
    disk = await _add_declare_disk(db_session)
    monkeypatch.setattr(zfs_backup_manager.settings, "backup_mount_base", str(tmp_path))
    cmds = []

    async def fake_run_command(cmd, **kwargs):
        cmds.append(cmd)
        return ("", "", 0)

    with patch("nazman.managers.zfs_backup_manager.run_command", side_effect=fake_run_command), \
         patch.object(ZfsManager, "get_pool_members", new=AsyncMock(return_value={})), \
         patch.object(zfs_backup_manager, "_ensure_unused", new=AsyncMock()), \
         patch("nazman.managers.zfs_backup_manager.get_device_path", return_value="/dev/sdb"), \
         patch.object(zfs_backup_manager, "_fs_uuid", new=AsyncMock(return_value="FSID1")):
        rec = await zfs_backup_manager.declare_backup_disk(
            db_session, disk.id, confirm=True, label="Backup 1")

    assert rec["device_path"] == "/dev/disk/by-id/ata-X-part1"
    assert rec["slot_uuid"] is None
    assert rec["partition_number"] == 1
    assert rec["label"] == "Backup 1"
    assert rec["fs_uuid"] == "FSID1"
    parted = [c for c in cmds if c[0] == "parted"]
    assert any("mklabel" in c for c in parted)
    assert ("mkfs.ext4", "-F", "/dev/disk/by-id/ata-X-part1") in [tuple(c) for c in cmds]


@pytest.mark.asyncio
async def test_declare_seeds_config_bundle_on_new_volume(db_session, tmp_path, monkeypatch):
    """A freshly declared volume is seeded with the configuration so it can
    rebuild the system even before any dataset backup runs."""
    disk = await _add_declare_disk(db_session)
    monkeypatch.setattr(zfs_backup_manager.settings, "backup_mount_base", str(tmp_path))

    backup = MagicMock()
    backup.capture_config_bundle = AsyncMock(return_value={"id": "x"})

    async def fake_run_command(cmd, **kwargs):
        return ("", "", 0)

    with patch("nazman.managers.zfs_backup_manager.run_command", side_effect=fake_run_command), \
         patch.object(ZfsManager, "get_pool_members", new=AsyncMock(return_value={})), \
         patch.object(zfs_backup_manager, "_ensure_unused", new=AsyncMock()), \
         patch("nazman.managers.zfs_backup_manager.get_device_path", return_value="/dev/sdb"), \
         patch.object(zfs_backup_manager, "backup", backup), \
         patch.object(zfs_backup_manager, "_fs_uuid", new=AsyncMock(return_value="FSID1")):
        await zfs_backup_manager.declare_backup_disk(
            db_session, disk.id, confirm=True, label="Backup 1")

    backup.capture_config_bundle.assert_awaited_once()
    args = backup.capture_config_bundle.await_args.args
    assert args[0] is db_session
    assert str(args[1]) == str(tmp_path / "FSID1")


@pytest.mark.asyncio
async def test_declare_partition_formats_in_place_no_parted(db_session, tmp_path, monkeypatch):
    disk = await _add_declare_disk(db_session)
    monkeypatch.setattr(zfs_backup_manager.settings, "backup_mount_base", str(tmp_path))
    cmds = []

    async def fake_run_command(cmd, **kwargs):
        cmds.append(cmd)
        return ("", "", 0)

    async def fake_read_slot_uuids(paths):
        return {"/dev/sdb": {"partitions": [
            {"name": "sdb1", "partlabel": "nazman:slot-abc", "slot_uuid": "slot-abc", "size_bytes": 5 * 10**10},
        ]}}

    with patch("nazman.managers.zfs_backup_manager.run_command", side_effect=fake_run_command), \
         patch.object(ZfsManager, "get_pool_members", new=AsyncMock(return_value={})), \
         patch.object(zfs_backup_manager, "_ensure_unused", new=AsyncMock()), \
         patch("nazman.managers.zfs_backup_manager.get_device_path", return_value="/dev/sdb"), \
         patch("nazman.managers.zfs_backup_manager.read_slot_uuids", side_effect=fake_read_slot_uuids), \
         patch.object(zfs_backup_manager, "_fs_uuid", new=AsyncMock(return_value="FSID2")):
        rec = await zfs_backup_manager.declare_backup_disk(
            db_session, disk.id, confirm=True, slot_uuid="slot-abc", label="Media 2")

    assert rec["slot_uuid"] == "slot-abc"
    assert rec["partition_number"] == 1
    assert rec["device_path"] == "/dev/disk/by-id/ata-X-part1"
    assert rec["label"] == "Media 2"
    assert [c for c in cmds if c[0] == "parted"] == []
    assert ("mkfs.ext4", "-F", "/dev/disk/by-id/ata-X-part1") in [tuple(c) for c in cmds]
    assert ("wipefs", "-a", "/dev/disk/by-id/ata-X-part1") in [tuple(c) for c in cmds]


@pytest.mark.asyncio
async def test_declare_rejects_unknown_slot(db_session, monkeypatch):
    disk = await _add_declare_disk(db_session)
    monkeypatch.setattr(zfs_backup_manager.settings, "backup_mount_base", "/tmp/zzz")

    async def fake_read_slot_uuids(paths):
        return {"/dev/sdb": {"partitions": []}}

    with patch.object(ZfsManager, "get_pool_members", new=AsyncMock(return_value={})), \
         patch("nazman.managers.zfs_backup_manager.get_device_path", return_value="/dev/sdb"), \
         patch("nazman.managers.zfs_backup_manager.read_slot_uuids", side_effect=fake_read_slot_uuids):
        with pytest.raises(ValidationError):
            await zfs_backup_manager.declare_backup_disk(
                db_session, disk.id, confirm=True, slot_uuid="missing-slot")

    assert db_session.query(BackupDisk).count() == 0


@pytest.mark.asyncio
async def test_declare_rejects_already_declared_disk(db_session, monkeypatch):
    disk = await _add_declare_disk(db_session)
    existing = BackupDisk(disk_id=disk.id, slot_uuid="slot-abc",
                          mount_point="/tmp/mnt", fs_uuid="EEX")
    db_session.add(existing)
    db_session.commit()
    monkeypatch.setattr(zfs_backup_manager.settings, "backup_mount_base", "/tmp/zzz")

    with patch.object(ZfsManager, "get_pool_members", new=AsyncMock(return_value={})), \
         patch("nazman.managers.zfs_backup_manager.get_device_path", return_value="/dev/sdb"):
        with pytest.raises(ValidationError):
            await zfs_backup_manager.declare_backup_disk(db_session, disk.id, confirm=True)


@pytest.mark.asyncio
async def test_declare_rejects_without_confirm_and_os_disk(db_session, monkeypatch):
    osd = Disk(by_id="/dev/disk/by-id/nvme-OS", model="NVMe", serial="OS1",
               size_bytes=10**11, disk_type="nvme", is_os_disk=True)
    db_session.add(osd)
    db_session.commit()
    monkeypatch.setattr(zfs_backup_manager.settings, "backup_mount_base", "/tmp/zzz")

    with pytest.raises(ValidationError):
        await zfs_backup_manager.declare_backup_disk(db_session, osd.id, confirm=True)
    with pytest.raises(ValidationError):
        await zfs_backup_manager.declare_backup_disk(db_session, osd.id, confirm=False)
    assert db_session.query(BackupDisk).count() == 0


@pytest.mark.asyncio
async def test_declare_rejects_partition_pool_member(db_session, monkeypatch):
    disk = await _add_declare_disk(db_session)
    monkeypatch.setattr(zfs_backup_manager.settings, "backup_mount_base", "/tmp/zzz")

    async def fake_read_slot_uuids(paths):
        return {"/dev/sdb": {"partitions": [
            {"name": "sdb1", "partlabel": "nazman:slot-abc", "slot_uuid": "slot-abc", "size_bytes": 5 * 10**10},
        ]}}

    members = {"/dev/disk/by-id/ata-X-part1": "poolA"}
    with patch.object(ZfsManager, "get_pool_members", new=AsyncMock(return_value=members)), \
         patch("nazman.managers.zfs_backup_manager.get_device_path", return_value="/dev/sdb"), \
         patch("nazman.managers.zfs_backup_manager.read_slot_uuids", side_effect=fake_read_slot_uuids):
        with pytest.raises(ValidationError) as ei:
            await zfs_backup_manager.declare_backup_disk(
                db_session, disk.id, confirm=True, slot_uuid="slot-abc")
    assert "pool 'poolA'" in str(ei.value)


@pytest.mark.asyncio
async def test_declare_rejects_whole_disk_when_partition_is_pool_member(db_session):
    disk = await _add_declare_disk(db_session)
    members = {"/dev/disk/by-id/ata-X-part1": "poolA"}
    with patch.object(ZfsManager, "get_pool_members", new=AsyncMock(return_value=members)):
        with pytest.raises(ValidationError) as ei:
            await zfs_backup_manager.declare_backup_disk(db_session, disk.id, confirm=True)
    assert "pool 'poolA'" in str(ei.value)
    assert db_session.query(BackupDisk).count() == 0


@pytest.mark.asyncio
async def test_ensure_unused_rejects_md_member_and_mount():
    md_json = json.dumps({"blockdevices": [{"name": "sdb", "type": "disk", "children": [
        {"name": "sdb1", "type": "part", "children": [{"name": "md127", "type": "md", "children": []}]}]}]})
    with patch("nazman.managers.zfs_backup_manager.run_command",
               return_value=(md_json, "", 0)):
        with pytest.raises(ValidationError) as ei:
            await zfs_backup_manager._ensure_unused("/dev/sdb")
    assert "md127" in str(ei.value)

    mount_json = json.dumps({"blockdevices": [{"name": "sdb", "type": "disk", "children": [
        {"name": "sdb1", "type": "part", "mountpoint": "/mnt/x"}]}]})
    with patch("nazman.managers.zfs_backup_manager.run_command",
               return_value=(mount_json, "", 0)):
        with pytest.raises(ValidationError) as ei:
            await zfs_backup_manager._ensure_unused("/dev/sdb")
    assert "mounted at /mnt/x" in str(ei.value)


@pytest.mark.asyncio
async def test_ensure_unused_allows_free_device():
    free_json = json.dumps({"blockdevices": [{"name": "sdb", "type": "disk", "children": [
        {"name": "sdb1", "type": "part"}]}]})
    with patch("nazman.managers.zfs_backup_manager.run_command",
               return_value=(free_json, "", 0)):
        await zfs_backup_manager._ensure_unused("/dev/sdb")

    with patch("nazman.managers.zfs_backup_manager.run_command",
               return_value=("", "no such device", 1)):
        await zfs_backup_manager._ensure_unused("/dev/sdb")


@pytest.mark.asyncio
async def test_declare_propagates_mkfs_failure(db_session):
    disk = await _add_declare_disk(db_session)
    db_session.commit()

    async def fake_run_command(cmd, **kwargs):
        if cmd[0] == "mkfs.ext4":
            raise CommandError(command="mkfs.ext4 -F /dev/disk/by-id/ata-X-part1",
                               returncode=1, stderr="device or resource busy")
        return ("", "", 0)

    with patch("nazman.managers.zfs_backup_manager.run_command", side_effect=fake_run_command), \
         patch.object(ZfsManager, "get_pool_members", new=AsyncMock(return_value={})), \
         patch.object(zfs_backup_manager, "_ensure_unused", new=AsyncMock()), \
         patch("nazman.managers.zfs_backup_manager.get_device_path", return_value="/dev/sdb"):
        with pytest.raises(BackupError) as ei:
            await zfs_backup_manager.declare_backup_disk(db_session, disk.id, confirm=True)
    assert "device or resource busy" in str(ei.value)
    assert db_session.query(BackupDisk).count() == 0


def test_parse_mdadm_examine():
    stdout = """\
/dev/sdb1:
          Magic : a92b4efc
        Version : 1.2
          Name : pootlenaz:0
  Creation Time : ...
"""
    info = zfs_backup_manager._parse_mdadm_examine(stdout)
    assert info == {"name": "pootlenaz:0", "version": "1.2"}
    assert zfs_backup_manager._parse_mdadm_examine("") is None


EXAMINE_SB = """\
/dev/sdb1:
          Version : 1.2
          Name : pootlenaz:0
"""


def _cmd_log_fake(by_device=None, stop_rc=0):
    """Return a fake run_command that routes by command/device, logging calls.

    ``by_device`` maps device kernel path -> mdadm examine stdout (non-empty)
    to serve for ``mdadm --examine``; everything else succeeds.
    """
    by_device = by_device or {}
    calls = []

    async def fake_run_command(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "mdadm" and cmd[1] == "--examine":
            for dev, out in by_device.items():
                if cmd[2] == dev:
                    return (out, "", 0)
            return ("", "", 0)
        if cmd[0] == "mdadm" and cmd[1] == "--stop":
            return ("", "", stop_rc)
        if cmd[0] == "lsblk":
            return ('{"blockdevices": []}', "", 0)
        return ("", "", 0)

    return fake_run_command, calls


@pytest.mark.asyncio
async def test_api_raid_info_endpoint(client, db_session):
    disk = await _add_declare_disk(db_session)
    payload = {"device": "/dev/sdb", "md": [
        {"device": "/dev/sdb1", "name": "pootlenaz:0", "version": "1.2",
         "os_backing": False},
    ], "partitions": [
        {"number": 1, "device_path": "/dev/sdb1", "slot_uuid": None,
         "kind": "backup_disk", "detail": "declared backup volume"},
    ]}
    with patch.object(ZfsBackupManager, "get_raid_info",
                      new=AsyncMock(return_value=payload)):
        response = client.get(f"/api/backup-zfs/disks/{disk.id}/raid-info")
    assert response.status_code == 200, response.text
    assert response.json()["md"][0]["name"] == "pootlenaz:0"
    assert response.json()["partitions"][0]["kind"] == "backup_disk"


def _raid_lsblk(parts):
    """A fake ``run_command`` serving an lsblk partition tree for classification."""
    nodes = []
    for p in parts:
        node = {"name": p["name"], "type": "part", "fstype": p.get("fstype"),
                "uuid": p.get("uuid"), "partlabel": p.get("partlabel"),
                "partuuid": p.get("partuuid")}
        nodes.append(node)
    json_out = json.dumps({"blockdevices": [{"name": "sdb", "type": "disk",
                                             "children": nodes}]})

    async def fake_run_command(cmd, **kwargs):
        if cmd[0] == "lsblk":
            return (json_out, "", 0)
        return ("", "", 0)

    return fake_run_command


def _classify_patch(fake_run_command, pool_members=None, md=None):
    return (
        patch("nazman.managers.zfs_backup_manager.run_command",
              side_effect=fake_run_command),
        patch("nazman.managers.zfs_backup_manager.get_device_path",
              return_value="/dev/sdb"),
        patch("nazman.managers.zfs_backup_manager.read_slot_uuids",
              new=AsyncMock(return_value={})),
        patch.object(zfs_backup_manager, "_md_superblocks",
                     new=AsyncMock(return_value=md or [])),
        patch.object(ZfsManager, "get_pool_members",
                     new=AsyncMock(return_value=pool_members or {})),
        patch("nazman.managers.zfs_backup_manager.os_reserved_partition_names",
              new=AsyncMock(return_value=set())),
    )


@contextmanager
def _classified(fake_run_command, pool_members=None, md=None):
    with ExitStack() as stack:
        for p in _classify_patch(fake_run_command, pool_members, md):
            stack.enter_context(p)
        yield


@pytest.mark.asyncio
async def test_raid_info_pinpoints_declared_backup_membership(db_session):
    disk = await _add_declare_disk(db_session)
    group = BackupGroup(name="Home")
    db_session.add(group)
    db_session.flush()
    bset = BackupSet(group_id=group.id, position=0, label="Offsite")
    db_session.add(bset)
    db_session.flush()
    rec = BackupDisk(
        disk_id=disk.id, slot_uuid=None, partition_number=1, fs_type="ext4",
        mount_point="/mnt/backup", fs_uuid="FSID1", backup_set_id=bset.id,
    )
    db_session.add(rec)
    db_session.flush()
    db_session.add(BackupRun(
        backup_disk_id=rec.id, dataset_name="tank/media",
        backup_type="full", status="success",
    ))
    db_session.add(BackupRun(
        backup_disk_id=rec.id, dataset_name="tank/photos",
        backup_type="incremental", status="success",
    ))
    db_session.commit()

    fake = _raid_lsblk([{"name": "sdb1", "fstype": "ext4", "uuid": "FSID1",
                         "partlabel": None, "partuuid": "PUUID1"}])
    with _classified(fake):
        info = await zfs_backup_manager.get_raid_info(db_session, disk.id)

    part = info["partitions"][0]
    assert part["kind"] == "backup_disk"
    assert part["number"] == 1
    assert part["slot_uuid"] == "PUUID1"
    assert "'Offsite'" in part["detail"]
    assert "group 'Home'" in part["detail"]
    assert "tank/media" in part["detail"]
    assert "tank/photos" in part["detail"]


@pytest.mark.asyncio
async def test_raid_info_matches_slot_declaration(db_session):
    disk = await _add_declare_disk(db_session)
    db_session.add(BackupDisk(
        disk_id=disk.id, slot_uuid="slot-abc", partition_number=1, fs_type="ext4",
        mount_point="/mnt/backup", fs_uuid="FSID1",
    ))
    db_session.commit()

    fake = _raid_lsblk([{"name": "sdb1", "fstype": "ext4", "uuid": "FSID1",
                         "partlabel": "nazman:slot-abc", "partuuid": "PUUID1"}])
    with _classified(fake), patch.object(
        zfs_backup_manager, "_resolve_partition",
        new=AsyncMock(return_value="/dev/sda1"),
    ):
        info = await zfs_backup_manager.get_raid_info(
            db_session, disk.id, slot_uuid="slot-abc")

    assert info["partitions"][0]["kind"] == "backup_disk"
    assert info["partitions"][0]["slot_uuid"] == "slot-abc"


@pytest.mark.asyncio
async def test_raid_info_classifies_zfs_pool_member(db_session):
    disk = await _add_declare_disk(db_session)
    fake = _raid_lsblk([{"name": "sdb1", "fstype": "zfs_member",
                         "uuid": None, "partlabel": None, "partuuid": "P1"}])
    with _classified(fake, pool_members={"sdb1": "tank"}):
        info = await zfs_backup_manager.get_raid_info(db_session, disk.id)

    part = info["partitions"][0]
    assert part["kind"] == "zfs_pool"
    assert "pool 'tank'" in part["detail"]


@pytest.mark.asyncio
async def test_raid_info_labels_foreign_members(db_session):
    disk = await _add_declare_disk(db_session)
    fake = _raid_lsblk([
        {"name": "sdb1", "fstype": "linux_raid_member", "uuid": None,
         "partlabel": None, "partuuid": "P1"},
        {"name": "sdb2", "fstype": "LVM2_member", "uuid": None,
         "partlabel": None, "partuuid": "P2"},
        {"name": "sdb3", "fstype": "swap", "uuid": None,
         "partlabel": None, "partuuid": "P3"},
        {"name": "sdb4", "fstype": "crypto_LUKS", "uuid": None,
         "partlabel": None, "partuuid": "P4"},
        {"name": "sdb5", "fstype": "ext4", "uuid": "UUU5",
         "partlabel": None, "partuuid": "P5"},
    ])
    md = [{"device": "/dev/sdb5", "name": "r2", "version": "1.2",
           "os_backing": False}]
    with _classified(fake, md=md):
        info = await zfs_backup_manager.get_raid_info(db_session, disk.id)

    kinds = {p["kind"] for p in info["partitions"]}
    assert kinds == {"md_raid", "lvm", "swap", "luks"}
    assert any("'r2'" in p["detail"] for p in info["partitions"])
    assert any("(v1.2)" in p["detail"] for p in info["partitions"])


@pytest.mark.asyncio
async def test_declare_requires_wipe_raid_when_superblock_present(db_session):
    disk = await _add_declare_disk(db_session)
    fake, _ = _cmd_log_fake({"/dev/sdb1": EXAMINE_SB})

    with patch("nazman.managers.zfs_backup_manager.run_command", side_effect=fake), \
         patch.object(ZfsManager, "get_pool_members", new=AsyncMock(return_value={})), \
         patch("nazman.managers.zfs_backup_manager.get_device_path", return_value="/dev/sdb"), \
         patch("nazman.managers.zfs_backup_manager.read_slot_uuids", new=AsyncMock(return_value={
             "/dev/sdb": {"partitions": [
                 {"name": "sdb1", "slot_uuid": "slot-abc", "size_bytes": 10},
             ]},
         })):
        with pytest.raises(ValidationError) as ei:
            await zfs_backup_manager.declare_backup_disk(
                db_session, disk.id, confirm=True)
    assert "software RAID metadata" in str(ei.value)


@pytest.mark.asyncio
async def test_declare_wipe_raid_stops_array_and_zeros_superblock(db_session, tmp_path):
    disk = await _add_declare_disk(db_session)

    def fake_lsblk(cmd, **kwargs):
        # lsblk on the whole disk shows the md array on sdb1.
        return ('{"blockdevices": [{"name": "sdb", "type": "disk", "children": ['
                '{"name": "sdb1", "type": "part", "children": ['
                '{"name": "md127", "type": "md", "children": []}]}]}]}', "", 0)

    async def fake_run_command(cmd, **kwargs):
        if cmd[0] == "mdadm" and cmd[1] == "--examine":
            if cmd[2] == "/dev/sdb1":
                return (EXAMINE_SB, "", 0)
            return ("", "", 0)
        if cmd[0] == "lsblk":
            return fake_lsblk(cmd)
        return ("", "", 0)

    cmds = []

    async def record_run_command(cmd, **kwargs):
        cmds.append(cmd)
        return await fake_run_command(cmd)

    with patch("nazman.managers.zfs_backup_manager.run_command",
               side_effect=record_run_command), \
         patch.object(ZfsManager, "get_pool_members", new=AsyncMock(return_value={})), \
         patch.object(zfs_backup_manager, "_ensure_unused", new=AsyncMock()), \
         patch("nazman.managers.zfs_backup_manager.get_device_path", return_value="/dev/sdb"), \
         patch("nazman.managers.zfs_backup_manager.read_slot_uuids", new=AsyncMock(return_value={
             "/dev/sdb": {"partitions": [
                 {"name": "sdb1", "partlabel": "nazman:slot-1", "slot_uuid": "slot-1",
                  "size_bytes": 10},
             ]},
         })), \
         patch.object(zfs_backup_manager, "_fs_uuid", new=AsyncMock(return_value="FSID3")), \
         patch.object(zfs_backup_manager.settings, "backup_mount_base", str(tmp_path)):
        rec = await zfs_backup_manager.declare_backup_disk(
            db_session, disk.id, confirm=True, wipe_raid=True)

    assert rec["fs_uuid"] == "FSID3"
    assert ("mdadm", "--stop", "/dev/md127") in [tuple(c) for c in cmds]
    assert ("mdadm", "--zero-superblock", "/dev/sdb1") in [tuple(c) for c in cmds]


@pytest.mark.asyncio
async def test_declare_wipe_raid_refuses_os_array(db_session):
    disk = await _add_declare_disk(db_session)

    async def fake_run_command(cmd, **kwargs):
        if cmd[0] == "mdadm" and cmd[1] == "--examine":
            if cmd[2] == "/dev/sdb1":
                return (EXAMINE_SB, "", 0)
            return ("", "", 0)
        if cmd[0] == "lsblk":
            return ('{"blockdevices": []}', "", 0)
        return ("", "", 0)

    with patch("nazman.managers.zfs_backup_manager.run_command", side_effect=fake_run_command), \
         patch.object(ZfsManager, "get_pool_members", new=AsyncMock(return_value={})), \
         patch("nazman.managers.zfs_backup_manager.get_device_path", return_value="/dev/sdb"), \
         patch("nazman.managers.zfs_backup_manager.read_slot_uuids", new=AsyncMock(return_value={
             "/dev/sdb": {"partitions": [
                 {"name": "sdb1", "slot_uuid": "slot-abc", "size_bytes": 10},
             ]},
         })), \
         patch("nazman.managers.zfs_backup_manager.os_reserved_partition_names",
               new=AsyncMock(return_value={"sdb1"})):
        with pytest.raises(ValidationError) as ei:
            await zfs_backup_manager.declare_backup_disk(
                db_session, disk.id, confirm=True, wipe_raid=True)
    assert "part of the OS" in str(ei.value)


@pytest.mark.asyncio
async def test_api_restore_run_ok(client, db_session, tmp_path):
    bd = BackupDisk(
        disk_id=999, mount_point=str(tmp_path), fs_uuid="AAA",
    )
    db_session.add(bd)
    db_session.flush()
    run = BackupRun(
        dataset_name="tank/media", backup_disk_id=bd.id,
        backup_type="full", stream_file="/tmp/nonexistent.zfs.gz",
        snapshot="tank/media@backup-x", status="success", size_bytes=10,
    )
    db_session.add(run)
    db_session.commit()

    with patch.object(ZfsBackupManager, "restore_dataset", new=AsyncMock(
        return_value={"dataset": "tank/media", "source": "/tmp/nonexistent.zfs.gz", "force": False}
    )):
        response = client.post(f"/api/backup-zfs/runs/{run.id}/restore", json={"target_dataset": "tank/media"})
    assert response.status_code == 200, response.text
    assert response.json()["dataset"] == "tank/media"


@pytest.mark.asyncio
async def test_recover_orphaned_backups_fails_stale_running_rows(db_session, tmp_path):
    """Startup recovery fails rows a dead process left 'running'."""
    group, sets, disks = _mk_group(db_session, tmp_path, disk_count=1)

    stale_session = BackupSession(
        group_id=group.id, backup_set_id=sets[0].id, backup_disk_id=disks[0].id,
        trigger="full", status="running", phase="sending")
    db_session.add(stale_session)
    db_session.flush()
    run = BackupRun(
        session_id=stale_session.id, group_id=group.id,
        backup_set_id=sets[0].id, backup_disk_id=disks[0].id,
        dataset_name="tank/media", backup_type="full",
        status="running", phase="sending", snapshot="tank/media@backup-orphan")
    db_session.add(run)
    # Terminal and non-runnable rows must never be touched.
    ok_session = BackupSession(
        group_id=group.id, backup_set_id=sets[0].id, backup_disk_id=disks[0].id,
        trigger="incremental", status="success", phase=None)
    wait_session = BackupSession(
        group_id=group.id, backup_set_id=sets[0].id, backup_disk_id=disks[0].id,
        trigger="full", status="needs_disk", phase=None)
    db_session.add_all([ok_session, wait_session])
    db_session.commit()

    with patch.object(ZfsBackupManager, "_discard_snapshot", new=AsyncMock()) as discard:
        await backup_groups.recover_orphaned_backups(db_session)

    stale = db_session.get(BackupSession, stale_session.id)
    assert stale.status == "failed"
    assert stale.phase is None
    assert "restart" in stale.error
    assert stale.completed_at is not None
    run_row = db_session.get(BackupRun, run.id)
    assert run_row.status == "failed"
    assert run_row.phase is None
    assert run_row.completed_at is not None
    discard.assert_awaited_once_with("tank/media@backup-orphan")
    assert db_session.get(BackupSession, ok_session.id).status == "success"
    assert db_session.get(BackupSession, wait_session.id).status == "needs_disk"


@pytest.mark.asyncio
async def test_recover_orphaned_backups_is_noop_when_nothing_running(db_session, tmp_path):
    """No running rows means no writes, no snapshot destroy calls."""
    group, sets, disks = _mk_group(db_session, tmp_path, disk_count=1)
    session = BackupSession(
        group_id=group.id, backup_set_id=sets[0].id, backup_disk_id=disks[0].id,
        trigger="full", status="failed", phase=None)
    db_session.add(session)
    db_session.commit()

    with patch.object(ZfsBackupManager, "_discard_snapshot", new=AsyncMock()) as discard:
        await backup_groups.recover_orphaned_backups(db_session)

    assert db_session.get(BackupSession, session.id).status == "failed"
    assert discard.await_count == 0


@pytest.mark.asyncio
async def test_sync_scheduled_tasks_creates_group_backup_tasks(db_session, tmp_path):
    """A group with both crons gets one ZFS_BACKUP job per cron."""
    from nazman.models.scheduler import ScheduledTask, TaskType

    group, sets, disks = _mk_group(db_session, tmp_path)
    group.full_cron = "0 2 * * 0"
    group.incremental_cron = "0 3 * * *"
    db_session.commit()

    await backup_groups.sync_scheduled_tasks(db_session)

    tasks = db_session.query(ScheduledTask).filter(
        ScheduledTask.task_type == TaskType.ZFS_BACKUP.value).all()
    assert len(tasks) == 2
    assert {t.config["type"] for t in tasks} == {"full", "incremental"}
    # The job identifies the group, not a single dataset/disk pair.
    assert {t.config["group_id"] for t in tasks} == {group.id}
    assert {t.schedule for t in tasks} == {"0 2 * * 0", "0 3 * * *"}


@pytest.mark.asyncio
async def test_sync_scheduled_tasks_skips_disabled_and_dataless_groups(db_session, tmp_path):
    """A disabled group, or one with no datasets, gets no cron job."""
    from nazman.models.scheduler import ScheduledTask, TaskType

    group, sets, disks = _mk_group(db_session, tmp_path)
    group.full_cron = "0 2 * * 0"
    group.enabled = False
    db_session.commit()

    await backup_groups.sync_scheduled_tasks(db_session)
    assert db_session.query(ScheduledTask).filter(
        ScheduledTask.task_type == TaskType.ZFS_BACKUP.value).count() == 0

    group.enabled = True
    db_session.query(BackupGroupDataset).filter(
        BackupGroupDataset.group_id == group.id).delete()
    db_session.commit()
    await backup_groups.sync_scheduled_tasks(db_session)
    assert db_session.query(ScheduledTask).filter(
        ScheduledTask.task_type == TaskType.ZFS_BACKUP.value).count() == 0


@pytest.mark.asyncio
async def test_sync_scheduled_tasks_removes_jobs_for_deleted_groups(db_session, tmp_path):
    """Disabling a group's cron removes the job again (reconciliation)."""
    from nazman.models.scheduler import ScheduledTask, TaskType

    group, sets, disks = _mk_group(db_session, tmp_path)
    group.full_cron = "0 2 * * 0"
    db_session.commit()

    await backup_groups.sync_scheduled_tasks(db_session)
    assert db_session.query(ScheduledTask).filter(
        ScheduledTask.task_type == TaskType.ZFS_BACKUP.value).count() == 1

    await backup_groups.delete_group(db_session, group.id)
    assert db_session.query(ScheduledTask).filter(
        ScheduledTask.task_type == TaskType.ZFS_BACKUP.value).count() == 0


@pytest.mark.asyncio
async def test_api_list_disk_streams_and_restore_file(client, db_session, tmp_path):

    bd = BackupDisk(
        disk_id=998, mount_point=str(tmp_path), fs_uuid="BBB",
    )
    db_session.add(bd)
    db_session.commit()

    with patch.object(ZfsBackupManager, "list_stream_files", new=AsyncMock(
        return_value=[{"path": str(tmp_path / "x.zfs.gz"), "dataset": "tank", "size_bytes": 100}]
    )):
        resp = client.get(f"/api/backup-zfs/disks/{bd.id}/streams")
    assert resp.status_code == 200, resp.text
    assert resp.json()[0]["dataset"] == "tank"

    with patch.object(ZfsBackupManager, "restore_dataset", new=AsyncMock(
        return_value={"dataset": "tank/media", "source": str(tmp_path / "x.zfs.gz"), "force": False}
    )):
        resp = client.post("/api/backup-zfs/restore-file",
                           json={"stream_file": str(tmp_path / "x.zfs.gz"), "target_dataset": "tank/media"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["dataset"] == "tank/media"

    resp = client.post("/api/backup-zfs/restore-file", json={"stream_file": "", "target_dataset": "tank/media"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_probe_device_offline_when_byid_path_missing(db_session):
    d = Disk(by_id="/dev/disk/by-id/ata-GONE", model="HDD", serial="GONE1",
             size_bytes=10**11, disk_type="hdd", is_os_disk=False)
    bd = BackupDisk(disk_id=d.id, mount_point="/tmp/zbx", fs_uuid="AAA")
    db_session.add(d)
    db_session.flush()
    bd.disk_id = d.id
    db_session.add(bd)
    db_session.commit()
    state = await zfs_backup_manager._probe_device(bd)
    assert state == "offline"


@pytest.mark.asyncio
async def test_probe_device_unmounted_when_present_and_uuid_matches(db_session):
    d = Disk(by_id="/dev/disk/by-id/ata-HERE", model="HDD", serial="HERE1",
             size_bytes=10**11, disk_type="hdd", is_os_disk=False)
    bd = BackupDisk(disk_id=d.id, mount_point="/tmp/zbx", fs_uuid="AAA")
    db_session.add(d)
    db_session.flush()
    bd.disk_id = d.id
    db_session.add(bd)
    db_session.commit()
    with patch("os.path.exists", return_value=True), \
         patch.object(zfs_backup_manager, "_fs_uuid", new=AsyncMock(return_value="AAA")):
        state = await zfs_backup_manager._probe_device(bd)
    assert state == "unmounted"


@pytest.mark.asyncio
async def test_probe_device_mismatch_when_uuid_differs(db_session):
    d = Disk(by_id="/dev/disk/by-id/ata-SWAP", model="HDD", serial="SWAP1",
             size_bytes=10**11, disk_type="hdd", is_os_disk=False)
    bd = BackupDisk(disk_id=d.id, mount_point="/tmp/zbx", fs_uuid="AAA")
    db_session.add(d)
    db_session.flush()
    bd.disk_id = d.id
    db_session.add(bd)
    db_session.commit()
    with patch("os.path.exists", return_value=True), \
         patch.object(zfs_backup_manager, "_fs_uuid", new=AsyncMock(return_value="BBB")):
        state = await zfs_backup_manager._probe_device(bd)
    assert state == "mismatch"


@pytest.mark.asyncio
async def test_mount_backup_disk_refuses_offline(db_session):
    d = Disk(by_id="/dev/disk/by-id/ata-GONE", model="HDD", serial="GONE2",
             size_bytes=10**11, disk_type="hdd", is_os_disk=False)
    bd = BackupDisk(disk_id=d.id, mount_point="/tmp/zbx", fs_uuid="AAA")
    db_session.add(d)
    db_session.flush()
    bd.disk_id = d.id
    db_session.add(bd)
    db_session.commit()
    with patch.object(zfs_backup_manager, "_probe_device", new=AsyncMock(return_value="offline")), \
         patch.object(zfs_backup_manager, "_wake_backup_disk", new=AsyncMock(return_value=False)):
        with pytest.raises(BackupError, match="power-cycle"):
            await zfs_backup_manager.mount_backup_disk(db_session, bd.id)


@pytest.mark.asyncio
async def test_mount_backup_disk_refuses_mismatch(db_session):
    d = Disk(by_id="/dev/disk/by-id/ata-SWAP", model="HDD", serial="SWAP2",
             size_bytes=10**11, disk_type="hdd", is_os_disk=False)
    bd = BackupDisk(disk_id=d.id, mount_point="/tmp/zbx", fs_uuid="AAA")
    db_session.add(d)
    db_session.flush()
    bd.disk_id = d.id
    db_session.add(bd)
    db_session.commit()
    with patch.object(zfs_backup_manager, "_probe_device", new=AsyncMock(return_value="mismatch")):
        with pytest.raises(BackupError, match="Filesystem changed"):
            await zfs_backup_manager.mount_backup_disk(db_session, bd.id)


@pytest.mark.asyncio
async def test_mount_backup_disk_mounts_present_unmounted_device(db_session, tmp_path, monkeypatch):
    d = Disk(by_id="/dev/disk/by-id/ata-HERE", model="HDD", serial="HERE3",
             size_bytes=10**11, disk_type="hdd", is_os_disk=False)
    bd = BackupDisk(disk_id=d.id, mount_point=str(tmp_path), fs_uuid="AAA")
    db_session.add(d)
    db_session.flush()
    bd.disk_id = d.id
    db_session.add(bd)
    db_session.commit()

    mounted = [False]
    cmds = []

    async def fake_run_command(cmd, **kwargs):
        cmds.append(cmd)
        if cmd[0] == "mount":
            mounted[0] = True
        return ("", "", 0)

    def fake_is_mount(self):
        return mounted[0] and str(self) == str(tmp_path)

    monkeypatch.setattr(Path, "is_mount", fake_is_mount)

    with patch("nazman.managers.zfs_backup_manager.run_command", side_effect=fake_run_command), \
         patch("os.path.exists", return_value=True), \
         patch.object(zfs_backup_manager, "_fs_uuid", new=AsyncMock(return_value="AAA")):
        rec = await zfs_backup_manager.mount_backup_disk(db_session, bd.id)

    assert rec["status"] == "mounted"
    assert ("mount", "/dev/disk/by-id/ata-HERE-part1", str(tmp_path)) in [tuple(c) for c in cmds]


@pytest.mark.asyncio
async def test_group_session_offline_disk_blocks_the_group(db_session, tmp_path):
    """A disk that will not mount blocks the group and says so, rather than
    marking datasets failed and rotating on."""
    group, sets, disks = _mk_group(db_session, tmp_path, disk_count=1)
    group.needs_disk = False
    db_session.commit()

    async def fake_run_zfs(*args, **kwargs):
        if args and args[0] == "list":
            return ("tank/media", "", 0)
        return ("", "", 0)

    with patch("nazman.managers.zfs_backup_manager.run_zfs", side_effect=fake_run_zfs), \
            patch("nazman.utils.zfs_query.run_zfs", side_effect=fake_run_zfs), \
         patch.object(ZfsBackupManager, "_wake_backup_disk", new=AsyncMock(return_value=False)), \
         patch.object(BackupGroupService, "_notify", new=AsyncMock()) as notify:
        session = await backup_groups.start_session(db_session, group.id, "full")
        result = await backup_groups.run_session(db_session, session.id)

    assert result["status"] == "needs_disk"
    assert "No usable disk" in db_session.get(BackupSession, session.id).error
    assert db_session.get(BackupGroup, group.id).needs_disk is True
    keys = [c.args[0] for c in notify.call_args_list]
    assert f"backup:group:{group.id}:needs_disk" in keys
    # Nothing was rotated, and no dataset was burned trying.
    assert db_session.get(BackupSet, sets[0].id).last_used_at is None
    assert db_session.query(BackupRun).filter(BackupRun.status == "success").count() == 0


@pytest.mark.asyncio
async def test_restore_dataset_mounts_owner_and_restores_idle(db_session, tmp_path):
    bd = BackupDisk(disk_id=999, mount_point=str(tmp_path), fs_uuid="BBB",
                    unmount_after_backup=True)
    db_session.add(bd)
    db_session.commit()

    fp = tmp_path / "data" / "tank" / "full-20260901-000000.zfs.gz"
    os.makedirs(fp.parent, exist_ok=True)
    fp.write_bytes(gzip.compress(b"STREAMSIM"))

    async def fake_pipeline(stages, **kwargs):
        assert stages[0][:2] == ["gunzip", "-c"]
        assert stages[1][:2] == ["zfs", "receive"]
        return ("", "", 0)

    with patch("nazman.managers.zfs_backup_manager.run_pipeline", side_effect=fake_pipeline), \
         patch.object(ZfsBackupManager, "mount_backup_disk", new=AsyncMock()) as mnt, \
         patch.object(zfs_backup_manager, "_restore_idle_state", new=AsyncMock()) as idle:
        res = await zfs_backup_manager.restore_dataset(db_session, str(fp), "tank/media")

    assert res["dataset"] == "tank/media"
    assert mnt.await_count == 1
    assert idle.await_count == 1


@pytest.mark.asyncio
async def test_restore_dataset_replays_raw_compact_stream(db_session, tmp_path):
    bd = BackupDisk(disk_id=998, mount_point=str(tmp_path), fs_uuid="DDD",
                    unmount_after_backup=True)
    db_session.add(bd)
    db_session.commit()

    fp = tmp_path / "data" / "tank" / "full-20260901-010000.zfs.gz"
    # Newer streams are zfs send -c compact streams: raw zfs data, not gzip.
    os.makedirs(fp.parent, exist_ok=True)
    fp.write_bytes(b"not-gzip zfs send stream data")

    async def fake_pipeline(stages, **kwargs):
        assert stages[0][:2] == ["cat", str(fp)]
        assert stages[1][:2] == ["zfs", "receive"]
        return ("", "", 0)

    with patch("nazman.managers.zfs_backup_manager.run_pipeline", side_effect=fake_pipeline), \
         patch.object(ZfsBackupManager, "mount_backup_disk", new=AsyncMock()) as mnt, \
         patch.object(zfs_backup_manager, "_restore_idle_state", new=AsyncMock()) as idle:
        res = await zfs_backup_manager.restore_dataset(db_session, str(fp), "tank/media")

    assert res["dataset"] == "tank/media"
    assert mnt.await_count == 1
    assert idle.await_count == 1


@pytest.mark.asyncio
async def test_restore_dataset_rejects_path_outside_backup_disks(db_session, tmp_path):
    bd = BackupDisk(disk_id=999, mount_point=str(tmp_path), fs_uuid="CCC")
    db_session.add(bd)
    db_session.commit()

    outside = tmp_path / "elsewhere" / "full-20260901-000000.zfs.gz"
    os.makedirs(outside.parent, exist_ok=True)
    outside.write_text("X")

    with patch("nazman.managers.zfs_backup_manager.run_pipeline", new=AsyncMock()):
        with pytest.raises(BackupError, match="under a registered backup disk"):
            await zfs_backup_manager.restore_dataset(db_session, str(outside), "tank/media")


@pytest.mark.asyncio
async def test_api_patch_backup_disk_toggle(client, db_session):
    bd = BackupDisk(disk_id=999, mount_point="/tmp/mnt", fs_uuid="AAA")
    db_session.add(bd)
    db_session.commit()

    resp = client.patch(f"/api/backup-zfs/disks/{bd.id}", json={"unmount_after_backup": False})
    assert resp.status_code == 200, resp.text
    assert resp.json()["unmount_after_backup"] is False

    resp = client.patch(f"/api/backup-zfs/disks/{bd.id}", json={"unmount_after_backup": True})
    assert resp.status_code == 200
    assert resp.json()["unmount_after_backup"] is True


@pytest.mark.asyncio
async def test_api_mount_offline_returns_400(client, db_session):
    bd = BackupDisk(disk_id=999, mount_point="/tmp/zbx", fs_uuid="AAA")
    db_session.add(bd)
    db_session.commit()

    with patch.object(ZfsBackupManager, "mount_backup_disk",
                      new=AsyncMock(side_effect=BackupError("Backup disk is offline"))):
        resp = client.post(f"/api/backup-zfs/disks/{bd.id}/mount")
    assert resp.status_code == 400
    assert "offline" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_sync_scheduled_tasks_task_names_are_group_scoped(db_session, tmp_path):
    """Job names identify the group, so renaming a group re-keys its jobs."""
    from nazman.models.scheduler import ScheduledTask, TaskType

    group, sets, disks = _mk_group(db_session, tmp_path)
    group.full_cron = "0 2 * * 0"
    group.incremental_cron = "0 3 * * *"
    db_session.commit()

    await backup_groups.sync_scheduled_tasks(db_session)

    names = {t.name for t in db_session.query(ScheduledTask).filter(
        ScheduledTask.task_type == TaskType.ZFS_BACKUP.value).all()}
    assert names == {
        f"backup-group-{group.id}-full", f"backup-group-{group.id}-incremental",
    }
    task = db_session.query(ScheduledTask).filter(
        ScheduledTask.name == f"backup-group-{group.id}-full").first()
    assert task.config == {"group_id": group.id, "type": "full"}


@pytest.mark.asyncio
async def test_sync_scheduled_tasks_cleans_orphaned_backup_jobs(db_session, tmp_path):
    """A ZFS_BACKUP job that no group claims any more is removed."""
    from nazman.models.scheduler import ScheduledTask, TaskType

    # A job left over from the old per-dataset schedule model.
    await scheduler_manager.create_task(
        db_session, name="zfs-full-tank/media-old", task_type=TaskType.ZFS_BACKUP,
        target="tank/media", schedule="0 2 * * 0",
        config={"dataset_name": "tank/media", "type": "full"},
    )
    db_session.commit()

    await backup_groups.sync_scheduled_tasks(db_session)

    remaining = db_session.query(ScheduledTask).filter(
        ScheduledTask.task_type == TaskType.ZFS_BACKUP.value).all()
    assert remaining == []


@pytest.mark.asyncio
async def test_group_attach_and_detach_disks_via_api(client, db_session, tmp_path):
    """A set holds an ordered chain of volumes, appended one at a time."""
    group, sets, disks = _mk_group(db_session, tmp_path, positions=1)
    spare = BackupDisk(disk_id=950, mount_point=str(tmp_path / "spare"), fs_uuid="SP0")
    db_session.add(spare)
    db_session.commit()

    with patch.object(type(backup_groups), "describe_set", new=AsyncMock(
            side_effect=lambda db, sid: {"id": sid, "disks": []})):
        r = client.post(f"/api/backup-groups/{group.id}/sets/{sets[0].id}/disks/{spare.id}")
    assert r.status_code == 201, r.text
    # The API runs on its own session, so drop this one's cached copies.
    db_session.expire_all()
    assert db_session.get(BackupDisk, spare.id).backup_set_id == sets[0].id
    assert db_session.get(BackupSet, sets[0].id).disks[-1].id == spare.id


@pytest.mark.asyncio
async def test_attach_disk_moves_it_and_realigns_the_old_set(client, db_session, tmp_path):
    """A volume holds one chain, so moving it between sets is allowed - but the
    set it leaves falls back to whatever disks remain."""
    group, sets, disks = _mk_group(db_session, tmp_path, positions=2)
    moved = disks[0]
    old_set, new_set = sets[0], sets[1]
    old_set_idle = BackupDisk(
        disk_id=960, mount_point=str(tmp_path / "spare0"), fs_uuid="SP0",
    )
    db_session.add(old_set_idle)
    db_session.commit()
    old_set_idle.backup_set_id = old_set.id
    db_session.commit()

    with patch.object(type(backup_groups), "describe_set", new=AsyncMock(
            side_effect=lambda db, sid: {"id": sid, "disks": []})):
        r = client.post(f"/api/backup-groups/{group.id}/sets/{new_set.id}/disks/{moved.id}")
    assert r.status_code == 201, r.text
    db_session.expire_all()
    assert db_session.get(BackupDisk, moved.id).backup_set_id == new_set.id
    # The old set now points at the volume it kept.
    assert db_session.get(BackupSet, old_set.id).active_disk_id == old_set_idle.id


@pytest.mark.asyncio
async def test_delete_set_keeps_disks_but_drops_its_runs(client, db_session, tmp_path):
    """Runs only mean something relative to a set, so they go with it; the
    volumes survive unassigned so the media is still described."""
    group, sets, disks = _mk_group(db_session, tmp_path, positions=2)
    db_session.add(BackupRun(
        group_id=group.id, backup_set_id=sets[0].id, dataset_name="tank/media",
        backup_disk_id=disks[0].id, backup_type="full", status="success",
    ))
    db_session.commit()

    gone_set_id, kept_disk_id = sets[0].id, disks[0].id
    r = client.delete(f"/api/backup-groups/{group.id}/sets/{gone_set_id}")
    assert r.status_code == 200, r.text
    db_session.expire_all()
    assert db_session.query(BackupSet).filter(BackupSet.id == gone_set_id).count() == 0
    assert db_session.query(BackupDisk).filter(BackupDisk.id == kept_disk_id).count() == 1
    assert db_session.get(BackupDisk, kept_disk_id).backup_set_id is None
    assert db_session.query(BackupRun).filter(BackupRun.backup_set_id == gone_set_id).count() == 0
    # The group falls back to its first remaining set.
    assert db_session.get(BackupGroup, group.id).active_set_id == sets[1].id


@pytest.mark.asyncio
async def test_delete_group_keeps_its_disks(client, db_session, tmp_path):
    group, sets, disks = _mk_group(db_session, tmp_path, positions=1)
    gone_group_id, gone_set_id, kept_disk_id = group.id, sets[0].id, disks[0].id
    r = client.delete(f"/api/backup-groups/{gone_group_id}")
    assert r.status_code == 200, r.text
    db_session.expire_all()
    assert db_session.query(BackupGroup).filter(BackupGroup.id == gone_group_id).count() == 0
    assert db_session.query(BackupSet).filter(BackupSet.id == gone_set_id).count() == 0
    assert db_session.query(BackupDisk).filter(BackupDisk.id == kept_disk_id).count() == 1
    assert db_session.get(BackupDisk, kept_disk_id).backup_set_id is None


@pytest.mark.asyncio
async def test_group_view_shapes_sets_datasets_and_sessions(client, db_session, tmp_path):
    """The group view is what the backup page renders from."""
    group, sets, disks = _mk_group(
        db_session, tmp_path, name="Weekly", datasets=("tank/media", "tank/docs"),
        disks_per_set=2, positions=1,
    )
    with patch.object(ZfsBackupManager, "serialize_now", new=AsyncMock(
            return_value={"total_bytes": 1000, "free_bytes": 400, "status": "ready"})):
        resp = client.get(f"/api/backup-groups/{group.id}")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["name"] == "Weekly"
    assert data["datasets"] == ["tank/media", "tank/docs"]
    assert data["set_count"] == 1
    assert len(data["sets"][0]["disks"]) == 2
    assert data["sets"][0]["disks"][0]["is_active"] is True
    assert data["ready"] is True
    assert data["blocking_reason"] is None


@pytest.mark.asyncio
async def test_group_view_reports_blocking_reason(client, db_session, tmp_path):
    """A group with no datasets says so instead of failing silently."""
    group = BackupGroup(name="Bare")
    db_session.add(group)
    db_session.commit()
    resp = client.get(f"/api/backup-groups/{group.id}")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ready"] is False
    assert "dataset" in data["blocking_reason"]


@pytest.mark.asyncio
async def test_wake_backup_disk_toggles_bridge_authorized(db_session, tmp_path):
    d = Disk(by_id="/dev/disk/by-id/ata-AWAKE", model="HDD", serial="AWAKE1",
             size_bytes=10**11, disk_type="hdd", is_os_disk=False)
    bd = BackupDisk(disk_id=d.id, mount_point="/tmp/w", fs_uuid="AAA")
    db_session.add(d)
    db_session.flush()
    bd.disk_id = d.id
    db_session.add(bd)
    db_session.commit()

    bridge = tmp_path / "bridge"
    bridge.mkdir()
    authorized = bridge / "authorized"
    authorized.write_text("0")

    real_exists = os.path.exists
    dev_path = zfs_backup_manager._dev_path(bd)

    def fake_exists(p):
        if str(p) == dev_path:
            return authorized.read_text() == "1"
        return real_exists(p)

    with patch("nazman.managers.zfs_backup_manager.os.path.exists", side_effect=fake_exists), \
         patch.object(zfs_backup_manager, "_usb_storage_bridges", return_value=[bridge]), \
         patch("nazman.managers.zfs_backup_manager.asyncio.sleep", new=AsyncMock()):
        ok = await zfs_backup_manager._wake_backup_disk(bd)

    assert ok is True
    assert authorized.read_text() == "1"


@pytest.mark.asyncio
async def test_wake_backup_disk_requires_power_cycle_when_bridge_gone(db_session):
    d = Disk(by_id="/dev/disk/by-id/ata-BRIDGELESS", model="HDD", serial="BRIDGE1",
             size_bytes=10**11, disk_type="hdd", is_os_disk=False)
    bd = BackupDisk(disk_id=d.id, mount_point="/tmp/w", fs_uuid="AAA")
    db_session.add(d)
    db_session.flush()
    bd.disk_id = d.id
    db_session.add(bd)
    db_session.commit()

    with patch("os.path.exists", return_value=False), \
         patch.object(zfs_backup_manager, "_usb_storage_bridges", return_value=[]):
        ok = await zfs_backup_manager._wake_backup_disk(bd)

    assert ok is False


@pytest.mark.asyncio
async def test_wake_backup_disk_public_raises_on_failure(db_session):
    d = Disk(by_id="/dev/disk/by-id/ata-WGONE", model="HDD", serial="WGONE1",
             size_bytes=10**11, disk_type="hdd", is_os_disk=False)
    bd = BackupDisk(disk_id=d.id, mount_point="/tmp/w", fs_uuid="AAA")
    db_session.add(d)
    db_session.flush()
    bd.disk_id = d.id
    db_session.add(bd)
    db_session.commit()

    with patch.object(zfs_backup_manager, "_probe_device", new=AsyncMock(return_value="offline")), \
         patch.object(zfs_backup_manager, "_wake_backup_disk", new=AsyncMock(return_value=False)):
        with pytest.raises(BackupError, match="power-cycle"):
            await zfs_backup_manager.wake_backup_disk(db_session, bd.id)


@pytest.mark.asyncio
async def test_mount_backup_disk_wakes_offline_disk_and_mounts(db_session, tmp_path, monkeypatch):
    d = Disk(by_id="/dev/disk/by-id/ata-HERE", model="HDD", serial="HERE4",
             size_bytes=10**11, disk_type="hdd", is_os_disk=False)
    bd = BackupDisk(disk_id=d.id, mount_point=str(tmp_path), fs_uuid="AAA")
    db_session.add(d)
    db_session.flush()
    bd.disk_id = d.id
    db_session.add(bd)
    db_session.commit()

    probes = ["offline", "unmounted", "mounted"]

    async def fake_probe(rec):
        return probes.pop(0)

    mounted = [False]
    cmds = []

    async def fake_run_command(cmd, **kwargs):
        cmds.append(cmd)
        if cmd[0] == "mount":
            mounted[0] = True
        return ("", "", 0)

    def fake_is_mount(self):
        return mounted[0] and str(self) == str(tmp_path)

    monkeypatch.setattr(Path, "is_mount", fake_is_mount)

    wake_mock = AsyncMock(return_value=True)

    with patch.object(zfs_backup_manager, "_probe_device", new=AsyncMock(side_effect=fake_probe)), \
         patch.object(zfs_backup_manager, "_wake_backup_disk", new=wake_mock), \
         patch("nazman.managers.zfs_backup_manager.run_command", side_effect=fake_run_command), \
         patch("os.path.exists", return_value=True), \
         patch.object(zfs_backup_manager, "_fs_uuid", new=AsyncMock(return_value="AAA")):
        rec = await zfs_backup_manager.mount_backup_disk(db_session, bd.id)
        wake_mock.assert_awaited_once()
    assert rec["status"] == "mounted"
    assert ("mount", "/dev/disk/by-id/ata-HERE-part1", str(tmp_path)) in [tuple(c) for c in cmds]


@pytest.mark.asyncio
async def test_mount_backup_disk_still_offline_after_wake_raises(db_session, tmp_path, monkeypatch):
    d = Disk(by_id="/dev/disk/by-id/ata-NOPE", model="HDD", serial="NOPE1",
             size_bytes=10**11, disk_type="hdd", is_os_disk=False)
    bd = BackupDisk(disk_id=d.id, mount_point=str(tmp_path), fs_uuid="AAA")
    db_session.add(d)
    db_session.flush()
    bd.disk_id = d.id
    db_session.add(bd)
    db_session.commit()

    probes = ["offline", "offline"]

    async def fake_probe(rec):
        return probes.pop(0)

    monkeypatch.setattr(Path, "is_mount", lambda self: False)

    with patch.object(zfs_backup_manager, "_probe_device", new=AsyncMock(side_effect=fake_probe)), \
         patch.object(zfs_backup_manager, "_wake_backup_disk", new=AsyncMock(return_value=True)):
        with pytest.raises(BackupError, match="power-cycle"):
            await zfs_backup_manager.mount_backup_disk(db_session, bd.id)


@pytest.mark.asyncio
async def test_api_wake_backup_disk(client, db_session):
    bd = BackupDisk(disk_id=1, mount_point="/tmp/w", fs_uuid="AAA")
    db_session.add(bd)
    db_session.commit()

    bd_dict = {"id": bd.id, "disk_id": 1, "slot_uuid": None, "partition_number": 1,
               "device_path": None, "label": None, "fs_type": "ext4",
               "mount_point": "/tmp/w", "fs_uuid": "AAA", "unmount_after_backup": True,
               "status": "offline", "total_bytes": 0, "free_bytes": 0}

    with patch.object(ZfsBackupManager, "wake_backup_disk", new=AsyncMock(return_value=bd_dict)):
        resp = client.post(f"/api/backup-zfs/disks/{bd.id}/wake")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "offline"

    with patch.object(ZfsBackupManager, "wake_backup_disk",
                      new=AsyncMock(side_effect=BackupError("power-cycle their enclosure"))):
        resp = client.post(f"/api/backup-zfs/disks/{bd.id}/wake")
    assert resp.status_code == 400
    assert "power-cycle" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_dev_path_derived_from_by_id_and_partition_number(db_session):
    d1 = Disk(by_id="/dev/disk/by-id/ata-DERIVED", model="HDD", serial="DERIVED1",
              size_bytes=10**11, disk_type="hdd", is_os_disk=False)
    d2 = Disk(by_id="/dev/disk/by-id/ata-DERIVED2", model="HDD", serial="DERIVED2",
              size_bytes=10**11, disk_type="hdd", is_os_disk=False)
    db_session.add_all([d1, d2])
    db_session.flush()
    whole = BackupDisk(mount_point="/tmp/p", fs_uuid="W", partition_number=1)
    part2 = BackupDisk(mount_point="/tmp/p2", fs_uuid="P", partition_number=2)
    whole.disk_id = d1.id
    part2.disk_id = d2.id
    db_session.add_all([whole, part2])
    db_session.commit()
    assert zfs_backup_manager._dev_path(whole) == "/dev/disk/by-id/ata-DERIVED-part1"
    assert zfs_backup_manager._dev_path(part2) == "/dev/disk/by-id/ata-DERIVED2-part2"
    none_disk = BackupDisk(disk_id=12345, mount_point="/tmp/none", fs_uuid="N")
    db_session.add(none_disk)
    db_session.commit()
    assert zfs_backup_manager._dev_path(none_disk) is None
    assert zfs_backup_manager._partition_number("/dev/disk/by-id/ata-X-part1") == 1
    assert zfs_backup_manager._partition_number("/dev/disk/by-id/ata-X-part7") == 7
    assert zfs_backup_manager._partition_number("/dev/sdb") == 1


def test_backup_disk_requires_unique_disk_and_fs_uuid(db_session):
    from sqlalchemy.exc import IntegrityError
    with pytest.raises(IntegrityError):
        db_session.add_all([
            BackupDisk(disk_id=1, mount_point="/tmp/a", fs_uuid="UU-1"),
            BackupDisk(disk_id=1, mount_point="/tmp/b", fs_uuid="UU-2"),
        ])
        db_session.commit()
    db_session.rollback()
    with pytest.raises(IntegrityError):
        db_session.add_all([
            BackupDisk(disk_id=2, mount_point="/tmp/a", fs_uuid="UU-3"),
            BackupDisk(disk_id=3, mount_point="/tmp/b", fs_uuid="UU-3"),
        ])
        db_session.commit()


@pytest.mark.asyncio
async def test_deregister_backup_disk_removes_runs_and_clears_set_pointers(db_session, tmp_path):
    """Undeclaring a volume drops its runs and every pointer that aimed at it."""
    group, sets, disks = _mk_group(db_session, tmp_path, disk_count=1)
    group.needs_disk = True
    db_session.add(BackupRun(
        group_id=group.id, backup_set_id=sets[0].id, dataset_name="tank/media",
        backup_disk_id=disks[0].id, backup_type="full", status="success",
    ))
    db_session.commit()
    disk_id = disks[0].id

    with patch("nazman.managers.zfs_backup_manager.run_command",
               return_value=("", "", 0)):
        await zfs_backup_manager.deregister_backup_disk(db_session, disk_id)

    assert db_session.query(BackupDisk).count() == 0
    assert db_session.query(BackupRun).count() == 0
    # The set no longer points at a disk that is gone...
    assert db_session.get(BackupSet, sets[0].id).active_disk_id is None
    # ...and the group is flagged, because that set now has no media at all.
    assert db_session.get(BackupGroup, group.id).needs_disk is True


@pytest.mark.asyncio
async def test_group_attach_disk_via_service(db_session, tmp_path):
    """Filing a declared volume into a set makes it the set's active disk."""
    group, sets, disks = _mk_group(db_session, tmp_path, positions=1)
    spare = BackupDisk(
        disk_id=970, mount_point=str(tmp_path / "spare"), fs_uuid="SP9",
    )
    db_session.add(spare)
    db_session.commit()
    spare_id = spare.id

    with patch.object(type(backup_groups), "describe_set", new=AsyncMock(
            side_effect=lambda db, sid: {"id": sid, "disks": []})):
        await backup_groups.attach_disk(db_session, sets[0].id, spare_id)

    assert db_session.get(BackupDisk, spare_id).backup_set_id == sets[0].id
    assert [
        d.id for d in db_session.query(BackupDisk)
        .filter(BackupDisk.backup_set_id == sets[0].id)
        .order_by(BackupDisk.id).all()
    ] == [disks[0].id, spare_id]


@pytest.mark.asyncio
async def test_group_detach_disk_reports_broken_chain(client, db_session, tmp_path):
    """Removing a volume says which datasets can no longer be restored, rather
    than letting the chain break silently."""
    group, sets, disks = _mk_group(
        db_session, tmp_path, datasets=("tank/media", "tank/docs"),
        disks_per_set=2, positions=1,
    )
    # media has a full on disk 0 and an incremental on disk 1 based on it.
    db_session.add_all([
        BackupRun(group_id=group.id, backup_set_id=sets[0].id, dataset_name="tank/media",
                  backup_disk_id=disks[0].id, backup_type="full", status="success",
                  snapshot="tank/media@backup-1"),
        BackupRun(group_id=group.id, backup_set_id=sets[0].id, dataset_name="tank/media",
                  backup_disk_id=disks[1].id, backup_type="incremental", status="success",
                  base_snapshot="tank/media@backup-1", snapshot="tank/media@backup-2"),
        BackupRun(group_id=group.id, backup_set_id=sets[0].id, dataset_name="tank/docs",
                  backup_disk_id=disks[0].id, backup_type="full", status="success",
                  snapshot="tank/docs@backup-1"),
    ])
    db_session.commit()
    first_disk_id = disks[0].id

    with patch.object(type(backup_groups), "_deregister_disk", new=AsyncMock()):
        r = client.delete(
            f"/api/backup-groups/{group.id}/sets/{sets[0].id}/disks/{first_disk_id}"
        )
    assert r.status_code == 200, r.text
    data = r.json()
    assert "tank/media" in data["broken_datasets"]
    assert "tank/docs" in data["broken_datasets"]


@pytest.mark.asyncio
async def test_group_incremental_uses_prior_set_anchor(db_session, tmp_path, monkeypatch):
    """Incremental must send -i <prior-anchor> <new-snapshot>, never -i <new> <new>.

    Regresses the ordering bug where the anchor was found AFTER the new
    snapshot was created, so the anchor resolved to the snapshot itself and
    `zfs send -R -i <snap> <snap>` failed ("incremental source ... is not
    earlier than it").
    """
    group, sets, disks = _mk_group(db_session, tmp_path, disk_count=1)
    # The set has been used before, so this run is a real incremental.
    db_session.get(BackupSet, sets[0].id).last_used_at = datetime.now(timezone.utc)
    db_session.commit()

    anchor = "tank/media@backup-20260901-120000"
    snaps = {anchor}
    sends = []

    # The anchor resolves from the set's own history, not from a live snapshot
    # listing, so the previous run has to exist as a row.
    db_session.add(BackupRun(
        group_id=group.id, backup_set_id=sets[0].id, dataset_name="tank/media",
        backup_disk_id=disks[0].id, backup_type="full", status="success",
        snapshot=anchor,
    ))
    db_session.commit()

    async def fake_run_zfs(*args, **kwargs):
        cmd = list(args)
        if cmd and cmd[0] == "snapshot":
            snaps.add(cmd[2])
            return ("", "", 0)
        if cmd and cmd[0] == "destroy":
            return ("", "", 0)
        if cmd and cmd[0] == "get":
            return ("123456", "", 0)
        if cmd and cmd[0] == "diff":
            # Report changes so the run is not skipped.
            return ("M\tsome/file\n", "", 0)
        if cmd and cmd[0] == "list":
            if "-t" in cmd and "snapshot" in cmd:
                return ("\n".join(sorted(snaps)), "", 0)
            return ("tank/media", "", 0)
        return ("", "", 0)

    async def fake_pipeline(stages, stdout_path=None, **kwargs):
        sends.append(list(stages[0]))
        return _fake_write(stages, stdout_path, content="INCRSTREAM")

    monkeypatch.setattr(Path, "is_mount", lambda self: True)

    with patch("nazman.managers.zfs_backup_manager.run_zfs", side_effect=fake_run_zfs), \
         patch("nazman.utils.zfs_query.run_zfs", side_effect=fake_run_zfs), \
         patch("nazman.managers.zfs_backup_manager.run_pipeline", side_effect=fake_pipeline), \
         patch("nazman.managers.zfs_backup_manager.run_command", new=AsyncMock()), \
         patch("os.path.exists", return_value=True), \
         patch.object(ZfsBackupManager, "_fs_uuid", new=AsyncMock(return_value="AAA")), \
         patch.object(ZfsBackupManager, "_dataset_exists", new=AsyncMock(return_value=True)), \
         patch.object(ZfsBackupManager, "mount_backup_disk", new=AsyncMock()), \
         patch.object(ZfsBackupManager, "_record_manifest", new=AsyncMock()):
        session = await backup_groups.start_session(db_session, group.id, "incremental")
        await backup_groups.run_session(db_session, session.id)

    run = db_session.query(BackupRun).order_by(BackupRun.id.desc()).first()
    assert run.status == "success"
    assert run.backup_type == "incremental"
    assert run.base_snapshot == anchor
    assert len(sends) == 1
    send = sends[0]
    assert send[:4] == ["zfs", "send", "-c", "-R"]
    # -i must reference the pre-existing anchor, not the freshly created one.
    base_arg = send[send.index("-i") + 1]
    assert base_arg == anchor
    assert send[-1] != anchor


@pytest.mark.asyncio
async def test_group_copies_writes_every_dataset_to_several_sets_and_rotates_by_copies(
    db_session, tmp_path, monkeypatch,
):
    group, sets, disks = _mk_group(db_session, tmp_path, disk_count=3, positions=3)
    group.copies = 2
    db_session.commit()
    monkeypatch.setattr(Path, "is_mount", lambda self: True)

    fake_zfs, fake_pipe = _stream_fakes()
    with patch("nazman.managers.zfs_backup_manager.run_zfs", side_effect=fake_zfs), \
            patch("nazman.utils.zfs_query.run_zfs", side_effect=fake_zfs), \
         patch("nazman.managers.zfs_backup_manager.run_pipeline", side_effect=fake_pipe), \
         patch("nazman.managers.zfs_backup_manager.run_command", new=AsyncMock()), \
         patch("os.path.exists", return_value=True), \
         patch.object(ZfsBackupManager, "_fs_uuid", new=AsyncMock(return_value="AAA")), \
         patch.object(ZfsBackupManager, "_dataset_exists", new=AsyncMock(return_value=True)), \
         patch.object(ZfsBackupManager, "_record_manifest", new=AsyncMock()):
        session = await backup_groups.start_session(db_session, group.id, "full")
        result = await backup_groups.run_session(db_session, session.id)

    assert result["status"] == "success"
    runs = db_session.query(BackupRun).order_by(BackupRun.id).all()
    assert sorted({r.backup_set_id for r in runs}) == sorted([sets[0].id, sets[1].id])
    # Each targeted set keeps its own disk, so the copies do not share media.
    assert {r.backup_disk_id for r in runs} == {disks[0].id, disks[1].id}
    assert db_session.get(BackupGroup, group.id).active_set_id == sets[2].id
    assert db_session.get(BackupSet, sets[0].id).last_used_at is not None
    assert db_session.get(BackupSet, sets[1].id).last_used_at is not None
    assert db_session.get(BackupSet, sets[2].id).last_used_at is None


@pytest.mark.asyncio
async def test_group_copies_are_capped_at_set_count(db_session, tmp_path, monkeypatch):
    group, sets, disks = _mk_group(db_session, tmp_path, disk_count=2, positions=2)
    group.copies = 5
    db_session.commit()
    monkeypatch.setattr(Path, "is_mount", lambda self: True)

    fake_zfs, fake_pipe = _stream_fakes()
    with patch("nazman.managers.zfs_backup_manager.run_zfs", side_effect=fake_zfs), \
            patch("nazman.utils.zfs_query.run_zfs", side_effect=fake_zfs), \
         patch("nazman.managers.zfs_backup_manager.run_pipeline", side_effect=fake_pipe), \
         patch("nazman.managers.zfs_backup_manager.run_command", new=AsyncMock()), \
         patch("os.path.exists", return_value=True), \
         patch.object(ZfsBackupManager, "_fs_uuid", new=AsyncMock(return_value="AAA")), \
         patch.object(ZfsBackupManager, "_dataset_exists", new=AsyncMock(return_value=True)), \
         patch.object(ZfsBackupManager, "_record_manifest", new=AsyncMock()):
        session = await backup_groups.start_session(db_session, group.id, "full")
        result = await backup_groups.run_session(db_session, session.id)

    assert result["status"] == "success"
    runs = db_session.query(BackupRun).order_by(BackupRun.id).all()
    assert {r.backup_set_id for r in runs} == {sets[0].id, sets[1].id}
    # copies == len(sets): every set is written, so nothing rotates.
    assert db_session.get(BackupGroup, group.id).active_set_id == sets[0].id


@pytest.mark.asyncio
async def test_group_recycles_full_disk_when_enabled(db_session, tmp_path, monkeypatch):
    group, sets, disks = _mk_group(db_session, tmp_path, disk_count=1)
    group.recycle_full_disks = True
    db_session.commit()
    monkeypatch.setattr(Path, "is_mount", lambda self: True)

    class FakeStatvfs:
        f_frsize = 1
        tries = 0

        def __init__(self):
            self.f_bavail = 0

        def __call__(self, path):
            FakeStatvfs.tries += 1
            if FakeStatvfs.tries == 1:
                self.f_bavail = 0  # the full disk blocks the first attempt...
            else:
                self.f_bavail = 10 << 30  # ...but room is available once recycled
            return self

    async def fake_run_zfs(*args, **kwargs):
        cmd = list(args)
        if cmd and cmd[0] == "list":
            if "-t" in cmd and "snapshot" in cmd:
                return ("", "", 0)
            return ("tank/media", "", 0)
        return ("", "", 0)

    async def fake_pipeline(stages, stdout_path=None, **kwargs):
        return _fake_write(stages, stdout_path)

    recycled = []
    async def fake_recycle(self, db, backup_disk_id):
        recycled.append(backup_disk_id)
        return {"id": backup_disk_id}

    with patch("nazman.managers.zfs_backup_manager.run_zfs", side_effect=fake_run_zfs), \
            patch("nazman.utils.zfs_query.run_zfs", side_effect=fake_run_zfs), \
         patch("nazman.managers.zfs_backup_manager.run_pipeline", side_effect=fake_pipeline), \
         patch("nazman.managers.zfs_backup_manager.run_command", new=AsyncMock()), \
         patch("nazman.managers.zfs_backup_manager.os.statvfs", new=FakeStatvfs()), \
         patch("os.path.exists", return_value=True), \
         patch.object(ZfsBackupManager, "_fs_uuid", new=AsyncMock(return_value="AAA")), \
         patch.object(ZfsBackupManager, "_dataset_exists", new=AsyncMock(return_value=True)), \
         patch.object(ZfsBackupManager, "mount_backup_disk", new=AsyncMock()), \
         patch.object(ZfsBackupManager, "estimate_needed", new=AsyncMock(return_value=1 << 20)), \
         patch.object(ZfsBackupManager, "recycle_disk", new=fake_recycle), \
         patch.object(ZfsBackupManager, "_record_manifest", new=AsyncMock()):
        session = await backup_groups.start_session(db_session, group.id, "full")
        result = await backup_groups.run_session(db_session, session.id)

    assert result["status"] == "success"
    assert recycled == [disks[0].id]
    assert db_session.get(BackupGroup, group.id).needs_disk is False
    session_row = db_session.get(BackupSession, session.id)
    assert "recycled" in (session_row.notes or "")
    run = db_session.query(BackupRun).order_by(BackupRun.id.desc()).first()
    assert run.status == "success"
    assert run.backup_disk_id == disks[0].id


@pytest.mark.asyncio
async def test_group_will_not_recycle_a_disk_written_this_session(
    db_session, tmp_path, monkeypatch,
):
    """Recycling must not destroy data written moments ago by the same session."""
    group, sets, disks = _mk_group(
        db_session, tmp_path, disk_count=1, datasets=("tank/media", "tank/docs"),
    )
    group.recycle_full_disks = True
    db_session.commit()
    monkeypatch.setattr(Path, "is_mount", lambda self: True)

    async def fake_run_zfs(*args, **kwargs):
        cmd = list(args)
        if cmd and cmd[0] == "list":
            if "-t" in cmd and "snapshot" in cmd:
                return ("", "", 0)
            return ("tank/media", "", 0)
        return ("", "", 0)

    async def fake_pipeline(stages, stdout_path=None, **kwargs):
        return _fake_write(stages, stdout_path)

    real = ZfsBackupManager.backup_dataset
    recycled = []

    async def fake_recycle(self, db, backup_disk_id):
        recycled.append(backup_disk_id)
        return {"id": backup_disk_id}

    async def spy(self, db, rec, run):
        if run.dataset_name == "tank/docs":
            return {"status": "needs_space", "needed_bytes": 1 << 20}
        return await real(self, db, rec, run)

    with patch("nazman.managers.zfs_backup_manager.run_zfs", side_effect=fake_run_zfs), \
            patch("nazman.utils.zfs_query.run_zfs", side_effect=fake_run_zfs), \
         patch("nazman.managers.zfs_backup_manager.run_pipeline", side_effect=fake_pipeline), \
         patch("nazman.managers.zfs_backup_manager.run_command", new=AsyncMock()), \
         patch("os.path.exists", return_value=True), \
         patch.object(ZfsBackupManager, "_fs_uuid", new=AsyncMock(return_value="AAA")), \
         patch.object(ZfsBackupManager, "_dataset_exists", new=AsyncMock(return_value=True)), \
         patch.object(ZfsBackupManager, "mount_backup_disk", new=AsyncMock()), \
         patch.object(ZfsBackupManager, "estimate_needed", new=AsyncMock(return_value=1 << 20)), \
         patch.object(ZfsBackupManager, "recycle_disk", new=fake_recycle), \
         patch.object(ZfsBackupManager, "backup_dataset", new=spy), \
         patch.object(BackupGroupService, "_notify", new=AsyncMock()):
        session = await backup_groups.start_session(db_session, group.id, "full")
        result = await backup_groups.run_session(db_session, session.id)

    assert result["status"] == "needs_disk"
    assert recycled == []
    session_row = db_session.get(BackupSession, session.id)
    assert not (session_row.notes or "").startswith("Disk '")
    # The dataset that did fit is not thrown away by a mid-session wipe.
    wrote = db_session.query(BackupRun).filter_by(dataset_name="tank/media").first()
    assert wrote is not None and wrote.status == "success"
    assert wrote.backup_disk_id == disks[0].id


@pytest.mark.asyncio
async def test_recycle_disk_wipes_reformats_and_clears_runs(db_session, tmp_path, monkeypatch):
    group, sets, disks = _mk_group(db_session, tmp_path, disk_count=1)
    monkeypatch.setattr(zfs_backup_manager.settings, "backup_mount_base", str(tmp_path))
    run = BackupRun(
        session_id=1, group_id=group.id, backup_set_id=sets[0].id,
        dataset_name="tank/media", backup_disk_id=disks[0].id,
        backup_type="full", status="success", snapshot="tank/media@backup-1",
    )
    db_session.add(run)
    db_session.commit()

    cmds = []

    async def fake_run_command(cmd, **kwargs):
        cmds.append(cmd)
        return ("", "", 0)

    with patch("nazman.managers.zfs_backup_manager.run_command", side_effect=fake_run_command), \
         patch.object(zfs_backup_manager, "_dev_path",
                      return_value="/dev/disk/by-id/ata-X-part1"), \
         patch.object(zfs_backup_manager, "_unmount_rec", new=AsyncMock(return_value=True)), \
         patch.object(zfs_backup_manager, "_fs_uuid", new=AsyncMock(return_value="NEWFS")), \
         patch("os.path.exists", return_value=True):
        rec = await zfs_backup_manager.recycle_disk(db_session, disks[0].id)

    assert ("wipefs", "-a", "/dev/disk/by-id/ata-X-part1") in [tuple(c) for c in cmds]
    assert ("mkfs.ext4", "-F", "/dev/disk/by-id/ata-X-part1") in [tuple(c) for c in cmds]
    row = db_session.get(BackupDisk, disks[0].id)
    assert row.fs_uuid == "NEWFS"
    assert row.mount_point == str(tmp_path / "NEWFS")
    assert db_session.query(BackupRun).filter_by(backup_disk_id=disks[0].id).count() == 0
    assert db_session.get(BackupDisk, disks[0].id).backup_set_id == sets[0].id
    assert rec["fs_uuid"] == "NEWFS"
