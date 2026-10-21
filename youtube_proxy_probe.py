"""One-shot anonymous public-proxy test; removed after collecting results."""
import asyncio
import ipaddress
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

LOG = logging.getLogger('telegram_music')
UNTIL = 1792584000  # 2026-10-21 12:00 UTC
MARKER = '/tmp/mudyar_public_proxy_probe_20261021.once'
PROXIES = ('http://94.158.49.82:3128', 'http://188.119.98.126:9999', 'http://202.159.35.84:9933', 'http://91.187.113.68:8080', 'http://149.40.26.240:8080', 'http://181.209.108.218:999', 'http://98.153.152.141:7070', 'http://176.111.37.5:39811', 'http://201.77.110.129:999', 'http://122.3.87.41:8080', 'http://89.43.133.229:8080', 'http://170.239.205.31:999')


def valid_proxy(value):
    u = urllib.parse.urlsplit(value)
    return (u.scheme == 'http' and not u.username and not u.password
            and not u.path and not u.query and not u.fragment
            and u.port is not None and ipaddress.ip_address(u.hostname).is_global)


def busy(players):
    return any(p.current is not None or p.idle_mode == 'radio' or p.skip_pending
               or (p.loading_task is not None and not p.loading_task.done())
               for p in players.values())


def classify(error):
    text = str(error).lower()
    for needle, label in [('403', 'http_403'), ('407', 'proxy_auth_required'),
                          ('429', 'http_429'), ('502', 'http_502'),
                          ('503', 'http_503'), ('timed out', 'timeout'),
                          ('refused', 'connection_refused'),
                          ('unreachable', 'unreachable'),
                          ('certificate', 'tls_error'),
                          ('confirm', 'youtube_verification')]:
        if needle in text:
            return label
    return type(error).__name__


def probe(index):
    proxy = PROXIES[index]
    if not valid_proxy(proxy):
        raise ValueError('invalid diagnostic endpoint')
    row = {'index': index, 'phase': 'https_connect', 'result': 'failed'}
    started = time.monotonic()
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(
            {'http': proxy, 'https': proxy}))
        req = urllib.request.Request('https://www.youtube.com/robots.txt',
                                    headers={'User-Agent': 'MusicSourceProbe/1.0'})
        with opener.open(req, timeout=5) as response:
            body = response.read(8192)
            if response.status != 200 or b'User-agent' not in body:
                raise ValueError('unexpected_https_response')
        row['https_seconds'] = round(time.monotonic() - started, 3)
        row['phase'] = 'extract'
        import yt_dlp
        options = {'proxy': proxy, 'noplaylist': True, 'quiet': True,
                   'no_warnings': True, 'socket_timeout': 7, 'retries': 0,
                   'extractor_retries': 0, 'skip_download': True,
                   'format': 'bestaudio/best',
                   'extractor_args': {'youtube': {'player_client': ['android_vr']}}}
        # No cookies, OAuth, account headers, or identity data are used.
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info('https://www.youtube.com/watch?v=nfs8NYg7yQM',
                                    download=False)
        url = info.get('url') if info else None
        if not url or urllib.parse.urlsplit(url).scheme != 'https':
            raise ValueError('no_https_media')
        row['extract_seconds'] = round(time.monotonic() - started, 3)
        row['phase'] = 'media_decode'
        # Same proxy for extraction and media. Signed URLs stay inside child.
        headers = ''.join(f'{k}: {v}\r\n' for k, v in info.get('http_headers', {}).items()
                          if '\n' not in k + v and '\r' not in k + v)
        command = ['ffmpeg', '-nostdin', '-v', 'error', '-tls_verify', '1',
                   '-rw_timeout', '5000000', '-http_proxy', proxy]
        if os.environ.get('SSL_CERT_FILE'):
            command += ['-ca_file', os.environ['SSL_CERT_FILE']]
        if headers:
            command += ['-headers', headers]
        command += ['-i', url, '-t', '1', '-vn', '-threads', '1', '-filter_threads',
                    '1', '-ac', '1', '-ar', '16000', '-f', 's16le', 'pipe:1']
        result = subprocess.run(command, capture_output=True, timeout=9)
        row['pcm_bytes'] = len(result.stdout)
        row['ffmpeg_returncode'] = result.returncode
        if result.returncode == 0 and 30400 <= len(result.stdout) <= 64000:
            row['result'] = 'decoded_audio'
        else:
            row['error'] = classify(result.stderr.decode(errors='replace'))
    except Exception as error:
        row['error'] = classify(error)
    row['total_seconds'] = round(time.monotonic() - started, 3)
    return row


async def run_proxy_probe(players):
    if not 0 < UNTIL - time.time() <= 10800:
        return
    try:
        os.close(os.open(MARKER, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except FileExistsError:
        return
    await asyncio.sleep(10)
    for index in range(len(PROXIES)):
        if busy(players) or time.time() >= UNTIL:
            LOG.info('YouTube public proxy probe: stopped_for_playback_or_expiry')
            return
        process = await asyncio.create_subprocess_exec(
            'nice', '-n', '15', sys.executable, str(Path(__file__).resolve()),
            str(index), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
        communication = asyncio.create_task(process.communicate())
        started = time.monotonic()
        row = {'index': index, 'result': 'timeout'}
        try:
            while not communication.done():
                if busy(players):
                    row['result'] = 'aborted_for_playback'
                    break
                if time.monotonic() - started > 35:
                    break
                await asyncio.wait({communication}, timeout=.1)
            if communication.done():
                output, _ = await communication
                if process.returncode == 0:
                    row = json.loads(output)
        finally:
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            await asyncio.gather(communication, return_exceptions=True)
        LOG.info('YouTube public proxy probe result: %s', json.dumps(row, sort_keys=True))
        if row['result'] == 'aborted_for_playback':
            return
    LOG.info('YouTube public proxy probe: complete; no source settings changed')


if __name__ == '__main__':
    print(json.dumps(probe(int(sys.argv[1]))), flush=True)
