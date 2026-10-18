"""Owner-allowlisted, low-priority 96 kbps effect files delivered to the clicked group."""
import asyncio
import hashlib
import json
import logging
import math
import os
import re
import shutil
import signal
import tempfile
import time
from dataclasses import dataclass
from html import escape
from pathlib import Path

from pyrogram.enums import ParseMode
from audio_options import AudioSettings, EFFECT_LABELS
from card_layout import DEFAULTS
from music_ui import queue_track_title
from player_visuals import allowed_audio_cdn, stream_headers_for
from runtime_status import cpu_sample, memory_sample

LOG = logging.getLogger('telegram_music')
MAX_INPUT = 20 * 1024 * 1024
MAX_OUTPUT = 12 * 1024 * 1024


def export_key(song, settings):
    stable = (str(song.source).strip().casefold(), queue_track_title(song),
              round(float(song.original_duration_seconds or song.duration_seconds or 0), 1),
              settings.quality, settings.effect, settings.normalize, 'mp3-96k-v1')
    return hashlib.sha256(json.dumps(stable, ensure_ascii=False).encode()).hexdigest()


def export_command(source, output, settings, seconds):
    command = ['ffmpeg','-nostdin','-hide_banner','-v','error','-threads','1']
    if str(source).startswith(('https://','http://')):
        if not allowed_audio_cdn(source):raise ValueError('Unsupported export source')
        headers = ''.join(f'{k}: {v}\r\n' for k,v in stream_headers_for(source).items())
        command += ['-rw_timeout','5000000','-reconnect','1','-reconnect_streamed','1','-reconnect_delay_max','2']
        if headers:command += ['-headers',headers]
    else:
        command += ['-protocol_whitelist','file,pipe']
    command += ['-i',str(source),'-vn','-map','0:a:0','-filter_threads','1']
    chain = settings.filters()
    if chain:command += ['-af',chain]
    rate = {'high':48000,'medium':32000,'low':24000}[settings.quality]
    channels = settings.parameters.value[1]
    command += ['-ac',str(channels),'-ar',str(rate),'-c:a','libmp3lame','-b:a','96k',
                '-threads','1','-t',str(min(720,seconds/settings.speed+2)),
                '-fs',str(MAX_OUTPUT),'-map_metadata','-1','-y',str(output)]
    nice = shutil.which('nice')
    return [nice,'-n','15',*command] if nice else command


class PlaybackPriority:
    """Sample only while an export is requested; never add a playback poller."""
    def __init__(self, players):
        self.players = players
        self.previous = None

    def busy(self):
        active = 0
        for player in self.players.values():
            task = player.loading_task
            if player.skip_pending or (task is not None and not task.done()):return True
            active += player.current is not None or player.idle_mode == 'radio'
        if active >= 3:return True
        now = time.monotonic()
        try:
            used,capacity = cpu_sample()
            old = self.previous
            self.previous = (now,used,capacity)
            if old and now-old[0] >= .25 and used >= old[1] and capacity == old[2]:
                if (used-old[1])/(now-old[0])/(capacity or 1) > .65:return True
            used_memory,limit = memory_sample()
            if limit and used_memory/limit > .85:return True
        except (OSError,ValueError,TypeError,ZeroDivisionError):
            # Still retain the transition and active-group protections above.
            pass
        return False


@dataclass
class ExportJob:
    key: str
    chat: int
    user: int
    title: str
    settings: AudioSettings
    seconds: float
    source: str | None = None
    folder: Path | None = None


