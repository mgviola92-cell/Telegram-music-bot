"""Independent, opt-in Karaoke jobs. Never pause/replace the currently playing stream."""
import asyncio
import secrets
import tempfile
import time
import logging
import shutil
from collections import OrderedDict,deque
from pathlib import Path
from pyrogram import filters
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardButton as B,InlineKeyboardMarkup as K
from mvsep_service import configured_provider,KaraokeQuota
from karaoke_audio import prepare_input,prepare_result

LOG=logging.getLogger('telegram_music')

def resources_available():
    """Reserve room for input, downloaded MP3 and disposable output."""
    if shutil.disk_usage(tempfile.gettempdir()).free < 128*1024*1024:return False
    try:
        used=int(Path('/sys/fs/cgroup/memory.current').read_text())
        limit=int(Path('/sys/fs/cgroup/memory.max').read_text())
        if limit-used < 96*1024*1024:return False
    except (OSError,ValueError):pass
    return True

class KaraokeUI:
    def __init__(self,bot,current,is_admin,enqueue):
        self.bot,self.current,self.is_admin,self.enqueue=bot,current,is_admin,enqueue
        self.provider=configured_provider();self.job=None;self.results=OrderedDict();self.recent=deque()
    def register(self):
        @self.bot.on_callback_query(filters.regex(r'^karaoke:(current|cancel|play)(:.*)?$'),group=-6)
        async def callback(_,cb):
            if not cb.message or not cb.from_user:await cb.answer();return
            chat=cb.message.chat.id
            if chat>=0 or not await self.is_admin(chat,cb.from_user.id):
                await cb.answer('Group Admin ကပဲ သုံးနိုင်တယ်။',show_alert=True);return
            parts=cb.data.split(':');action=parts[1]
            if action=='cancel':
                if self.job and self.job['chat']==chat and self.job['panel'] and self.job['panel'].id==cb.message.id:
                    self.job['task'].cancel()
                await cb.answer('ရပ်နေတယ်…');return
            if action=='play':
                row=self.results.get(parts[2] if len(parts)>2 else '')
                if not row or row['until']<time.monotonic() or row['chat']!=chat or row['message'].id!=cb.message.id:
                    await cb.answer('သက်တမ်းကုန်ပြီ။ Audio ဖိုင်ကို Reply /play သုံးနိုင်တယ်။',show_alert=True);return
                await cb.answer('Queue ထဲ ထည့်နေတယ်…')
                await self.enqueue(cb,row['message']);return
            if not self.provider:
                await cb.answer('Karaoke ဝန်ဆောင်မှု မဖွင့်ရသေးဘူး။',show_alert=True);return
            source=self.current(chat,cb.message.id)
            if not source:
                await cb.answer('လက်ရှိသီချင်းကဒ်ကနေ ပြန်နှိပ်ပါ။',show_alert=True);return
            if self.job:
                await cb.answer('Karaoke တစ်ပုဒ် ပြင်ဆင်နေတယ်။ ပြီးမှ ပြန်တောင်းပါ။',show_alert=True);return
            now=time.monotonic()
            while self.recent and self.recent[0]<now-3600:self.recent.popleft()
            if len(self.recent)>=6:
                await cb.answer('အခမဲ့အသုံးပြုမှု limit ကြောင့် ခဏနားထားတယ်။',show_alert=True);return
            title,path,seconds=source
            ready=next((r for r in self.results.values() if r['chat']==chat and r.get('source')==source and r['until']>now),None)
            if ready:
                await cb.answer('တီးလုံးဖိုင်ကို ဒီ chat ထဲ ပို့ထားပြီးပြီ။ အဲဒီဖိုင်ရဲ့ ဖွင့်မယ်ခလုတ် သုံးနိုင်တယ်။',show_alert=True);return
            if seconds and seconds>300:
                await cb.answer('Karaoke အတွက် ၅ မိနစ်အောက် သီချင်း ရွေးပါ။',show_alert=True);return
            if not resources_available():
                await cb.answer('လက်ရှိ server ဝန်များနေတယ်။ သီချင်းကို ဆက်နားထောင်ပြီး Karaoke ကို နောက်မှ ပြန်စမ်းပါ။',show_alert=True);return
            self.recent.append(now)
            # Reserve before any await, so simultaneous clicks cannot create two jobs.
            job={'chat':chat,'panel':None,'task':None};self.job=job
            job['task']=asyncio.create_task(self.run(cb,job,title,path,source))
            await cb.answer('🎤 Karaoke ပြင်ဆင်နေတယ်…')
    async def run(self,cb,job,title,source,source_key=None):
        try:
            panel=await cb.message.reply_text('🎤 တီးလုံးသီးသန့် ပြင်ဆင်နေတယ်… ပုံမှန်သီချင်းကို ဆက်နားထောင်နိုင်တယ်။',
                reply_markup=K([[B('⏹ မလုပ်တော့ဘူး',callback_data='karaoke:cancel')]]))
            job['panel']=panel
            async with asyncio.timeout(300):
                # Reject auth/quota/busy accounts before fetching a full song.
                await self.provider.preflight()
                with tempfile.TemporaryDirectory(prefix='mudyar_karaoke_') as folder:
                    path,seconds=await prepare_input(source,folder)
                    await panel.edit_text('🎤 အဆိုတော်အသံ ခွဲထုတ်နေတယ်…',reply_markup=K([[B('⏹ မလုပ်တော့ဘူး',callback_data='karaoke:cancel')]]))
                    async def progress(status):
                        text=('🎤 တီးလုံးဖိုင် အလှည့်စောင့်နေတယ်…' if status=='waiting' else '🎤 အဆိုတော်အသံ ခွဲထုတ်နေတယ်…')
                        if status=='done':text='🎤 တီးလုံးဖိုင် ပြန်ယူနေတယ်…'
                        try:await panel.edit_text(text,reply_markup=K([[B('⏹ မလုပ်တော့ဘူး',callback_data='karaoke:cancel')]]))
                        except Exception:pass
                    raw=await self.provider.separate(path,Path(folder)/'remote.mp3',progress)
                    output=await prepare_result(raw,Path(folder)/'karaoke.mp3',seconds)
                    token=secrets.token_hex(8)
                    sent=await asyncio.wait_for(self.bot.send_audio(job['chat'],output,title=('Karaoke · '+title)[:120],duration=int(seconds),
                        caption='🎤 တီးလုံးသီးသန့် အသင့်ဖြစ်ပြီ။ ဖွင့်မယ်နှိပ်ရင် Queue ထဲ ထည့်ပေးမယ်။',parse_mode=ParseMode.DISABLED,
                        reply_markup=K([[B('▶ ဖွင့်မယ်',callback_data='karaoke:play:'+token)]])),45)
                    self.results[token]={'chat':job['chat'],'message':sent,'until':time.monotonic()+3600,'source':source_key}
                    while len(self.results)>8:self.results.popitem(last=False)
                    await panel.delete()
        except asyncio.CancelledError:
            if job['panel']:
                try:await job['panel'].edit_text('⏹ Karaoke တောင်းဆိုမှု ရပ်လိုက်ပြီ။')
                except Exception:pass
        except Exception as exc:
            LOG.warning('Karaoke job unavailable error_type=%s',type(exc).__name__)
            if job['panel']:
                try:await job['panel'].edit_text('အခမဲ့ Karaoke quota မရသေးဘူး။ နောက်မှ ပြန်စမ်းပါ။' if isinstance(exc,KaraokeQuota) else
                    '⚠️ Karaoke မရသေးဘူး။ ပုံမှန်သီချင်း ဆက်နားထောင်နိုင်တယ်။ နောက်မှ ပြန်စမ်းပါ။')
                except Exception:pass
        finally:
            if self.job is job:self.job=None
    async def close(self):
        if self.job and self.job['task']:
            task=self.job['task'];task.cancel();await asyncio.gather(task,return_exceptions=True)
