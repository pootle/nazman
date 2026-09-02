"""Non-persistent in-memory metrics recorder.

Runs a background loop in the (single-worker) app process, sampling configured
metrics every `monitoring_refresh_interval` seconds into fixed-size ring
buffers. Recent history is available to the API/dashboard at any time, so the
dashboard shows recent activity immediately on arrival instead of starting empty.

Non-persistent: buffers live in memory and are reset on restart.

Extensible: register additional metrics (e.g. network traffic, disk busy time)
with a collector callable and they will be recorded automatically.
"""

import asyncio
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Callable, DefaultDict, Deque, Dict, List, Optional

import psutil

from ..config import get_settings

Sample = Dict[str, Any]  # {"ts": float, "value": float}


class MetricsManager:
    def __init__(self) -> None:
        self._buffers: DefaultDict[str, Deque[Sample]] = defaultdict(lambda: deque())
        self._collectors: Dict[str, Callable] = {}
        self._task: Optional[asyncio.Task] = None
        self._started = False
        self._default_size = get_settings().monitoring_history_size
        self._interval = get_settings().monitoring_refresh_interval
        # Latest captured values, used to persist samples to the metrics store.
        self._latest: Dict[str, float] = {}
        self._last_disk_values: Dict[str, float] = {}
        self._pool_disks_map: Dict[str, List[str]] = {}
        self._pool_cache_ts: float = 0.0
        self._pool_cache_secs: float = 60.0

    # ── Registration ────────────────────────────────────────────────────

    def register(self, name: str, collector: Callable, size: Optional[int] = None) -> None:
        """Register a metric collector and its ring buffer.

        `collector` is a plain (sync) callable returning a float value, or an
        object with a ``collect(push)`` method that pushes one or more values.
        `size` is the max samples kept; defaults to the configured history size.
        """
        self._collectors[name] = collector
        if name not in self._buffers:
            maxlen = size or self._default_size
            self._buffers[name] = deque(maxlen=maxlen)

    # ── Lifecycle ───────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the background recording loop (idempotent)."""
        if self._started:
            return
        self._started = True
        # Take an immediate sample so buffers are populated right away
        self.sample()
        self._task = asyncio.create_task(self._record_loop())
        try:
            await asyncio.sleep(0)  # let the task get scheduled
        except asyncio.CancelledError:
            pass

    async def stop(self) -> None:
        """Stop the background recording loop (idempotent)."""
        if not self._started:
            return
        self._started = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            self._task = None

    async def _record_loop(self) -> None:
        while self._started:
            await asyncio.sleep(self._interval)
            if self._started:
                self.sample()
                await self._after_sample()

    # ── Persistence (to metrics store) ──────────────────────────────────

    async def _after_sample(self) -> None:
        """Persist the latest sample to the disk metrics store (per-pool).

        System metrics (cpu/memory/net) are stored under the sentinel SYSTEM_POOL.
        For every pool with logging enabled, that pool's disk busy% values are
        stored under the pool name.
        """
        try:
            from .metrics_store import metrics_store, SYSTEM_POOL
        except Exception:
            return

        if not self._latest:
            return

        ts = int(time.time())
        try:
            enabled = [p for p in metrics_store.list_enabled_pools() if p != "*"]
            if enabled:
                for metric in ("cpu", "memory", "net"):
                    if metric in self._latest:
                        metrics_store.record(SYSTEM_POOL, metric, "<system>", ts,
                                             self._latest[metric])

            if self._last_disk_values:
                for pool in enabled:
                    bases = await self._resolve_pool_disks(pool)
                    if not bases:
                        continue
                    for base in bases:
                        side = self._last_disk_values.get(base)
                        if side is not None:
                            metrics_store.record(pool, "disk", base, ts, side)
            metrics_store.tick()
            # Prune old rows (self-gated to run at most once per day).
            try:
                metrics_store.prune()
            except Exception:
                pass
        except Exception:
            pass

    async def _resolve_pool_disks(self, pool_name: str) -> List[str]:
        """Return all base disk device names belonging to a pool (cached)."""
        now = time.time()
        if now - self._pool_cache_ts >= self._pool_cache_secs or \
                pool_name not in self._pool_disks_map:
            try:
                await self._refresh_pool_disks()
            except Exception:
                pass
            self._pool_cache_ts = now
        return self._pool_disks_map.get(pool_name, [])

    async def _refresh_pool_disks(self) -> None:
        from .zfs_manager import zfs_manager

        series_names = get_disk_series_names()
        new_map: Dict[str, List[str]] = {}
        try:
            from .metrics_store import metrics_store

            pool_names = metrics_store.list_enabled_pools()
        except Exception:
            pool_names = []

        for pool in pool_names:
            if pool == "*":
                continue
            try:
                status = await zfs_manager.get_pool_status(pool)
            except Exception:
                continue
            bases: List[str] = []
            for group in ("data_vdevs", "special_vdevs", "log_vdevs", "cache_vdevs"):
                for vdev in status.get(group, []):
                    for child in vdev.get("children", []):
                        leaf = child.get("name") or child.get("path") or ""
                        base = normalize_base_name(leaf)
                        if base in series_names and base not in bases:
                            bases.append(base)
            new_map[pool] = bases
        self._pool_disks_map = new_map

    # ── Sampling ────────────────────────────────────────────────────────

    def _push(self, name: str, value: float) -> None:
        self._latest[name] = float(value)
        if name.startswith("disk_"):
            self._last_disk_values[name[len("disk_"):]] = float(value)
        buf = self._buffers.get(name)
        if buf is None:
            buf = deque(maxlen=self._default_size)
            self._buffers[name] = buf
        elif not buf.maxlen:
            self._buffers[name] = deque(buf, maxlen=self._default_size)
            buf = self._buffers[name]
        buf.append({"ts": time.time(), "value": float(value)})

    def sample(self) -> None:
        """Collect a sample from every registered metric and append to buffers."""
        # Allow dynamic collectors to (re)discover their sub-series first
        for collector in self._collectors.values():
            if hasattr(collector, "refresh"):
                try:
                    collector.refresh()
                except Exception:
                    pass

        for name, collector in self._collectors.items():
            try:
                if hasattr(collector, "collect"):
                    collector.collect(self._push)
                else:
                    self._push(name, collector())
            except Exception:
                continue

    # ── Accessors ───────────────────────────────────────────────────────

    def get_series(self, name: str, limit: Optional[int] = None) -> List[Sample]:
        """Return the ring buffer for a metric (most recent last)."""
        buf = self._buffers.get(name)
        if buf is None:
            return []
        if limit is not None and limit > 0:
            return list(buf)[-limit:]
        return list(buf)

    def get_metrics(self) -> Dict[str, List[Sample]]:
        """Return all metric series."""
        return {name: list(buf) for name, buf in self._buffers.items()}


