import asyncio
import io
import json
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from music_owner_store import MusicOwnerStore, TAG, FILE, DEFAULTS, validate_text, parse_state
from music_owner_ui import MusicOwnerUI
from welcome_audio import WelcomeAudio

class SavedClient:
    def __init__(self):self.raw=None;self.writes=0;self.fail=False
    async def search_messages(self,*args,**kwargs):
        if self.fail:raise RuntimeError('offline')
        if self.raw is not None:
            yield SimpleNamespace(id=42,caption=TAG,document=SimpleNamespace(file_name=FILE,file_size=len(self.raw)))
    async def download_media(self,*args,**kwargs):return io.BytesIO(self.raw)
    async def send_document(self,chat,file,**kwargs):
        if self.fail:raise RuntimeError('offline')
        assert chat=='me'
        self.raw=file.read();self.writes+=1;return SimpleNamespace(id=42)
    async def edit_message_media(self,chat,message,media,**kwargs):
        if self.fail:raise RuntimeError('offline')
        assert chat=='me' and message==42
        assert isinstance(media.media,str), 'Pyrogram document replacement requires a path'
        path=Path(media.media)
        assert path.is_file() and path.stat().st_mode & 0o777 == 0o600
        self.edited_path=path
        self.raw=path.read_bytes();self.writes+=1

class OwnerStoreChecks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client=SavedClient();self.store=MusicOwnerStore(self.client,'x'*32)
        await self.store.load()
    async def asyncTearDown(self):await self.store.close()

    async def test_one_time_owner_binding_survives_restart(self):
        self.assertFalse(self.store.authorized(7))
        self.assertFalse(await self.store.claim(7,'wrong'))
        self.assertTrue(await self.store.claim(7,'x'*32))
        self.assertTrue(self.store.authorized(7))
        self.assertFalse(await self.store.claim(8,'x'*32))
        restart=MusicOwnerStore(self.client,'x'*32);await restart.load()
        self.assertTrue(restart.authorized(7))
        self.assertFalse(await restart.claim(8,'x'*32))
        self.assertNotIn('x'*32,self.client.raw.decode())

    async def test_custom_text_voice_and_groups_reload(self):
        self.store.observe_group(SimpleNamespace(id=-1001,title='မြန်မာ Group'))
        await self.store.claim(7,'x'*32)
        await self.store.set_text(7,'vc','မင်္ဂလာပါ {name}။ ဝင်လာတာ ဝမ်းသာပါတယ်။')
        await self.store.set_voice(7,'female')
        restart=MusicOwnerStore(self.client);await restart.load()
        self.assertIn('ခန့်',restart.text('vc','ခန့်'))
        self.assertEqual(restart.data['voice'],'female')
        self.assertEqual(restart.data['groups']['-1001'],'မြန်မာ Group')
        self.assertEqual(restart.message_id,42)
        self.assertFalse(self.client.edited_path.exists())
        welcome=WelcomeAudio(restart)
        self.assertEqual(welcome.voice(-1001),'female')
        self.assertIn('ခန့်',welcome.render('ခန့်'))
        welcome.voices[-1001]='male'
        self.assertEqual(welcome.voice(-1001),'male')

    async def test_non_owner_cannot_modify_settings(self):
        await self.store.claim(7,'x'*32)
        for action in [self.store.set_text(8,'vc','Hello'),self.store.reset_text(8,'vc'),
                       self.store.set_voice(8,'female')]:
            with self.assertRaises(PermissionError):await action
        self.assertEqual(self.client.writes,1)

    async def test_failed_save_does_not_change_live_settings(self):
        await self.store.claim(7,'x'*32)
        self.client.fail=True
        with self.assertRaises(RuntimeError):await self.store.set_text(7,'vc','New greeting')
        self.assertEqual(self.store.text('vc','Name'),DEFAULTS['vc'].format(name='Name'))
        self.client.fail=False

    async def test_corrupt_state_and_network_failure_lock_owner_controls(self):
        self.client.raw=b'{broken'
        restart=MusicOwnerStore(self.client,'x'*32);await restart.load()
        self.assertFalse(restart.ready)
        self.assertFalse(await restart.claim(7,'x'*32))
        self.client.fail=True
        await restart.load()
        self.assertFalse(restart.ready)
        self.client.fail=False;self.client.raw=None

    async def test_reset_restores_only_selected_template(self):
        await self.store.claim(7,'x'*32)
        await self.store.set_text(7,'vc','VC')
        await self.store.set_text(7,'party_batch','Batch')
        await self.store.reset_text(7,'vc')
        self.assertEqual(self.store.text('party_batch'),'Batch')
        self.assertEqual(self.store.text('vc','A'),DEFAULTS['vc'].format(name='A'))

    async def test_persistence_probe_never_claims_an_owner(self):
        await self.store.probe_roundtrip()
        self.assertTrue(self.store.ready)
        self.assertIsNone(self.store.data['owner'])
        self.assertTrue(await self.store.claim(7,'x'*32))

    def test_invalid_placeholders_and_long_text_rejected(self):
        for text in ['{name.__class__}','{unknown}','{name!r}','{name:100000}','{','a'*601,'\x00']:
            with self.assertRaises(ValueError):validate_text('party',text)
        self.assertEqual(validate_text('vc','Hello {name}'),'Hello {name}')
        with self.assertRaises(ValueError):validate_text('party_batch','Hello {name}')

