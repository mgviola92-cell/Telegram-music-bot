"""Manual, bounded YouTube audio diagnostic. Never imported by the bot.
No URLs, request headers, cookies or tokens are printed; only result summaries.
"""
import argparse
from contextlib import contextmanager
import json
import os
import subprocess
import time
import tempfile
from http.cookiejar import MozillaCookieJar
from urllib.parse import urlsplit


@contextmanager
def measure_js(metrics):
    """Time the existing Deno solver without changing its input or result."""
    from yt_dlp.extractor.youtube.jsc._builtin.deno import DenoJCP
    original = DenoJCP._run_js_runtime
    def measured(instance, script):
        started = time.monotonic()
        try:
            return original(instance, script)
        finally:
            metrics['js_seconds'] += time.monotonic() - started
            metrics['js_calls'] += 1
    DenoJCP._run_js_runtime = measured
    try:
        yield
    finally:
        DenoJCP._run_js_runtime = original


def probe(mode, url, cookiefile=None, system_ca=False, cookie_work_dir=None, primary_web_only=False):
    from yt_dlp import YoutubeDL
    messages=[]
    marks=[]
    clock=[None]
    metrics={'js_seconds':0.,'js_calls':0}
    class Logger:
        def debug(self,msg):
            if clock[0] is not None and len(marks)<24:
                for text,label in [('Downloading webpage','webpage'),
                                   ('player API JSON','player_api'),
                                   ('Downloading player','player_js'),
                                   ('Solving JS challenges using','js_solve'),
                                   ('Downloading m3u8 information','manifest')]:
                    if text in str(msg):
                        marks.append(f'{label}@{time.monotonic()-clock[0]:.2f}')
                        break
            if 'Generating a ' in msg and 'PO Token' in msg:messages.append('pot_requested')
        def warning(self,msg):
            if 'PO Token' in msg:messages.append('pot_warning')
            if 'challenge' in msg.casefold():messages.append('challenge_warning')
        def error(self,msg):pass
    options=dict(quiet=True,verbose=True,no_warnings=True,logger=Logger(),noplaylist=True,
                 skip_download=True,format='bestaudio/best',socket_timeout=8,
                 retries=0,extractor_retries=0,js_runtimes={'node':{}},
                 cachedir=False)
    if system_ca:options['compat_opts']={'no-certifi'}
    if mode=='pot':options['extractor_args']={'youtube':{'player_client':['mweb']},
        'youtubepot-bgutilhttp':{'base_url':['http://127.0.0.1:4416']}}
    if mode in {'cookies','cookies-safari'}:
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
        if mode=='cookies-safari':options['extractor_args']={'youtube':{'player_client':['web_safari']}}
    else:cookie_dir=None
    try:
        import deno
        options['js_runtimes']={'deno':{'path':deno.find_deno_bin()}}
    except ImportError:pass
    if primary_web_only:
        options['extractor_args']={'youtube':{'player_client':['web']}}
    started=time.monotonic()
    row={'mode':mode,'result':'unavailable','primary_web_only':primary_web_only}
    try:
        extract_started=time.monotonic()
        clock[0]=extract_started
        with measure_js(metrics), YoutubeDL(options) as ydl:
            info=ydl.extract_info(url,download=False)
        row['extract_seconds']=round(time.monotonic()-extract_started,2)
        stream=info.get('url')
        if not stream or urlsplit(stream).scheme!='https':raise ValueError('No HTTPS audio URL')
        row.update(metadata=True,audio_only=info.get('vcodec')=='none',
                   format_id=info.get('format_id'),protocol=info.get('protocol'))
        row['audio_formats']=sum(f.get('vcodec')=='none' and f.get('acodec') not in (None,'none') for f in info.get('formats',[]))
        if not row['audio_only'] and mode not in {'cookies','cookies-safari'}:raise ValueError('Selected format includes video')
        headers=''.join(f'{key}: {value}\r\n' for key,value in info.get('http_headers',{}).items()
                        if '\r' not in str(value) and '\n' not in str(value))
        cmd=['ffmpeg','-nostdin','-v','error','-rw_timeout','5000000']
        # Local trusted CA, not disabling TLS verification.
        cmd+=['-tls_verify','1']
        ca=os.environ.get('SSL_CERT_FILE')
        if system_ca and ca:cmd+=['-ca_file',ca]
        if headers:cmd+=['-headers',headers]
        cmd+=['-i',stream,'-t','2','-vn','-threads','1','-filter_threads','1','-ac','1','-ar','16000','-f','s16le','pipe:1']
        decode_started=time.monotonic()
        audio=subprocess.run(cmd,capture_output=True,timeout=18)
        row['decode_seconds']=round(time.monotonic()-decode_started,2)
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
    row.update(js_seconds=round(metrics['js_seconds'],2),js_calls=metrics['js_calls'],
               stage_marks=';'.join(marks),seconds=round(time.monotonic()-started,2),pot_requested='pot_requested' in messages,
               pot_warning='pot_warning' in messages,challenge_warning='challenge_warning' in messages)
    return row


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--mode',choices=['runtime','pot','cookies','cookies-safari'],required=True)
    p.add_argument('--url',default='https://www.youtube.com/watch?v=aqz-KE-bpKQ')
    p.add_argument('--primary-web-only',action='store_true')
    p.add_argument('--cookie-file');p.add_argument('--cookie-work-dir');p.add_argument('--system-ca',action='store_true')
    args=p.parse_args()
    print(json.dumps(probe(args.mode,args.url,args.cookie_file,args.system_ca,args.cookie_work_dir,args.primary_web_only)),flush=True)
