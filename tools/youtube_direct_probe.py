"""Manual, bounded YouTube audio diagnostic. Never imported by the bot.
No URLs, request headers, cookies or tokens are printed; only result summaries.
"""
import argparse
import json
import os
import subprocess
import time
import tempfile
from http.cookiejar import MozillaCookieJar
from urllib.parse import urlsplit


def install_samsung_client():
    """Upstream client config, isolated to this diagnostic child process.

    Source: yt-dlp 897341241f2bfb2439fe0367f4c7c9ccd29a3828,
    yt_dlp/extractor/youtube/_base.py (Unlicense). No production patch.
    """
    from copy import deepcopy
    from yt_dlp.extractor.youtube._base import INNERTUBE_CLIENTS
    if 'tv_samsung' not in INNERTUBE_CLIENTS:
        config=deepcopy(INNERTUBE_CLIENTS['tv_downgraded'])
        config['INNERTUBE_CONTEXT']['client'].update({
            'clientName':'TVHTML5','clientVersion':'5.20260707',
            'deviceMake':'Samsung','deviceModel':'UKS9800',
            'userAgent':'Mozilla/5.0 (SMART-TV; Linux; Tizen 2.4.0) AppleWebKit/538.1 (KHTML, like Gecko) Version/2.4.0 TV Safari/538.1',
            'osName':'Tizen','osVersion':'2.4.0'})
        INNERTUBE_CLIENTS['tv_samsung']=config


def probe(mode, url, cookiefile=None, system_ca=False, cookie_work_dir=None, pot_base_url=None):
    from yt_dlp import YoutubeDL
    messages=[]
    class Logger:
        def debug(self,msg):
            if 'Generating a ' in msg and 'PO Token' in msg:messages.append('pot_requested')
        def warning(self,msg):
            if 'PO Token' in msg:messages.append('pot_warning')
            if 'challenge' in msg.casefold():messages.append('challenge_warning')
            if 'SABR' in msg:messages.append('sabr_warning')
        def error(self,msg):pass
    options=dict(quiet=True,no_warnings=False,logger=Logger(),noplaylist=True,
                 skip_download=True,format='bestaudio/best',socket_timeout=8,
                 retries=0,extractor_retries=0,js_runtimes={'node':{}},
                 cachedir=False)
    if system_ca:options['compat_opts']={'no-certifi'}
    if mode=='pot':options['extractor_args']={'youtube':{'player_client':['mweb']},
        'youtubepot-bgutilhttp':{'base_url':['http://127.0.0.1:4416']}}
    if mode in {'cookies','cookies-safari','cookies-samsung','cookies-pot'}:
        if not cookiefile or not os.path.isfile(cookiefile):return {'mode':mode,'result':'not_tested_no_user_cookies'}
        # Never mutate the read-only Render Secret File or export cookies in logs.
        jar=MozillaCookieJar(cookiefile)
        try:jar.load(ignore_discard=True,ignore_expires=False)
        except Exception:return {'mode':mode,'result':'invalid_cookie_file'}
        scoped=[c for c in jar if c.domain.lstrip('.')=='youtube.com' or c.domain.endswith('.youtube.com')]
        if not scoped:return {'mode':mode,'result':'no_unexpired_youtube_cookies'}
        cookie_dir=tempfile.TemporaryDirectory(prefix='mudyar_cookie_probe_',dir=cookie_work_dir)
        destination=os.path.join(cookie_dir.name,'cookies.txt')
        safe_jar=MozillaCookieJar(destination)
        for cookie in scoped:safe_jar.set_cookie(cookie)
        safe_jar.save(ignore_discard=True)
        os.chmod(destination,0o600)
        options['cookiefile']=destination
        if mode in {'cookies-safari','cookies-samsung'}:
            if mode=='cookies-samsung':install_samsung_client()
            client='tv_samsung' if mode=='cookies-samsung' else 'web_safari'
            options['extractor_args']={'youtube':{'player_client':[client]}}
            options['format']='bestaudio[protocol=https]/bestaudio'
        if mode=='cookies-pot':
            if not pot_base_url or not pot_base_url.startswith('http://127.0.0.1:'):
                cookie_dir.cleanup();return {'mode':mode,'result':'helper_endpoint_missing'}
            options['extractor_args']={'youtube':{'player_client':['web_creator']},
                'youtubepot-bgutilhttp':{'base_url':[pot_base_url]}}
            options['format']='bestaudio[protocol=https]/bestaudio'
            options['verbose']=True
    else:cookie_dir=None
    try:
        import deno
        options['js_runtimes']={'deno':{'path':deno.find_deno_bin()}}
    except ImportError:pass
    started=time.monotonic()
    row={'mode':mode,'result':'unavailable'}
    try:
        with YoutubeDL(options) as ydl:
            info=ydl.extract_info(url,download=False)
        stream=info.get('url')
        if not stream or urlsplit(stream).scheme!='https':raise ValueError('No HTTPS audio URL')
        row.update(metadata=True,audio_only=info.get('vcodec')=='none',
                   format_id=info.get('format_id'),protocol=info.get('protocol'))
        row['audio_formats']=sum(f.get('vcodec')=='none' and f.get('acodec') not in (None,'none') for f in info.get('formats',[]))
        if not row['audio_only'] and mode!='cookies':raise ValueError('Selected format includes video')
        headers=''.join(f'{key}: {value}\r\n' for key,value in info.get('http_headers',{}).items()
                        if '\r' not in str(value) and '\n' not in str(value))
        cmd=['ffmpeg','-nostdin','-v','error','-rw_timeout','5000000']
        # Local trusted CA, not disabling TLS verification.
        cmd+=['-tls_verify','1']
        ca=os.environ.get('SSL_CERT_FILE')
        if system_ca and ca:cmd+=['-ca_file',ca]
        if headers:cmd+=['-headers',headers]
        cmd+=['-i',stream,'-t','2','-vn','-threads','1','-filter_threads','1','-ac','1','-ar','16000','-f','s16le','pipe:1']
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
                      'format_unavailable' if 'requested format is not available' in message else
                      'http_403' if '403' in message else
                      'cookies_rejected' if 'cookies' in message and ('invalid' in message or 'expired' in message) else
                      'tls_certificate' if 'certificate' in message else
                      'unavailable_video' if 'video is unavailable' in message else type(exc).__name__)
    if cookie_dir is not None:cookie_dir.cleanup()
    row.update(seconds=round(time.monotonic()-started,2),pot_requested='pot_requested' in messages,
               pot_warning='pot_warning' in messages,challenge_warning='challenge_warning' in messages,sabr_warning='sabr_warning' in messages)
    return row


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--mode',choices=['runtime','pot','cookies','cookies-safari','cookies-samsung','cookies-pot'],required=True)
    p.add_argument('--url',default='https://www.youtube.com/watch?v=aqz-KE-bpKQ')
    p.add_argument('--cookie-file');p.add_argument('--cookie-work-dir');p.add_argument('--system-ca',action='store_true')
    args=p.parse_args()
    print(json.dumps(probe(args.mode,args.url,args.cookie_file,args.system_ca,args.cookie_work_dir)),flush=True)
