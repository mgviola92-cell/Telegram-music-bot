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
JOIN_DELAY_SECONDS = 5.0
PARTY_BATCH_COOLDOWN = 20.0
VOICES = {'male':'my-MM-ThihaNeural','female':'my-MM-NilarNeural'}


def safe_name(name):
    name = ''.join(c for c in unicodedata.normalize('NFKC',name or '')
                   if c.isalnum() or unicodedata.category(c).startswith('M') or c == ' ')
    return re.sub(r'\s+',' ',name).strip()[:32]


def greeting_text(name, kind='vc', style='normal'):
    name = safe_name(name)
    if style == 'party_batch':
        return 'အခုဝင်လာကြတဲ့ သူငယ်ချင်းအားလုံး မင်္ဂလာပါ။ လူတောင်းပွဲလေးကနေ ကြိုဆိုပါတယ်။'
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
        current = memoryview(b'')
        chunk = memoryview(b'')
        while not self.stop.is_set():
            if not chunk:
                if not current:
                    try:
                        current = memoryview(self.pending.get_nowait())
                    except queue.Empty:
                        pass
                chunk, current = (current[:chunk_size],current[chunk_size:]) if current else (memoryview(silence),memoryview(b''))
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
    def __init__(self,settings=None,bot=None):
        self.settings = settings
        self.recordings = None
        if bot is not None:
            from greeting_recordings import RecordingCache
            self.recordings = RecordingCache(bot)
        self.modes = {}
        self.voices = {}
        self.styles = {}
        self.chat_tasks = {}
        self.arrivals = {}
        self.batch_tasks = {}
        self.batch_events = {}
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

    def voice(self,chat):
        default=self.settings.data['voice'] if self.settings and self.settings.ready else 'male'
        return self.voices.get(chat,default)

    def render(self,name,kind='vc',style='normal'):
        if self.settings and self.settings.ready:
            key=style if style in {'party','party_batch'} else kind
            return self.settings.text(key,safe_name(name) or 'သူငယ်ချင်း')
        return greeting_text(name,kind,style)

    def mode(self, chat):
        return self.modes.get(chat,self.default)

    def close_chat(self, chat):
        batch = self.batch_tasks.pop(chat,None)
        if batch:
            batch.cancel()
        self.arrivals.pop(chat,None)
        self.batch_events.pop(chat,None)
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

    async def speech(self,name,kind,style,voice):
        key=style if style in {'party','party_batch'} else kind
        if self.settings and self.settings.ready and self.recordings is not None:
            mode=self.settings.data.get('audio_modes',{}).get(key,'tts');clip=self.settings.data.get('audio',{}).get(key)
            if mode!='tts' and clip:
                try:
                    pcm=await self.recordings.get(clip['file_id'])
                    if mode=='name_clip':
                        intro=await self.pcm('မင်္ဂလာပါ၊ '+(safe_name(name) or 'သူငယ်ချင်း')+'။',voice)
                        pcm=intro+bytes(BYTES_PER_SECOND//5)+pcm
                    if len(pcm)>MAX_SECONDS*BYTES_PER_SECOND:raise ValueError('Greeting too long')
                    return pcm
                except Exception as exc:log.info('Recorded greeting unavailable; using text (%s)',type(exc).__name__)
        return await self.pcm(self.render(name,kind,style),voice)

    async def warmup(self):
        try:
            pcm = await self.pcm(self.render(''),VOICES['male'])
            self.ready = True
            log.info('Burmese welcome TTS ready pcm_bytes=%s voice=%s',len(pcm),VOICES['male'])
        except Exception as exc:
            # Normal music must never wait on or fail because of TTS availability.
            log.warning('Welcome TTS unavailable; normal audio unchanged (%s)',type(exc).__name__)
            if self.settings and self.settings.ready and self.settings.data.get('audio'):self.ready=True
        if self.ready:
            for voice in VOICES.values():
                try:
                    await self.pcm(self.render('',style='party_batch'),voice)
                except Exception as exc:
                    log.info('Party batch prewarm unavailable (%s)',type(exc).__name__)

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

    def request(self,chat,user,name,kind='vc',force=False,*,delay=None,present=None,style=None):
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
        expected_voice = self.voice(chat)
        not_before = now + (max(0,delay) if delay is not None else
                            (JOIN_DELAY_SECONDS if kind == 'vc' and not force else 0))
        async def deliver():
            try:
                voice = VOICES[expected_voice]
                pcm = await asyncio.wait_for(self.speech(name,kind,style or expected_style,voice),18)
                await asyncio.sleep(max(0,not_before-time.monotonic()))
                if (self.pipes.get(chat) is not expected or self.mode(chat) == 'off'
                        or self.styles.get(chat,'normal') != expected_style
                        or self.voice(chat) != expected_voice):
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

    def arrive_party(self,chat,user,participants):
        """Debounce a wave of arrivals; one generic greeting, no per-user RPCs."""
        if (not self.ready or chat not in self.pipes or self.styles.get(chat) != 'party'
                or self.mode(chat) not in {'vc','both'}):
            return False
        now = time.monotonic()
        if now-self.recent.get((chat,user),-1000) < 600:
            return False
        pending = self.arrivals.setdefault(chat,collections.OrderedDict())
        if len(pending) >= 128:
            return False
        pending[user] = now
        self.recent[(chat,user)] = now
        while len(self.recent)>512:
            self.recent.popitem(last=False)
        event = self.batch_events.setdefault(chat,asyncio.Event())
        event.set()
        if chat in self.batch_tasks:
            return True
        expected = self.pipes[chat]
        async def batch():
            try:
                while pending:
                    if (self.pipes.get(chat) is not expected or self.styles.get(chat) != 'party'
                            or self.mode(chat) not in {'vc','both'}):
                        return
                    # Five seconds after the most recent arrival in this wave.
                    deadline = max(max(pending.values())+JOIN_DELAY_SECONDS,
                                   self.last_group.get(chat,-1000)+PARTY_BATCH_COOLDOWN)
                    if chat in self.busy or time.monotonic()<deadline:
                        event.clear()
                        try:
                            await asyncio.wait_for(event.wait(),.1 if chat in self.busy else
                                                   max(.01,min(.25,deadline-time.monotonic())))
                        except asyncio.TimeoutError:
                            pass
                        continue
                    eligible = {u for u,t in pending.items()
                                if time.monotonic()-t >= JOIN_DELAY_SECONDS}
                    if not eligible:
                        continue
                    # One participant snapshot checks the entire batch at once.
                    present = set(await asyncio.wait_for(participants(),4))
                    for u in eligible:
                        pending.pop(u,None)
                    if not eligible & present:
                        continue
                    async def still_here(members=frozenset(eligible)):
                        return bool(members & set(await participants()))
                    if self.request(chat,0,'',force=True,delay=0,present=still_here,style='party_batch'):
                        log.info('Party welcome batch queued chat=%s arrivals=%s',chat,len(eligible & present))
            except Exception as exc:
                log.info('Party arrival batch skipped chat=%s (%s)',chat,type(exc).__name__)
            finally:
                if self.batch_tasks.get(chat) is asyncio.current_task():
                    self.batch_tasks.pop(chat,None)
                    self.batch_events.pop(chat,None)
                    self.arrivals.pop(chat,None)
        task = asyncio.create_task(batch())
        self.batch_tasks[chat] = task
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return True

    async def close(self):
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)
        for chat in list(self.pipes):
            self.close_chat(chat)
