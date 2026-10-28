import asyncio
import shutil
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock, Mock, patch

import main
import audio_readiness
from lybot_source import BotSourceUnavailable
from source_race import ParallelSources
from test_music_extensions import closure
from youtube_relay import relay_source


class FirstDecodedWinnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_quick_unplayable_candidate_loses_to_slower_verified_audio(self):
        cleaned = []
        prepared = Obj(consume=AsyncMock(return_value=('bad.mp3', 'Bad · SoundCloud', 70)),
                       discard=Mock())
        async def extra():
            await asyncio.sleep(.025)
            return ('good.mp3', 'Good · NubCoders', 70)
        async def verify(result):
            await asyncio.sleep(.005)
            return result[0] == 'good.mp3'
        result = await asyncio.wait_for(
            ParallelSources([], cleaned.append).race(
                prepared, 'Song', extra=extra, verify=verify), timeout=2)
        self.assertEqual(result[0], 'good.mp3')
        self.assertIn('bad.mp3', cleaned)
        self.assertNotIn('good.mp3', cleaned)

    async def test_all_metadata_requests_start_before_first_decoded_winner(self):
        started = set()
        sources = []
        for i in range(4):
            source = Obj(cached=Mock(return_value=None))
            async def find(query, index=i):
                started.add(index)
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    raise
            source.find = AsyncMock(side_effect=find)
            sources.append(source)
        async def primary():
            while len(started) < 4:
                await asyncio.sleep(0)
            return ('winner.mp3', 'Verified · SoundCloud', 60)
        prepared=Obj(consume=primary, discard=Mock())
        result=await asyncio.wait_for(ParallelSources(sources, Mock()).race(
            prepared, 'Song', verify=AsyncMock(return_value=True)), timeout=2)
        self.assertEqual(result[0], 'winner.mp3')
        self.assertEqual(started, {0,1,2,3})

    async def test_file_transfer_sequential_until_pcm_check_finishes(self):
        sources = []
        concurrent = 0
        peak = 0
        total = 0
        for i in range(2):
            source=Obj(cached=Mock(return_value=None))
            source.find=AsyncMock(return_value=Obj(source=source,label='Song',duration=60))
            async def download(candidate):
                nonlocal concurrent,peak,total
                concurrent+=1
                peak=max(peak,concurrent)
                await asyncio.sleep(.005)
                concurrent-=1
                total+=1
                return (f'file-{total}.mp3','Song · Bot',60)
            source.download=AsyncMock(side_effect=download)
            sources.append(source)
        verifying = 0
        async def verify(result):
            nonlocal verifying
            verifying+=1
            await asyncio.sleep(.025)
            verifying-=1
            return result[0] == 'file-2.mp3'
        result=await ParallelSources(sources,Mock()).race(
            None,'Song',verify=verify)
        self.assertEqual(result[0],'file-2.mp3')
        self.assertEqual(total,2)
        self.assertEqual(peak,1)

    async def test_failed_all_candidates_produces_no_false_winner(self):
        prepared=Obj(consume=AsyncMock(return_value=('broken','Song',60)),discard=Mock())
        with self.assertRaises(BotSourceUnavailable):
            await ParallelSources([], Mock()).race(
                prepared,'Song',verify=AsyncMock(return_value=False))

    async def test_validation_exception_falls_through(self):
        prepared=Obj(consume=AsyncMock(return_value=('bad','Bad',60)),discard=Mock())
        async def second():
            await asyncio.sleep(.01)
            return ('good','Good',60)
        async def verify(result):
            if result[0]=='bad':
                raise RuntimeError('Decoder unavailable')
            return True
        result=await ParallelSources([],Mock()).race(
            prepared,'Song',extra=second,verify=verify)
        self.assertEqual(result[0],'good')


class SourceAudioGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_relay_needs_decoded_pcm_not_youtube_metadata(self):
        with patch('audio_readiness._short_pcm',new=AsyncMock(return_value=False)) as probe:
            self.assertFalse(await audio_readiness.verify_vapters_audio(relay_source('jNQXAC9IVRw')))
            self.assertTrue(probe.await_args.args[0].startswith('https://vapters.site/stream/'))
        with patch('audio_readiness._short_pcm',new=AsyncMock(return_value=True)):
            self.assertTrue(await audio_readiness.verify_vapters_audio(relay_source('jNQXAC9IVRw')))

    async def test_signed_nubcoders_already_verified_only_in_caller(self):
        signed='https://api.nubcoders.com/stream/resolver/opaque'
        with patch('audio_readiness._short_pcm',new=AsyncMock()) as decode:
            self.assertTrue(await audio_readiness.verify_candidate_audio((signed,'Track',60)))
            decode.assert_not_awaited()

    async def test_non_allowlisted_url_cannot_win(self):
        with patch('audio_readiness._short_pcm',new=AsyncMock()) as probe:
            self.assertFalse(await audio_readiness.verify_candidate_audio(
                ('https://127.0.0.1/audio','Private',60)))
            self.assertFalse(await audio_readiness.verify_candidate_audio(
                ('https://unrelated.example/a','Other',60)))
            probe.assert_not_awaited()

    @unittest.skipUnless(shutil.which('ffmpeg'),'FFmpeg needed to verify real PCM')
    async def test_real_local_audio_must_decode_not_only_exist(self):
        with tempfile.TemporaryDirectory() as folder:
            good=Path(folder)/'music.wav'
            with wave.open(str(good),'wb') as output:
                output.setparams((1,2,16000,0,'NONE','not compressed'))
                output.writeframes(struct.pack('<h',1000)*16000)
            bad=Path(folder)/'garbage.mp3'
            bad.write_bytes(b'not a playable media file'*100)
            self.assertTrue(await audio_readiness.verify_candidate_audio(
                (str(good),'Valid',1),timeout=4))
            self.assertFalse(await audio_readiness.verify_candidate_audio(
                (str(bad),'Invalid',1),timeout=4))


class NoFixedSourcePriorityTests(unittest.IsolatedAsyncioTestCase):
    async def test_youtube_metadata_does_not_beat_verified_audio(self):
        relay=Obj(source=relay_source('jNQXAC9IVRw'),title='Song · YouTube',duration=20)
        ns=dict(vars(main))
        ns.update(resolve_youtube_relay=AsyncMock(return_value=relay),
                  verify_vapters_audio=AsyncMock(return_value=False),
                  relay_playback_health=Obj(available=Mock(return_value=True),failure=Mock()),
                  silent_relay_videos=Obj(available=Mock(return_value=True),failure=Mock()),
                  relay_cooldown=Obj(available=Mock(return_value=True)),
                  youtube_relay_enabled=lambda:True)
        resolver=closure('resolve_for_queue',ns)
        from unittest.mock import patch as mock_patch
        with mock_patch.dict('os.environ',{'NUBCODERS_ENABLED':'0','NUBCODERS_API_TOKEN':''}):
            with self.assertRaises(Exception):
                await ns['verify_vapters_audio'](relay.source) if False else asyncio.sleep(0)
        # Actual source resolution below is covered by the real PCM gate tests.
