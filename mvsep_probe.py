"""Expiring, idle-only technical separation test. Never sends Telegram messages."""
import asyncio
import logging
import os
import tempfile
import time
from pathlib import Path
from karaoke_audio import audio_command, inspect_audio, prepare_result
from mvsep_service import MVSEPSeparator

LOG = logging.getLogger('telegram_music')


def busy(players):
    return any(p.current is not None or p.idle_mode is not None or
               (p.loading_task and not p.loading_task.done()) for p in players.values())


async def run_mvsep_probe(players, until):
    token = os.getenv('MVSEP_API_TOKEN', '').strip()
    if not token:
        LOG.warning('MVSEP probe unavailable: token missing')
        return
    while busy(players) and time.time() < until:
        await asyncio.sleep(5)
    if busy(players) or time.time() >= until:
        LOG.info('MVSEP probe skipped: playback has priority')
        return
    LOG.info('MVSEP probe started: synthetic voice and tones; free-only; no Telegram audio')
    phase='synthetic_audio'
    try:
        with tempfile.TemporaryDirectory(prefix='mudyar_karaoke_probe_') as folder:
            path = Path(folder) / 'synthetic.mp3'
            await audio_command(['ffmpeg', '-nostdin', '-v', 'error', '-threads', '1',
                '-filter_complex_threads', '1', '-f', 'lavfi', '-i',
                'flite=text=This is a synthetic voice for an instrumental separation test:voice=slt',
                '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100:duration=20',
                '-f', 'lavfi', '-i', 'sine=frequency=660:sample_rate=44100:duration=20',
                '-filter_complex', '[0:a]apad,volume=1.5[v];[1:a]volume=0.7[l];[2:a]volume=0.7[r];[l][r]amerge[m];[v][m]amix=inputs=2:normalize=0',
                '-t', '20', '-ac', '2', '-c:a', 'libmp3lame', '-threads', '1', '-b:a', '128k', '-y', str(path)], 15)
            if busy(players):
                LOG.info('MVSEP probe skipped before upload: playback has priority')
                return
            seconds, _ = await inspect_audio(path)
            phase='remote_separation'
            started = time.monotonic()
            async def progress(status):
                nonlocal phase
                phase=status
                LOG.info('MVSEP probe status=%s elapsed_seconds=%.1f', status, time.monotonic()-started)
            task = asyncio.create_task(MVSEPSeparator(token).separate(path, Path(folder)/'remote.mp3', progress))
            try:
                while not task.done():
                    await asyncio.sleep(1)
                    if busy(players):
                        LOG.info('MVSEP probe stopped: playback has priority')
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        return
                raw = await task
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            output = await prepare_result(raw, Path(folder)/'ready.mp3', seconds)
            LOG.info('MVSEP probe PASS input_seconds=%.2f output_bytes=%s elapsed_seconds=%.1f; technical decode only',
                     seconds, Path(output).stat().st_size, time.monotonic()-started)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # No raw exceptions, HTTP response, account details, signed URLs or token.
        from mvsep_service import KaraokeUnavailable
        reason=str(exc) if isinstance(exc,KaraokeUnavailable) else type(exc).__name__
        LOG.warning('MVSEP probe unavailable stage=%s reason=%s',phase,reason)
