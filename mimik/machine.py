"""Measure the PC silently (U35) — stdlib only.

The backend configures itself from what the machine actually is:
total RAM, CPU count, disk free. Nothing here shows a setting to the
user; the values are logged in the debug log at startup and used under
the hood. Doctor reports the measured specs in plain words.

Derived settings (no visible UI anywhere):
  - Ghidra heap: a quarter of RAM, clamped to 1G-4G (2G on his 8 GB
    PC, the headless default).
  - Capture cap: how many flows per host the store keeps in memory
    (5000 on 4-16 GB machines, 2500 below 4 GB, 10000 above 16 GB).
  - Worker threads: MIMIK runs at most two background threads (the
    proxy poller + one task worker); the measurement confirms 2 is
    right for this class of machine (U25: heavy work must leave the
    desktop usable, never freeze it).
"""
import os
import shutil

_GB = 1024 * 1024 * 1024

_cache = None


def _ram_total_bytes():
    """Total RAM in bytes: /proc/meminfo first (Linux), sysconf
    fallback. 0 when neither works (unknown machine)."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    try:
        return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError, AttributeError):
        return 0


def measure(path=None):
    """{"ram_total", "cpu_count", "disk_free", "disk_total"} in bytes.

    Cached after the first call: the machine does not change mid-run.
    Disk is measured at `path` (default: the home dir, which holds
    the data dir). Never raises: an unreadable machine reads as 0s.
    """
    global _cache
    if _cache is not None:
        return dict(_cache)
    ram = _ram_total_bytes()
    cpus = os.cpu_count() or 1
    try:
        usage = shutil.disk_usage(path or os.path.expanduser("~"))
        free, total = usage.free, usage.total
    except OSError:
        free = total = 0
    _cache = {"ram_total": ram, "cpu_count": cpus,
              "disk_free": free, "disk_total": total}
    return dict(_cache)


def _reset_cache():
    """Drop the cached measurement (tests only)."""
    global _cache
    _cache = None


def ghidra_heap(ram_total=None):
    """The Ghidra headless heap for this machine ("2G" on 8 GB): a
    quarter of RAM, clamped to 1G-4G, so a small machine still runs
    and a big one does not starve the rest of the system. An unknown
    machine gets the documented 2G default."""
    if ram_total is None:
        ram_total = measure()["ram_total"]
    if ram_total <= 0:
        return "2G"
    gigs = max(1, min(4, ram_total // _GB // 4))
    return f"{gigs}G"


def flows_cap(ram_total=None):
    """The per-host capture cap for this machine: 5000 on 4-16 GB,
    2500 below 4 GB, 10000 above 16 GB. An unknown machine gets 5000."""
    if ram_total is None:
        ram_total = measure()["ram_total"]
    if ram_total <= 0:
        return 5000
    if ram_total < 4 * _GB:
        return 2500
    if ram_total > 16 * _GB:
        return 10000
    return 5000


def max_workers(cpu_count=None):
    """Background-thread budget for this machine: 2 on a multi-core
    machine (the proxy poller + one task worker), 1 on a single core."""
    if cpu_count is None:
        cpu_count = measure()["cpu_count"]
    return max(1, min(2, cpu_count))


def settings():
    """Measurements + derived settings, one dict."""
    m = measure()
    return {
        "ram_total": m["ram_total"],
        "cpu_count": m["cpu_count"],
        "disk_free": m["disk_free"],
        "disk_total": m["disk_total"],
        "ghidra_heap": ghidra_heap(m["ram_total"]),
        "flows_cap": flows_cap(m["ram_total"]),
        "max_workers": max_workers(m["cpu_count"]),
    }


def _fmt(nbytes):
    """8.0 GB / 512 MB / 0 B, plain words for Doctor + the log."""
    if nbytes >= _GB:
        return f"{nbytes / _GB:.1f} GB"
    if nbytes >= 1024 * 1024:
        return f"{nbytes / (1024 * 1024):.0f} MB"
    return f"{nbytes} B"


def describe():
    """Plain-words lines for Doctor: facts about this PC, no settings
    shown (U35: the user watches measurements, the backend adjusts)."""
    m = measure()
    return [
        f"this PC: {_fmt(m['ram_total'])} RAM, "
        f"{m['cpu_count']} CPU(s), {_fmt(m['disk_free'])} disk free",
    ]


def log_measurements():
    """Log the measured specs + derived settings (startup, once)."""
    from . import log

    s = settings()
    log.log_info(
        "machine",
        f"RAM {_fmt(s['ram_total'])}, {s['cpu_count']} CPU(s), "
        f"disk free {_fmt(s['disk_free'])}; "
        f"Ghidra heap {s['ghidra_heap']}, "
        f"capture cap {s['flows_cap']} flows/host, "
        f"workers {s['max_workers']}")
