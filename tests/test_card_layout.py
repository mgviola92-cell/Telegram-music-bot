import asyncio
import copy
import json
import unittest
from unittest.mock import AsyncMock
from card_layout import configure,render,validate,DEFAULTS
from music_owner_store import MusicOwnerStore,parse_state
from default_artwork import default_photo

class CardLayoutTest(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):configure(lambda:{})

    async def test_owner_save_survives_restart_without_losing_greetings(self):
        store=MusicOwnerStore(None);store.ready=True;store.data['owner']=42
        store.data['texts']={'vc':'မင်္ဂလာပါ {name}'}
        saved=[]
        async def persist(data):
            saved.append(json.dumps(data,ensure_ascii=False).encode());store.data=data
        store._persist=persist
        await store.set_card(42,'brand','My Music')
        await store.set_card(42,'playing','{brand} → {title} · {requester}')
        restored=parse_state(saved[-1])
        self.assertEqual(restored['texts']['vc'],'မင်္ဂလာပါ {name}')
        configure(lambda:restored['cards'])
        self.assertEqual(render('playing',title='Purple',requester='Sam'),'My Music → Purple · Sam')
        await store.set_card(42,'playing')
        self.assertNotIn('playing',store.data['cards'])
        with self.assertRaises(PermissionError):await store.set_card(99,'brand','Bad')

    async def test_legacy_state_and_invalid_templates(self):
        store=MusicOwnerStore(None)
        self.assertEqual(parse_state(json.dumps(store.data).encode())['cards'],{})
        for value in ['{title.__class__}','{title!r}','{title:99999}','{unknown}','{','x'*401]:
            with self.assertRaises(ValueError):validate('playing',value)
        with self.assertRaises(ValueError):validate('brand','{title}')
        configure(lambda:{'playing':'{title} / {requester}'})
        self.assertEqual(render('playing',title='{brand}',requester='A\nB'),'{brand} / A B')

    async def test_fallback_jpeg_is_small_and_fresh(self):
        a,b=default_photo(),default_photo()
        self.assertIsNot(a,b)
        self.assertTrue(a.read().startswith(b'\xff\xd8'))
        self.assertLess(len(b.read()),80000)

    async def test_real_announcement_uses_fallback_photo_and_preserves_controls(self):
        import ast
        from pathlib import Path
        from types import SimpleNamespace as Obj
        import main
        tree=ast.parse(Path(main.__file__).read_text())
        outer=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='main')
        handler=next(n for n in outer.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='announce_now_playing')
        song=main.Track('Purple · LyBot','query',duration_seconds=168,requester='Sam')
        player=main.Player(current=song)
        panel=Obj(id=33)
        bot=Obj(send_photo=AsyncMock(return_value=panel),delete_messages=AsyncMock(),send_message=AsyncMock())
        progress=Obj(reply_to_message_id=17,delete=AsyncMock())
        from telegram_photo_cache import TelegramPhotoCache
        ns=dict(vars(main));ns.update(photo_cache=TelegramPhotoCache(),bot=bot,calls=Obj(),identity=Obj(id=7),
             ensure_voice_audio=AsyncMock(return_value=False),artwork_for=lambda q:None,elapsed_for=lambda p:5)
        exec(compile(ast.Module(body=[handler],type_ignores=[]),'card-handler','exec'),ns)
        await ns['announce_now_playing'](1,player,song,progress)
        kw=bot.send_photo.call_args.kwargs
        self.assertEqual(kw['reply_to_message_id'],17)
        self.assertIn('Requested by : Sam',kw['caption'])
        self.assertIn('02:48',kw['caption'])
        buttons=[b.callback_data for row in kw['reply_markup'].inline_keyboard for b in row]
        for action in ['pause','skip','stop','loop','queue','effects','quality','normalize','top']:
            self.assertIn('mud:'+action+':1',buttons)
        self.assertTrue(player.panel_is_photo)
        self.assertIsNone(player.progress_task)
        bot.send_message.assert_not_called()
        progress.delete.assert_awaited_once()
