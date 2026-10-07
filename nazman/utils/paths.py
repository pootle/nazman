import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def ensure_dir(path) -> Path:
    """Create *path* as a directory, recovering from a stale file or dangling
    symlink at the location, and re-raise any other failure with a log entry.
    """
    p = Path(path)
    try:
        if p.is_symlink() or (p.exists() and not p.is_dir()):
            p.unlink()
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.exception("Failed to create directory %s", p)
        raise
    return p