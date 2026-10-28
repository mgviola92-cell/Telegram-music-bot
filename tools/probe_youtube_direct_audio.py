"""Read-only test of YouTube direct playable audio from cloud runner.
Print no raw media URLs, cookies, auth headers or server responses.
Downloads no full tracks. Every client attempt is subprocess-time-bounded.
"""
import json
import subprocess
import sys
import time


CLIENTS = ('default', 'web_safari', 'mweb', 'web_embedded', 'android_vr')
VIDEO = 'https://www.youtube.com/watch?v=jNQXAC9IVRw'


def one(client):
    import urllib.parse
    from yt_dlp import YoutubeDL
    class NoLog:
        def debug(self, *args): pass
        def warning(self, *args): pass
        def error(self, *args): pass
    started = time.monotonic()
    config = {
        'quiet': True, 'no_warnings': True, 'skip_download': True,
        'noplaylist': True, 'socket_timeout': 8, 'retries': 0,
        'extractor_retries': 0, 'fragment_retries': 0,
        'format': 'bestaudio/best',
        'extractor_args': {'youtube': {'player_client': [client]}},
        'logger': NoLog(),
    }
    try:
        with YoutubeDL(config) as ydl:
            result = ydl.extract_info(VIDEO, download=False)
        if not isinstance(result, dict):
            print(json.dumps({'client': client, 'result': 'empty',
                              'seconds': round(time.monotonic()-started, 2)}), flush=True)
            return
        formats = []
        if result.get('url'):
            formats.append(result)
        formats.extend(result.get('requested_formats') or [])
        formats.extend(result.get('formats') or [])
        candidates = []
        for fmt in formats:
            url = fmt.get('url') if isinstance(fmt, dict) else None
            if not isinstance(url, str):
                continue
            try:
                parsed = urllib.parse.urlsplit(url)
            except ValueError:
                continue
            hostname = (parsed.hostname or '').lower()
            if parsed.scheme == 'https' and (hostname == 'googlevideo.com' or
                    hostname.endswith('.googlevideo.com')):
                candidates.append((url, fmt))
        # Signed URLs are never printed, persisted or included in exceptions.
        observed = {'client': client, 'resolved': True, 'candidate_count': len(candidates),
                    'metadata_s': round(time.monotonic()-started, 2)}
        for url, fmt in candidates[:3]:
            started_decode = time.monotonic()
            cmd = ['ffmpeg','-hide_banner','-nostdin','-loglevel','quiet',
                   '-rw_timeout','6000000','-i',url,'-t','0.15','-vn',
                   '-ar','16000','-ac','1','-f','s16le','pipe:1']
            try:
                decoded = subprocess.run(cmd, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, timeout=11)
                observed['pcm_bytes'] = len(decoded.stdout)
                observed['ffmpeg_rc'] = decoded.returncode
                observed['decode_s'] = round(time.monotonic()-started_decode, 2)
                observed['format_type'] = str(fmt.get('protocol') or 'unknown')[:20]
                if len(decoded.stdout) >= 1024 and decoded.returncode == 0:
                    observed['playable'] = True
                    break
            except subprocess.TimeoutExpired:
                observed['decode_timeout'] = True
        observed['playable'] = bool(observed.get('playable'))
        observed['total_s'] = round(time.monotonic()-started, 2)
        print(json.dumps(observed),flush=True)
    except BaseException as exc:
        print(json.dumps({'client': client, 'error': type(exc).__name__,
                          'seconds': round(time.monotonic()-started, 2)}),flush=True)


def main():
    import shutil
    import yt_dlp
    print(json.dumps({'yt_dlp_version':yt_dlp.version.__version__,
                     'deno_path_present': bool(shutil.which('deno')),
                     'ffmpeg_path_present':bool(shutil.which('ffmpeg'))}),flush=True)
    for client in CLIENTS:
        begin=time.monotonic()
        try:
            proc = subprocess.run([sys.executable,__file__,'--one',client],
                                  capture_output=True,text=True,timeout=36)
            for line in proc.stdout.splitlines():
                try:
                    data=json.loads(line)
                    if isinstance(data,dict) and data.get('client')==client:
                        print(json.dumps(data),flush=True)
                except ValueError:
                    pass
            if not proc.stdout.strip():
                print(json.dumps({'client':client,'result':'no output',
                                  'seconds':round(time.monotonic()-begin,2)}),flush=True)
        except subprocess.TimeoutExpired:
            print(json.dumps({'client':client,'result':'bounded timeout',
                              'seconds':round(time.monotonic()-begin,2)}),flush=True)


if __name__=='__main__':
    if len(sys.argv)==3 and sys.argv[1]=='--one' and sys.argv[2] in CLIENTS:
        one(sys.argv[2])
    else:
        main()
