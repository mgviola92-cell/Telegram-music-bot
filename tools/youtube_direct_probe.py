"""Manual, bounded YouTube audio diagnostic. Never imported by the bot.
No URLs, request headers, cookies or tokens are printed; only result summaries.
"""
import argparse
import json
import os
import subprocess
import time
from urllib.parse import urlsplit


def probe(mode, url, cookiefile=None, system_ca=False):
    from yt_dlp import YoutubeDL
    messages=[]
    class Logger:
        def debug(self,msg):
            if 'Generating a ' in msg and 'PO Token' in msg:messages.append('pot_requested')
        def warning(self,msg):
            if 'PO Token' in msg:messages.append('pot_warning')
        def error(self,msg):pass
    options=dict(quiet=True,no_warnings=True,logger=Logger(),noplaylist=True,
                 skip_download=True,format='bestaudio/best',socket_timeout=8,
                 retries=0,extractor_retries=0,js_runtimes={'node':{}},
                 cachedir=False)
    if system_ca:options['compat_opts']={'no-certifi'}
    if mode=='pot':options['extractor_args']={'youtube':{'player_client':['mweb']},
        'youtubepot-bgutilhttp':{'base_url':['http://127.0.0.1:4416']}}
    if mode=='cookies':
        if not cookiefile or not os.path.isfile(cookiefile):return {'mode':mode,'result':'not_tested_no_user_cookies'}
        options['cookiefile']=cookiefile
    started=time.monotonic()
    row={'mode':mode,'result':'unavailable'}
    try:
        with YoutubeDL(options) as ydl:
            info=ydl.extract_info(url,download=False)
        stream=info.get('url')
        if not stream or urlsplit(stream).scheme!='https':raise ValueError('No HTTPS audio URL')
        row.update(metadata=True,audio_only=info.get('vcodec')=='none',
                   format_id=info.get('format_id'),protocol=info.get('protocol'))
        if not row['audio_only']:raise ValueError('Selected format includes video')
        headers=''.join(f'{key}: {value}\r\n' for key,value in info.get('http_headers',{}).items()
                        if '\r' not in str(value) and '\n' not in str(value))
        cmd=['ffmpeg','-nostdin','-v','error','-rw_timeout','5000000']
        # Local trusted CA, not disabling TLS verification.
        cmd+=['-tls_verify','1']
        ca=os.environ.get('SSL_CERT_FILE')
        if system_ca and ca:cmd+=['-ca_file',ca]
        if headers:cmd+=['-headers',headers]
        cmd+=['-i',stream,'-t','2','-vn','-ac','1','-ar','16000','-f','s16le','pipe:1']
        audio=subprocess.run(cmd,capture_output=True,timeout=18)
        row['decoded_pcm_bytes']=len(audio.stdout)
        error=audio.stderr.decode(errors='replace').casefold()
        row['media_error']=('http_403' if '403' in error else 'http_429' if '429' in error else
                            'tls_certificate' if 'certificate' in error else
                            'network' if 'connection' in error or 'resolve' in error else
                            'invalid_media' if 'invalid data' in error else 'none' if not error else 'ffmpeg_error')
        row['result']='decoded_audio' if audio.returncode==0 and len(audio.stdout)>=16000 else 'media_decode_failed'
    except Exception as exc:
        # Do not expose signed CDN URLs or cookie/session material in exceptions.
        message=str(exc).casefold()
        row['error']=('verification_required' if 'confirm' in message and 'bot' in message else
                      'tls_certificate' if 'certificate' in message else
                      'unavailable_video' if 'video is unavailable' in message else type(exc).__name__)
    row.update(seconds=round(time.monotonic()-started,2),pot_requested='pot_requested' in messages,
               pot_warning='pot_warning' in messages)
    return row


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--mode',choices=['runtime','pot','cookies'],required=True)
    p.add_argument('--url',default='https://www.youtube.com/watch?v=aqz-KE-bpKQ')
    p.add_argument('--cookie-file');p.add_argument('--system-ca',action='store_true')
    args=p.parse_args()
    print(json.dumps(probe(args.mode,args.url,args.cookie_file,args.system_ca)),flush=True)
