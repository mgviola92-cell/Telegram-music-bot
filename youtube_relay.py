"""YouTube metadata and fixed provider streaming, without local media files.

Only a validated YouTube ID reaches the relay. No account cookies, bot token,
assistant session or user identifiers are sent. The original POST relay remains
available by explicit configuration only; Vapters is the default audio backend.
"""
import asyncio
import json
import logging
import math
import os
import re
import shlex
import time
import urllib.request
import unicodedata
from dataclasses import dataclass

from aiohttp import ClientError
from pytgcalls.types import MediaStream
from pytgcalls.ffmpeg import build_command
from audio_options import AudioSettings, apply_filters
from music_search import youtube_id, youtube_title

LOG = logging.getLogger('telegram_music')
ENDPOINT = 'https://cold-lemming-3841.alexainc.deno.net/download'
# Keep the internal ID marker stable for queues, caches and export identities.
_MARKER = ENDPOINT + '#video_id='
VAPTERS_ENDPOINT = 'https://vapters.site/stream/'
_ID = re.compile(r'[A-Za-z0-9_-]{11}\Z')
_CACHE = {}
_ALIASES = {}
_DIRECT_BACKOFF = 0.0
_INFLIGHT = {}
STARTUP_PROBE_UNTIL = 1792507124  # One owner-requested exact-query diagnostic.
VAPTERS_PROBE_UNTIL = 1792594359  # Temporary owner-authorized integration trial.


def enabled():
    return (os.getenv('YOUTUBE_RELAY_ENABLED', '1') == '1'
            and audio_provider() in {'vapters', 'legacy'})


def audio_provider():
    """The legacy relay is parked, never an automatic fallback or parallel job."""
    return os.getenv('YOUTUBE_AUDIO_PROVIDER', 'vapters').strip().lower()


def vapters_url(video_id):
    if not isinstance(video_id, str) or not _ID.fullmatch(video_id):
        raise ValueError('Invalid YouTube ID')
    return VAPTERS_ENDPOINT + video_id + '?vid=false&quality=128'


def source_id(source):
    if not isinstance(source, str) or not source.startswith(_MARKER):
        return None
    value = source[len(_MARKER):]
    return value if _ID.fullmatch(value) else None


def relay_source(video_id):
    if not isinstance(video_id, str) or not _ID.fullmatch(video_id):
        raise ValueError('Invalid YouTube ID')
    return _MARKER + video_id


def sender_startup_timeout(stream):
    """Allow the selected remote provider to start; keep ordinary streams' budget.

    WelcomeStream and CheckedAudioStream wrap the actual source. Inspect only
    our fixed marker, with bounded traversal, never arbitrary request URLs.
    """
    for _ in range(4):
        if source_id(getattr(stream, 'relay_source', None)):
            return 16 if getattr(stream, 'provider', audio_provider()) == 'legacy' else 8
        stream = getattr(stream, 'original', None)
        if stream is None:
            break
    return 4


def relay_command(command, source, *, fast_mp3=None, provider=None):
    """Route both playback and effect exports; never POST to legacy by default."""
    provider = audio_provider() if provider is None else provider
    if provider == 'legacy':
        return _legacy_relay_command(command, source, fast_mp3=fast_mp3)
    if provider != 'vapters':
        raise ValueError('YouTube audio provider is disabled or unknown')
    video_id = source_id(source)
    if not video_id:
        raise ValueError('Invalid YouTube source')
    result = list(command)
    for name in ('-reconnect', '-reconnect_at_eof', '-reconnect_streamed'):
        if name in result:
            result[result.index(name) + 1] = '0'
    index = result.index('-i')
    result[index + 1] = vapters_url(video_id)
    options = ['-threads', '1', '-filter_threads', '1',
               '-rw_timeout', '8000000', '-tls_verify', '1',
               '-user_agent', 'MusicBotAudio/1.0']
    if os.environ.get('SSL_CERT_FILE'):
        options += ['-ca_file', os.environ['SSL_CERT_FILE']]
    result[index:index] = options
    # The provider returns MP3 or AAC/M4A depending on the song. Autodetect;
    # legacy fast_mp3 must not force the wrong container here.
    return result


