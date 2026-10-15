"""Separate SQLite playback counters, no listener identities or Game Bot DB."""
import asyncio
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from music_ui import clean_track_title, top_tracks_popup
from music_search import title_key


class MusicStats:
    def __init__(self, path):
        self.lock = threading.Lock()
        self.path = path
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=2)
        self.db.execute('CREATE TABLE IF NOT EXISTS music_plays (chat INTEGER, song TEXT, title TEXT, plays INTEGER, last_play REAL, PRIMARY KEY(chat,song))')
        self.db.commit()

    def record(self, chat, title):
        title = clean_track_title(title)[:160]
        key = title_key(title)
        if not key:
            return
        with self.lock:
            self.db.execute('INSERT INTO music_plays VALUES (?,?,?,1,?) ON CONFLICT(chat,song) DO UPDATE SET plays=plays+1,title=excluded.title,last_play=excluded.last_play',
                            (chat, key, title, time.time()))
            # Small metadata table, bounded independently per group and globally.
            self.db.execute('DELETE FROM music_plays WHERE chat=? AND song NOT IN (SELECT song FROM music_plays WHERE chat=? ORDER BY last_play DESC LIMIT 200)', (chat,chat))
            self.db.execute('DELETE FROM music_plays WHERE rowid NOT IN (SELECT rowid FROM music_plays ORDER BY last_play DESC LIMIT 10000)')
            self.db.commit()

    def snapshot(self, chat):
        with self.lock:
            if chat is None:
                rows = self.db.execute('SELECT MAX(title),SUM(plays) AS count FROM music_plays GROUP BY song ORDER BY count DESC,MAX(last_play) DESC LIMIT 10').fetchall()
                total = self.db.execute('SELECT COALESCE(SUM(plays),0),COUNT(DISTINCT song) FROM music_plays').fetchone()
            else:
                rows = self.db.execute('SELECT title,plays FROM music_plays WHERE chat=? ORDER BY plays DESC,last_play DESC LIMIT 10', (chat,)).fetchall()
                total = self.db.execute('SELECT COALESCE(SUM(plays),0),COUNT(*) FROM music_plays WHERE chat=?', (chat,)).fetchone()
        return rows, total

    def close(self):
        self.db.close()


_STORE = None
_STORE_LOCK = threading.Lock()

def store():
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            path = os.getenv('MUSIC_STATS_PATH', '/tmp/mudyar_music_stats.sqlite3')
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            _STORE = MusicStats(path)
        return _STORE

async def record_play(chat, title):
    try:
        await asyncio.to_thread(store().record, chat, title)
    except Exception as exc:
        logging.getLogger('telegram_music').info('Music statistics unavailable (%s)', type(exc).__name__)

async def stats_text(chat, *, global_scope=False):
    rows, (plays, unique) = await asyncio.to_thread(store().snapshot, None if global_scope else chat)
    scope = "All Groups" if global_scope else "This Group"
    text = f'📊 {scope} Music\n▶ ဖွင့်ခဲ့မှု {plays} ကြိမ် · သီချင်း {unique} ပုဒ်\n\n'
    text += '\n'.join(f'{i}. {title[:65]} — {count} ကြိမ်' for i,(title,count) in enumerate(rows,1)) or 'သီချင်းမှတ်တမ်း မရှိသေးဘူး။'
    if not os.getenv('MUSIC_STATS_PATH'):
        text += '\n\nမှတ်တမ်းကို server ပြန်တင်ချိန်မှာ reset ဖြစ်နိုင်တယ်။'
    return text


async def stats_popup(chat, *, global_scope=False):
    rows, _ = await asyncio.to_thread(store().snapshot, None if global_scope else chat)
    return top_tracks_popup(rows, global_scope=global_scope)
