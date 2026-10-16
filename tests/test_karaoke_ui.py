import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as N
from unittest.mock import AsyncMock,patch
from karaoke_ui import KaraokeUI,resources_available
from mvsep_service import KaraokeQuota


class UIIsolationTests(unittest.IsolatedAsyncioTestCase):
    def context(self):
        handlers=[]
        bot=N(on_callback_query=lambda *a,**k:lambda f:handlers.append(f) or f)
        source=('Actual Song','/tmp/song.mp3',180)
        ui=KaraokeUI(bot,lambda *a:source,AsyncMock(return_value=True),AsyncMock())
        ui.provider=N(separate=AsyncMock(),preflight=AsyncMock())
        ui.register()
        cb=N(data='karaoke:current',from_user=N(id=7),message=N(id=4,chat=N(id=-100)),answer=AsyncMock())
        return ui,handlers[0],cb
    async def test_double_click_reserves_only_one_independent_job(self):
        ui,callback,cb=self.context()
        started=asyncio.Event()
        async def run(*a):
            started.set()
            await asyncio.Event().wait()
        ui.run=run
        with patch('karaoke_ui.resources_available',return_value=True):
            await callback(None,cb)
            await started.wait()
            task=ui.job['task']
            await callback(None,cb)
        self.assertIs(ui.job['task'],task)
        self.assertEqual(len(ui.recent),1)
        await ui.close()
        self.assertTrue(task.cancelled())
    async def test_non_admin_and_missing_provider_never_submit(self):
        ui,callback,cb=self.context()
        ui.is_admin.return_value=False
        await callback(None,cb)
        self.assertIsNone(ui.job)
        ui.is_admin.return_value=True
        ui.provider=None
        await callback(None,cb)
        self.assertIsNone(ui.job)
    async def test_low_resources_only_declines_karaoke_and_never_enqueues(self):
        ui,callback,cb=self.context()
        with patch('karaoke_ui.resources_available',return_value=False):
            await callback(None,cb)
        self.assertIsNone(ui.job)
        ui.enqueue.assert_not_awaited()
        self.assertEqual(len(ui.recent),0)
    async def test_remote_quota_error_cleans_job_without_enqueueing(self):
        ui,callback,cb=self.context()
        panel=N(id=9,edit_text=AsyncMock())
        cb.message.reply_text=AsyncMock(return_value=panel)
        ui.provider.separate.side_effect=KaraokeQuota('quota')
        with patch('karaoke_ui.prepare_input',AsyncMock(return_value=('/tmp/copy',180))),patch('karaoke_ui.resources_available',return_value=True):
            await callback(None,cb)
            task=ui.job['task']
            await task
        self.assertIsNone(ui.job)
        ui.enqueue.assert_not_awaited()
        self.assertIn('quota',panel.edit_text.call_args.args[0])
    async def test_failed_preflight_does_not_fetch_full_song(self):
        ui,callback,cb=self.context()
        cb.message.reply_text=AsyncMock(return_value=N(id=9,edit_text=AsyncMock()))
        ui.provider.preflight.side_effect=KaraokeQuota('quota')
        with patch('karaoke_ui.prepare_input',AsyncMock()) as prepare,patch('karaoke_ui.resources_available',return_value=True):
            await callback(None,cb)
            task=ui.job['task']
            await task
        prepare.assert_not_awaited()
        ui.provider.separate.assert_not_awaited()
        self.assertIsNone(ui.job)
    async def test_completed_file_does_not_play_until_explicit_button(self):
        ui,callback,cb=self.context()
        panel=N(id=9,edit_text=AsyncMock(),delete=AsyncMock())
        cb.message.reply_text=AsyncMock(return_value=panel)
        sent=N(id=33)
        ui.bot.send_audio=AsyncMock(return_value=sent)
        with patch('karaoke_ui.prepare_input',AsyncMock(return_value=('/tmp/copy',180))),patch('karaoke_ui.prepare_result',AsyncMock(return_value='/tmp/out.mp3')),patch('karaoke_ui.resources_available',return_value=True):
            await callback(None,cb)
            task=ui.job['task']
            await task
        ui.enqueue.assert_not_awaited()
        token=next(iter(ui.results))
        cb.data='karaoke:play:'+token
        cb.message=N(id=33,chat=N(id=-100))
        await callback(None,cb)
        ui.enqueue.assert_awaited_once_with(cb,sent)
    async def test_success_result_cannot_be_queued_from_other_message(self):
        ui,callback,cb=self.context()
        import time
        ui.results['abc']={'until':time.monotonic()+100,'chat':-100,'message':N(id=33)}
        cb.data='karaoke:play:abc'
        await callback(None,cb)
        ui.enqueue.assert_not_awaited()
    def test_disk_headroom_guard(self):
        with patch('karaoke_ui.shutil.disk_usage',return_value=N(free=120*1024*1024)):
            self.assertFalse(resources_available())
