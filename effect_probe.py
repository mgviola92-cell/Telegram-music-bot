"""Bounded FFmpeg DSP benchmark; synthetic input only, no Telegram/VC operations."""
import asyncio
import logging
import re
import time
from audio_options import AudioSettings,EFFECTS
LOG=logging.getLogger('telegram_music')

async def measure_effect(effect,normalize=False,seconds=60):
    settings=AudioSettings(effect=effect,normalize=normalize)
    args=['ffmpeg','-nostdin','-hide_banner','-v','info','-benchmark','-f','lavfi','-i',
          f'sine=frequency=440:sample_rate=48000:duration={seconds}','-ac','2']
    if settings.filters():args+=['-af',settings.filters()]
    args+=['-f','null','-']
    started=time.monotonic()
    proc=await asyncio.create_subprocess_exec(*args,stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.PIPE)
    try:
        _,stderr=await asyncio.wait_for(proc.communicate(),15)
        if proc.returncode:raise RuntimeError('Effect decode failed: '+effect)
        text=stderr.decode(errors='replace')
        match=re.search(r'utime=([\d.]+)s stime=([\d.]+)s',text)
        if not match:raise RuntimeError('Missing FFmpeg benchmark')
        return {'effect':effect,'normalize':normalize,'audio_seconds':seconds,
                'cpu_seconds':round(sum(float(x) for x in match.groups()),4),
                'wall_seconds':round(time.monotonic()-started,4)}
    finally:
        if proc.returncode is None:proc.kill();await proc.wait()

async def run_effect_probe(players):
    rows=[]
    for effect,normalize in [(e,False) for e in EFFECTS]+[('tiny',True),('chorus',True)]:
        if any(p.current is not None or p.idle_mode is not None or (p.loading_task and not p.loading_task.done()) for p in players.values()):
            LOG.info('Effect benchmark stopped: VC activity has priority');return
        try:
            row=await measure_effect(effect,normalize);rows.append(row)
            LOG.info('Effect benchmark PASS effect=%s normalize=%s audio_seconds=%s cpu_seconds=%.4f wall_seconds=%.4f',
                     effect,normalize,row['audio_seconds'],row['cpu_seconds'],row['wall_seconds'])
        except Exception as exc:
            LOG.warning('Effect benchmark unavailable effect=%s (%s)',effect,type(exc).__name__)
        await asyncio.sleep(.2)
    LOG.info('Effect benchmark finished cases=%s',len(rows))
