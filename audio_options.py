"""Optional live FFmpeg filters; default capture command stays unchanged."""
import shlex
from dataclasses import dataclass
from pytgcalls.types import AudioQuality, MediaStream

QUALITIES = {'high': AudioQuality.HIGH, 'medium': AudioQuality.MEDIUM, 'low': AudioQuality.LOW}
EFFECTS = ('normal', 'nightcore', 'bassboost', 'slowed')


@dataclass(frozen=True)
class AudioSettings:
    quality: str = 'high'
    normalize: bool = False
    effect: str = 'normal'

    def __post_init__(self):
        if self.quality not in QUALITIES or self.effect not in EFFECTS:
            raise ValueError('Unknown audio setting')

    @property
    def parameters(self):
        return QUALITIES[self.quality]

    @property
    def speed(self):
        return {'nightcore': 1.15, 'slowed': .85}.get(self.effect, 1.)

    def filters(self):
        rate = self.parameters.value[0]
        chain = []
        if self.effect == 'nightcore':
            chain += [f'aresample={rate}', f'asetrate={round(rate*1.15)}', f'aresample={rate}']
        elif self.effect == 'slowed':
            chain += [f'aresample={rate}', f'asetrate={round(rate*.85)}', f'aresample={rate}', 'aecho=0.8:0.7:60:0.18']
        elif self.effect == 'bassboost':
            chain += ['bass=g=6']
        if self.normalize:
            chain += ['loudnorm=I=-16:TP=-1.5:LRA=11', f'aresample={rate}']
        if chain:
            chain += ['alimiter=limit=0.95:level=false']
        return ','.join(chain)


def apply_filters(command, settings):
    result = list(command)
    filters = settings.filters()
    if filters:
        if '-af' in result:
            index = result.index('-af') + 1
            result[index] += ',' + filters
        else:
            # FFmpeg filter is an OUTPUT option, after -i, before PCM stdout.
            result[-1:-1] = ['-af', filters]
    return result


class ProcessedAudioStream(MediaStream):
    def __init__(self, path, settings):
        self.settings = settings
        super().__init__(path, settings.parameters, video_flags=MediaStream.Flags.IGNORE,
                         audio_flags=MediaStream.Flags.REQUIRED)

    async def check_stream(self):
        await super().check_stream()
        if self.microphone is None:
            raise RuntimeError('No audio stream')
        self.microphone.path = shlex.join(apply_filters(shlex.split(self.microphone.path), self.settings))
