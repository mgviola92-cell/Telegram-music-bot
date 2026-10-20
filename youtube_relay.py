"""Opt-in YouTube trial: fixed public relay, POST input, no local media file.

Only a validated YouTube ID reaches the relay. No account cookies, bot token,
assistant session or user identifiers are sent. Normal named /play is unchanged.
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
from dataclasses import dataclass

from pytgcalls.types import MediaStream
from pytgcalls.ffmpeg import build_command
from audio_options import AudioSettings, apply_filters
from music_search import youtube_id, youtube_title

LOG = logging.getLogger('telegram_music')
ENDPOINT = 'https://cold-lemming-3841.alexainc.deno.net/download'
_MARKER = ENDPOINT + '#video_id='
_ID = re.compile(r'[A-Za-z0-9_-]{11}\Z')
_CACHE = {}
STARTUP_PROBE_UNTIL = 1792499169


def enabled():
    return os.getenv('YOUTUBE_RELAY_ENABLED', '1') == '1'


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
    """Cold POST audio may need >4s; keep ordinary streams' original budget.

    WelcomeStream and CheckedAudioStream wrap the actual source. Inspect only
    our fixed marker, with bounded traversal, never arbitrary request URLs.
    """
    for _ in range(4):
        if source_id(getattr(stream, 'relay_source', None)):
            return 16
        stream = getattr(stream, 'original', None)
        if stream is None:
            break
    return 4


def relay_command(command, source):
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
    return result


class RelayAudioStream(MediaStream):
    def __init__(self, source, settings=None):
        if not source_id(source):
            raise ValueError('Invalid relay source')
        self.settings = settings or AudioSettings()
        self.relay_source = source
        super().__init__(source, self.settings.parameters,
                         video_flags=MediaStream.Flags.IGNORE,
                         audio_flags=MediaStream.Flags.REQUIRED)

    async def check_stream(self):
        # MP3 input is verified in the isolated live probe and by native frame
        # checks at playback. A separate FFprobe would POST/download twice.
        command = build_command('ffmpeg', None, ENDPOINT, self._audio_parameters,
                                is_livestream=True)
        command = relay_command(command, self.relay_source)
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


async def resolve_request(query):
    if not enabled():
        raise ValueError('YouTube relay trial is disabled')
    query = str(query).strip()
    if not query or len(query) > 2048:
        raise ValueError('Invalid YouTube request')
    key = youtube_id(query) or query.casefold()
    row = _CACHE.get(key)
    if row and row[0] > time.monotonic():
        return row[1]
    async with asyncio.timeout(14):
        video_id = youtube_id(query)
        if video_id:
            try:
                track = await asyncio.to_thread(_link_metadata, video_id)
            except (LookupError, OSError, json.JSONDecodeError):
                title = await youtube_title(query)
                track = RelayTrack(relay_source(video_id), title + ' · YouTube', None)
        else:
            if query.startswith(('http://', 'https://')) or len(query) > 120:
                raise ValueError('Use a YouTube link or song name')
            track = await asyncio.to_thread(_flat_search, query)
    if len(_CACHE) >= 64:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[key] = time.monotonic() + 600, track
    return track


async def startup_probe(players):
    """One expiring, idle-only Render test; no messages and no VC joins."""
    if not enabled() or not 0 < STARTUP_PROBE_UNTIL - time.time() <= 1800:
        return
    await asyncio.sleep(8)
    def busy():
        return any(p.current is not None or p.idle_mode == 'radio' or p.skip_pending
                   or (p.loading_task and not p.loading_task.done()) for p in players.values())
    if busy():
        LOG.info('YouTube relay probe: skipped_for_playback')
        return
    stream = RelayAudioStream(relay_source('jNQXAC9IVRw'))
    await stream.check_stream()
    command = shlex.split(stream.microphone.path)
    command[command.index('-t') + 1] = '0.5'
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE,
                                              stderr=asyncio.subprocess.DEVNULL)
    result = asyncio.create_task(proc.communicate())
    try:
        while not result.done():
            if busy() or time.monotonic() - started > 22:
                LOG.info('YouTube relay probe: aborted_or_timeout')
                return
            await asyncio.wait({result}, timeout=.25)
        pcm, _ = await result
        LOG.info('YouTube relay probe: %s', json.dumps({'ok': proc.returncode == 0 and bool(pcm),
                 'pcm_bytes': len(pcm), 'nonzero': any(pcm),
                 'seconds': round(time.monotonic() - started, 2)}))
    finally:
        if proc.returncode is None:
            proc.kill()
        await asyncio.gather(result, return_exceptions=True)
