"""Single flight for identical cross-group resolutions. Every local user owns a copy."""
import asyncio
import shutil
import tempfile
from pathlib import Path
from queue_guard import identities
from song_cache import get_cached_song,save_cached_song

class SharedAudio:
    def __init__(self,resolver,cleanup):self.resolver,self.cleanup=resolver,cleanup;self.jobs={}
    async def resolve(self,track,prefer_stream):
        selected=getattr(track,'selected',None)
        keys=identities(track)
        stable=next((k for k in keys if k[0]=='telegram'),None) or ('source',track.video,track.source.strip().casefold())
        if getattr(track, 'relay_requested', False):stable=('youtube-relay',stable)
        key=repr((stable,bool(prefer_stream)))
        cached=get_cached_song('shared:'+key,track.video)
        if cached:return cached
        job=self.jobs.get(key)
        if job is None:
            job={'task':asyncio.create_task(self.resolver(track,prefer_stream)),'users':0}
            self.jobs[key]=job
        job['users']+=1
        try:
            result=await asyncio.shield(job['task'])
            path,title,duration=result
            if path.startswith(('https://','http://')):return result
            save_cached_song('shared:'+key,track.video,path,title,duration)
            cached=get_cached_song('shared:'+key,track.video)
            if cached:return cached
            folder=Path(tempfile.mkdtemp(prefix='mudyar_media_'))
            target=folder/('shared'+Path(path).suffix)
            try:shutil.copyfile(path,target)
            except BaseException:shutil.rmtree(folder,ignore_errors=True);raise
            return str(target),title,duration
        finally:
            job['users']-=1
            if not job['users']:
                if self.jobs.get(key) is job:self.jobs.pop(key,None)
                task=job['task']
                if not task.done():task.cancel()
                def clean(done):
                    if not done.cancelled():
                        try:
                            path=done.result()[0]
                            if not path.startswith(('https://','http://')):self.cleanup(path)
                        except BaseException:pass
                task.add_done_callback(clean)
