"""On-demand local status; no network polling, dependencies or disk writes."""
import os
import shutil
import tempfile
import time
from pathlib import Path

_STARTED=time.monotonic()


def uptime(seconds):
    seconds=max(0,int(seconds));days,seconds=divmod(seconds,86400);hours,seconds=divmod(seconds,3600);minutes,seconds=divmod(seconds,60)
    return f'{days} ရက် {hours:02d} နာရီ {minutes:02d} မိနစ် {seconds:02d} စက္ကန့်'


def read(path):
    try:return Path(path).read_text()[:4096].strip()
    except (OSError,ValueError):return ''


def cpu_sample():
    stats=dict(row.split() for row in read('/sys/fs/cgroup/cpu.stat').splitlines() if len(row.split())==2)
    used=float(stats['usage_usec'])/1e6 if 'usage_usec' in stats else sum(os.times()[:4])
    quota=read('/sys/fs/cgroup/cpu.max').split()
    capacity=float(quota[0])/float(quota[1]) if len(quota)==2 and quota[0]!='max' and float(quota[1])>0 else None
    return used,capacity


def memory_sample():
    used=read('/sys/fs/cgroup/memory.current');limit=read('/sys/fs/cgroup/memory.max')
    if used.isdigit() and limit.isdigit() and 0<int(limit)<2**60:
        return int(used),int(limit)
    for row in read('/proc/self/status').splitlines():
        if row.startswith('VmRSS:'):return int(row.split()[1])*1024,None
    return None,None


class RuntimeStatus:
    def __init__(self):
        self.previous=None;self.cached=None;self.cached_at=0
        try:self.previous=(time.monotonic(),*cpu_sample())
        except (ValueError,TypeError,ZeroDivisionError):pass
    def text(self):
        now=time.monotonic()
        if self.cached is not None and now-self.cached_at<5:return self.cached
        cpu='မရရှိသေး'
        try:
            used,capacity=cpu_sample()
            if self.previous:
                old,old_used,old_capacity=self.previous
                if now-old>=.5 and used>=old_used and capacity==old_capacity:
                    percent=100*(used-old_used)/(now-old)/(capacity or 1)
                    cpu=f'{percent:.1f}%' + ('' if capacity else ' · core ၁ လုံးအခြေခံ')
            self.previous=(now,used,capacity)
        except (ValueError,TypeError,ZeroDivisionError):pass
        ram='မရရှိသေး'
        try:
            used,limit=memory_sample()
            if used is not None:
                ram=f'{used/1048576:.1f} MB'
                if limit:ram+=f' / {limit/1048576:.0f} MB · {100*used/limit:.1f}%'
        except (ValueError,TypeError,ZeroDivisionError):pass
        disk='မရရှိသေး'
        try:
            d=shutil.disk_usage(tempfile.gettempdir())
            disk=f'{100*d.used/d.total:.1f}% သုံးပြီး · {d.free/1073741824:.1f} GB လွတ်'
        except (OSError,ValueError,ZeroDivisionError):pass
        self.cached=('▸ လည်ပတ်ချိန် : '+uptime(now-_STARTED)+'\n'
                     '▸ CPU အသုံး : '+cpu+'\n'
                     '▸ RAM အသုံး : '+ram+'\n'
                     '▸ ဖိုင်စနစ်နေရာ : '+disk)
        self.cached_at=now
        return self.cached

STATUS=RuntimeStatus()