class OwnerUIChecks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client=SavedClient();self.store=MusicOwnerStore(self.client,'x'*32);await self.store.load()
        await self.store.claim(7,'x'*32)
        self.handlers=[]
        class Bot:
            def on_message(inner,*args,**kwargs):return lambda f:self.handlers.append(f) or f
            def on_callback_query(inner,*args,**kwargs):return lambda f:self.handlers.append(f) or f
            send_message=AsyncMock()
        self.bot=Bot();self.w=WelcomeAudio(self.store)
        self.ui=MusicOwnerUI(self.bot,self.store,self.w,{})
        self.ui.register();self.ui.panel=42
    async def asyncTearDown(self):await self.store.close()

    async def test_forwarded_or_other_user_buttons_cannot_mutate(self):
        callback=next(f for f in self.handlers if f.__name__=='owner_callback')
        for user,chat in [(8,8),(7,-1001),(7,8)]:
            cb=SimpleNamespace(from_user=SimpleNamespace(id=user),message=SimpleNamespace(id=42,chat=SimpleNamespace(id=chat)),
                               data='own:voice:female',answer=AsyncMock())
            await callback(None,cb)
            self.assertEqual(self.store.data['voice'],'male')
            cb.answer.assert_awaited_once()

    def test_every_template_submenu_keeps_owner_navigation(self):
        for key in DEFAULTS:
            _,k=self.ui.menu(key)
            self.assertTrue(any(b.callback_data=='own:home' for row in k.inline_keyboard for b in row))
            self.assertTrue(all(len(b.callback_data.encode())<=64 for row in k.inline_keyboard for b in row))

    async def test_card_template_owner_flow_persists_without_tts(self):
        callback=next(f for f in self.handlers if f.__name__=='owner_callback')
        panel=SimpleNamespace(id=42,chat=SimpleNamespace(id=7),edit_text=AsyncMock())
        cb=SimpleNamespace(from_user=SimpleNamespace(id=7),message=panel,
                           data='own:cardedit:queued',answer=AsyncMock())
        await callback(None,cb)
        self.assertEqual(self.ui.pending[7][0],'card-queued')
        handler=next(f for f in self.handlers if f.__name__=='owner_text')
        class Stop(Exception):pass
        def stop():raise Stop()
        msg=SimpleNamespace(from_user=SimpleNamespace(id=7),text='➜ #{position} {title} · {requester}',
                            reply_text=AsyncMock(),stop_propagation=stop)
        with self.assertRaises(Stop):await handler(None,msg)
        restart=MusicOwnerStore(self.client);await restart.load()
        self.assertEqual(restart.data['cards']['queued'],msg.text)
        self.assertNotIn(7,self.ui.pending)
        self.assertFalse(self.ui.busy)
        cb.data='own:cardreset:queued'
        await callback(None,cb)
        self.assertNotIn('queued',self.store.data['cards'])

    async def test_default_photo_upload_reset_and_owner_only(self):
        callback=next(f for f in self.handlers if f.__name__=='owner_callback')
        recording=next(f for f in self.handlers if f.__name__=='owner_recording')
        panel=SimpleNamespace(id=42,chat=SimpleNamespace(id=7),edit_text=AsyncMock())
        cb=SimpleNamespace(from_user=SimpleNamespace(id=7),message=panel,data='own:photo-upload',answer=AsyncMock())
        await callback(None,cb)
        self.assertEqual(self.ui.record_pending[7][0],'card-photo')
        class Stop(Exception):pass
        def stop():raise Stop()
        msg=SimpleNamespace(from_user=SimpleNamespace(id=7),text=None,photo=SimpleNamespace(file_id='AgAC_valid_photo_123',file_size=12345),reply_text=AsyncMock(),stop_propagation=stop)
        with self.assertRaises(Stop):await recording(None,msg)
        restored=MusicOwnerStore(self.client);await restored.load()
        self.assertEqual(restored.data['default_photo'],'AgAC_valid_photo_123')
        self.assertFalse(self.ui.busy)
        self.assertNotIn(7,self.ui.record_pending)
        with self.assertRaises(PermissionError):await self.store.set_default_photo(8,None)
        cb.data='own:photo-reset';await callback(None,cb)
        restored=MusicOwnerStore(self.client);await restored.load()
        self.assertIsNone(restored.data['default_photo'])
        self.assertTrue(restored.authorized(7))

    async def test_document_does_not_replace_photo_and_cancel_clears_pending(self):
        callback=next(f for f in self.handlers if f.__name__=='owner_callback')
        recording=next(f for f in self.handlers if f.__name__=='owner_recording')
        panel=SimpleNamespace(id=42,chat=SimpleNamespace(id=7),edit_text=AsyncMock())
        cb=SimpleNamespace(from_user=SimpleNamespace(id=7),message=panel,data='own:photo-upload',answer=AsyncMock())
        await callback(None,cb)
        class Stop(Exception):pass
        def stop():raise Stop()
        msg=SimpleNamespace(from_user=SimpleNamespace(id=7),text=None,photo=None,reply_text=AsyncMock(),stop_propagation=stop)
        with self.assertRaises(Stop):await recording(None,msg)
        self.assertFalse(self.store.data.get('default_photo'))
        self.assertIn(7,self.ui.record_pending)
        cb.data='own:photo';await callback(None,cb)
        self.assertNotIn(7,self.ui.record_pending)
