class NAZManError(Exception):
    """Base exception for NAZMan application."""
    pass


class CommandError(NAZManError):
    """Raised when a system command fails."""
    
    def __init__(self, command: str, returncode: int, stderr: str):
        self.command = command
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"Command failed: {command} (exit {returncode}): {stderr}")


class CommandTimeoutError(NAZManError):
    """Raised when a command times out."""
    
    def __init__(self, command: str, timeout: int):
        self.command = command
        self.timeout = timeout
        super().__init__(f"Command timed out after {timeout}s: {command}")


class DatabaseError(NAZManError):
    """Raised when a database operation fails."""
    pass


class ValidationError(NAZManError):
    """Raised when input validation fails."""
    pass


class NotFoundError(NAZManError):
    """Raised when a requested entity does not exist.

    Handlers map this to HTTP 404, so callers must not string-sniff messages
    to distinguish "missing" from "failed".
    """
    pass


class DiskError(NAZManError):
    """Raised when a disk operation fails."""
    pass


class DiskNotFoundError(DiskError, NotFoundError):
    """Raised when a disk row does not exist."""
    pass


class PoolError(NAZManError):
    """Raised when a pool operation fails."""
    pass


class PoolNotFoundError(PoolError, NotFoundError):
    """Raised when a pool does not exist."""
    pass


class DatasetError(NAZManError):
    """Raised when a dataset operation fails."""
    pass


class DatasetNotFoundError(DatasetError, NotFoundError):
    """Raised when a dataset does not exist."""
    pass


class NfsError(NAZManError):
    """Raised when an NFS operation fails."""
    pass


class SmbError(NAZManError):
    """Raised when an SMB/Samba operation fails."""
    pass


class BackupError(NAZManError):
    """Raised when a backup operation fails."""
    pass


class BackupDiskNotFoundError(BackupError, NotFoundError):
    """Raised when a declared backup disk does not exist."""
    pass


class BackupRunNotFoundError(BackupError, NotFoundError):
    """Raised when a backup run record does not exist."""
    pass
