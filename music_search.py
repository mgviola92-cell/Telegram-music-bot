"""Metadata-only choices and bounded YouTube-title lookup; no audio downloads."""
import asyncio
import json
import re
import time
import unicodedata
import threading
from dataclasses import dataclass
from urllib.parse import urlsplit, parse_qs
from aiohttp import ClientSession, ClientTimeout
from music_ui import clean_track_title


_BLOCKED = {}
_BLOCK_LOCK = threading.RLock()

def restricted_error(error):
    text = str(error).casefold()
    return any(word in text for word in ('drm protected', 'drm-protected', 'subscriber-only', 'subscriber_only'))

def block_source(url):
    with _BLOCK_LOCK:
        if len(_BLOCKED) >= 128:
            _BLOCKED.pop(next(iter(_BLOCKED)))
        _BLOCKED[url] = time.monotonic() + 1800

def source_blocked(url):
    with _BLOCK_LOCK:
        expiry = _BLOCKED.get(url, 0)
        if expiry > time.monotonic():
            return True
        _BLOCKED.pop(url, None)
        return False


@dataclass
class SearchChoice:
    title: str
    source: str
    duration: float | None = None
    candidate: object = None


def title_key(value):
    normalized = unicodedata.normalize('NFKC', clean_track_title(value)).casefold()
    return ''.join(c for c in normalized if c.isalnum() or unicodedata.category(c).startswith('M'))


def youtube_id(url):
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or '').lower()
        if parsed.scheme not in ('https', 'http') or parsed.username or parsed.password:
            return None
        if host in ('youtu.be', 'www.youtu.be'):
            value = parsed.path.strip('/').split('/')[0]
        elif host in ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com'):
            bits = parsed.path.strip('/').split('/')
            value = parse_qs(parsed.query).get('v', [''])[0] if parsed.path == '/watch' else (
                bits[1] if len(bits) == 2 and bits[0] in ('shorts', 'live', 'embed') else '')
        else:
            return None
        return value if re.fullmatch(r'[\w-]{11}', value, re.ASCII) else None
    except ValueError:
        return None


_YOUTUBE_TITLES = {}

async def youtube_title(url):
    video_id = youtube_id(url)
    if not video_id:
        raise ValueError('Unsupported YouTube video link')
    row = _YOUTUBE_TITLES.get(video_id)
    if row and row[1] > time.monotonic():
        return row[0]
    async with ClientSession(timeout=ClientTimeout(total=8)) as session:
        async with session.get('https://www.youtube.com/oembed', params={
            'url': 'https://www.youtube.com/watch?v=' + video_id, 'format': 'json'},
            allow_redirects=False) as response:
            if response.status != 200:
                raise ValueError('YouTube title unavailable')
            payload = await response.content.read(32769)
            if len(payload) > 32768:
                raise ValueError('Metadata too large')
            data = json.loads(payload)
    title = str(data.get('title') or '').strip()
    if not title:
        raise ValueError('No song title')
    title = clean_track_title(title)[:120]
    if not title:
        raise ValueError('No usable title')
    if len(_YOUTUBE_TITLES) >= 64:
        _YOUTUBE_TITLES.pop(next(iter(_YOUTUBE_TITLES)))
    _YOUTUBE_TITLES[video_id] = title, time.monotonic() + 3600
    return title


def soundcloud_choices(query):
    from yt_dlp import YoutubeDL
    opts = dict(quiet=True, no_warnings=True, extract_flat=True, skip_download=True,
                socket_timeout=6, retries=0)
    with YoutubeDL(opts) as searcher:
        data = searcher.extract_info('scsearch10:' + query, download=False)
    choices = []
    for row in (data or {}).get('entries', []) or []:
        if (not row or row.get('is_drm') or row.get('has_drm')
                or row.get('availability') in ('private','subscriber_only','needs_auth','premium_only')
                or row.get('access') in ('blocked','preview')):
            continue
        link = row.get('webpage_url') or row.get('url')
        if not isinstance(link, str):
            continue
        if source_blocked(link):
            continue
        host = (urlsplit(link).hostname or '').lower() if isinstance(link, str) else ''
        try:
            duration = float(row['duration']) if row.get('duration') is not None else None
        except (TypeError, ValueError):
            continue
        if (host not in ('soundcloud.com', 'www.soundcloud.com', 'm.soundcloud.com')
                or urlsplit(link).scheme != 'https' or urlsplit(link).username or urlsplit(link).password):
            continue
        if duration is not None and (duration <= 0 or duration > 600):
            continue
        title = str(row.get('title') or '').strip()
        artist = str(row.get('artist') or row.get('uploader') or '').strip()
        if title:
            title += (' – ' + artist) if artist and artist.casefold() not in title.casefold() else ''
            choices.append(SearchChoice(clean_track_title(title)[:120], link, duration))
    return choices


async def collect_choices(query, sources=(), *, limit=5, timeout=12, youtube=False):
    async def primary():
        return await asyncio.to_thread(soundcloud_choices, query)
    async def provider(source):
        candidate = await source.find(query)
        return [SearchChoice(clean_track_title(candidate.label), query, candidate.duration, candidate)]
    tasks = [asyncio.create_task(primary())] + [asyncio.create_task(provider(s)) for s in sources]
    if youtube:
        async def relay_choices():
            from youtube_relay import resolve_request, source_id
            track = await resolve_request(query)
            return [SearchChoice(track.title, 'https://www.youtube.com/watch?v=' + source_id(track.source), track.duration)]
        tasks.append(asyncio.create_task(relay_choices()))
    pending = set(tasks)
    choices, seen = [], set()
    try:
        async with asyncio.timeout(timeout):
            while pending and len(choices) < limit:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    try:
                        rows = task.result()
                    except Exception:
                        continue
                    for choice in rows:
                        key = title_key(choice.title)
                        if key and key not in seen:
                            seen.add(key)
                            choices.append(choice)
        return choices[:limit]
    except TimeoutError:
        return choices[:limit]
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class SearchSessions:
    def __init__(self):
        self.items = {}

    def put(self, token, chat, user, choices):
        now = time.monotonic()
        self.items = {k:v for k,v in self.items.items() if v[0] > now}
        if len(self.items) >= 64:
            self.items.pop(next(iter(self.items)))
        self.items[token] = (now + 180, chat, user, choices)

    def select(self, token, index, chat, user):
        row = self.items.get(token)
        if not row or row[0] < time.monotonic() or row[1:3] != (chat, user) or not 0 <= index < len(row[3]):
            return None
        self.items.pop(token, None)
        return row[3][index]


def related_query(title):
    parts = re.split(r'\s+[–—]\s+', clean_track_title(title))
    return (parts[-1] if len(parts) > 1 else parts[0])[:120]


def choose_related(choices, recent):
    seen = set(recent)
    return next((c for c in choices if title_key(c.title) not in seen), None)