def _legacy_relay_command(command, source, *, fast_mp3=None):
    """Preserve native PCM/output filters; replace only the HTTP input."""
    video_id = source_id(source)
    if not video_id:
        raise ValueError('Invalid relay source')
    result = list(command)
    for name in ('-reconnect', '-reconnect_at_eof', '-reconnect_streamed'):
        if name in result:
            result[result.index(name) + 1] = '0'
    index = result.index('-i')
    result[index + 1] = ENDPOINT
    payload = json.dumps({'url': 'https://www.youtube.com/watch?v=' + video_id,
                          'type': 'audio'}, separators=(',', ':')).encode().hex()
    # post_data is FFmpeg's binary AVOption: pass the JSON as hex, not shell code.
    result[index:index] = ['-rw_timeout', '15000000', '-seekable', '0',
                           '-method', 'POST', '-post_data', payload,
                           '-headers', 'Content-Type: application/json\r\n',
                           '-user_agent', 'AlexaMusic/ytdlgo-1.0.0']
    use_fast = fast_mp3 if fast_mp3 is not None else os.getenv('YOUTUBE_RELAY_FAST_MP3', '0') == '1'
    if use_fast:
        index = result.index('-i')
        result[index:index] = ['-f', 'mp3', '-probesize', '32768', '-analyzeduration', '100000']
    return result


class RelayAudioStream(MediaStream):
    def __init__(self, source, settings=None, *, fast_mp3=None):
        if not source_id(source):
            raise ValueError('Invalid relay source')
        self.settings = settings or AudioSettings()
        self.relay_source = source
        self.provider = audio_provider()
        if self.provider not in {'vapters', 'legacy'}:
            raise ValueError('YouTube audio provider is disabled or unknown')
        self.fast_mp3 = fast_mp3
        super().__init__(source, self.settings.parameters,
                         video_flags=MediaStream.Flags.IGNORE,
                         audio_flags=MediaStream.Flags.REQUIRED)

    async def check_stream(self):
        # Native frame checks verify playback. Avoid a second FFprobe request
        # which would open another provider stream before the real decoder.
        command = build_command('ffmpeg', None, ENDPOINT, self._audio_parameters,
                                is_livestream=True)
        command = relay_command(command, self.relay_source, fast_mp3=self.fast_mp3,
                                provider=self.provider)
        command[-1:-1] = ['-t', str(600 / self.settings.speed)]
        self.microphone.path = shlex.join(apply_filters(command, self.settings))


@dataclass(frozen=True)
class RelayTrack:
    source: str
    title: str
    duration: float | None


def _duration(value):
    if value is None:
        return None
    value = float(value)
    if not math.isfinite(value) or not 0 < value <= 600:
        raise ValueError('YouTube trial accepts tracks up to ten minutes')
    return value


def _query_key(query):
    return ' '.join(unicodedata.normalize('NFKC', query).split()).casefold()


def _cached(query):
    key = youtube_id(query) or _ALIASES.get(_query_key(query))
    row = _CACHE.get(key)
    if row and row[0] > time.monotonic():
        _CACHE.pop(key)
        _CACHE[key] = row
        return row[1]
    if row:
        _CACHE.pop(key, None)
    return None


def _remember(query, track):
    video_id = source_id(track.source)
    if not video_id:
        raise ValueError('Invalid relay metadata source')
    _CACHE.pop(video_id, None)
    if len(_CACHE) >= 64:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[video_id] = time.monotonic() + 600, track
    if not youtube_id(query):
        key = _query_key(query)
        _ALIASES.pop(key, None)
        if len(_ALIASES) >= 128:
            _ALIASES.pop(next(iter(_ALIASES)))
        _ALIASES[key] = video_id


def _text(node):
    if not isinstance(node, dict):
        return ''
    return str(node.get('simpleText') or ''.join(str(x.get('text', ''))
               for x in node.get('runs', []) if isinstance(x, dict))).strip()


