import pytest
from unittest.mock import patch, AsyncMock
from nazman.utils.commands import (
    run_command,
    run_zpool,
    run_zfs,
    run_command_sync,
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
