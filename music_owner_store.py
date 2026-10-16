"""Music-only owner settings persisted as one private Saved Messages document."""
import asyncio
import copy
import hmac
import io
import json
import logging
import os
import string
import re
import time
import tempfile
from pathlib import Path
from card_layout import validate as validate_card
from pyrogram.types import InputMediaDocument
from pyrogram.enums import ParseMode

log=logging.getLogger('telegram_music')
TAG='MUDYAR_MUSIC_SETTINGS_V1'
FILE='mudyar_music_settings.json'
MAX_BYTES=128*1024
LABELS={'vc':'ပုံမှန် VC ကြိုဆိုစာ','group':'Group လူသစ်ကြိုဆိုစာ',
        'party':'လူတောင်းပွဲ အရှည်မိတ်ဆက်','party_batch':'အစုလိုက် အတိုကြိုဆိုစာ'}
DEFAULTS={
 'vc':'မင်္ဂလာပါ၊ {name}။ အသံခန်းလေးထဲကို ကြိုဆိုပါတယ်။',
 'group':'မင်္ဂလာပါ၊ {name}။ ဂရုလေးထဲကို ကြိုဆိုပါတယ်။',
 'party':'မင်္ဂလာပါ။ လူတောင်းပွဲလေးကနေ ကြိုဆိုပါတယ်။ ဒီအသံခန်းလေးထဲမှာ လူဝင်တောင်းလို့ ရပါတယ်။ '
         'ကိုယ်တိုင် ဝင်ပြောချင်ရင်လည်း အက်မင်တွေဆီ စကားပြောခွင့် တောင်းပြီး ဝင်ပြောလို့ ရပါတယ်။ '
         'အားလုံး ပျော်ပျော်ပါးပါး ပါဝင်ကြပါဦး။',
 'party_batch':'အခုဝင်လာကြတဲ့ သူငယ်ချင်းအားလုံး မင်္ဂလာပါ။ လူတောင်းပွဲလေးကနေ ကြိုဆိုပါတယ်။'}


def validate_text(kind,text):
    if kind not in DEFAULTS or not isinstance(text,str):
        raise ValueError('Unknown greeting')
    text=text.strip()
    limit=600 if kind=='party' else 300
    if not text or len(text)>limit or any(ord(c)<32 and c not in '\n\t' for c in text):
        raise ValueError(f'စာသားကို ၁–{limit} စာလုံးအတွင်း ရေးပါ။')
    try:
        for _,field,spec,conversion in string.Formatter().parse(text):
            if field is not None and (field!='name' or spec or conversion or kind in {'party','party_batch'}):
                raise ValueError('နာမည်နေရာအတွက် {name} ကို ပုံမှန်ကြိုဆိုစာမှာပဲ သုံးနိုင်တယ်။')
    except ValueError:
        raise ValueError('ပုံမှန်ကြိုဆိုစာမှာ {name} ပဲ သုံးနိုင်တယ်။ အရှည်နဲ့ အစုလိုက်စာမှာ နာမည်မထည့်ပါနဲ့။') from None
    return text


MAX_RECORDINGS=10

def recording_choices(data,key):
    clip=data.get('audio',{}).get(key)
    return [clip]+list(clip.get('variants',[])) if clip else []


