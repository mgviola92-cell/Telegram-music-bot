"""Race optional remote audio providers without downloading full tracks.

The first provider returning a validated playable-source candidate wins.
Telegram VC native frame readiness is separately verified at playback.
"""
import asyncio


async def race_candidates(providers):
    tasks = {asyncio.create_task(call()): name for name, call in providers}
    if not tasks:
        raise LookupError("No enabled remote providers")
    pending = set(tasks)
    try:
        while pending:
            finished, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in finished:
                if task.cancelled():
                    continue
                try:
                    candidate = task.result()
                except Exception:
                    continue
                if isinstance(candidate, (tuple, list)) and len(candidate) >= 2 and isinstance(candidate[0], str) and candidate[0]:
                    return candidate
        raise LookupError("No remote audio provider resolved")
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
