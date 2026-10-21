"""Constrained NubCoders source and real short PCM readiness check.

A signed resolver URL is not proof of playable audio. FFmpeg validates a
short sample before the URL may win a race. Never log signed URLs or stderr.
"""
import asyncio
import shlex
from urllib.parse import urlsplit

from pytgcalls.ffmpeg import build_command
from pytgcalls.types import MediaStream
from audio_options import AudioSettings, apply_filters


def is_nubcoders_stream(url):
    if not isinstance(url, str) or len(url) > 4096:
        return False
    try:
        p = urlsplit(url)
        return (p.scheme == "https" and p.hostname == "api.nubcoders.com"
                and p.port in (None, 443) and not p.username and not p.password
                and p.path.startswith("/stream/resolver/"))
    except ValueError:
        return False


class NubCodersAudioStream(MediaStream):
    """Feed the HTTPS signed resolver directly into FFmpeg, not a HEAD probe."""

    def __init__(self, source, settings=None):
        if not is_nubcoders_stream(source):
            raise ValueError("Invalid NubCoders stream source")
        self.source_url = source
        self.settings = settings or AudioSettings()
        super().__init__(source, self.settings.parameters,
                         video_flags=MediaStream.Flags.IGNORE,
                         audio_flags=MediaStream.Flags.REQUIRED)

    async def check_stream(self):
        command = build_command("ffmpeg", None, self.source_url, self._audio_parameters,
                                is_livestream=True)
        index = command.index("-i")
        command[index:index] = ["-rw_timeout", "8000000", "-user_agent", "MudyarMusicBot/1.0"]
        self.microphone.path = shlex.join(apply_filters(command, self.settings))


async def verify_nubcoders_audio(source, *, timeout=7):
    """Prove the first 0.15s decodes to real PCM; bounded, cancellable, no logs."""
    if not is_nubcoders_stream(source):
        return False
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error",
            "-rw_timeout", "6000000", "-i", source, "-t", "0.15", "-vn",
            "-f", "s16le", "-ar", "16000", "-ac", "1", "pipe:1",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
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
