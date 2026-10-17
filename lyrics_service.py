"""On-demand bounded LRCLIB lookups and paginated Telegram text views."""
import asyncio
import json
import re
import secrets
import time
import unicodedata
from collections import OrderedDict
from aiohttp import ClientSession,ClientTimeout
from pyrogram import filters
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardButton as B,InlineKeyboardMarkup as K
from music_ui import clean_track_title
from player_visuals import lyrics_search_url


def query_text(text):
    return ' '.join(''.join(c if c.isalnum() or unicodedata.category(c).startswith('M') else ' ' for c in unicodedata.normalize('NFKC',text)).split())

def words(text):
    return set(query_text(text).casefold().split())

def choose_record(rows,title,duration=None):
    wanted=words(title);ranked=[]
    for row in rows if isinstance(rows,list) else []:
        if not isinstance(row,dict):continue
        actual=words(str(row.get('trackName',''))+' '+str(row.get('artistName','')))
        coverage=len(wanted&actual)/max(1,len(wanted))
        if coverage<.8:continue
        try:delta=abs(float(row.get('duration',0))-float(duration)) if duration else 0
        except (TypeError,ValueError):continue
        if duration and delta>max(12,float(duration)*.1):continue
        lyrics=row.get('plainLyrics')
        if not lyrics and row.get('syncedLyrics'):
            lyrics=re.sub(r'\[[^\]\n]*\]','',str(row['syncedLyrics'])).strip()
        if not isinstance(lyrics,str) or not lyrics.strip() or len(lyrics)>32000:continue
        ranked.append((coverage-delta/10000,lyrics.strip(),str(row.get('trackName',''))+' – '+str(row.get('artistName',''))))
    return max(ranked,key=lambda r:r[0])[1:] if ranked else None

