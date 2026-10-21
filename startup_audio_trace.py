"""Bounded startup telemetry from the existing decoder; never read audio twice."""
import asyncio
import copy
import itertools
import logging
import os
from pathlib import Path
import shlex
import tempfile
import time
import weakref
from pytgcalls.types import MediaStream

LOG = logging.getLogger('telegram_music')
_STATES = weakref.WeakKeyDictionary()
_IDS = itertools.count(1)


def source_kind(stream):
    from youtube_relay import source_id
    from nubcoders_audio import is_nubcoders_stream
    from player_visuals import allowed_audio_cdn
    for _ in range(5):
        if is_nubcoders_stream(getattr(stream, 'source_url', None)):
            return 'nubcoders_audio'
        if source_id(getattr(stream, 'relay_source', None)):
            return 'youtube_relay'
        path = getattr(stream, '_audio_path', None) or getattr(stream, '_media_path', None)
        if isinstance(path, str):
            if allowed_audio_cdn(path):
                return 'soundcloud_direct'
            if not path.startswith(('http:', 'https:')) and 'mudyar_media_' in path:
                return 'local_audio'
        stream = getattr(stream, 'original', None)
        if stream is None:
            break
    return None  # Radio, idle, video and unknown inputs keep their original command.


class StartupTrace:
    def __init__(self, chat, kind):
        self.chat, self.kind, self.token = chat, kind, next(_IDS)
        self.created = time.monotonic()
        self.ready = None
        self.first_output = None
        self.folder = None
        self.path = None
        self.task = None
        self.closed = False

    def record(self, stage, **fields):
        LOG.info('Audio startup trace=%s chat=%s source=%s stage=%s elapsed=%.3fs first_output=%s %s',
                 self.token, self.chat, self.kind, stage, time.monotonic()-self.created,
                 round(self.first_output-self.ready, 3) if self.first_output is not None else None,
                 ' '.join(f'{k}={v}' for k,v in fields.items()))

    def command(self, command):
        result = list(command)
        if self.path is None:
            self.folder = Path(tempfile.mkdtemp(prefix='mudyar_startup_trace_'))
            self.path = self.folder / 'progress.txt'
            self.path.touch(mode=0o600)
        # Global telemetry output only. The input, filters and PCM output stay intact.
        result[1:1] = ['-progress', str(self.path)]
        self.ready = time.monotonic()
        self.record('command_ready')
        if self.task is None:
            self.task = asyncio.create_task(self._watch())
        return result

    async def _watch(self):
        offset = 0
        buffer = ''
        try:
            while not self.closed and time.monotonic()-self.ready < 25:
                try:
                    with self.path.open() as handle:
                        handle.seek(offset)
                        text = handle.read(8192)
                        offset = handle.tell()
                    buffer += text
                    lines = buffer.split('\n')
                    buffer = lines.pop()[-1024:]
                    for line in lines:
                        key, _, value = line.partition('=')
                        if key == 'out_time_us':
                            try:
                                positive = int(value) > 0
                            except ValueError:
                                positive = False
                            if positive:
                                self.first_output = time.monotonic()
                                self.record('decoder_output_progress')
                                return
                    if offset > 32768:
                        self.record('progress_limit')
                        return
                except (OSError, UnicodeError):
                    pass
                await asyncio.sleep(.2)
            if not self.closed:
                self.record('no_output_observed')
        except asyncio.CancelledError:
            pass

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.task:
            self.task.cancel()
        if self.path:
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass
        if self.folder:
            try:
                self.folder.rmdir()
            except OSError:
                pass


class TracedAudioStream(MediaStream):
    def __init__(self, original, trace):
        self.__dict__.update(original.__dict__)
        self.original, self.startup_trace = original, trace

    async def check_stream(self):
        await self.original.check_stream()
        # Native descriptions are mutable; never change a prepared stream's command.
        for name in ('microphone', 'speaker', 'camera', 'screen'):
            setattr(self, name, copy.copy(getattr(self.original, name, None)))
        if self.microphone is not None:
            command = shlex.split(self.microphone.path)
            if command and Path(command[0]).name == 'ffmpeg' and '-progress' not in command:
                try:
                    self.microphone.path = shlex.join(self.startup_trace.command(command))
                except OSError as exc:
                    self.startup_trace.record('telemetry_unavailable', error=type(exc).__name__)


def wrap(calls, chat, stream):
    state = _STATES.setdefault(calls, {'count': 0, 'chats': {}})
    previous = state['chats'].pop(chat, None)
    if previous:
        previous.close()
    # One bounded trial per process. At most 12 music starts, no network probe.
    if os.getenv('MUSIC_STARTUP_DIAGNOSTICS', '1') != '1' or state['count'] >= 12:
        return stream, None
    kind = source_kind(stream)
    if kind is None or getattr(stream, 'camera', None) is not None:
        return stream, None
    trace = StartupTrace(chat, kind)
    state['count'] += 1
    state['chats'][chat] = trace
    return TracedAudioStream(stream, trace), trace


def current(calls, chat):
    try:
        return _STATES.get(calls, {}).get('chats', {}).get(chat)
    except TypeError:
        return None  # SimpleNamespace test doubles are not weak-referenceable.


async def close(calls):
    state = _STATES.pop(calls, None)
    if state:
        traces = list(state['chats'].values())
        for trace in traces:
            trace.close()
        await asyncio.gather(*(t.task for t in traces if t.task), return_exceptions=True)
