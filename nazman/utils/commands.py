import asyncio
import subprocess
import shlex
import time
from typing import List, Optional, Tuple
from .exceptions import CommandError, CommandTimeoutError
from .command_log import command_log


async def run_command(
    cmd: List[str],
    timeout: int = 300,
    check: bool = True,
    capture_output: bool = True,
    input: Optional[str] = None,
    op: str | None = None,
    category: str | None = None,
) -> Tuple[str, str, int]:
    """
    Run a system command asynchronously.

    Args:
        cmd: Command and arguments as a list
        timeout: Timeout in seconds
        check: Raise exception on non-zero exit code
        capture_output: Capture stdout and stderr
        input: Optional string to write to the process's stdin
        op: Optional command-log type tag (read/write/system). Auto-detected
            from the command when not given.
        category: Optional command-log category tag (e.g. zfs, smartctl).

    Returns:
        Tuple of (stdout, stderr, returncode)
    """
    display_cmd = shlex.join(cmd)
    start = time.monotonic()
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE if input is not None else None,
            stdout=asyncio.subprocess.PIPE if capture_output else None,
            stderr=asyncio.subprocess.PIPE if capture_output else None
        )
        
        stdin_data = input.encode('utf-8') if input is not None else None
        stdout, stderr = await asyncio.wait_for(
            process.communicate(input=stdin_data),
            timeout=timeout
        )
        
        stdout_str = stdout.decode('utf-8') if stdout else ""
        stderr_str = stderr.decode('utf-8') if stderr else ""
        duration_ms = int((time.monotonic() - start) * 1000)
        
        if check and process.returncode != 0:
            command_log.record(
                command=display_cmd,
                status="failed",
                returncode=process.returncode,
                stderr=stderr_str,
                duration_ms=duration_ms,
                op=op,
                category=category,
            )
            raise CommandError(
                command=display_cmd,
                returncode=process.returncode,
                stderr=stderr_str
            )

        command_log.record(
            command=display_cmd,
            status="success",
            returncode=process.returncode,
            duration_ms=duration_ms,
            op=op,
            category=category,
        )
        return stdout_str, stderr_str, process.returncode
        
    except asyncio.TimeoutError:
        process.kill()
        command_log.record(
            command=display_cmd,
            status="timeout",
            duration_ms=int((time.monotonic() - start) * 1000),
            op=op,
            category=category,
        )
        raise CommandTimeoutError(command=display_cmd, timeout=timeout)
    except Exception as e:
        if isinstance(e, (CommandError, CommandTimeoutError)):
            raise
        command_log.record(
            command=display_cmd,
            status="error",
            returncode=-1,
            stderr=str(e),
            duration_ms=int((time.monotonic() - start) * 1000),
            op=op,
            category=category,
        )
        raise CommandError(
            command=display_cmd,
            returncode=-1,
            stderr=str(e)
        )


async def run_zpool(*args, **kwargs) -> Tuple[str, str, int]:
    """Run zpool command with arguments."""
    category = kwargs.pop("category", None) or "zpool"
    return await run_command(["zpool"] + list(args), category=category, **kwargs)


async def run_zfs(*args, **kwargs) -> Tuple[str, str, int]:
    """Run zfs command with arguments."""
    category = kwargs.pop("category", None) or "zfs"
    return await run_command(["zfs"] + list(args), category=category, **kwargs)


async def run_pipeline(
    cmd: str,
    timeout: int = 3600,
    check: bool = True,
    op: str | None = None,
    category: str | None = None,
) -> Tuple[str, str, int]:
    """Run a shell pipeline/redirection command (e.g. ``zfs send | gzip > file``).

    ``run_command`` uses ``create_subprocess_exec`` which has no shell, so
    pipelines and redirects need a ``sh -c`` wrapper.  Result is still recorded
    in the command log for auditability.
    """
    import shlex as _shlex
    display_cmd = cmd
    start = time.monotonic()
    try:
        process = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=timeout
        )
        stdout_str = stdout.decode("utf-8") if stdout else ""
        stderr_str = stderr.decode("utf-8") if stderr else ""
        duration_ms = int((time.monotonic() - start) * 1000)

        if check and process.returncode != 0:
            command_log.record(
                command=display_cmd, status="failed",
                returncode=process.returncode, stderr=stderr_str,
                duration_ms=duration_ms, op=op, category=category,
            )
            raise CommandError(
                command=display_cmd, returncode=process.returncode,
                stderr=stderr_str,
            )

        command_log.record(
            command=display_cmd, status="success",
            returncode=process.returncode, duration_ms=duration_ms,
            op=op, category=category,
        )
        return stdout_str, stderr_str, process.returncode

    except asyncio.TimeoutError:
        process.kill()
        command_log.record(
            command=display_cmd, status="timeout", duration_ms=timeout * 1000,
            op=op, category=category,
        )
        raise CommandTimeoutError(command=display_cmd, timeout=timeout)
    except Exception as e:
        if isinstance(e, (CommandError, CommandTimeoutError)):
            raise
        command_log.record(
            command=display_cmd, status="error", returncode=-1,
            stderr=str(e), duration_ms=duration_ms, op=op, category=category,
        )
        raise CommandError(command=display_cmd, returncode=-1, stderr=str(e))


async def run_command_sync(
    cmd: List[str],
    timeout: int = 300,
    op: str | None = None,
    category: str | None = None,
) -> str:
    """
    Run a command synchronously (for use in sync contexts).
    Returns stdout on success, raises CommandError on failure.
    """
    display_cmd = shlex.join(cmd)
    start = time.monotonic()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True
        )
        command_log.record(
            command=display_cmd,
            status="success",
            returncode=result.returncode,
            duration_ms=int((time.monotonic() - start) * 1000),
            op=op,
            category=category,
        )
        return result.stdout
    except subprocess.CalledProcessError as e:
        command_log.record(
            command=display_cmd,
            status="failed",
            returncode=e.returncode,
            stderr=e.stderr,
            duration_ms=int((time.monotonic() - start) * 1000),
            op=op,
            category=category,
        )
        raise CommandError(
            command=display_cmd,
            returncode=e.returncode,
            stderr=e.stderr
        )
    except subprocess.TimeoutExpired:
        command_log.record(
            command=display_cmd,
            status="timeout",
            duration_ms=int((time.monotonic() - start) * 1000),
            op=op,
            category=category,
        )
        raise CommandTimeoutError(command=display_cmd, timeout=timeout)
    except Exception as e:
        command_log.record(
            command=display_cmd,
            status="error",
            returncode=-1,
            stderr=str(e),
            duration_ms=int((time.monotonic() - start) * 1000),
            op=op,
            category=category,
        )
        raise


def parse_zpool_status(output: str) -> dict:
    """Parse zpool status output into structured data."""
    # TODO: Implement parsing
    return {"raw": output}


def parse_zpool_list(output: str) -> List[dict]:
    """Parse zpool list output into structured data."""
    # TODO: Implement parsing
    return [{"raw": output}]
