import asyncio
import unittest
from types import SimpleNamespace as O
from unittest.mock import AsyncMock, Mock
from bandwidth_saver import EmptyRoomSaver
from telegram_photo_cache import TelegramPhotoCache

class BandwidthChecks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now=1000
        self.p=O(current=O(),idle_mode=None,started_at=1,loading_task=None,paused=False,paused_since=0,paused_seconds=0,lock=asyncio.Lock())
        self.calls=O(pause=AsyncMock(),resume=AsyncMock(),get_participants=AsyncMock(return_value=[O(user_id=7)]))
        self.s=EmptyRoomSaver(self.calls,{1:self.p},7,O(mode=lambda c:'vc'),AsyncMock(),clock=lambda:self.now)
    async def test_empty_delay_arrival_resume_and_manual_pause(self):
        await self.s.check(1,self.p);self.now+=179
        await self.s.check(1,self.p);self.calls.pause.assert_not_called()
        self.now+=1;await self.s.check(1,self.p)
        self.assertTrue(self.p.paused);self.calls.pause.assert_awaited_once()
        self.now+=10;await self.s.arrival(1,8)
        self.assertFalse(self.p.paused);self.assertEqual(self.p.paused_seconds,10)
        self.p.paused=True;self.s.manual(1)
        self.calls.get_participants.return_value=[O(user_id=7),O(user_id=8)]
        await self.s.check(1,self.p);await self.s.arrival(1,8)
        self.assertTrue(self.p.paused);self.assertEqual(self.calls.resume.await_count,1)
    async def test_unknown_snapshot_or_error_never_proves_empty(self):
        for members in [[],[O(user_id=8)],[O(user_id=None)]]:
            self.calls.get_participants.return_value=members
            await self.s.check(1,self.p);self.now+=200;await self.s.check(1,self.p)
        self.calls.pause.assert_not_called()
        self.calls.get_participants.side_effect=RuntimeError()
        await self.s.check(1,self.p);self.calls.pause.assert_not_called()
    async def test_new_song_cannot_inherit_auto_resume(self):
        await self.s.check(1,self.p);self.now+=200;await self.s.check(1,self.p)
        self.p.current=O();self.p.paused=True
        await self.s.arrival(1,8)
        self.calls.resume.assert_not_called()
    async def test_silence_keeps_welcome_pipe_alive(self):
        self.p.current=None;self.p.idle_mode='silence'
        await self.s.check(1,self.p);self.now+=200;await self.s.check(1,self.p)
        self.calls.pause.assert_not_called()
    async def test_photo_reuse_and_invalid_reference_fallback(self):
        cache=TelegramPhotoCache();sent=O(photo=O(file_id='own-photo'))
        bot=O(send_photo=AsyncMock(return_value=sent))
        await cache.send(bot,1,'default',lambda:b'JPEG',caption='A')
        await cache.send(bot,2,'default',lambda:b'JPEG',caption='B')
        self.assertEqual(bot.send_photo.call_args.kwargs['photo'],'own-photo')
        class FileReferenceExpired(Exception):pass
        bot.send_photo.side_effect=[FileReferenceExpired(),sent]
        await cache.send(bot,3,'default',lambda:b'JPEG')
        self.assertEqual(bot.send_photo.call_args.kwargs['photo'],b'JPEG')
    async def test_photo_network_error_does_not_retry_upload(self):
        cache=TelegramPhotoCache();cache.items['x']='file'
        bot=O(send_photo=AsyncMock(side_effect=TimeoutError()))
        with self.assertRaises(TimeoutError):await cache.send(bot,1,'x',b'bytes')
        self.assertEqual(bot.send_photo.await_count,1)
    async def test_no_stream_or_manual_idle_pause_is_not_resumed(self):
        self.p.current=None;self.p.paused=True
        await self.s.arrival(1,8)
        self.calls.resume.assert_not_called()
    async def test_native_pause_failure_does_not_claim_paused(self):
        self.calls.pause.return_value=False
        await self.s.check(1,self.p);self.now+=200;await self.s.check(1,self.p)
        self.assertFalse(self.p.paused);self.assertNotIn(1,self.s.held)
    async def test_poll_recovers_missed_join_event(self):
        await self.s.check(1,self.p);self.now+=200;await self.s.check(1,self.p)
        self.calls.get_participants.return_value=[O(user_id=7),O(user_id=8)]
        await self.s.check(1,self.p)
        self.assertFalse(self.p.paused)
    async def test_join_handler_resumes_before_paused_welcome_check(self):
        import ast
        from pathlib import Path
        import main
        tree=ast.parse(Path(main.__file__).read_text())
        outer=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='main')
        fn=next(n for n in outer.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='welcome_vc_join')
        fn.decorator_list=[]
        self.p.paused=True
        async def arrival(chat,user):self.p.paused=False
        welcome=O(mode=lambda c:'vc',pipes={1:O(created=0)},styles={},request=Mock())
        assistant=O(get_users=AsyncMock(return_value=O(id=8,first_name='Sam',is_bot=False,is_deleted=False)))
        ns=dict(vars(main));ns.update(bandwidth_saver=O(arrival=AsyncMock(side_effect=arrival)),
              players={1:self.p},identity=O(id=7),welcome=welcome,assistant=assistant,calls=self.calls)
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'welcome-join','exec'),ns)
        await ns['welcome_vc_join'](None,O(chat_id=1,participant=O(user_id=8)))
        self.assertFalse(self.p.paused)
        welcome.request.assert_called_once()
    async def test_custom_photo_and_invalid_id_falls_back_to_bundle(self):
        from telegram_photo_cache import send_default_photo
        cache=TelegramPhotoCache();sent=O(photo=O(file_id='cached_id'))
        bot=O(send_photo=AsyncMock(return_value=sent))
        await send_default_photo(cache,bot,1,'AgAC_custom_123')
        self.assertEqual(bot.send_photo.call_args.kwargs['photo'],'AgAC_custom_123')
        class FileIdInvalid(Exception):pass
        bot.send_photo.side_effect=[FileIdInvalid(),sent]
        await send_default_photo(TelegramPhotoCache(),bot,1,'AgAC_invalid_123')
        self.assertTrue(bot.send_photo.call_args.kwargs['photo'].read().startswith(b'\xff\xd8'))