def _search_track(data):
    """Read ordinary video results, excluding ads, live and upcoming entries."""
    def rows(node, depth=0):
        if depth > 32:
            return
        if isinstance(node, dict):
            for name, value in node.items():
                if name.lower().endswith('videorenderer') and isinstance(value, dict):
                    yield value
                elif 'adslot' not in name.lower() and 'promoted' not in name.lower():
                    yield from rows(value, depth + 1)
        elif isinstance(node, list):
            for value in node:
                yield from rows(value, depth + 1)
    for row in rows(data):
        video_id, title = row.get('videoId'), _text(row.get('title'))
        if not isinstance(video_id, str) or not _ID.fullmatch(video_id) or not title:
            continue
        if row.get('upcomingEventData') or row.get('isLive'):
            continue
        clock = _text(row.get('lengthText'))
        if not re.fullmatch(r'\d{1,3}:\d{2}(?::\d{2})?', clock):
            continue  # Live/scheduled results have no finite duration.
        if any(int(part) >= 60 for part in clock.split(':')[1:]):
            continue
        seconds = 0
        for part in clock.split(':'):
            seconds = seconds * 60 + int(part)
        try:
            duration = _duration(seconds)
        except (ValueError, TypeError):
            LOG.info('YouTube search skipped ineligible duration seconds=%s', seconds)
            continue
        return RelayTrack(relay_source(video_id), title[:140] + ' · YouTube', duration)
    raise LookupError('No finite YouTube song result')


def _direct_search(query):
    body = {'context': {'client': {'clientName': 'ANDROID', 'clientVersion': '20.10.38',
            'androidSdkVersion': 30, 'hl': 'en', 'gl': 'US'}}, 'query': query}
    request = urllib.request.Request(
        'https://youtubei.googleapis.com/youtubei/v1/search?prettyPrint=false&fields=contents',
        data=json.dumps(body).encode(), headers={'Content-Type': 'application/json',
        'X-Youtube-Client-Name': '3',
        'User-Agent': 'com.google.android.youtube/20.10.38 (Linux; U; Android 11) gzip'})
    with urllib.request.urlopen(request, timeout=2) as response:
        if 'json' not in response.headers.get('Content-Type', ''):
            raise LookupError('Metadata response was not JSON')
        raw = response.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError('Search metadata too large')
    return _search_track(json.loads(raw))


async def _direct_search_async(query):
    from youtube_metadata import HTTP
    body = {'context': {'client': {'clientName': 'ANDROID', 'clientVersion': '20.10.38',
            'androidSdkVersion': 30, 'hl': 'en', 'gl': 'US'}}, 'query': query}
    data = await HTTP.request('search', body)
    try:
        return _search_track(data)
    except (LookupError, ValueError, TypeError, AttributeError) as exc:
        LOG.info('YouTube metadata parse failed error=%s detail=%s', type(exc).__name__, str(exc)[:160])
        raise


async def _link_metadata_async(video_id):
    from youtube_metadata import HTTP
    body = {'context': {'client': {'clientName': 'ANDROID', 'clientVersion': '20.10.38',
            'androidSdkVersion': 30, 'hl': 'en', 'gl': 'US'}}, 'videoId': video_id}
    details = (await HTTP.request('player', body)).get('videoDetails') or {}
    if details.get('isLiveContent'):
        raise ValueError('Live YouTube is not supported')
    title = str(details.get('title') or '').strip()
    if not title:
        raise LookupError('No public title')
    return RelayTrack(relay_source(video_id), title[:140] + ' · YouTube',
                      _duration(details.get('lengthSeconds')))


def _flat_search(query):
    from yt_dlp import YoutubeDL
    with YoutubeDL(dict(quiet=True, no_warnings=True, extract_flat=True,
                        skip_download=True, socket_timeout=5, retries=0,
                        playlistend=1)) as ydl:
        data = ydl.extract_info('ytsearch1:' + query, download=False)
    entry = next(iter((data or {}).get('entries') or []), None)
    if not entry or not _ID.fullmatch(str(entry.get('id', ''))):
        raise ValueError('No YouTube match')
    return RelayTrack(relay_source(entry['id']), str(entry.get('title') or query)[:140]
                      + ' · YouTube', _duration(entry.get('duration')))


