import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as N
import unittest
from unittest.mock import patch,AsyncMock
import youtube_cookie_probe as runner
from tools.youtube_direct_probe import probe,install_samsung_client


class CookieProbeTest(unittest.TestCase):
    def test_samsung_config_isolated_copy_preserves_existing_clients(self):
        from copy import deepcopy
        from yt_dlp.extractor.youtube._base import INNERTUBE_CLIENTS
        original=deepcopy(INNERTUBE_CLIENTS)
        try:
            install_samsung_client()
            self.assertEqual(INNERTUBE_CLIENTS['tv_samsung']['INNERTUBE_CONTEXT']['client']['deviceMake'],'Samsung')
            self.assertEqual(INNERTUBE_CLIENTS['tv_downgraded'],original['tv_downgraded'])
        finally:
            INNERTUBE_CLIENTS.clear();INNERTUBE_CLIENTS.update(original)

    def test_missing_and_invalid_cookie_file_are_sanitized(self):
        self.assertEqual(probe('cookies','url')['result'],'not_tested_no_user_cookies')
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'cookie.txt';path.write_text('private-invalid-session')
            result=probe('cookies','url',str(path))
            self.assertEqual(result['result'],'invalid_cookie_file')
            self.assertNotIn('private-invalid-session',json.dumps(result))

    def test_readonly_secret_is_copied_filtered_and_deleted_without_logging_values(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'cookies.txt'
            raw='# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t1999999999\tSID\tprivate-test-value\n.example.org\tTRUE\t/\tTRUE\t1999999999\tSID\tunrelated\n'
            path.write_text(raw);path.chmod(0o400)
            captured=[]
            class Extract:
                def __init__(self,opts):
                    copy=Path(opts['cookiefile']);captured.append(copy)
                    self.assertions=copy.read_text()
                    assert 'private-test-value' in self.assertions
                    assert 'unrelated' not in self.assertions
                    assert copy!=path
                def __enter__(self):return self
                def __exit__(self,*args):pass
                def extract_info(self,*args,**kwargs):raise RuntimeError('private-test-value signed-url')
            with patch('yt_dlp.YoutubeDL',Extract):result=probe('cookies','url',str(path),cookie_work_dir=folder)
            self.assertEqual(path.read_text(),raw)
            self.assertFalse(captured[0].exists())
            self.assertNotIn('private-test-value',json.dumps(result))

    def test_expired_and_other_domain_cookies_are_not_used(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'cookies.txt'
            path.write_text('# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t1\tSID\texpired\n')
            self.assertEqual(probe('cookies','url',str(path))['result'],'no_unexpired_youtube_cookies')


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
            row=await runner.attempt('cookies',{})
        self.assertEqual(row['result'],'aborted_for_playback');killed.assert_called_once()
        self.assertFalse(paths[0].exists())

    async def test_success_is_sanitized_and_temp_folder_cleaned(self):
        paths=[]
        class Process:
            pid=1234;returncode=0
            async def communicate(self):return json.dumps({'result':'decoded_audio','decoded_pcm_bytes':64000,'cookies':'secret'}).encode(),b''
        async def spawn(*command,**kw):
            paths.append(Path(command[command.index('--cookie-work-dir')+1]));return Process()
        with patch('youtube_cookie_probe.asyncio.create_subprocess_exec',spawn):row=await runner.attempt('cookies',{})
        self.assertEqual(row['result'],'decoded_audio');self.assertNotIn('cookies',row)
        self.assertFalse(paths[0].exists())
