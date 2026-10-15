"""Bounded Burmese welcome TTS and optional live ducking; never replaces a song."""
import asyncio
import collections
import logging
import os
import queue
import re
import shlex
import tempfile
import threading
import time
import unicodedata
from pathlib import Path
from pytgcalls.types import MediaStream

log = logging.getLogger('telegram_music')
RATE, CHANNELS = 48000, 2
BYTES_PER_SECOND = RATE * CHANNELS * 2
MAX_SECONDS = 40
JOIN_DELAY_SECONDS = 6.0
VOICES = {'male':'my-MM-ThihaNeural','female':'my-MM-NilarNeural'}


def greeting_text(name, kind='vc', style='normal'):
    name = ''.join(c for c in unicodedata.normalize('NFKC',name or '')
                   if c.isalnum() or unicodedata.category(c).startswith('M') or c == ' ')
    name = re.sub(r'\s+',' ',name).strip()[:32]
    if style == 'party':
        return (f'မင်္ဂလာပါ{("၊ " + name) if name else ""}။ လူတောင်းပွဲလေးကနေ ကြိုဆိုပါတယ်။ '
                'ဒီအသံခန်းလေးထဲမှာ လူဝင်တောင်းလို့ ရပါတယ်။ '
                'ကိုယ်တိုင် ဝင်ပြောချင်ရင်လည်း အက်မင်တွေဆီ စကားပြောခွင့် တောင်းပြီး ဝင်ပြောလို့ ရပါတယ်။ '
                'အားလုံး ပျော်ပျော်ပါးပါး ပါဝင်ကြပါဦး။')
    place = 'အသံခန်းလေးထဲကို' if kind == 'vc' else 'ဂရုလေးထဲကို'
    return f'မင်္ဂလာပါ{("၊ " + name) if name else ""}။ {place} ကြိုဆိုပါတယ်။'


def mixing_command(command, fifo):
    """Keep decoder input/quality/effects intact; mix PCM after input processing."""
    result = list(command)
    chain = 'anull'
    if '-af' in result:
        i = result.index('-af')
        chain = result[i+1]
        del result[i:i+2]
    # Output options (-ar/-ac/-f) must stay after BOTH inputs.
    i = result.index('-i') + 2
    result[i:i] = ['-thread_queue_size','2','-analyzeduration','1','-probesize','32','-f','s16le','-ar',str(RATE),'-ac',str(CHANNELS),
                  '-i',str(fifo)]
    graph = (f'[0:a]{chain}[music];[1:a]asplit=2[voice][key];'
             '[music][key]sidechaincompress=threshold=0.008:ratio=20:attack=5:release=1000[duck];'
             '[duck][voice]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,'
             'alimiter=limit=0.95:level=false[out]')
    # Existing -map 0:a would bypass the mixer, so replace it.
    while '-map' in result:
        j = result.index('-map')
        del result[j:j+2]
    result[-1:-1] = ['-filter_complex',graph,'-map','[out]']
    return result


class GreetingPipe:
    """A private bounded FIFO; silence is generated in 20ms chunks, not stored."""
    def __init__(self):
        self.created = time.monotonic()
        self.folder = Path(tempfile.mkdtemp(prefix='mudyar_welcome_'))
        self.path = self.folder / 'voice.pcm'
        os.mkfifo(self.path,0o600)
        self.fd = os.open(self.path,os.O_RDWR | os.O_NONBLOCK)
        try:
            import fcntl
            fcntl.fcntl(self.fd, fcntl.F_SETPIPE_SZ, 4096)
        except (ImportError, OSError, AttributeError):
            pass
        self.pending = queue.Queue(maxsize=1)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._write,daemon=True,name='welcome-pcm')
        self.thread.start()

    def offer(self, pcm):
        if self.stop.is_set() or not pcm or len(pcm) > MAX_SECONDS*BYTES_PER_SECOND:
            return False
        try:
            self.pending.put_nowait(pcm)
            return True
        except queue.Full:
            return False

    def _write(self):
        chunk_size = BYTES_PER_SECOND // 50
        silence = bytes(chunk_size)
        current = b''
        chunk = b''
        while not self.stop.is_set():
            if not chunk:
                if not current:
                    try:
                        current = self.pending.get_nowait()
                    except queue.Empty:
                        pass
                chunk, current = (current[:chunk_size],current[chunk_size:]) if current else (silence,b'')
            try:
                written = os.write(self.fd,chunk)
                chunk = chunk[written:]
            except BlockingIOError:
                self.stop.wait(.01)
            except OSError:
                break

    def close(self):
        if self.stop.is_set():
            return
        self.stop.set()
        self.thread.join(timeout=.5)
        os.close(self.fd)
        self.path.unlink(missing_ok=True)
        self.folder.rmdir()


class WelcomeStream(MediaStream):
    def __init__(self, original, pipe):
        self.__dict__.update(original.__dict__)
        self.original = original
        self.pipe = pipe

    async def check_stream(self):
        await self.original.check_stream()
        self.__dict__.update(self.original.__dict__)
        if self.microphone is None:
            raise RuntimeError('No welcome mixer microphone')
        self.microphone.path = shlex.join(mixing_command(shlex.split(self.microphone.path),self.pipe.path))