def parse_state(raw):
    if len(raw)>MAX_BYTES:
        raise ValueError('Owner state too large')
    d=json.loads(raw)
    if not isinstance(d,dict) or d.get('v')!=1:
        raise ValueError('Invalid owner state')
    owner=d.get('owner')
    if owner is not None and (type(owner) is not int or owner<=0):
        raise ValueError('Invalid owner identity')
    texts=d.get('texts',{})
    if not isinstance(texts,dict) or len(texts)>4:
        raise ValueError('Invalid templates')
    for key,value in texts.items():validate_text(key,value)
    groups=d.get('groups',{})
    if not isinstance(groups,dict) or len(groups)>200:
        raise ValueError('Invalid group list')
    for key,title in groups.items():
        if not key.startswith('-') or not key[1:].isdigit() or not isinstance(title,str) or len(title)>70:
            raise ValueError('Invalid group')
    if d.get('voice','male') not in {'male','female'}:
        raise ValueError('Invalid voice')
    audio=d.get('audio',{});modes=d.get('audio_modes',{})
    if not isinstance(audio,dict) or len(audio)>4 or not isinstance(modes,dict) or len(modes)>4:
        raise ValueError('Invalid recording settings')
    for key,clip in audio.items():
        if key not in DEFAULTS or not isinstance(clip,dict):raise ValueError('Invalid recording')
        variants=clip.get('variants',[])
        if not isinstance(variants,list) or len(variants)>=MAX_RECORDINGS:raise ValueError('Too many recordings')
        seen=set()
        for entry in [clip]+variants:
            if (not isinstance(entry,dict) or not isinstance(entry.get('file_id'),str)
                    or not 1<=len(entry['file_id'])<=2000 or type(entry.get('seconds')) not in {int,float}
                    or not .25<=entry['seconds']<=40 or entry['file_id'] in seen):raise ValueError('Invalid recording')
            seen.add(entry['file_id'])
    for key,mode in modes.items():
        if key not in DEFAULTS or mode not in {'tts','clip','name_clip'}:raise ValueError('Invalid audio mode')
        if mode!='tts' and key not in audio:raise ValueError('Missing recording')
        if mode=='name_clip' and (key not in {'vc','group'} or any(c['seconds']>30 for c in recording_choices({'audio':audio},key))):raise ValueError('Invalid named recording')
    broadcast=d.get('broadcast')
    if broadcast is not None and (owner is None or not isinstance(broadcast,dict) or broadcast.get('chat_id')!=owner
            or type(broadcast.get('message_id')) is not int or broadcast['message_id']<=0):raise ValueError('Invalid broadcast source')
    photo=d.get('default_photo')
    if photo is not None and (not isinstance(photo,str) or not re.fullmatch(r'[A-Za-z0-9_-]{10,2048}',photo)):
        raise ValueError('Invalid default photo')
    cards=d.get('cards',{})
    if not isinstance(cards,dict) or len(cards)>3:raise ValueError('Invalid cards')
    for key,value in cards.items():validate_card(key,value)
    revision=d.get('revision',0)
    if type(revision) is not int or revision<0:raise ValueError('Invalid revision')
    return {'v':1,'owner':owner,'texts':texts,'groups':groups,'voice':d.get('voice','male'),'revision':revision,'audio':audio,'audio_modes':modes,'broadcast':broadcast,'cards':cards,'default_photo':photo}