# ── Network collector ────────────────────────────────────────────────────

def list_network_interfaces() -> List[Dict[str, Any]]:
    """Enumerate network interfaces with their link speed (Mbps).

    Returns [{name, speed_mbps, up}] for every non-loopback interface where the
    link speed can be read from the kernel (``/sys/class/net/<iface>/speed``).
    """
    net_dir = Path("/sys/class/net")
    result = []
    if not net_dir.is_dir():
        return result
    for iface_path in net_dir.iterdir():
        name = iface_path.name
        if name == "lo":
            continue
        speed_file = iface_path / "speed"
        speed = None
        try:
            if speed_file.is_file():
                raw = speed_file.read_text().strip()
                if raw and raw != "unknown":
                    speed = int(raw)
        except Exception:
            speed = None
        up = False
        up_file = iface_path / "operstate"
        try:
            up = up_file.read_text().strip() == "up"
        except Exception:
            pass
        result.append({"name": name, "speed_mbps": speed, "up": up})
    return result


def _detect_network_interface(configured: str) -> Optional[str]:
    if configured:
        return configured
    for iface in list_network_interfaces():
        if iface["up"] and iface["speed_mbps"]:
            return iface["name"]
    return None


class _NetworkCollector:
    """Stateful collector computing network utilisation as % of link bandwidth."""

    def __init__(self) -> None:
        self._last: Optional[Dict[str, int]] = None
        self._last_ts: Optional[float] = None
        self._iface: Optional[str] = None

    def refresh(self) -> None:
        configured = get_settings().network_interface
        iface = _detect_network_interface(configured)
        if iface != self._iface:
            self._iface = iface
            self._last = None
            self._last_ts = None

    def __call__(self) -> float:
        if self._iface is None:
            return 0.0
        try:
            speed = self._link_speed_mbps()
        except Exception:
            return 0.0
        if not speed:
            self._last = None
            self._last_ts = None
            return 0.0

        counters = psutil.net_io_counters(pernic=True)
        cur = counters.get(self._iface)
        if cur is None:
            return 0.0
        cur_bytes = cur.bytes_recv + cur.bytes_sent
        now = time.time()

        if self._last is None or self._last_ts is None:
            self._last = cur_bytes
            self._last_ts = now
            return 0.0

        dt = now - self._last_ts
        if dt <= 0:
            return 0.0

        delta_bytes = max(0, cur_bytes - self._last)
        self._last = cur_bytes
        self._last_ts = now

        # bytes/sec -> bits/sec -> percentage of link bandwidth
        bits_per_sec = (delta_bytes * 8) / dt
        pct = (bits_per_sec / (speed * 1_000_000)) * 100.0
        return max(0.0, min(100.0, pct))

    def _link_speed_mbps(self) -> Optional[int]:
        if self._iface is None:
            return None
        speed_file = Path("/sys/class/net") / self._iface / "speed"
        try:
            raw = speed_file.read_text().strip()
        except Exception:
            return None
        try:
            return int(raw) if raw and raw != "unknown" else None
        except (ValueError, TypeError):
            return None


