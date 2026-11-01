import asyncio
import io
import unittest
from types import SimpleNamespace as N
from unittest.mock import AsyncMock, Mock

from music_owner_store import MusicOwnerStore, TAG, FILE
from music_owner_ui import MusicOwnerUI
from welcome_audio import WelcomeAudio


class StoreClient:
    def __init__(self, *, fail_edit=False, fail_all=False):
        self.docs = {}
        self.last = 40
        self.fail_edit = fail_edit
        self.fail_all = fail_all
        self.deleted = []
    async def search_messages(self, chat, query, limit):
        for mid, data in sorted(self.docs.items(), reverse=True)[:limit]:
            yield N(id=mid, caption=TAG, document=N(file_name=FILE,file_size=len(data)))
    async def download_media(self, msg, in_memory=True):
        return io.BytesIO(self.docs[msg.id])
    async def send_document(self, chat, file, **kwargs):
        if self.fail_all:raise ConnectionError('offline')
        self.last += 1
        self.docs[self.last]=file.read()
        return N(id=self.last)
    async def edit_message_media(self, chat, mid, media, **kwargs):
        if self.fail_all or self.fail_edit:raise RuntimeError('Edit rejected')
        with open(media.media,'rb') as infile:self.docs[mid]=infile.read()
    async def delete_messages(self, chat, mid):
        self.deleted.append(mid);self.docs.pop(mid,None)


class StoreRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_edit_failure_creates_new_verified_document_before_deleting_old(self):
        client=StoreClient(fail_edit=True)
        store=MusicOwnerStore(client,setup_code='x'*32)
        await store.load();await store.claim(7,'x'*32)
        old=store.message_id
        await store.set_broadcast(7,77)
        self.assertNotEqual(store.message_id,old)
        self.assertIn(old,client.deleted)
        restart=MusicOwnerStore(client);await restart.load()
        self.assertEqual(restart.data['broadcast'],{'chat_id':7,'message_id':77})
        self.assertTrue(restart.authorized(7))
    async def test_total_persistence_outage_does_not_claim_saved_state(self):
        client=StoreClient();store=MusicOwnerStore(client,setup_code='x'*32)
        await store.load();await store.claim(7,'x'*32)
        client.fail_all=True
        with self.assertRaises(ConnectionError):await store.set_broadcast(7,88)
        self.assertIsNone(store.data.get('broadcast'))


class StopDispatch(Exception):pass
def stop():raise StopDispatch()


class BroadcastFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client=StoreClient()
        self.store=MusicOwnerStore(self.client,setup_code='x'*32)
        await self.store.load();await self.store.claim(7,'x'*32)
        self.store.observe_group(N(id=-1001,title='One'))
        self.store.observe_group(N(id=-1002,title='Two'))
        self.handlers=[]
        parent=self
        class Bot:
            def on_message(self,*a,**k):
                return lambda fn:parent.handlers.append(fn) or fn
            def on_callback_query(self,*a,**k):
                return lambda fn:parent.handlers.append(fn) or fn
            copy_message=AsyncMock()
            send_message=AsyncMock()
        self.bot=Bot()
        self.ui=MusicOwnerUI(self.bot,self.store,WelcomeAudio(self.store),{})
        self.ui.register()
        self.ui.panel=42
        self.callback=next(x for x in self.handlers if x.__name__=='broadcast_callback')
        self.intake=next(x for x in self.handlers if x.__name__=='broadcast_draft')
        self.old_panel=N(id=42,chat=N(id=7),edit_text=AsyncMock())
        self.new_panel=N(id=43,chat=N(id=7),edit_text=AsyncMock())
        self.cb=lambda action,panel:self._callback(action,panel)
    def _callback(self,action,panel):
        return N(data='bc:'+action,from_user=N(id=7),message=panel,answer=AsyncMock())
    async def asyncTearDown(self):
        await self.store.close()

    async def test_forwarded_text_saved_without_copy_to_self_then_all_or_groups(self):
        await self.callback(None,self.cb('new',self.old_panel))
        self.assertTrue(self.ui.broadcast.waiting)
        self.assertEqual(self.ui.broadcast.selection,set())
        reply=AsyncMock(return_value=self.new_panel)
        msg=N(id=91,chat=N(id=7),from_user=N(id=7),text='New announcement',
              photo=None,video=None,audio=None,voice=None,document=None,animation=None,
              has_protected_content=False,reply_text=reply,stop_propagation=stop)
        with self.assertRaises(StopDispatch):
            await self.intake(None,msg)
        self.assertEqual(self.store.data['broadcast'],{'chat_id':7,'message_id':91})
        self.assertEqual(self.ui.panel,43)
        self.bot.copy_message.assert_not_awaited()
        keyboard=reply.call_args.kwargs['reply_markup']
        buttons=[b.callback_data for row in keyboard.inline_keyboard for b in row]
        self.assertIn('bc:all',buttons)
        self.assertIn('bc:pick:0',buttons)
        await self.callback(None,self.cb('all',self.new_panel))
        self.assertEqual(self.ui.broadcast.targets(),[-1002,-1001])
        await self.callback(None,self.cb('pick:0',self.new_panel))
        self.assertTrue(self.new_panel.edit_text.await_count>=2)
        restart=MusicOwnerStore(self.client);await restart.load()
        self.assertEqual(restart.data['broadcast']['message_id'],91)

    async def test_saved_messages_outage_allows_transparent_session_only_broadcast(self):
        await self.callback(None,self.cb('new',self.old_panel))
        self.store.set_broadcast=AsyncMock(side_effect=ConnectionError('offline'))
        msg=N(id=99,chat=N(id=7),from_user=N(id=7),text='Temporary',
              photo=None,video=None,audio=None,voice=None,document=None,animation=None,
              has_protected_content=False,reply_text=AsyncMock(return_value=self.new_panel),
              stop_propagation=stop)
        with self.assertRaises(StopDispatch):await self.intake(None,msg)
        self.assertEqual(self.ui.broadcast.source(),{'chat_id':7,'message_id':99})
        self.assertIn('Restart',msg.reply_text.call_args.args[0])
        self.ui.broadcast.selection=None
        await self.callback(None,self.cb('review',self.new_panel))
        self.bot.copy_message.assert_awaited_once_with(7,7,99)
        self.assertEqual(len(self.ui.broadcast.review['targets']),2)

    async def test_protected_forward_does_not_save_source(self):
        await self.callback(None,self.cb('new',self.old_panel))
        msg=N(id=99,from_user=N(id=7),text='Protected',
              photo=None,video=None,audio=None,voice=None,document=None,animation=None,
              has_protected_content=True,reply_text=AsyncMock(),stop_propagation=stop)
        with self.assertRaises(StopDispatch):await self.intake(None,msg)
        self.assertIsNone(self.store.data.get('broadcast'))
        msg.reply_text.assert_awaited_once()
