"""Reuse this bot's uploaded photo IDs; bounded RAM metadata, no disk files."""
from collections import OrderedDict
import asyncio

class TelegramPhotoCache:
    def __init__(self):self.items=OrderedDict();self.lock=asyncio.Lock()
    async def send(self,bot,chat,key,original,**kwargs):
        async with self.lock:
            cached=self.items.get(key)
            if cached:
                try:
                    sent=await bot.send_photo(chat,photo=cached,**kwargs)
                    self.items.move_to_end(key)
                    return sent
                except Exception as exc:
                    if type(exc).__name__ not in {'FileIdInvalid','FileReferenceExpired','FileReferenceEmpty','MediaEmpty'}:raise
                    self.items.pop(key,None)
            sent=await bot.send_photo(chat,photo=original() if callable(original) else original,**kwargs)
            file_id=getattr(getattr(sent,'photo',None),'file_id',None)
            if file_id:
                self.items[key]=file_id
                while len(self.items)>32:self.items.popitem(last=False)
            return sent
