"""Expiring deployment-only network test; no Telegram messages or VC joins."""
import asyncio
import logging
from welcome_audio import VOICES, greeting_text, GreetingPipe, mixing_command, BYTES_PER_SECOND, MAX_SECONDS

log=logging.getLogger('telegram_music')

async def run_radio_welcome_probe(catalog,welcome,resolve_direct=None,*,party_only=False):
    if not party_only:
        await catalog.load_local()
    async def check(station):
        try:
            await catalog.verify(station.key)
            log.info('Radio live probe PASS region=%s station=%s',station.region,station.name)
        except Exception as exc:
            log.info('Radio live probe unavailable region=%s station=%s reason=%s',
                     station.region,station.name,type(exc).__name__)
    if not party_only:
        await asyncio.gather(*(check(s) for s in list(catalog.stations.values())))
    for label,voice in VOICES.items():
        try:
            style='party' if party_only else 'normal'
            pcm=await welcome.pcm(greeting_text('မောင်ခန့်',style=style),voice)
            if len(pcm)/BYTES_PER_SECOND >= MAX_SECONDS-.2:
                raise RuntimeError('Speech duration limit reached')
            pipe=GreetingPipe()
            try:
                pipe.offer(pcm)
                cmd=mixing_command(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i',
                     'sine=frequency=220:sample_rate=48000:duration=2','-ar','48000','-ac','2',
                     '-f','s16le','pipe:1'],pipe.path)
                proc=await asyncio.create_subprocess_exec(*cmd,stdout=asyncio.subprocess.PIPE,
                                                         stderr=asyncio.subprocess.PIPE)
                try:
                    output,_=await asyncio.wait_for(proc.communicate(),8)
                    if proc.returncode or len(output)<300000:
                        raise RuntimeError('Mixer output missing')
                finally:
                    if proc.returncode is None:
                        proc.kill();await proc.wait()
                log.info('Welcome live probe PASS voice=%s style=%s speech_seconds=%.2f tts_pcm_bytes=%s mixed_pcm_bytes=%s',
                         label,style,len(pcm)/BYTES_PER_SECOND,len(pcm),len(output))
            finally:
                pipe.close()
        except Exception as exc:
            log.info('Welcome live probe unavailable voice=%s reason=%s',label,type(exc).__name__)

    if party_only:
        for label,voice in VOICES.items():
            try:
                pcm=await welcome.pcm(greeting_text('',style='party_batch'),voice)
                log.info('Party batch speech probe PASS voice=%s speech_seconds=%.2f',
                         label,len(pcm)/BYTES_PER_SECOND)
            except Exception as exc:
                log.info('Party batch speech probe unavailable voice=%s (%s)',label,type(exc).__name__)

    if resolve_direct is not None:
        pipe=GreetingPipe()
        try:
            import shlex
            from network_audio import DirectAudioStream
            from welcome_audio import WelcomeStream
            url,_,_=await asyncio.wait_for(asyncio.to_thread(resolve_direct,'Purple Wink pGb'),30)
            stream=WelcomeStream(DirectAudioStream(url),pipe)
            await asyncio.wait_for(stream.check_stream(),15)
            cmd=shlex.split(stream.microphone.path)
            cmd[-1:-1]=['-t','2']
            proc=await asyncio.create_subprocess_exec(*cmd,stdout=asyncio.subprocess.PIPE,
                                                      stderr=asyncio.subprocess.PIPE)
            try:
                output,_=await asyncio.wait_for(proc.communicate(),15)
                if proc.returncode or len(output)<300000:
                    raise RuntimeError('Direct mixer output missing')
            finally:
                if proc.returncode is None:
                    proc.kill();await proc.wait()
            log.info('Welcome direct HTTPS mixer probe PASS pcm_bytes=%s',len(output))
        except Exception as exc:
            log.info('Welcome direct HTTPS mixer probe unavailable (%s)',type(exc).__name__)
        finally:
            pipe.close()
