import unittest
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock
from ntgcalls import ConnectionMode
from pyrogram import raw
from voice_transport import clear_stale_voice, ensure_native_sender, AudioTransportUnavailable

class VoiceTransportTest(unittest.IsolatedAsyncioTestCase):
    async def test_clears_only_assistant_participation_without_closing_vc(self):
        call=raw.types.InputGroupCall(id=123,access_hash=456)
        peer=raw.types.InputPeerChannel(channel_id=99,access_hash=88)
        mine=Obj(peer=raw.types.PeerUser(user_id=77),source=100)
        client=Obj(resolve_peer=AsyncMock(return_value=peer),invoke=AsyncMock(side_effect=[Obj(full_chat=Obj(call=call)),Obj(participants=[mine]),None]))
        await clear_stale_voice(client,-10099,77)
        self.assertIsInstance(client.invoke.call_args.args[0],raw.functions.phone.LeaveGroupCall)
        self.assertEqual(client.invoke.call_args.args[0].source,100)

    async def test_does_not_leave_for_other_participant(self):
        client=Obj(resolve_peer=AsyncMock(return_value=raw.types.InputPeerChannel(channel_id=99,access_hash=88)),invoke=AsyncMock(side_effect=[Obj(full_chat=Obj(call=raw.types.InputGroupCall(id=1,access_hash=2))),Obj(participants=[Obj(peer=raw.types.PeerUser(user_id=55),source=100)])]))
        await clear_stale_voice(client,-10099,77)
        self.assertEqual(client.invoke.await_count,2)

    async def test_no_vc_does_not_create_one(self):
        client=Obj(resolve_peer=AsyncMock(return_value=raw.types.InputPeerChannel(channel_id=99,access_hash=88)),invoke=AsyncMock(return_value=Obj(full_chat=Obj(call=None))))
        await clear_stale_voice(client,-10099,77)
        self.assertEqual(client.invoke.await_count,1)

    async def test_stream_replacement_resumes_and_unmutes_native_capture(self):
        calls=Obj(resume=AsyncMock(),unmute=AsyncMock(),_binding=Obj(get_connection_mode=AsyncMock(return_value=ConnectionMode.RTC),get_state=AsyncMock(return_value=Obj(muted=False))))
        await ensure_native_sender(calls,-10099)
        calls.resume.assert_awaited_once_with(-10099)
        calls.unmute.assert_awaited_once_with(-10099)

    async def test_receive_only_and_muted_transport_are_not_playback_success(self):
        for mode,muted in [(ConnectionMode.STREAM,False),(ConnectionMode.RTC,True)]:
            calls=Obj(resume=AsyncMock(),unmute=AsyncMock(),_binding=Obj(get_connection_mode=AsyncMock(return_value=mode),get_state=AsyncMock(return_value=Obj(muted=muted))))
            with self.assertRaises(AudioTransportUnavailable):await ensure_native_sender(calls,-10099)

    async def test_muted_native_sender_gets_one_clean_reconnection(self):
        from main import play_with_reconnect
        calls=Obj(play=AsyncMock(),leave_call=AsyncMock(),resume=AsyncMock(),unmute=AsyncMock(),_binding=Obj(get_connection_mode=AsyncMock(return_value=ConnectionMode.RTC),get_state=AsyncMock(side_effect=[Obj(muted=True),Obj(muted=False)])))
        await play_with_reconnect(calls,-10099,object())
        self.assertEqual(calls.play.await_count,2)
        calls.leave_call.assert_awaited_once_with(-10099)
