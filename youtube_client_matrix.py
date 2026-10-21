"""Expiring, owner-authorized Render diagnostic; never resolves live requests.

Run isolated children sequentially, kill their whole group when music starts,
and keep temporary dependencies/cookie copies outside the bot environment.
"""
import argparse
import asyncio
from contextlib import contextmanager
from http.cookiejar import MozillaCookieJar
import json
import logging
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

LOG = logging.getLogger('telegram_music')
# This diagnostic release is deliberately inert after this fixed UTC deadline.
PROBE_UNTIL = 1792579200  # 2026-10-21 10:40 UTC
PROBE_MARKER = Path('/tmp/mudyar_client_matrix_extended_20261021.started')
CASES = (
    ('yt-dlp', 'web_embedded', True),
    ('yt-dlp', 'tv', True),
    ('pytubefix', 'ANDROID_VR', False),
    ('yt-dlp', 'android', False),
    ('yt-dlp', 'android_vr', False),
)
VIDEOS = ('nfs8NYg7yQM',)
FIELDS = {'adapter', 'client', 'video_id', 'result', 'error', 'cookie_result',
          'cookies_used', 'audio_only', 'audio_formats', 'format_id',
          'extract_seconds', 'decode_seconds', 'decoded_pcm_bytes', 'seconds',
          'media_status', 'media_kind', 'ffmpeg_error', 'version', 'last_stage',
          'diagnostic_note'}
RESULTS = {'decoded_audio', 'decode_failed', 'extract_failed', 'no_formats',
           'cookie_unavailable', 'probe_timeout', 'aborted_for_playback',
           'probe_process_failed', 'dependency_unavailable', 'setup_ok',
           'setup_failed'}


def busy(players):
    return any(p.current is not None or p.idle_mode == 'radio' or p.skip_pending
               or (p.loading_task is not None and not p.loading_task.done())
               for p in players.values())


def error_kind(error):
    text = str(error).lower()
    for terms, label in ((('confirm', 'bot'), 'verification_required'),
                         (('403',), 'http_403'), (('429',), 'http_429'),
                         (('timed out',), 'timeout'),
                         (('certificate',), 'tls_error'),
                         (('drm',), 'drm'),
                         (('requested format is not available',), 'no_formats'),
                         (('innertube_context',), 'missing_player_context'),
                         (('reload',), 'reload_required'),
                         (('failed to extract any player response',), 'no_player_response'),
                         (('invalid data',), 'invalid_media'),
                         (('resolve',), 'network')):
        if all(term in text for term in terms):
            return label
    return 'extractor_error'


@contextmanager
def cookie_copy(source, folder):
    """Copy only unexpired YouTube cookies; never modify the Render secret."""
    jar = MozillaCookieJar(str(source))
    try:
        jar.load(ignore_discard=True, ignore_expires=False)
    except (OSError, ValueError):
        yield None, 'missing_or_invalid'
        return
    scoped = [c for c in jar if c.domain.lstrip('.') == 'youtube.com'
              or c.domain.endswith('.youtube.com')]
    if not scoped:
        yield None, 'no_unexpired_youtube_cookies'
        return
    with tempfile.TemporaryDirectory(prefix='cookies_', dir=folder) as name:
        path = Path(name) / 'cookies.txt'
        output = MozillaCookieJar(str(path))
        for cookie in scoped:
            output.set_cookie(cookie)
        output.save(ignore_discard=True)
        path.chmod(0o600)
        yield str(path), 'available'


def header_block(headers):
    # Only extractor-selected playback headers, never blanket account headers.
    allowed = {'user-agent', 'accept', 'accept-language', 'origin', 'referer',
               'accept-encoding'}
    safe = {str(k): str(v) for k, v in headers.items()
            if str(k).lower() in allowed
            and '\r' not in str(v) and '\n' not in str(v)}
    safe.setdefault('Referer', 'https://www.youtube.com/')
    safe.setdefault('Origin', 'https://www.youtube.com')
    return ''.join(f'{k}: {v}\r\n' for k, v in safe.items())


