"""Receive source bot messages/edits without repeatedly requesting chat history."""
import asyncio
from collections import OrderedDict
from pyrogram.handlers import MessageHandler, EditedMessageHandler


class BotReplyHub:
    def __init__(self, client):
        self.waiters = {}
        # One handler pair per assistant, shared by all source bots/requests.
        client.add_handler(MessageHandler(self.receive), group=-110)
        client.add_handler(EditedMessageHandler(self.receive), group=-110)

    async def receive(self, client, message):
        if getattr(message, 'outgoing', False):
            return
        chat_id = getattr(getattr(message, 'chat', None), 'id', None)
        for inbox in tuple(self.waiters.get(chat_id, ())):
            inbox.feed(message)


def reply_hub(client):
    if not callable(getattr(client, 'add_handler', None)):
        return None
    hub = getattr(client, '_music_source_replies', None)
    if hub is None:
        hub = BotReplyHub(client)
        client._music_source_replies = hub
    return hub


class ReplyInbox:
    """Subscribe before sending; keep fast replies and handle edited result cards."""
    def __init__(self, client, chat_id):
        self.client, self.chat_id = client, chat_id
        self.pending = OrderedDict()
        self.changed = asyncio.Event()
        self.closed = False
        self.delay = 0
        self.hub = reply_hub(client)
        if self.hub is not None:
            self.hub.waiters.setdefault(chat_id, set()).add(self)

    def feed(self, message):
        if self.closed:
            return
        key = getattr(message, 'id', None)
        if key is None:
            return
        self.pending.pop(key, None)
        self.pending[key] = message
        while len(self.pending) > 32:
            self.pending.popitem(last=False)
        self.changed.set()

    async def batch(self, after):
        if self.hub is None:
            # Compatibility for non-dispatching clients. Production Pyrogram
            # uses updates above; it never enters this polling path.
            if self.delay:
                await asyncio.sleep(self.delay)
            self.delay = min(3, self.delay + 1.5)
            messages = [m async for m in self.client.get_chat_history(self.chat_id, limit=15)
                        if m.id > after and not getattr(m, 'outgoing', False)]
            return list(reversed(messages))
        while not self.closed:
            messages = [m for m in self.pending.values() if m.id > after]
            self.pending.clear()
            self.changed.clear()
            if messages:
                return sorted(messages, key=lambda m: m.id)
            await self.changed.wait()
        raise asyncio.CancelledError()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.pending.clear()
        self.changed.set()
        if self.hub is not None:
            waiting = self.hub.waiters.get(self.chat_id)
            if waiting is not None:
                waiting.discard(self)
                if not waiting:
                    self.hub.waiters.pop(self.chat_id, None)
