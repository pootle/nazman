import gzip
import os
import pytest
from unittest.mock import patch, AsyncMock
from nazman.utils.commands import (
    run_command,
    run_zpool,
    run_zfs,
    run_command_sync,
    run_pipeline,
)
from nazman.utils.exceptions import CommandError, CommandTimeoutError


@pytest.mark.asyncio
async def test_run_command_success():
    stdout, stderr, rc = await run_command(["echo", "hello"], timeout=10)
    assert "hello" in stdout
    assert rc == 0


@pytest.mark.asyncio
async def test_run_command_failure_no_check():
    stdout, stderr, rc = await run_command(
        ["false"], timeout=10, check=False
    )
    assert rc != 0


@pytest.mark.asyncio
async def test_run_command_failure_with_check():
    with pytest.raises(CommandError) as exc_info:
        await run_command(["false"], timeout=10, check=True)
    assert exc_info.value.returncode != 0


@pytest.mark.asyncio
async def test_run_command_timeout():
    with pytest.raises(CommandTimeoutError):
        await run_command(["sleep", "60"], timeout=0.1)


@pytest.mark.asyncio
async def test_run_command_env():
    stdout, stderr, rc = await run_command(
        ["printenv", "NAZMAN_TEST_ENV"], env={"NAZMAN_TEST_ENV": "hello"}, timeout=10
    )
    assert rc == 0
    assert stdout.strip() == "hello"


@pytest.mark.asyncio
async def test_run_zpool_success():
    with patch("nazman.utils.commands.run_command", new_callable=AsyncMock,
               return_value=("pool\n", "", 0)):
        stdout, stderr, rc = await run_zpool("list", "-H", timeout=10)
    assert rc == 0


@pytest.mark.asyncio
async def test_run_zfs_help():
    with patch("nazman.utils.commands.run_command", new_callable=AsyncMock,
               return_value=("usage: zfs ...", "", 0)):
        stdout, stderr, rc = await run_zfs("help", timeout=10)
    assert rc == 0


@pytest.mark.asyncio
async def test_run_command_sync_success():
    stdout = await run_command_sync(["echo", "hello"])
    assert "hello" in stdout


@pytest.mark.asyncio
async def test_run_command_sync_failure():
    with pytest.raises(CommandError):
        await run_command_sync(["false"], timeout=10)


@pytest.mark.asyncio
async def test_run_pipeline_connects_stages():
    stdout, stderr, rc = await run_pipeline(
        [["printf", "b\\na\\nc\\n"], ["sort"]], timeout=10
    )
    assert rc == 0
    assert stdout == "a\nb\nc\n"


@pytest.mark.asyncio
async def test_run_pipeline_writes_stdout_path(tmp_path):
    out = tmp_path / "out.gz"
    stdout, stderr, rc = await run_pipeline(
        [["echo", "payload"], ["gzip", "-6"]], stdout_path=str(out), timeout=10
    )
    assert rc == 0
    assert stdout == ""
    with gzip.open(out, "rb") as f:
        assert f.read() == b"payload\n"


@pytest.mark.asyncio
async def test_run_pipeline_reads_stdin_path(tmp_path):
    src = tmp_path / "in.gz"
    with gzip.open(src, "wb") as f:
        f.write(b"hello\n")
    stdout, stderr, rc = await run_pipeline(
        [["gunzip", "-c"], ["cat"]], stdin_path=str(src), timeout=10
    )
    assert rc == 0
    assert stdout == "hello\n"


@pytest.mark.asyncio
async def test_run_pipeline_reports_first_failing_stage():
    stdout, stderr, rc = await run_pipeline(
        [["sh", "-c", "echo x; exit 3"], ["cat"]], timeout=10, check=False
    )
    assert rc == 3


@pytest.mark.asyncio
async def test_run_pipeline_check_raises():
    with pytest.raises(CommandError):
        await run_pipeline([["false"], ["cat"]], timeout=10)


@pytest.mark.asyncio
async def test_run_pipeline_does_not_interpret_shell_metacharacters(tmp_path):
    marker = tmp_path / "injected"
    stdout, stderr, rc = await run_pipeline(
        [["echo", f"; touch {marker}"], ["cat"]], timeout=10
    )
    assert rc == 0
    assert "touch" in stdout
    assert not marker.exists()


@pytest.mark.asyncio
async def test_run_pipeline_timeout_kills_all_stages():
    with pytest.raises(CommandTimeoutError):
        await run_pipeline([["sleep", "60"], ["sleep", "60"]], timeout=0.2)
    await asyncio_sleep(0.1)
    ps = os.popen("ps -eo pid,args | grep -E '^ *[0-9]+ sleep 60$'").read()
    assert ps.strip() == ""


async def asyncio_sleep(secs):
    import asyncio
    await asyncio.sleep(secs)