def decode_media(url, headers):
    """Decode one second directly with FFmpeg; do not upload to any chat."""
    command = ['ffmpeg', '-nostdin', '-v', 'error', '-rw_timeout', '6000000',
               '-tls_verify', '1']
    ca = os.environ.get('SSL_CERT_FILE')
    if ca:
        command += ['-ca_file', ca]
    command += ['-headers', header_block(headers), '-i', url, '-t', '1', '-vn',
                '-threads', '1', '-filter_threads', '1', '-ac', '1', '-ar',
                '16000', '-f', 's16le', 'pipe:1']
    started = time.monotonic()
    try:
        output = subprocess.run(command, capture_output=True, timeout=16)
        row = {'decoded_pcm_bytes': len(output.stdout),
               'result': 'decoded_audio' if output.returncode == 0
               and len(output.stdout) >= 32000 else 'decode_failed'}
        if row['result'] != 'decoded_audio':
            row['ffmpeg_error'] = error_kind(output.stderr.decode(errors='replace'))
    except subprocess.TimeoutExpired:
        row = {'result': 'decode_failed', 'ffmpeg_error': 'timeout',
               'decoded_pcm_bytes': 0}
    row['decode_seconds'] = round(time.monotonic() - started, 3)
    if row['result'] == 'decoded_audio':
        return row
    # A small failure-only fetch distinguishes HTML/access errors from audio.
    # URL, headers and response body are deliberately never returned to logs.
    try:
        request_headers = dict(headers)
        request_headers['Range'] = 'bytes=0-4095'
        with urllib.request.urlopen(urllib.request.Request(url, headers=request_headers),
                                    timeout=5) as response:
            body = response.read(4096)
            row['media_status'] = response.status
            row['media_kind'] = ('site_unavailable' if b'Site Unavailable' in body
                                 else 'html' if b'<html' in body.lower()
                                 else 'binary')
    except urllib.error.HTTPError as error:
        row['media_status'] = error.code
    except Exception:
        row['media_kind'] = 'unreadable'
    return row


def extract_ytdlp(client, video_id, cookie_file, profile):
    from yt_dlp import YoutubeDL
    from yt_dlp.version import __version__

    class Silent:
        def debug(self, message):
            text = str(message).lower()
            for phrase, label in (('downloading webpage', 'webpage'),
                                  ('player api json', 'player_api'),
                                  ('downloading player', 'player_js'),
                                  ('solving js challenges', 'js_challenge'),
                                  ('downloading m3u8', 'hls_manifest')):
                if phrase in text:
                    profile['last_stage'] = label
                    break
        def warning(self, message):
            text = str(message).lower()
            for phrase, label in (('po token', 'po_token_warning'),
                                  ('challenge', 'js_challenge_warning'),
                                  ('does not support cookies', 'cookies_unsupported'),
                                  ('drm', 'drm_warning')):
                if phrase in text:
                    profile['diagnostic_note'] = label
        def error(self, *args): pass

    options = dict(quiet=True, logger=Silent(), noplaylist=True, skip_download=True,
                   format='bestaudio/best', socket_timeout=20, retries=0,
                   extractor_retries=0, cachedir=False,
                   extractor_args={'youtube': {'player_client': [client]}})
    if cookie_file:
        options['cookiefile'] = cookie_file
    try:
        import deno
        options['js_runtimes'] = {'deno': {'path': deno.find_deno_bin()}}
    except ImportError:
        pass
    with YoutubeDL(options) as downloader:
        info = downloader.extract_info('https://www.youtube.com/watch?v=' + video_id,
                                       download=False)
    return info.get('url'), info.get('http_headers', {}), {
        'version': __version__, 'format_id': str(info.get('format_id', 'unknown')),
        'audio_only': info.get('vcodec') == 'none',
        'audio_formats': sum(f.get('vcodec') == 'none'
                             and f.get('acodec') not in (None, 'none')
                             for f in info.get('formats', []))}


def extract_pytubefix(client, video_id, profile):
    from pytubefix import YouTube, __version__, request
    from pytubefix.innertube import _default_clients
    youtube = YouTube('https://www.youtube.com/watch?v=' + video_id, client=client,
                      use_oauth=False, allow_oauth_cache=False)
    original = request._execute_request
    def measured(url, *args, **kwargs):
        path = urlsplit(url).path
        profile['last_stage'] = ('player_api' if '/youtubei/' in path
                                 else 'player_js' if '/s/player/' in path
                                 else 'webpage')
        return original(url, *args, **kwargs)
    request._execute_request = measured
    try:
        streams = youtube.streams
    finally:
        request._execute_request = original
    stream = streams.get_audio_only()
    if stream is None:
        raise ValueError('requested format is not available')
    return stream.url, dict(_default_clients[client]['header']), {
        'version': __version__, 'format_id': str(stream.itag), 'audio_only': True,
        'audio_formats': len(streams.filter(only_audio=True))}


def probe(adapter, client, video_id, folder, cookie_file=None):
    import socket
    socket.setdefaulttimeout(20)
    started = time.monotonic()
    row = {'adapter': adapter, 'client': client, 'video_id': video_id,
           'cookies_used': bool(cookie_file), 'result': 'extract_failed'}
    profile = {}
    try:
        if adapter == 'yt-dlp':
            url, headers, metadata = extract_ytdlp(client, video_id, cookie_file, profile)
        else:
            url, headers, metadata = extract_pytubefix(client, video_id, profile)
        row.update(metadata, extract_seconds=round(time.monotonic() - started, 3))
        if not isinstance(url, str) or not url.startswith('https://'):
            row['result'] = 'no_formats'
        else:
            row.update(decode_media(url, headers))
    except ImportError:
        row.update(result='dependency_unavailable', error='dependency_unavailable')
    except Exception as error:
        row['error'] = error_kind(error)
    row.update(profile)
    row['seconds'] = round(time.monotonic() - started, 3)
    return row


