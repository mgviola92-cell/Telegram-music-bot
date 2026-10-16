"""Bounded plain-text card templates, shared by player, queue and owner UI."""
import re
import string
from html import escape
DEFAULTS = {
    'brand': 'မုဒြာ ╳ 𝔐𝔲𝔰𝔦𝔠 𝔅𝔬𝔱 ❤️‍🔥',
    'playing': '{brand}\n\n➜ {status}\n\n▸ Title : {title}\n▸ Duration : {duration}\n▸ Requested by : {requester}',
    'queued': '➜ Added to Queue #{position}\n\n▸ Title : {title}\n▸ Duration : {duration}\n▸ Requested by : {requester}',
}
LABELS = {'brand':'Bot နာမည်', 'playing':'သီချင်းကဒ် စာပုံစံ', 'queued':'Queue reply စာပုံစံ'}
FIELDS = {'brand','status','title','duration','requester','position'}
_getter = lambda: {}

def configure(getter):
    global _getter
    _getter = getter

def validate(key, text):
    if key not in DEFAULTS or not isinstance(text,str): raise ValueError('Invalid card setting')
    text=text.strip()
    if not text or len(text)>(100 if key=='brand' else 400): raise ValueError('နာမည် ၁၀၀ / စာပုံစံ ၄၀၀ စာလုံးအတွင်း ရေးပါ။')
    if any(ord(c)<32 and c not in '\n\t' for c in text): raise ValueError('Invalid text')
    try:
        for _,field,spec,conversion in string.Formatter().parse(text):
            if field is not None and (key=='brand' or field not in FIELDS or spec or conversion): raise ValueError()
    except ValueError: raise ValueError('ပြထားတဲ့ placeholder တွေကိုပဲ သုံးပါ။') from None
    return text

def clean(value, limit):
    return re.sub(r'[\x00-\x1f\x7f]', ' ', str(value or '')).strip()[:limit]

def mention(name,user_id=None):
    label=escape(clean(name,70))
    return f'<a href="tg://user?id={user_id}">{label}</a>' if type(user_id) is int and user_id>0 else label


def render(key, *, html_mode=False, requester_id=None, **values):
    settings=_getter() or {}
    context={field:'—' for field in FIELDS}
    context.update(values)
    context['brand']=settings.get('brand', DEFAULTS['brand'])
    context={k:clean(v,100 if k in {'title','brand'} else 70) for k,v in context.items()}
    template=settings.get(key,DEFAULTS[key])
    if not html_mode:
        result=template.format(**context)
        return result.encode('utf-16-le')[:1800].decode('utf-16-le',errors='ignore')
    # Clip visible text before adding tags; never truncate through an HTML entity.
    result=[];remaining=750
    def clip(text):
        nonlocal remaining
        value=text.encode('utf-16-le')[:remaining*2].decode('utf-16-le',errors='ignore')
        remaining-=len(value.encode('utf-16-le'))//2
        return value
    for literal,field,_,_ in string.Formatter().parse(template):
        result.append(escape(clip(literal)))
        if field is not None:
            value=clip(context[field])
            if field=='requester':result.append(mention(value,requester_id))
            elif field in {'brand','status'}:result.append('<b>'+escape(value)+'</b>')
            else:result.append(escape(value))
    return ''.join(result)
