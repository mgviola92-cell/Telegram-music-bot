import ast
from pathlib import Path
import unittest
from pyrogram.parser.html import HTML
from pyrogram import raw
from player_visuals import player_caption
from card_layout import configure,render
import main

class MentionTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):configure(lambda:{})
    async def test_real_parser_mentions_quote_and_escape(self):
        text=player_caption('<script> & Song',0,180,paused=True,requester='Sam <A>',requester_id=42,
                            html_mode=True,actor='Admin & B',actor_id=43)
        data=await HTML(None).parse(text)
        self.assertIn('<script> & Song',data['message'])
        self.assertIn('Stream paused by Admin & B',data['message'])
        mentions=[e for e in data['entities'] if isinstance(e,raw.types.InputMessageEntityMentionName)]
        self.assertEqual([e.user_id for e in mentions],[42,43])
        self.assertTrue(any(isinstance(e,raw.types.MessageEntityBlockquote) for e in (data['entities'] or [])))
        self.assertNotIn('<script>',text)
    async def test_repeated_fields_cannot_break_caption_or_tags(self):
        configure(lambda:{'playing':'{requester} '*25+'{title}'})
        text=render('playing',html_mode=True,requester_id=42,requester='😀<&'*50,title='T')
        data=await HTML(None).parse(text)
        self.assertLessEqual(len(data['message'].encode('utf-16-le'))//2,750)
        self.assertEqual(text.count('<a '),text.count('</a>'))
    async def test_resume_attribution_and_unknown_user(self):
        data=await HTML(None).parse(player_caption('Song',0,180,html_mode=True,actor='Admin',actor_id=43))
        self.assertIn('Stream resumed by Admin',data['message'])
        data=await HTML(None).parse(render('queued',html_mode=True,requester='<Unknown>'))
        self.assertFalse(any(isinstance(e,raw.types.InputMessageEntityMentionName) for e in (data['entities'] or [])))
    def test_closed_call_and_watchdog_cleanup_do_not_send_notice(self):
        tree=ast.parse(Path(main.__file__).read_text())
        outer=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='main')
        for name in ['call_left','watch_voice_connections']:
            fn=next(n for n in outer.body if isinstance(n,ast.AsyncFunctionDef) and n.name==name)
            sends=[n for n in ast.walk(fn) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='send_message']
            self.assertEqual(sends,[])
