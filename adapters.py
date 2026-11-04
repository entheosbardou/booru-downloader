"""Booru API adapters. Credentials are private profile data, never returned to the browser."""
import base64
import json
import re
import time
from http.cookies import SimpleCookie
from datetime import datetime, timezone
import xml.etree.ElementTree as ET
from pathlib import PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse, urlunparse, quote, parse_qsl
from urllib.request import Request, HTTPRedirectHandler, build_opener

from rate_limits import governor

ADAPTERS = {
    'moebooru': 'Moebooru', 'danbooru': 'Danbooru', 'gelbooru': 'Gelbooru / DAPI',
    'e621': 'e621 / e926', 'philomena': 'Philomena', 'shimmie': 'Shimmie / Paheal',
    'sankaku': 'Sankaku', 'zerochan': 'Zerochan JSON', 'animepictures': 'Anime-Pictures',
    'eshuushuu': 'e-shuushuu', 'szurubooru': 'Szurubooru',
    'json': 'Configurable JSON API', 'unconfigured': 'Needs adapter setup',
}
PRESETS = [
    dict(name='Yande.re', url='https://yande.re', api='moebooru'),
    dict(name='Konachan', url='https://konachan.com', api='moebooru'),
    dict(name='Konachan.net', url='https://konachan.net', api='moebooru'),
    dict(name='Danbooru', url='https://danbooru.donmai.us', api='danbooru', file_hosts=['cdn.donmai.us']),
    dict(name='Danbooru Safe', url='https://safebooru.donmai.us', api='danbooru', file_hosts=['cdn.donmai.us']),
    dict(name='AIBooru', url='https://aibooru.online', api='danbooru', file_hosts=['cdn.aibooru.download']),
    dict(name='Sonohara', url='https://sonohara.donmai.us', api_url='https://shima.donmai.us', api='danbooru', file_hosts=['cdn.donmai.us']),
    dict(name='Gelbooru', url='https://gelbooru.com', api='gelbooru', requires_credentials=True),
    dict(name='Safebooru', url='https://safebooru.org', api='gelbooru'),
    dict(name='TBIB', url='https://tbib.org', api='gelbooru'),
    dict(name='Xbooru', url='https://xbooru.com', api='gelbooru'),
    dict(name='Realbooru', url='https://realbooru.com', api='gelbooru'),
    dict(name='Hypnohub', url='https://hypnohub.net', api='gelbooru'),
    dict(name='Sakugabooru', url='https://www.sakugabooru.com', api='moebooru', file_hosts=['sakugabooru.com']),
    dict(name='e621', url='https://e621.net', api='e621', file_hosts=['e621.net']),
    dict(name='e926', url='https://e926.net', api='e621', file_hosts=['e621.net']),
    dict(name='Derpibooru', url='https://derpibooru.org', api='philomena', post_path='/', file_hosts=['derpicdn.net']),
    dict(name='Furbooru', url='https://furbooru.org', api='philomena', post_path='/', file_hosts=['furrycdn.org'], extra_params={'filter_id':2}),
    dict(name='Ponybooru', url='https://ponybooru.org', api='philomena', post_path='/'),
    dict(name='Twibooru', url='https://twibooru.org', api='philomena', api_path='/api/v3/search/posts', post_path='/', min_delay=6, extra_params={'filter_id':2}),
    dict(name='Rule34.xxx', url='https://rule34.xxx', api='gelbooru', api_url='https://api.rule34.xxx', requires_credentials=True),
    dict(name='Rule34 Paheal', url='https://rule34.paheal.net', api='shimmie', file_hosts=['paheal.net', 'paheal-cdn.net']),
    dict(name='Rule34Hentai', url='https://rule34hentai.net', api='shimmie'),
    dict(name='SizeChangeBooru', url='https://sizechangebooru.com', api_url='https://sizebooru.com', api='shimmie', file_hosts=['sizebooru.com']),
    dict(name='FindAFox', url='https://findafox.net', api='shimmie'),
    dict(name='FindAWolf', url='https://findawolf.com', api='shimmie', notes='Directory lists findawolf.com; findawolf.net did not resolve.'),
    dict(name='Bcbnsfw', url='https://booru.bcbnsfw.space', api='unconfigured', notes='Adapter needs verification; site returned a regional access restriction during inspection.'),
    dict(name='Sankaku Channel', url='https://chan.sankakucomplex.com', api='sankaku',
         api_url='https://sankakuapi.com', file_hosts=['sankakucomplex.com', 'sankaku.app'], requires_credentials=False),
    dict(name='Zerochan', url='https://www.zerochan.net', api='zerochan', post_path='/', file_hosts=['zerochan.net']),
    dict(name='Anime-Pictures', url='https://anime-pictures.net', api='animepictures',
         api_url='https://api.anime-pictures.net', file_hosts=['anime-pictures.net'], download_interval=5),
    dict(name='E-Shuushuu', url='https://e-shuushuu.net', api='eshuushuu', post_path='/image/', file_hosts=['e-shuushuu.net']),
]


