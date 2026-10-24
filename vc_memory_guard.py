"""Bounded memory guard for Render's container quota.

Only real cgroup memory.current/memory.max can authorize a pressure action.
Never use total host RAM or guess a quota, and never evict a playing call.
"""
from dataclasses import dataclass
from pathlib import Path

_MIB = 1024 * 1024


@dataclass(frozen=True)
class ContainerMemory:
    used: int
    limit: int

    @property
    def ratio(self):
        return self.used / self.limit


def _integer(path):
    try:
        value = Path(path).read_text(encoding="ascii").strip()
        if value == "max":
            return None
        number = int(value)
        return number if number >= 0 else None
    except (OSError, ValueError):
        return None


def container_memory():
    """Return actual bounded cgroup usage, or None on unknown/unlimited hosts."""
    paths = (
        ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.max"),
        ("/sys/fs/cgroup/memory/memory.usage_in_bytes",
         "/sys/fs/cgroup/memory/memory.limit_in_bytes"),
    )
    for usage_file, limit_file in paths:
        used = _integer(usage_file)
        limit = _integer(limit_file)
        if used is not None and limit is not None and 64 * _MIB <= limit < 1 << 40:
            return ContainerMemory(used, limit)
    return None


def allow_prefetch(snapshot=None):
    """Reserve >=20% memory for real VC decoders rather than speculative work."""
    snapshot = container_memory() if snapshot is None else snapshot
    return snapshot is None or snapshot.ratio < 0.80


def need_idle_reclamation(snapshot=None):
    """Reclaim only *idle* native calls before a 512MiB OOM."""
    snapshot = container_memory() if snapshot is None else snapshot
    return snapshot is not None and snapshot.ratio >= 0.84
