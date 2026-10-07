"""Human-readable size string parsing shared across managers.

Handles the on-disk (lsblk / zpool / zfs) size formats: raw bytes as well as
single-letter suffixes (K/M/G/T/P) and the extended KB/MB/GB/TB/PB and
KiB/MiB/GiB/TiB/PiB forms.  Always returns whole bytes.
"""

_BASE_MULTIPLIERS = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "P": 1024**5}
_EXTENDED_SUFFIXES = (
    "KB", "MB", "GB", "TB", "PB",
    "KIB", "MIB", "GIB", "TIB", "PIB",
)


def parse_size_to_bytes(size_str: str) -> int:
    """Parse ``size_str`` into bytes (int), or 0 when unparseable."""
    if not size_str:
        return 0
    s = size_str.strip()
    if not s:
        return 0
    try:
        return int(float(s))
    except ValueError:
        pass
    upper = s.upper()
    for suf in _EXTENDED_SUFFIXES:
        if upper.endswith(suf):
            try:
                return int(float(s[: -len(suf)]) * _BASE_MULTIPLIERS[suf[0]])
            except ValueError:
                return 0
    unit = upper[-1]
    if unit in _BASE_MULTIPLIERS:
        try:
            return int(float(s[:-1]) * _BASE_MULTIPLIERS[unit])
        except ValueError:
            return 0
    return 0