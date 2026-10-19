"""One-time, expiring Render cookie diagnostic. Does not resolve live tracks."""
import asyncio
import json
import logging
import os
from pathlib import Path
import signal
import sys
import time
import tempfile
import shutil

LOG=logging.getLogger('telegram_music')
# Short fixed window for the owner-authorized test; inert after expiry.
PROBE_UNTIL = 1792439847


def busy(players):
    return any(p.current is not None or p.idle_mode=='radio' or p.skip_pending or
               (p.loading_task is not None and not p.loading_task.done()) for p in players.values())


async def attempt(mode,players):
    script=Path(__file__).parent/'tools'/('youtube_pot_probe.py' if mode=='cookies-pot' else 'youtube_direct_probe.py')
    folder=tempfile.TemporaryDirectory(prefix='mudyar_cookie_probejob_')
    command=[sys.executable,str(script),'--mode',mode,'--cookie-file','/etc/secrets/cookies.txt',
             '--cookie-work-dir',folder.name]
    nice=shutil.which('nice')
    if nice:command=[nice,'-n','15',*command]
    try:
        proc=await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True)
    except BaseException:
        folder.cleanup();raise
    result=asyncio.create_task(proc.communicate())
    try:
        until=time.monotonic()+(170 if mode=='cookies-pot' else 65)
        while not result.done():
            if busy(players):return {'mode':mode,'result':'aborted_for_playback'}
            if mode=='cookies-pot':
                from runtime_status import memory_sample
                used,limit=memory_sample()
                if limit and used/limit>.85:
                    phase=Path(folder.name)/'helper.phase'
                    try:stage=phase.read_text().strip()
                    except OSError:stage='starting'
                    if stage not in {'install','helper_start','extract_audio'}:stage='starting'
                    return {'mode':mode,'result':'aborted_for_memory','helper_phase':stage,
                            'used_mb':round(used/1024**2,1),'limit_mb':round(limit/1024**2,1)}
            if time.monotonic()>=until:return {'mode':mode,'result':'probe_timeout'}
            await asyncio.wait({result},timeout=.25)
        out,_=await result
        try:row=json.loads(out)
        except (ValueError,UnicodeError):return {'mode':mode,'result':'probe_process_failed'}
        # Fixed result allowlist; never forward arbitrary child output to logs.
        fields={'mode','result','error','media_error','metadata','audio_only',
                'helper_phase','used_mb','limit_mb','helper_ready','helper_setup_seconds','sabr_warning','format_id','audio_formats','challenge_warning','protocol','decoded_pcm_bytes','seconds','pot_requested','pot_warning'}
        return {k:v for k,v in row.items() if k in fields and isinstance(v,(str,int,float,bool))}
    finally:
        if proc.returncode is None:
            try:os.killpg(proc.pid,signal.SIGKILL)
            except ProcessLookupError:pass
        await asyncio.gather(result,return_exceptions=True)
        folder.cleanup()


async def run_cookie_probe(players,until=None):
    until=PROBE_UNTIL if until is None else until
    if not 0<until-time.time()<=1800:return
    if not Path('/etc/secrets/cookies.txt').is_file():
        LOG.info('YouTube cookie probe: secret_file_missing');return
    await asyncio.sleep(8)
    if busy(players):
        LOG.info('YouTube cookie probe: skipped_for_playback');return
    for mode in ('cookies-pot',):
        if time.time()>=until or busy(players):return
        row=await attempt(mode,players)
        LOG.info('YouTube cookie probe: %s',json.dumps(row,sort_keys=True))
        if row.get('result') in {'aborted_for_playback','invalid_cookie_file','no_unexpired_youtube_cookies'}:return
        if row.get('result')=='decoded_audio' and row.get('audio_only'):return
        await asyncio.sleep(3)
