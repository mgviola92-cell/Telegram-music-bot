import asyncio
import json
from pathlib import Path
from types import SimpleNamespace as N
import unittest
from unittest.mock import patch,AsyncMock
import youtube_cookie_probe as runner


class CookieProbeRunnerTest(unittest.IsolatedAsyncioTestCase):
    async def test_expired_diagnostic_never_reads_secret_or_spawns(self):
        with patch('youtube_cookie_probe.Path.is_file',side_effect=AssertionError('Expired probe must be inert')):
            await runner.run_cookie_probe({},until=1)

    async def test_active_playback_skips_probe(self):
        player=N(current=object(),idle_mode=None,skip_pending=False,loading_task=None)
        with patch('youtube_cookie_probe.Path.is_file',return_value=True),patch('youtube_cookie_probe.asyncio.sleep',AsyncMock()),patch('youtube_cookie_probe.attempt',AsyncMock()) as attempt:
            await runner.run_cookie_probe({1:player},until=__import__('time').time()+300)
            attempt.assert_not_awaited()

    async def test_probe_is_killed_and_secret_workdir_cleaned_when_playback_starts(self):
        gate=asyncio.Event();paths=[]
        class Process:
            pid=1234;returncode=None
            async def communicate(self):await gate.wait();self.returncode=-9;return b'',b''
        async def spawn(*command,**kw):
            paths.append(Path(command[command.index('--cookie-work-dir')+1]));return Process()
        def kill(*args):gate.set()
        with patch('youtube_cookie_probe.asyncio.create_subprocess_exec',spawn),patch('youtube_cookie_probe.busy',return_value=True),patch('youtube_cookie_probe.os.killpg',side_effect=kill) as killed:
            row=await runner.attempt('stream-trial',{})
        self.assertEqual(row['result'],'aborted_for_playback');killed.assert_called_once()
        self.assertFalse(paths[0].exists())

    async def test_success_is_sanitized_and_temp_folder_cleaned(self):
        paths=[]
        class Process:
            pid=1234;returncode=0
            async def communicate(self):return json.dumps({'result':'decoded_audio','decoded_pcm_bytes':64000,'cookies':'secret'}).encode(),b''
        async def spawn(*command,**kw):
            paths.append(Path(command[command.index('--cookie-work-dir')+1]));return Process()
        with patch('youtube_cookie_probe.asyncio.create_subprocess_exec',spawn):row=await runner.attempt('stream-trial',{})
        self.assertEqual(row['result'],'decoded_audio');self.assertNotIn('cookies',row)
        self.assertFalse(paths[0].exists())

