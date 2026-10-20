"""Bounded owner-cookie HTTPS streaming trial; no media download or token daemon."""
import asyncio
from http.cookiejar import MozillaCookieJar
import os
from pathlib import Path
import tempfile
import threading
import time
import math
import json
import signal
import shutil
import sys
from urllib.parse import urlsplit

_SLOTS = threading.BoundedSemaphore(1)
_RATES = {}
_PAGES = {}
_LOCK = threading.RLock()
_BYTES = 0.0
_COOLDOWN_UNTIL = 0.0
_UNKNOWN_SECONDS = 0.0
_ASYNC_SLOT = asyncio.Semaphore(1)


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
    def __init__(self):
        self.challenge_failed=False
        self.cookies_expired=False
        self.runtime_failure=None
    def debug(self, message):
        text=str(message).casefold()
        if any(w in text for w in ('out of memory','memory limit','allocation failed')):
            self.runtime_failure='js_heap_limit'
        elif any(w in text for w in ('stack overflow','stack limit')):
            self.runtime_failure='js_stack_limit'
        elif 'quickjs' in text and any(w in text for w in ('not found','not available','unsupported')):
            self.runtime_failure='js_runtime_missing'
        elif any(w in text for w in ('solver lib script','solver core script','ejs package')) and any(w in text for w in ('failed','not found','missing')):
            self.runtime_failure='js_solver_missing' 
    def warning(self, message):
        self.debug(message)
        text=str(message).casefold()
        if any(word in text for word in ('signature','nsig','challenge','javascript')):
            self.challenge_failed=True
        if 'cookie' in text and any(word in text for word in ('no longer valid','expired','invalid')):
            self.cookies_expired=True
    def error(self, message): pass


def resolve(query, cancel=None, cookiefile='/etc/secrets/cookies.txt', check=None,
            cookie_work_dir=None, duration_limit=600):
    global _COOLDOWN_UNTIL
    from yt_dlp import YoutubeDL
    from player_visuals import save_stream_headers, register_youtube_stream
    if time.monotonic() < _COOLDOWN_UNTIL:
        raise RuntimeError('YouTube trial cooling down')
    from youtube_resources import memory_snapshot, over_budget
    if over_budget(memory_snapshot(),starting=True):
        raise RuntimeError('YouTube trial deferred for memory')
    if not _SLOTS.acquire(blocking=False):
        raise RuntimeError('YouTube trial busy')
    logger=QuietLogger()
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
            opts = dict(cookiefile=destination, format='bestaudio/best',
                        skip_download=True, noplaylist=True, playlist_items='1',
                        quiet=True, verbose=True, logger=logger, no_warnings=False,
                        socket_timeout=8, retries=0, extractor_retries=0, cachedir=False)
            # Restore the default authenticated clients used by the successful
            # format18 test. Audio-only is not a playback prerequisite.
            runtime = Path(__file__).resolve().parent / 'runtime_bin' / 'qjs'
            opts['js_runtimes'] = {'quickjs': {'path': str(runtime)}}
            target = query if query.startswith('https://') else 'ytsearch1:'+query
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(target, download=False)
            if cancel is not None and cancel.is_set(): raise InterruptedError()
            if info and info.get('entries') is not None:
                info = next((e for e in info['entries'] if e), None)
            if (not info or not media_url(info.get('url')) or info.get('is_live')
                    or info.get('is_drm') or info.get('has_drm')):
                raise RuntimeError('No usable public YouTube stream')
            if check and not query.startswith('https://') and not check(
                    str(info.get('title') or ''), query, str(info.get('uploader') or '')):
                raise RuntimeError('YouTube result does not match requested recording')
            seconds = float(info.get('duration') or 0)
            if not 0 < seconds <= duration_limit: raise RuntimeError('Track exceeds trial duration limit')
            url = info['url']
            register_youtube_stream(url)
            save_stream_headers(url, info.get('http_headers') or {})
            title = str(info.get('title') or query)[:85]+' · YouTube (live)'
            # tbr includes VIDEO for muxed format18. Never report audio bitrate as total.
            rate = info.get('tbr')
            if not rate and info.get('filesize'):
                rate = float(info['filesize'])*8/seconds/1000
            with _LOCK:
                if len(_RATES) >= 128:
                    oldest=next(iter(_RATES));_RATES.pop(oldest);_PAGES.pop(oldest,None)
                rate = float(rate) if rate else 0
                _RATES[url] = rate if math.isfinite(rate) and rate > 0 else None
                _PAGES[url] = info.get('webpage_url') or target
            return url, title, seconds
    except InterruptedError: raise
    except Exception as exc:
        _COOLDOWN_UNTIL = time.monotonic()+120
        # yt-dlp exceptions may contain signed URLs; do not propagate them.
        message=str(exc).casefold()
        reason=(logger.runtime_failure if logger.runtime_failure else
                'cookies_expired' if logger.cookies_expired else
                'challenge_unavailable' if logger.challenge_failed else
                'format_unavailable' if 'requested format' in message else
                'verification_required' if 'confirm' in message or 'sign in' in message else
                'http_403' if '403' in message else
                'duration_limit' if 'duration limit' in message else 'unavailable')
        raise RuntimeError('youtube_'+reason) from None
    finally:
        _SLOTS.release()


