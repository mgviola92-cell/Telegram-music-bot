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
from audio_options import EFFECT_LABELS


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


def player_controls(chat_id: int, *, paused: bool = False, loop: bool = False,
                    title: str = "", normalize: bool = False, quality: str = "high",
                    effect: str = "normal", menu: str = "main", queue_items=None) -> InlineKeyboardMarkup:
    """All controls stay on the current player card; mutations are admin-only."""
    def button(label, action):
        return InlineKeyboardButton(label, callback_data=f"mud:{action}:{chat_id}")
    rows = [
        [button("▶️ Resume" if paused else "⏸ Pause", "resume" if paused else "pause"),
         button("⏭ Skip", "skip"), button("⏹ Stop", "stop")],
        [button("🔁 Loop ON" if loop else "🔁 Loop OFF", "loop"), button("📜 Queue", "queue")],
        [button("စာရင်းပြင်မယ်", "qmanage")],
    ]
    if menu=='queue':
        for i,track in enumerate(queue_items or [],1):
            rows.append([button(f'× {i}. '+queue_track_title(track)[:42],'qdel'+track.queue_token)])
        if not queue_items:rows.append([button('စောင့်ဆိုင်းစာရင်း အလွတ်','qmanage')])
    elif menu == "quality":
        rows.append([button(("✓ " if quality == value else "") + label, "q"+value)
                     for label,value in (("High","high"),("Medium","medium"),("Low","low"))])
    elif menu == "effects":
        options=list(EFFECT_LABELS.items())
        for i in range(0,len(options),2):
            rows.append([button(("✓ " if effect==value else "")+label,"fx"+value)
                         for value,label in options[i:i+2]])
    elif menu == "top":
        rows.append([button("🌐 All Groups", "topall"), button("👥 This Group", "topgroup")])
    else:
        rows.extend([
            [button("🔊 Normalize " + ("ON" if normalize else "OFF"), "normalize"),
             button("🎚 Quality · " + quality.title(), "quality")],
            [button("🎛 Effects" + (" · " + EFFECT_LABELS.get(effect,effect.title()) if effect != "normal" else ""), "effects"),
             button("🏆 Top Tracks", "top")],
            [button("Effect MP3 · 96k", "export")]])
    if menu != "main":
        rows.append([button("‹ Back", "back")])
    rows.append([InlineKeyboardButton("🎤 Lyrics", callback_data="lyrics:current")])
    return InlineKeyboardMarkup(rows)


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


def top_tracks_popup(rows, *, global_scope=False):
    lines = ['🏆 ' + ('All Groups' if global_scope else 'This Group')]
    for index, (title, plays) in enumerate(rows[:3], 1):
        lines.append(f'{index}. {_clip_popup(clean_track_title(title), 42)} · {plays}')
    if not rows:
        lines.append('သီချင်းမှတ်တမ်း မရှိသေးဘူး။')
    return _clip_popup('\n'.join(lines), 198)
