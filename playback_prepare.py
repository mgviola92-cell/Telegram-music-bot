"""Per-track decoder preparation and concurrent, media-free first VC connection."""
import asyncio
import copy
import logging
import json
import shlex
import weakref
import time
from pytgcalls.types import MediaStream, GroupCallConfig
from voice_transport import clear_stale_voice

LOG = logging.getLogger('telegram_music')
_PROBE_SLOTS = weakref.WeakKeyDictionary()


def decoder_slots():
    loop = asyncio.get_running_loop()
    if loop not in _PROBE_SLOTS:
        _PROBE_SLOTS[loop] = asyncio.Semaphore(2)
    return _PROBE_SLOTS[loop]



class CheckedAudioStream(MediaStream):
    """Probe once; give each playback attempt its own mutable capture description.

    WelcomeStream adds a mixer to microphone.path. Reusing the pristine probe
    prevents that mixer (or an effect) being applied twice on reconnect.
    """
    def __init__(self, original):
        self.original = original
        self.probe_task = None

    async def _probe(self):
        from network_audio import DirectAudioStream, hls_command
        from audio_options import ProcessedAudioStream, apply_filters
        from pytgcalls.ffmpeg import build_command
        from pytgcalls.exceptions import NoAudioSourceFound
        original = self.original
        if type(original) not in (MediaStream, DirectAudioStream, ProcessedAudioStream):
            return await original.check_stream()
        source = original._audio_path or original._media_path
        headers = getattr(original, '_direct_headers', None) or original._headers
        probe = build_command('ffprobe', original._ffmpeg_parameters, source,
                              original._audio_parameters, headers=headers)
        if isinstance(original, DirectAudioStream):
            probe = hls_command(probe, headers or {})
        process = await asyncio.create_subprocess_exec(
            *probe, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=8)
        finally:
            if process.returncode is None:
                process.kill()
                await process.communicate()
        metadata = json.loads(output)
        if process.returncode or not any(row.get('codec_type') == 'audio'
                                        for row in metadata.get('streams', [])):
            raise NoAudioSourceFound('Prepared audio')
        command = build_command('ffmpeg', original._ffmpeg_parameters, source,
                                original._audio_parameters, headers=headers,
                                is_livestream='duration' not in metadata.get('format', {}))
        if isinstance(original, DirectAudioStream):
            command = hls_command(command, headers or {})
        settings = getattr(original, 'settings', None)
        if settings is not None:
            command = apply_filters(command, settings)
        original.microphone.path = shlex.join(command)

    async def check_stream(self):
        if self.probe_task is None:
            self.probe_task = asyncio.create_task(self._probe())
        # Let FFprobe finish and reap its process even when /stop cancels a loader.
        await asyncio.shield(self.probe_task)
        for name in ('microphone', 'speaker', 'camera', 'screen'):
            setattr(self, name, copy.copy(getattr(self.original, name, None)))


async def warm_voice(calls, assistant, chat_id, assistant_id, fresh):
    """Join with no media while the first source resolves; never create a VC."""
    started = time.monotonic()
    try:
        if chat_id in await calls.calls:
            return
        if chat_id not in fresh:
            try:
                await asyncio.wait_for(clear_stale_voice(assistant, chat_id, assistant_id), 8)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.info('Early VC stale check unavailable (%s)', type(exc).__name__)
            fresh.add(chat_id)
        # PyTgCalls accepts None as an empty MediaDescription. No decoder,
        # downloaded silence, radio source, or greeting is started here.
        await asyncio.wait_for(calls.play(chat_id, None, GroupCallConfig(auto_start=False)), 8)
        LOG.info('Early VC connection ready chat %s after %.2fs', chat_id, time.monotonic()-started)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Normal playback retains its existing reconnection/fallback behaviour.
        LOG.info('Early VC connection unavailable chat %s (%s)', chat_id, type(exc).__name__)
