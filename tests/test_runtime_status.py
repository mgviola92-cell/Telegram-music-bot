import unittest
from unittest.mock import patch,AsyncMock
from types import SimpleNamespace as O
from runtime_status import RuntimeStatus,uptime
from music_home import MusicHome
from telegram_photo_cache import TelegramPhotoCache

class RuntimeChecks(unittest.TestCase):
    def test_container_memory_cpu_and_on_demand_cache(self):
        with patch('runtime_status.time.monotonic',return_value=10),patch('runtime_status.cpu_sample',return_value=(1,.5)):
            status=RuntimeStatus()
        with patch('runtime_status.time.monotonic',return_value=20),patch('runtime_status.cpu_sample',return_value=(2,.5)) as cpu,patch('runtime_status.memory_sample',return_value=(128*1048576,512*1048576)),patch('runtime_status.shutil.disk_usage',return_value=O(total=1024,used=256,free=768)):
            text=status.text()
            self.assertIn('20.0%',text);self.assertIn('128.0 MB / 512 MB · 25.0%',text)
            self.assertIn('25.0% သုံးပြီး',text)
            self.assertEqual(status.text(),text);self.assertEqual(cpu.call_count,1)
    def test_unavailable_samples_never_fake_zero_remaining(self):
        with patch('runtime_status.cpu_sample',side_effect=ValueError()):s=RuntimeStatus()
        with patch('runtime_status.cpu_sample',side_effect=ValueError()),patch('runtime_status.memory_sample',return_value=(None,None)),patch('runtime_status.shutil.disk_usage',side_effect=OSError()):
            text=s.text()
            self.assertEqual(text.count('မရရှိသေး'),3)
            self.assertNotIn('0%',text)
        self.assertIn('1 ရက် 22 နာရီ',uptime(86400+22*3600))

class HomeChecks(unittest.IsolatedAsyncioTestCase):
    async def test_photo_refresh_and_help_edit_same_card_without_another_upload(self):
        handlers=[]
        class Bot:
            get_me=AsyncMock(return_value=O(username='mudyar_music_test'))
            send_photo=AsyncMock(return_value=O(id=4,photo=O(file_id='own-photo-id')))
            def on_callback_query(self,*a,**kw):return lambda f:handlers.append(f) or f
        bot=Bot();store=O(data={});home=MusicHome(bot,store,{},TelegramPhotoCache());home.register()
        user=O(id=7,first_name='<Sam>')
        msg=O(chat=O(id=7),id=1,from_user=user,reply_text=AsyncMock())
        await home.show(msg)
        self.assertIn('&lt;Sam&gt;',bot.send_photo.call_args.kwargs['caption'])
        self.assertLessEqual(len(bot.send_photo.call_args.kwargs['caption']),1500)
        panel=O(chat=O(id=7),id=4,edit_caption=AsyncMock())
        cb=O(message=panel,from_user=user,data='home:help',answer=AsyncMock())
        await handlers[0](None,cb)
        self.assertIn('/cmd',panel.edit_caption.call_args.args[0])
        self.assertEqual(bot.send_photo.await_count,1)
        self.assertFalse(msg.reply_text.called)
        cb.from_user=O(id=8)
        await handlers[0](None,cb)
        self.assertTrue(cb.answer.call_args.kwargs['show_alert'])
