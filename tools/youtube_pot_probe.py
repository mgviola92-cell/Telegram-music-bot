"""Temporary loopback token helper + authorized cookie audio-only diagnostic."""
import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
if __package__:
    from .youtube_direct_probe import probe
else:
    from youtube_direct_probe import probe


def helper_environment(folder):
    # The helper never needs bot credentials or account cookies in its env.
    names={'PATH','LD_LIBRARY_PATH','SSL_CERT_FILE','SSL_CERT_DIR','LANG','LC_ALL',
           'NODE_EXTRA_CA_CERTS','REQUESTS_CA_BUNDLE','SYSTEMROOT',
           'HTTP_PROXY','HTTPS_PROXY','NO_PROXY','http_proxy','https_proxy','no_proxy'}
    env={k:v for k,v in os.environ.items() if k in names}
    if 'NODE_EXTRA_CA_CERTS' not in env and env.get('SSL_CERT_FILE'):
        env['NODE_EXTRA_CA_CERTS']=env['SSL_CERT_FILE']
    env['npm_config_cache']=str(Path(folder)/'npm-cache')
    env['npm_config_update_notifier']='false'
    env['NODE_OPTIONS']='--max-old-space-size=96'
    env['npm_config_maxsockets']='1'
    env['npm_config_progress']='false'
    return env


def run(cookie_file,work_dir):
    if not Path(cookie_file).is_file():return {'mode':'cookies-pot','result':'not_tested_no_user_cookies'}
    node=shutil.which('node')
    if not node:return {'mode':'cookies-pot','result':'helper_runtime_missing'}
    source=Path(__file__).parent.parent/'diagnostic_pot'
    if not (source/'node_modules').is_dir():return {'mode':'cookies-pot','result':'helper_build_required'}
    phase=Path(work_dir)/'helper.phase'
    with tempfile.TemporaryDirectory(prefix='pot_',dir=work_dir) as folder:
        target=source
        env=helper_environment(folder)
        setup_seconds=0.0
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        phase.write_text('helper_start')
        helper=subprocess.Popen([node,'build/main.js','--host','127.0.0.1','--port',str(port)],
            cwd=target,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            ready=False
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            for _ in range(40):
                if helper.poll() is not None:break
                try:
                    with opener.open(f'http://127.0.0.1:{port}/ping',timeout=.3) as response:
                        ready=response.status==200
                    if ready:break
                except OSError:pass
                time.sleep(.1)
            if not ready:return {'mode':'cookies-pot','result':'helper_start_failed'}
            phase.write_text('extract_audio')
            row=probe('cookies-pot','https://www.youtube.com/watch?v=aqz-KE-bpKQ',
                cookie_file,cookie_work_dir=folder,pot_base_url=f'http://127.0.0.1:{port}')
            row.update(helper_ready=True,helper_prebuilt=True,helper_setup_seconds=setup_seconds)
            return row
        finally:
            if helper.poll() is None:helper.terminate()
            try:helper.wait(timeout=4)
            except subprocess.TimeoutExpired:helper.kill();helper.wait()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--mode');parser.add_argument('--cookie-file',required=True)
    parser.add_argument('--cookie-work-dir',required=True);args=parser.parse_args()
    try:row=run(args.cookie_file,args.cookie_work_dir)
    except Exception as exc:row={'mode':'cookies-pot','result':'helper_failed','error':type(exc).__name__}
    print(json.dumps(row),flush=True)
