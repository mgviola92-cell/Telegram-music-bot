import asyncio
import io
import json
import os
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace as N
from unittest.mock import AsyncMock,patch
from aiohttp import web
import karaoke_service as ks
from karaoke_ui import KaraokeUI
from music_ui import player_controls

class RemoteAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder=tempfile.TemporaryDirectory();self.input=Path(self.folder.name)/'input.mp3';self.input.write_bytes(b'input audio')
        self.mode='ok';self.requests=0
        async def upload(req):
            self.assertEqual(req.headers.get('x-hf-authorization'),'Bearer test-token')
            reader=await req.multipart();part=await reader.next();self.assertEqual(await part.read(),b'input audio')
            return web.json_response(['/tmp/input.mp3'])
        async def submit(req):
            self.requests+=1;data=await req.json();self.assertEqual(data['data'][1],'Standard — Instruments')
            return web.json_response({'event_id':'a'*32})
        async def poll(req):
            if self.mode=='quota':body='event: error\ndata: {"error":"ZeroGPU quota exceeded"}\n\n'
            elif self.mode=='badurl':body='event: complete\ndata: '+json.dumps([{'url':'https://evil.invalid/output.wav'}])+'\n\n'
            else:body='event: complete\ndata: '+json.dumps([{'url':self.base+'/gradio_api/file=/tmp/result.wav'}])+'\n\n'
            return web.Response(text=body,content_type='text/event-stream')
        app=web.Application();app.router.add_post('/gradio_api/upload',upload);app.router.add_post('/gradio_api/call/separate',submit)
        app.router.add_get('/gradio_api/call/separate/{event}',poll);app.router.add_get('/gradio_api/file=/tmp/result.wav',lambda r:web.Response(body=b'output audio'))
        self.runner=web.AppRunner(app);await self.runner.setup();site=web.TCPSite(self.runner,'127.0.0.1',0);await site.start()
        self.base='http://127.0.0.1:'+str(site._server.sockets[0].getsockname()[1]);self.patch=patch('karaoke_service.HF_BASE',self.base);self.patch.start()
    async def asyncTearDown(self):
        self.patch.stop();await self.runner.cleanup();self.folder.cleanup()
    async def test_full_hf_upload_queue_and_result_download(self):
        out=Path(self.folder.name)/'out.wav';await ks.HFSeparator('test-token').separate(self.input,out)
        self.assertEqual(out.read_bytes(),b'output audio');self.assertEqual(self.requests,1)
    async def test_quota_failure_is_not_retried_or_rotated(self):
        self.mode='quota'
        with self.assertRaises(ks.KaraokeQuota):await ks.HFSeparator('test-token').separate(self.input,Path(self.folder.name)/'out')
        self.assertEqual(self.requests,1)
    async def test_untrusted_result_url_is_rejected(self):
        self.mode='badurl'
        with self.assertRaises(ks.KaraokeUnavailable):await ks.HFSeparator('test-token').separate(self.input,Path(self.folder.name)/'out')

class LimitsTests(unittest.IsolatedAsyncioTestCase):
    def test_disabled_without_explicit_opt_in_and_credentials(self):
        with patch.dict(os.environ,{},clear=True):self.assertIsNone(ks.configured_provider())
        with patch.dict(os.environ,{'MUSIC_KARAOKE_ENABLED':'1','KARAOKE_PROVIDER':'hf'},clear=True):self.assertIsNone(ks.configured_provider())
        with self.assertRaises(ValueError):ks.ModalSeparator('https://test.modal.run','key','secret')
        with self.assertRaises(ValueError):ks.modal_url('https://127.0.0.1/separate')
        with self.assertRaises(ValueError):ks.modal_url('https://test.modal.run@evil.invalid')
    def test_player_button_is_hidden_until_configured(self):
        with patch.dict(os.environ,{},clear=True):
            callbacks=[b.callback_data for row in player_controls(-100).inline_keyboard for b in row]
            self.assertNotIn('karaoke:current',callbacks)
        with patch.dict(os.environ,{'MUSIC_KARAOKE_ENABLED':'1','HF_TOKEN':'test'},clear=True):
            callbacks=[b.callback_data for row in player_controls(-100).inline_keyboard for b in row]
            self.assertIn('karaoke:current',callbacks)
    async def test_media_copy_is_disposable_and_does_not_delete_playing_source(self):
        with tempfile.TemporaryDirectory() as root,tempfile.TemporaryDirectory() as work:
            source=Path(root)/'song.wav'
            with wave.open(str(source),'wb') as f:f.setparams((2,2,44100,0,'NONE','not compressed'));f.writeframes(bytes(44100*4))
            copied,seconds=await ks.prepare_input(str(source),work)
            self.assertNotEqual(copied,str(source));self.assertTrue(source.exists());self.assertAlmostEqual(seconds,1,places=1)
            out=await ks.encode_result(copied,Path(work)/'result.mp3',seconds)
            self.assertGreater(Path(out).stat().st_size,0);self.assertTrue(source.exists())
    async def test_download_size_limit_removes_partial_file(self):
        class Content:
            async def iter_chunked(self,size):yield b'123';yield b'456'
        with tempfile.TemporaryDirectory() as work:
            p=Path(work)/'partial'
            with self.assertRaises(ks.KaraokeUnavailable):await ks.save_bounded(N(content=Content()),p,5)
            self.assertFalse(p.exists())

class UIIsolationTests(unittest.IsolatedAsyncioTestCase):
    def context(self):
        handlers=[];bot=N(on_callback_query=lambda *a,**k:lambda f:handlers.append(f) or f)
        current=N();ui=KaraokeUI(bot,lambda *a:('Song','/tmp/song.mp3',180),AsyncMock(return_value=True),AsyncMock())
        ui.provider=N(separate=AsyncMock());ui.register()
        cb=N(data='karaoke:current',from_user=N(id=7),message=N(id=4,chat=N(id=-100)),answer=AsyncMock())
        return ui,handlers[0],cb,current
    async def test_double_click_reserves_only_one_independent_job(self):
        ui,callback,cb,current=self.context();started=asyncio.Event()
        async def run(*a):started.set();await asyncio.Event().wait()
        ui.run=run
        await callback(None,cb);await started.wait();task=ui.job['task'];await callback(None,cb)
        self.assertIs(ui.job['task'],task);self.assertEqual(len(ui.recent),1)
        await ui.close();self.assertTrue(task.cancelled())
    async def test_non_admin_and_missing_provider_never_submit(self):
        ui,callback,cb,_=self.context();ui.is_admin.return_value=False
        await callback(None,cb);self.assertIsNone(ui.job)
        ui.is_admin.return_value=True;ui.provider=None;await callback(None,cb);self.assertIsNone(ui.job)
    async def test_remote_quota_error_cleans_job_without_enqueueing(self):
        ui,callback,cb,_=self.context();panel=N(id=9,edit_text=AsyncMock());cb.message.reply_text=AsyncMock(return_value=panel)
        ui.provider.separate.side_effect=ks.KaraokeQuota('quota')
        with patch('karaoke_ui.prepare_input',AsyncMock(return_value=('/tmp/copy',180))):
            await callback(None,cb);task=ui.job['task'];await task
        self.assertIsNone(ui.job);ui.enqueue.assert_not_awaited();self.assertIn('quota',panel.edit_text.call_args.args[0])