def _link_metadata(video_id):
    body = {'context': {'client': {'clientName': 'ANDROID', 'clientVersion': '20.10.38',
            'androidSdkVersion': 30, 'hl': 'en', 'gl': 'US'}}, 'videoId': video_id}
    request = urllib.request.Request('https://youtubei.googleapis.com/youtubei/v1/player',
        data=json.dumps(body).encode(), headers={'Content-Type': 'application/json',
        'X-Youtube-Client-Name': '3',
        'User-Agent': 'com.google.android.youtube/20.10.38 (Linux; U; Android 11) gzip'})
    with urllib.request.urlopen(request, timeout=5) as response:
        raw = response.read(512 * 1024 + 1)
    if len(raw) > 512 * 1024:
        raise ValueError('Metadata too large')
    details = json.loads(raw).get('videoDetails') or {}
    if details.get('isLiveContent'):
        raise ValueError('Live YouTube is not supported by this trial')
    title = str(details.get('title') or '').strip()
    if not title:
        raise LookupError('No public title')
    return RelayTrack(relay_source(video_id), title[:140] + ' · YouTube',
                      _duration(details.get('lengthSeconds')))


async def _resolve_request(query):
    global _DIRECT_BACKOFF
    if not enabled():
        raise ValueError('YouTube relay trial is disabled')
    query = str(query).strip()
    if not query or len(query) > 2048:
        raise ValueError('Invalid YouTube request')
    cached = _cached(query)
    if cached:
        LOG.info('YouTube relay metadata cache hit')
        return cached
    async with asyncio.timeout(14):
        video_id = youtube_id(query)
        if video_id:
            try:
                track = await _link_metadata_async(video_id)
            except (LookupError, OSError, json.JSONDecodeError, asyncio.TimeoutError, ClientError):
                title = await youtube_title(query)
                track = RelayTrack(relay_source(video_id), title + ' · YouTube', None)
        else:
            if query.startswith(('http://', 'https://')) or len(query) > 120:
                raise ValueError('Use a YouTube link or song name')
            started = time.monotonic()
            method = 'flat_fallback'
            track = None
            if os.getenv('YOUTUBE_RELAY_DIRECT_SEARCH', '1') == '1' and time.monotonic() >= _DIRECT_BACKOFF:
                direct = asyncio.create_task(_direct_search_async(query))
                fallback = None
                try:
                    done, _ = await asyncio.wait({direct}, timeout=.75)
                    if done:
                        try:
                            track = direct.result()
                            method = 'direct_json'
                        except Exception as exc:
                            _DIRECT_BACKOFF = time.monotonic() + 15
                            LOG.info('YouTube direct metadata unavailable (%s); using fallback', type(exc).__name__)
                    if track is None:
                        fallback = asyncio.create_task(asyncio.to_thread(_flat_search, query))
                        pending = {fallback} | ({direct} if not direct.done() else set())
                        last_error = None
                        while pending and track is None:
                            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                            for task in sorted(done, key=lambda t: t is not direct):
                                try:
                                    track = task.result()
                                    method = 'direct_json' if task is direct else 'flat_fallback'
                                    break
                                except Exception as exc:
                                    last_error = exc
                                    LOG.info('YouTube metadata candidate failed method=%s error=%s detail=%s',
                                             'direct_json' if task is direct else 'flat_fallback',
                                             type(exc).__name__, re.sub(r'https?://\S+', '<url>', str(exc))[:200])
                                    if task is direct:
                                        _DIRECT_BACKOFF = time.monotonic() + 15
                            if not pending and track is None:
                                raise last_error or LookupError('No YouTube metadata')
                finally:
                    tasks = [t for t in (direct, fallback) if t is not None]
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
            else:
                track = await asyncio.to_thread(_flat_search, query)
            LOG.info('YouTube metadata lookup method=%s seconds=%.2f', method, time.monotonic() - started)
    _remember(query, track)
    return track


