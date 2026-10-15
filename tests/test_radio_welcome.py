import asyncio
import math
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from radio_catalog import RadioCatalog, safe_url
from welcome_audio import GreetingPipe, WelcomeAudio, greeting_text, mixing_command

class RadioChecks(unittest.IsolatedAsyncioTestCase):
    def test_reject_unknown_private_and_credentials(self):
        for url in ['http://ice5.somafm.com/a','https://127.0.0.1/a',
                    'https://evil.example/a','https://u:p@stream.zeno.fm/a',
                    'https://stream.zeno.fm:22/a']:
            self.assertFalse(safe_url(url))
        self.assertTrue(safe_url('https://cherry.akiyaresearch.com:444/stream/89/'))

    def test_menu_pagination_and_no_url_callbacks(self):
        c=RadioCatalog()
        _,k=c.menu(-100,'inter',1)
        self.assertLessEqual(len(k.inline_keyboard),8)
        self.assertTrue(all(len(b.callback_data.encode())<=64
                        for row in k.inline_keyboard for b in row))
        self.assertNotIn('https:',str(k))

    async def test_directory_json_can_arrive_in_multiple_chunks(self):
        import json
        payload=json.dumps([{'name':'Local FM','url_resolved':'https://stream.zeno.fm/example'}]).encode()
        class Content:
            async def iter_chunked(self,size):
                yield payload[:7]
                yield payload[7:]
        class Response:
            content=Content()
            def raise_for_status(self): pass
            async def __aenter__(self): return self
            async def __aexit__(self,*args): pass
        class Session:
            def __init__(self,**kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self,*args): pass
            def get(self,*args,**kwargs): return Response()
        catalog=RadioCatalog()
        with patch('radio_catalog.ClientSession',Session):
            await catalog.load_local()
        self.assertTrue(catalog.loaded)
        self.assertEqual(catalog.stations['mm1'].name,'Local FM')

    async def test_failed_station_is_bounded_and_retried_later(self):
        c=RadioCatalog()
        with patch('radio_catalog.decode_check',side_effect=RuntimeError) as check:
            for _ in range(2):
                with self.assertRaises(RuntimeError): await c.verify('groovesalad')
            self.assertEqual(check.call_count,1)

class WelcomeChecks(unittest.IsolatedAsyncioTestCase):
    def test_text_strips_markup_and_controls(self):
        text=greeting_text('<a>Bob\n\x00</a>')
        self.assertNotIn('<',text)
        self.assertNotIn('\x00',text)
        self.assertIn('ကြိုဆိုပါတယ်',text)

    async def test_cooldown_and_no_stale_delivery_after_stop(self):
        w=WelcomeAudio();w.ready=True
        class Pipe:
            def __init__(self):self.audio=[]
            def offer(self,data): self.audio.append(data)
            def close(self):pass
        p=Pipe();w.pipes[1]=p
        gate=asyncio.Event()
        async def pcm(*args):await gate.wait();return b'pcm'
        w.pcm=pcm
        self.assertTrue(w.request(1,2,'Name'))
        self.assertFalse(w.request(1,3,'Other'))
        w.close_chat(1);gate.set()
        await asyncio.gather(*w.tasks,return_exceptions=True)
        self.assertEqual(p.audio,[])
        await w.close()

    async def test_real_ffmpeg_overlay_and_music_continuity(self):
        # Three seconds of music, a brief louder welcome, then music continues.
        import wave
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'music.wav'
            with wave.open(str(path),'wb') as f:
                f.setparams((2,2,48000,0,'NONE','not compressed'))
                f.writeframes(b''.join(struct.pack('<hh',int(5000*math.sin(i*2*math.pi*220/48000)),
                                            int(5000*math.sin(i*2*math.pi*220/48000)))
                                     for i in range(48000*3)))
            pipe=GreetingPipe()
            try:
                voice=b''.join(struct.pack('<hh',int(10000*math.sin(i*2*math.pi*880/48000)),
                                              int(10000*math.sin(i*2*math.pi*880/48000)))
                               for i in range(48000//2))
                pipe.offer(voice)
                cmd=mixing_command(['ffmpeg','-nostdin','-v','error','-i',str(path),
                          '-t','3','-ar','48000','-ac','2','-f','s16le','pipe:1'],pipe.path)
                proc=await asyncio.create_subprocess_exec(*cmd,stdout=asyncio.subprocess.PIPE,
                                                         stderr=asyncio.subprocess.PIPE)
                try:
                    output,error=await asyncio.wait_for(proc.communicate(),8)
                finally:
                    if proc.returncode is None:
                        proc.kill();await proc.wait()
                self.assertEqual(proc.returncode,0,error.decode())
                self.assertGreaterEqual(len(output),48000*4*2.9)
                samples=struct.unpack('<'+'h'*(len(output)//2),output)
                self.assertGreater(max(abs(x) for x in samples[-9600:]),3000)
                self.assertGreater(max(abs(x) for x in samples[:48000]),8000)
                def music_amplitude(start,end):
                    mono=samples[::2]
                    a,b=int(start*48000),int(end*48000)
                    return abs(sum(mono[i]*math.sin(i*2*math.pi*220/48000) for i in range(a,b))*2/(b-a))
                self.assertLess(min(music_amplitude(i/10,i/10+.1) for i in range(1,12)),
                                music_amplitude(2,2.2)*.15)
            finally:pipe.close()
            self.assertFalse(pipe.path.exists())

    def test_output_filter_order_and_quality_preserved(self):
        cmd=mixing_command(['ffmpeg','-headers','User-Agent: Test\r\n','-i','https://x',
                  '-af','bass=g=6','-ar','24000','-ac','1','-f','s16le','pipe:1'],'/tmp/fifo')
        self.assertEqual(cmd[cmd.index('-headers')+1],'User-Agent: Test\r\n')
        self.assertIn('[0:a]bass=g=6[music]',cmd[cmd.index('-filter_complex')+1])
        self.assertEqual(cmd[-1],'pipe:1')
        self.assertEqual(cmd[cmd.index('-map')+1],'[out]')
