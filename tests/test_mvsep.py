import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as N
from unittest.mock import patch
from aiohttp import web
import mvsep_service as ms
from karaoke_audio import audio_command, inspect_audio, prepare_input, prepare_result


class MVSEPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.path = Path(self.root.name)/'song.mp3'
        self.path.write_bytes(b'input')
        self.created = 0
        self.cleanup = []
        self.premium = 0
        self.status = 'done'
        self.auth = True
        self.queued = False
        self.found = True
        async def user(req):
            self.assertEqual(req.query.get('api_token'), 'test-token')
            return web.json_response({'success': self.auth, 'data': {
                'premium_enabled': self.premium, 'current_queue': ['job'] if self.queued else []}})
        async def create(req):
            self.created += 1
            form = await req.post()
            self.assertEqual(form['api_token'], 'test-token')
            self.assertEqual(form['sep_type'], '23')
            self.assertEqual(form['add_opt1'], '8')
            self.assertEqual(form['output_format'], '0')
            self.assertEqual(form['is_demo'], '0')
            self.assertEqual(form['audiofile'].file.read(), b'input')
            return web.json_response({'success': True, 'data': {'hash': 'test-job.mp3'}})
        async def get(req):
            self.assertNotIn('api_token', req.query)
            self.assertEqual(req.query.get('mirror'), '0')
            return web.json_response({'success': True, 'status': self.status, 'data': {'files': [
                {'type': 'Vocals', 'url': 'https://de2.mvsep.com/storage/vocals.mp3'},
                {'type': 'Instrum' if self.found else 'Other',
                 'url': 'https://de2.mvsep.com/storage/result.mp3'}]}})
        async def cleanup(req):
            self.cleanup.append(req.path.rsplit('/', 1)[1])
            form=await req.post()
            self.assertEqual(form['hash'], 'test-job.mp3')
            return web.json_response({'success': True})
        async def output(req):
            self.assertNotIn('Authorization', req.headers)
            self.assertNotIn('api_token', req.query)
            return web.Response(body=b'instrumental')
        app=web.Application()
        app.router.add_get('/api/app/user', user)
        app.router.add_post('/api/separation/create', create)
        app.router.add_get('/api/separation/get', get)
        app.router.add_post('/api/separation/cancel', cleanup)
        app.router.add_post('/api/separation/delete', cleanup)
        app.router.add_get('/storage/result.mp3', output)
        self.runner=web.AppRunner(app)
        await self.runner.setup()
        site=web.TCPSite(self.runner, '127.0.0.1', 0)
        await site.start()
        self.base='http://127.0.0.1:'+str(site._server.sockets[0].getsockname()[1])
        self.base_patch=patch('mvsep_service.BASE', self.base)
        self.base_patch.start()
        self.url_patch=patch('mvsep_service.output_url', lambda u: self.base+'/storage/result.mp3')
        self.url_patch.start()
    async def asyncTearDown(self):
        self.base_patch.stop()
        self.url_patch.stop()
        await self.runner.cleanup()
        self.root.cleanup()
    async def test_real_multipart_poll_instrumental_download_and_delete(self):
        out=Path(self.root.name)/'out.mp3'
        await ms.MVSEPSeparator('test-token').separate(self.path, out)
        self.assertEqual(out.read_bytes(), b'instrumental')
        self.assertEqual(self.created,1)
        self.assertEqual(self.cleanup,['delete'])
    async def test_premium_mode_refused_before_any_job_or_setting_change(self):
        self.premium=1
        with self.assertRaises(ms.KaraokeQuota):
            await ms.MVSEPSeparator('test-token').separate(self.path, self.path.with_name('out'))
        self.assertEqual(self.created,0)
        self.assertEqual(self.cleanup,[])
    async def test_existing_user_job_is_not_canceled_or_overlapped(self):
        self.queued=True
        with self.assertRaises(ms.KaraokeQuota):
            await ms.MVSEPSeparator('test-token').separate(self.path, self.path.with_name('out'))
        self.assertEqual(self.created,0)
        self.assertEqual(self.cleanup,[])
    async def test_bad_token_does_not_submit_or_retry(self):
        self.auth=False
        with self.assertRaises(ms.KaraokeUnavailable):
            await ms.MVSEPSeparator('test-token').separate(self.path, self.path.with_name('out'))
        self.assertEqual(self.created,0)
    async def test_cancel_only_our_job_and_no_resubmit(self):
        self.status='waiting'
        waiting=asyncio.Event()
        async def progress(status):
            if status=='waiting':waiting.set()
        task=asyncio.create_task(ms.MVSEPSeparator('test-token').separate(
            self.path, self.path.with_name('out'), progress))
        await asyncio.wait_for(waiting.wait(),2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(self.created,1)
        self.assertEqual(self.cleanup,['cancel','delete'])
    async def test_missing_instrumental_does_not_return_vocals(self):
        self.found=False
        with self.assertRaises(ms.KaraokeUnavailable):
            await ms.MVSEPSeparator('test-token').separate(self.path,self.path.with_name('out'))


class MediaTests(unittest.IsolatedAsyncioTestCase):
    def test_untrusted_hosts_redirect_targets_and_embedded_credentials_rejected(self):
        for url in ['https://mvsep.com.evil.test/storage/x.mp3','http://de.mvsep.com/storage/x',
                    'https://de.mvsep.com@evil.test/storage/x','https://de.mvsep.com:444/storage/x',
                    'https://de.mvsep.com/api/secrets','https://127.0.0.1/storage/x']:
            with self.assertRaises(ms.KaraokeUnavailable):ms.output_url(url)
        self.assertEqual(ms.output_url('https://de2.mvsep.com/storage/x.mp3'),
                         'https://de2.mvsep.com/storage/x.mp3')
    def test_feature_off_without_opt_in_even_with_token(self):
        with patch.dict(os.environ,{'MVSEP_API_TOKEN':'test-token'},clear=True):
            self.assertIsNone(ms.configured_provider())
    async def test_real_mp3_copy_decode_and_duration_check_preserves_source(self):
        with tempfile.TemporaryDirectory() as root,tempfile.TemporaryDirectory() as work:
            p=Path(root)/'song.mp3'
            await audio_command(['ffmpeg','-v','error','-f','lavfi','-i',
                'sine=frequency=440:duration=1','-c:a','libmp3lame','-y',str(p)])
            original=p.read_bytes()
            copied,seconds=await prepare_input(str(p),work)
            out=await prepare_result(copied,Path(work)/'out.mp3',seconds)
            duration,codec=await inspect_audio(out)
            self.assertEqual(codec,'mp3')
            self.assertAlmostEqual(duration,seconds,places=1)
            self.assertEqual(p.read_bytes(),original)
            with self.assertRaises(ms.KaraokeUnavailable):
                await prepare_result(copied,Path(work)/'bad.mp3',100)
    async def test_oversize_download_removes_partial(self):
        class Content:
            async def iter_chunked(self,size):
                yield b'123'
                yield b'456'
        with tempfile.TemporaryDirectory() as work:
            p=Path(work)/'out'
            with self.assertRaises(ms.KaraokeUnavailable):
                await ms.save_bounded(N(content=Content()),p,5)
            self.assertFalse(p.exists())