ACCESS_NOTES = {
    'anime-pictures.net': ('Public API', 'Public posts do not require an API key. Restricted posts can require the Anime-Pictures login-session cookie, including for Google OAuth accounts. Use the site session, never Google credentials.', 'https://anime-pictures.net'),
    'gelbooru.com': ('API credentials', 'User ID and API key; API authentication can be enforced for all ratings, not only NSFW.', 'https://gelbooru.com/index.php?page=wiki&s=view&id=18780'),
    'rule34.xxx': ('API credentials', 'User ID and API key are mandatory for API requests, including NSFW.', 'https://api.rule34.xxx/'),
    'chan.sankakucomplex.com': ('Public / optional token', 'Anonymous requests can return public posts. An optional account access token applies account access; restricted or premium posts can still require additional permissions.', 'https://chan.sankakucomplex.com'),
    'danbooru.donmai.us': ('Account-dependent', 'Public posts can be anonymous. Username and API key authenticate your account; restricted post visibility depends on account privileges. NSFW rating alone does not mean every post needs a key.', 'https://danbooru.donmai.us/wiki_pages/help:api'),
    'e621.net': ('Public / optional login', 'Public post listing supports anonymous access. Username and API key are optional for account access; site restrictions may still apply.', 'https://e621.net/help/api'),
    'yande.re': ('Public API', 'Public listing supports anonymous access. No separate NSFW API key requirement is documented in the configured API; individual site restrictions still apply.', 'https://yande.re/help/api'),
    'konachan.com': ('Public API', 'Public API adapter supports anonymous requests. A separate NSFW authentication requirement has not been independently verified.', 'https://konachan.com/help/api'),
    'derpibooru.org': ('Public / optional key', 'Anonymous image search is supported. An optional API key applies account filters; rating filters and account settings affect visible content.', 'https://derpibooru.org/pages/api'),
    'furbooru.org': ('Public / optional key', 'Anonymous image search is supported. An optional API key applies account filters; rating filters and account settings affect visible content.', 'https://furbooru.org/pages/api'),
}
SAFE_SITES = {'safebooru.org','safebooru.donmai.us','konachan.net','e926.net','e-shuushuu.net'}

def access_notes(url):
    host = urlparse(url).hostname
    if host in SAFE_SITES:
        return dict(label='SFW site', detail='This source is intended for safe content. Adding credentials does not turn it into an NSFW source.', reference=url, nsfw='Not offered')
    label, detail, reference = ACCESS_NOTES.get(host, ('Unverified', 'A separate NSFW key/login requirement has not been verified for this site. Connection tests attempt NSFW metadata when rating filters are available; they do not certify access to every original.', url))
    return dict(label=label, detail=detail, reference=reference, nsfw='Credentials required for API' if host in ('gelbooru.com','rule34.xxx') else 'Public; some posts account-dependent' if host=='chan.sankakucomplex.com' else 'Account-dependent' if host=='danbooru.donmai.us' else 'No blanket key requirement documented' if host in ACCESS_NOTES else 'Unverified')


def origin(value):
    p = urlparse(str(value).strip())
    try:
        valid = p.scheme == 'https' and p.hostname and not p.username and not p.password and p.port in (None, 443) and p.path in ('', '/') and not p.query and not p.fragment
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('Site URLs must be HTTPS origins, for example https://yande.re')
    return 'https://' + p.hostname.lower()


def safe_subdir(value):
    value = str(value or '').strip().replace('\\', '/')
    if not value:
        return ''
    if value.startswith('/') or any(part in ('', '.', '..') or not re.fullmatch(r'[A-Za-z0-9._ -]+', part) for part in value.split('/')):
        raise ValueError('Download subfolder must be a relative folder name without .. or special characters.')
    return value