class EffectExports:
    def __init__(self,bot,store,players):
        self.bot,self.store,self.players = bot,store,players
        self.priority = PlaybackPriority(players)
        self.lock = asyncio.Lock()
        self.tasks = set()
        self.jobs = {}
        self.cooldowns = {}

    def protected_paths(self):
        return [str(job.folder) for job in self.jobs.values() if job.folder]

    async def request(self,cb,player):
        user = getattr(getattr(cb,'from_user',None),'id',None)
        if not self.store.can_export(user):
            await cb.answer('Owner ခွင့်ပြုထားတဲ့ User ID တွေပဲ ထုတ်နိုင်တယ်။',show_alert=True);return
        message = cb.message
        song = player.current
        if (not message or message.chat.id >= 0 or player.panel_id != message.id or song is None):
            await cb.answer('ဒီ Music Card သက်တမ်းကုန်ပြီ။',show_alert=True);return
        settings = player.playing_settings  # Actual current audio, not next-track settings.
        if song.video or (settings.effect=='normal' and not settings.normalize):
            await cb.answer('လက်ရှိသီချင်းမှာ effect မပါသေးဘူး။ ရွေးထားတာက နောက်သီချင်းကစ သက်ရောက်မယ်။',show_alert=True);return
        seconds = song.original_duration_seconds or ((song.duration_seconds or 0) * song.output_speed)
        if not math.isfinite(seconds) or not 0 < seconds <= 600:
            await cb.answer('၁၀ မိနစ်အတွင်း သီချင်းကိုပဲ ဖိုင်ထုတ်နိုင်တယ်။',show_alert=True);return
        now = time.monotonic()
        if self.cooldowns.get(user,0) > now:
            await cb.answer('ဖိုင်ထုတ်ခိုင်းပြီးပြီ။ ခဏစောင့်ပါ။',show_alert=True);return
        if len(self.jobs) >= 2:
            await cb.answer('Export အလုပ်ရှိနေတယ်။ ပြီးမှ ပြန်နှိပ်ပါ။',show_alert=True);return
        key = export_key(song,settings)
        job = ExportJob(key,message.chat.id,user,queue_track_title(song)[:120],settings,seconds)
        try:
            if key not in self.store.data.get('effect_exports',{}):
                if shutil.disk_usage(tempfile.gettempdir()).free < 64*1024*1024:
                    raise ValueError('Temporary storage busy')
                source = player.temp_file or song.source
                if str(source).startswith(('https://','http://')):
                    if not allowed_audio_cdn(source):raise ValueError('Unsupported source')
                    job.source = str(source)
                else:
                    path = Path(source)
                    if not path.is_file() or not 0 < path.stat().st_size <= MAX_INPUT:
                        raise ValueError('Playback file unavailable')
                    job.folder = Path(tempfile.mkdtemp(prefix='mudyar_export_'))
                    target = job.folder / ('input'+(path.suffix or '.audio'))
                    # Same-filesystem hardlink pins the input even if /skip or
                    # /stop deletes the playback path. No copy/download or lock.
                    os.link(path,target)
                    job.source = str(target)
        except (OSError,ValueError,TypeError):
            if job.folder:shutil.rmtree(job.folder,ignore_errors=True)
            await cb.answer('ဒီသီချင်းဖိုင်ကို အခု မထုတ်နိုင်သေးဘူး။ နောက်မှ ပြန်စမ်းပါ။',show_alert=True);return
        self.cooldowns[user] = now+60
        if len(self.cooldowns)>128:self.cooldowns.pop(next(iter(self.cooldowns)))
        token = object()
        self.jobs[token] = job
        task = asyncio.create_task(self._run(token,job))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        await cb.answer('96 kbps ဖိုင်ကို ဒီ Group ထဲပို့ပေးမယ်။ သီချင်းဖွင့်တာကို ဦးစားပေးပြီး စောင့်လုပ်မယ်။',show_alert=True)

    async def _wait_for_slot(self,job,timeout=180):
        until = time.monotonic()+timeout
        # Let the CPU sampler get a current short-window baseline first.
        self.priority.busy()
        await asyncio.sleep(.5)
        while self.priority.busy():
            if not self.store.can_export(job.user):raise PermissionError('Export permission removed')
            if time.monotonic()>=until:raise TimeoutError('Playback stays busy')
            await asyncio.sleep(1)

    async def _encode(self,job,output):
        command = export_command(job.source,output,job.settings,job.seconds)
        process = await asyncio.create_subprocess_exec(*command,stdout=asyncio.subprocess.DEVNULL,
                                                     stderr=asyncio.subprocess.DEVNULL)
        waiting = asyncio.create_task(process.wait())
        paused = False
        until = time.monotonic()+180
        try:
            while not waiting.done():
                if not self.store.can_export(job.user):raise PermissionError('Export permission removed')
                if time.monotonic()>=until:raise TimeoutError('Export time limit')
                busy = self.priority.busy()
                if busy != paused and hasattr(signal,'SIGSTOP'):
                    try:
                        process.send_signal(signal.SIGSTOP if busy else signal.SIGCONT)
                        paused = busy
                    except ProcessLookupError:pass
                try:await asyncio.wait_for(asyncio.shield(waiting),timeout=.5)
                except TimeoutError:pass
            if process.returncode or not output.is_file() or not 0<output.stat().st_size<MAX_OUTPUT:
                raise ValueError('Export failed')
        finally:
            if process.returncode is None:
                process.kill()
            await asyncio.gather(waiting,return_exceptions=True)

    async def _send(self,job,audio,duration):
        # Recheck permission even on file_id cache hits and after conversion.
        if not self.store.can_export(job.user):raise PermissionError('Export permission removed')
        brand = self.store.data.get('cards',{}).get('brand',DEFAULTS['brand'])
        label = EFFECT_LABELS.get(job.settings.effect,job.settings.effect)
        if job.settings.normalize:label += ' · Normalize'
        filename = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', job.title)[:80]
        filename += ' - '+job.settings.effect+' 96k.mp3'
        async with asyncio.timeout(45):
            return await self.bot.send_audio(job.chat,audio,title=job.title,file_name=filename,
                duration=max(1,round(duration)),caption=escape(brand)+'\n'+escape(label)+' · 96 kbps',
                parse_mode=ParseMode.HTML)

    async def _cached(self,job):
        cached = self.store.data.get('effect_exports',{}).get(job.key)
        if not cached:return False
        try:
            await self._send(job,cached['file_id'],cached['duration'])
            LOG.info('Effect MP3 delivered using Telegram file_id')
            return True
        except PermissionError:raise
        except Exception as exc:
            if type(exc).__name__ not in {'FileIdInvalid','FileReferenceExpired','FileReferenceEmpty','FileReferenceInvalid','MediaEmpty'}:
                raise
            await self.store.cache_export(job.key,None)
            return False

    async def _run(self,token,job):
        try:
            # Cached files cost no encoding/upload and need not wait behind a
            # CPU-heavy export. The overall request cap still applies.
            if await self._cached(job):return
            async with self.lock:
                if not self.store.can_export(job.user):return
                if await self._cached(job):return
                if not job.source:raise ValueError('Cached export unavailable')
                await self._wait_for_slot(job)
                if not self.store.can_export(job.user):raise PermissionError('Export permission removed')
                if job.folder is None:job.folder=Path(tempfile.mkdtemp(prefix='mudyar_export_'))
                output = job.folder/'effect.mp3'
                await self._encode(job,output)
                await self._wait_for_slot(job,timeout=60)
                duration = min(720,job.seconds/job.settings.speed)
                sent = await self._send(job,str(output),duration)
                media = getattr(sent,'audio',None)
                file_id = getattr(media,'file_id',None)
                if file_id:
                    try:
                        await self.store.cache_export(job.key,{'file_id':file_id,'title':job.title,
                                                              'duration':duration})
                    except Exception as exc:
                        LOG.info('Effect file cache save unavailable (%s)',type(exc).__name__)
                LOG.info('Effect MP3 exported at 96 kbps')
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.info('Effect MP3 unavailable (%s)',type(exc).__name__)
            try:
                await asyncio.wait_for(self.bot.send_message(job.chat,
                    'Effect MP3 မထုတ်နိုင်သေးဘူး။ Server အလုပ်များနေရင် နောက်မှ ပြန်စမ်းပါ။'),10)
            except Exception:pass
        finally:
            self.jobs.pop(token,None)
            if job.folder:await asyncio.to_thread(shutil.rmtree,job.folder,True)

    async def close(self):
        tasks=list(self.tasks)
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
