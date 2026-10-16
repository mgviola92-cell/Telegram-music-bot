"""Bounded private song-file requests; group VC playback keeps priority."""
import asyncio
import json
import secrets
import time
from collections import OrderedDict
from pathlib import Path
from pyrogram import filters
from pyrogram.types import InlineKeyboardButton as B, InlineKeyboardMarkup as K
from song_recognition import DeliveredAudioCache, send_recognized_audio
from music_ui import clean_track_title
from player_visuals import lyrics_search_url

async def verify_song_file(path):
    p=Path(path)
    if not p.is_file() or not 0<p.stat().st_size<=20*1024*1024:raise ValueError('Invalid song file')
    proc=await asyncio.create_subprocess_exec('ffprobe','-v','error','-protocol_whitelist','file,pipe',
        '-format_whitelist','mp3,ogg,wav,flac,mov,aac,matroska,webm','-show_entries','format=duration',
        '-of','json',str(p),stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    try:
        output,_=await asyncio.wait_for(proc.communicate(),5)
        duration=float(json.loads(output)['format']['duration'])
        if proc.returncode or not 0<duration<=600:raise ValueError('Song exceeds ten minutes')
        return duration
    finally:
        if proc.returncode is None:proc.kill();await proc.wait()

class PrivateSongs:
    def __init__(self,bot,resolver,players,cleanup):
        self.bot,self.resolver,self.players,self.cleanup=bot,resolver,players,cleanup
        self.cache=DeliveredAudioCache();self.recent=OrderedDict();self.job=None
    def preempt(self):
        if self.job and self.job['stage'] in {'resolve','probe'}:
            self.job['reason']='vc';self.job['task'].cancel()
    def register(self):
        @self.bot.on_callback_query(filters.regex(r'^dms:'),group=-4)
        async def cancel_song(_,cb):
            uid=cb.from_user.id if cb.from_user else None
            j=self.job
            if (not j or not j.get('panel') or uid!=j['user'] or not cb.message or cb.message.chat.id!=uid
                    or cb.message.id!=j['panel'].id or cb.data!='dms:'+j['nonce']):
                await cb.answer('ဒီ request မရှိတော့ဘူး။');return
            await cb.answer('ရပ်နေတယ်…');j['reason']='cancel';j['task'].cancel()
        @self.bot.on_message(filters.private & filters.text,group=1)
        async def private_song(_,msg):
            text=(msg.text or '').strip()
            if text.startswith('/'):
                parts=text.split(maxsplit=1)
                command=parts[0].split('@')[0].lower()
                if command not in {'/play','/p','/song','/music','/search'}:return
                text=parts[1].strip() if len(parts)>1 else ''
            if not text or len(text)>180:
                await msg.reply_text('🎵 /play သီချင်းနာမည် လို့ တောင်းနိုင်တယ်။ ၁၀ မိနစ်အထိ MP3 ဖိုင် ပို့ပေးမယ်။');return
            uid=msg.from_user.id if msg.from_user else None
            if not uid:return
            if self.job:
                await msg.reply_text('⏳ သီချင်းတစ်ပုဒ် ပြင်ဆင်နေတယ်။ ခဏနေ ပြန်တောင်းပါ။');return
            now=time.monotonic()
            if now-self.recent.get(uid,-1000)<15:
                await msg.reply_text('⏳ နောက်သီချင်းအတွက် ခဏစောင့်ပါ။');return
            cached=self.cache.get(text.casefold())
            if not cached and any(p.loading_task and not p.loading_task.done() for p in self.players.values()):
                await msg.reply_text('🎧 Group VC သီချင်း ပြင်ဆင်နေတယ်။ ပြီးရင် ပြန်တောင်းပါ။');return
            self.recent[uid]=now
            while len(self.recent)>256:self.recent.popitem(last=False)
            nonce=secrets.token_hex(8)
            job={'user':uid,'task':asyncio.current_task(),'stage':'resolve','reason':'cancel','nonce':nonce,'panel':None}
            self.job=job;path=None;panel=None
            try:
                panel=await msg.reply_text('🔎 သီချင်းရှာနေတယ်…',reply_markup=K([[B('⏹ မတောင်းတော့ဘူး',callback_data='dms:'+nonce)]]))
                job['panel']=panel
                if cached:
                    title,file_id,duration=cached
                    job['stage']='upload'
                    try:
                        await send_recognized_audio(self.bot,msg,None,title,duration,
                            K([[B('🎤 Lyrics',url=lyrics_search_url(title))]]),file_id=file_id)
                        await panel.delete();return
                    except Exception:
                        self.cache.discard(text.casefold());job['stage']='resolve'
                path,title,_=await asyncio.wait_for(self.resolver(text),60)
                job['stage']='probe';duration=await verify_song_file(path)
                title=clean_track_title(title)
                job['stage']='upload';await panel.edit_text('🎵 သီချင်းဖိုင် ပို့နေတယ်…',
                    reply_markup=K([[B('⏹ ရပ်မယ်',callback_data='dms:'+nonce)]]))
                sent=await send_recognized_audio(self.bot,msg,path,title,duration,
                    K([[B('🎤 Lyrics',url=lyrics_search_url(title))]]))
                self.cache.put(text.casefold(),title,sent.audio)
                await panel.delete()
            except asyncio.CancelledError:
                try:await panel.edit_text('🎧 Group VC request ကို ဦးစားပေးဖို့ ခဏရပ်ထားတယ်။ ပြန်တောင်းနိုင်တယ်။' if job['reason']=='vc' else '⏹ သီချင်းတောင်းဆိုမှု ရပ်လိုက်ပြီ။')
                except Exception:pass
            except Exception:
                try:await panel.edit_text('⚠️ ဒီသီချင်းဖိုင်ကို လက်ရှိ မရသေးဘူး။ နာမည်/အဆိုတော်နဲ့ ပြန်တောင်းပါ။')
                except Exception:pass
            finally:
                if path:self.cleanup(path)
                if self.job is job:self.job=None
    async def close(self):
        if self.job:
            task=self.job['task'];self.job['reason']='shutdown';task.cancel()
            await asyncio.gather(task,return_exceptions=True)
