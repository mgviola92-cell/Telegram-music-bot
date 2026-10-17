import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace as N
from unittest.mock import AsyncMock,patch
from lyrics_service import LyricsLookup
from lyric_sources import TelegramLyrics,LyricsResult,lyric_text,lyric_button

class RaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_valid_wins_and_other_is_cancelled(self):
        cancelled=asyncio.Event()
        async def slow(*args):
            try:await asyncio.sleep(10)
            finally:cancelled.set()
        lookup=LyricsLookup(N(find=slow));lookup.lrclib=AsyncMock(return_value=('text','Perfect'))
        result=await lookup.find('Perfect',263)
        self.assertEqual(result,('text','Perfect'));self.assertEqual(result.source,'LRCLIB')
        self.assertTrue(cancelled.is_set())
    async def test_missing_or_error_does_not_hide_other_source(self):
        for value in (None,ValueError()):
            lookup=LyricsLookup(N(find=AsyncMock(return_value=LyricsResult('text','Song','VoiceShazamBot'))))
            lookup.lrclib=AsyncMock(side_effect=value) if isinstance(value,Exception) else AsyncMock(return_value=value)
            result=await lookup.find('Song');self.assertEqual(result.source,'VoiceShazamBot')
    async def test_duplicate_groups_share_job_and_cache(self):
        calls=[]
        async def resolve(*args):calls.append(1);await asyncio.sleep(.01);return ('text','Song')
        lookup=LyricsLookup();lookup.lrclib=resolve
        a,b=await asyncio.gather(lookup.find('Song'),lookup.find('Song'))
        self.assertEqual(a,b);await lookup.find('Song');self.assertEqual(calls,[1])
    async def test_music_interrupts_telegram_but_http_can_finish(self):
        started=asyncio.Event();source=N(lock=asyncio.Lock())
        telegram=TelegramLyrics(source)
        async def fake_find(*args):
            telegram.tasks.add(asyncio.current_task());started.set()
            try:await asyncio.sleep(10)
            finally:telegram.tasks.discard(asyncio.current_task())
        telegram.find=fake_find;lookup=LyricsLookup(telegram)
        async def http(*args):await started.wait();telegram.interrupt();await asyncio.sleep(.01);return ('text','Song')
        lookup.lrclib=http
        self.assertEqual(await lookup.find('Song'),('text','Song'));self.assertFalse(telegram.tasks)

class TelegramTests(unittest.IsolatedAsyncioTestCase):
    async def test_busy_source_skips_without_search_or_download(self):
        source=N(lock=asyncio.Lock(),find=AsyncMock())
        await source.lock.acquire()
        self.assertIsNone(await TelegramLyrics(source).find('Song'));source.find.assert_not_awaited()
        source.lock.release()
        self.assertIsNone(await TelegramLyrics(source,lambda:True).find('Song'));source.find.assert_not_awaited()
    async def test_extract_callback_text_and_release_lock(self):
        message=N(id=10,reply_markup=N(inline_keyboard=[[N(text='Lyrics',callback_data='lyr')]]))
        async def history(*a,**k):yield N(id=10)
        source=N(username='VoiceShazamBot',peer=N(id=42),lock=asyncio.Lock(),find=AsyncMock(return_value=N(message=message,label='Song',duration=None)),
                 client=N(get_messages=AsyncMock(return_value=message),get_chat_history=history))
        text=('a lyric line with words\n'*6)
        with patch('lyric_sources.select_song',AsyncMock(return_value=N(message=text))):
            result=await TelegramLyrics(source).find('Song')
        self.assertEqual(result[0],text.strip());self.assertFalse(source.lock.locked())
    def test_prompts_and_links_are_not_lyrics(self):
        for text in ['searching\n'*20,'https://example.com\n'*10,'lyrics not found\n'*10]:self.assertIsNone(lyric_text(N(text=text)))
        self.assertIsNone(lyric_button(N(reply_markup=N(inline_keyboard=[[N(text='Lyrics',url='https://example.com')]]))))