async def resolve_bounded(query, check=None):
    """Kill the whole extraction process group on timeout, memory pressure or cancellation."""
    global _COOLDOWN_UNTIL
    from youtube_resources import memory_snapshot, over_budget
    from player_visuals import register_youtube_stream, save_stream_headers
    from song_links import remember_link
    if _ASYNC_SLOT.locked() or time.monotonic() < _COOLDOWN_UNTIL:
        raise RuntimeError('YouTube trial deferred')
    async with _ASYNC_SLOT:
        with tempfile.TemporaryDirectory(prefix='mudyar_youtube_job_') as folder:
            env={k:v for k,v in os.environ.items() if k in {
                'PATH','LD_LIBRARY_PATH','SSL_CERT_FILE','SSL_CERT_DIR','LANG','LC_ALL',
                'NODE_EXTRA_CA_CERTS','REQUESTS_CA_BUNDLE','SYSTEMROOT',
                'HTTP_PROXY','HTTPS_PROXY','NO_PROXY','http_proxy','https_proxy','no_proxy'}}
            env['TMPDIR']=folder
            cmd=[sys.executable,str(Path(__file__).resolve()),'--resolve',query,'--work-dir',folder]
            if shutil.which('nice'):cmd=[shutil.which('nice'),'-n','15',*cmd]
            proc=await asyncio.create_subprocess_exec(*cmd,env=env,
                stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,start_new_session=True)
            output=asyncio.create_task(proc.communicate())
            deadline=time.monotonic()+55
            try:
                while not output.done():
                    if over_budget(memory_snapshot()):
                        raise RuntimeError('YouTube extraction stopped for memory')
                    if time.monotonic()>=deadline:
                        raise RuntimeError('YouTube extraction timed out')
                    await asyncio.wait({output},timeout=.25)
                raw,_=await output
                if proc.returncode or len(raw)>32768:raise RuntimeError('YouTube worker unavailable')
                row=json.loads(raw)
                url,title,seconds=row['source'],row['title'],float(row['seconds'])
                if not media_url(url) or not 0<seconds<=600 or not isinstance(title,str):
                    raise RuntimeError('Invalid YouTube stream result')
                if check and not query.startswith('https://') and not check(
                        title.rsplit(' · YouTube',1)[0],query,''):
                    raise RuntimeError('YouTube result does not match request')
                register_youtube_stream(url);save_stream_headers(url,row.get('headers') or {})
                rate=float(row.get('rate') or 0)
                with _LOCK:
                    if len(_RATES)>=128:_RATES.pop(next(iter(_RATES)))
                    _RATES[url]=rate if math.isfinite(rate) and rate>0 else None
                remember_link(title,row.get('webpage') or query)
                return url,title[:110],seconds
            except asyncio.CancelledError:
                raise
            except Exception:
                _COOLDOWN_UNTIL=time.monotonic()+120
                raise RuntimeError('YouTube direct trial unavailable') from None
            finally:
                if proc.returncode is None:
                    try:os.killpg(proc.pid,signal.SIGKILL)
                    except ProcessLookupError:pass
                await asyncio.gather(output,return_exceptions=True)


async def race(existing, song, prefer_stream, cleanup, check=None):
    """Race free text against existing sources; direct YouTube links select YouTube."""
    yt = asyncio.create_task(resolve_bounded(song.source, check=check))
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


if __name__ == '__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--resolve',required=True)
    parser.add_argument('--work-dir',required=True);args=parser.parse_args()
    try:
        source,title,seconds=resolve(args.resolve,cookie_work_dir=args.work_dir)
        from player_visuals import stream_headers_for
        print(json.dumps(dict(source=source,title=title,seconds=seconds,
                              headers=stream_headers_for(source),rate=_RATES.get(source),
                              webpage=_PAGES.get(source))))
    except Exception:
        print('{}');sys.exit(1)
