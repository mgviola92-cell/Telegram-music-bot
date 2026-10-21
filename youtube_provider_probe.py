"""Expiring provider comparison. No cookies, sessions, VC joins or audio files."""
import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import sys
import time

LOG = logging.getLogger('telegram_music')
PROBE_UNTIL = 1792582200  # 2026-10-21 11:30 UTC, never a recurring task.
MARKER = Path('/tmp/mudyar_provider_sustained_20261021.started')
VIDEOS = ('nfs8NYg7yQM', 'qtYb7nV4814', '2Vv-BfVoq4g')
PROVIDERS = ('vapters', 'tomatofist', 'legacy_relay')
RUN_PROVIDERS = ('vapters',)
SAMPLE_SECONDS = 10
STREAM_REALTIME = True
FIELDS = {'provider', 'video_id', 'result', 'error', 'first_pcm_seconds',
          'total_seconds', 'pcm_bytes', 'duration_seconds', 'title', 'phase',
          'ffmpeg_returncode', 'sample_seconds'}


def busy(players):
    return any(p.current is not None or p.idle_mode == 'radio' or p.skip_pending
               or (p.loading_task is not None and not p.loading_task.done())
               for p in players.values())


def input_options(provider, video_id):
    if provider not in PROVIDERS or video_id not in VIDEOS:
        raise ValueError('invalid fixed diagnostic case')
    if provider == 'vapters':
        return ['-user_agent', 'MusicSourceProbe/1.0', '-i',
                f'https://vapters.site/stream/{video_id}?vid=false&quality=128']
    if provider == 'tomatofist':
        return ['-user_agent', 'MusicSourceProbe/1.0', '-i',
                f'https://api.tomatofist.com/stream/{video_id}']
    body = json.dumps({'url': 'https://www.youtube.com/watch?v=' + video_id,
                       'type': 'audio'}, separators=(',', ':')).encode().hex()
    return ['-seekable', '0', '-method', 'POST', '-post_data', body,
            '-headers', 'Content-Type: application/json\r\n',
            '-user_agent', 'AlexaMusic/ytdlgo-1.0.0', '-i',
            'https://cold-lemming-3841.alexainc.deno.net/download']


def classify(stderr):
    text = stderr.lower()
    for token, label in ((b'403', 'http_403'), (b'404', 'http_404'),
                          (b'429', 'http_429'), (b'500', 'http_500'),
                          (b'503', 'http_503'), (b'timed out', 'timeout'),
                          (b'certificate', 'tls_error'),
                          (b'failed to resolve', 'dns_error'),
                          (b'invalid data', 'invalid_media')):
        if token in text:
            return label
    return 'no_decoded_audio'


def probe(provider, video_id, timeout=28):
    row = {'provider': provider, 'video_id': video_id, 'result': 'failed',
           'pcm_bytes': 0, 'sample_seconds': SAMPLE_SECONDS}
    command = ['ffmpeg', '-nostdin', '-hide_banner', '-v', 'info', '-rw_timeout',
               '10000000', '-tls_verify', '1']
    if os.environ.get('SSL_CERT_FILE'):
        command += ['-ca_file', os.environ['SSL_CERT_FILE']]
    if STREAM_REALTIME:
        command += ['-re']
    command += input_options(provider, video_id)
    command += ['-t', str(SAMPLE_SECONDS), '-vn', '-threads', '1', '-filter_threads', '1',
                '-ac', '1', '-ar', '16000', '-flush_packets', '1',
                '-f', 's16le', 'pipe:1']
    started = time.monotonic()
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stderr = bytearray()
    selector = selectors.DefaultSelector()
    try:
        for pipe, label in ((process.stdout, 'pcm'), (process.stderr, 'error')):
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, label)
        while selector.get_map():
            if time.monotonic() - started >= timeout:
                row['error'] = 'timeout'
                break
            for key, _ in selector.select(timeout=0.1):
                data = os.read(key.fd, 65536)
                if not data:
                    selector.unregister(key.fileobj)
                elif key.data == 'pcm':
                    if not row['pcm_bytes']:
                        row['first_pcm_seconds'] = round(time.monotonic() - started, 3)
                    row['pcm_bytes'] += len(data)
                elif len(stderr) < 16384:
                    stderr.extend(data[:16384 - len(stderr)])
            if row['pcm_bytes'] > SAMPLE_SECONDS * 32000 * 2:
                row['error'] = 'sample_budget_exceeded'
                break
        if selector.get_map() and process.poll() is None:
            process.kill()
        process.wait(timeout=2)
        row['ffmpeg_returncode'] = process.returncode
        # AAC priming may produce slightly less than the requested PCM duration.
        if process.returncode == 0 and row['pcm_bytes'] >= SAMPLE_SECONDS * 32000 * .95:
            row['result'] = 'decoded_audio'
        else:
            row.setdefault('error', classify(bytes(stderr)))
        # Only bounded media metadata; never log raw FFmpeg errors or URLs.
        text = stderr.decode(errors='replace')
        duration = re.search(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)', text)
        if duration:
            row['duration_seconds'] = round(int(duration[1]) * 3600
                                            + int(duration[2]) * 60 + float(duration[3]), 2)
        title = re.search(r'^\s*title\s*:\s*(.+)$', text, re.MULTILINE | re.IGNORECASE)
        if title and 'http' not in title[1].lower():
            row['title'] = title[1].strip()[:160]
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()
        process.stderr.close()
    row['total_seconds'] = round(time.monotonic() - started, 3)
    return row