class LyricsLookup:
    def __init__(self,telegram=None):self.cache=OrderedDict();self.slots=asyncio.Semaphore(2);self.jobs={};self.telegram=telegram
    async def find(self,title,duration=None):
        title=clean_track_title(title)[:150];key=(title.casefold(),round(duration or 0))
        cached=self.cache.get(key)
        if cached and cached[0]>time.monotonic():self.cache.move_to_end(key);return cached[1]
        # The same track across groups shares one bounded search.
        task=self.jobs.get(key)
        if task is None:
            if len(self.jobs)>=2:return None
            task=asyncio.create_task(self.race(title,duration));self.jobs[key]=task
            task.add_done_callback(lambda done:self.jobs.pop(key,None))
        result=await asyncio.shield(task)
        self.cache[key]=(time.monotonic()+(3600 if result else 60),result)
        while len(self.cache)>32:self.cache.popitem(last=False)
        return result
    async def race(self,title,duration):
        from lyric_sources import LyricsResult
        tasks=[asyncio.create_task(self.lrclib(title,duration))]
        if self.telegram:tasks.append(asyncio.create_task(self.telegram.find(title,duration)))
        try:
            async with asyncio.timeout(9):
                pending=set(tasks)
                while pending:
                    done,pending=await asyncio.wait(pending,return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        if task.cancelled():continue
                        try:result=task.result()
                        except Exception as exc:
                            import logging
                            logging.getLogger('telegram_music').info('Lyrics provider unavailable (%s)',type(exc).__name__)
                            continue
                        if result:
                            return result if isinstance(result,LyricsResult) else LyricsResult(*result,'LRCLIB')
        except TimeoutError:pass
        finally:
            for task in tasks:
                if not task.done():task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
        return None
    async def lrclib(self,title,duration=None):
        async with self.slots:
            async with ClientSession(timeout=ClientTimeout(total=7),headers={'User-Agent':'MudyarMusic/1.0 (Telegram lyrics lookup)','Accept':'application/json'}) as client:
                async with client.get('https://lrclib.net/api/search',params={'q':query_text(title)},allow_redirects=False) as response:
                    if response.status!=200:raise ValueError('Lyrics service unavailable')
                    payload=bytearray()
                    async for chunk in response.content.iter_chunked(16384):
                        payload.extend(chunk)
                        if len(payload)>256*1024:raise ValueError('Lyrics response too large')
                    records=json.loads(payload)
                    return choose_record(records,title,duration)

class LyricsUI:
    def __init__(self,bot,current,telegram=None):
        self.bot,self.current=bot,current;self.lookup=LyricsLookup(telegram)
        self.links=OrderedDict();self.views=OrderedDict();self.recent=OrderedDict();self.busy=set()
    async def probe(self):
        import logging
        log=logging.getLogger('telegram_music')
        try:
            result=await self.lookup.lrclib('Perfect Ed Sheeran',263)
            if not result:raise ValueError('No matching record')
            log.info('Lyrics live probe PASS characters=%s',len(result[0]))
        except Exception as exc:log.info('Lyrics live probe unavailable (%s)',type(exc).__name__)
        telegram=self.lookup.telegram
        if telegram:
            try:
                result=await telegram.find('Purple Wink pGb',168,timeout=24)
                log.info('Lyrics Telegram live probe result=%s characters=%s','PASS' if result else 'no-text',len(result[0]) if result else 0)
            except Exception as exc:log.info('Lyrics Telegram live probe unavailable (%s)',type(exc).__name__)


    def button(self,title,duration=None):
        token=secrets.token_hex(8);self.links[token]=(time.monotonic()+3600,title,duration)
        while len(self.links)>256:self.links.popitem(last=False)
        return B('🎤 Lyrics',callback_data='lyr:get:'+token)
    @staticmethod
    def search_buttons(title):
        from urllib.parse import quote_plus
        def url(site):return 'https://www.google.com/search?q='+quote_plus('site:'+site+' '+title+' lyrics')
        return K([[B('🔎 စာသားရှာမယ်',url=lyrics_search_url(title))],
                  [B('Smule',url=url('smule.com')),B('Genius',url=url('genius.com')),B('Musixmatch',url=url('musixmatch.com'))]])
    async def show(self,row,token,page):
        chunks=row['pages'];page=max(0,min(page,len(chunks)-1))
        text='🎤 '+row['title'][:130]+f"\n{page+1}/{len(chunks)} · {row.get('provider','LRCLIB')}\n\n"+chunks[page]
        nav=[]
        if page:nav.append(B('‹ အရှေ့',callback_data=f'lyr:page:{token}:{page-1}'))
        if page+1<len(chunks):nav.append(B('နောက် ›',callback_data=f'lyr:page:{token}:{page+1}'))
        keys=([nav] if nav else [])+[[B('🔎 အခြားစာသား ရှာမယ်',url=lyrics_search_url(row['title'])),B('ပိတ်မယ်',callback_data='lyr:close:'+token)]]
        await row['panel'].edit_text(text,parse_mode=ParseMode.DISABLED,reply_markup=K(keys),disable_web_page_preview=True)
    def register(self):
        @self.bot.on_callback_query(filters.regex(r'^(lyr:|lyrics:current$)'),group=-6)
        async def lyrics_callback(_,cb):
            if not cb.message or not cb.from_user:await cb.answer();return
            uid=cb.from_user.id;chat=cb.message.chat.id;parts=cb.data.split(':')
            if parts[0]=='lyr' and parts[1] in {'page','close'}:
                token=parts[2] if len(parts)>2 else '';row=self.views.get(token)
                if not row or row['until']<time.monotonic() or row['user']!=uid or row['panel'].id!=cb.message.id or row['chat']!=chat:
                    await cb.answer('စာသားကြည့်ရန် ခလုတ် ပြန်နှိပ်ပါ။',show_alert=True);return
                await cb.answer()
                if parts[1]=='close':self.views.pop(token,None);await cb.message.delete();return
                try:await self.show(row,token,int(parts[3]))
                except (ValueError,IndexError):pass
                return
            if cb.data=='lyrics:current':
                current=self.current(chat,cb.message.id)
                if not current:await cb.answer('လက်ရှိသီချင်းကဒ်ကနေ ပြန်နှိပ်ပါ။',show_alert=True);return
                title,duration=current
            elif len(parts)==3 and parts[1]=='get':
                link=self.links.get(parts[2])
                if not link or link[0]<time.monotonic():
                    # Audio metadata survives process restarts even when callback tokens do not.
                    audio=getattr(cb.message,'audio',None)
                    if not audio or not getattr(audio,'title',None):await cb.answer('သီချင်းဖိုင်ကို ပြန်တောင်းပြီး Lyrics နှိပ်ပါ။',show_alert=True);return
                    title,duration=audio.title,audio.duration
                else:_,title,duration=link
            else:await cb.answer();return
            old=next(((t,r) for t,r in self.views.items() if r['chat']==chat and r['user']==uid and r['source']==cb.message.id and r.get('request_title')==title and r['until']>time.monotonic()),None)
            if old:
                await cb.answer('စာသားကို အောက်မှာ ပြထားတယ်။');return
            if uid in self.busy or len(self.busy)>=2 or self.recent.get(uid,0)>time.monotonic():
                await cb.answer('ခဏနေ ပြန်နှိပ်ပါ။',show_alert=True);return
            self.busy.add(uid);self.recent[uid]=time.monotonic()+10
            while len(self.recent)>256:self.recent.popitem(last=False)
            panel=None
            try:
                await cb.answer('🎤 စာသားရှာနေတယ်…')
                panel=await cb.message.reply_text('🎤 သီချင်းစာသား ရှာနေတယ်…')
                result=await asyncio.wait_for(self.lookup.find(title,duration),10)
                if not result:
                    await panel.edit_text('ဒီသီချင်းစာသား မတွေ့သေးဘူး။',reply_markup=self.search_buttons(title));return
                lyrics,matched=result
                token=secrets.token_hex(8)
                # <=1400 Unicode code points keeps even supplementary text below Telegram's UTF-16 limit.
                row={'chat':chat,'user':uid,'source':cb.message.id,'request_title':title,'title':matched,'provider':getattr(result,'source','LRCLIB'),'pages':[lyrics[i:i+1400] for i in range(0,len(lyrics),1400)],'panel':panel,'until':time.monotonic()+600}
                self.views=OrderedDict((t,r) for t,r in self.views.items() if r['until']>time.monotonic())
                while len(self.views)>=32:self.views.popitem(last=False)
                self.views[token]=row;await self.show(row,token,0)
            except Exception:
                if panel:
                    try:await panel.edit_text('စာသားဝန်ဆောင်မှု လက်ရှိ မရသေးဘူး။',reply_markup=self.search_buttons(title))
                    except Exception:pass
            finally:self.busy.discard(uid)
