import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as N
from unittest.mock import AsyncMock
from song_recognition import SongRecognizer, recognition_media, recognized_title, make_sample
from lybot_source import BotSourceUnavailable


class RecognitionTests(unittest.IsolatedAsyncioTestCase):
    def test_supported_media_and_rejected_document(self):
        self.assertIsNone(recognition_media(N(document=N(mime_type='application/pdf'))))
        self.assertIsNotNone(recognition_media(N(voice=N(file_id='voice'))))
        self.assertIsNotNone(recognition_media(N(document=N(mime_type='video/mp4'))))

    def test_actual_audio_metadata(self):
        self.assertEqual(recognized_title(N(audio=N(title='Purple', performer='Wink & pGb'))), 'Purple – Wink & pGb')
        self.assertIsNone(recognized_title(N(text='Searching…')))

    async def test_shared_lock_and_fresh_response(self):
        async def history(*args, **kwargs):
            yield N(id=9, audio=N(title='Old', performer=''), outgoing=False)
            yield N(id=11, audio=N(title='Wrong reply', performer=''), outgoing=False, reply_to_message_id=8)
            yield N(id=12, audio=N(title='Purple', performer='Wink'), outgoing=False, reply_to_message_id=10)
        client = N(send_audio=AsyncMock(return_value=N(id=10)), get_chat_history=history)
        source = N(client=client, lock=asyncio.Lock(), peer=N(id=123), started=True)
        recognizer = SongRecognizer(None, source)
        self.assertEqual(await recognizer.identify(Path('sample.mp3')), 'Purple – Wink')
        self.assertFalse(source.lock.locked())
        client.send_audio.assert_awaited_once()

    async def test_wrong_identity_releases_lock(self):
        source = N(client=N(get_users=AsyncMock(return_value=N(is_bot=True, username='wrong'))),
                   lock=asyncio.Lock(), peer=None, username='VoiceShazamBot', started=False)
        with self.assertRaises(BotSourceUnavailable):
            await SongRecognizer(None, source).identify(Path('sample.mp3'))
        self.assertFalse(source.lock.locked())

    async def test_reject_large_before_download(self):
        bot = N(download_media=AsyncMock())
        recognizer = SongRecognizer(bot, None)
        with self.assertRaises(ValueError):
            await recognizer.recognize(N(video=N(file_size=21*1024*1024)))
        bot.download_media.assert_not_awaited()

    async def test_real_video_sample(self):
        with tempfile.TemporaryDirectory() as folder:
            video, output = Path(folder)/'test.mp4', Path(folder)/'sample.mp3'
            process = await asyncio.create_subprocess_exec('ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                'color=c=black:s=64x64:r=5', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1',
                '-t', '1', '-c:v', 'mpeg4', '-c:a', 'aac', str(video))
            self.assertEqual(await process.wait(), 0)
            await make_sample(video, output)
            self.assertGreater(output.stat().st_size, 1000)

    async def test_download_failure_cleanup_and_busy_reset(self):
        paths = []
        async def fail(*args, **kwargs):
            paths.append(Path(kwargs['file_name']))
            paths[-1].write_bytes(b'partial')
            raise RuntimeError('download failed')
        recognizer = SongRecognizer(N(download_media=fail), None)
        with self.assertRaises(RuntimeError):
            await recognizer.recognize(N(audio=N(file_size=100, file_id='x')))
        self.assertFalse(recognizer.busy)
        self.assertFalse(paths[0].parent.exists())
