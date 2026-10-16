"""Conservative empty-room pause; no bitrate changes or audio replacement."""
import asyncio
import logging
import time

log=logging.getLogger('telegram_music')

class EmptyRoomSaver:
    def __init__(self,calls,players,own_id,welcome,changed,clock=time.monotonic):
        self.calls,self.players,self.own_id=calls,players,own_id
        self.welcome,self.changed,self.clock=welcome,changed,clock
        self.empty={};self.held={}
    @staticmethod
    def token(player):
        if player.current is not None:return ('song',id(player.current))
        if player.idle_mode is not None:return ('idle',player.idle_mode,player.started_at)
        return None
    def manual(self,chat):
        self.held.pop(chat,None);self.empty.pop(chat,None)
    async def resume(self,chat,player):
        if await asyncio.wait_for(self.calls.resume(chat),5) is False:
            return
        if player.paused_since:player.paused_seconds+=self.clock()-player.paused_since
        player.paused_since=0.;player.paused=False
        self.manual(chat)
        if player.current is not None:
            player.current.control_actor="";player.current.control_actor_id=None
        log.info('Bandwidth saver resumed VC chat %s',chat)
        await self.changed(chat,player)
    async def arrival(self,chat,user):
        player=self.players.get(chat)
        if user==self.own_id or not player:return
        async with player.lock:
            if chat in self.held and self.held[chat]==self.token(player) and player.paused:
                await self.resume(chat,player)
            self.empty.pop(chat,None)
    async def check(self,chat,player):
        token=self.token(player)
        if not token or (player.loading_task and not player.loading_task.done()):
            self.manual(chat);return
        if chat in self.held and self.held[chat]!=token:self.manual(chat)
        # An explicit admin pause is never resumed by this task.
        if player.paused and chat not in self.held:return
        try:members=await asyncio.wait_for(self.calls.get_participants(chat),5)
        except Exception:
            self.empty.pop(chat,None);return
        if not members:
            self.empty.pop(chat,None);return
        ids={getattr(p,'user_id',None) for p in members}
        # Empty/partial/unknown snapshots do not prove an empty room.
        if self.own_id not in ids or None in ids:
            self.empty.pop(chat,None);return
        async with player.lock:
            if self.token(player)!=token:return
            if ids-{self.own_id}:
                self.empty.pop(chat,None)
                if self.held.get(chat)==token and player.paused:await self.resume(chat,player)
                return
            if player.paused:return
            # Silence feeds greeting audio: leave it running when greetings are on.
            if player.idle_mode=='silence' and self.welcome.mode(chat) in {'vc','both'}:return
            old=self.empty.get(chat)
            if not old or old[0]!=token:
                self.empty[chat]=(token,self.clock());return
            if self.clock()-old[1]<180:return
            if await asyncio.wait_for(self.calls.pause(chat),5) is False:return
            player.paused=True;player.paused_since=self.clock();self.held[chat]=token
            if player.current is not None:
                player.current.control_actor="";player.current.control_actor_id=None
            log.info('Bandwidth saver paused empty VC chat %s',chat)
            await self.changed(chat,player)
    async def run(self):
        while True:
            await asyncio.sleep(30)
            for chat,player in list(self.players.items()):
                try:await self.check(chat,player)
                except asyncio.CancelledError:raise
                except Exception as exc:log.info('Bandwidth saver deferred (%s)',type(exc).__name__)
