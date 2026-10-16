"""Opt-in remote separation only. No model runs on the Music Bot host."""
import asyncio
import json
import os
import re
import shutil
from pathlib import Path
from urllib.parse import urlsplit
from aiohttp import ClientSession,ClientTimeout,FormData

MAX_INPUT=20*1024*1024
MAX_OUTPUT=64*1024*1024
HF_BASE='https://modulsx-bs-roformer-leap-audio-separator.hf.space'

class KaraokeUnavailable(RuntimeError):pass
class KaraokeQuota(KaraokeUnavailable):pass

def modal_url(value):
    p=urlsplit(value)
    if (p.scheme!='https' or not p.hostname or not p.hostname.endswith('.modal.run')
            or p.username or p.password or p.port not in (None,443) or p.query or p.fragment):
        raise ValueError('Invalid Modal endpoint')
    return value.rstrip('/')

async def read_bounded(response,limit):
    body=bytearray()
    async for chunk in response.content.iter_chunked(65536):
        body.extend(chunk)
        if len(body)>limit:raise KaraokeUnavailable('Response too large')
    return bytes(body)

async def response_json(response):
    if response.status not in (200,201):raise KaraokeUnavailable('Remote service unavailable')
    return json.loads(await read_bounded(response,1024*1024))

class HFSeparator:
    name='hf'
    def __init__(self,token=''):self.token=token
    async def separate(self,path,destination):
        # Authentication uses the owner's legitimate free quota; never rotate identities.
        headers={'Authorization':'Bearer '+self.token,'x-hf-authorization':'Bearer '+self.token} if self.token else {}
        async with ClientSession(timeout=ClientTimeout(total=210),headers=headers) as session:
            with open(path,'rb') as source:
                form=FormData();form.add_field('files',source,filename='input'+Path(path).suffix,content_type='application/octet-stream')
                async with session.post(HF_BASE+'/gradio_api/upload',data=form,allow_redirects=False) as r:
                    uploaded=await response_json(r)
            if not isinstance(uploaded,list) or not uploaded or not isinstance(uploaded[0],str):raise KaraokeUnavailable('Invalid upload')
            payload={'data':[{'path':uploaded[0],'meta':{'_type':'gradio.FileData'}},'Standard — Instruments']}
            async with session.post(HF_BASE+'/gradio_api/call/separate',json=payload,allow_redirects=False) as r:event=await response_json(r)
            event_id=event.get('event_id','')
            if not isinstance(event_id,str) or not re.fullmatch('[a-f0-9]{16,64}',event_id):raise KaraokeUnavailable('Invalid event')
            result=None;kind='';size=0
            async with session.get(HF_BASE+'/gradio_api/call/separate/'+event_id,allow_redirects=False) as r:
                if r.status!=200:raise KaraokeUnavailable('Remote queue unavailable')
                async for raw in r.content:
                    size+=len(raw)
                    if size>1024*1024:raise KaraokeUnavailable('Queue response too large')
                    line=raw.decode().strip()
                    if line.startswith('event:'):kind=line[6:].strip()
                    elif line.startswith('data:'):
                        data=json.loads(line[5:])
                        if kind=='error':
                            if 'quota' in str(data).casefold():raise KaraokeQuota('Free quota unavailable')
                            raise KaraokeUnavailable('Separation failed')
                        if kind=='complete':result=data;break
            if not isinstance(result,list) or not result or not isinstance(result[0],dict):raise KaraokeUnavailable('No instrumental result')
            url=result[0].get('url','')
            # Do not forward credentials to redirects, arbitrary output hosts or user URLs.
            if not isinstance(url,str) or not url.startswith(HF_BASE+'/gradio_api/file='):raise KaraokeUnavailable('Untrusted output URL')
            async with session.get(url,allow_redirects=False) as r:
                if r.status!=200:raise KaraokeUnavailable('Output unavailable')
                await save_bounded(r,destination,MAX_OUTPUT)
        return str(destination)

async def save_bounded(response,destination,limit):
    size=0
    try:
        with open(destination,'wb') as output:
            async for chunk in response.content.iter_chunked(65536):
                size+=len(chunk)
                if size>limit:raise KaraokeUnavailable('Output too large')
                output.write(chunk)
        if not size:raise KaraokeUnavailable('Empty output')
    except BaseException:
        Path(destination).unlink(missing_ok=True);raise

