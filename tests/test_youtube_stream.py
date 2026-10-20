import asyncio
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace as N
import unittest
from unittest.mock import patch, AsyncMock

import youtube_stream as yt


class ResolveTest(unittest.TestCase):
    def setUp(self):
        self.old_cooldown = yt._COOLDOWN_UNTIL
        yt._COOLDOWN_UNTIL = 0
    def tearDown(self):
        yt._COOLDOWN_UNTIL = self.old_cooldown

    def test_stream_uses_scoped_cookie_copy_and_never_downloads_media(self):
        with tempfile.TemporaryDirectory() as folder:
            secret = Path(folder)/'cookies.txt'
            raw = ('# Netscape HTTP Cookie File\n'
                   '.youtube.com\tTRUE\t/\tTRUE\t1999999999\tSID\tprivate-value\n'
                   '.other.org\tTRUE\t/\tTRUE\t1999999999\tSID\tunrelated-value\n')
            secret.write_text(raw); secret.chmod(0o400)
            copies=[]
            class Extractor:
                def __init__(self, opts):
                    copies.append(Path(opts['cookiefile']))
                    assert 'unrelated-value' not in copies[-1].read_text()
                    assert copies[-1].stat().st_mode & 0o777 == 0o600
                    assert opts['skip_download']
                    assert 'quickjs' in opts['js_runtimes']
                    assert opts['format'] == 'bestaudio/best'
                    assert 'extractor_args' not in opts
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def extract_info(self, target, download):
                    assert download is False
                    return dict(url='https://r1.googlevideo.com/videoplayback?test=1',
                                title='Sample', duration=120, tbr=400, format_id='18')
            with patch('yt_dlp.YoutubeDL', Extractor), patch('runtime_status.memory_sample', return_value=(0,0)):
                result=yt.resolve('Sample', cookiefile=str(secret))
            self.assertEqual(secret.read_text(), raw)
            self.assertFalse(copies[0].exists())
            self.assertIn('YouTube (live)', result[1])
            from player_visuals import allowed_audio_cdn
            self.assertTrue(allowed_audio_cdn(result[0]))
            self.assertFalse(allowed_audio_cdn('https://r1.googlevideo.com/not-extracted'))

    def test_memory_guard_skips_before_cookie_read_or_extraction(self):
        with patch('youtube_resources.over_budget', return_value=True), patch('youtube_stream.MozillaCookieJar') as jar:
            with self.assertRaises(RuntimeError): yt.resolve('Sample')
            jar.assert_not_called()

    def test_bad_cookie_failure_does_not_expose_cookie_path_or_value(self):
        with tempfile.TemporaryDirectory() as folder:
            secret=Path(folder)/'private-cookie';secret.write_text('secret-value')
            with patch('runtime_status.memory_sample', return_value=(0,0)):
                with self.assertRaises(RuntimeError) as error: yt.resolve('Sample',cookiefile=str(secret))
            self.assertNotIn('secret-value',str(error.exception))
            self.assertNotIn(folder,str(error.exception))

    def test_media_host_rejects_credentials_and_lookalike_domains(self):
        for url in ['http://r1.googlevideo.com/a','https://googlevideo.com.evil.org/a',
                    'https://u:p@r1.googlevideo.com/a','https://r1.googlevideo.com:8443/a']:
            self.assertFalse(yt.media_url(url))


class RaceTest(unittest.IsolatedAsyncioTestCase):
    async def test_memory_pressure_kills_worker_group_and_removes_private_cookie_folder(self):
        old=yt._COOLDOWN_UNTIL;yt._COOLDOWN_UNTIL=0
        gate=asyncio.Event();folders=[]
        class Process:
            pid=12345;returncode=None
            async def communicate(self):
                await gate.wait();self.returncode=-9;return b'',b''
        async def spawn(*cmd,**kwargs):
            folders.append(Path(cmd[cmd.index('--work-dir')+1]))
            self.assertTrue(kwargs['start_new_session'])
            self.assertNotIn('BOT_TOKEN',kwargs['env'])
            self.assertEqual(kwargs['env']['TMPDIR'],str(folders[-1]))
            return Process()
        try:
            with patch('youtube_stream.asyncio.create_subprocess_exec',spawn),patch('youtube_resources.over_budget',return_value=True),patch('youtube_stream.os.killpg',side_effect=lambda *args:gate.set()) as kill:
                with self.assertRaises(RuntimeError):await yt.resolve_bounded('Sample')
            kill.assert_called_once()
            self.assertFalse(folders[0].exists())
        finally:yt._COOLDOWN_UNTIL=old

    async def test_existing_source_can_win_without_waiting_for_youtube(self):
        started=threading.Event(); cancelled=threading.Event()
        async def lookup(query, **kwargs):
            started.set()
            try:await asyncio.Future()
            finally:cancelled.set()
        async def existing(*args):
            await asyncio.to_thread(started.wait,1)
            return ('https://a.sndcdn.com/audio','Sample · SoundCloud',120)
        with patch('youtube_stream.resolve_bounded',lookup):
            result=await asyncio.wait_for(yt.race(existing,N(source='Sample'),True,lambda _:None),1)
            self.assertIn('SoundCloud',result[1])
            self.assertTrue(await asyncio.to_thread(cancelled.wait,1))

    async def test_failed_youtube_keeps_existing_source_running(self):
        async def lookup(*args, **kwargs): raise RuntimeError('unavailable')
        async def existing(*args):
            await asyncio.sleep(.01)
            return ('local.mp3','Sample',120)
        with patch('youtube_stream.resolve_bounded',lookup):
            result=await yt.race(existing,N(source='Sample'),True,lambda _:None)
        self.assertEqual(result[0],'local.mp3')

    async def test_paused_player_is_not_counted_and_unknown_bitrate_is_not_fabricated(self):
        old=(yt._BYTES,yt._UNKNOWN_SECONDS,dict(yt._RATES))
        yt._BYTES=0;yt._UNKNOWN_SECONDS=0;yt._RATES.clear();yt._RATES['url']=800
        players={1:N(current=N(output_speed=1),temp_file='url',paused=True)}
        steps=0
        async def tick(_):
            nonlocal steps
            steps+=1
            if steps==2: players[1].paused=False
            if steps==3: raise asyncio.CancelledError()
        try:
            with patch('youtube_stream.asyncio.sleep',side_effect=tick),patch('youtube_stream.time.monotonic',side_effect=[0,2,4]):
                with self.assertRaises(asyncio.CancelledError): await yt.meter(players)
            self.assertEqual(yt._BYTES,200000)
            self.assertIn('ခန့်မှန်း',yt.usage_text())
            self.assertIn('ဤ run',yt.usage_text())
        finally:
            yt._BYTES,yt._UNKNOWN_SECONDS=old[:2];yt._RATES.clear();yt._RATES.update(old[2])

class RuntimeDiagnosticsTest(unittest.TestCase):
    def test_only_fixed_runtime_failure_codes_are_retained(self):
        for text,code in [('QuickJS: out of memory at secret-url','js_heap_limit'),
                          ('QuickJS: stack overflow at secret-url','js_stack_limit'),
                          ('QuickJS executable not found secret-path','js_runtime_missing')]:
            logger=yt.QuietLogger();logger.warning(text)
            self.assertEqual(logger.runtime_failure,code)
            self.assertNotIn('secret',repr(logger.__dict__))
