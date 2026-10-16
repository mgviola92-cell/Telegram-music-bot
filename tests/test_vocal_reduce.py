"""Real DSP measurements: centre attenuation, mono safety and live URL wiring."""
import math
import shlex
import struct
import subprocess
import tempfile
import wave
from pathlib import Path
import unittest
from unittest.mock import AsyncMock,patch
from types import SimpleNamespace as N
from audio_options import AudioSettings,ProcessedAudioStream,apply_filters
from network_audio import DirectAudioStream
from pytgcalls.types import MediaStream


def render(source,effect='vocal',channels=2,quality='high'):
    args=['ffmpeg','-nostdin','-v','error','-f','lavfi','-i',source,
          '-t','1','-ar','48000','-ac',str(channels),'-f','s16le','pipe:1']
    result=subprocess.run(apply_filters(args,AudioSettings(effect=effect,quality=quality)),
                          capture_output=True,timeout=5)
    if result.returncode:raise AssertionError(result.stderr.decode())
    samples=struct.unpack('<'+'h'*(len(result.stdout)//2),result.stdout)
    return [samples[c::channels][4800:43200] for c in range(channels)]


def rms(samples):
    return math.sqrt(sum(x*x for x in samples)/len(samples))


class VocalReduceTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_pytgcalls_capture_command_decodes_vocal_pcm(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'source.wav'
            samples=[]
            for n in range(24000):
                centre=.2*math.sin(2*math.pi*1000*n/48000)
                side=.1*math.sin(2*math.pi*440*n/48000)
                samples.extend((int((centre+side)*32767),int((centre-side)*32767)))
            with wave.open(str(path),'wb') as f:
                f.setparams((2,2,48000,0,'NONE','not compressed'))
                f.writeframes(struct.pack('<'+'h'*len(samples),*samples))
            stream=ProcessedAudioStream(str(path),AudioSettings(effect='vocal'))
            await stream.check_stream()
            args=shlex.split(stream.microphone.path)
            result=subprocess.run(args,capture_output=True,timeout=8)
            self.assertEqual(result.returncode,0,result.stderr.decode())
            self.assertGreater(len(result.stdout),90000)
    def test_centre_is_reduced_twenty_db_but_never_fully_muted(self):
        source='aevalsrc=0.2*sin(2*PI*1000*t)|0.2*sin(2*PI*1000*t):s=48000:d=1'
        normal=render(source,'normal')[0]
        reduced=render(source)[0]
        self.assertAlmostEqual(20*math.log10(rms(reduced)/rms(normal)),-20,delta=.3)
        self.assertGreater(rms(reduced),100)
    def test_stereo_accompaniment_survives_phone_mono_downmix(self):
        source='aevalsrc=0.2*sin(2*PI*440*t)|-0.2*sin(2*PI*440*t):s=48000:d=1'
        left,right=render(source)
        mono=[(l+r)/2 for l,r in zip(left,right)]
        self.assertGreater(rms(mono),4000)
        self.assertAlmostEqual(rms(mono),rms(left),delta=1)
    def test_genuine_mono_is_not_attenuated(self):
        source='sine=frequency=1000:sample_rate=48000:duration=1'
        normal=render(source,'normal')[0]
        reduced=render(source)[0]
        self.assertAlmostEqual(rms(reduced)/rms(normal),1,delta=.02)
    def test_all_output_qualities_keep_reduction_before_mono_conversion(self):
        source='aevalsrc=0.2*sin(2*PI*1000*t)|0.2*sin(2*PI*1000*t):s=48000:d=1'
        for quality in ('high','medium','low'):
            channels=2 if quality=='high' else 1
            normal=render(source,'normal',channels=channels,quality=quality)[0]
            reduced=render(source,channels=channels,quality=quality)[0]
            self.assertAlmostEqual(rms(reduced)/rms(normal),.1,delta=.01)
    def test_expression_survives_native_command_quoting_roundtrip(self):
        args=apply_filters(['ffmpeg','-i','https://cf-hls-media.sndcdn.com/test.m3u8','-f','s16le','pipe:1'],AudioSettings(effect='vocal'))
        self.assertEqual(shlex.split(shlex.join(args)),args)
        self.assertNotIn("'",AudioSettings(effect='vocal').filters())
    async def test_direct_url_and_local_stream_use_same_live_filter(self):
        base=['ffmpeg','-i','https://cf-hls-media.sndcdn.com/test.m3u8','-f','s16le','pipe:1']
        async def check(stream):stream.microphone=N(path=shlex.join(base))
        with patch.object(MediaStream,'check_stream',check):
            for stream in (DirectAudioStream(base[2],AudioSettings(effect='vocal')),
                           ProcessedAudioStream('/tmp/test.mp3',AudioSettings(effect='vocal'))):
                await stream.check_stream()
                args=shlex.split(stream.microphone.path)
                self.assertEqual(args[args.index('-i')+1],base[2])
                self.assertIn('aeval=',args[args.index('-af')+1])
                self.assertNotIn('download',args)