class MusicOwnerStore:
    def __init__(self,client,setup_code=None):
        self.client=client
        self.setup_code=setup_code if setup_code is not None else os.getenv('MUSIC_OWNER_SETUP_CODE','')
        self.data={'v':1,'owner':None,'texts':{},'groups':{},'voice':'male','revision':0,'audio':{},'audio_modes':{},'broadcast':None}
        self.ready=False
        self.message_id=None
        self.lock=asyncio.Lock()
        self.observed={}
        self.flush_task=None
        self.attempts={}

    async def load(self):
        try:
            async def fetch():
                async for msg in self.client.search_messages('me',query=TAG,limit=8):
                    if (msg.caption or '')!=TAG or not msg.document or msg.document.file_name!=FILE:
                        continue
                    if msg.document.file_size>MAX_BYTES:
                        raise ValueError('Owner document too large')
                    f=await self.client.download_media(msg,in_memory=True)
                    if f is None:
                        raise RuntimeError('No owner state download')
                    try:
                        f.seek(0)
                        raw=f.read(MAX_BYTES+1)
                    finally:f.close()
                    self.data=parse_state(raw)
                    self.message_id=msg.id
                    break
            await asyncio.wait_for(fetch(),15)
            self.ready=True
            log.info('Music owner settings loaded configured=%s templates=%s groups=%s',
                     bool(self.data['owner']),len(self.data['texts']),len(self.data['groups']))
        except Exception as exc:
            self.ready=False
            log.warning('Music owner settings unavailable; owner controls locked (%s)',type(exc).__name__)

    def authorized(self,user):
        return self.ready and bool(user) and self.data['owner']==user

    def text(self,key,name=''):
        return self.data['texts'].get(key,DEFAULTS[key]).format(name=name)

    async def _persist(self,data):
        data=copy.deepcopy(data)
        data['revision']=self.data.get('revision',0)+1
        data['groups'].update(self.observed)
        data['groups']=dict(list(data['groups'].items())[-200:])
        raw=json.dumps(data,ensure_ascii=False,separators=(',',':')).encode()
        parse_state(raw)
        f=io.BytesIO(raw);f.name=FILE
        if self.message_id is None:
            msg=await asyncio.wait_for(self.client.send_document('me',f,caption=TAG,parse_mode=ParseMode.DISABLED,disable_notification=True),15)
            new_id=msg.id
        else:
            try:
                # pyrogrammod 2.4.1's document edit guesses MIME from a path,
                # even when InputMediaDocument accepts BytesIO. Stage privately.
                with tempfile.TemporaryDirectory(prefix='mudyar_owner_') as folder:
                    path=Path(folder)/FILE
                    path.write_bytes(raw)
                    path.chmod(0o600)
                    await asyncio.wait_for(self.client.edit_message_media('me',self.message_id,
                        InputMediaDocument(str(path),caption=TAG,parse_mode=ParseMode.DISABLED),file_name=FILE),15)
            except Exception as exc:
                if type(exc).__name__!='MessageNotModified':raise
            new_id=self.message_id
        self.data=data
        self.message_id=new_id
        # Entries observed while the RPC awaited still need another flush.
        for key,value in list(self.observed.items()):
            if data['groups'].get(key)==value:self.observed.pop(key,None)

    async def claim(self,user,code):
        if not self.ready or not user or not self.setup_code or len(self.setup_code)<20:
            return False
        now=time.monotonic()
        stamp,count=self.attempts.get(user,(now,0))
        if now-stamp>600:stamp,count=now,0
        self.attempts[user]=(stamp,count+1)
        if len(self.attempts)>128:self.attempts.pop(next(iter(self.attempts)))
        if count>=5 or not hmac.compare_digest(str(code),self.setup_code):
            return False
        async with self.lock:
            if self.data['owner'] is not None:return False
            data=copy.deepcopy(self.data);data['owner']=int(user)
            await self._persist(data)
            self.setup_code=''  # One-time claim: persisted owner identity is now authoritative.
            return True

    async def set_text(self,user,key,text):
        text=validate_text(key,text)
        async with self.lock:
            if not self.authorized(user):raise PermissionError('Owner only')
            data=copy.deepcopy(self.data);data['texts'][key]=text
            await self._persist(data)

    async def reset_text(self,user,key):
        if key not in DEFAULTS:raise ValueError('Unknown greeting')
        async with self.lock:
            if not self.authorized(user):raise PermissionError('Owner only')
            data=copy.deepcopy(self.data);data['texts'].pop(key,None)
            await self._persist(data)

    async def set_default_photo(self,user,file_id=None):
        async with self.lock:
            if not self.authorized(user):raise PermissionError('Owner only')
            data=copy.deepcopy(self.data);data['default_photo']=file_id
            await self._persist(data)

    async def set_card(self,user,key,text=None):
        from card_layout import DEFAULTS as CARD_DEFAULTS
        if key not in CARD_DEFAULTS:raise ValueError('Invalid card')
        if text is not None:text=validate_card(key,text)
        async with self.lock:
            if not self.authorized(user):raise PermissionError('Owner only')
            data=copy.deepcopy(self.data)
            cards=data.setdefault('cards',{})
            if text is None:cards.pop(key,None)
            else:cards[key]=text
            await self._persist(data)

    async def set_voice(self,user,voice):
        if voice not in {'male','female'}:raise ValueError('Invalid voice')
        async with self.lock:
            if not self.authorized(user):raise PermissionError('Owner only')
            data=copy.deepcopy(self.data);data['voice']=voice
            await self._persist(data)

    async def set_recording(self,user,key,file_id,seconds):
        async with self.lock:
            if not self.authorized(user):raise PermissionError('Owner only')
            data=copy.deepcopy(self.data)
            rows=recording_choices(data,key)
            if len(rows)>=MAX_RECORDINGS:raise ValueError('အသံ ၁၀ ဖိုင် ပြည့်နေပြီ။ အဟောင်းတစ်ဖိုင် အရင်ဖြုတ်ပါ။')
            new={'file_id':file_id,'seconds':seconds}
            if rows:
                data['audio'][key].setdefault('variants',[]).append(new)
            else:data['audio'][key]=new
            if data['audio_modes'].get(key)!='name_clip' or seconds>30:data['audio_modes'][key]='clip'
            await self._persist(data)
    async def set_audio_mode(self,user,key,mode):
        async with self.lock:
            if not self.authorized(user):raise PermissionError('Owner only')
            data=copy.deepcopy(self.data);data['audio_modes'][key]=mode;await self._persist(data)
    async def remove_recording(self,user,key,file_id=None):
        async with self.lock:
            if not self.authorized(user):raise PermissionError('Owner only')
            data=copy.deepcopy(self.data)
            rows=[{'file_id':c['file_id'],'seconds':c['seconds']} for c in recording_choices(data,key) if c['file_id']!=file_id] if file_id else []
            if rows:
                data['audio'][key]=rows[0]
                if len(rows)>1:data['audio'][key]['variants']=rows[1:]
            else:data['audio'].pop(key,None);data['audio_modes'].pop(key,None)
            await self._persist(data)
    async def set_broadcast(self,user,message_id):
        async with self.lock:
            if not self.authorized(user):raise PermissionError('Owner only')
            data=copy.deepcopy(self.data);data['broadcast']={'chat_id':user,'message_id':message_id}
            await self._persist(data)

    def observe_group(self,chat):
        if not self.ready or chat.id>=0:return
        key=str(chat.id);title=(chat.title or 'Group')[:70]
        if self.data['groups'].get(key)==title and key not in self.observed:return
        self.observed[key]=title
        if len(self.observed)>200:self.observed.pop(next(iter(self.observed)))
        if self.data['owner'] and (self.flush_task is None or self.flush_task.done()):
            self.flush_task=asyncio.create_task(self._flush_later())

    async def _flush_later(self):
        await asyncio.sleep(20)
        try:
            async with self.lock:
                if self.observed:await self._persist(self.data)
        except Exception as exc:
            log.info('Owner group list save deferred (%s)',type(exc).__name__)

    async def probe_roundtrip(self):
        if not self.ready:return
        try:
            async with self.lock:
                await self._persist(self.data)
                await self._persist(self.data)  # Verify media replacement as well as first creation.
                check=MusicOwnerStore(self.client,setup_code='')
                await check.load()
                if not check.ready or check.data!=self.data:
                    raise RuntimeError('Owner settings roundtrip mismatch')
                log.info('Music owner persistence probe PASS configured=%s revision=%s',bool(self.data['owner']),self.data['revision'])
        except Exception as exc:
            log.warning('Music owner persistence probe unavailable (%s)',type(exc).__name__)

    async def close(self):
        if self.flush_task and not self.flush_task.done():
            self.flush_task.cancel()
            await asyncio.gather(self.flush_task,return_exceptions=True)
        if self.ready and self.data['owner'] and self.observed:
            try:
                async with self.lock:await self._persist(self.data)
            except Exception:pass
