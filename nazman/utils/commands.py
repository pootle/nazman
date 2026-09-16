import asyncio
import os
import signal
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
    env: Optional[dict] = None,
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
        env: Optional dict of environment variables to set for the child
            process. Defaults to inheriting the parent environment.

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
            stderr=asyncio.subprocess.PIPE if capture_output else None,
            env=env,
        )
        
        stdin_data = input.encode('utf-8') if input is not None else None
        stdout, stderr = await asyncio.wait_for(
            process.communicate(input=stdin_data),
            timeout=timeout
        )
        
        stdout_str = stdout.decode('utf-8') if stdout else ""
        stderr_str = stderr.decode('utf-8') if stderr else ""
        duration_ms = int((time.monotonic() - start) * 1000)
        
        if process.returncode != 0:
            command_log.record(
                command=display_cmd,
                status="failed",
                returncode=process.returncode,
                stderr=stderr_str,
                duration_ms=duration_ms,
                op=op,
                category=category,
            )
            if check:
                raise CommandError(
                    command=display_cmd,
                    returncode=process.returncode,
                    stderr=stderr_str
                )
        else:
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
        await process.wait()
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


def _pipeline_display(
    stages: List[List[str]],
    stdin_path: Optional[str],
    stdout_path: Optional[str],
) -> str:
    text = " | ".join(shlex.join(stage) for stage in stages)
    if stdin_path is not None:
        text = f"{text} < {shlex.quote(stdin_path)}"
    if stdout_path is not None:
        text = f"{text} > {shlex.quote(stdout_path)}"
    return text


def _kill_process_group(process: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        process.kill()
    except ProcessLookupError:
        pass


async def run_pipeline(
    stages: List[List[str]],
    timeout: int = 3600,
    check: bool = True,
    op: str | None = None,
    category: str | None = None,
    stdin_path: Optional[str] = None,
    stdout_path: Optional[str] = None,
) -> Tuple[str, str, int]:
    """Run a pipeline of argv commands (e.g. ``zfs send ... | gzip``) without a shell.

    Each stage is exec'd directly with its argv list; stdout of one stage is
    connected to stdin of the next via an OS pipe.  Optionally the first stage
    reads from ``stdin_path`` and the last stage writes to ``stdout_path``.
    Every stage runs in its own session so a timeout kills the whole group
    rather than orphaning children.  The returned exit code is the first
    non-zero code in pipeline order (like ``set -o pipefail``).
    """
    if not stages or any(not stage for stage in stages):
        raise ValueError("run_pipeline requires at least one non-empty stage")

    display_cmd = _pipeline_display(stages, stdin_path, stdout_path)
    start = time.monotonic()
    procs: List[asyncio.subprocess.Process] = []
    open_files = []
    parent_fds: List[int] = []

    try:
        first_stdin = None
        if stdin_path is not None:
            first_stdin = open(stdin_path, "rb")
            open_files.append(first_stdin)
        last_stdout = asyncio.subprocess.PIPE
        if stdout_path is not None:
            last_stdout = open(stdout_path, "wb")
            open_files.append(last_stdout)

        prev_read = None
        for idx, stage in enumerate(stages):
            is_last = idx == len(stages) - 1
            stdin_arg = first_stdin if idx == 0 else prev_read
            if is_last:
                stdout_arg = last_stdout
                next_read = None
            else:
                next_read, write_end = os.pipe()
                parent_fds.extend([next_read, write_end])
                stdout_arg = write_end

            process = await asyncio.create_subprocess_exec(
                *stage,
                stdin=stdin_arg,
                stdout=stdout_arg,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            procs.append(process)

            if prev_read is not None:
                os.close(prev_read)
                parent_fds.remove(prev_read)
            if not is_last:
                os.close(write_end)
                parent_fds.remove(write_end)
            prev_read = next_read

        results = await asyncio.wait_for(
            asyncio.gather(*(p.communicate() for p in procs)),
            timeout=timeout,
        )
        duration_ms = int((time.monotonic() - start) * 1000)

        stdout_str = ""
        last_out = results[-1][0]
        if last_out:
            stdout_str = last_out.decode("utf-8", errors="replace")
        stderr_parts = []
        for stage, (_, err) in zip(stages, results):
            if err:
                stderr_parts.append(f"{stage[0]}: {err.decode('utf-8', errors='replace').strip()}")
        stderr_str = "\n".join(stderr_parts)

        returncode = 0
        for p in procs:
            if p.returncode != 0:
                returncode = p.returncode
                break

        if returncode != 0:
            command_log.record(
                command=display_cmd, status="failed",
                returncode=returncode, stderr=stderr_str,
                duration_ms=duration_ms, op=op, category=category,
            )
            if check:
                raise CommandError(
                    command=display_cmd, returncode=returncode,
                    stderr=stderr_str,
                )
        else:
            command_log.record(
                command=display_cmd, status="success",
                returncode=returncode, duration_ms=duration_ms,
                op=op, category=category,
            )
        return stdout_str, stderr_str, returncode

    except asyncio.TimeoutError:
        for p in procs:
            _kill_process_group(p)
        await asyncio.gather(*(p.wait() for p in procs), return_exceptions=True)
        command_log.record(
            command=display_cmd, status="timeout",
            duration_ms=int((time.monotonic() - start) * 1000),
            op=op, category=category,
        )
        raise CommandTimeoutError(command=display_cmd, timeout=timeout)
    except Exception as e:
        if isinstance(e, (CommandError, CommandTimeoutError)):
            raise
        for p in procs:
            _kill_process_group(p)
        await asyncio.gather(*(p.wait() for p in procs), return_exceptions=True)
        command_log.record(
            command=display_cmd, status="error", returncode=-1,
            stderr=str(e), duration_ms=int((time.monotonic() - start) * 1000),
            op=op, category=category,
        )
        raise CommandError(command=display_cmd, returncode=-1, stderr=str(e))
    finally:
        for fd in parent_fds:
            try:
                os.close(fd)
            except OSError:
                pass
        for f in open_files:
            try:
                f.close()
            except OSError:
                pass


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
