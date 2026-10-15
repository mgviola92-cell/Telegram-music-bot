"""Independent, lightweight queue and inline-player helpers for Mudyar Music.

Inspired by common VC-music bot interaction patterns; implemented from scratch.
No network calls, database, media downloads, or additional dependencies.
"""
import random
import re
from collections import deque
from typing import Any

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from player_visuals import lyrics_search_url


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


def clean_track_title(title):
    title = re.sub(r"\s+·\s+(?:SoundCloud|LyBot|Song_Vkm_bot|VoiceShazamBot|Internet Archive|Audius).*?$", "", str(title), flags=re.I)
    title = re.sub(r"\.(?:mp3|m4a|ogg|opus|wav|flac|aac)\b", "", title, flags=re.I)
    title = re.sub(r"\(?MP3[_ -]?\d+K\)?|\bLYRICS?\s+VIDEO\b|\bOFFICIAL\s+(?:MUSIC\s+)?VIDEO\b", "", title, flags=re.I)
    return re.sub(r"\s+", " ", title).strip(" ·-_—–")[:100]


def queue_track_title(track):
    if not getattr(track, 'metadata_resolved', False) and getattr(track, 'youtube', False):
        return '⚠️ Source ရှာမရသေး' if getattr(track, 'metadata_failed', False) else '🔎 သီချင်းရှာနေဆဲ…'
    return clean_track_title(track.title)


def queue_display(current: str | None, titles: list[str], loop: bool = False) -> str:
    current_line = clean_track_title(current) if current else 'မရှိသေးဘူး'
    rows = "\n".join(f"{i}. {clean_track_title(title)}" for i,title in enumerate(titles,1))
    return ("🎧 မုဒြာ Music Queue\n\n"
            f"▶️ လက်ရှိဖွင့်နေသည်\n{current_line}\n\n"
            f"⏭ နောက်ထပ် ({len(titles)})\n" + (rows or 'Queue အလွတ်') +
            f"\n\n🔁 Repeat current: {'ON' if loop else 'OFF'}")


def player_controls(chat_id: int, *, paused: bool = False, loop: bool = False, title: str = "") -> InlineKeyboardMarkup:
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
        [InlineKeyboardButton("🎤 Lyrics ↗", url=lyrics_search_url(title))],
    ])


def _clip_popup(text, units):
    text = str(text)
    if len(text.encode('utf-16-le')) // 2 <= units:
        return text
    result, used = [], 0
    for char in text:
        size = 2 if ord(char) > 0xFFFF else 1
        if used + size > units - 1:
            break
        result.append(char)
        used += size
    return ''.join(result).rstrip() + '…'


def queue_popup(current, titles, loop=False):
    """Fit whole compact queue rows within Telegram's callback text limit."""
    rows = [f'🎧 Queue · {len(titles)} ပုဒ်',
            '▶ ' + _clip_popup(clean_track_title(current) if current else 'မရှိသေးဘူး', 42)]
    if titles:
        rows.append('')
        limit = 3
        width = 32 if len(titles) >= 3 else 46
        rows.extend(f'{i}. {_clip_popup(clean_track_title(title), width)}'
                    for i,title in enumerate(titles[:limit],1))
        if len(titles) > limit:
            rows.append(f'+{len(titles)-limit} ပုဒ်')
    else:
        rows.append('နောက်ထပ်သီချင်း မရှိသေးဘူး')
    rows.append('🔁 ' + ('ON' if loop else 'OFF'))
    return _clip_popup('\n'.join(rows), 198)
