import ast
import asyncio
import logging
import time
import unittest
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from main import Player, voice_slot_count
from welcome_audio import WelcomeAudio, greeting_text, BYTES_PER_SECOND, GreetingPipe

class FakePipe:
    def __init__(self): self.audio=[];self.arrived=asyncio.Event()
    def offer(self,pcm):self.audio.append(pcm);self.arrived.set();return True
    def close(self): pass

class DelayedWelcome(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.w=WelcomeAudio();self.w.ready=True
        self.pipe=FakePipe();self.w.pipes[1]=self.pipe
        self.w.pcm=AsyncMock(return_value=b'\x01\x00'*100)
    async def asyncTearDown(self):await self.w.close()

    async def test_joins_wait_even_when_tts_is_cached(self):
        from unittest.mock import patch
        with patch('welcome_audio.JOIN_DELAY_SECONDS',.08):
            started=time.monotonic()
            self.assertTrue(self.w.request(1,2,'Name'))
            await asyncio.sleep(.02)
            self.assertEqual(self.pipe.audio,[])
            await asyncio.wait_for(self.pipe.arrived.wait(),.3)
            self.assertGreaterEqual(time.monotonic()-started,.075)

    async def test_user_left_before_greeting(self):
        present=AsyncMock(return_value=False)
        self.w.request(1,2,'Name',delay=.01,present=present)
        await asyncio.gather(*self.w.tasks)
        self.assertEqual(self.pipe.audio,[])
        present.assert_awaited_once()

    async def test_style_switch_discards_old_greeting(self):
        self.w.request(1,2,'Name',delay=.03)
        self.w.styles[1]='party'
        await asyncio.gather(*self.w.tasks)
        self.assertEqual(self.pipe.audio,[])

    async def test_stop_during_delay_cancels_speech(self):
        self.w.request(1,2,'Name',delay=.08)
        await asyncio.sleep(.01)
        self.w.close_chat(1)
        await asyncio.gather(*self.w.tasks,return_exceptions=True)
        self.assertEqual(self.pipe.audio,[])
        self.assertNotIn(1,self.w.busy)

    async def test_party_uses_full_invitation_and_blocks_overlap(self):
        self.w.styles[1]='party'
        self.w.request(1,2,'မောင်ခန့်',force=True,delay=0)
        await asyncio.wait_for(self.pipe.arrived.wait(),.3)
        text=self.w.pcm.call_args.args[0]
        self.assertIn('လူဝင်တောင်းလို့ ရပါတယ်',text)
        self.assertIn('အက်မင်တွေဆီ စကားပြောခွင့် တောင်းပြီး',text)
        self.assertIn('မောင်ခန့်',text)
        self.assertFalse(self.w.request(1,3,'Other',force=True))

    def test_normal_greeting_stays_short(self):
        self.assertNotIn('လူတောင်းပွဲ',greeting_text('Name'))
        self.assertGreater(len(greeting_text('Name',style='party')),len(greeting_text('Name'))*2)

    def test_long_greeting_pipe_capacity(self):
        p=GreetingPipe()
        try:self.assertTrue(p.offer(bytes(BYTES_PER_SECOND*15)))
        finally:p.close()

class PartyCommand(unittest.IsolatedAsyncioTestCase):
    def context(self,current=None):
        # Exercise the actual registered handler without starting Telegram clients.
        tree=ast.parse(Path('main.py').read_text())
        node=next(n for n in ast.walk(tree) if isinstance(n,ast.AsyncFunctionDef)
                  and n.name=='welcome_party_command')
        node.decorator_list=[]
        player=Player(current=current)
        pipe=FakePipe()
        w=WelcomeAudio();w.ready=True
        w.pipes[1]=pipe if current else None
        if not current:w.pipes.clear()
        w.request=lambda *args,**kwargs: True
        panel=SimpleNamespace(id=7,edit_text=AsyncMock())
        msg=SimpleNamespace(chat=SimpleNamespace(id=1),from_user=SimpleNamespace(id=2),
                            reply_text=AsyncMock(return_value=panel))
        async def idle(*args,**kwargs):
            player.idle_mode='silence';w.pipes[1]=pipe;return True
        ctx=dict(asyncio=asyncio,Client=object,Message=object,players={1:player},welcome=w,
                 require_admin=AsyncMock(return_value=True),welcome_warmup_task=None,
                 ensure_assistant_in_group=AsyncMock(),voice_slot_count=voice_slot_count,
                 max_groups=4,fresh_voice_chats={1},clear_stale_voice=AsyncMock(),
                 assistant=object(),identity=SimpleNamespace(id=9),start_idle=AsyncMock(side_effect=idle),
                 welcome_menu=lambda c:('menu',None),log=logging.getLogger('test'))
        exec(compile(ast.Module(body=[node],type_ignores=[]),'main.py','exec'),ctx)
        return ctx,msg,player,panel

    async def test_standalone_joins_silence_without_a_song(self):
        ctx,msg,p,panel=self.context()
        await ctx['welcome_party_command'](None,msg)
        ctx['ensure_assistant_in_group'].assert_awaited_once()
        ctx['start_idle'].assert_awaited_once_with(1,p,quiet=True)
        self.assertEqual(ctx['welcome'].styles[1],'party')
        self.assertEqual(ctx['welcome'].mode(1),'vc')
        self.assertIsNone(p.loading_task)

    async def test_existing_song_does_not_restart(self):
        song=SimpleNamespace(title='Current')
        ctx,msg,p,panel=self.context(song)
        await ctx['welcome_party_command'](None,msg)
        ctx['start_idle'].assert_not_awaited()
        ctx['ensure_assistant_in_group'].assert_not_awaited()
        self.assertIs(p.current,song)

    async def test_stop_before_join_invalidates_request(self):
        ctx,msg,p,panel=self.context()
        async def stop_during_join(*args):p.radio_revision+=1
        ctx['ensure_assistant_in_group'].side_effect=stop_during_join
        await ctx['welcome_party_command'](None,msg)
        ctx['start_idle'].assert_not_awaited()
        self.assertIsNone(p.loading_task)

    async def test_non_admin_cannot_start(self):
        ctx,msg,p,panel=self.context()
        ctx['require_admin'].return_value=False
        await ctx['welcome_party_command'](None,msg)
        ctx['start_idle'].assert_not_awaited()
        self.assertEqual(ctx['welcome'].styles,{})