def number(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def field(data, path, default=None):
    if not path:
        return data
    value = data
    for part in str(path).split('.'):
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return default
    return value


class Redirects(HTTPRedirectHandler):
    def __init__(self, validator):
        self.validator = validator

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.validator(newurl)
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected and urlparse(req.full_url).netloc != urlparse(newurl).netloc:
            for key in ('Cookie', 'Authorization'):
                redirected.remove_header(key)
        return redirected


class BooruSource:
    def __init__(self, config):
        self.config = dict(config)
        self.id = number(config.get('id'))
        self.url = origin(config.get('url', ''))
        self.api_url = origin(config.get('api_url') or self.url)
        self.api = config.get('api', 'moebooru')
        if self.api not in ADAPTERS:
            raise ValueError('Choose a supported API adapter.')
        self.name = str(config.get('name') or urlparse(self.url).hostname).strip()[:100]
        self.username = str(config.get('username') or '')
        self.api_key = str(config.get('api_key') or '')
        self.cookie = str(config.get('cookie') or '')
        if any('\r' in value or '\n' in value for value in (self.cookie, self.api_key)):
            raise ValueError('Credentials must be single-line values.')
        if len(self.api_key) > 8192 or len(self.cookie) > 16384:
            raise ValueError('Credential value is too long.')
        self.user_agent = str(config.get('user_agent') or 'BooruCollectionDesk/2.0 (personal collection client)').strip()
        if '\n' in self.user_agent or '\r' in self.user_agent:
            raise ValueError('User agent must be a single line.')
        self.subdir = safe_subdir(config.get('download_subdir', ''))
        self.enabled = bool(config.get('enabled', True))
        self.requires_credentials = bool(config.get('requires_credentials', False))
        self.min_delay = max(0, min(6000, float(config.get('min_delay') if config.get('min_delay') is not None else 2)))
        self.download_interval = max(0, min(6000, float(config.get('download_interval') or 0)))
        self.mapping = config.get('mapping') or {}
        self.extra_params = config.get('extra_params') or {}
        if not isinstance(self.mapping, dict) or not isinstance(self.extra_params, dict):
            raise ValueError('Field mappings and extra parameters must be JSON objects.')
        self.response_path = str(config.get('response_path') or '')
        self.auth_mode = str(config.get('auth_mode') or 'auto')
        if self.auth_mode not in ('auto', 'none', 'basic', 'bearer', 'token', 'query', 'header'):
            raise ValueError('Unknown authentication mode.')
        self.api_key_name = str(config.get('api_key_name') or 'api_key')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', self.api_key_name):
            raise ValueError('API key parameter/header name is invalid.')
        self.api_path = str(config.get('api_path') or '')
        if self.api_path and (not self.api_path.startswith('/') or any(c in self.api_path for c in ('?', '#', '\\')) or self.api_path.startswith('//')):
            raise ValueError('Custom API path must start with / and contain no query or hostname.')
        self.post_path = str(config.get('post_path') or '')
        if self.post_path and (not self.post_path.startswith('/') or self.post_path.startswith('//') or any(c in self.post_path for c in ('?', '#', '\\'))):
            raise ValueError('Post path must be a path prefix, e.g. /posts/ or /.')
        self.page_ids = None
        self.next_cursor = None
        self.exhausted = False
        extra = config.get('file_hosts') or []
        if isinstance(extra, str):
            extra = extra.split(',')
        hosts = list(dict.fromkeys([urlparse(self.url).hostname, urlparse(self.api_url).hostname]))
        if self.api == 'danbooru' and urlparse(self.url).hostname.endswith('.donmai.us'):
            hosts.append('cdn.donmai.us')
        # Retained for compatibility with older exports; no host allowlist is enforced.
        self.file_hosts = hosts

    @property
    def rating_labels(self):
        if self.api == 'danbooru':
            return {'g': 'General', 's': 'Sensitive', 'q': 'Questionable', 'e': 'Explicit'}
        if self.api == 'philomena':
            return {'s': 'Safe', 'suggestive': 'Suggestive', 'q': 'Questionable', 'e': 'Explicit'}
        if self.api in ('shimmie', 'zerochan'):
            return {}
        if self.api == 'eshuushuu':
            return {'s': 'Safe'}
        if self.api == 'animepictures':
            return {'s':'Non-erotic','q':'Erotic','e':'Restricted erotic'}
        if self.api in ('json', 'unconfigured'):
            return {}
        return {'s': 'Safe', 'q': 'Questionable', 'e': 'Explicit'}

    @property
    def rating_groups(self):
        if not self.rating_labels:
            return []
        if self.api == 'eshuushuu':
            return ['sfw']
        return ['sfw','sketchy','nsfw']

    def public(self):
        return dict(id=self.id, name=self.name, url=self.url, api_url=self.api_url,
                    api=self.api, api_label=ADAPTERS[self.api], api_path=self.api_path,
                    file_hosts=self.file_hosts, download_subdir=self.subdir, enabled=self.enabled,
                    has_credentials=bool(self.api_key), requires_credentials=self.requires_credentials,
                    has_cookie=bool(self.cookie), post_path=self.post_path,
                    needs_setup=self.api == 'unconfigured', notes=self.config.get('notes', ''),
                    min_delay=self.min_delay, download_interval=self.download_interval,
                    mapping=self.mapping, extra_params=self.extra_params, response_path=self.response_path,
                    auth_mode=self.auth_mode, api_key_name=self.api_key_name,
                    query_param=self.config.get('query_param', 'q'), page_param=self.config.get('page_param', 'page'),
                    limit_param=self.config.get('limit_param', 'limit'), page_base=number(self.config.get('page_base'), 1),
                    needs_credentials=self.requires_credentials and not self.api_key,
                    user_agent=self.user_agent, rating_labels=self.rating_labels, rating_groups=self.rating_groups,
                    rating_order=list(self.rating_labels), safe_rating='g' if self.api == 'danbooru' else 's',
                    query_help='Use the site’s query syntax, e.g. landscape, safe' if self.api == 'philomena' else 'Use a Zerochan tag name, e.g. Landscape' if self.api == 'zerochan' else 'Use the site’s search tags or query',
                    last_test=self.config.get('last_test'), access=access_notes(self.url))

    def post_url(self, pid):
        pid = quote(str(pid), safe='')
        if self.post_path:
            return self.url + self.post_path + pid
        if self.api == 'gelbooru':
            return self.url + '/index.php?' + urlencode({'page': 'post', 's': 'view', 'id': pid})
        path = '/post/show/' if self.api == 'moebooru' else '/post/view/' if self.api == 'shimmie' else '/images/' if self.api == 'philomena' else '/posts/'
        return self.url + path + str(pid)

    @property
    def cursor_kind(self):
        return 'token' if self.api == 'sankaku' else 'page' if self.api in ('zerochan', 'animepictures', 'shimmie', 'eshuushuu', 'szurubooru', 'json') else 'id'

    def with_rating(self, tags, rating):
        if ',' in rating:
            groups = rating.split(',')
            if any(g not in ('sfw','sketchy','nsfw') for g in groups):
                raise ValueError('Choose valid rating groups.')
            if not set(groups).intersection(self.rating_groups):
                raise ValueError(f'{self.name} cannot filter the selected ratings.')
            if re.search(r'(^|\s)(rating|safety):', tags):
                raise ValueError('Use either a rating tag or the rating selector.')
            return tags
        if not rating:
            return tags
        if self.api == 'eshuushuu' and rating == 'sfw':
            return tags
        if not self.rating_labels:
            raise ValueError(f'{self.name} has no reliable rating filter. Choose Any rating or deselect it.')
        if rating not in ('sfw', 'sketchy', 'nsfw'):
            raise ValueError('Choose a valid rating group.')
        if self.api == 'animepictures':
            if rating not in self.rating_groups:
                raise ValueError('Choose a supported Anime-Pictures rating.')
            return tags
        if self.api == 'szurubooru':
            return tags + ' safety:' + {'sfw':'safe', 'sketchy':'sketchy', 'nsfw':'unsafe'}[rating]
        if self.api == 'philomena':
            term = {'sfw': 'safe', 'sketchy': '(suggestive || questionable)', 'nsfw': 'explicit'}[rating]
            return f'({tags}), {term}'
        if re.search(r'(^|\s)rating:', tags):
            raise ValueError('Use either a rating tag or the rating selector.')
        if self.api == 'gelbooru':
            return tags + ' rating:' + {'sfw':'safe', 'sketchy':'questionable', 'nsfw':'explicit'}[rating]
        term = {'sfw': 'g' if self.api == 'danbooru' else 's', 'sketchy': 's,q' if self.api == 'danbooru' else 'q', 'nsfw': 'e'}[rating]
        return tags + ' rating:' + term

    def apply_rating(self, tags, rating):
        if not rating:
            return tags
        if rating not in self.rating_labels:
            raise ValueError('This rating is not available on the selected site.')
        if self.api == 'philomena':
            term = {'s': 'safe', 'suggestive': 'suggestive', 'q': 'questionable', 'e': 'explicit'}[rating]
            return f'({tags}), {term}'
        if re.search(r'(^|\s)rating:', tags):
            raise ValueError('Use either a rating tag or the rating selector.')
        return tags + ' rating:' + rating

    def rating_directory(self, rating):
        if self.api == 'danbooru':
            return {'g': 'sfw', 's': 'sketchy', 'q': 'sketchy', 'e': 'nsfw'}.get(rating, 'Unrated')
        return {'g': 'sfw', 's': 'sfw', 'suggestive': 'sketchy', 'q': 'sketchy', 'e': 'nsfw'}.get(rating, 'Unrated')

    def absolute(self, value):
        if not value:
            return ''
        url = urljoin(self.url + '/', str(value))
        p = urlparse(url)
        if p.scheme == 'http':
            url = urlunparse(p._replace(scheme='https'))
        return url

    def valid_file_url(self, url):
        p = urlparse(url)
        if p.scheme != 'https' or not p.hostname or p.username or p.password or p.port not in (None, 443):
            raise ValueError('Image URLs must use HTTPS without embedded credentials or a custom port.')
        return url

    def valid_api_url(self, url):
        p = urlparse(url)
        if p.scheme != 'https' or p.hostname != urlparse(self.api_url).hostname or p.port not in (None, 443):
            raise ValueError('API redirect leaves the configured API host. Check the site profile.')
        return url

    def request(self, tags, cursor=None, offset=0, limit=100, rating=''):
        tags = self.with_rating(tags, rating)
        limit = min(50 if self.api == 'philomena' else 100, limit)
        headers = {'User-Agent': self.user_agent, 'Accept': 'application/json, application/xml;q=0.8'}
        if self.cookie:
            headers['Cookie'] = self.cookie
        if self.requires_credentials and not self.api_key:
            raise ValueError(f'{self.name} needs API credentials. Open Sites and add your user ID and API key.')
        if self.api in ('danbooru', 'e621', 'moebooru') and self.api_key:
            if not self.username:
                raise ValueError('This API needs both a username and an API key.')
            headers['Authorization'] = 'Basic ' + base64.b64encode(f'{self.username}:{self.api_key}'.encode()).decode()
        if self.api == 'philomena':
            query = f'({tags}), id.lt:{cursor}' if cursor else tags
            path = '/api/v1/json/search/images'
            params = dict(q=query, per_page=limit, page=1, sf='id', sd='desc')
            if self.api_key:
                params['key'] = self.api_key
        elif self.api == 'e621':
            path = '/posts.json'
            params = dict(tags=tags, limit=limit, page='b' + str(cursor) if cursor else '1')
        elif self.api == 'gelbooru':
            path = '/index.php'
            query = tags + (f' id:<{cursor}' if cursor else '')
            params = dict(page='dapi', s='post', q='index', json=1, limit=limit, pid=0, tags=query)
            if self.api_key:
                params.update(api_key=self.api_key, user_id=self.username)
        elif self.api == 'shimmie':
            path = '/api/danbooru/find_posts'
            # Shimmie supports one-based pages; an ID filter pins the descending window.
            params = dict(tags=tags, limit=limit, page=offset + 1)
        elif self.api == 'sankaku':
            path = '/v2/posts/keyset'
            params = dict(tags=tags, limit=limit, lang='en')
            if cursor:
                params['next'] = str(cursor)
            token = self.api_key
            if not token and self.cookie:
                cookies = SimpleCookie()
                try:
                    cookies.load(self.cookie)
                    token = cookies['accessToken'].value if 'accessToken' in cookies else ''
                except Exception:
                    token = ''
            if token:
                headers['Authorization'] = 'Bearer ' + token
            headers['Accept'] = 'application/vnd.sankaku.api+json;v=2'
        elif self.api == 'zerochan':
            path = '/' + quote(tags, safe=',+')
            params = dict(json=1, l=20, p=offset + 1)
        elif self.api == 'animepictures':
            path = '/api/v3/posts'
            params = dict(search_tag=tags, page=offset, posts_per_page=min(limit,80), lang='en')
        elif self.api == 'eshuushuu':
            path = '/api/v1/images'
            params = dict(tags=tags, tags_mode='all', page=offset + 1, per_page=limit, sort_by='image_id', sort_order='DESC')
            if not tags:
                params.pop('tags')
            if self.api_key:
                headers['Authorization'] = 'Bearer ' + self.api_key
        elif self.api == 'szurubooru':
            path = '/api/posts'
            params = dict(query=tags, offset=offset * limit, limit=limit)
            if self.api_key:
                headers['Authorization'] = 'Token ' + base64.b64encode(f'{self.username}:{self.api_key}'.encode()).decode()
        elif self.api == 'json':
            path = '/api/posts'
            params = {str(self.config.get('query_param') or 'q'): tags,
                      str(self.config.get('page_param') or 'page'): offset + number(self.config.get('page_base'), 1),
                      str(self.config.get('limit_param') or 'limit'): limit}
        elif self.api == 'unconfigured':
            raise ValueError('This catalog entry needs an API adapter configured under Sites before it can be searched.')
        else:
            path = '/posts.json' if self.api == 'danbooru' else '/post.json'
            params = dict(tags=tags + ' order:id_desc' + (f' id:<{cursor}' if cursor else ''), limit=limit)
        params.update(self.extra_params)
        if self.auth_mode != 'auto':
            headers.pop('Authorization', None)
            if self.api_key and self.auth_mode in ('basic', 'token'):
                headers['Authorization'] = ('Basic ' if self.auth_mode == 'basic' else 'Token ') + base64.b64encode(f'{self.username}:{self.api_key}'.encode()).decode()
            elif self.api_key and self.auth_mode == 'bearer':
                headers['Authorization'] = 'Bearer ' + self.api_key
            elif self.api_key and self.auth_mode == 'header':
                headers[self.api_key_name] = self.api_key
            elif self.api_key and self.auth_mode == 'query':
                params[self.api_key_name] = self.api_key
        return Request(self.api_url + (self.api_path or path) + '?' + urlencode(params), headers=headers)

    def parse(self, payload):
        payload = payload.lstrip(b'\xef\xbb\xbf \r\n\t')
        if payload.lstrip().startswith(b'<'):
            try:
                root = ET.fromstring(payload)
            except ET.ParseError:
                raise ValueError('The site returned an invalid API response or access challenge instead of posts.') from None
            if root.tag == 'response' and root.get('success') == 'false':
                raise ValueError('Site API error: '+str(root.get('reason') or 'Request rejected.')[:350])
            if root.tag != 'posts':
                raise ValueError('The site returned a web page instead of its post API. Check its API URL and credentials.')
            records = [dict(node.attrib) for node in root.findall('post')]
        else:
            data = json.loads(payload)
            if isinstance(data, list):
                records = data
            elif isinstance(data, dict):
                if self.api == 'sankaku':
                    self.next_cursor = (data.get('meta') or {}).get('next')
                    self.exhausted = not self.next_cursor
                if self.api == 'philomena':
                    key = 'images' if 'images' in data else 'posts'
                else:
                    key = 'images' if self.api == 'eshuushuu' else 'results' if self.api == 'szurubooru' else 'posts' if self.api in ('e621', 'animepictures') else 'data' if self.api == 'sankaku' else 'items' if self.api == 'zerochan' else 'post'
                if self.api == 'json':
                    records = field(data, self.response_path) if self.response_path else data.get('posts', data.get('items', data.get('results')))
                    if records is None:
                        raise ValueError('No post list found at the configured response path.')
                    key = None
                if key is None:
                    pass
                elif key not in data:
                    # Gelbooru can omit post for an empty response with a zero count.
                    if self.api == 'gelbooru' and str(data.get('@attributes', {}).get('count')) == '0':
                        records = []
                    else:
                        raise ValueError('Unexpected API response. Check the adapter and site credentials.')
                else:
                    records = data[key]
                    if isinstance(records, dict):
                        records = [records]
            else:
                raise ValueError('Unexpected API response.')
        if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
            raise ValueError('Unexpected post list.')
        result = [self.normalize(record) for record in records]
        if any(not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', str(post['id'])) or str(post['id']) == '0' for post in result):
            raise ValueError('The API returned a post without a valid ID.')
        return result

    def normalize(self, raw):
        post = dict(raw)
        if self.api == 'json':
            for key, path in self.mapping.items():
                post[key] = field(raw, path)
            raw = post
        if self.api == 'e621':
            file = raw.get('file') or {}
            preview = raw.get('preview') or {}
            tag_groups = raw.get('tags') or {}
            tags = [tag for group in tag_groups.values() for tag in group] if isinstance(tag_groups, dict) else []
            post.update(tags=' '.join(tags), file_url=file.get('url'), preview_url=preview.get('url'),
                        width=file.get('width'), height=file.get('height'), md5=file.get('md5'),
                        file_ext=file.get('ext'), file_size=file.get('size'),
                        score=(raw.get('score') or {}).get('total', 0), source='\n'.join(raw.get('sources') or []))
        elif self.api == 'danbooru':
            post.update(tags=raw.get('tag_string', ''), width=raw.get('image_width'), height=raw.get('image_height'),
                        preview_url=raw.get('preview_file_url'), sample_url=raw.get('large_file_url'))
        elif self.api == 'philomena':
            tags = raw.get('tags') or []
            reps = raw.get('representations') or {}
            rating = 'e' if 'explicit' in tags else 'q' if 'questionable' in tags else 'suggestive' if 'suggestive' in tags else 's' if 'safe' in tags else '?'
            post.update(tags=', '.join(tags), file_url=reps.get('full') or raw.get('view_url'),
                        preview_url=reps.get('thumb') or reps.get('small'), file_ext=raw.get('format'),
                        file_size=raw.get('size'), rating=rating,
                        checksum_algorithm='sha512', checksum=raw.get('sha512_hash', ''),
                        source='\n'.join(raw.get('source_urls') or [raw.get('source_url') or '']))
        elif self.api == 'sankaku':
            tags = raw.get('tag_names') or raw.get('tags') or []
            post['tags'] = ' '.join(str(tag.get('name_en') or tag.get('name') or '') if isinstance(tag, dict) else str(tag) for tag in tags) if isinstance(tags, list) else str(tags)
        elif self.api == 'zerochan':
            post.update(file_url=raw.get('full') or raw.get('file_url'), preview_url=raw.get('thumbnail') or raw.get('small') or raw.get('preview_url'),
                        md5=raw.get('hash') or raw.get('md5'), file_size=raw.get('size'), rating='?')
        elif self.api == 'animepictures':
            md5 = str(raw.get('md5') or '')
            ext = str(raw.get('ext') or raw.get('file_ext') or '').lstrip('.')
            tags = raw.get('tags') or []
            if isinstance(tags, list):
                tags = ' '.join(str((tag.get('tag') or {}).get('tag') or '') if isinstance(tag, dict) and isinstance(tag.get('tag'), dict) else str(tag.get('tag') or '') if isinstance(tag, dict) else str(tag) for tag in tags)
            post.update(tags=tags, file_url=(f'https://oimages.anime-pictures.net/{md5[:3]}/{md5}.{ext}' if len(md5) == 32 and ext else raw.get('file_url') or ''),
                        preview_url=raw.get('preview_url') or raw.get('small_preview') or (f'https://opreviews.anime-pictures.net/{md5[:3]}/{md5}_sp.png' if len(md5)==32 else ''), file_ext=ext,
                        file_size=raw.get('size') or raw.get('file_size'), rating={0:'s',1:'q',2:'e',3:'e'}.get(number(raw.get('erotics'),-1),'?'))
        elif self.api == 'eshuushuu':
            post.update(id=raw.get('image_id'), file_url=raw.get('url'), preview_url=raw.get('thumbnail_url'),
                        md5=raw.get('md5_hash'), file_ext=raw.get('ext'), file_size=raw.get('filesize'), rating='s',
                        tags=' '.join(str(tag.get('title') or '').replace(' ', '_') for tag in raw.get('tags') or []))
            raw = post
        elif self.api == 'szurubooru':
            post.update(file_url=raw.get('contentUrl'), preview_url=raw.get('thumbnailUrl'),
                        width=raw.get('canvasWidth'), height=raw.get('canvasHeight'), file_size=raw.get('fileSize'),
                        md5=raw.get('checksumMD5'), source=raw.get('source') or '',
                        tags=' '.join(str((tag.get('names') or [''])[0]) for tag in raw.get('tags') or []),
                        rating={'safe':'s','sketchy':'q','unsafe':'e'}.get(raw.get('safety'), '?'))
            if not raw.get('checksumMD5') and raw.get('checksumSHA1'):
                post.update(checksum_algorithm='sha1', checksum=raw['checksumSHA1'])
        if self.api == 'gelbooru' and urlparse(self.url).hostname == 'tbib.org':
            directory = str(raw.get('directory') or '')
            filename = str(raw.get('image') or '')
            if directory.isdigit() and filename and PurePosixPath(filename).name == filename and '\\' not in filename and '.' in filename:
                if not post.get('file_url'):
                    post['file_url'] = self.url+'/images/'+directory+'/'+quote(filename,safe='._-')
                if not post.get('preview_url'):
                    thumb = filename.rsplit('.',1)[0]+'.jpg'
                    post['preview_url'] = self.url+'/thumbnails/'+directory+'/thumbnail_'+quote(thumb,safe='._-')
        if self.api == 'shimmie' and not post.get('file_ext'):
            post['file_ext'] = PurePosixPath(str(raw.get('file_name') or '')).suffix.lstrip('.')
        value = raw.get('id')
        post['id'] = int(value) if str(value).isdigit() and len(str(value)) < 19 else str(value or '')
        for key in ('width', 'height', 'file_size', 'score'):
            post[key] = number(post.get(key))
        tags = post.get('tags') or ''
        post['tags'] = ' '.join(tags) if isinstance(tags, list) else str(tags)
        rating = str(post.get('rating') or '?').lower()
        post['rating'] = {'safe': 's', 'general': 'g', 'sensitive': 's', 'questionable': 'q', 'explicit': 'e'}.get(rating, rating)
        post['file_url'] = self.absolute(post.get('file_url'))
        post['preview_url'] = self.absolute(post.get('preview_url'))
        post['md5'] = str(post.get('md5') or post.get('hash') or '').lower()
        post['file_ext'] = str(post.get('file_ext') or PurePosixPath(urlparse(post['file_url']).path).suffix.lstrip('.')).lower()
        post['post_url'] = self.post_url(post['id'])
        created = raw.get('created_at') or raw.get('datetime')
        if isinstance(created, dict):
            created = created.get('s')
        try:
            stamp = datetime.fromisoformat(str(created).replace('Z', '+00:00'))
            stamp = stamp.replace(tzinfo=timezone.utc) if not stamp.tzinfo else stamp
            sort_key = stamp.timestamp()
        except (TypeError, ValueError):
            sort_key = number(created) or number(post['id'])
        post['_sort'] = sort_key
        if not post.get('checksum'):
            match = re.search(r'(?:^|/)([a-f0-9]{32})\.[a-z0-9]+$', urlparse(post['file_url']).path, re.I)
            post['md5'] = post['md5'] or (match.group(1).lower() if match else '')
            post['checksum_algorithm'] = 'md5'
            post['checksum'] = post['md5']
        return post

    def fetch(self, tags, cursor=None, offset=0, limit=100, delay=2, before_request=None, retries=5, timeout=45, rating=''):
        if ',' in rating:
            self.with_rating(tags, rating)
            self.page_ids = None
            records = self.fetch(tags, cursor=cursor, offset=offset, limit=limit, delay=delay,
                                 before_request=before_request, retries=retries, timeout=timeout, rating='')
            if self.page_ids is None:
                self.page_ids = [p['id'] for p in records]
            self.exhausted = self.exhausted or not self.page_ids
            return [p for p in records if self.rating_directory(p.get('rating')) in rating.split(',')]
        if self.api == 'eshuushuu':
            if rating not in ('', 'sfw'):
                return []
            ids = []
            for name in str(tags).split(','):
                name = name.strip()
                if not name or name == '*':
                    continue
                if name.isdigit():
                    ids.append(name)
                    continue
                lookup = Request(self.api_url + '/api/v1/tags?' + urlencode({'search':name.replace('_', ' '), 'per_page':100}), headers={'User-Agent':self.user_agent})
                response = self._read(lookup, delay, before_request, retries, timeout, json.loads)
                candidates = response.get('tags', []) if isinstance(response, dict) else response
                match = next((tag for tag in candidates if str(tag.get('title', '')).casefold().replace('_',' ') == name.casefold().replace('_',' ')), None)
                if not match:
                    raise ValueError(f'No exact tag match on {self.name}. Use comma-separated tag names or tag IDs.')
                ids.append(str(match['tag_id']))
            tags = ','.join(ids)
        req = self.request(tags, cursor=cursor, offset=offset, limit=limit, rating=rating)
        try:
            records = self._read(req, delay, before_request, retries, timeout)
        except json.JSONDecodeError:
            if self.api != 'gelbooru':
                raise ValueError('The site returned an empty or invalid JSON response. Check its API configuration and access requirements.') from None
            # Older DAPI installations silently return nothing for json=1; use their XML endpoint.
            parsed = urlparse(req.full_url)
            params = [(k,v) for k,v in parse_qsl(parsed.query, keep_blank_values=True) if k != 'json']
            xml_req = Request(urlunparse(parsed._replace(query=urlencode(params))), headers=dict(req.header_items()))
            try:
                records = self._read(xml_req, delay, before_request, retries, timeout)
            except (json.JSONDecodeError, ET.ParseError):
                raise ValueError('Neither JSON nor XML returned a valid post list. Check the site API and access requirements.') from None
        if self.api == 'zerochan':
            records = [self.detail(post, delay, before_request, retries, timeout) for post in records]
        elif self.api == 'animepictures':
            self.page_ids = [post['id'] for post in records]
            self.exhausted = not records
            # The list supplies original URL components, dimensions, rating and checksum.
            # Per-post tag-detail requests made an 80-result page need 80 additional calls.
            for post in records:
                if not post.get('tags'):
                    post['metadata_note'] = 'Tags are not included by this site’s search API.'
            if rating:
                records = [post for post in records if self.rating_directory(post['rating']) == rating]
        return records

    def download_headers(self, url):
        headers = {'User-Agent': self.user_agent, 'Referer': self.url+'/'}
        if self.api == 'sankaku':
            headers['Referer'] = 'https://sankaku.app/'
        if self.api == 'animepictures' and urlparse(url).hostname == 'oimages.anime-pictures.net':
            headers['Cookie'] = 'kira=2'
        # Saved credentials go only to the configured API host, never a CDN.
        if urlparse(url).hostname != urlparse(self.api_url).hostname:
            return headers
        if self.cookie:
            headers['Cookie'] = self.cookie
        if self.api_key:
            mode = self.auth_mode
            if mode == 'basic' or (mode == 'auto' and self.api in ('danbooru','e621','moebooru')):
                if not self.username:
                    raise ValueError('This site needs both a username and an API key.')
                headers['Authorization'] = 'Basic ' + base64.b64encode(f'{self.username}:{self.api_key}'.encode()).decode()
            elif mode == 'bearer':
                headers['Authorization'] = 'Bearer ' + self.api_key
            elif mode == 'token':
                headers['Authorization'] = 'Token ' + base64.b64encode(f'{self.username}:{self.api_key}'.encode()).decode()
        return headers

    def preview_headers(self, url):
        return self.download_headers(url)

    def detail(self, post, delay=2, before_request=None, retries=3, timeout=45):
        if self.api == 'zerochan':
            url = self.api_url + '/' + quote(str(post['id']), safe='') + '?json=1'
        elif self.api == 'animepictures':
            url = self.api_url + '/api/v3/posts/' + quote(str(post['id']), safe='')
        else:
            return post
        headers = {'User-Agent': self.user_agent, 'Accept': 'application/json'}
        if self.cookie:
            headers['Cookie'] = self.cookie
        req = Request(url, headers=headers)
        def detail_parser(payload):
            data = json.loads(payload)
            if self.api == 'animepictures' and isinstance(data, dict) and 'post' in data:
                data = dict(data['post'], tags=data.get('tags') or data['post'].get('tags') or [])
            if not isinstance(data, dict):
                raise ValueError('The site did not return full post metadata.')
            return self.normalize(dict(data, id=data.get('id') or post['id']))
        return self._read(req, delay, before_request, retries, timeout, detail_parser)

    def _read(self, req, delay, before_request, retries, timeout, parser=None):
        opener = build_opener(Redirects(self.valid_api_url))
        for attempt in range(retries):
            if before_request:
                before_request()
            else:
                time.sleep(delay)
            governor.before(self.url, req.full_url, interval=self.min_delay)
            try:
                with opener.open(req, timeout=timeout) as response:
                    governor.observe(self.url, req.full_url, response.headers)
                    payload = response.read(16 * 1024 * 1024 + 1)
                if len(payload) > 16 * 1024 * 1024:
                    raise ValueError('The site returned an oversized API response.')
                return (parser or self.parse)(payload)
            except HTTPError as exc:
                governor.observe(self.url, req.full_url, exc.headers, exc.code)
                if exc.code not in (429, 500, 502, 503, 504) or attempt == retries - 1:
                    hint = ' Check credentials, API URL, or site access restrictions.' if exc.code in (401, 403) else ''
                    raise ValueError(f'{self.name} API returned HTTP {exc.code}.{hint}') from None
                retry = exc.headers.get('Retry-After', '')
                time.sleep(max(2 ** (attempt + 1), min(300, int(retry)) if retry.isdigit() else 0))
            except (URLError, TimeoutError):
                if attempt == retries - 1:
                    raise ValueError(f'Could not reach {self.name}. Check the site connection and try again.') from None
                time.sleep(2 ** (attempt + 1))
