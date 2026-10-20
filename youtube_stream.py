"""Bounded owner-cookie HTTPS streaming trial; no media download or token daemon."""
import asyncio
from http.cookiejar import MozillaCookieJar
import os
from pathlib import Path
import tempfile
import threading
import time
import math
from urllib.parse import urlsplit

_SLOTS = threading.BoundedSemaphore(1)
_RATES = {}
_LOCK = threading.RLock()
_BYTES = 0.0
_COOLDOWN_UNTIL = 0.0
_UNKNOWN_SECONDS = 0.0


def enabled():
    return (os.getenv('YOUTUBE_DIRECT_TRIAL', '1') != '0'
            and Path('/etc/secrets/cookies.txt').is_file())


def media_url(url):
    try:
        p = urlsplit(url)
        return (p.scheme == 'https' and p.port in (None, 443) and not p.username
                and not p.password and (p.hostname or '').endswith('.googlevideo.com'))
    except (ValueError, TypeError):
        return False


class QuietLogger:
    def debug(self, message): pass
    def warning(self, message): pass
    def error(self, message): pass


def resolve(query, cancel=None, cookiefile='/etc/secrets/cookies.txt', check=None, cookie_work_dir=None):
    global _COOLDOWN_UNTIL
    from yt_dlp import YoutubeDL
    from player_visuals import save_stream_headers, register_youtube_stream
    from song_links import remember_link
    if time.monotonic() < _COOLDOWN_UNTIL:
        raise RuntimeError('YouTube trial cooling down')
    from runtime_status import memory_sample
    used, limit = memory_sample()
    if limit and used/limit > .75:
        raise RuntimeError('YouTube trial deferred for memory')
    if not _SLOTS.acquire(blocking=False):
        raise RuntimeError('YouTube trial busy')
    try:
        if cancel is not None and cancel.is_set(): raise InterruptedError()
        with tempfile.TemporaryDirectory(prefix='mudyar_youtube_cookie_', dir=cookie_work_dir) as folder:
            original = MozillaCookieJar(cookiefile)
            original.load(ignore_discard=True, ignore_expires=False)
            destination = str(Path(folder)/'cookies.txt')
            jar = MozillaCookieJar(destination)
            for c in original:
                if c.domain.lstrip('.') == 'youtube.com' or c.domain.endswith('.youtube.com'):
                    jar.set_cookie(c)
            if not len(jar): raise RuntimeError('No usable YouTube session')
            jar.save(ignore_discard=True); os.chmod(destination, 0o600)
            opts = dict(cookiefile=destination, format='bestaudio[protocol=https]/18[protocol=https]',
                        skip_download=True, noplaylist=True, playlist_items='1',
                        quiet=True, logger=QuietLogger(), no_warnings=True,
                        socket_timeout=8, retries=0, extractor_retries=0, cachedir=False)
            try:
                import deno
                opts['js_runtimes'] = {'deno': {'path': deno.find_deno_bin()}}
            except ImportError: pass
            target = query if query.startswith('https://') else 'ytsearch1:'+query
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(target, download=False)
            if cancel is not None and cancel.is_set(): raise InterruptedError()
            if info and info.get('entries') is not None:
                info = next((e for e in info['entries'] if e), None)
            if not info or not media_url(info.get('url')) or info.get('is_live') or info.get('is_drm'):
                raise RuntimeError('No usable public YouTube stream')
            if check and not query.startswith('https://') and not check(
                    str(info.get('title') or ''), query, str(info.get('uploader') or '')):
                raise RuntimeError('YouTube result does not match requested recording')
            seconds = float(info.get('duration') or 0)
            if not 0 < seconds <= 600: raise RuntimeError('Track exceeds trial duration limit')
            url = info['url']
            register_youtube_stream(url)
            save_stream_headers(url, info.get('http_headers') or {})
            title = str(info.get('title') or query)[:85]+' · YouTube (live)'
            remember_link(title, info.get('webpage_url') or target)
            # tbr includes VIDEO for muxed format18. Never report audio bitrate as total.
            rate = info.get('tbr')
            if not rate and info.get('filesize'):
                rate = float(info['filesize'])*8/seconds/1000
            with _LOCK:
                if len(_RATES) >= 128: _RATES.pop(next(iter(_RATES)))
                rate = float(rate) if rate else 0
                _RATES[url] = rate if math.isfinite(rate) and rate > 0 else None
            return url, title, seconds
    except InterruptedError: raise
    except Exception:
        _COOLDOWN_UNTIL = time.monotonic()+120
        # yt-dlp exceptions may contain signed URLs; do not propagate them.
        raise RuntimeError('YouTube direct trial unavailable') from None
    finally:
        _SLOTS.release()


async def race(existing, song, prefer_stream, cleanup, check=None):
    """Race free text against existing sources; direct YouTube links select YouTube."""
    cancel = threading.Event()
    yt = asyncio.create_task(asyncio.wait_for(
        asyncio.to_thread(resolve, song.source, cancel, check=check), 50))
    direct = song.source.startswith('https://')
    tasks = {yt} if direct else {yt, asyncio.create_task(existing(song, prefer_stream))}
    all_tasks = set(tasks)
    try:
        while tasks:
            done, tasks = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                try:
                    result = task.result()
                except Exception:
                    if not tasks and all(t.cancelled() or t.exception() for t in done):
                        raise RuntimeError('No playable music source') from None
                    continue
                # Clean a simultaneous losing local result as well as later losers.
                def discard(t):
                    if t.cancelled(): return
                    try:
                        path = t.result()[0]
                        if not path.startswith(('https://', 'http://')): cleanup(path)
                    except BaseException: pass
                for other in all_tasks-{task}:
                    other.add_done_callback(discard)
                return result
        raise RuntimeError('No playable music source')
    finally:
        cancel.set()
        for task in all_tasks:
            if not task.done(): task.cancel()
        await asyncio.gather(*all_tasks, return_exceptions=True)


async def meter(players):
    global _BYTES, _UNKNOWN_SECONDS
    last = time.monotonic()
    while True:
        await asyncio.sleep(2)
        now = time.monotonic(); dt = min(4, now-last); last = now
        with _LOCK:
            for p in list(players.values()):
                if getattr(p, 'current', None) and not getattr(p, 'paused', False):
                    source = getattr(p, 'temp_file', None)
                    if source not in _RATES: continue
                    rate = _RATES[source]
                    if rate:
                        speed = getattr(p.current, 'output_speed', 1.) or 1.
                        _BYTES += rate*1000/8*dt*speed
                    else: _UNKNOWN_SECONDS += dt


def usage_text():
    with _LOCK:
        missing = '; bitrate မသိသည့်အပိုင်း မပါ' if _UNKNOWN_SECONDS else ''
        return (f'▸ YouTube အဝင်ဒေတာ ခန့်မှန်း : {_BYTES/1e6:.1f} MB (ဤ run)\n'
                '  Render outbound quota မဟုတ်'+missing)
