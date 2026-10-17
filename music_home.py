"""Compact Burmese /start card. Refresh and help edit the same panel."""
import asyncio
import time
from html import escape
from collections import OrderedDict
from pyrogram import filters
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardButton as B,InlineKeyboardMarkup as K
from card_layout import render,mention
from runtime_status import STATUS
from bandwidth_status import BANDWIDTH
from music_help import HELP_TEXT
from telegram_photo_cache import send_default_photo

class MusicHome:
    def __init__(self,bot,store,players,photos):
        self.bot,self.store,self.players,self.photos=bot,store,players,photos
        self.panels=OrderedDict();self.recent=OrderedDict()
    def text(self,user=None):
        name=mention(getattr(user,'first_name','သူငယ်ချင်း'),getattr(user,'id',None))
        active=sum(p.current is not None or p.idle_mode is not None for p in self.players.values())
        return ('<b>'+escape(render('brand'))+'</b>\n\n'
                'မင်္ဂလာပါ '+name+'။\n'
                'သီချင်းနားထောင်ဖို့ မုဒြာကနေ ကြိုဆိုပါတယ်။\n\n'
                '<b>လက်ရှိအခြေအနေ</b>\n'+escape(STATUS.text()+'\n'+BANDWIDTH.text())+'\n'
                f'▸ ချိတ်ဆက်ထားသော VC : {active}\n\n'
                'သီချင်းနာမည် ရိုက်ပြီး ရှာနိုင်တယ်။\n'
                'Group အသံခန်းမှာ /play နဲ့ ဖွင့်နိုင်တယ်။')
    @staticmethod
    def keyboard(username,help_page=False):
        return K([[B('Group ထဲ ထည့်မယ်',url=f'https://t.me/{username}?startgroup=true')],
                  [B('‹ ပင်မစာမျက်နှာ' if help_page else 'အသုံးပြုနည်း',callback_data='home:main' if help_page else 'home:help'),
                   B('ပြန်စစ်မယ်',callback_data='home:refresh')]])
    async def show(self,msg,help_page=False):
        uid=getattr(getattr(msg,'from_user',None),'id',0);now=time.monotonic()
        key=(msg.chat.id,uid)
        if now-self.recent.get(key,-1000)<5:return
        self.recent[key]=now
        while len(self.recent)>128:self.recent.popitem(last=False)
        await BANDWIDTH.refresh()
        me=await self.bot.get_me()
        text=escape(HELP_TEXT) if help_page else self.text(msg.from_user)
        buttons=self.keyboard(me.username,help_page)
        panel=None;photo=False
        if not help_page:
            try:
                panel=await asyncio.wait_for(send_default_photo(self.photos,self.bot,msg.chat.id,
                    self.store.data.get('default_photo'),caption=text,reply_markup=buttons,
                    parse_mode=ParseMode.HTML,reply_to_message_id=msg.id),5)
                photo=True
            except Exception:pass
        if panel is None:panel=await msg.reply_text(text,reply_markup=buttons,parse_mode=ParseMode.HTML)
        self.panels[(msg.chat.id,panel.id)]=(uid,me.username,photo,now,0,msg.from_user)
        while len(self.panels)>128:self.panels.popitem(last=False)
    def register(self):
        @self.bot.on_callback_query(filters.regex(r'^home:(main|help|refresh)$'))
        async def home_callback(_,cb):
            if not cb.message:return
            key=(cb.message.chat.id,cb.message.id);row=self.panels.get(key);now=time.monotonic()
            if not row or now-row[3]>3600 or row[0]!=cb.from_user.id:
                await cb.answer('ကိုယ်တိုင် /start ခေါ်ပြီး သုံးပါ။',show_alert=True);return
            if now-row[4]<3:await cb.answer('ခဏစောင့်ပြီး ပြန်စစ်ပါ။');return
            await cb.answer()
            self.panels[key]=(*row[:4],now,row[5])
            help_page=cb.data=='home:help'
            if not help_page:await BANDWIDTH.refresh()
            text=escape(HELP_TEXT) if help_page else self.text(row[5])
            try:
                # Telegram photo captions have a smaller limit than text messages.
                # Help is long, so edit the same card to a concise command guide.
                if row[2] and help_page:
                    text=('<b>မုဒြာ အသုံးပြုနည်း</b>\n\n'
                          '▸ /play သီချင်းအမည် — VC မှာ ဖွင့်မယ်\n'
                          '▸ /search — ရလဒ်ရွေးမယ်\n'
                          '▸ /findsong — Video/Voice ကို reply ပြီး ရှာမယ်\n'
                          '▸ /join — VC ဝင်မယ်\n'
                          '▸ /radio — ရေဒီယိုလိုင်း ရွေးမယ်\n'
                          '▸ /pause /resume /skip /stop\n'
                          '▸ /queue /loop /shuffle /clear\n'
                          '▸ /လူတောင်းပွဲ /welcome\n\n'
                          'Effects၊ Quality၊ Lyrics ကို သီချင်းကဒ်မှာ သုံးပါ။\n'
                          'Private chat မှာ သီချင်းနာမည် ရိုက်ပြီး MP3 ရှာပါ။\n'
                          'Command အပြည့်အစုံအတွက် /cmd သုံးပါ။')
                edit=cb.message.edit_caption if row[2] else cb.message.edit_text
                await edit(text,reply_markup=self.keyboard(row[1],help_page),parse_mode=ParseMode.HTML)
            except Exception as exc:
                if type(exc).__name__!='MessageNotModified':
                    # Keep the original card usable after transient Telegram errors.
                    self.panels[key]=row
