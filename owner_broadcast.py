"""Owner-triggered, reviewed copies to known groups; never auto-broadcast on boot."""
import asyncio
import copy
import secrets
import time
from pyrogram import filters
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardButton as B, InlineKeyboardMarkup as K

class OwnerBroadcast:
    def __init__(self,ui):
        self.ui=ui;self.bot=ui.bot;self.store=ui.store
        self.waiting=False;self.selection=None;self.review=None
        self.task=None;self.cancel=asyncio.Event();self.progress=None
        self.counts=(0,0,0)
    def groups(self):
        d=dict(self.store.data['groups']);d.update(self.store.observed)
        return {int(k):v for k,v in d.items()}
    def targets(self):
        g=self.groups();return sorted(g if self.selection is None else set(g)&self.selection)
    async def menu(self,msg,page=0,pick=False):
        def b(t,a):return B(t,callback_data='bc:'+a)
        rows=[];g=self.groups();targets=self.targets()
        text=f'📣 Broadcast\nမှတ်တမ်းရ Group: {len(g)} · ရွေးထားတာ: {len(targets)}\n'
        text+='ကြေညာစာ: '+('သိမ်းထားပြီး' if self.store.data.get('broadcast') else 'မရွေးရသေးဘူး')
        if pick:
            entries=list(g.items());page=max(0,min(page,max(0,(len(entries)-1)//6)))
            for cid,title in entries[page*6:page*6+6]:rows.append([b(('✓ ' if cid in targets else '')+title[:45],f'toggle:{cid}:{page}')])
            nav=[]
            if page:nav.append(b('‹',f'pick:{page-1}'))
            if (page+1)*6<len(entries):nav.append(b('›',f'pick:{page+1}'))
            if nav:rows.append(nav)
            rows.append([b('အားလုံး','all'),b('ဘာမှမရွေး','none')])
            rows.append([b('↩ Broadcast','home')])
        else:
            rows=[[b('✏️ ကြေညာစာအသစ် ပို့မယ်','new')],
                  [b('👀 ကြေညာစာ ကြည့်မယ်','preview')],
                  [b('Group ရွေးမယ်','pick:0'),b('အားလုံးရွေး','all')],
                  [b('📤 ပို့မယ် · ပြန်စစ်','review')],
                  [B('↩ Owner Menu',callback_data='own:home')]]
            if self.task and not self.task.done():rows.insert(0,[b('⏹ လက်ရှိပို့နေတာ ရပ်မယ်','cancel')])
        await msg.edit_text(text,reply_markup=K(rows),parse_mode=ParseMode.DISABLED)
    def take_review(self,token):
        r=self.review;self.review=None
        if not r or r['token']!=token or r['until']<time.monotonic():return None
        return r
    async def send_job(self,owner,source,targets):
        ok=failed=0
        try:
            for cid in targets:
                if self.cancel.is_set():break
                try:
                    try:await asyncio.wait_for(self.bot.copy_message(cid,source['chat_id'],source['message_id']),15)
                    except Exception as exc:
                        if type(exc).__name__!='FloodWait':raise
                        delay=int(getattr(exc,'value',60))
                        if delay>60:break
                        try:await asyncio.wait_for(self.cancel.wait(),max(1,delay))
                        except asyncio.TimeoutError:pass
                        if self.cancel.is_set():break
                        await asyncio.wait_for(self.bot.copy_message(cid,source['chat_id'],source['message_id']),15)
                    ok+=1
                except asyncio.CancelledError:raise
                except Exception:failed+=1
                self.counts=(ok,failed,len(targets)-ok-failed)
                if (ok+failed)%5==0 and self.progress:
                    try:await self.progress.edit_text(f'📣 ပို့ပြီး: {ok} · မအတည်ပြုနိုင်: {failed}\nကျန်: {len(targets)-ok-failed}',
                        reply_markup=K([[B('⏹ ရပ်မယ်',callback_data='bc:cancel')]]))
                    except Exception:pass
                try:await asyncio.wait_for(self.cancel.wait(),.18)
                except asyncio.TimeoutError:pass
        finally:
            self.counts=(ok,failed,len(targets)-ok-failed)
            if self.progress:
                try:await self.progress.edit_text(f'📣 Broadcast ပြီးဆုံး\nပို့ပြီး: {ok}\nမပို့နိုင်/မအတည်ပြုနိုင်: {failed}\nမပို့ရသေး: {len(targets)-ok-failed}')
                except Exception:pass
    def register(self):
        @self.bot.on_callback_query(filters.regex(r'^bc:'),group=-3)
        async def broadcast_callback(_,cb):
            uid=cb.from_user.id if cb.from_user else None
            if not self.store.authorized(uid) or not cb.message or cb.message.chat.id!=uid:
                await cb.answer('Owner private chat မှာပဲ သုံးနိုင်တယ်။',show_alert=True);return
            action=cb.data.split(':')[1:]
            is_progress=self.progress and cb.message.id==self.progress.id
            if cb.message.id!=self.ui.panel and not (is_progress and action[0]=='cancel'):
                await cb.answer('/owner ပြန်ခေါ်ပါ။',show_alert=True);return
            if self.ui.busy:await cb.answer('ခဏစောင့်ပါ။');return
            await cb.answer();self.ui.pending.pop(uid,None);self.ui.record_pending.pop(uid,None)
            self.waiting=False
            try:
                a=action[0]
                if a=='cancel':self.cancel.set();return
                if a=='confirm':
                    if self.task and not self.task.done():return
                    review=self.take_review(action[1] if len(action)>1 else '')
                    if not review:await self.menu(cb.message);return
                    self.cancel=asyncio.Event()
                    self.progress=await self.bot.send_message(uid,'📣 Broadcast စပို့နေတယ်…',
                        reply_markup=K([[B('⏹ ရပ်မယ်',callback_data='bc:cancel')]]))
                    self.task=asyncio.create_task(self.send_job(uid,review['source'],review['targets']))
                    return
                self.review=None
                if a=='new':
                    self.waiting=True
                    await cb.message.edit_text('📣 ကြေညာစာအဖြစ် ပို့ချင်တဲ့ စာ/ပုံ/ဗီဒီယို/အသံဖိုင် တစ်စောင်ကို ဒီ private chat ထဲ ပို့ပါ။ Forward လုပ်ပို့လည်း ရတယ်။',
                        reply_markup=K([[B('↩ မပို့တော့ဘူး',callback_data='bc:home')]]));return
                if a=='preview':
                    source=self.store.data.get('broadcast')
                    if source:await self.bot.copy_message(uid,source['chat_id'],source['message_id'])
                elif a=='all':self.selection=None
                elif a=='none':self.selection=set()
                elif a=='toggle':
                    cid=int(action[1])
                    if cid not in self.groups():return
                    if self.selection is None:self.selection=set(self.groups())
                    if cid in self.selection:self.selection.remove(cid)
                    else:self.selection.add(cid)
                    await self.menu(cb.message,int(action[2]),True);return
                elif a=='pick':await self.menu(cb.message,int(action[1]),True);return
                elif a=='review':
                    source=copy.deepcopy(self.store.data.get('broadcast'));targets=self.targets()
                    if not source or not targets:
                        await cb.message.edit_text('ကြေညာစာနဲ့ Group ကို အရင်ရွေးပါ။',reply_markup=K([[B('↩ Broadcast',callback_data='bc:home')]]));return
                    await self.bot.copy_message(uid,source['chat_id'],source['message_id'])
                    token=secrets.token_hex(8)
                    self.review={'token':token,'source':source,'targets':targets,'until':time.monotonic()+300}
                    await cb.message.edit_text(f'📣 အပေါ်က ကြေညာစာကို Group {len(targets)} ခုဆီ ပို့မယ်။',
                        reply_markup=K([[B('✅ အခု ပို့မယ်',callback_data='bc:confirm:'+token)],
                                        [B('↩ မပို့သေးဘူး',callback_data='bc:home')]]));return
                await self.menu(cb.message)
            except Exception:
                await self.bot.send_message(uid,'⚠️ Broadcast ပြင်ဆင်မှု မအောင်မြင်ဘူး။ /owner ပြန်သုံးပါ။')
        @self.bot.on_message(filters.private,group=-5)
        async def broadcast_draft(_,msg):
            uid=msg.from_user.id if msg.from_user else None
            if not self.waiting or not self.store.authorized(uid):return
            if msg.text and msg.text.startswith('/'):
                self.waiting=False;return
            if self.ui.busy:msg.stop_propagation();return
            supported=any(getattr(msg,k,None) for k in ('text','photo','video','audio','voice','document','animation'))
            if not supported or getattr(msg,'has_protected_content',False):
                await msg.reply_text('Copy ပို့လို့ရတဲ့ စာ/ပုံ/ဗီဒီယို/အသံဖိုင် ပို့ပါ။');msg.stop_propagation();return
            self.ui.busy=True
            try:
                await self.bot.copy_message(uid,uid,msg.id)
                await self.store.set_broadcast(uid,msg.id)
                self.waiting=False;self.review=None
                panel=await msg.reply_text('📣 ကြေညာစာ သိမ်းပြီးပြီ။',reply_markup=K([[B('Group ရွေးပြီး ပို့မယ်',callback_data='bc:home')]]))
                self.ui.panel=panel.id
            except Exception:await msg.reply_text('⚠️ ကြေညာစာ မသိမ်းရသေးဘူး။ ပြန်စမ်းပါ။')
            finally:self.ui.busy=False
            msg.stop_propagation()
    async def close(self):
        self.cancel.set()
        if self.task and not self.task.done():
            try:await asyncio.wait_for(asyncio.shield(self.task),18)
            except asyncio.TimeoutError:self.task.cancel();await asyncio.gather(self.task,return_exceptions=True)
