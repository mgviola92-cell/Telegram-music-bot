"""Conservative extraction budget using cAdvisor-style cgroup working set."""
from pathlib import Path


def memory_snapshot():
    from runtime_status import memory_sample
    used,limit=memory_sample()
    used=max(0,used or 0)
    inactive=anon=0
    try:
        stats=dict(line.split() for line in
                   Path('/sys/fs/cgroup/memory.stat').read_text().splitlines())
        inactive=max(0,min(used,int(stats.get('inactive_file',0))))
        anon=max(0,int(stats.get('anon',0)))
    except (OSError,ValueError):pass
    return dict(used=used,working=used-inactive,limit=limit,
                inactive=inactive,anon=anon)


def over_budget(sample, *, starting=False):
    limit=sample['limit']
    if not limit:return False
    # Keep a separate raw ceiling even if file cache is reclaimable. Never
    # subtract active file, anonymous memory, kernel memory or slab.
    return (sample['working']/limit > (.75 if starting else .85)
            or sample['used']/limit > (.85 if starting else .92))