class WelcomeAudio:
    def __init__(self):
        self.modes = {}
        self.voices = {}
        self.styles = {}
        self.chat_tasks = {}
        self.pipes = {}
        self.cache = collections.OrderedDict()
        self.recent = collections.OrderedDict()
        self.last_group = {}
        self.busy = set()
        self.tasks = set()
        self.ready = False
        self.tts_slots = asyncio.Semaphore(1)
        self.default = os.getenv('MUSIC_WELCOME_DEFAULT','vc')
        if self.default not in {'off','vc','group','both'}:
            self.default = 'off'

    def mode(self, chat):
        return self.modes.get(chat,self.default)

    def close_chat(self, chat):
        task = self.chat_tasks.pop(chat,None)
        if task:
            task.cancel()
        self.busy.discard(chat)
        pipe = self.pipes.pop(chat,None)
        if pipe:
            pipe.close()

    async def pcm(self, text, voice):
        key = (text,voice)
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        async with self.tts_slots:
            import edge_tts
            audio = bytearray()
            async def synthesize():
                async for chunk in edge_tts.Communicate(text,voice,rate='-5%').stream():
                    if chunk['type'] == 'audio':
                        audio.extend(chunk['data'])
                        if len(audio) > 512000:
                            raise ValueError('TTS audio too large')
            await asyncio.wait_for(synthesize(),12)
            proc = await asyncio.create_subprocess_exec('ffmpeg','-nostdin','-hide_banner','-loglevel','error',
                '-i','pipe:0','-t',str(MAX_SECONDS+.1),'-ar',str(RATE),'-ac',str(CHANNELS),'-f','s16le','pipe:1',
                stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            try:
                pcm, _ = await asyncio.wait_for(proc.communicate(bytes(audio)),5)
                if len(pcm) > MAX_SECONDS*BYTES_PER_SECOND:
                    raise ValueError('Welcome speech exceeds duration limit')
                if proc.returncode or len(pcm) < BYTES_PER_SECOND//4:
                    raise RuntimeError('No TTS PCM')
            finally:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
            self.cache[key] = pcm
            while len(self.cache) > 16 or sum(map(len,self.cache.values())) > 8*1024*1024:
                self.cache.popitem(last=False)
            return pcm

    async def warmup(self):
        try:
            pcm = await self.pcm(greeting_text(''),VOICES['male'])
            self.ready = True
            log.info('Burmese welcome TTS ready pcm_bytes=%s voice=%s',len(pcm),VOICES['male'])
        except Exception as exc:
            # Normal music must never wait on or fail because of TTS availability.
            log.warning('Welcome TTS unavailable; normal audio unchanged (%s)',type(exc).__name__)

    def prepare(self, chat, stream):
        # No greeting overlay on video (A/V sync) or while TTS service is unavailable.
        if not self.ready or self.mode(chat) == 'off' or getattr(stream,'camera',None) is not None:
            return stream,None
        pipe = GreetingPipe()
        return WelcomeStream(stream,pipe),pipe

    def commit(self,chat,pipe):
        self.close_chat(chat)
        if pipe:
            self.pipes[chat] = pipe

    def request(self,chat,user,name,kind='vc',force=False,*,delay=None,present=None):
        if not self.ready or chat not in self.pipes:
            return False
        if not force and self.mode(chat) not in {kind,'both'}:
            return False
        now = time.monotonic()
        if chat in self.busy or (not force and (now-self.last_group.get(chat,-1000)<20
                            or now-self.recent.get((chat,user),-1000)<600)):
            return False
        self.last_group[chat] = now
        self.recent[(chat,user)] = now
        while len(self.recent) > 512:
            self.recent.popitem(last=False)
        self.busy.add(chat)
        expected = self.pipes[chat]
        expected_style = self.styles.get(chat,'normal')
        expected_voice = self.voices.get(chat,'male')
        not_before = now + (max(0,delay) if delay is not None else
                            (JOIN_DELAY_SECONDS if kind == 'vc' and not force else 0))
        async def deliver():
            try:
                voice = VOICES[expected_voice]
                pcm = await asyncio.wait_for(self.pcm(greeting_text(name,kind,expected_style),voice),18)
                await asyncio.sleep(max(0,not_before-time.monotonic()))
                if (self.pipes.get(chat) is not expected or self.mode(chat) == 'off'
                        or self.styles.get(chat,'normal') != expected_style
                        or self.voices.get(chat,'male') != expected_voice):
                    return
                if present is not None and not await asyncio.wait_for(present(),4):
                    return  # The newcomer already left or playback was paused.
                if expected.offer(pcm):
                    log.info('Welcome queued chat=%s style=%s join_wait=%.2fs speech=%.2fs',
                             chat,expected_style,time.monotonic()-now,len(pcm)/BYTES_PER_SECOND)
                    await asyncio.sleep(len(pcm)/BYTES_PER_SECOND + .3)
            except Exception as exc:
                log.info('Welcome skipped chat=%s (%s)',chat,type(exc).__name__)
            finally:
                if self.chat_tasks.get(chat) is asyncio.current_task():
                    self.chat_tasks.pop(chat,None)
                    self.busy.discard(chat)
        task = asyncio.create_task(deliver())
        self.chat_tasks[chat] = task
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return True

    async def close(self):
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)
        for chat in list(self.pipes):
            self.close_chat(chat)
