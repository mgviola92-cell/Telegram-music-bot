"""Bounded recorded greeting ingestion and PCM reuse."""
import asyncio
import io
from collections import OrderedDict
from welcome_audio import BYTES_PER_SECOND, MAX_SECONDS
MAX_UPLOAD=5*1024*1024
async def run_audio(args,data,timeout=10):
    p=await asyncio.create_subprocess_exec('ffmpeg','-nostdin','-v','error',*args,
        stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    try:
        output,_=await asyncio.wait_for(p.communicate(data),timeout)
        if p.returncode or not output:raise ValueError('အသံဖိုင်ကို ဖတ်မရဘူး။')
        return output
    finally:
        if p.returncode is None:p.kill();await p.wait()
async def decode_recording(data):
    if not 0<len(data)<=MAX_UPLOAD:raise ValueError('အသံဖိုင်ကို 5 MB အောက် ပို့ပါ။')
    pcm=await run_audio(['-protocol_whitelist','file,pipe','-format_whitelist','mp3,ogg,wav,flac,mov,aac,matroska,webm',
        '-i','pipe:0','-t',str(MAX_SECONDS+.1),'-vn','-ar','48000','-ac','2','-f','s16le','pipe:1'],data)
    if not BYTES_PER_SECOND//4<=len(pcm)<=MAX_SECONDS*BYTES_PER_SECOND:
        raise ValueError('အသံကို ၀.၂၅–၄၀ စက္ကန့်အတွင်း ပို့ပါ။')
    return pcm
async def encode_recording(pcm):
    return await run_audio(['-f','s16le','-ar','48000','-ac','2','-i','pipe:0',
        '-c:a','libmp3lame','-b:a','96k','-f','mp3','pipe:1'],pcm)
async def fetch_bytes(bot,source,size=None):
    if size is not None and not 0<size<=MAX_UPLOAD:raise ValueError('အသံဖိုင်ကို 5 MB အောက် ပို့ပါ။')
    result=bytearray()
    async def fetch():
        async for chunk in bot.stream_media(source,limit=6):
            result.extend(chunk)
            if len(result)>MAX_UPLOAD:raise ValueError('ဖိုင်ကြီးလွန်းတယ်။')
    await asyncio.wait_for(fetch(),15)
    if not result or (size is not None and len(result)!=size):raise ValueError('ဖိုင်အပြည့် မရသေးဘူး။')
    return bytes(result)
class RecordingCache:
    def __init__(self,bot):self.bot=bot;self.items=OrderedDict();self.lock=asyncio.Lock()
    def put(self,key,pcm):
        self.items[key]=pcm;self.items.move_to_end(key)
        while len(self.items)>8 or sum(map(len,self.items.values()))>8*1024*1024:self.items.popitem(last=False)
    async def get(self,key):
        if key in self.items:self.items.move_to_end(key);return self.items[key]
        async with self.lock:
            if key in self.items:return self.items[key]
            pcm=await decode_recording(await fetch_bytes(self.bot,key));self.put(key,pcm);return pcm
async def ingest_recording(bot,msg,key,store,user,cache):
    media=getattr(msg,'voice',None) or getattr(msg,'audio',None) or getattr(msg,'document',None)
    if media is None:raise ValueError('Voice message ဒါမှမဟုတ် အသံဖိုင် ပို့ပါ။')
    pcm=await decode_recording(await fetch_bytes(bot,msg,media.file_size))
    encoded=await encode_recording(pcm)
    pcm=await decode_recording(encoded)
    file=io.BytesIO(encoded);file.name='mudyar_greeting.mp3'
    sent=await bot.send_audio(user,file,title='မုဒြာ · ကိုယ်ပိုင်ကြိုဆိုအသံ',duration=round(len(pcm)/BYTES_PER_SECOND))
    if not sent.audio:raise RuntimeError('No Telegram audio identifier')
    await store.set_recording(user,key,sent.audio.file_id,len(pcm)/BYTES_PER_SECOND)
    cache.put(sent.audio.file_id,pcm);return len(pcm)/BYTES_PER_SECOND
