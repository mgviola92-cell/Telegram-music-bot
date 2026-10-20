"""One two-second read through the exact production cookie-only resolver."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
sys.path.insert(0, str(Path(__file__).parent.parent))
from youtube_stream import resolve
from player_visuals import stream_headers_for

if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--mode');parser.add_argument('--cookie-file',required=True)
    parser.add_argument('--cookie-work-dir',required=True);args=parser.parse_args()
    row={'mode':'stream-trial','result':'unavailable'};started=time.monotonic()
    phase=Path(args.cookie_work_dir)/'helper.phase'
    try:
        os.environ['TMPDIR']=args.cookie_work_dir
        import tempfile
        tempfile.tempdir=args.cookie_work_dir
        phase.write_text('extract_audio')
        url,_,_=resolve('https://www.youtube.com/watch?v=aqz-KE-bpKQ',
                        cookiefile=args.cookie_file,cookie_work_dir=args.cookie_work_dir,
                        duration_limit=660)  # Fixed 10:34 sample; decode only 2 seconds.
        headers=''.join(f'{k}: {v}\r\n' for k,v in stream_headers_for(url).items())
        phase.write_text('media_decode')
        cmd=['ffmpeg','-nostdin','-v','error','-rw_timeout','5000000','-tls_verify','1',
             '-threads','1','-vn','-analyzeduration','1000000','-probesize','262144']
        if headers:cmd+=['-headers',headers]
        cmd+=['-i',url,'-t','2','-vn','-threads','1','-ac','1','-ar','16000','-f','s16le','pipe:1']
        audio=subprocess.run(cmd,capture_output=True,timeout=18)
        row.update(decoded_pcm_bytes=len(audio.stdout),metadata=True)
        if audio.returncode==0 and len(audio.stdout)>=32000:row['result']='decoded_audio'
        else:row['result']='media_unavailable'
    except Exception as exc:
        row['error']=type(exc).__name__
        if str(exc) in {'youtube_format_unavailable','youtube_verification_required',
                        'youtube_http_403','youtube_duration_limit','youtube_unavailable',
                        'youtube_cookies_expired','youtube_challenge_unavailable',
                        'youtube_js_heap_limit','youtube_js_stack_limit',
                        'youtube_js_runtime_missing','youtube_js_solver_missing'}:
            row['media_error']=str(exc)
    row['seconds']=round(time.monotonic()-started,2)
    print(json.dumps(row),flush=True)
