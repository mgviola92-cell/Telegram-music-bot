"""Private, owner-only music controls. No outbound group broadcasts."""
import asyncio
import io
import logging
import time
from pyrogram import filters
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardButton as B, InlineKeyboardMarkup as K
from music_owner_store import DEFAULTS, LABELS, validate_text
from welcome_audio import VOICES, BYTES_PER_SECOND, safe_name

log=logging.getLogger('telegram_music')

class MusicOwnerUI:
    def __init__(self,bot,store,welcome,players):
        self.bot,self.store,self.welcome,self.players=bot,store,welcome,players
        self.panel=None
        self.pending={}
        self.busy=False
        self.last_load=-1000

    def menu(self,page='home'):
        def b(label,action):return B(label,callback_data='own:'+action)
        if page=='texts':
            return ('📝 ကြိုဆိုစာ ပြင်မယ် · Group အားလုံးအတွက်\nပြင်ချင်တဲ့ စာသားကို ရွေးပါ။',
                    K([[b(v,'show:'+k)] for k,v in LABELS.items()]+[[b('↩ Owner Menu','home')]]))
        if page in LABELS:
            value=self.store.data['texts'].get(page,DEFAULTS[page])
            text='📝 '+LABELS[page]+'\n\n'+value+'\n\n'
            text+=('နာမည်နေရာမှာ {name} သုံးနိုင်တယ်။\n' if page in {'vc','group'} else
                   'အားလုံးကို ပြောမယ့်စာဖြစ်လို့ နာမည် placeholder မသုံးပါနဲ့။\n')
            return text,K([[b('✏️ စာသားပြင်မယ်','edit:'+page)],
                [b('👨 အသံစမ်း','preview:'+page+':male'),b('👩 အသံစမ်း','preview:'+page+':female')],
                [b('မူလစာသား ပြန်ထားမယ်','reset:'+page)],
                [b('↩ ကြိုဆိုစာများ','texts'),b('⚙️ Owner Menu','home')]])
        if page=='voices':
            return ('🎙 Default ကြိုဆိုအသံ: '+self.store.data['voice']+
                    '\nGroup မှာ သီးခြားရွေးထားတဲ့ အသံရှိရင် အဲဒီအသံကို ဆက်သုံးမယ်။',
                    K([[b('👨 Thiha','voice:male'),b('👩 Nilar','voice:female')],
                       [b('↩ Owner Menu','home')]]))
        return ('⚙️ မုဒြာ Music · Owner Menu\n\nကြိုဆိုစာနဲ့ default အသံကို ဒီနေရာကနေ ပြင်နိုင်တယ်။\n'
                'သိမ်းထားတာတွေက restart/deploy ပြီးလည်း ပြန်ရမယ်။',K([
                [b('📝 ကြိုဆိုစာ အတို / အရှည်','texts')],
                [b('🎙 Default အသံ','voices')],
                [b('📊 Bot Status','status'),b('👥 Group စာရင်း','groups:0')]]))

    async def show(self,msg,page='home',edit=False):
        text,keyboard=self.menu(page)
        if edit:
            try:await msg.edit_text(text,reply_markup=keyboard,parse_mode=ParseMode.DISABLED)
            except Exception as exc:
                if type(exc).__name__!='MessageNotModified':raise
        else:
            panel=await msg.reply_text(text,reply_markup=keyboard,parse_mode=ParseMode.DISABLED)
            self.panel=panel.id

    async def preview(self,chat,key,voice,name):
        pcm=await asyncio.wait_for(self.welcome.pcm(self.store.text(key,safe_name(name) or 'သူငယ်ချင်း'),VOICES[voice]),20)
        proc=await asyncio.create_subprocess_exec('ffmpeg','-nostdin','-v','error','-f','s16le',
             '-ar','48000','-ac','2','-i','pipe:0','-c:a','libmp3lame','-b:a','96k','-f','mp3','pipe:1',
             stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        try:
            data,_=await asyncio.wait_for(proc.communicate(pcm),6)
            if proc.returncode or not data:raise RuntimeError('No preview audio')
        finally:
            if proc.returncode is None:proc.kill();await proc.wait()
        f=io.BytesIO(data);f.name='mudyar_welcome_preview.mp3'
        await self.bot.send_audio(chat,f,title=LABELS[key],performer='မုဒြာ Music',
                                  duration=round(len(pcm)/BYTES_PER_SECOND),caption='🎙 '+voice)

    def register(self):
        @self.bot.on_message(filters.private & filters.command(['owner','start','cancel']),group=-2)
        async def owner_command(_,msg):
            user=msg.from_user.id if msg.from_user else None
            command=(msg.command or [''])[0]
            if command=='owner' and not self.store.ready and time.monotonic()-self.last_load>60:
                self.last_load=time.monotonic()
                await self.store.load()
            if not self.store.authorized(user):
                if command!='owner':return
                claimed=False
                if len(msg.command)==2:
                    try:claimed=await self.store.claim(user,msg.command[1])
                    except Exception as exc:log.info('Music owner claim unavailable (%s)',type(exc).__name__)
                    # Remove the one-time code from the private chat after handling it.
                    try:await msg.delete()
                    except Exception:pass
                if not claimed:
                    await msg.reply_text('🔒 Owner အတွက်ပဲ သုံးနိုင်တယ်။ ပထမဆုံး ချိတ်ဆက်ဖို့ တစ်ကြိမ်သုံး setup code လိုတယ်။')
                    msg.stop_propagation();return
            self.pending.pop(user,None)
            if self.busy:
                await msg.reply_text('⏳ လက်ရှိအလုပ် ပြီးအောင် ခဏစောင့်ပါ။')
            else:await self.show(msg)
            msg.stop_propagation()

        @self.bot.on_callback_query(filters.regex(r'^own:'),group=-2)
        async def owner_callback(_,cb):
            user=cb.from_user.id if cb.from_user else None
            if (not self.store.authorized(user) or not cb.message or cb.message.chat.id!=user
                    or cb.message.id!=self.panel):
                await cb.answer('Owner private menu မှာပဲ သုံးနိုင်တယ်။ /owner ပြန်ခေါ်ပါ။',show_alert=True)
                return
            if self.busy:
                await cb.answer('အလုပ်လုပ်နေတယ်။ ခဏစောင့်ပါ။');return
            await cb.answer()
            self.pending.pop(user,None)
            parts=cb.data.split(':')[1:]
            action=parts[0]
            key=parts[1] if len(parts)>1 else ''
            try:
                if action in {'home','texts','voices'}:
                    await self.show(cb.message,action,True)
                elif action=='show' and key in LABELS:
                    await self.show(cb.message,key,True)
                elif action=='edit' and key in LABELS:
                    self.pending[user]=(key,time.monotonic()+300,cb.message)
                    await cb.message.edit_text('✏️ '+LABELS[key]+'\n\nစာသားအသစ်ကို ဒီ private chat ထဲ ပို့ပါ။\n'
                        'အသံနှစ်မျိုးနဲ့ အရှည်စစ်ပြီးမှ သိမ်းမယ်။\n/cancel နဲ့ မပြင်တော့ဘဲ ပြန်ထွက်နိုင်တယ်။',
                        reply_markup=K([[B('↩ မပြင်တော့ဘူး',callback_data='own:show:'+key)]]),parse_mode=ParseMode.DISABLED)
                elif action=='preview' and key in LABELS and len(parts)==3 and parts[2] in VOICES:
                    self.busy=True
                    try:await self.preview(user,key,parts[2],cb.from_user.first_name)
                    finally:self.busy=False
                elif action=='reset' and key in LABELS:
                    self.busy=True
                    try:
                        await self.store.reset_text(user,key)
                        await self.show(cb.message,key,True)
                    finally:self.busy=False
                elif action=='voice' and key in VOICES:
                    self.busy=True
                    try:
                        await self.store.set_voice(user,key)
                        await self.show(cb.message,'voices',True)
                    finally:self.busy=False
                elif action=='status':
                    active=sum(p.current is not None or p.idle_mode is not None for p in self.players.values())
                    groups=len(set(self.store.data['groups'])|set(self.store.observed))
                    text=f'📊 Music Bot\nအသံဝန်ဆောင်မှု: {"Ready" if self.welcome.ready else "Unavailable"}\nActive VC: {active}\nမှတ်တမ်းရ Group: {groups}\nစိတ်ကြိုက်စာသား: {len(self.store.data["texts"])} / 4'
                    await cb.message.edit_text(text,reply_markup=K([[B('↩ Owner Menu',callback_data='own:home')]]))
                elif action=='groups':
                    page=max(0,int(key or 0))
                    groups=dict(self.store.data['groups']);groups.update(self.store.observed)
                    entries=list(groups.items());page=min(page,max(0,(len(entries)-1)//8))
                    text='👥 Music Bot မှတ်တမ်းရ Group များ\n'
                    for cid,title in entries[page*8:page*8+8]:text+='\n'+title+'\n'+cid+'\n'
                    if not entries:text+='\nGroup မှာ Bot ကို သုံးလာတဲ့အခါ စာရင်းထဲ ဝင်မယ်။'
                    nav=[]
                    if page:nav.append(B('‹',callback_data=f'own:groups:{page-1}'))
                    if (page+1)*8<len(entries):nav.append(B('›',callback_data=f'own:groups:{page+1}'))
                    await cb.message.edit_text(text,parse_mode=ParseMode.DISABLED,
                        reply_markup=K(([nav] if nav else [])+[[B('↩ Owner Menu',callback_data='own:home')]]))
            except Exception as exc:
                log.info('Music owner action unavailable action=%s (%s)',action,type(exc).__name__)
                await self.bot.send_message(user,'⚠️ ယာယီမအောင်မြင်ဘူး။ /owner နဲ့ ပြန်စမ်းပါ။')

        @self.bot.on_message(filters.private & filters.text,group=-3)
        async def owner_text(_,msg):
            user=msg.from_user.id if msg.from_user else None
            pending=self.pending.get(user)
            if (not self.store.authorized(user) or not pending or msg.text.startswith('/')):return
            if self.busy:
                await msg.reply_text('⏳ သိမ်းနေတယ်။ ပြီးမှ နောက်စာ ပို့ပါ။');msg.stop_propagation();return
            key,until,panel=pending
            if time.monotonic()>until:
                self.pending.pop(user,None)
                await msg.reply_text('Edit သက်တမ်းကုန်ပြီ။ /owner ပြန်ခေါ်ပါ။')
                msg.stop_propagation();return
            try:text=validate_text(key,msg.text)
            except ValueError as exc:
                await msg.reply_text(str(exc));msg.stop_propagation();return
            self.busy=True
            try:
                await panel.edit_text('⏳ အသံအရှည် စစ်ပြီး သိမ်းနေတယ်…')
                rendered=text.format(name=safe_name(msg.from_user.first_name))
                # Verify that neither voice truncates the new text before committing it.
                for voice in VOICES.values():await asyncio.wait_for(self.welcome.pcm(rendered,voice),20)
                await self.store.set_text(user,key,text)
                self.pending.pop(user,None)
                await self.show(panel,key,True)
            except Exception as exc:
                log.info('Music greeting save unavailable (%s)',type(exc).__name__)
                await panel.edit_text('⚠️ မသိမ်းရသေးဘူး။ စာသားကို ပိုတိုအောင် ပြင်ပြီး ပြန်ပို့ပါ၊ '
                                      'ဒါမှမဟုတ် /owner ပြန်ခေါ်ပါ။')
            finally:self.busy=False
            msg.stop_propagation()
