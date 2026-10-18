import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace as N
from unittest.mock import AsyncMock, patch
from pyrogram.errors import FloodWait
from pyrogram.handlers import MessageHandler, EditedMessageHandler

from lybot_source import LyBotSource, BotSourceUnavailable
from source_replies import ReplyInbox, reply_hub
from song_recognition import SongRecognizer


class UpdateClient:
    def __init__(self):
        self.handlers=[]
        self.sent=asyncio.Event()
        self.reply=True
        self.peers={name:N(id=i+100,is_bot=True,username=name)
                    for i,name in enumerate(['LyBot','Song_Vkm_bot','VoiceShazamBot','VKMusicalRobot'])}
    def add_handler(self,handler,group=0):self.handlers.append((handler,group))
    async def get_users(self,name):return self.peers[name]
    async def emit(self,message,edited=False):
        kind=EditedMessageHandler if edited else MessageHandler
        for handler,_ in self.handlers:
            if type(handler) is kind:await handler.callback(self,message)
    async def send_message(self,chat,text,**kwargs):
        self.sent.set()
        if self.reply and text!='/start':
            # The reply can arrive before send_message returns to the caller.
            await self.emit(N(id=11,chat=N(id=chat),outgoing=False,
                              audio=N(title=text,performer='Artist',file_size=100,duration=120)))
        return N(id=10)
    async def get_chat_history(self,*args,**kwargs):
        raise AssertionError('Production update path must not request history')
        yield


class SourceUpdateTests(unittest.IsolatedAsyncioTestCase):
    def test_artist_number_spacing_preserves_title_and_variant_checks(self):
        from lybot_source import matches_query
        from main import suitable_recording
        for query in ['team 143 moment', 'moment team143']:
            for artist in ['TEAM143', 'Team 143']:
                self.assertTrue(matches_query(N(title='Moment', performer=artist), query))
                self.assertTrue(suitable_recording('Moment', query, artist))
                self.assertFalse(matches_query(N(title='Different', performer=artist), query))
                self.assertFalse(suitable_recording('Different', query, artist))
                self.assertFalse(suitable_recording('Moment Remix', query, artist))
                self.assertFalse(suitable_recording('Moment 8D', query, artist))

    async def test_fast_audio_response_before_send_returns_never_uses_history(self):
        client=UpdateClient();source=LyBotSource(client)
        result=await asyncio.wait_for(source.find('Moment'),.5)
        self.assertIn('Moment',result.label)
        self.assertFalse(source.lock.locked())
        self.assertEqual(reply_hub(client).waiters,{})

    async def test_four_sources_share_two_handlers_and_search_in_parallel(self):
        client=UpdateClient()
        sources=[LyBotSource(client,name) for name in client.peers]
        for source in sources:source.started=True
        found=await asyncio.wait_for(asyncio.gather(*(s.find('Moment') for s in sources)),.5)
        self.assertEqual(len(found),4)
        self.assertEqual(len(client.handlers),2)
        self.assertEqual(reply_hub(client).waiters,{})

    async def test_button_result_and_edited_audio_are_received_without_polling(self):
        client=UpdateClient();client.reply=False
        async def send(chat,text,**kwargs):
            await client.emit(N(id=11,chat=N(id=chat),outgoing=False,
                reply_markup=N(inline_keyboard=[[N(text='Moment – Artist',callback_data='song')]])))
            return N(id=10)
        client.send_message=send
        async def select(client,message,label):
            # Some providers edit their new response instead of sending another.
            await client.emit(N(id=11,chat=message.chat,outgoing=False,
                audio=N(title='Moment',performer='Artist',file_size=100,duration=120)),edited=True)
        source=LyBotSource(client)
        with patch('lybot_source.select_song',side_effect=select):
            found=await asyncio.wait_for(source.find('Moment'),.5)
        self.assertEqual(found.message.id,11)

    async def test_cancel_unregisters_request_and_releases_source_lock(self):
        client=UpdateClient();client.reply=False;source=LyBotSource(client)
        task=asyncio.create_task(source.find('Moment'))
        await client.sent.wait()
        self.assertTrue(reply_hub(client).waiters)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertFalse(source.lock.locked())
        self.assertEqual(reply_hub(client).waiters,{})
        # Late responses must not accumulate after cancellation.
        await client.emit(N(id=20,chat=N(id=100),outgoing=False))
        self.assertEqual(reply_hub(client).waiters,{})

    async def test_flood_wait_on_send_is_respected_and_does_not_leak_waiter(self):
        client=UpdateClient();source=LyBotSource(client)
        client.send_message=AsyncMock(side_effect=FloodWait(19))
        with self.assertRaises(BotSourceUnavailable):await source.find('Moment')
        self.assertFalse(source.lock.locked())
        self.assertEqual(reply_hub(client).waiters,{})

    async def test_request_ignores_other_chat_old_and_outgoing_messages(self):
        client=UpdateClient();inbox=ReplyInbox(client,100)
        await client.emit(N(id=20,chat=N(id=200),outgoing=False))
        await client.emit(N(id=20,chat=N(id=100),outgoing=True))
        await client.emit(N(id=9,chat=N(id=100),outgoing=False))
        task=asyncio.create_task(inbox.batch(10))
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        expected=N(id=12,chat=N(id=100),outgoing=False)
        await client.emit(expected)
        self.assertEqual(await task,[expected])
        inbox.close()

    async def test_pending_messages_are_bounded_and_edits_replace_same_id(self):
        client=UpdateClient();inbox=ReplyInbox(client,100)
        for i in range(60):
            await client.emit(N(id=i,chat=N(id=100),outgoing=False,text='first'))
        self.assertEqual(len(inbox.pending),32)
        await client.emit(N(id=59,chat=N(id=100),outgoing=False,text='edited'),edited=True)
        batch=await inbox.batch(10)
        self.assertEqual(batch[-1].text,'edited')
        self.assertEqual(len(batch),32)
        inbox.close()

    async def test_recognition_uses_same_update_hub_and_preserves_audio_candidate(self):
        client=UpdateClient();source=LyBotSource(client,'VoiceShazamBot');source.started=True
        source.peer=client.peers['VoiceShazamBot']
        async def send_audio(chat,*args,**kwargs):
            await client.emit(N(id=12,chat=N(id=chat),outgoing=False,reply_to_message_id=10,
                audio=N(title='Moment',performer='Team143',file_size=100,duration=120)))
            return N(id=10)
        client.send_audio=send_audio
        title,candidate=await asyncio.wait_for(SongRecognizer(None,source).identify(Path('sample.mp3'),with_audio=True),.5)
        self.assertEqual(title,'Moment – Team143')
        self.assertIs(candidate.source,source)
        self.assertFalse(source.lock.locked())
        self.assertEqual(reply_hub(client).waiters,{})
