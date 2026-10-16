"""Bounded private song-file requests; group VC playback keeps priority."""
import asyncio
import json
import secrets
import time
from collections import OrderedDict
from pathlib import Path
from pyrogram import filters
from pyrogram.enums import ParseMode
from music_search import collect_choices
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
    def __init__(self,bot,resolver,players,cleanup,searcher=None,lyric_button=None):
        self.bot,self.resolver,self.players,self.cleanup=bot,resolver,players,cleanup
        self.cache=DeliveredAudioCache();self.recent=OrderedDict();self.job=None
        self.lyric_button=lyric_button or (lambda t,d:B("🎤 Lyrics",url=lyrics_search_url(t)))
        self.searcher=searcher or collect_choices;self.sessions=OrderedDict();self.searching=set();self.search_recent=OrderedDict();self.search_tasks={}
    def preempt(self):
        for task in list(self.search_tasks.values()):task.cancel()
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
        @self.bot.on_callback_query(filters.regex(r'^dml:'),group=-4)
        async def search_result(_,cb):
            uid=cb.from_user.id if cb.from_user else None
            try:_,token,action,value=cb.data.split(':');index=int(value)
            except (ValueError,TypeError):await cb.answer();return
            row=self.sessions.get(token)
            if (not row or row['until']<time.monotonic() or row['user']!=uid or not cb.message
                    or cb.message.chat.id!=uid or row['panel'].id!=cb.message.id):
                await cb.answer('ဒီစာရင်း သက်တမ်းကုန်ပြီ။ သီချင်းနာမည် ပြန်ရိုက်ပါ။',show_alert=True);return
            if action=='page':
                if not 0<=index<((len(row['choices'])+4)//5):await cb.answer();return
                await cb.answer()
                try:await self.show_results(row,token,index)
                except Exception:pass
            elif action=='pick' and 0<=index<len(row['choices']):
                if self.job:await cb.answer('သီချင်းဖိုင် ပြင်ဆင်နေတယ်။ ခဏစောင့်ပါ။',show_alert=True);return
                await cb.answer('🎵 သီချင်းဖိုင် ပြင်ဆင်နေတယ်…')
                choice=row['choices'][index]
                from types import SimpleNamespace
                proxy=SimpleNamespace(from_user=cb.from_user,chat=cb.message.chat,id=cb.message.id,
                                      reply_text=cb.message.reply_text)
                await self.deliver(proxy,choice.title,choice)
            else:await cb.answer()

        @self.bot.on_message(filters.private & filters.text,group=1)
        async def private_song(_,msg):
            text=(msg.text or '').strip()
            if text.startswith('/'):
                parts=text.split(maxsplit=1)
                command=parts[0].split('@')[0].lower()
                if command not in {'/play','/p','/song','/music','/search'}:return
                text=parts[1].strip() if len(parts)>1 else ''
            if not text or len(text)>180:
                await msg.reply_text('🔎 သီချင်းနာမည် ရိုက်ပြီး စာရင်းထဲက ရွေးပါ။ ၁၀ မိနစ်အထိ MP3 ဖိုင် ပို့ပေးမယ်။');return
            uid=msg.from_user.id if msg.from_user else None
            if not uid:return
            now=time.monotonic()
            if uid in self.searching or len(self.searching)>=2 or now-self.search_recent.get(uid,-1000)<5:
                await msg.reply_text('⏳ စာရင်းရှာနေတယ်။ ခဏနေ ပြန်ရှာပါ။');return
            if any(p.loading_task and not p.loading_task.done() for p in self.players.values()):
                await msg.reply_text('🎧 Group VC သီချင်း ပြင်ဆင်နေတယ်။ ပြီးရင် ပြန်ရှာပါ။');return
            self.searching.add(uid);self.search_recent[uid]=now
            while len(self.search_recent)>256:self.search_recent.popitem(last=False)
            panel=None
            try:
                panel=await msg.reply_text('🔎 သီချင်းစာရင်း ရှာနေတယ်…')
                task=asyncio.create_task(self.searcher(text));self.search_tasks[uid]=task
                choices=await asyncio.wait_for(task,12)
                if not choices:
                    await panel.edit_text('သီချင်းမတွေ့သေးဘူး။ အဆိုတော်နာမည်ပါ ထည့်ပြီး ပြန်ရှာပါ။');return
                self.sessions=OrderedDict((k,v) for k,v in self.sessions.items() if v['until']>time.monotonic() and v['user']!=uid)
                while len(self.sessions)>=64:self.sessions.popitem(last=False)
                token=secrets.token_hex(8)
                row={'user':uid,'until':time.monotonic()+300,'query':text,'choices':list(choices)[:15],'panel':panel}
                self.sessions[token]=row
                await self.show_results(row,token,0)
            except asyncio.CancelledError:
                if panel:
                    try:await panel.edit_text("🎧 စာရင်းရှာတာ ရပ်ထားတယ်။ ခဏနေ ပြန်ရှာပါ။")
                    except Exception:pass
            except Exception:
                if panel:
                    try:await panel.edit_text('⚠️ စာရင်း မရသေးဘူး။ ခဏနေ ပြန်ရှာပါ။')
                    except Exception:pass
            finally:
                self.searching.discard(uid);self.search_tasks.pop(uid,None)

    async def show_results(self,row,token,page):
        choices=row['choices'];pages=(len(choices)+4)//5
        lines=['🎵 '+row['query'][:100],f'ရွေးချယ်ပါ · စာမျက်နှာ {page+1}/{pages}','']
        rows=[]
        for i in range(page*5,min(len(choices),(page+1)*5)):
            c=choices[i];duration=f' · {int(c.duration)//60}:{int(c.duration)%60:02d}' if c.duration else ''
            lines.append(f'{i+1}. {c.title[:100]}{duration}')
            rows.append([B(f'{i+1}. {c.title[:48]}',callback_data=f'dml:{token}:pick:{i}')])
        nav=[]
        if page:nav.append(B('‹ အရှေ့',callback_data=f'dml:{token}:page:{page-1}'))
        if page+1<pages:nav.append(B('နောက် ›',callback_data=f'dml:{token}:page:{page+1}'))
        if nav:rows.append(nav)
        await row['panel'].edit_text('\n'.join(lines),reply_markup=K(rows),parse_mode=ParseMode.DISABLED)

    @staticmethod
    def cache_key(text,choice=None):
        if choice is None:return text.casefold()
        candidate=choice.candidate
        return (choice.source+'|'+choice.title+'|'+str(getattr(getattr(candidate,'message',None),'id',''))+'|'+
                str(getattr(getattr(candidate,'source',None),'username',''))).casefold()

    async def deliver(self,msg,text,choice=None):
        uid=msg.from_user.id if msg.from_user else None
        if not uid:return
        if self.job:
            await msg.reply_text('⏳ သီချင်းတစ်ပုဒ် ပြင်ဆင်နေတယ်။ ခဏနေ ပြန်တောင်းပါ။');return
        now=time.monotonic()
        if now-self.recent.get(uid,-1000)<15:
            await msg.reply_text('⏳ နောက်သီချင်းအတွက် ခဏစောင့်ပါ။');return
        cached=self.cache.get(self.cache_key(text,choice))
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
                        K([[self.lyric_button(title,duration)]]),file_id=file_id)
                    await panel.delete();return
                except Exception:
                    self.cache.discard(self.cache_key(text,choice));job['stage']='resolve'
            path,title,_=await asyncio.wait_for(self.resolver(choice if choice is not None else text),60)
            job['stage']='probe';duration=await verify_song_file(path)
            title=clean_track_title(title)
            job['stage']='upload';await panel.edit_text('🎵 သီချင်းဖိုင် ပို့နေတယ်…',
                reply_markup=K([[B('⏹ ရပ်မယ်',callback_data='dms:'+nonce)]]))
            sent=await send_recognized_audio(self.bot,msg,path,title,duration,
                K([[self.lyric_button(title,duration)]]))
            self.cache.put(self.cache_key(text,choice),title,sent.audio)
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
        tasks=list(self.search_tasks.values())
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        if self.job:
            task=self.job['task'];self.job['reason']='shutdown';task.cancel()
            await asyncio.gather(task,return_exceptions=True)