async def attempt(provider, video_id, players):
    command = [sys.executable, str(Path(__file__).resolve()),
               '--provider', provider, '--video-id', video_id]
    if shutil.which('nice'):
        command = ['nice', '-n', '15', *command]
    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True)
    communication = asyncio.create_task(process.communicate())
    deadline = time.monotonic() + 35
    row = {'provider': provider, 'video_id': video_id, 'result': 'process_failed'}
    try:
        while not communication.done():
            if busy(players):
                return {**row, 'result': 'aborted_for_playback'}
            if time.monotonic() >= deadline:
                return {**row, 'result': 'timeout'}
            await asyncio.wait({communication}, timeout=0.1)
        output, _ = await communication
        if process.returncode == 0:
            try:
                parsed = json.loads(output)
                if parsed.get('result') in {'failed', 'decoded_audio'}:
                    row = {k: v for k, v in parsed.items() if k in FIELDS
                           and isinstance(v, (str, int, float))}
            except (ValueError, AttributeError):
                pass
        return row
    finally:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await asyncio.gather(communication, return_exceptions=True)


async def run_provider_probe(players, until=None):
    until = PROBE_UNTIL if until is None else until
    if not 0 < until - time.time() <= 10800:
        return
    try:
        descriptor = os.open(MARKER, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
    except FileExistsError:
        return
    await asyncio.sleep(10)
    while busy(players) and time.time() < until:
        await asyncio.sleep(3)
    if time.time() >= until:
        return
    successful = []
    # Test every provider even if an earlier one succeeded. One stream at a time.
    for provider in RUN_PROVIDERS:
        for video_id in VIDEOS if provider != 'legacy_relay' else VIDEOS[:1]:
            if busy(players) or time.time() >= until:
                LOG.info('YouTube provider comparison: stopped_for_playback_or_expiry')
                return
            row = await attempt(provider, video_id, players)
            row['phase'] = 'first'
            LOG.info('YouTube provider comparison result: %s', json.dumps(row, sort_keys=True))
            if row['result'] == 'aborted_for_playback':
                return
            if row['result'] == 'decoded_audio':
                successful.append((provider, video_id))
            await asyncio.sleep(2)
    for provider, video_id in successful:
        if busy(players) or time.time() >= until:
            LOG.info('YouTube provider comparison: stopped_for_playback_or_expiry')
            return
        row = await attempt(provider, video_id, players)
        row['phase'] = 'repeat'
        LOG.info('YouTube provider comparison result: %s', json.dumps(row, sort_keys=True))
        if row['result'] == 'aborted_for_playback':
            return
        await asyncio.sleep(2)
    LOG.info('YouTube provider comparison: complete; no playback source configuration changed')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--provider', choices=PROVIDERS, required=True)
    parser.add_argument('--video-id', choices=VIDEOS, required=True)
    args = parser.parse_args()
    print(json.dumps(probe(args.provider, args.video_id)), flush=True)