class ModalSeparator:
    name='modal'
    def __init__(self,url,key,secret,free_confirmed=False):
        if not free_confirmed:raise ValueError('Modal spend cap must be confirmed before use')
        self.url=modal_url(url);self.key=key;self.secret=secret
        if not key or not secret:raise ValueError('Modal proxy credentials missing')
    async def separate(self,path,destination):
        headers={'Modal-Key':self.key,'Modal-Secret':self.secret,'Content-Type':'application/octet-stream'}
        async with ClientSession(timeout=ClientTimeout(total=210),headers=headers) as session:
            with open(path,'rb') as source:
                async with session.post(self.url,data=source,allow_redirects=False) as r:
                    if r.status in (402,429):raise KaraokeQuota('Free budget exhausted')
                    if r.status!=200 or not r.content_type.startswith('audio/'):raise KaraokeUnavailable('Modal separation unavailable')
                    await save_bounded(r,destination,MAX_OUTPUT)
        return str(destination)

def configured_provider():
    if os.getenv('MUSIC_KARAOKE_ENABLED','0')!='1':return None
    if os.getenv('KARAOKE_PROVIDER','hf')=='hf':
        token=os.getenv('HF_TOKEN','')
        return HFSeparator(token) if token else None
    if os.getenv('KARAOKE_PROVIDER')=='modal':
        try:return ModalSeparator(os.getenv('KARAOKE_MODAL_URL',''),os.getenv('KARAOKE_MODAL_KEY',''),os.getenv('KARAOKE_MODAL_SECRET',''),os.getenv('KARAOKE_MODAL_FREE_CONFIRMED')=='1')
        except ValueError:return None
    return None

async def audio_command(args,timeout=45):
    proc=await asyncio.create_subprocess_exec(*args,stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
    try:
        await asyncio.wait_for(proc.wait(),timeout)
        if proc.returncode:raise KaraokeUnavailable('Media preparation failed')
    finally:
        if proc.returncode is None:proc.kill();await proc.wait()

async def inspect_audio(path,limit=MAX_INPUT):
    p=Path(path)
    if not p.is_file() or not 0<p.stat().st_size<=limit:raise KaraokeUnavailable('Invalid media size')
    proc=await asyncio.create_subprocess_exec('ffprobe','-v','error','-protocol_whitelist','file,pipe',
        '-format_whitelist','mp3,ogg,wav,flac,mov,aac,matroska,webm','-show_entries','format=duration','-of','json',str(p),stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
    try:
        output,_=await asyncio.wait_for(proc.communicate(),6)
        seconds=float(json.loads(output)['format']['duration'])
        if proc.returncode or not .25<=seconds<=300:raise KaraokeUnavailable('Karaoke supports up to five minutes')
        return seconds
    finally:
        if proc.returncode is None:proc.kill();await proc.wait()

async def prepare_input(source,directory):
    destination=Path(directory)/'input.mka'
    if not source.startswith(('https://','http://')):
        original=Path(source)
        if not original.is_file() or original.stat().st_size>MAX_INPUT:raise KaraokeUnavailable('Source unavailable')
        destination=Path(directory)/('input'+original.suffix)
        await asyncio.to_thread(shutil.copyfile,original,destination)
    else:
        from player_visuals import allowed_audio_cdn,stream_headers_for
        if not allowed_audio_cdn(source):raise KaraokeUnavailable('Unsupported streaming source')
        args=['ffmpeg','-nostdin','-v','error','-protocol_whitelist','https,http,tls,tcp,crypto']
        headers=stream_headers_for(source)
        if headers:args+=['-headers',''.join(k+': '+v+'\r\n' for k,v in headers.items() if '\r' not in k+v and '\n' not in k+v)]
        args+=['-i',source,'-t','301','-vn','-c:a','copy','-fs',str(MAX_INPUT+1),'-y',str(destination)]
        await audio_command(args,60)
    return str(destination),await inspect_audio(destination)

async def encode_result(source,destination,expected):
    seconds=await inspect_audio(source,MAX_OUTPUT)
    if abs(seconds-expected)>max(3,expected*.03):raise KaraokeUnavailable('Output duration mismatch')
    await audio_command(['ffmpeg','-nostdin','-v','error','-protocol_whitelist','file,pipe','-i',str(source),'-vn','-ar','44100','-ac','2','-c:a','libmp3lame','-b:a','128k','-fs',str(MAX_INPUT+1),'-y',str(destination)],45)
    await inspect_audio(destination)
    return str(destination)
