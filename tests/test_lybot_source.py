import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock, Mock, patch
from lybot_source import LyBotSource, BotSourceUnavailable, resolve_with_fallback, song_button, matches_selection

class LyBotTest(unittest.IsolatedAsyncioTestCase):
    async def test_primary_success_does_not_contact_lybot(self):
        prepared=Obj(consume=AsyncMock(return_value=('remote','Song',120)),discard=Mock())
        source=Obj(cached=Mock(return_value=None),resolve=AsyncMock())
        self.assertEqual(await resolve_with_fallback(prepared,'Song',source),('remote','Song',120))
        source.resolve.assert_not_called()

    async def test_slow_primary_is_discarded_before_fallback(self):
        async def slow():
            await asyncio.Event().wait()
        prepared=Obj(consume=slow,discard=Mock())
        source=Obj(cached=Mock(return_value=None),resolve=AsyncMock(return_value=('local','LyBot song',120)))
        result=await resolve_with_fallback(prepared,'Song',source,primary_timeout=.001)
        self.assertEqual(result[0],'local')
        prepared.discard.assert_called_once()
        source.resolve.assert_awaited_once_with('Song')

    async def test_stop_cancels_without_sending_fallback(self):
        prepared=Obj(consume=AsyncMock(side_effect=asyncio.CancelledError),discard=Mock())
        source=Obj(cached=Mock(return_value=None),resolve=AsyncMock())
        with self.assertRaises(asyncio.CancelledError):
            await resolve_with_fallback(prepared,'Song',source)
        source.resolve.assert_not_called()
        prepared.discard.assert_called_once()

    async def test_cache_hit_skips_primary_and_bot(self):
        prepared=Obj(consume=AsyncMock(),discard=Mock())
        source=Obj(cached=Mock(return_value=('cached','Song',120)),resolve=AsyncMock())
        self.assertEqual((await resolve_with_fallback(prepared,'Song',source))[0],'cached')
        prepared.consume.assert_not_called()
        source.resolve.assert_not_called()

    def test_buttons_exclude_links_navigation_and_payment(self):
        def msg(*buttons):return Obj(reply_markup=Obj(inline_keyboard=[list(buttons)]))
        self.assertIsNone(song_button(msg(Obj(text='▶️',callback_data='next'),Obj(text='Subscribe – Premium',callback_data='buy'),Obj(text='Song – Artist',url='https://example.com'))))
        self.assertEqual(song_button(msg(Obj(text='Song – Artist',callback_data='track'))),'Song – Artist')
        self.assertFalse(matches_selection(Obj(title='Late wrong song'),'Song – Artist'))
        self.assertTrue(matches_selection(Obj(title='Song'),'Song – Artist'))
        self.assertFalse(matches_selection(Obj(title='ကာ'),'ကိ – Artist'))

    async def test_observed_button_audio_exchange_downloads_complete_file(self):
        audio=Obj(title='Song',performer='Artist',file_size=5,duration=120,file_name='song.mp3')
        result=Obj(id=12,outgoing=False,reply_markup=Obj(inline_keyboard=[[Obj(text='Song – Artist',callback_data='track')]]))
        media=Obj(id=14,outgoing=False,audio=audio)
        class Client:
            def __init__(self):self.selected=False
            async def get_users(self,name):return Obj(id=123,is_bot=True,username='LyBot')
            async def send_message(self,*a,**kw):return Obj(id=10)
            async def get_chat_history(self,*a,**kw):
                if self.selected:yield media
                else:yield result
            async def stream_media(self,*a,**kw):
                yield b'abc';yield b'de'
        client=Client()
        async def select(*a):client.selected=True
        source=LyBotSource(client)
        with patch.object(source,'cached',return_value=None),patch('lybot_source.select_song',side_effect=select),patch('lybot_source.save_cached_song') as save:
            path,title,duration=await source.resolve('Song')
        try:
            self.assertEqual(Path(path).read_bytes(),b'abcde')
            self.assertIn('LyBot',title)
            self.assertEqual(duration,120)
            save.assert_called_once()
            self.assertFalse(source.lock.locked())
        finally:shutil.rmtree(Path(path).parent)

    async def test_incomplete_file_is_cleaned_and_lock_released(self):
        audio=Obj(title='Song',file_size=10,duration=120,file_name='song.mp3')
        result=Obj(id=12,outgoing=False,reply_markup=Obj(inline_keyboard=[[Obj(text='Song – Artist',callback_data='track')]]))
        media=Obj(id=14,outgoing=False,audio=audio)
        class Client:
            selected=False
            async def get_users(self,name):return Obj(id=123,is_bot=True,username='LyBot')
            async def send_message(self,*a,**kw):return Obj(id=10)
            async def get_chat_history(self,*a,**kw):
                if self.selected:yield media
                else:yield result
            async def stream_media(self,*a,**kw):yield b'incomplete'
        client=Client()
        async def select(*a):client.selected=True
        source=LyBotSource(client)
        with tempfile.TemporaryDirectory() as root:
            folder=Path(root)/'partial';folder.mkdir()
            with patch.object(source,'cached',return_value=None),patch('lybot_source.select_song',side_effect=select),patch('lybot_source.tempfile.mkdtemp',return_value=str(folder)):
                # Advertised size is deliberately larger than actual content.
                audio.file_size=99
                with self.assertRaises(BotSourceUnavailable):await source.resolve('Song')
            self.assertFalse(folder.exists())
            self.assertFalse(source.lock.locked())

    async def test_new_bot_direct_audio_with_zero_duration_is_probed(self):
        import io,wave
        buf=io.BytesIO()
        with wave.open(buf,'wb') as wav:
            wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(8000)
            wav.writeframes(b'\0\0'*8000)
        data=buf.getvalue()
        audio=Obj(title='Purple',performer='Wink & pGb',file_size=len(data),duration=0,file_name='song.wav')
        message=Obj(id=14,outgoing=False,audio=audio)
        class Client:
            async def get_users(self,name):return Obj(id=456,is_bot=True,username='VoiceShazamBot')
            async def send_message(self,*a,**kw):return Obj(id=10)
            async def get_chat_history(self,*a,**kw):yield message
            async def stream_media(self,*a,**kw):yield data
        source=LyBotSource(Client(),'VoiceShazamBot')
        with patch.object(source,'cached',return_value=None),patch('lybot_source.save_cached_song'):
            path,title,duration=await source.resolve('purple pgb wink')
        try:
            self.assertEqual(duration,1)
            self.assertIn('Purple – Wink & pGb',title)
            self.assertIn('VoiceShazamBot',title)
        finally:shutil.rmtree(Path(path).parent)

    def test_new_bot_duration_badge_and_artist_title_order(self):
        message=Obj(reply_markup=Obj(inline_keyboard=[[Obj(text='• 2:49 • Purple — pGb & WINK',callback_data='track')]]))
        self.assertEqual(song_button(message,query='purple pgb wink'),'• 2:49 • Purple — pGb & WINK')
        self.assertTrue(matches_selection(Obj(title='Purple'),'• 2:49 • Purple — pGb & WINK'))
        self.assertTrue(matches_selection(Obj(title='Purple'),'Wink & pGb — Purple'))

    def test_subscription_gate_is_reported_without_clicking_join_or_payment(self):
        from lybot_source import subscription_gate
        gate=Obj(text="To access the bot features, you need to subscribe to resources:",reply_markup=Obj(inline_keyboard=[[Obj(text='➕ Subscribe'),Obj(text='Check subscriptions')]]))
        self.assertTrue(subscription_gate(gate))
        self.assertFalse(subscription_gate(Obj(text='Song search results',reply_markup=Obj(inline_keyboard=[[Obj(text='Purple — Wink & pGb')]]))))
