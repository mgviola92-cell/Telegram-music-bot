"""Parallel metadata searches, one Telegram transfer, first ready audio wins."""
import asyncio
import logging
from lybot_source import BotSourceUnavailable

LOG = logging.getLogger('telegram_music')


class ParallelSources:
    def __init__(self, sources, cleanup):
        self.sources, self.cleanup = sources, cleanup

    def cached(self, query):
        for source in self.sources:
            result = source.cached(query)
            if result:
                return result
        return None

    async def resolve(self, query):
        return await self.race(None, query)

    async def race(self, prepared, query, *, extra=None):
        cached = self.cached(query)
        if cached:
            if prepared:
                prepared.discard()
            return cached
        tasks, pending, candidates = {}, set(), []
        winner = None
        downloading = None
        def add(coroutine, kind):
            task = asyncio.create_task(coroutine)
            tasks[task] = kind
            pending.add(task)
            return task
        async def primary():
            return await asyncio.wait_for(prepared.consume(), timeout=12)
        primary_task = add(primary(), 'primary') if prepared else None
        if extra is not None:
            add(extra(), 'primary_extra')
        for source in self.sources:
            add(source.find(query), 'metadata')
        try:
            async with asyncio.timeout(30):
                while pending:
                    done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                    for task in sorted(done, key=lambda t: tasks[t] == 'metadata'):
                        pending.remove(task)
                        try:
                            result = task.result()
                        except Exception as exc:
                            LOG.info('Parallel source attempt unavailable (%s)',type(exc).__name__)
                            if task is downloading:
                                downloading = None
                            continue
                        if tasks[task] != 'metadata':
                            winner = task
                            LOG.info('Parallel audio winner: %s', result[1].rsplit(' · ',1)[-1][:60])
                            return result
                        candidates.append(result)
                    if downloading is None and candidates:
                        candidate = candidates.pop(0)
                        downloading = add(candidate.source.download(candidate), 'download')
                raise BotSourceUnavailable('No matching playable audio from the music sources')
        except TimeoutError:
            raise BotSourceUnavailable('Music sources did not return playable audio in time') from None
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
            if prepared and winner is not primary_task:
                prepared.discard()
            # Two ready results can race in the same event-loop iteration.
            for task,kind in tasks.items():
                if task is not winner and kind in {'primary','primary_extra','download'} and not task.cancelled():
                    try:
                        self.cleanup(task.result()[0])
                    except Exception:
                        pass

    async def describe(self, query, primary_description):
        async def describe_bot(source):
            candidate = await source.find(query)
            return candidate.label,candidate.duration
        tasks = [asyncio.create_task(describe_bot(s)) for s in self.sources]
        tasks.append(asyncio.create_task(primary_description(query)))
        pending = set(tasks)
        try:
            async with asyncio.timeout(20):
                while pending:
                    done,pending=await asyncio.wait(pending,return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        try:
                            result=task.result()
                            if result:
                                return result
                        except Exception:
                            pass
                return None
        except TimeoutError:
            return None
        finally:
            for task in tasks:
                if not task.done():task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
