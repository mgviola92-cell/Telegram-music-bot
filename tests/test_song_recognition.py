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

    async def test_return_original_recognized_audio_candidate(self):
        media = N(title='Purple', performer='Wink', file_size=5000, duration=168)
        original = N(id=12, audio=media, outgoing=False, reply_to_message_id=10)
        async def history(*args, **kwargs):
            yield original
        source = N(client=N(send_audio=AsyncMock(return_value=N(id=10)), get_chat_history=history),
                   lock=asyncio.Lock(), peer=N(id=123), started=True)
        title, candidate = await SongRecognizer(None, source).identify(Path('sample.mp3'), with_audio=True)
        self.assertEqual(title, 'Purple – Wink')
        self.assertIs(candidate.message, original)
        self.assertIs(candidate.source, source)
        self.assertEqual(candidate.duration, 168)

    async def test_audio_uploaded_by_our_bot_with_button_and_own_brand(self):
        from song_recognition import send_recognized_audio
        bot = N(get_me=AsyncMock(return_value=N(username='mudyar_music_bot')),
                send_audio=AsyncMock())
        markup = object()
        await send_recognized_audio(bot, N(chat=N(id=-100), id=23), '/tmp/audio.mp3',
                                    'Purple – Wink', 168, markup)
        args, kwargs = bot.send_audio.call_args
        self.assertEqual(args, (-100, '/tmp/audio.mp3'))
        self.assertEqual(kwargs['caption'], '@mudyar_music_bot')
        self.assertIs(kwargs['reply_markup'], markup)
        self.assertEqual(kwargs['reply_to_message_id'], 23)

    def test_play_callback_uses_real_chat_enum_and_uploaded_audio(self):
        import ast
        source = Path('main.py').read_text()
        module = ast.parse(source)
        node = next(n for n in ast.walk(module) if isinstance(n, ast.AsyncFunctionDef) and n.name == 'recognition_play')
        node.decorator_list = []
        namespace = {}
        from pyrogram.enums import ChatType
        play = AsyncMock()
        namespace.update(recognition_results={'a'*16: (-100, 7, 'Purple', float('inf'))},
                         time=__import__('time'), ChatType=ChatType, SimpleNamespace=N,
                         play_command=play, bot=object())
        # Remove annotations, which are unrelated to callback behavior.
        node.returns = None
        for arg in node.args.args:
            arg.annotation = None
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<callback>', 'exec'), namespace)
        async def check():
            message = N(chat=N(id=-100, type=ChatType.SUPERGROUP), audio=N(file_id='our-bot-file'),
                        edit_reply_markup=AsyncMock(), reply_text=AsyncMock())
            cb = N(data='findplay:'+'a'*16, message=message, from_user=N(id=7), answer=AsyncMock())
            await namespace['recognition_play'](None, cb)
            proxy = play.call_args.args[1]
            self.assertIs(proxy.reply_to_message, message)
            self.assertIs(proxy.reply_text, message.reply_text)
            self.assertNotIn('a'*16, namespace['recognition_results'])
        asyncio.run(check())
