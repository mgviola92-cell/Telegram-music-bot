"""Bounded memory guard for Render's container quota.

Only real cgroup memory.current/memory.max can authorize a pressure action.
Never use total host RAM or guess a quota, and never evict a playing call.
"""
import asyncio
import logging
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


LOG = logging.getLogger("telegram_music")


async def reclaim_oldest_idle(players, calls, welcome, bot, *, leave_timeout=8):
    """Shed one old idle native sender; never touch music, loading or queued tracks."""
    options = sorted(
        (p.idle_started_at, cid) for cid, p in list(players.items())
        if p.idle_mode is not None and p.current is None and not p.queue
        and (p.loading_task is None or p.loading_task.done())
    )
    for _, cid in options:
        player = players[cid]
        async with player.lock:
            if (player.current is not None or player.idle_mode is None or player.queue
                    or (player.loading_task is not None and not player.loading_task.done())):
                continue
            try:
                await asyncio.wait_for(calls.leave_call(cid), timeout=leave_timeout)
            except Exception as exc:
                # The native connection was already absent: clear stale idle state.
                if type(exc).__name__ != "NotInCallError":
                    LOG.info("Idle memory release unavailable chat %s (%s)",
                             cid, type(exc).__name__)
                    continue
            welcome.close_chat(cid)
            player.idle_mode = None
            player.idle_started_at = 0.
            player.started_at = 0.
            player.idle_notice_sent = False
        LOG.warning("Container RAM pressure: released idle VC chat %s to preserve playing calls", cid)
        try:
            await asyncio.wait_for(bot.send_message(
                cid, "🎧 Server Memory ကို ထိန်းဖို့ သီချင်းမဖွင့်ထားတဲ့ VC ကနေ "
                     "ခဏထွက်ထားတယ်။ /play နဲ့ ပြန်ဝင်နိုင်ပါတယ်။"
            ), timeout=4)
        except Exception:
            pass
        return cid
    return None
