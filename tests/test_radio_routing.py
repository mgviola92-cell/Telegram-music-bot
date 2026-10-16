"""Regression: adding a Radio preset must never intercept the /radio command."""
import ast
import unittest
from pathlib import Path
from types import SimpleNamespace as N
from unittest.mock import AsyncMock,Mock
import main
from audio_options import EFFECTS
from music_ui import player_controls

class RadioRoutingTests(unittest.IsolatedAsyncioTestCase):
    def functions(self):
        return [n for n in ast.walk(ast.parse(Path('main.py').read_text())) if isinstance(n,ast.AsyncFunctionDef)]
    def routes(self):
        result={}
        for node in self.functions():
            if node.name=='log_music_command':continue
            for decorator in node.decorator_list:
                if not isinstance(decorator,ast.Call) or not isinstance(decorator.func,ast.Attribute) or decorator.func.attr!='on_message':continue
                for call in ast.walk(decorator):
                    if isinstance(call,ast.Call) and isinstance(call.func,ast.Attribute) and call.func.attr=='command':
                        value=eval(compile(ast.Expression(call),'main.py','eval'),vars(main))
                        for command in value.commands:result.setdefault(command,[]).append(node.name)
        return result
    def test_radio_has_only_the_station_handler(self):
        self.assertEqual(self.routes().get('radio'),['radio_command'])
    def test_effect_presets_are_not_registered_as_commands(self):
        routes=self.routes()
        for command in set(EFFECTS)-{'radio'} | {'effect','effects','quality','normalize'}:
            self.assertNotIn(command,routes)
    async def test_radio_command_opens_station_picker_and_preserves_effect(self):
        node=next(n for n in self.functions() if n.name=='radio_command');node.decorator_list=[]
        song=main.Track('Playing','query');player=main.Player(current=song)
        original=player.audio_settings
        catalog=N(menu=Mock(return_value=('📻 Local / International',None)))
        msg=N(chat=N(id=-100),command=['radio'],reply_text=AsyncMock(return_value=N(id=12)))
        ns=dict(vars(main));ns.update(players={-100:player},radio_catalog=catalog)
        exec(compile(ast.Module(body=[node],type_ignores=[]),'main.py','exec'),ns)
        await ns['radio_command'](None,msg)
        self.assertIn('Local / International',msg.reply_text.call_args.args[0])
        self.assertIs(player.current,song);self.assertEqual(player.audio_settings,original)
        self.assertEqual(player.radio_panel_id,12)
    def test_radio_audio_preset_remains_available_on_music_card(self):
        buttons=[b for row in player_controls(-100,menu='effects').inline_keyboard for b in row]
        self.assertIn('mud:fxradio:-100',[b.callback_data for b in buttons])
