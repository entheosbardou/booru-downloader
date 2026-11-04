"""Operator-configured Moebooru source, shared by the web app and CLI."""
import os
import re
import base64
from urllib.parse import urlparse

DEFAULT_SOURCE_URL = 'https://yande.re'
parsed = urlparse(os.environ.get('SOURCE_URL', DEFAULT_SOURCE_URL).strip())
if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
        or parsed.port not in (None, 443) or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
    raise ValueError('SOURCE_URL must be an HTTPS site origin, e.g. https://yande.re')
SOURCE_URL = 'https://' + parsed.hostname.lower()
SOURCE_NAME = os.environ.get('SOURCE_NAME', '').strip() or parsed.hostname
SOURCE_API = os.environ.get('SOURCE_API', 'moebooru').strip().lower()
if SOURCE_API not in ('moebooru', 'danbooru'):
    raise ValueError('SOURCE_API must be moebooru or danbooru.')
POSTS_PATH = '/posts.json' if SOURCE_API == 'danbooru' else '/post.json'
POST_PATH = '/posts/' if SOURCE_API == 'danbooru' else '/post/show/'
RATING_LABELS = ({'g': 'General', 's': 'Sensitive', 'q': 'Questionable', 'e': 'Explicit'}
                 if SOURCE_API == 'danbooru' else {'s': 'Safe', 'q': 'Questionable', 'e': 'Explicit'})
SAFE_RATING = 'g' if SOURCE_API == 'danbooru' else 's'
USER_AGENT = os.environ.get('SOURCE_USER_AGENT', 'PersonalCollectionDesk/1.0').strip()
_username = os.environ.get('SOURCE_USERNAME', '')
_api_key = os.environ.get('SOURCE_API_KEY', '')
if bool(_username) != bool(_api_key):
    raise ValueError('Set SOURCE_USERNAME and SOURCE_API_KEY together, or leave both empty.')
FILE_HOSTS = [parsed.hostname.lower()]
if SOURCE_API == 'danbooru' and parsed.hostname.endswith('.donmai.us'):
    FILE_HOSTS.append('cdn.donmai.us')
for value in os.environ.get('SOURCE_FILE_HOSTS', '').split(','):
    value = value.strip().lower()
    if not value:
        continue
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', value) or '..' in value:
        raise ValueError('SOURCE_FILE_HOSTS must contain comma-separated hostnames, without URLs.')
    if value not in FILE_HOSTS:
        FILE_HOSTS.append(value)


def valid_file_url(url):
    target = urlparse(url)
    if (target.scheme != 'https' or not target.hostname or target.username or target.password
            or target.port not in (None, 443) or not any(
                target.hostname == host or target.hostname.endswith('.' + host) for host in FILE_HOSTS)):
        raise ValueError('Image URL is outside the configured source file hosts.')
    return url


def api_headers():
    headers = {'User-Agent': USER_AGENT, 'Accept': 'application/json'}
    if _username and _api_key:
        token = base64.b64encode(f'{_username}:{_api_key}'.encode()).decode('ascii')
        headers['Authorization'] = 'Basic ' + token
    return headers


def post_url(pid):
    return SOURCE_URL + POST_PATH + str(pid)


def normalize_post(raw):
    if SOURCE_API != 'danbooru':
        return raw
    return dict(raw, tags=raw.get('tag_string', ''), width=raw.get('image_width', 0),
                height=raw.get('image_height', 0), preview_url=raw.get('preview_file_url', ''),
                sample_url=raw.get('large_file_url', ''))


def rating_directory(rating):
    if SOURCE_API == 'danbooru':
        return {'g': 'sfw', 's': 'sketchy', 'q': 'sketchy', 'e': 'nsfw'}.get(rating, 'Unrated')
    return {'s': 'sfw', 'q': 'sketchy', 'e': 'nsfw'}.get(rating, 'Unrated')
