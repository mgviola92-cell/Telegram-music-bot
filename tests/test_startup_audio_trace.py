import asyncio
import shlex
from pathlib import Path
from types import SimpleNamespace as N
import unittest
from unittest.mock import patch
from startup_audio_trace import StartupTrace, TracedAudioStream, wrap, close, current


class StartupTelemetry(unittest.IsolatedAsyncioTestCase):
    async def test_actual_ffmpeg_pcm_is_identical_with_telemetry(self):
        command=['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-f','lavfi','-i',
                 'sine=frequency=440:duration=0.5','-ar','48000','-ac','2','-f','s16le','pipe:1']
        async def decode(args):
            proc=await asyncio.create_subprocess_exec(*args,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            output,error=await asyncio.wait_for(proc.communicate(),5)
            self.assertEqual(proc.returncode,0,error)
            return output
        baseline=await decode(command)
        trace=StartupTrace(1,'test')
        try:
            observed=await decode(trace.command(command))
            await asyncio.wait_for(trace.task,2)
            self.assertEqual(observed,baseline)
            self.assertIsNotNone(trace.first_output)
            self.assertLess(trace.path.stat().st_size,32768)
            path=trace.path
        finally:trace.close()
        self.assertFalse(path.exists())

    async def test_zero_output_does_not_report_decoder_ready(self):
        trace=StartupTrace(1,'test')
        trace.command(['ffmpeg','-i','test','-f','s16le','pipe:1'])
        try:
            trace.path.write_text('out_time_us=0\nprogress=continue\n')
            await asyncio.sleep(.22)
            self.assertIsNone(trace.first_output)
            with trace.path.open('a') as f:f.write('out_time_us=20000\nprogress=continue\n')
            await asyncio.wait_for(trace.task,1)
            self.assertIsNotNone(trace.first_output)
        finally:trace.close()

    async def test_capture_copy_preserves_source_and_filters(self):
        class Source:
            def __init__(self):self.microphone=N(path="ffmpeg -i song -af volume=0.5 -f s16le pipe:1")
            async def check_stream(self):pass
        original=Source();trace=StartupTrace(1,'test')
        try:
            stream=TracedAudioStream(original,trace)
            await stream.check_stream()
            self.assertNotIn('-progress',original.microphone.path)
            args=shlex.split(stream.microphone.path)
            self.assertEqual(args[args.index('-af')+1],'volume=0.5')
            self.assertEqual(args[-3:],['-f','s16le','pipe:1'])
            self.assertIsNot(stream.microphone,original.microphone)
        finally:
            trace.close()
            await asyncio.gather(trace.task,return_exceptions=True)

    async def test_telemetry_file_error_keeps_working_command(self):
        class Source:
            def __init__(self):self.microphone=N(path='ffmpeg -i song -f s16le pipe:1')
            async def check_stream(self):pass
        original=Source();trace=StartupTrace(1,'test')
        with patch.object(trace,'command',side_effect=OSError('disk unavailable')):
            stream=TracedAudioStream(original,trace)
            await stream.check_stream()
        self.assertEqual(stream.microphone.path,original.microphone.path)
        trace.close()

    async def test_music_trial_is_bounded_and_skips_idle(self):
        from youtube_relay import RelayAudioStream,relay_source
        class Calls:pass
        calls=Calls();source=RelayAudioStream(relay_source('qtYb7nV4814'))
        with patch.dict('os.environ',{'MUSIC_STARTUP_DIAGNOSTICS':'1'}):
            for i in range(12):
                observed,trace=wrap(calls,1,source)
                self.assertIsNotNone(trace)
            observed,trace=wrap(calls,1,source)
            self.assertIs(observed,source);self.assertIsNone(trace)
        await close(calls)
        self.assertIsNone(current(calls,1));self.assertIsNone(current(N(),1))

    async def test_real_client_route_correlates_decoder_and_native_clock(self):
        from unittest.mock import AsyncMock
        from pytgcalls import PyTgCalls
        from ntgcalls import ConnectionMode
        from main import play_with_reconnect,wait_for_audio_frames
        from youtube_relay import RelayAudioStream,relay_source
        calls=object.__new__(PyTgCalls)
        async def capture(chat,stream,config):
            await stream.check_stream()
            trace=current(calls,chat)
            trace.path.write_text('out_time_us=20000\nprogress=continue\n')
        calls.play=AsyncMock(side_effect=capture)
        calls.resume=AsyncMock();calls.unmute=AsyncMock()
        calls.time=AsyncMock(side_effect=[0,1]);calls.leave_call=AsyncMock()
        calls._binding=N(get_connection_mode=AsyncMock(return_value=ConnectionMode.RTC),
                         get_state=AsyncMock(return_value=N(muted=False)))
        with patch.dict('os.environ',{'MUSIC_STARTUP_DIAGNOSTICS':'1'}):
            try:
                await play_with_reconnect(calls,-100,RelayAudioStream(relay_source('qtYb7nV4814')))
                await wait_for_audio_frames(calls,-100)
                trace=current(calls,-100)
                self.assertIsNotNone(trace.first_output)
                self.assertEqual(trace.kind,'youtube_relay')
                calls.play.assert_awaited_once();calls.leave_call.assert_not_awaited()
            finally:await close(calls)