async def guarded_process(command, players, timeout, env=None):
    nice = shutil.which('nice')
    if nice:
        command = [nice, '-n', '15', *command]
    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        env=env, start_new_session=True)
    communication = asyncio.create_task(process.communicate())
    deadline = time.monotonic() + timeout
    try:
        while not communication.done():
            if busy(players):
                return None, 'aborted_for_playback'
            if time.monotonic() >= deadline:
                return None, 'probe_timeout'
            await asyncio.wait({communication}, timeout=0.1)
        output, _ = await communication
        return output if process.returncode == 0 else None, 'completed'
    finally:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await asyncio.gather(communication, return_exceptions=True)


async def run_client_matrix(players, until=None):
    until = PROBE_UNTIL if until is None else until
    if not 0 < until - time.time() <= 10800:
        return
    # Do not repeat within this process, including a second accidental call.
    try:
        descriptor = os.open(PROBE_MARKER, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
    except FileExistsError:
        return
    await asyncio.sleep(10)
    while busy(players) and time.time() < until:
        await asyncio.sleep(3)
    if time.time() >= until:
        LOG.info('YouTube client matrix: expired_waiting_for_idle')
        return
    LOG.info('YouTube client matrix: starting sequential idle-only probes')
    with tempfile.TemporaryDirectory(prefix='mudyar_client_matrix_') as folder:
        dependencies = Path(folder) / 'dependencies'
        installed = False
        for adapter, client, cookies in CASES:
            if time.time() >= until or busy(players):
                LOG.info('YouTube client matrix: stopped_for_playback_or_expiry')
                return
            if adapter == 'pytubefix' and not installed:
                # --no-deps keeps installed bot packages completely unchanged.
                command = [sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check',
                           '--no-input', '--no-cache-dir', '--no-deps', '--only-binary=:all:',
                           '--target', str(dependencies), 'pytubefix==11.2.1',
                           'nodejs-wheel-binaries==24.19.0']
                output, status = await guarded_process(command, players,
                                                      min(100, until - time.time()))
                installed = output is not None
                LOG.info('YouTube client matrix: pytubefix temporary setup %s',
                         'setup_ok' if installed else status if status != 'completed' else 'setup_failed')
                if status == 'aborted_for_playback':
                    return
                if not installed:
                    continue
            for video_id in VIDEOS:
                if time.time() >= until or busy(players):
                    LOG.info('YouTube client matrix: stopped_for_playback_or_expiry')
                    return
                command = [sys.executable, str(Path(__file__).resolve()),
                           '--adapter', adapter, '--client', client, '--video-id', video_id,
                           '--workdir', folder]
                if cookies:
                    command += ['--cookie-file', '/etc/secrets/cookies.txt']
                env = dict(os.environ)
                if adapter == 'pytubefix':
                    env['PYTHONPATH'] = str(dependencies)
                LOG.info('YouTube client matrix: attempt %s %s %s cookies=%s',
                         adapter, client, video_id, cookies)
                output, status = await guarded_process(command, players,
                                                      min(90, until - time.time()), env=env)
                row = {'adapter': adapter, 'client': client, 'video_id': video_id,
                       'result': status if status != 'completed' else 'probe_process_failed'}
                if output:
                    try:
                        candidate = json.loads(output)
                        if candidate.get('result') in RESULTS:
                            row = {k: v for k, v in candidate.items() if k in FIELDS
                                   and isinstance(v, (str, int, float, bool))}
                    except (ValueError, AttributeError):
                        pass
                LOG.info('YouTube client matrix result: %s', json.dumps(row, sort_keys=True))
                if status == 'aborted_for_playback':
                    return
                await asyncio.sleep(2)
    LOG.info('YouTube client matrix: complete; temporary dependencies and cookies removed')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--adapter', choices=('yt-dlp', 'pytubefix'), required=True)
    parser.add_argument('--client', choices=tuple(case[1] for case in CASES), required=True)
    parser.add_argument('--video-id', choices=VIDEOS, required=True)
    parser.add_argument('--workdir', required=True)
    parser.add_argument('--cookie-file')
    args = parser.parse_args()
    if args.cookie_file:
        with cookie_copy(args.cookie_file, args.workdir) as (copied, status):
            row = probe(args.adapter, args.client, args.video_id, args.workdir, copied) if copied else {
                'adapter': args.adapter, 'client': args.client, 'video_id': args.video_id,
                'result': 'cookie_unavailable'}
            row['cookie_result'] = status
    else:
        row = probe(args.adapter, args.client, args.video_id, args.workdir)
    print(json.dumps(row), flush=True)


if __name__ == '__main__':
    main()
