"""On-demand Render metrics with explicit service scope and timestamp; no secrets in output."""
import asyncio
import json
import math
import os
import time
from datetime import datetime,timezone,timedelta
import aiohttp

def parse_usage(data):
    if not isinstance(data,list):raise ValueError('Invalid metrics')
    factors={'b':1,'bytes':1,'kb':1000,'mb':1000000,'gb':1000000000}
    total=0;latest=None
    for row in data:
        factor=factors.get(str(row.get('unit','')).lower())
        if factor is None:raise ValueError('Unknown unit')
        for point in row.get('values',[]):
            value=float(point['value'])
            if not math.isfinite(value) or value<0:raise ValueError('Invalid usage')
            stamp=datetime.fromisoformat(point['timestamp'].replace('Z','+00:00'))
            total+=value*factor
            latest=max(latest,stamp) if latest else stamp
    if latest is None:raise ValueError('No samples')
    return total,latest

class BandwidthStatus:
    def __init__(self):self.row=None;self.until=0;self.lock=asyncio.Lock()
    def snapshot(self):
        try:
            amount=float(os.getenv('MUSIC_BANDWIDTH_SNAPSHOT_MB','nan'))
            stamp=datetime.fromisoformat(os.getenv('MUSIC_BANDWIDTH_SNAPSHOT_AT','').replace('Z','+00:00'))
            if math.isfinite(amount) and amount>=0:return amount*1000000,stamp
        except (ValueError,TypeError):pass
        return None
    async def refresh(self):
        if time.monotonic()<self.until or self.lock.locked():return
        key=os.getenv('RENDER_METRICS_API_KEY','')
        if not key:return
        service=os.getenv('MUSIC_RENDER_SERVICE_ID','')
        if not service.startswith('srv-'):return
        async with self.lock:
            self.until=time.monotonic()+900
            now=datetime.now(timezone.utc);start=now.replace(day=1,hour=0,minute=0,second=0,microsecond=0)
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
                    async with session.get('https://api.render.com/v1/metrics/bandwidth',headers={'Authorization':'Bearer '+key},
                          params={'resource':service,'startTime':start.isoformat(),'endTime':now.isoformat()},allow_redirects=False) as response:
                        if response.status!=200:return
                        chunks=[];size=0
                        async for chunk in response.content.iter_chunked(16384):
                            size+=len(chunk)
                            if size>1024*1024:return
                            chunks.append(chunk)
                        self.row=parse_usage(json.loads(b''.join(chunks)))
            except (Exception,):return
    def text(self):
        row=self.row or self.snapshot()
        if not row:return '▸ Bandwidth : Render data မချိတ်ရသေး'
        used,stamp=row
        now=datetime.now(timezone.utc)
        if (stamp.year,stamp.month)!=(now.year,now.month):return '▸ Bandwidth : ဒီလ data မရသေး'
        try:limit=float(os.getenv('MUSIC_BANDWIDTH_LIMIT_GB','5'))*1e9
        except ValueError:limit=5e9
        if not math.isfinite(limit) or limit<=0:limit=5e9
        local=stamp.astimezone(timezone(timedelta(hours=6,minutes=30)))
        return (f'▸ ဒီ Bot bandwidth : {used/1e6:.1f} MB\n'
                f'▸ {limit/1e9:g} GB နှင့်ယှဉ်လျှင် ကျန် : {max(0,limit-used)/1e9:.2f} GB ခန့်\n'
                f'  နောက်ဆုံး data : {local:%m/%d %H:%M} · workspace စုစုပေါင်း မဟုတ်')

BANDWIDTH=BandwidthStatus()