# ── Disk collector ───────────────────────────────────────────────────────

def _scan_disk_devices() -> List[str]:
    """Return base kernel device names of physical block devices."""
    base_dir = Path("/sys/block")
    names = []
    for dev in base_dir.iterdir():
        name = dev.name
        if name.startswith("dm-") or name.startswith("loop"):
            continue
        # Skip internal boot/partition helper devices
        if "boot" in name:
            continue
        # Skip partitions (partition children have a 'partition' file)
        if (dev / "partition").exists():
            continue
        # Only consider obviously-disk devices
        if name.startswith("sd") or name.startswith("nvme") or name.startswith("vd") \
                or name.startswith("mmcblk") or name.startswith("hd"):
            names.append(name)
    return names


def normalize_base_name(leaf: str) -> str:
    """Map a ZFS leaf device name to a base block device name.

    Handles kernel names (``sda1`` -> ``sda``, ``nvme0n1p1`` -> ``nvme0n1``),
    ``/dev/...`` prefixes and ``/dev/disk/by-id/...`` symlinks, and bare by-id
    alias strings (e.g. ``ata-WDC_...-part1``).
    """
    import os
    name = leaf.strip()
    if not name:
        return leaf
    # by-id kernel symlink target wrapping
    if name.startswith("."):
        name = name[1:]
    leaf_bare = name.rsplit("/", 1)[-1]
    # Try absolute leaf path directly
    try:
        if os.path.isabs(name) and os.path.exists(name):
            return _strip_partition(os.path.realpath(name).rsplit("/", 1)[-1])
    except Exception:
        pass
    # Try /dev/<leaf> (handles bare kernel names and bare by-id aliases)
    for dev_dir in ("/dev", "/dev/disk/by-id"):
        try:
            p = f"{dev_dir}/{leaf_bare}"
            if os.path.exists(p):
                return _strip_partition(os.path.realpath(p).rsplit("/", 1)[-1])
        except Exception:
            pass
    return _strip_partition(leaf_bare)


