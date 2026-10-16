import ast
import asyncio
import struct
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace as N
from unittest.mock import AsyncMock,Mock
import main
from audio_options import AudioSettings,EFFECTS,EFFECT_LABELS,apply_filters
from music_ui import player_controls
from effect_probe import measure_effect

class NewEffectTests(unittest.IsolatedAsyncioTestCase):
    def test_inline_picker_exposes_all_presets(self):
        markup=player_controls(-100,menu='effects')
        callbacks=[b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]
        self.assertEqual({c.split(':')[1][2:] for c in callbacks if c.startswith('mud:fx')},set(EFFECTS))
        self.assertTrue(all(len(c.encode())<=64 for c in callbacks))
        self.assertNotIn('meow',EFFECTS)
    def test_fun_voices_change_pitch_and_speed_consistently(self):
        for effect in ('chipmunk','tiny'):
            setting=AudioSettings(effect=effect);rate=48000
            args=['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=2',
                  '-ar',str(rate),'-ac','2','-f','s16le','pipe:1']
            result=subprocess.run(apply_filters(args,setting),capture_output=True,timeout=5)
            self.assertEqual(result.returncode,0,result.stderr.decode())
            samples=struct.unpack('<'+'h'*(len(result.stdout)//2),result.stdout)[::2]
            self.assertAlmostEqual(len(samples)/rate,2/setting.speed,delta=.06)
            part=samples[4800:48000];crossings=sum(a<=0<b for a,b in zip(part,part[1:]))
            frequency=crossings/(len(part)/rate)
            self.assertAlmostEqual(frequency,440*setting.speed,delta=20)
    async def test_new_inline_effects_update_next_track_without_restarting_current(self):
        tree=ast.parse(Path('main.py').read_text())
        node=next(n for n in ast.walk(tree) if isinstance(n,ast.AsyncFunctionDef) and n.name=='panel_callback')
        node.decorator_list=[]
        for effect in EFFECTS:
            current=main.Track('Current','query');player=main.Player(current=current);player.panel_id=4
            ns=dict(vars(main));ns.update(players={-100:player},is_chat_admin=AsyncMock(return_value=True),update_player_panel=AsyncMock())
            exec(compile(ast.Module(body=[node],type_ignores=[]),'main.py','exec'),ns)
            cb=N(data=f'mud:fx{effect}:-100',message=N(id=4,chat=N(id=-100)),from_user=N(id=7),answer=AsyncMock())
            await ns['panel_callback'](None,cb)
            self.assertEqual(player.audio_settings.effect,effect)
            self.assertIs(player.current,current);self.assertEqual(player.playing_settings,AudioSettings())
    async def test_benchmark_decodes_without_vc_or_network(self):
        result=await measure_effect('tiny',True,seconds=3)
        self.assertGreaterEqual(result['cpu_seconds'],0);self.assertGreater(result['wall_seconds'],0)
