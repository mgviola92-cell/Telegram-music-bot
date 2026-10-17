"""Low-priority Telegram lyrics: metadata and text only, never audio downloads."""
import asyncio
import logging
import re
import time
from bot_source_probe import select_song
from music_ui import clean_track_title

LOG=logging.getLogger('telegram_music')

class LyricsResult(tuple):
    def __new__(cls,text,title,source):
        result=super().__new__(cls,(text,title));result.source=source;return result

def lyric_button(message):
    for row in getattr(getattr(message,'reply_markup',None),'inline_keyboard',[]) or []:
        for button in row:
            label=str(getattr(button,'text','')).casefold()
            if ('lyrics' in label or 'စာသား' in label) and getattr(button,'callback_data',None):return button.text
    return None

def lyric_text(message):
    # Accept actual multi-line text, never a song caption, service prompt or URL.
    text=str(getattr(message,'text',None) or '').strip()
    if not 80<=len(text)<=16000 or len(text.splitlines())<4:return None
    lowered=text.casefold()
    if any(x in lowered for x in ('lyrics not found','no lyrics','subscribe','http://','https://','searching','စာသားမတွေ့')):return None
    return text

class TelegramLyrics:
    def __init__(self,source,busy=lambda:False):
        self.source,self.busy=source,busy;self.tasks=set();self.pause_until=0;self.cooldown=0
    def interrupt(self):
        self.pause_until=time.monotonic()+15
        for task in tuple(self.tasks):task.cancel()
    async def find(self,title,duration=None,*,timeout=8):
        source=self.source
        if (source is None or self.tasks or self.busy() or source.lock.locked()
                or time.monotonic()<max(self.pause_until,self.cooldown)):return None
        task=asyncio.current_task();self.tasks.add(task)
        try:
            async with asyncio.timeout(min(24,max(1,timeout))):
                from lyrics_service import words
                wanted=words(title)
                candidate=next((row[0] for row in list(getattr(source,'candidates',{}).values())
                    if row[1]>time.monotonic() and words(clean_track_title(row[0].label))==wanted),None)
                if candidate is None:candidate=await source.find(title)
                LOG.info('Lyrics Telegram metadata ready source=%s',source.username)
                from lyrics_service import words
                wanted=words(title);actual=words(candidate.label)
                if not wanted or len(wanted&actual)/len(wanted)<.8:return None
                if duration and candidate.duration and abs(duration-candidate.duration)>max(12,duration*.1):return None
                # Another music request may have acquired the source after metadata search.
                if self.busy() or source.lock.locked():return None
                await source.lock.acquire()
                try:
                    message=candidate.message
                    fresh=await source.client.get_messages(source.peer.id,message.id)
                    label=lyric_button(fresh)
                    if not label:
                        LOG.info('Lyrics Telegram no callback button source=%s',source.username);return None
                    before=[m.id async for m in source.client.get_chat_history(source.peer.id,limit=5)]
                    cutoff=max(before+[message.id])
                    # Some bots edit/send lyrics but never answer the callback RPC.
                    # Poll concurrently, as the audio-source selector already does.
                    callback=asyncio.create_task(select_song(source.client,fresh,label))
                    seen=set()
                    try:
                        while True:
                            if callback.done() and not callback.cancelled():
                                try:
                                    answer=callback.result()
                                    text=lyric_text(type('Answer',(),{'text':getattr(answer,'message',None)})())
                                    if text:return LyricsResult(text,clean_track_title(candidate.label),'Shazam')
                                except Exception:pass
                            edited=await source.client.get_messages(source.peer.id,message.id)
                            text=lyric_text(edited)
                            if text:return LyricsResult(text,clean_track_title(candidate.label),'Shazam')
                            async for reply in source.client.get_chat_history(source.peer.id,limit=6):
                                if reply.id<=cutoff or getattr(reply,'outgoing',False):continue
                                reply_id=getattr(reply,'reply_to_message_id',None)
                                if reply_id and reply_id!=message.id:continue
                                text=lyric_text(reply)
                                if text:return LyricsResult(text,clean_track_title(candidate.label),'Shazam')
                                if reply.id not in seen:
                                    seen.add(reply.id)
                                    LOG.info('Lyrics Telegram response text_chars=%s caption_chars=%s',len(str(getattr(reply,'text','') or '')),len(str(getattr(reply,'caption','') or '')))
                            await asyncio.sleep(.6)
                    finally:
                        if not callback.done():callback.cancel()
                        await asyncio.gather(callback,return_exceptions=True)
                finally:source.lock.release()
        except asyncio.CancelledError:raise
        except Exception as exc:
            LOG.info('Lyrics Telegram unavailable source=%s reason=%s',source.username,type(exc).__name__)
            self.cooldown=time.monotonic()+60
            return None
        finally:self.tasks.discard(task)
