import os
import secrets
from pathlib import Path
from pydantic_settings import BaseSettings
from functools import lru_cache
from typing import Any, Optional

SECRET_PATH = "/etc/nazman/auth.secret"
SECRET_LEN = 32


class Settings(BaseSettings):
    # Database
    database_path: str = "/var/lib/nazman/nazman.db"
    
    # Backup
    backup_enabled: bool = True
    backup_repo_path: str = "/mnt/backup/nazman-config"
    backup_auto_commit: bool = True
    backup_push_on_commit: bool = True
    backup_mount_base: str = "/mnt/backup"  # parent dir under which backup disks are mounted
    backup_gzip_level: int = 6
    backup_full_margin: float = 1.2  # capacity safety margin multiplier for full backups
    
    # Auth
    auth_enabled: bool = True
    auth_password_hash: str = ""
    
    # Monitoring
    monitoring_refresh_interval: int = 5
    monitoring_history_size: int = 12
    monitoring_enable_websocket: bool = True
    network_interface: str = ""

    # Metrics logging (per-pool disk + system metrics to disk)
    metrics_log_enabled: bool = False
    metrics_log_retention_days: int = 30
    metrics_log_path: str = "/var/lib/nazman/metrics.db"

    # Command log
    command_log_size: int = 25
    command_log_path: str = "/var/lib/nazman/command_log.db"
    command_log_retention_days: int = 30
    
    # Logging
    logging_level: str = "INFO"
    logging_file: str = "/var/log/nazman/nazman.log"
    
    # App
    app_host: str = "0.0.0.0"
    app_port: int = 8080
    app_title: str = "NAZMan - ZFS NAS Management"
    app_version: str = "0.2.0"

    model_config = {
        "env_file": "/etc/nazman/nazman.conf",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


def ensure_auth_secret() -> str:
    """Return the JWT signing secret, generating and persisting one if absent.

    Stored in ``/etc/nazman/auth.secret`` with mode 0600 so only root can read it.
    """
    try:
        existing = Path(SECRET_PATH).read_text(encoding="utf-8").strip()
        if existing and len(existing) >= SECRET_LEN:
            return existing
    except OSError:
        pass

    secret = secrets.token_hex(SECRET_LEN)
    try:
        p = Path(SECRET_PATH)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(secret + "\n", encoding="utf-8")
        os.chmod(SECRET_PATH, 0o600)
    except OSError:
        pass

    return secret


@lru_cache()
def get_settings() -> Settings:
    return Settings()


def set_setting(key: str, value: Any) -> bool:
    """Persist a single config key back to the INI-style conf file.

    Rewrites known keys in place, preserving comments and unknown lines. Returns
    True on success. Used so runtime toggles (e.g. metrics logging) survive a
    restart. The in-memory settings object is refreshed so the new value is live.
    """
    path = Path(get_settings().model_config["env_file"])
    key_lower = (key or "").lower()
    updated = False
    lines = []
    try:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        lines = text.splitlines()
    except OSError:
        lines = []

    # Normalise value to the format pydantic-settings expects for bools.
    if isinstance(value, bool):
        rendered = "true" if value else "false"
    else:
        rendered = str(value)

    out = []
    found = False
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith(("#", ";")) and "=" in stripped:
            k = stripped.split("=", 1)[0].strip()
            if k == key_lower:
                out.append(f"{key_lower}={rendered}")
                found = True
                continue
        out.append(line)
    if not found:
        out.append(f"{key_lower}={rendered}")

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
        updated = True
    except OSError:
        updated = False

    if updated:
        get_settings.cache_clear()

    return updated


def ensure_directories():
    """Ensure required directories exist."""
    settings = get_settings()
    
    try:
        # Database directory
        db_dir = Path(settings.database_path).parent
        db_dir.mkdir(parents=True, exist_ok=True)
        
        # Log directory
        log_dir = Path(settings.logging_file).parent
        log_dir.mkdir(parents=True, exist_ok=True)

        # Metrics log directory
        metrics_dir = Path(settings.metrics_log_path).parent
        metrics_dir.mkdir(parents=True, exist_ok=True)
        
        # Backup directory
        if settings.backup_enabled:
            backup_dir = Path(settings.backup_repo_path)
            backup_dir.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        pass
