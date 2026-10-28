"""Short real PCM readiness for concurrent music sources.

Source lookup is parallel, but at most two speculative FFmpeg decoders may
run on the 512MiB Render instance. URLs alone are not playable proof.
Never log command lines, URLs, tokens or FFmpeg stderr.
"""
import asyncio
import logging
import time
import weakref
from pathlib import Path

from playback_prepare import decoder_slots
from player_visuals import allowed_audio_cdn, stream_headers_for
from nubcoders_audio import is_nubcoders_stream
from youtube_relay import source_id as relay_source_id, vapters_url

LOG = logging.getLogger('telegram_music')
_CANDIDATE_SLOTS = weakref.WeakKeyDictionary()


def candidate_decoder_slots():
    """At most one speculative music decoder; all metadata searches stay parallel."""
    loop = asyncio.get_running_loop()
    if loop not in _CANDIDATE_SLOTS:
        _CANDIDATE_SLOTS[loop] = asyncio.Semaphore(1)
    return _CANDIDATE_SLOTS[loop]


async def _short_pcm(source, *, headers=None, timeout=6):
    command = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'quiet',
               '-threads', '1', '-rw_timeout', '5000000']
    if headers:
        combined = ''.join(f'{key}: {value}\r\n' for key, value in headers.items())
        if combined:
            command += ['-headers', combined]
    command += ['-i', source, '-t', '0.15', '-vn', '-f', 's16le',
                '-ar', '16000', '-ac', '1', 'pipe:1']
    process = None
    async with decoder_slots(), candidate_decoder_slots():
        try:
            process = await asyncio.create_subprocess_exec(
                *command, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL)
            try:
                pcm, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
            except TimeoutError:
                if process.returncode is None:
                    process.kill()
                    await process.communicate()
                return False
            return process.returncode == 0 and len(pcm) >= 1024
        except asyncio.CancelledError:
            if process is not None and process.returncode is None:
                process.kill()
                await process.communicate()
            raise
        except (OSError, ValueError):
            return False


async def verify_vapters_audio(marker, *, timeout=6):
    """Validate the *actual HTTP audio*, not only the resolved YouTube title."""
    video_id = relay_source_id(marker)
    if video_id is None:
        return False
    started = time.monotonic()
    ok = await _short_pcm(vapters_url(video_id), timeout=timeout)
    LOG.info('Vapters PCM readiness=%s latency=%.2fs', ok, time.monotonic()-started)
    return ok


async def verify_candidate_audio(result, *, timeout=6):
    """Gate all regular audio contenders by a short, decodable audio frame."""
    if not isinstance(result, (tuple, list)) or len(result) < 2:
        return False
    source = result[0]
    if not isinstance(source, str) or not source:
        return False
    if is_nubcoders_stream(source):
        # NubCoders caller already proved PCM before publishing this signed URL.
        return True
    if relay_source_id(source):
        # Vapters caller must prove PCM before publishing this marker.
        return True
    if allowed_audio_cdn(source):
        return await _short_pcm(source, headers=stream_headers_for(source), timeout=timeout)
    # File transfers are owned by the bot and must exist locally. Never
    # interpret another URL scheme as a filesystem candidate.
    if len(source) > 4096 or '://' in source:
        return False
    try:
        path = Path(source)
        if not path.is_file() or not path.stat().st_size:
            return False
    except (OSError, ValueError):
        return False
    return await _short_pcm(str(path), timeout=timeout)
