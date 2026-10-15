"""Bounded FFmpeg-only diagnostic: HTTPS input to a short PCM sample in memory."""
import asyncio
from array import array
import re
import time

from pytgcalls.ffmpeg import build_command
from pytgcalls.types.raw import AudioParameters
from player_visuals import allowed_audio_cdn
from network_audio import hls_command


def safe_error(text: str) -> str:
    text = re.sub(r'https?://[^\s\]\"\']+', '[URL]', text)
    text = re.sub(r'(?i)(policy|signature|token|key-pair-id|authorization|cookie)[=:][^\s]+', r'\1=[REDACTED]', text)
    return re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', text)[-1200:]


async def probe_audio_url(url: str, headers: dict, *, timeout: float = 15, variant: str = "library") -> dict:
    if not allowed_audio_cdn(url):
        raise ValueError('Unapproved audio host')
    command = build_command('ffmpeg', None, url, AudioParameters(48000, 2), headers=headers)
    for index in range(len(command) - 1):
        if command[index] == '-v':
            command[index + 1] = 'error'
    if variant in {"combined_headers", "hls_no_eof"}:
        command = hls_command(command, headers, disable_eof=variant == "hls_no_eof")
    command[-1:-1] = ['-t', '0.5']
    started = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    count, peak = 0, 0
    errors = bytearray()

    async def pcm():
        nonlocal count, peak
        while chunk := await process.stdout.read(4096):
            count += len(chunk)
            samples = array('h')
            samples.frombytes(chunk[:len(chunk) // 2 * 2])
            if samples:
                peak = max(peak, max(abs(v) for v in samples))

    async def stderr():
        while chunk := await process.stderr.read(4096):
            if len(errors) < 16000:
                errors.extend(chunk[:16000 - len(errors)])

    readers = [asyncio.create_task(pcm()), asyncio.create_task(stderr())]
    timed_out = False
    try:
        await asyncio.wait_for(asyncio.shield(process.wait()), timeout=timeout)
    except asyncio.TimeoutError:
        timed_out = True
        process.kill()
        await process.wait()
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    finally:
        await asyncio.gather(*readers, return_exceptions=True)
    return {
        'variant': variant, 'ok': count > 0 and process.returncode == 0,
        'pcm_bytes': count, 'peak': peak, 'timeout': timed_out,
        'exit_code': process.returncode, 'seconds': round(time.monotonic() - started, 2),
        'stderr': safe_error(errors.decode('utf-8', errors='replace')),
    }