async def resolve_request(query):
    """Share metadata work across search/play without sharing caller cancellation."""
    query = str(query).strip()
    if not enabled() or not query or len(query) > 2048:
        raise ValueError('Invalid or disabled YouTube request')
    cached = _cached(query)
    if cached:
        LOG.info('YouTube relay metadata cache hit')
        return cached
    key = (asyncio.get_running_loop(), youtube_id(query) or _query_key(query))
    job = _INFLIGHT.get(key)
    if job is None:
        if len(_INFLIGHT) >= 32:
            raise LookupError('YouTube metadata busy')
        job = {'task': asyncio.create_task(_resolve_request(query)), 'users': 0}
        _INFLIGHT[key] = job
    else:
        LOG.info('YouTube metadata joined existing lookup')
    job['users'] += 1
    try:
        return await asyncio.shield(job['task'])
    finally:
        job['users'] -= 1
        if not job['users']:
            if _INFLIGHT.get(key) is job:
                _INFLIGHT.pop(key, None)
            task = job['task']
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def _busy(players):
    return any(p.current is not None or p.idle_mode == 'radio' or p.skip_pending
               or (p.loading_task and not p.loading_task.done()) for p in players.values())


async def _probe_profile(players, fast_mp3, video_id='nfs8NYg7yQM'):
    stream = RelayAudioStream(relay_source(video_id), fast_mp3=fast_mp3)
    await stream.check_stream()
    command = shlex.split(stream.microphone.path)
    command[command.index('-t') + 1] = '5'
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE,
                                              stderr=asyncio.subprocess.DEVNULL)
    async def capture():
        head = await proc.stdout.read(4096)
        first_audio = time.monotonic() - started
        tail = await proc.stdout.read()
        await proc.wait()
        return head + tail, first_audio
    result = asyncio.create_task(capture())
    try:
        while not result.done():
            if _busy(players) or time.monotonic() - started > 22:
                LOG.info('YouTube relay probe: aborted_or_timeout')
                return False
            await asyncio.wait({result}, timeout=.25)
        pcm, first_audio = await result
        LOG.info('YouTube relay probe: %s', json.dumps({'ok': proc.returncode == 0 and bool(pcm),
                 'provider': stream.provider, 'video_id': video_id,
                 'pcm_bytes': len(pcm), 'nonzero': any(pcm),
                 'profile': 'mp3_tuned' if fast_mp3 else 'baseline',
                 'first_audio_seconds': round(first_audio, 2),
                 'seconds': round(time.monotonic() - started, 2)}))
        return proc.returncode == 0 and bool(pcm)
    finally:
        if proc.returncode is None:
            proc.kill()
        await asyncio.gather(result, return_exceptions=True)


async def startup_probe(players):
    """Owner's exact failing query; idle-only, metadata only, no chat or VC."""
    if enabled() and audio_provider() == 'vapters' and 0 < VAPTERS_PROBE_UNTIL - time.time() <= 1800:
        try:
            os.close(os.open('/tmp/mudyar_vapters_integration_20261021.once',
                             os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        except FileExistsError:
            return
        await asyncio.sleep(8)
        for video_id in ('nfs8NYg7yQM', 'qtYb7nV4814', '2Vv-BfVoq4g'):
            if _busy(players) or time.time() >= VAPTERS_PROBE_UNTIL:
                return
            await _probe_profile(players, False, video_id=video_id)
            await asyncio.sleep(2)
        LOG.info('Vapters integrated command probe complete; no VC joins or source changes')
        return
    if not enabled() or not 0 < STARTUP_PROBE_UNTIL - time.time() <= 1800:
        return
    await asyncio.sleep(8)
    query = 'လူသားအသည်းနှစ်လုံးနဲ့ကျနော်'
    for method in ('direct_parser', 'full_metadata_resolver'):
        if _busy(players) or time.time() >= STARTUP_PROBE_UNTIL:
            LOG.info('YouTube metadata comparison: skipped_for_playback')
            return
        started = time.monotonic()
        try:
            track = await (_direct_search_async(query) if method == 'direct_parser' else resolve_request(query))
            LOG.info('YouTube exact-query diagnostic method=%s ok=True seconds=%.3f video_id=%s duration=%s',
                     method, time.monotonic()-started, source_id(track.source), track.duration)
        except Exception as exc:
            LOG.info('YouTube exact-query diagnostic method=%s ok=False seconds=%.3f error=%s detail=%s',
                     method, time.monotonic()-started, type(exc).__name__,
                     re.sub(r'https?://\S+', '<url>', str(exc))[:200])
