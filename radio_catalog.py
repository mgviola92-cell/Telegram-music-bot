"""Small radio catalogue; station checks never delay ordinary /play lookups."""
import asyncio
import ipaddress
import json
import logging
import socket
import time
from dataclasses import dataclass
from urllib.parse import urlsplit
from aiohttp import ClientSession, ClientTimeout
from pyrogram.types import InlineKeyboardButton as Button, InlineKeyboardMarkup as Markup

log = logging.getLogger('telegram_music')

@dataclass(frozen=True)
class Station:
    key: str
    name: str
    region: str
    url: str

# Public direct-player endpoints published at somafm.com/<channel>/directstreamlinks.html.
_CHANNELS = [('groovesalad','Groove Salad · Chill'), ('dronezone','Drone Zone · Ambient'),
 ('secretagent','Secret Agent · Lounge'), ('indiepop','Indie Pop Rocks'),
 ('poptron','PopTron · Electronic Pop'), ('defcon','DEF CON · Electronic'),
 ('lush','Lush · Vocals'), ('beatblender','Beat Blender'), ('deepspaceone','Deep Space One'),
 ('spacestation','Space Station Soma'), ('sonicuniverse','Sonic Universe · Jazz'),
 ('seventies','Left Coast 70s'), ('bootliquor','Boot Liquor · Americana'),
 ('folkfwd','Folk Forward'), ('u80s','Underground 80s'), ('metal','Metal Detector'),
 ('reggae','Heavyweight Reggae'), ('bossa','Bossa Beyond')]


def safe_url(url):
    try:
        p = urlsplit(url)
        host = (p.hostname or '').lower()
        approved = (host.endswith('.somafm.com') or host in {'stream.zeno.fm',
                    'cherry.akiyaresearch.com'} or host.endswith('.zeno.fm'))
        return (p.scheme == 'https' and approved and p.port in (None, 443, 444)
                and not p.username and not p.password and not p.fragment)
    except ValueError:
        return False


async def public_address(url):
    p = urlsplit(url)
    records = await asyncio.get_running_loop().getaddrinfo(p.hostname, p.port or 443,
                                                           type=socket.SOCK_STREAM)
    if not records or any(not ipaddress.ip_address(r[4][0]).is_global for r in records):
        raise ValueError('Non-public radio address')


async def decode_check(url, seconds=1):
    if not safe_url(url):
        raise ValueError('Unknown radio host')
    await asyncio.wait_for(public_address(url), 3)
    proc = await asyncio.create_subprocess_exec('ffmpeg','-nostdin','-hide_banner','-loglevel','error',
        '-rw_timeout','6000000','-i',url,'-t',str(seconds),'-vn','-ar','24000','-ac','1',
        '-f','s16le','pipe:1', stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        pcm, _ = await asyncio.wait_for(proc.communicate(), 12)
        if proc.returncode or len(pcm) < 24000:
            raise RuntimeError('Radio produced no usable audio')
        return len(pcm)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


class RadioCatalog:
    def __init__(self):
        self.stations = {key: Station(key,name,'inter',f'https://ice5.somafm.com/{key}-128-mp3')
                         for key,name in _CHANNELS}
        self.checked = {}
        self.loaded = False
        self.last_load = -1000
        self.load_lock = asyncio.Lock()
        self.check_lock = asyncio.Semaphore(3)

    async def load_local(self):
        async with self.load_lock:
            if self.loaded or time.monotonic() - self.last_load < 60:
                return
            self.last_load = time.monotonic()
            # Directory supplies broadcaster endpoints, never arbitrary user URLs.
            for host in ('de1.api.radio-browser.info','de2.api.radio-browser.info'):
                try:
                    async with ClientSession(timeout=ClientTimeout(total=5)) as session:
                        async with session.get(f'https://{host}/json/stations/bycountrycodeexact/MM',
                                               headers={'User-Agent':'MudyarMusicBot/1.0'}) as response:
                            response.raise_for_status()
                            raw = await response.content.read(200001)
                            if len(raw) > 200000:
                                raise ValueError('Catalogue too large')
                            items = json.loads(raw)
                    seen = set()
                    for item in items:
                        url = item.get('url_resolved') or item.get('url','')
                        name = str(item.get('name','')).strip()[:55]
                        if not name or not safe_url(url) or url in seen:
                            continue
                        seen.add(url)
                        key = 'mm' + str(len(seen))
                        self.stations[key] = Station(key,name,'local',url)
                        if len(seen) >= 18:
                            break
                    self.loaded = True
                    log.info("Local radio catalogue loaded stations=%s",len(seen))
                    break
                except Exception as exc:
                    log.info('Local radio directory unavailable (%s)',type(exc).__name__)

    async def verify(self, key):
        station = self.stations.get(key)
        if station is None:
            raise ValueError('Unknown station')
        cached = self.checked.get(key)
        if cached and time.monotonic() - cached[0] < (300 if cached[1] else 60):
            if not cached[1]:
                raise RuntimeError('Station is temporarily offline')
            return station
        async with self.check_lock:
            try:
                size = await decode_check(station.url)
            except Exception:
                self.checked[key] = (time.monotonic(), False)
                raise
            self.checked[key] = (time.monotonic(), True)
            log.info('Radio audio verified station=%s pcm_bytes=%s',station.name,size)
            return station

    def menu(self, chat_id, region='home', page=0, selected=None):
        def button(text, action):
            return Button(text, callback_data=f'rad:{chat_id}:{action}')
        if region not in {'local','inter'}:
            return '📻 Radio\nနားထောင်ချင်တဲ့ လိုင်းကို ရွေးပါ။', Markup([
                [button('🇲🇲 Local','local:0'),button('🌍 International','inter:0')],
                [button('⏹ Radio OFF','off:0')]])
        stations = [s for s in self.stations.values() if s.region == region
                    and self.checked.get(s.key,(0,True))[1]]
        page = min(max(0,page),max(0,(len(stations)-1)//6))
        rows = [[button(('✓ ' if s.key == selected else '') + s.name,f'pick:{s.key}')]
                for s in stations[page*6:page*6+6]]
        nav = []
        if page:
            nav.append(button('‹',f'{region}:{page-1}'))
        if (page+1)*6 < len(stations):
            nav.append(button('›',f'{region}:{page+1}'))
        if nav:
            rows.append(nav)
        rows.append([button('↩ Local / International','home:0'),button('⏹ OFF','off:0')])
        title = '🇲🇲 Local Radio' if region == 'local' else '🌍 International Radio'
        text = f'{title}\n{page+1}/{max(1,(len(stations)+5)//6)} · လိုင်းရွေးပါ။'
        if not stations:
            text += '\nလက်ရှိ Local stream မရသေးဘူး။ နောက်မှ ပြန်စမ်းပါ။'
        return text, Markup(rows)
