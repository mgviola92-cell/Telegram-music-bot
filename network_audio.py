"""SoundCloud HLS to the native PCM pipe, without reconnecting at segment EOF."""
import shlex
from pytgcalls.types import AudioQuality, MediaStream
from player_visuals import allowed_audio_cdn, stream_headers_for


def hls_command(command: list[str], headers: dict, *, disable_eof: bool = True) -> list[str]:
    result = []
    index = 0
    while index < len(command):
        option = command[index]
        if option == '-headers':
            index += 2
            continue
        if option == '-reconnect_at_eof' and disable_eof:
            result.extend([option, '0'])
            index += 2
            continue
        result.append(option)
        index += 1
    combined = ''.join(f'{key}: {value}\r\n' for key, value in headers.items())
    if combined:
        position = result.index('-i')
        result[position:position] = ['-headers', combined]
    return result


class DirectAudioStream(MediaStream):
    def __init__(self, url: str):
        if not allowed_audio_cdn(url):
            raise ValueError('Unapproved audio host')
        self._direct_headers = stream_headers_for(url)
        super().__init__(url, AudioQuality.HIGH, video_flags=MediaStream.Flags.IGNORE,
                         audio_flags=MediaStream.Flags.REQUIRED, headers=self._direct_headers)

    async def check_stream(self):
        await super().check_stream()
        # Only FFmpeg capture changes; keep the library's probing/PCM parameters.
        if self.microphone is None:
            raise RuntimeError('Direct audio stream has no microphone source')
        command = hls_command(shlex.split(self.microphone.path), self._direct_headers)
        self.microphone.path = shlex.join(command)