def _strip_partition(name: str) -> str:
    """Strip a partition suffix from a kernel device name."""
    # nvme0n1p2 -> nvme0n1 ; mmcblk0p1 -> mmcblk0
    import re
    m = re.match(r"^(nvme\d+n\d+)p\d+$", name)
    if m:
        return m.group(1)
    m = re.match(r"^(mmcblk\d+)p\d+$", name)
    if m:
        return m.group(1)
    # sda1, vda1, sdb2 ...
    m = re.match(r"^(sd[a-z]+)\d+$", name)
    if m:
        return m.group(1)
    # hdX / vdX partitions
    m = re.match(r"^([shv]d[a-z]+)\d+$", name)
    if m:
        return m.group(1)
    return name


def _read_io_ticks(base: str) -> Optional[int]:
    """Read /sys/block/<base>/stat field 9 (io_ticks = ms busy)."""
    try:
        text = (Path("/sys/block") / base / "stat").read_text()
    except Exception:
        return None
    fields = text.split()
    if len(fields) < 10:
        return None
    try:
        return int(fields[9])
    except (ValueError, IndexError):
        return None


class _DiskCollector:
    """Collects busy-time utilisation (%) for every physical disk.

    ``refresh()`` rescans /sys/block to pick up newly attached disks. Each disk
    gets its own series name ``disk_<base>`` via ``collect(push)``.
    """

    def __init__(self) -> None:
        self._prev: Dict[str, int] = {}
        self._last_ts: Optional[float] = None
        self._devices: List[str] = []
        self._series_names: Dict[str, str] = {}

    def refresh(self) -> None:
        devices = _scan_disk_devices()
        new_series = {}
        for base in devices:
            series = f"disk_{base}"
            new_series[base] = series
            if base not in self._series_names and base not in self._prev:
                self._prev[base] = _read_io_ticks(base)
        removed = set(self._series_names) - set(devices)
        for base in removed:
            self._prev.pop(base, None)
        self._devices = devices
        self._series_names = new_series

    def collect(self, push: Callable[[str, float], None]) -> None:
        now = time.time()
        if self._last_ts is None:
            self._last_ts = now
            return
        dt_ms = (now - self._last_ts) * 1000.0
        self._last_ts = now
        if dt_ms <= 0:
            return
        for base in self._devices:
            series = self._series_names.get(base)
            if series is None:
                continue
            cur = _read_io_ticks(base)
            if cur is None:
                continue
            prev = self._prev.get(base)
            self._prev[base] = cur
            if prev is None:
                continue
            if cur < prev:  # counter reset (device reattached)
                continue
            pct = ((cur - prev) / dt_ms) * 100.0
            push(series, max(0.0, min(100.0, pct)))

    def series_names(self) -> Dict[str, str]:
        return dict(self._series_names)


# ── Built-in collectors ─────────────────────────────────────────────────

def _collect_cpu() -> float:
    return psutil.cpu_percent()


def _collect_memory() -> float:
    return psutil.virtual_memory().percent


# Singleton instance
metrics_manager = MetricsManager()

# Register default metrics
metrics_manager.register("cpu", _collect_cpu)
metrics_manager.register("memory", _collect_memory)
metrics_manager.register("net", _NetworkCollector())
metrics_manager.register("disk", _DiskCollector())


def get_disk_series_names() -> Dict[str, str]:
    """Return a mapping of base block device name -> metrics series name."""
    collector = metrics_manager._collectors.get("disk")
    if isinstance(collector, _DiskCollector):
        return collector.series_names()
    return {}


def get_net_metric_name() -> str:
    """Return the metrics series name for the network metric."""
    return "net"


def get_selected_network_interface() -> Optional[str]:
    """Return the currently-selected network interface (resolved fallback)."""
    return _detect_network_interface(get_settings().network_interface)
