"""Own-session VC lifecycle and native outgoing transport checks."""
import asyncio
import logging
from pyrogram import raw
from ntgcalls import ConnectionMode

LOG = logging.getLogger('telegram_music')


class AudioTransportUnavailable(RuntimeError):
    pass


async def clear_stale_voice(client, chat_id, own_id):
    """Remove only this assistant's old participation; never close the group VC."""
    peer = await client.resolve_peer(chat_id)
    if not isinstance(peer, raw.types.InputPeerChannel):
        return
    full = await client.invoke(raw.functions.channels.GetFullChannel(
        channel=raw.types.InputChannel(channel_id=peer.channel_id, access_hash=peer.access_hash)))
    call = getattr(full.full_chat, 'call', None)
    if call is None:
        return
    participants = await client.invoke(raw.functions.phone.GetGroupParticipants(
        call=call, ids=[raw.types.InputPeerSelf()], sources=[], offset='', limit=1))
    for participant in participants.participants:
        if getattr(getattr(participant, 'peer', None), 'user_id', None) == own_id:
            await client.invoke(raw.functions.phone.LeaveGroupCall(call=call, source=participant.source))
            LOG.info('Cleared previous assistant VC participation chat %s', chat_id)
            await asyncio.sleep(0.8)
            return


async def ensure_native_sender(calls, chat_id, *, startup_timeout=4):
    binding = getattr(calls, '_binding', None)
    if binding is None:
        return
    # Stream replacement preserves native capture state. A new song must resume it.
    await asyncio.wait_for(calls.resume(chat_id), timeout=startup_timeout)
    await asyncio.wait_for(calls.unmute(chat_id), timeout=startup_timeout)
    mode = await asyncio.wait_for(binding.get_connection_mode(chat_id), timeout=4)
    state = await asyncio.wait_for(binding.get_state(chat_id), timeout=4)
    LOG.info('VC native audio transport chat %s: mode=%s capture_muted=%s',
             chat_id, str(mode), state.muted)
    if mode != ConnectionMode.RTC or state.muted:
        raise AudioTransportUnavailable('VC is not an unmuted RTC audio sender')
