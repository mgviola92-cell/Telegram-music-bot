"""Independent, lightweight queue and inline-player helpers for Mudyar Music.

Inspired by common VC-music bot interaction patterns; implemented from scratch.
No network calls, database, media downloads, or additional dependencies.
"""
import random
from collections import deque
from typing import Any

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def queue_remove(items: deque, position: int) -> Any:
    """Remove the 1-based NEXT-queue position; don't touch the playing song."""
    if position < 1 or position > len(items):
        raise IndexError("No song at that queue position")
    entries = list(items)
    removed = entries.pop(position - 1)
    items.clear()
    items.extend(entries)
    return removed


def queue_shuffle(items: deque) -> int:
    """Shuffle upcoming entries only. Currently playing track is untouched."""
    entries = list(items)
    random.shuffle(entries)
    items.clear()
    items.extend(entries)
    return len(entries)


def queue_display(current: str | None, titles: list[str], loop: bool = False) -> str:
    current_line = (current or "မရှိသေးဘူး")[:110]
    up_next = "\n".join(f"{i}. {title[:90]}" for i, title in enumerate(titles, 1))
    return (
        "🎧 မုဒြာ Music Queue\n"
        f"🎵 Playing: {current_line}\n"
        f"🔁 Repeat current: {'ON' if loop else 'OFF'}\n\n"
        "📜 Up next:\n" + (up_next or "Queue အလွတ်")
    )


def player_controls(chat_id: int, *, paused: bool = False, loop: bool = False) -> InlineKeyboardMarkup:
    """Small, group-bound callback data; mutating actions are admin-checked."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("▶️ Resume" if paused else "⏸ Pause",
                                 callback_data=f"mud:{'resume' if paused else 'pause'}:{chat_id}"),
            InlineKeyboardButton("⏭ Skip", callback_data=f"mud:skip:{chat_id}"),
            InlineKeyboardButton("⏹ Stop", callback_data=f"mud:stop:{chat_id}"),
        ],
        [
            InlineKeyboardButton("🔁 Loop ON" if loop else "🔁 Loop OFF",
                                 callback_data=f"mud:loop:{chat_id}"),
            InlineKeyboardButton("📜 Queue", callback_data=f"mud:queue:{chat_id}"),
        ],
    ])
