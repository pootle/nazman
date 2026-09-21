import re
from typing import Optional
from .exceptions import ValidationError


def validate_pool_name(name: str) -> str:
    """Validate ZFS pool name."""
    if not name:
        raise ValidationError("Pool name cannot be empty")
    
    if len(name) > 256:
        raise ValidationError("Pool name too long (max 256 characters)")
    
    # Pool names can contain letters, numbers, hyphens, underscores, periods
    if not re.match(r'^[a-zA-Z0-9._-]+$', name):
        raise ValidationError(
            "Pool name can only contain letters, numbers, hyphens, underscores, and periods"
        )
    
    return name.lower()


def validate_dataset_name(name: str) -> str:
    """Validate ZFS dataset name."""
    if not name:
        raise ValidationError("Dataset name cannot be empty")
    
    if len(name) > 256:
        raise ValidationError("Dataset name too long (max 256 characters)")
    
    # Dataset names can contain letters, numbers, hyphens, underscores, periods, slashes
    if not re.match(r'^[a-zA-Z0-9._/-]+$', name):
        raise ValidationError(
            "Dataset name can only contain letters, numbers, hyphens, underscores, periods, and slashes"
        )
    
    # Reject leading or trailing slashes
    if name.startswith('/') or name.endswith('/'):
        raise ValidationError("Dataset name must not start or end with a slash")
    
    # Reject empty segments (e.g. "pool//ds")
    if '//' in name:
        raise ValidationError("Dataset name must not contain empty segments")
    
    return name.lower()


def validate_snapshot_component(name: str) -> str:
    """Validate the part of a snapshot name after the '@'."""
    if not name:
        raise ValidationError("Snapshot name cannot be empty")

    if len(name) > 256:
        raise ValidationError("Snapshot name too long (max 256 characters)")

    if not re.match(r'^[a-zA-Z0-9._:-]+$', name):
        raise ValidationError(
            "Snapshot name can only contain letters, numbers, hyphens, underscores, periods, and colons"
        )

    return name


def validate_snapshot_name(name: str) -> str:
    """Validate a full snapshot name of the form dataset@snapshot.

    Rejecting anything without exactly one '@' is what stops a snapshot
    endpoint from ever being pointed at a dataset.
    """
    if not name:
        raise ValidationError("Snapshot name cannot be empty")

    if name.count('@') != 1:
        raise ValidationError("Snapshot name must be of the form dataset@snapshot")

    dataset, snap = name.split('@', 1)
    validate_dataset_name(dataset)
    validate_snapshot_component(snap)
    return name


def validate_device_path(path: str) -> str:
    """Validate device path."""
    if not path:
        raise ValidationError("Device path cannot be empty")
    
    if not path.startswith('/dev/'):
        raise ValidationError("Device path must start with /dev/")
    
    return path


def validate_ip_cidr(cidr: str) -> str:
    """Validate IP CIDR notation (e.g., 192.168.1.0/24)."""
    if not cidr:
        raise ValidationError("CIDR cannot be empty")
    
    # Support both IP and CIDR formats
    pattern = r'^(\d{1,3}\.){3}\d{1,3}(/\d{1,2})?$'
    if not re.match(pattern, cidr):
        raise ValidationError(f"Invalid IP/CIDR format: {cidr}")
    
    # Validate IP octets
    ip_part = cidr.split('/')[0]
    octets = ip_part.split('.')
    for octet in octets:
        if int(octet) > 255:
            raise ValidationError(f"Invalid IP address: {cidr}")
    
    # Validate CIDR prefix if present
    if '/' in cidr:
        prefix = int(cidr.split('/')[1])
        if prefix < 0 or prefix > 32:
            raise ValidationError(f"Invalid CIDR prefix: {prefix}")
    
    return cidr


def validate_size_string(size: str) -> str:
    """Validate size string (e.g., 10G, 1T, 500M)."""
    if not size:
        raise ValidationError("Size cannot be empty")
    
    pattern = r'^(\d+)([KMGTP]?)$'
    match = re.match(pattern, size.upper())
    
    if not match:
        raise ValidationError(f"Invalid size format: {size}")
    
    return size.lower()


def validate_schedule(schedule: str) -> str:
    """Validate cron-like schedule string."""
    if not schedule:
        raise ValidationError("Schedule cannot be empty")
    
    parts = schedule.split()
    if len(parts) != 5:
        raise ValidationError("Schedule must have 5 fields: minute hour day month weekday")
    
    # Basic validation - each field should be a number or * or comma-separated values
    for part in parts:
        if not re.match(r'^[\d,*\/\-]+$', part):
            raise ValidationError(f"Invalid schedule field: {part}")
    
    return schedule


def validate_telegram_bot_token(token: str) -> str:
    """Validate a Telegram bot token (``<bot_id>:<base64url secret>``)."""
    token = (token or "").strip()
    if ":" not in token:
        raise ValidationError("Telegram bot token must look like '<bot id>:<secret>'")
    bot_id, secret = token.split(":", 1)
    if not bot_id.isdigit():
        raise ValidationError("Telegram bot token id part must be numeric")
    if not re.match(r'^[A-Za-z0-9_-]{30,}$', secret):
        raise ValidationError("Telegram bot token secret part looks invalid")
    return token


def validate_telegram_chat_id(chat_id: str) -> str:
    """Validate a Telegram chat id (numeric, optionally leading '-' for groups)."""
    chat_id = (chat_id or "").strip()
    if not chat_id:
        raise ValidationError("Telegram chat id cannot be empty")
    if not chat_id.lstrip("-").isdigit():
        raise ValidationError("Telegram chat id must be a numeric id")
    return chat_id


def validate_alert_threshold(threshold) -> int:
    """Validate a pool usage alert threshold (1-100 percent)."""
    try:
        value = int(threshold)
    except (TypeError, ValueError):
        raise ValidationError("Alert threshold must be a number between 1 and 100")
    if value < 1 or value > 100:
        raise ValidationError("Alert threshold must be between 1 and 100")
    return value


def validate_alert_cooldown(minutes) -> int:
    """Validate an alert cooldown in minutes (>= 0, 0 disables suppression)."""
    try:
        value = int(minutes)
    except (TypeError, ValueError):
        raise ValidationError("Alert cooldown must be a whole number of minutes")
    if value < 0:
        raise ValidationError("Alert cooldown cannot be negative")
    return value
