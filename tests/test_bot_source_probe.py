import time
import unittest
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock
from bot_source_probe import audio_media, decode_sample, run_bot_source_probe

class ProbeTest(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_and_expired_do_not_send(self):
        client=Obj(get_users=AsyncMock(), send_message=AsyncMock())
        for expiry in (0,time.time()-1,time.time()+1800):
            await run_bot_source_probe(client,enabled_until=expiry)
        client.get_users.assert_not_called()
        client.send_message.assert_not_called()

    async def test_refuses_wrong_recipient_before_sending(self):
        for peer in (Obj(is_bot=False,username='LyBot'),Obj(is_bot=True,username='OtherBot')):
            client=Obj(get_users=AsyncMock(return_value=peer),send_message=AsyncMock())
            await run_bot_source_probe(client,enabled_until=time.time()+60)
            client.send_message.assert_not_called()

    def test_only_accepts_audio(self):
        self.assertIsNone(audio_media(Obj(document=Obj(mime_type='application/pdf'))))
        doc=Obj(mime_type='audio/mpeg')
        self.assertIs(audio_media(Obj(document=doc)),doc)

    async def test_decodes_telegram_style_audio_bytes_in_memory(self):
        import io, math, struct, wave
        buf=io.BytesIO()
        with wave.open(buf,'wb') as w:
            w.setnchannels(1);w.setsampwidth(2);w.setframerate(48000)
            w.writeframes(b''.join(struct.pack('<h',int(1200*math.sin(i*2*math.pi*440/48000))) for i in range(48000)))
        result=await decode_sample(buf.getvalue())
        self.assertEqual(result['exit_code'],0,result)
        self.assertEqual(result['pcm_bytes'],96000)
        self.assertGreater(result['peak'],0)
