"""Shared per-origin/endpoint pacing and server-directed cooldowns (one app process)."""
import threading,time,math,re
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

class Deferred(Exception):
    def __init__(self,until):
        self.until=until
        super().__init__('Waiting for site rate-limit cooldown')

def seconds(value,now=None):
    now=time.time() if now is None else now
    try:
        n=float(value)
        return max(0,n) if math.isfinite(n) and n<=31536000 else 0
    except (ValueError,TypeError):
        try:return max(0,parsedate_to_datetime(str(value)).timestamp()-now)
        except (ValueError,TypeError,OverflowError):return 0

def reset_at(value,now,relative=False):
    try:
        n=float(str(value).split(',')[0].strip())
        if not math.isfinite(n) or n<0:return 0
        if n>1e12:n/=1000
        return now+n if relative or n<1e9 else n
    except (ValueError,TypeError):return now+seconds(value,now)

class Governor:
    def __init__(self,clock=time.time,sleep=time.sleep):
        self.clock=clock;self.sleep=sleep;self.guard=threading.Lock();self.states={}
    def key(self,site,url,api):
        host=urlparse(url).hostname or ''
        if not api:return ('media',host)
        root=urlparse(site).hostname or host
        if root.endswith('.donmai.us'):root='danbooru.donmai.us'
        if root in ('e621.net','e926.net'):root='e621.net'
        path=urlparse(url).path
        endpoint='api'
        if root=='twibooru.org':
            endpoint=re.sub(r'/posts/[^/]+','/posts/:id',path)
        return root,endpoint
    def interval(self,site,url,api):
        if not api:return 0.1
        host=urlparse(site).hostname
        if host=='twibooru.org':return 6.1 if urlparse(url).path=='/api/v3/search/posts' else 1.05
        if host=='rule34.xxx' or (host or '').endswith('.donmai.us'):return 1.05
        return 2.0 # Conservative app default, not a claim about the site's quota.
    def state(self,key):return self.states.setdefault(key,dict(next=0,until=0,interval=0,failures=0))
    def before(self,site,url,api=True,interval=None):
        key=self.key(site,url,api)
        with self.guard:
            s=self.state(key);now=self.clock()
            if s['until']>now:raise Deferred(s['until'])
            when=max(now,s['next'])
            if when-now>7:raise Deferred(when)
            s['next']=when+max(self.interval(site,url,api) if interval is None else interval,s['interval'])
        # No network or sleeps under the shared lock. Recheck cooldown after waiting.
        if when>now:self.sleep(when-now)
        with self.guard:
            if self.state(key)['until']>self.clock():raise Deferred(self.state(key)['until'])
    def observe(self,site,url,headers,status=200,api=True):
        h={k.lower():v for k,v in headers.items()};now=self.clock();key=self.key(site,url,api)
        until=0
        with self.guard:
            s=self.state(key)
            for prefix,relative in [('x-rl',False),('x-ratelimit',False),('x-rate-limit',False),('ratelimit',True)]:
                remaining=h.get(prefix+'-remaining');reset=h.get(prefix+'-reset')
                if remaining is None:continue
                try:remaining=max(0,float(remaining))
                except (ValueError,TypeError):continue
                end=reset_at(reset,now,relative) if reset else 0
                if remaining==0:until=max(until,end if end>now else now+60)
                elif end>now:s['interval']=max(s['interval'],(end-now)/(remaining+0.001))
            # Danbooru publishes bucket details as JSON in a single header.
            if h.get('x-rate-limit'):
                try:
                    import json
                    b=json.loads(h['x-rate-limit'])
                    buckets=list(b.values()) if isinstance(b,dict) else []
                    for bucket in buckets:
                        if not isinstance(bucket,dict):continue
                        left=bucket.get('remaining',bucket.get('remaining_points'))
                        rate=bucket.get('recharge_rate')
                        if left is not None and float(left)<=0 and rate and float(rate)>0:until=max(until,now+1/float(rate)+1)
                except (ValueError,TypeError):pass
            limited=status in (429,421) or (status in (403,503) and bool(h.get('retry-after')))
            if limited:
                s['failures']+=1
                until=max(until,now+max(seconds(h.get('retry-after'),now),min(900,60*2**min(s['failures']-1,4))))
            elif status<400 and s['until']<=now:s['failures']=0
            s['until']=max(s['until'],until)
            if limited:raise Deferred(s['until'])
        return until

governor=Governor()
