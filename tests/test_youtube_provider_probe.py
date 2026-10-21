import asyncio
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

import youtube_provider_probe as probe


class StreamProbeTests(TestCase):
    def test_input_urls_are_fixed_and_no_private_credentials_are_sent(self):
        for provider in probe.PROVIDERS:
            command = probe.input_options(provider, probe.VIDEOS[0])
            self.assertTrue(command[command.index('-i') + 1].startswith('https://'))
            self.assertNotIn('Cookie', ' '.join(command))
            self.assertNotIn('Authorization', ' '.join(command))
        with self.assertRaises(ValueError):
            probe.input_options('vapters', 'http://localhost')

    def test_legacy_post_body_matches_existing_contract(self):
        command = probe.input_options('legacy_relay', probe.VIDEOS[0])
        body = json.loads(bytes.fromhex(command[command.index('-post_data') + 1]))
        self.assertEqual(body, {'url': 'https://www.youtube.com/watch?v=nfs8NYg7yQM',
                                'type': 'audio'})

    def test_pcm_measures_actual_first_bytes_and_safe_metadata(self):
        original = subprocess.Popen
        def spawn(command, **kwargs):
            self.assertIn('-tls_verify', command)
            return original([sys.executable, '-c',
                "import sys,time;time.sleep(.12);"
                "sys.stderr.write('Duration: 00:03:51.00\\n    title : Attention\\n');"
                "sys.stdout.buffer.write(bytes(32000))"], **kwargs)
        with patch.object(probe.subprocess, 'Popen', side_effect=spawn), \
             patch.object(probe, 'SAMPLE_SECONDS', 1):
            row = probe.probe('vapters', probe.VIDEOS[0], timeout=3)
        self.assertEqual(row['result'], 'decoded_audio')
        self.assertEqual(row['pcm_bytes'], 32000)
        self.assertGreaterEqual(row['first_pcm_seconds'], .1)
        self.assertEqual(row['duration_seconds'], 231)
        self.assertEqual(row['title'], 'Attention')
        self.assertNotIn('https://', json.dumps(row))

    def test_small_aac_priming_trim_is_not_falsely_rejected(self):
        original = subprocess.Popen
        def spawn(command, **kwargs):
            return original([sys.executable, '-c',
                             'import sys;sys.stdout.buffer.write(bytes(31760))'], **kwargs)
        with patch.object(probe.subprocess, 'Popen', side_effect=spawn), \
             patch.object(probe, 'SAMPLE_SECONDS', 1):
            row = probe.probe('vapters', probe.VIDEOS[0], timeout=3)
        self.assertEqual(row['result'], 'decoded_audio')
        self.assertEqual(row['ffmpeg_returncode'], 0)

    def test_stalled_ffmpeg_is_killed_and_reaped(self):
        original = subprocess.Popen
        processes = []
        def spawn(command, **kwargs):
            process = original([sys.executable, '-c', 'import time;time.sleep(30)'], **kwargs)
            processes.append(process)
            return process
        with patch.object(probe.subprocess, 'Popen', side_effect=spawn):
            row = probe.probe('vapters', probe.VIDEOS[0], timeout=.15)
        self.assertEqual(row['error'], 'timeout')
        self.assertIsNotNone(processes[0].poll())


class RunnerTests(IsolatedAsyncioTestCase):
    async def test_expiry_is_inert_without_touching_files(self):
        with patch.object(probe.os, 'open', side_effect=AssertionError('not inert')):
            await probe.run_provider_probe({}, until=1)

    async def test_playback_kills_entire_probe_group(self):
        gate = asyncio.Event()
        class Process:
            pid = 1234
            returncode = None
            async def communicate(self):
                await gate.wait()
                self.returncode = -9
                return b'', b''
        with patch.object(probe.asyncio, 'create_subprocess_exec', AsyncMock(return_value=Process())), \
             patch.object(probe, 'busy', return_value=True), \
             patch.object(probe.os, 'killpg', side_effect=lambda *args: gate.set()) as kill:
            row = await probe.attempt('vapters', probe.VIDEOS[0], {})
        self.assertEqual(row['result'], 'aborted_for_playback')
        kill.assert_called_once_with(1234, probe.signal.SIGKILL)

    async def test_later_providers_and_successful_repeats_still_run_in_order(self):
        cases = []
        async def attempt(provider, video, players):
            cases.append((provider, video))
            return {'provider': provider, 'video_id': video,
                    'result': 'failed' if provider == 'legacy_relay' else 'decoded_audio'}
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(probe, 'MARKER', Path(folder) / 'started'), \
             patch.object(probe, 'RUN_PROVIDERS', probe.PROVIDERS), \
             patch.object(probe.asyncio, 'sleep', AsyncMock()), \
             patch.object(probe, 'attempt', side_effect=attempt):
            await probe.run_provider_probe({}, until=time.time() + 600)
            count = len(cases)
            await probe.run_provider_probe({}, until=time.time() + 600)
        first = [(provider, video) for provider in ('vapters', 'tomatofist')
                 for video in probe.VIDEOS]
        self.assertEqual(cases, first + [('legacy_relay', probe.VIDEOS[0])] + first)
        self.assertEqual(count, len(cases))

    async def test_aborted_probe_stops_all_following_network_work(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(probe, 'MARKER', Path(folder) / 'started'), \
             patch.object(probe.asyncio, 'sleep', AsyncMock()), \
             patch.object(probe, 'attempt', AsyncMock(return_value={'result': 'aborted_for_playback'})) as attempt:
            await probe.run_provider_probe({}, until=time.time() + 600)
        attempt.assert_awaited_once()
