"""Optional live FFmpeg filters; default capture command stays unchanged."""
import shlex
from dataclasses import dataclass
from pytgcalls.types import AudioQuality, MediaStream

QUALITIES = {'high': AudioQuality.HIGH, 'medium': AudioQuality.MEDIUM, 'low': AudioQuality.LOW}
EFFECT_LABELS = {
    'normal':'Normal', 'nightcore':'Nightcore', 'bassboost':'Bassboost', 'slowed':'Slowed',
    'radio':'📻 Radio', 'echo':'🫧 Echo', 'chorus':'🎶 Chorus', 'tremolo':'🌊 Tremolo',
    'vibrato':'🌀 Vibrato', 'wide':'🎧 Wide Stereo', 'chipmunk':'🐿 Chipmunk', 'tiny':'👾 Tiny Voice',
}
EFFECTS = tuple(EFFECT_LABELS)
SPEEDS = {'nightcore':1.15,'slowed':.85,'chipmunk':1.35,'tiny':1.25}


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
        return SPEEDS.get(self.effect, 1.)

    def filters(self):
        rate = self.parameters.value[0]
        chain = []
        if self.effect in SPEEDS:
            chain += [f'aresample={rate}', f'asetrate={round(rate*self.speed)}', f'aresample={rate}']
            if self.effect == 'slowed':chain += ['aecho=0.8:0.7:60:0.18']
            elif self.effect == 'tiny':chain += ['highpass=f=120','vibrato=f=5:d=0.12']
        elif self.effect == 'bassboost':chain += ['bass=g=6']
        elif self.effect == 'radio':chain += ['highpass=f=300','lowpass=f=3400']
        elif self.effect == 'echo':chain += ['aecho=0.8:0.7:120:0.25']
        elif self.effect == 'chorus':chain += ['chorus=0.6:0.9:50:0.3:0.3:2']
        elif self.effect == 'tremolo':chain += ['tremolo=f=5:d=0.45']
        elif self.effect == 'vibrato':chain += ['vibrato=f=6:d=0.3']
        elif self.effect == 'wide' and self.parameters.value[1]==2:
            chain += ['stereowiden=delay=12:feedback=0.15:crossfeed=0.1:drymix=0.9']
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
