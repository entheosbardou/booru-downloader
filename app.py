"""Single-process, durable search/download queue for multiple booru APIs."""
import csv
import hashlib
import io
import json
import os
import re
import threading
import time
import weakref
import math
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from urllib.request import HTTPRedirectHandler, Request, build_opener

from flask import Flask, Response, abort, jsonify, request, send_from_directory, send_file
from crawler import FIELDS
from source import (DEFAULT_SOURCE_URL, SOURCE_URL, SOURCE_NAME, SOURCE_API, FILE_HOSTS, valid_file_url,
                    POST_PATH, RATING_LABELS, SAFE_RATING, USER_AGENT, post_url, rating_directory)
from adapters import BooruSource, PRESETS, ADAPTERS, safe_subdir, Redirects
from rate_limits import governor, Deferred
import tag_index
import purge_search
from relational import post_value
from library_groups import query_key, register_collection, shared_folder, migrate_library_groups

MYSQL_ENABLED = bool(os.environ.get('MYSQL_HOST'))
DATA = Path(os.environ.get('DATA_DIR', '/data'))
ROOT = Path(os.environ.get('DOWNLOAD_ROOT', '/downloads')).resolve()
DATA.mkdir(parents=True, exist_ok=True)
app = Flask(__name__, static_folder='static')
app.config['MAX_CONTENT_LENGTH'] = 1024 * 1024
WAKE = threading.Event()
SEARCH_WORKERS = 3
DOWNLOAD_WORKERS = 10
DOWNLOADS_PER_SITE = 5
PACING_GUARD = threading.Lock()
PACING = {}
FILE_GUARD = threading.Lock()
FILE_LOCKS = weakref.WeakValueDictionary()
PROGRESS_GUARD = threading.Lock()
TRANSFER_PROGRESS = {}
PREVIEW_SLOTS = threading.BoundedSemaphore(2)
PURGE_DIR = DATA / 'purge-queue'


def pace_search(delay, source_id=1):
    with PACING_GUARD:
        state = PACING.setdefault(source_id, [threading.Lock(), 0.0])
    with state[0]:
        time.sleep(max(0, state[1] - time.monotonic()))
        state[1] = time.monotonic() + delay


def file_lock(path):
    with FILE_GUARD:
        key = str(path)
        lock = FILE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            FILE_LOCKS[key] = lock
        return lock


def search_folder(sid, tags):
    label = re.sub(r'[^A-Za-z0-9._-]+', '_', tags).strip('._-')[:64] or 'search'
    return label


@contextmanager
def database():
    if not MYSQL_ENABLED:raise RuntimeError('MYSQL_HOST is required for the relational application')
    from mysql_store import connect
    db = connect()
    try:
        with db:
            yield db
    finally:
        db.close()


def create_collection(db, tags, rating, created, deleted=None):
    grouped = 'query_key' in {r[1] for r in db.execute('PRAGMA table_info(collections)')}
    if grouped and deleted is None:
        existing = db.execute('SELECT id FROM collections WHERE query_key=? AND rating=? AND deleted_at IS NULL',(query_key(tags),rating)).fetchone()
        if existing:
            return existing[0]
    base = search_folder(0, tags)
    folder = base
    suffix = 1
    while db.execute('SELECT 1 FROM collections WHERE folder_name=?', (folder,)).fetchone():
        suffix += 1
        folder = f'{base}_{suffix}'
    db.raw_execute('INSERT IGNORE INTO library_folders(query_key,folder_name) VALUES(%s,%s)',(query_key(tags),folder))
    gid = db.execute('INSERT INTO collections(tags,rating,created,folder_name,deleted_at) VALUES (?,?,?,?,?)',
                     (tags,rating,created,folder,deleted)).lastrowid
    if grouped:
        register_collection(db,db.execute('SELECT * FROM collections WHERE id=?',(gid,)).fetchone())
    return gid



def fast_page_query(ids, sort, limit, offset):
    field = {'post_newest':'post_sort','post_oldest':'post_sort','added_newest':'added_key','added_oldest':'added_key','score':'score','resolution':'pixels'}[sort]
    direction = 'ASC' if sort in ('post_oldest','added_oldest') else 'DESC'
    order = field+' '+direction+('' if field=='added_key' else ',post_id '+direction+',added_key '+direction)
    # Each site contributes at most the requested window, then merge tiny lists.
    pieces = [f'SELECT * FROM (SELECT post_id,search_id,added_key,{field} AS sort_value FROM result_order INDEXED BY result_order_{field} WHERE search_id=? ORDER BY {order} LIMIT ?) AS site_{i}' for i,_ in enumerate(ids)]
    sql = 'SELECT * FROM ('+' UNION ALL '.join(pieces)+') AS site_windows ORDER BY sort_value '+direction+',post_id '+direction+',added_key '+direction+' LIMIT ? OFFSET ?'
    params = [value for sid in ids for value in (sid,limit+offset)]
    return sql,params+[limit,offset]


from mysql_store import startup
startup()


def get_source(db, source_id, require_enabled=False):
    row = db.execute('SELECT * FROM sources WHERE id=?', (source_id,)).fetchone()
    if not row:
        raise ValueError('Site profile not found.')
    client = source_client(db,row)
    if require_enabled and not client.enabled:
        raise ValueError('This site is disabled. Enable it under Sites first.')
    return client


def source_client(db,row):
    return BooruSource(dict(db.source_config(row['id']), id=row['id'], name=row['name'],url=row['url'], api=row['api'], enabled=bool(row['enabled']) and row['deleted_at'] is None))


def destination(value, create=False):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Choose a default download folder in Settings first.')
    path = Path(value)
    if not path.is_absolute():
        raise ValueError('Use an absolute container folder under ' + str(ROOT))
    path = path.resolve()
    if path != ROOT and ROOT not in path.parents:
        raise ValueError('The download folder must stay under ' + str(ROOT))
    if not ROOT.is_dir():
        raise ValueError('The download mount is unavailable.')
    if create:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ('.write-test-' + os.urandom(8).hex())
        try:
            with probe.open('xb') as handle:
                handle.write(b'')
        finally:
            probe.unlink(missing_ok=True)
    return path



def search_display_name(search):
    labels={'sfw':'SFW','s':'SFW','sketchy':'Sketchy','q':'Sketchy','nsfw':'NSFW','e':'NSFW'}
    rating=search['rating'] or ''
    selection=' + '.join(labels.get(value,value) for value in rating.split(',') if value) if rating else 'All ratings'
    return search['tags']+' ('+selection+')'


def read_setting(db, key, default=''):
    row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    return row[0] if row else default


def add_job(db, kind, search_id, payload, total):
    if kind == 'crawl' and db.execute("SELECT 1 FROM jobs WHERE search_id=? AND kind='crawl' AND status IN ('queued','running')", (search_id,)).fetchone():
        raise ValueError('This search is already queued or running.')
    job = db.create_job(kind, search_id, payload, total)
    WAKE.set()
    return job


def positive(value, maximum):
    number = int(value)
    if not 1 <= number <= maximum:
        raise ValueError(f'Enter a number between 1 and {maximum}.')
    return number


@app.before_request
def same_origin():
    if request.view_args and 'sid' in request.view_args and request.endpoint != 'restore_search':
        with database() as db:
            if not db.execute('SELECT 1 FROM searches WHERE id=? AND deleted_at IS NULL', (request.view_args['sid'],)).fetchone():
                abort(404)
    if request.method == 'POST':
        if not request.is_json:
            abort(415)
        origin = request.headers.get('Origin')
        if origin and urlparse(origin).netloc != request.host:
            abort(403)


@app.errorhandler(Deferred)
def rate_limit_response(exc):
    return jsonify(error='Site rate limit: please wait before retrying.'),503,{'Retry-After':str(max(1,math.ceil(exc.until-time.time()))),'X-Preview-Busy':'1'}


@app.errorhandler(ValueError)
def bad_input(exc):
    return jsonify(error=str(exc)), 400


@app.errorhandler(OSError)
def io_error(exc):
    return jsonify(error=str(exc)), 400


@app.get('/')
def index():
    return send_from_directory(app.static_folder, 'index.html')


@app.get('/api/health')
def health():
    with database() as db:
        db.execute('SELECT 1').fetchone()
    return jsonify(ok=True)


@app.get('/api/state')
def state():
    with database() as db:
        clients = {row['id']: source_client(db,row) for row in db.execute('SELECT * FROM sources ORDER BY id')}
        searches = [dict(row) for row in db.execute("""WITH pending AS (
          SELECT j.search_id,count(DISTINCT j.download_post_id) AS total FROM jobs j
          WHERE j.kind='download' AND j.status IN ('queued','running')
          AND EXISTS(SELECT 1 FROM matches m WHERE m.search_id=j.search_id AND m.post_id=j.download_post_id)
          GROUP BY j.search_id)
          SELECT s.*,COALESCE(c.total,0) AS count,max(0,COALESCE(c.total,0)-COALESCE(p.total,0)) AS available
          FROM searches s LEFT JOIN search_counts c ON c.search_id=s.id LEFT JOIN pending p ON p.search_id=s.id
          WHERE s.deleted_at IS NULL ORDER BY s.id DESC""")]
        jobs = [dict(row) for row in db.execute('''SELECT j.id,j.kind,j.search_id,j.status,j.progress,j.total,j.message,j.cancel,s.source_id,
          CASE WHEN j.kind='download' THEN (SELECT remote_id FROM posts WHERE id=j.download_post_id) END AS post_id
          FROM jobs j JOIN searches s ON s.id=j.search_id WHERE s.deleted_at IS NULL AND j.id IN (
            SELECT id FROM jobs WHERE status='running'
            UNION SELECT id FROM (SELECT recent.id FROM jobs recent JOIN searches rs ON rs.id=recent.search_id
              WHERE recent.status!='cleared' AND rs.deleted_at IS NULL ORDER BY recent.id DESC LIMIT 100) AS recent_jobs)
          ORDER BY (j.status='running') DESC,j.id DESC LIMIT 100''')]
        with PROGRESS_GUARD:
            for job in jobs:
                if job['status']=='running' and job['id'] in TRANSFER_PROGRESS:
                    job['message']=TRANSFER_PROGRESS[job['id']]
        for job in jobs:
            client = clients[job['source_id']]
            job['site_name'] = client.name
            job['post_url'] = client.post_url(job['post_id']) if job['post_id'] else None
        latest_crawls = {row['search_id']:row for row in db.execute("SELECT j.search_id,j.status,j.message FROM jobs j JOIN (SELECT search_id,max(id) AS id FROM jobs WHERE kind='crawl' GROUP BY search_id) latest ON latest.id=j.id")}
        busy_crawls = {row[0] for row in db.execute("SELECT DISTINCT search_id FROM jobs WHERE kind='crawl' AND status IN ('queued','running')")}
        for search in searches:
            search['display_name'] = search_display_name(search)
            search['site_name'] = clients[search['source_id']].name
            recent = latest_crawls.get(search['id'])
            search['last_crawl'] = dict(recent) if recent else None
            search['busy'] = search['id'] in busy_crawls
            search['enabled'] = clients[search['source_id']].enabled
        collections = []
        for parent in db.execute('SELECT * FROM collections WHERE deleted_at IS NULL ORDER BY id DESC'):
            children = [child for child in searches if child['collection_id'] == parent['id']]
            if not children:
                continue
            collections.append(dict(parent, display_name=search_display_name(parent), folder_name=shared_folder(db,parent), children=[child['id'] for child in children],
                                    count=sum(child['count'] for child in children), available=sum(child['available'] for child in children),
                                    complete=all(child['complete'] for child in children), busy=any(child['busy'] for child in children),
                                    exhausted=sum(bool(child['complete']) for child in children)))
        revision = list(db.execute("SELECT (SELECT count(*) FROM downloads), (SELECT max(completed) FROM downloads), count(*), sum(CASE WHEN kind='download' AND status IN ('queued','running') THEN 1 ELSE 0 END) FROM jobs").fetchone())
        activity = {row['status']: row['count'] for row in db.execute('''SELECT status,count(*) AS count
          FROM jobs WHERE status!='cleared' AND search_id IN (SELECT id FROM searches WHERE deleted_at IS NULL) GROUP BY status''')}
        configured_default = read_setting(db, 'default_source_id', '')
        default_id = int(configured_default) if configured_default.isdigit() and int(configured_default) in clients else next(iter(clients), None)
        return jsonify(source=clients[default_id].public() if default_id is not None else None, sources=[client.public() for client in clients.values() if not db.execute('SELECT deleted_at FROM sources WHERE id=?',(client.id,)).fetchone()[0]],
                       archived_sources=[client.public() for client in clients.values() if db.execute('SELECT deleted_at FROM sources WHERE id=?',(client.id,)).fetchone()[0]],
                       default_source_id=default_id, adapters=ADAPTERS,
                       tag_index=tag_index.status(db), searches=searches, collections=collections, jobs=jobs, activity=activity, download_revision=revision, settings={
            'default_path': read_setting(db, 'default_path'),
            'download_root': str(ROOT), 'root_available': ROOT.is_dir(),
            'delay': float(read_setting(db, 'delay', '2'))})


@app.post('/api/sources')
@app.post('/api/sources/<int:source_id>')
def save_source(source_id=None):
    body = request.get_json()
    if not isinstance(body, dict):
        raise ValueError('Expected a site configuration object.')
    allowed = ('name', 'url', 'api', 'api_url', 'api_path', 'post_path', 'file_hosts', 'download_subdir',
               'user_agent', 'min_delay', 'download_interval', 'requires_credentials', 'enabled',
               'mapping', 'extra_params', 'response_path', 'auth_mode', 'api_key_name',
               'query_param', 'page_param', 'limit_param', 'page_base')
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        if source_id and db.execute('SELECT deleted_at FROM sources WHERE id=?',(source_id,)).fetchone() and db.execute('SELECT deleted_at FROM sources WHERE id=?',(source_id,)).fetchone()[0] is not None:
            raise ValueError('This site was removed. Restore it before editing.')
        old = get_source(db, source_id) if source_id else None
        config = dict(old.config) if old else {}
        config.update({key: body[key] for key in allowed if key in body})
        if not source_id and 'download_subdir' not in body:
            config['download_subdir'] = urlparse(str(config.get('url', ''))).hostname or 'site'
        if body.get('clear_credentials'):
            for key in ('username', 'api_key', 'cookie'):
                config.pop(key, None)
        for key in ('username', 'api_key', 'cookie'):
            if body.get(key):
                config[key] = str(body[key])
        client = BooruSource(config)
        if old and old.api_url != client.api_url:
            if not body.get('api_key'):
                config.pop('api_key', None)
            if not body.get('cookie'):
                config.pop('cookie', None)
        if not source_id:
            duplicate = db.execute('SELECT id FROM sources WHERE url=? AND api=? AND deleted_at IS NULL', (client.url, client.api)).fetchone()
            if duplicate:
                raise ValueError('That website and API software already have a saved site. Select it under Edit saved site.')
        config.update(name=client.name, url=client.url, api=client.api, api_url=client.api_url,
                      file_hosts=client.file_hosts, download_subdir=client.subdir)
        config.pop('last_test', None)
        source_id = db.save_source(source_id, config)
        if old and (old.url != client.url or old.api != client.api or old.api_url != client.api_url):
            db.execute('DELETE FROM source_checks WHERE source_id=?', (source_id,))
        if not client.enabled:
            db.execute("UPDATE jobs SET cancel=1,status=CASE WHEN status='queued' THEN 'stopped' ELSE status END WHERE status IN ('queued','running') AND search_id IN (SELECT id FROM searches WHERE source_id=?)", (source_id,))
    return jsonify(id=source_id)


@app.post('/api/sources/<int:source_id>/remove')
def remove_source(source_id):
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        get_source(db,source_id)
        db.raw_execute("""UPDATE sources SET deleted_at=COALESCE(deleted_at,UTC_TIMESTAMP(6)),enabled=0,
          api_url=NULL,api_path=NULL,download_subdir=NULL,user_agent=NULL,min_delay=NULL,
          download_interval=NULL,requires_credentials=NULL,auth_mode=NULL,api_key_name=NULL,
          response_path=NULL,query_param=NULL,page_param=NULL,limit_param=NULL,page_base=NULL,
          notes=NULL,credential_ref=NULL WHERE id=%s""",(source_id,))
        db.execute("UPDATE jobs SET cancel=1,status=CASE WHEN status='queued' THEN 'stopped' ELSE status END WHERE status IN ('queued','running') AND search_id IN (SELECT id FROM searches WHERE source_id=?)",(source_id,))
        default = db.raw_execute('SELECT value FROM settings WHERE `key`=%s',('default_source_id',)).fetchone()
        if default and default[0]==str(source_id):
            replacement = db.raw_execute('SELECT id FROM sources WHERE deleted_at IS NULL ORDER BY id LIMIT 1').fetchone()
            if replacement:
                db.raw_execute('UPDATE settings SET value=%s WHERE `key`=%s',(str(replacement[0]),'default_source_id'))
            else:
                db.raw_execute('DELETE FROM settings WHERE `key`=%s',('default_source_id',))
        for table in ('source_allowed_hosts','source_field_mappings','source_options','source_checks','source_runtime'):
            db.raw_execute(f'DELETE FROM {table} WHERE source_id=%s',(source_id,))
    delete_site_credential_files(source_id)
    return jsonify(ok=True,archived=True)


def delete_site_credential_files(source_id):
    import shutil
    credential_root = Path(os.environ.get('BOORU_CREDENTIAL_DIR','/run/booru-credentials'))
    if credential_root.is_dir():
        for directory in credential_root.iterdir():
            if re.fullmatch(rf'source-{source_id}-[0-9a-f]{{32}}',directory.name):
                shutil.rmtree(directory)


def cleanup_archived_source(db, source_id):
    """Remove operational records after active workers have stopped."""
    active = db.raw_execute("""SELECT 1 FROM jobs j JOIN collection_sources s ON s.id=j.search_id
      WHERE s.source_id=%s AND j.status='running' LIMIT 1""",(source_id,)).fetchone()
    if active:
        return
    jobs = [row[0] for row in db.raw_execute("""SELECT j.id FROM jobs j JOIN collection_sources s ON s.id=j.search_id
      WHERE s.source_id=%s ORDER BY j.id LIMIT 500""",(source_id,)).fetchall()]
    if jobs:
        db.raw_execute('DELETE FROM jobs WHERE id IN ('+','.join(['%s']*len(jobs))+')',jobs)
        return
    for table in ('source_tags','tag_aliases','source_allowed_hosts','source_field_mappings','source_options','source_checks'):
        if db.raw_execute(f'DELETE FROM {table} WHERE source_id=%s LIMIT 1000',(source_id,)).rowcount:
            return
    db.raw_execute('DELETE FROM source_runtime WHERE source_id=%s',(source_id,))


def archived_source_cleanup_worker():
    while True:
        try:
            with database() as db:
                sources = [row[0] for row in db.raw_execute('SELECT id FROM sources WHERE deleted_at IS NOT NULL AND enabled=0 ORDER BY id').fetchall()]
                for source_id in sources:
                    cleanup_archived_source(db,source_id)
                    delete_site_credential_files(source_id)
        except Exception as exc:
            app.logger.exception('Archived site cleanup failed: %s',exc)
        time.sleep(2)


@app.post('/api/sources/<int:source_id>/restore')
def restore_source(source_id):
    with database() as db:
        get_source(db,source_id)
        db.execute('UPDATE sources SET deleted_at=NULL WHERE id=?',(source_id,))
    return jsonify(ok=True)


@app.post('/api/sources/<int:source_id>/test')
def test_source(source_id):
    with database() as db:
        client = get_source(db, source_id)
        delay = client.min_delay
    query = '' if client.api == 'animepictures' else 'Landscape' if client.api == 'zerochan' else 'safety:safe' if client.api == 'szurubooru' else 'safe' if client.api == 'philomena' else '*' if client.api in ('eshuushuu', 'zerochan', 'shimmie', 'json', 'unconfigured') else 'rating:g' if client.api == 'danbooru' else 'rating:safe' if client.api == 'gelbooru' else 'rating:s'
    try:
        records = client.fetch(query, limit=1, delay=delay,
                               before_request=lambda: pace_search(delay, source_id), retries=1, timeout=15)
        nsfw = False
        nsfw_note = ' NSFW access is unverified.'
        from adapters import SAFE_SITES
        if urlparse(client.url).hostname in SAFE_SITES:
            nsfw_note = ' SFW-only source; NSFW is not offered.'
        elif client.rating_labels and client.api != 'eshuushuu':
            try:
                sample = client.fetch('nude' if client.api == 'animepictures' else '*', limit=1, rating='nsfw', delay=delay,
                                      before_request=lambda: pace_search(delay,source_id),retries=1,timeout=15)
                nsfw = any(client.rating_directory(post.get('rating')) == 'nsfw' and post.get('file_url') and client.valid_file_url(post['file_url']) for post in sample)
                nsfw_note = ' NSFW metadata and an original URL are accessible.' if nsfw else ' NSFW access not confirmed: no accessible matching original was returned.'
            except Exception:
                nsfw_note = ' NSFW access was not confirmed; check account permissions or site restrictions.'
        result = dict(ok=True, message=f'Connected. {len(records)} sample post(s) returned.'+nsfw_note,
                      checked_at=time.time(),nsfw_available=nsfw)
    except Exception as exc:
        result = dict(ok=False, message=str(exc)[:300], checked_at=time.time())
    with database() as db:
        db.save_source_check(source_id,result)
    return jsonify(result)


@app.post('/api/settings')
def settings():
    body = request.get_json()
    path = body.get('default_path', '').strip()
    if path:
        destination(path, create=True)
    delay = float(body.get('delay', 2))
    if not math.isfinite(delay) or not 0 <= delay <= 6000:
        raise ValueError('File request rate must be between 0.01 and 60,000 per minute, or 0 for no manual limit.')
    with database() as db:
        for key, value in [('default_path', path), ('delay', str(delay))]:
            db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, value))
    return jsonify(ok=True)


@app.post('/api/searches')
def new_search():
    body = request.get_json()
    tags = str(body.get('tags', '')).strip()
    if not tags or len(tags) > 512:
        raise ValueError('Enter search tags (up to 512 characters).')
    if any(tag.startswith(('id:', 'order:', 'sort:')) for tag in tags.split()):
        raise ValueError('ID and ordering operators are reserved for resumable searches.')
    batches = positive(body.get('batches', 10), 1000)
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        source_ids = body['source_ids'] if 'source_ids' in body else [body.get('source_id') or int(read_setting(db, 'default_source_id', '1'))]
        if not isinstance(source_ids, list) or not source_ids or len(source_ids) > 100 or any(type(i) is not int for i in source_ids):
            raise ValueError('Choose one or more sites.')
        source_ids = list(dict.fromkeys(source_ids))
        ratings = body.get('ratings')
        if ratings is not None:
            if not isinstance(ratings,list) or not ratings or any(r not in ('any','sfw','sketchy','nsfw') for r in ratings):
                raise ValueError('Choose one or more valid rating groups.')
            rating = '' if 'any' in ratings else ','.join(r for r in ('sfw','sketchy','nsfw') if r in ratings)
        else:
            rating = str(body.get('rating') or '')
            if rating not in ('', 'sfw', 'sketchy', 'nsfw'):
                raise ValueError('Choose a valid rating group.')
        for source_id in source_ids:
            client = get_source(db, source_id, require_enabled=True)
            if client.api == 'unconfigured':
                raise ValueError(f'{client.name} needs an adapter configured under Sites.')
            if client.requires_credentials and not client.api_key:
                raise ValueError(f'{client.name} needs credentials under Sites first.')
            client.with_rating(tags, rating)
        gid = create_collection(db, tags, rating, time.time())
        searches, jobs = [], []
        parent = db.execute('SELECT * FROM collections WHERE id=?',(gid,)).fetchone()
        for source_id in source_ids:
            existing = db.execute('SELECT * FROM searches WHERE collection_id=? AND source_id=? ORDER BY deleted_at IS NOT NULL,id LIMIT 1',(gid,source_id)).fetchone()
            if existing:
                sid = existing['id']
                if existing['deleted_at'] is not None:
                    db.execute('UPDATE searches SET deleted_at=NULL WHERE id=?',(sid,))
                    if not existing['complete']:
                        jobs.append(add_job(db,'crawl',sid,{'batches':batches},batches))
                searches.append(sid)
                continue
            sid = db.execute('INSERT INTO searches(tags,source_id,rating,created,collection_id) VALUES (?,?,?,?,?)',
                             (parent['tags'], source_id, rating, time.time(), gid)).lastrowid
            jobs.append(add_job(db, 'crawl', sid, {'batches': batches}, batches))
            searches.append(sid)
    return jsonify(id=searches[0], collection_id=gid, ids=searches, jobs=jobs)


def scoped_searches(db, sid=None, gid=None, include_deleted=False):
    if gid is not None:
        parent = db.execute('SELECT * FROM collections WHERE id=?', (gid,)).fetchone()
        if not parent or (not include_deleted and parent['deleted_at'] is not None):
            abort(404)
        rows = db.execute('SELECT * FROM searches WHERE collection_id=?' + ('' if include_deleted else ' AND deleted_at IS NULL'), (gid,)).fetchall()
    else:
        rows = db.execute('SELECT * FROM searches WHERE id=?' + ('' if include_deleted else ' AND deleted_at IS NULL'), (sid,)).fetchall()
    if not rows:
        abort(404)
    return rows


@app.post('/api/collections/<int:gid>/resume')
@app.post('/api/searches/<int:sid>/resume')
def resume(sid=None, gid=None):
    body = request.get_json()
    batches = positive(body.get('batches', 10), 1000)
    jobs, skipped = [], []
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        for search in scoped_searches(db, sid, gid):
            client = get_source(db, search['source_id'])
            busy = db.execute("SELECT 1 FROM jobs WHERE search_id=? AND kind='crawl' AND status IN ('queued','running')", (search['id'],)).fetchone()
            reason = 'already running' if busy else 'disabled' if not client.enabled else 'exhausted' if search['complete'] and not body.get('restart') else None
            if reason:
                skipped.append(dict(search_id=search['id'],site=client.name,reason=reason))
                continue
            if body.get('restart'):
                db.execute("UPDATE searches SET cursor=NULL,offset=0,page_signature='',complete=0 WHERE id=?", (search['id'],))
            jobs.append(add_job(db, 'crawl', search['id'], {'batches':batches}, batches))
    return jsonify(jobs=jobs, queued=len(jobs), skipped=skipped)


@app.post('/api/collections/<int:gid>/delete')
@app.post('/api/searches/<int:sid>/delete')
def delete_search(sid=None, gid=None):
    search_ids = []
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        rows = scoped_searches(db,sid,gid)
        search_ids = [row['id'] for row in rows]
        stamp = time.time()
        if gid is not None:
            db.execute('UPDATE collections SET deleted_at=? WHERE id=?', (stamp,gid))
        for row in rows:
            db.execute('UPDATE searches SET deleted_at=? WHERE id=?', (stamp,row['id']))
            db.execute("UPDATE jobs SET cancel=1,status=CASE WHEN status='queued' THEN 'stopped' ELSE status END WHERE search_id=? AND status IN ('queued','running')", (row['id'],))
    purge_search.request(PURGE_DIR, search_ids)
    return jsonify(ok=True, purging=True)


def search_purge_worker():
    PURGE_DIR.mkdir(parents=True, exist_ok=True)
    while True:
        markers = list(PURGE_DIR.glob('*.pending'))
        if markers:
            marker = markers[0]
            try:
                search_id = int(marker.stem)
                with database() as db:
                    done = purge_search.step(db, search_id)
                if done:
                    marker.unlink(missing_ok=True)
            except Exception as exc:
                app.logger.exception('Search purge failed for %s: %s', marker.name, exc)
        time.sleep(0.25 if markers else 2)


@app.post('/api/collections/<int:gid>/restore')
@app.post('/api/searches/<int:sid>/restore')
def restore_search(sid=None, gid=None):
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        rows = scoped_searches(db,sid,gid,include_deleted=True)
        if any(purge_search.pending(PURGE_DIR,row['id']).exists() for row in rows):
            raise ValueError('This search is being permanently removed and cannot be restored.')
        restored_gid = gid if gid is not None else rows[0]['collection_id']
        restored_parent = db.execute('SELECT * FROM collections WHERE id=?',(restored_gid,)).fetchone()
        collision = db.execute('SELECT id FROM collections WHERE query_key=? AND rating=? AND deleted_at IS NULL AND id!=?',
                               (query_key(restored_parent['tags']),restored_parent['rating'],restored_gid)).fetchone()
        if collision:
            db.defer_active_keys=True
            db.raw_execute('UPDATE collections SET active_query=NULL WHERE query_key=%s AND rating=%s',(query_key(restored_parent['tags']),restored_parent['rating']))
            db.raw_execute('UPDATE collection_sources s JOIN collections c ON c.id=s.collection_id SET s.active_collection=NULL WHERE c.query_key=%s AND c.rating=%s',(query_key(restored_parent['tags']),restored_parent['rating']))
            db.execute("DELETE FROM settings WHERE key='shared_query_collections_v1'")
        if gid is not None:
            stamp = db.execute('SELECT deleted_at FROM collections WHERE id=?',(gid,)).fetchone()[0]
            db.execute('UPDATE collections SET deleted_at=NULL WHERE id=?',(gid,))
            # Do not restore children that had been independently removed earlier.
            db.execute('UPDATE searches SET deleted_at=NULL WHERE collection_id=? AND deleted_at=?',(gid,stamp))
        else:
            db.execute('UPDATE searches SET deleted_at=NULL WHERE id=?',(sid,))
            db.execute('UPDATE collections SET deleted_at=NULL WHERE id=?',(rows[0]['collection_id'],))
        if collision:
            migrate_library_groups(db)
            db.raw_execute('UPDATE collections SET active_query=IF(deleted_at IS NULL,query_key,NULL)')
            db.raw_execute('UPDATE collection_sources SET active_collection=IF(deleted_at IS NULL,collection_id,NULL)')
            db.defer_active_keys=False
    return jsonify(ok=True)


def refresh_expiring_media(record_id, client, post, field, force=False):
    if client.api != 'sankaku':
        return post
    def expired(value):
        params = parse_qs(urlparse(value or '').query)
        expires = params.get('expires') or params.get('e')
        return bool(expires and expires[0].isdigit() and int(expires[0]) <= time.time()+30)
    if not force and not expired(post.get(field)):
        return post
    with file_lock('sankaku-media-'+str(record_id)):
        with database() as db:
            row = db.execute('SELECT * FROM post_records WHERE id=?',(record_id,)).fetchone()
            cached = post_value(row)
        if not force and not expired(cached.get(field)):
            return cached
        lookup = 'md5:'+post['md5'] if re.fullmatch(r'[a-fA-F0-9]{32}',post.get('md5') or '') else 'id_range:'+str(post['id'])
        fresh = client.fetch(lookup, limit=1, delay=0, retries=1, timeout=20,
                             before_request=lambda: pace_search(client.min_delay,client.id))
        match = next((p for p in fresh if str(p['id']) == str(post['id'])),None)
        if not match:
            raise ValueError('Sankaku no longer provides this post. Open its source page to check access.')
        cached.update(match)
        with database() as db:
            db.save_post(cached,client.id,record_id=record_id)
        return cached


@app.get('/api/posts/<int:record_id>/preview')
def proxy_preview(record_id):
    if not PREVIEW_SLOTS.acquire(blocking=False):
        return Response(status=503,headers={'Retry-After':'1','X-Preview-Busy':'1','Cache-Control':'no-store'})
    try:
        return fetch_preview(record_id)
    finally:
        PREVIEW_SLOTS.release()


def fetch_preview(record_id):
    with database() as db:
        record = db.execute('SELECT * FROM post_records WHERE id=?',(record_id,)).fetchone()
        if not record:
            abort(404)
        client = get_source(db,record['source_id'])
        post = post_value(record)
        media_interval = client.download_interval or float(read_setting(db, 'delay', '2'))
    post = refresh_expiring_media(record_id, client, post, 'preview_url')
    url = client.valid_file_url(client.absolute(post.get('preview_url') or ''))
    req = Request(url,headers=client.preview_headers(url))
    governor.before(client.url,url,api=False,interval=media_interval)
    try:
        with build_opener(Redirects(client.valid_file_url)).open(req,timeout=12) as response:
            governor.observe(client.url,url,response.headers,api=False)
            kind = response.headers.get('Content-Type','').split(';')[0].lower()
            if not kind.startswith('image/'):
                raise ValueError('The source did not provide an image preview.')
            body = response.read(4*1024*1024+1)
            if len(body)>4*1024*1024 or not body:
                raise ValueError('The preview is empty or too large.')
            cache = 'no-store' if 'no-store' in response.headers.get('Cache-Control','') else 'private, max-age=300'
    except HTTPError as exc:
        governor.observe(client.url,url,exc.headers,exc.code,api=False)
        raise ValueError('The source preview is currently unavailable.') from None
    except OSError:
        raise ValueError('The source preview is currently unavailable.') from None
    return Response(body,content_type=kind,headers={'Cache-Control':cache,'X-Content-Type-Options':'nosniff'})


@app.get('/api/collections/<int:gid>/posts')
@app.get('/api/searches/<int:sid>/posts')
def posts(sid=None, gid=None):
    page = positive(request.args.get('page', 1), 1000000)
    per_page = positive(request.args.get('per_page', 48), 500)
    chunk_size = min(per_page,positive(request.args.get('chunk_size',per_page),500))
    chunk_offset = int(request.args.get('chunk_offset',0))
    if not 0 <= chunk_offset < per_page:
        raise ValueError('Choose a valid page chunk offset.')
    term = request.args.get('filter', '').strip()
    orders = {
        'post_newest': ('p.sort_key','DESC'),
        'post_oldest': ('p.sort_key','ASC'),
        'added_newest': ('m.rowid','DESC'),
        'added_oldest': ('m.rowid','ASC'),
        'score': ("COALESCE(p.score,0)",'DESC'),
        'resolution': ("COALESCE(p.width,0)*COALESCE(p.height,0)",'DESC'),
    }
    sort = request.args.get('sort','post_newest')
    if sort not in orders:
        raise ValueError('Choose a supported result sort order.')
    expression,direction = orders[sort]
    with database() as db:
        searches = scoped_searches(db,sid,gid)
        remote_match=bool(term and re.fullmatch(r'[A-Za-z0-9_-]{3,128}',term))
        if remote_match:
            searches=db.execute('''SELECT s.* FROM searches s JOIN collections c ON c.id=s.collection_id
                WHERE s.deleted_at IS NULL AND c.deleted_at IS NULL ORDER BY s.id''').fetchall()
        ids = [row['id'] for row in searches]
        slots = ','.join('?' for _ in ids)
        where = f"m.search_id IN ({slots})"
        params = list(ids)
        lookup_job=None
        if term and remote_match:
            filtered_sql='''SELECT ro.* FROM result_order ro JOIN posts p ON p.id=ro.post_id
              WHERE ro.search_id IN ('''+slots+''') AND p.remote_id=?'''
            filtered_params=[*ids,term]
            if re.fullmatch(r'[0-9]{1,19}',term):
                lookup_job=db.execute("""SELECT j.id,j.kind,j.status,j.message,j.created,j.search_id,j.download_post_id,
                    s.tags,src.name AS site_name,p.remote_id,p.post_url FROM jobs j
                    JOIN searches s ON s.id=j.search_id JOIN sources src ON src.id=s.source_id
                    LEFT JOIN posts p ON p.id=j.download_post_id WHERE j.id=? AND j.kind='download'""",(int(term),)).fetchone()
                if lookup_job:
                    filtered_sql='''SELECT ro.* FROM result_order ro JOIN posts p ON p.id=ro.post_id
                      WHERE ro.search_id IN ('''+slots+''') AND p.remote_id=?
                      UNION SELECT ro.* FROM result_order ro JOIN jobs j ON j.search_id=ro.search_id
                        AND j.download_post_id=ro.post_id WHERE ro.search_id IN ('''+slots+''')
                        AND j.id=? AND j.kind='download' '''
                    filtered_params=[*ids,term,*ids,int(term)]
            total=db.execute('SELECT count(*) FROM ('+filtered_sql+') AS filtered',filtered_params).fetchone()[0]
        elif term:
            filtered_sql, filtered_params = tag_index.filter_query(db,ids,term)
            total = db.execute('SELECT count(*) FROM ('+filtered_sql+') AS filtered',filtered_params).fetchone()[0]
        else:
            total = db.execute('SELECT COALESCE(sum(total),0) FROM search_counts WHERE search_id IN ('+slots+')',ids).fetchone()[0]
        total_pages = max(1, (total+per_page-1)//per_page)
        page = min(page,total_pages)
        parent = db.execute('SELECT tags,folder_name FROM collections WHERE id=?',(searches[0]['collection_id'],)).fetchone()
        if sort.startswith('added_') and not term:
            page_from = 'matches m'
        else:
            cover = ' INDEXED BY posts_sort_cover' if sort.startswith('post_') and not term else ''
            page_from = 'matches m JOIN posts p'+cover+' ON p.id=m.post_id'
        page_limit = min(chunk_size,per_page-chunk_offset)
        page_offset = (page-1)*per_page+chunk_offset
        if not term:
            page_sql,page_params = fast_page_query(ids,sort,page_limit,page_offset)
        else:
            field = {'post_newest':'post_sort','post_oldest':'post_sort','added_newest':'added_key','added_oldest':'added_key','score':'score','resolution':'pixels'}[sort]
            page_sql = f"SELECT post_id,search_id,added_key,{field} AS sort_value FROM ({filtered_sql}) AS filtered ORDER BY sort_value {direction},post_id {direction},added_key {direction} LIMIT ? OFFSET ?"
            page_params = [*filtered_params,page_limit,page_offset]
        rows = db.execute(f"""WITH page AS ({page_sql})
          SELECT p.*,p.id AS record_id,page.search_id,
          EXISTS(SELECT 1 FROM downloads d WHERE d.post_id=p.id) AS downloaded,
          EXISTS(SELECT 1 FROM jobs j WHERE j.kind='download' AND j.status IN ('queued','running') AND j.download_post_id=p.id) AS queued
          FROM page JOIN post_records p ON p.id=page.post_id
          ORDER BY page.sort_value {direction},page.post_id {direction},page.added_key {direction}""",
          page_params).fetchall()
        result=[]
        clients={}
        for row in rows:
            if row['source_id'] not in clients:clients[row['source_id']]=get_source(db,row['source_id'])
            client=clients[row['source_id']]
            post=post_value(row)
            result.append(dict(post,record_id=row['record_id'],search_id=row['search_id'],source_id=client.id,site_name=client.name,
                               post_url=client.post_url(post['id']),downloaded=bool(row['downloaded']),queued=bool(row['queued'])))
        index_status = tag_index.status(db)
    job_value=dict(lookup_job) if lookup_job else None
    return jsonify(tag_index=index_status, posts=result,page=page,per_page=per_page,total=total,total_pages=total_pages,job=job_value,
                   page_length=min(per_page,max(0,total-(page-1)*per_page)),
                   more_on_page=chunk_offset+len(result)<min(per_page,max(0,total-(page-1)*per_page)))


@app.get('/api/saved-posts/lookup')
def saved_post_lookup():
    remote_id=request.args.get('remote_id','').strip()
    job_text=request.args.get('job_id','').strip()
    if not remote_id and not job_text:
        raise ValueError('Enter a remote post ID or download job number.')
    if len(remote_id)>128 or (job_text and (not job_text.isdigit() or len(job_text)>19)):
        raise ValueError('Enter a valid remote post ID or download job number.')
    with database() as db:
        job=None
        if job_text:
            job=db.execute("""SELECT j.id,j.kind,j.status,j.message,j.created,j.search_id,j.download_post_id,
                s.tags,s.source_id,src.name AS site_name,p.remote_id,p.post_url,p.id AS post_record_id
                FROM jobs j JOIN searches s ON s.id=j.search_id JOIN sources src ON src.id=s.source_id
                LEFT JOIN posts p ON p.id=j.download_post_id WHERE j.id=?""",(int(job_text),)).fetchone()
        if remote_id:
            rows=db.execute("""SELECT p.*,p.id AS record_id,s.id AS search_id,src.name AS site_name,src.id AS source_id,
                src.url,src.post_path,
                EXISTS(SELECT 1 FROM downloads d WHERE d.post_id=p.id) AS downloaded,
                EXISTS(SELECT 1 FROM jobs j WHERE j.kind='download' AND j.status IN ('queued','running') AND j.download_post_id=p.id) AS queued
                FROM posts p JOIN matches m ON m.post_id=p.id JOIN searches s ON s.id=m.search_id
                JOIN sources src ON src.id=p.source_id JOIN collections c ON c.id=s.collection_id
                WHERE p.remote_id=? AND s.deleted_at IS NULL AND c.deleted_at IS NULL
                GROUP BY p.id ORDER BY p.first_seen_at DESC LIMIT 100""",(remote_id,)).fetchall()
        else:
            rows=[]
        found=[]
        for row in rows:
            value=post_value(row);base=(row['url'] or '').rstrip('/');path=row['post_path'] or '/post/show/'
            found.append(dict(value,record_id=row['record_id'],search_id=row['search_id'],source_id=row['source_id'],
                site_name=row['site_name'],post_url=base+path+str(value['id']),downloaded=bool(row['downloaded']),queued=bool(row['queued'])))
    return jsonify(posts=found,job=dict(job) if job else None,job_found=bool(job))


@app.post('/api/collections/<int:gid>/posts/status')
@app.post('/api/searches/<int:sid>/posts/status')
def visible_post_status(sid=None,gid=None):
    ids = request.get_json().get('ids')
    if not isinstance(ids,list) or len(ids)>500 or any(type(i) is not int for i in ids):
        raise ValueError('Choose up to 500 visible posts.')
    with database() as db:
        searches = scoped_searches(db,sid,gid)
        if not ids:
            return jsonify(posts=[])
        search_ids = [s['id'] for s in searches]
        parent = db.execute('SELECT tags,folder_name FROM collections WHERE id=?',(searches[0]['collection_id'],)).fetchone()
        root = '/'+shared_folder(db,parent)+'/'
        legacy = '/'+search_folder(0,searches[0]['tags'])+'/'
        rows = db.execute("""SELECT p.id AS record_id,
          EXISTS(SELECT 1 FROM downloads d WHERE d.post_id=p.id) AS downloaded,
          EXISTS(SELECT 1 FROM jobs j WHERE j.kind='download'
            AND j.status IN ('queued','running') AND j.download_post_id=p.id) AS queued
          FROM posts p JOIN matches m ON m.post_id=p.id WHERE m.search_id IN ("""+
          ','.join('?' for _ in search_ids)+') AND p.id IN ('+','.join('?' for _ in ids)+')',
          (*search_ids,*ids)).fetchall()
    return jsonify(posts=[dict(record_id=r['record_id'],downloaded=bool(r['downloaded']),queued=bool(r['queued'])) for r in rows])


@app.get('/api/collections/<int:gid>/export.<fmt>')
@app.get('/api/searches/<int:sid>/export.<fmt>')
def export(fmt, sid=None, gid=None):
    if fmt not in ('json', 'csv'):
        abort(404)
    with database() as db:
        scoped_searches(db,sid,gid)
    def records():
        with database() as db:
            children = scoped_searches(db,sid,gid)
            ids = [row['id'] for row in children]
            slots = ','.join('?' for _ in ids)
            for row in db.execute(f'SELECT p.* FROM post_records p JOIN matches m ON m.post_id=p.id WHERE m.search_id IN ({slots}) ORDER BY p.sort_key DESC,p.id DESC',ids):
                client = get_source(db,row['source_id'])
                post = post_value(row)
                post['post_url'] = client.post_url(post['id'])
                post['site'] = client.name
                post['site_url'] = client.url
                yield post
    def generate():
        if fmt == 'json':
            yield '['
            first = True
            for post in records():
                yield ('' if first else ',') + json.dumps(post, ensure_ascii=False)
                first = False
            yield ']'
        else:
            stream = io.StringIO()
            writer = csv.DictWriter(stream, ['site', 'site_url'] + FIELDS, extrasaction='ignore')
            writer.writeheader()
            yield '\ufeff' + stream.getvalue()
            for post in records():
                stream.seek(0)
                stream.truncate()
                writer.writerow({k: "'"+v if isinstance(v, str) and v.startswith(('=', '+', '-', '@', '\t', '\r')) else v for k,v in post.items()})
                yield stream.getvalue()
    return Response(generate(), mimetype='application/json' if fmt == 'json' else 'text/csv',
                    headers={'Content-Disposition': f"attachment; filename={'collection-'+str(gid) if gid else 'search-'+str(sid)}.{fmt}"})


@app.post('/api/collections/<int:gid>/download')
@app.post('/api/searches/<int:sid>/download')
def queue_downloads(sid=None, gid=None):
    body = request.get_json()
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        searches = scoped_searches(db,sid,gid)
        folder = destination(read_setting(db,'default_path'),create=True)
        ids = body.get('ids')
        if ids is not None and (not isinstance(ids,list) or len(ids)>1000 or any(type(i) is not int for i in ids)):
            raise ValueError('Select up to 1,000 posts per request.')
        chosen = set(ids) if ids is not None else None
        jobs=[]
        linked=0
        seen=set()
        for search in searches:
            client = get_source(db,search['source_id'])
            if not client.enabled:
                continue
            parent = db.execute('SELECT tags,folder_name FROM collections WHERE id=?',(search['collection_id'],)).fetchone()
            params=[search['id']]
            selection=''
            if chosen is not None:
                if not chosen:
                    continue
                selection=' AND m.post_id IN ('+','.join('?' for _ in chosen)+')'
                params.extend(chosen)
            available=db.execute("""SELECT m.post_id,
              EXISTS(SELECT 1 FROM downloads d WHERE d.post_id=m.post_id) AS saved,
              EXISTS(SELECT 1 FROM jobs j WHERE j.kind='download' AND j.status IN ('queued','running')
                AND j.download_post_id=m.post_id) AS pending
              FROM matches m WHERE m.search_id=?"""+selection+' ORDER BY m.post_id DESC',params).fetchall()
            for row in available:
                pid=row['post_id']
                if pid in seen:
                    continue
                seen.add(pid)
                if row['saved']:
                    linked+=1
                    continue
                if not row['pending']:
                    jobs.append(add_job(db,'download',search['id'],dict(id=pid,folder=str(folder),collection_name=shared_folder(db,parent),source_subdir=client.subdir or urlparse(client.url).hostname),1))
    return jsonify(queued=len(jobs),linked=linked)


@app.post('/api/jobs/<int:jid>/stop')
def stop(jid):
    with database() as db:
        db.execute("UPDATE jobs SET cancel=1,status=CASE WHEN status='queued' THEN 'stopped' ELSE status END WHERE id=? AND status IN ('queued','running')", (jid,))
    return jsonify(ok=True)


@app.post('/api/jobs/stop-downloads')
def stop_downloads():
    with database() as db:
        db.execute("UPDATE jobs SET cancel=1,status=CASE WHEN status='queued' THEN 'stopped' ELSE status END WHERE kind='download' AND status IN ('queued','running')")
    return jsonify(ok=True)


@app.post('/api/jobs/clear-queue')
def clear_queue():
    with database() as db:
        count = db.execute("UPDATE jobs SET status='cleared',cancel=1,message='Removed from queue' WHERE status='queued'").rowcount
    return jsonify(cleared=count)


@app.post('/api/jobs/clear-completed')
def clear_completed():
    with database() as db:
        count = db.execute("UPDATE jobs SET status='cleared' WHERE status='done'").rowcount
    return jsonify(cleared=count)


@app.get('/api/jobs/failures')
def failures():
    page = positive(request.args.get('page', 1), 1000000)
    size = positive(request.args.get('per_page', 20), 100)
    export_all = request.args.get('all') == '1'
    with database() as db:
        total = db.execute("SELECT count(*) FROM jobs j JOIN searches s ON s.id=j.search_id WHERE j.status='error' AND s.deleted_at IS NULL").fetchone()[0]
        pages = max(1, (total + size - 1) // size)
        page = min(page, pages)
        query = """SELECT j.id,j.kind,j.search_id,j.message,j.created,s.source_id,s.tags,
          j.download_post_id AS post_record_id,
          CASE WHEN j.kind='download' THEN (SELECT remote_id FROM posts WHERE id=j.download_post_id) END AS post_id
          FROM jobs j JOIN searches s ON s.id=j.search_id WHERE j.status='error' AND s.deleted_at IS NULL
          ORDER BY j.id DESC"""
        rows = db.execute(query if export_all else query + ' LIMIT ? OFFSET ?', () if export_all else (size, (page-1)*size)).fetchall()
        result = []
        for row in rows:
            client = get_source(db, row['source_id'])
            result.append(dict(row, site_name=client.name, site_api=client.api,
                               post_url=client.post_url(row['post_id']) if row['post_id'] else None))
    return jsonify(failures=result, total=total, page=page, total_pages=pages)


@app.post('/api/posts/<int:record_id>/skip')
def skip_post(record_id):
    with database() as db:
        post = db.execute('SELECT availability_status FROM posts WHERE id=?',(record_id,)).fetchone()
        if not post:
            abort(404)
        already_skipped = post['availability_status'] in ('empty_redirect','skipped')
        if not already_skipped:
            db.execute("UPDATE posts SET availability_status='skipped' WHERE id=?",(record_id,))
        linked = db.execute('SELECT search_id,count(*) AS total FROM matches WHERE post_id=? GROUP BY search_id',(record_id,)).fetchall()
        db.execute('DELETE FROM result_order WHERE post_id=?',(record_id,))
        db.execute('DELETE FROM matches WHERE post_id=?',(record_id,))
        for row in linked:
            db.raw_execute('UPDATE search_counts SET total=GREATEST(0,total-%s) WHERE search_id=%s',(row['total'],row['search_id']))
        cleared = db.execute("""UPDATE jobs SET status='cleared',cancel=1,message='Post skipped by user'
          WHERE kind='download' AND download_post_id=? AND status IN ('queued','error','stopped')""",(record_id,)).rowcount
        stopping = db.execute("""UPDATE jobs SET cancel=1,message='Post skipped by user; stopping download'
          WHERE kind='download' AND download_post_id=? AND status='running'""",(record_id,)).rowcount
    WAKE.set()
    return jsonify(skipped=True,already_skipped=already_skipped,
                   removed_results=sum(row['total'] for row in linked),cleared_downloads=cleared,
                   stopping_downloads=stopping)


@app.post('/api/jobs/restart-stopped')
@app.post('/api/jobs/<int:jid>/restart')
@app.post('/api/jobs/retry-failed')
@app.post('/api/jobs/<int:jid>/retry')
def retry_failed(jid=None):
    wanted = 'stopped' if request.path.endswith(('/restart-stopped','/restart')) else 'error'
    retried = 0
    skipped = []
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        rows = db.execute("""SELECT j.*,s.source_id FROM jobs j JOIN searches s ON s.id=j.search_id
          WHERE j.status=? AND s.deleted_at IS NULL""" + (' AND j.id=?' if jid is not None else '') + ' ORDER BY j.id',
          (wanted,jid) if jid is not None else (wanted,)).fetchall()
        for job in rows:
            client = get_source(db,job['source_id'])
            reason = None
            if not client.enabled:
                reason = 'Enable this site before retrying.'
            elif job['kind'] == 'crawl':
                pending = db.execute("SELECT 1 FROM jobs WHERE search_id=? AND kind='crawl' AND status IN ('queued','running') LIMIT 1",(job['search_id'],)).fetchone()
                if pending:
                    reason = 'This search already has queued or running work.'
            elif job['kind'] == 'download':
                pending = db.execute("SELECT 1 FROM jobs WHERE search_id=? AND kind='download' AND status IN ('queued','running') AND download_post_id=? LIMIT 1",
                                     (job['search_id'],job['download_post_id'])).fetchone()
                if pending:
                    reason = 'This download is already queued or running.'
            else:
                reason = 'This task type cannot be retried.'
            if reason:
                skipped.append(dict(id=job['id'],reason=reason))
                continue
            # Keep the original destination, payload and search checkpoint.
            payload = db.job_payload(job)
            if job['kind']=='download':
                parent = db.execute('SELECT c.tags,c.folder_name FROM searches s JOIN collections c ON c.id=s.collection_id WHERE s.id=?',(job['search_id'],)).fetchone()
                payload['collection_name'] = shared_folder(db,parent)
                payload['source_subdir'] = payload.get('source_subdir') or client.subdir or urlparse(client.url).hostname
            db.execute("UPDATE jobs SET status='queued',cancel=0,message=? WHERE id=? AND status=?",
                       ('Restart queued' if wanted=='stopped' else 'Retry queued: previous error: '+job['message'][:420],job['id'],wanted))
            if job['kind']=='download': db.update_job_payload(job['id'],payload)
            retried += 1
    if retried:
        WAKE.set()
    return jsonify(retried=retried,skipped=skipped)


@app.post('/api/jobs/<int:jid>/clear-error')
def clear_error(jid):
    with database() as db:
        changed = db.execute("UPDATE jobs SET status='cleared' WHERE id=? AND status='error'", (jid,)).rowcount
    return jsonify(cleared=changed)


@app.post('/api/jobs/clear-errors')
def clear_errors():
    with database() as db:
        count = db.execute("UPDATE jobs SET status='cleared' WHERE status IN ('error','stopped')").rowcount
    return jsonify(cleared=count)


def report_transfer_progress(jid, message):
    with PROGRESS_GUARD:
        TRANSFER_PROGRESS[jid] = message


def clear_transfer_progress(jid):
    with PROGRESS_GUARD:
        TRANSFER_PROGRESS.pop(jid,None)


def update(jid, **values):
    with database() as db:
        db.execute('UPDATE jobs SET ' + ','.join(k+'=?' for k in values) + ' WHERE id=?', [*values.values(), jid])
    if 'status' in values or 'progress' in values:
        clear_transfer_progress(jid)


class Stopped(Exception):
    pass


def check_stop(jid):
    with database() as db:
        row = db.execute('SELECT cancel FROM jobs WHERE id=?', (jid,)).fetchone()
    if row[0]:
        raise Stopped()


def crawl(job, payload):
    for batch in range(int(job['progress']), int(payload['batches'])):
        check_stop(job['id'])
        with database() as db:
            search = db.execute('SELECT * FROM searches WHERE id=?', (job['search_id'],)).fetchone()
            client = get_source(db, search['source_id'], require_enabled=True)
            delay = client.min_delay
        respect_download_backoff(job,client)
        if search['complete']:
            return
        cursor = search['cursor']
        def paced():
            check_stop(job['id'])
            pace_search(delay, client.id)
            check_stop(job['id'])
        batch_posts = client.fetch(search['tags'], cursor=cursor, offset=search['offset'], rating=search['rating'],
                                   limit=100, delay=delay, before_request=paced)
        check_stop(job['id'])
        multi_rating = ',' in search['rating']
        ids = client.page_ids if multi_rating and client.page_ids is not None else [post['id'] for post in batch_posts]
        if len(ids) != len(set(map(str, ids))):
            raise ValueError('The site returned duplicate IDs in a batch. Checkpoint retained.')
        if client.cursor_kind == 'id' and ids:
            if any(not isinstance(pid, int) for pid in ids) or ids != sorted(ids, reverse=True) or (cursor and any(pid >= int(cursor) for pid in ids)):
                raise ValueError('The site did not honor ID ordering/cursor. Checkpoint retained; check its adapter.')
        signature = hashlib.sha256(json.dumps(client.page_ids if client.api == 'animepictures' and client.page_ids is not None else ids).encode()).hexdigest()
        if (ids or client.page_ids) and client.cursor_kind == 'page' and signature == search['page_signature']:
            raise ValueError('The site repeated the previous page. Check its pagination settings.')
        next_cursor = min(ids) if ids and client.cursor_kind == 'id' else client.next_cursor if client.cursor_kind == 'token' else None
        complete = client.exhausted if client.api == 'animepictures' or multi_rating else not ids or client.exhausted
        with database() as db:
            for post in batch_posts:
                if client.api == 'animepictures' and not post.get('tags'):
                    cached = db.execute('SELECT * FROM post_records WHERE source_id=? AND remote_id=?',(client.id,str(post['id']))).fetchone()
                    if cached:
                        post['tags'] = post_value(cached).get('tags') or ''
                record_id = db.save_post(post,client.id,post.get('_sort') or (post['id'] if isinstance(post['id'],int) else time.time()))
                if record_id is None:
                    continue
                db.execute('INSERT OR IGNORE INTO matches VALUES (?,?)', (search['id'], record_id))
            db.execute('UPDATE searches SET cursor=?,offset=offset+1,page_signature=?,complete=? WHERE id=?',
                       (next_cursor, signature, int(complete), search['id']))
            db.execute('UPDATE jobs SET progress=?,message=? WHERE id=?',
                       (batch+1, f'{client.name}: batch {batch+1}, {len(batch_posts)} posts saved', job['id']))
        if complete:
            return


def digest(path, algorithm='md5'):
    hasher = hashlib.new(algorithm)
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024*1024), b''):
            hasher.update(chunk)
    return hasher.hexdigest()


def saved_file(post_id):
    with database() as db:
        rows=db.execute('SELECT path FROM downloads WHERE post_id=? ORDER BY completed,path',(post_id,)).fetchall()
    for row in rows:
        original=Path(row['path'])
        if original.is_symlink():
            continue
        try:
            path=destination(str(original))
        except ValueError:
            continue
        if path.is_file() and path.stat().st_size>0:
            return path
        # A missing file must be recoverable by downloading again.
        with database() as db:
            db.missing_download(post_id,row['path'])
    return None


@app.get('/api/posts/<int:record_id>/file')
def open_saved_file(record_id):
    path=saved_file(record_id)
    if path is None:
        return jsonify(error='The saved file is missing. Reload the results to download it again.'),404
    response=send_file(path,conditional=True,as_attachment=path.suffix.lower() in ('.svg','.swf','.zip'))
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['Cache-Control']='private, no-cache'
    return response


def download(job, payload):
    # Internal post IDs are already unique by site + remote post ID.
    with file_lock('shared-download-'+str(payload['id'])):
        check_stop(job['id'])
        with database() as db:
            scope=db.execute('SELECT p.source_id AS post_source,s.source_id AS search_source FROM posts p JOIN searches s ON s.id=? WHERE p.id=?',(job['search_id'],payload['id'])).fetchone()
            if not scope or scope['post_source']!=scope['search_source']:
                raise ValueError('Post and search do not belong to the same site.')
        path=saved_file(payload['id'])
        if path is not None:
            with database() as db:
                db.execute("UPDATE jobs SET status='done',progress=1,message=? WHERE id=?",
                           ('Linked to saved file: '+path.name,job['id']))
            return
        return download_new_file(job,payload)


def download_new_file(job, payload):
    check_stop(job['id'])
    with database() as db:
        record = db.execute('SELECT * FROM post_records WHERE id=?', (payload['id'],)).fetchone()
        search = db.execute('SELECT * FROM searches WHERE id=?', (job['search_id'],)).fetchone()
        if not record or record['source_id'] != search['source_id']:
            raise ValueError('Post and search do not belong to the same site.')
        post = post_value(record)
        client = get_source(db, search['source_id'], require_enabled=True)
        delay = client.download_interval or float(read_setting(db, 'delay', '2'))
    rating_folder = client.rating_directory(post.get('rating'))
    subdir = safe_subdir(payload.get('source_subdir', client.subdir))
    grouped = payload.get('collection_name')
    if grouped:
        collection = destination(str(Path(payload['folder']) / safe_subdir(grouped)),create=True)
    else:
        # Jobs queued before this change retain their original destination.
        collection = destination(str(Path(payload['folder']) / subdir / search_folder(job['search_id'], search['tags'])), create=True)
    info_folder = destination(str(collection / '_download-info'))
    info_folder.mkdir(parents=True, exist_ok=True)
    folder = destination(str(collection / subdir / rating_folder if grouped else collection / rating_folder), create=True)
    algorithm = str(post.get('checksum_algorithm') or 'md5').lower()
    expected = str(post.get('checksum') or post.get('md5') or '').lower()
    lengths = {'md5':32, 'sha1':40, 'sha256':64, 'sha512':128}
    if algorithm not in lengths or (expected and (len(expected) != lengths[algorithm] or any(c not in '0123456789abcdef' for c in expected))):
        raise ValueError('The site provided an invalid file checksum.')
    ext = str(post.get('file_ext') or '').lower().lstrip('.')
    if ext not in ('jpg','jpeg','png','gif','webp','avif','bmp','tif','tiff','svg','jxl','zip','mp4','webm','swf'):
        raise ValueError('The original is unavailable or its file type is unsupported. Check the source post/account access.')
    remote = str(post['id'])
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', remote):
        raise ValueError('The post has an invalid file identifier.')
    target = folder / f"{remote}_{expected[:32] if expected else 'original'}.{ext}"
    with file_lock(target):
        try:
            transfer_file(job, payload, post, client, target, algorithm, expected, delay)
        except ValueError as exc:
            if str(exc) != 'Downloaded file failed checksum/size verification.':
                raise
            # Keep unverifiable originals in a clearly marked area while
            # preserving the collection's source and rating subdirectories.
            error_folder = destination(str(collection / 'checksum error' / subdir / rating_folder), create=True)
            error_target = error_folder / target.name
            with file_lock(error_target):
                transfer_file(job, payload, post, client, error_target, algorithm, expected, delay, verify=False)


def transfer_file(job, payload, post, client, target, algorithm, expected, delay, verify=True):
    check_stop(job['id'])
    if target.is_symlink():
        raise ValueError('Download target cannot be a symbolic link.')
    with database() as db:
        previous = db.execute('SELECT md5 FROM downloads WHERE post_id=? AND path=?', (payload['id'], str(target))).fetchone()
    reusable = target.is_file() and ((expected and digest(target, algorithm) == expected) or (not expected and previous and digest(target) == previous[0]))
    if not reusable:
        if not post.get('file_url'):
            raise ValueError('The site API did not provide an original file for this post. Open the source post to check account access; refreshing cannot restore a file the site withholds.')
        post = refresh_expiring_media(payload['id'], client, post, 'file_url')
        url = client.valid_file_url(post.get('file_url') or '')
        partial = target.with_suffix(target.suffix + '.part-' + str(job['id']))
        for attempt in range(int(job['max_attempts'])):
            check_stop(job['id'])
            respect_download_backoff(job,client)
            if attempt:
                time.sleep(max(delay, 2**attempt))
            pace_search(delay, ('download', client.id))
            try:
                respect_download_backoff(job,client)
                hasher = hashlib.new(algorithm)
                size = 0
                last_progress = 0.0
                last_stop_check = 0.0
                req = Request(url, headers=client.download_headers(url))
                partial.unlink(missing_ok=True)
                governor.before(client.url,url,api=False,interval=delay)
                with build_opener(Redirects(client.valid_file_url)).open(req, timeout=45) as response, partial.open('xb') as out:
                    governor.observe(client.url,url,response.headers,api=False)
                    if not response.headers.get('Content-Type', '').lower().startswith(('image/', 'video/', 'application/zip', 'application/octet-stream', 'application/x-shockwave-flash')):
                        raise ValueError('The site did not return an original image or supported media file.')
                    length = int(response.headers.get('Content-Length') or 0)
                    while True:
                        now = time.monotonic()
                        if now-last_stop_check >= 0.5:
                            check_stop(job['id'])
                            last_stop_check = now
                        chunk = response.read(1024*1024)
                        if not chunk:
                            break
                        out.write(chunk)
                        hasher.update(chunk)
                        size += len(chunk)
                        now = time.monotonic()
                        if now-last_progress >= 1:
                            report_transfer_progress(job['id'],f"{client.name} #{post['id']}: {size//1024:,} KB")
                            last_progress = now
                if (verify and ((expected and hasher.hexdigest() != expected) or (post.get('file_size') and size != post['file_size']) or (length and size != length))) or size == 0:
                    raise ValueError('Downloaded file failed checksum/size verification.')
                partial.replace(target)
                break
            except (Stopped,RateLimited,Deferred):
                raise
            except HTTPError as exc:
                governor.observe(client.url,url,exc.headers,exc.code,api=False)
                if exc.code == 429 or (exc.code == 503 and exc.headers.get('Retry-After')):
                    defer_rate_limited_download(job,client,exc.headers.get('Retry-After'))
                if client.api == 'sankaku' and exc.code in (401,403,404,410) and attempt < int(job['max_attempts'])-1:
                    post = refresh_expiring_media(payload['id'],client,post,'file_url',force=True)
                    url = client.valid_file_url(post.get('file_url') or '')
                if attempt == int(job['max_attempts'])-1:
                    raise
            except Exception:
                if attempt == int(job['max_attempts'])-1:
                    raise
            finally:
                partial.unlink(missing_ok=True)
    local_md5 = digest(target)
    with database() as db:
        db.record_download(payload['id'], str(target), local_md5, time.time(), sha256=digest(target,'sha256'),size=target.stat().st_size)
        if not reusable:
            db.raw_execute('UPDATE source_runtime SET backoff_until=NULL,consecutive_failures=0 WHERE source_id=%s AND backoff_until<=UTC_TIMESTAMP(6)',(client.id,))
        db.execute("UPDATE jobs SET status='done',progress=1,message=? WHERE id=?",(f'Saved {target.name}',job['id']))
    clear_transfer_progress(job['id'])


def claim_job(kind):
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        if kind == 'download':
            if db.execute("SELECT count(*) FROM jobs WHERE kind='download' AND status='running'").fetchone()[0] >= DOWNLOAD_WORKERS:
                return None
            # Probe the oldest pending item per search, rather than scanning a large
            # queue belonging to a site that already has an active transfer.
            job = db.execute("""WITH active AS (
                SELECT rs.source_id,count(*) AS count FROM jobs running JOIN searches rs ON rs.id=running.search_id
                WHERE running.kind='download' AND running.status='running' GROUP BY rs.source_id)
              SELECT j.* FROM searches s JOIN sources src ON src.id=s.source_id AND src.deleted_at IS NULL LEFT JOIN active a ON a.source_id=s.source_id
              LEFT JOIN download_dispatch d ON d.source_id=s.source_id JOIN jobs j ON j.id=(
                SELECT q.id FROM jobs q WHERE q.search_id=s.id AND q.kind='download'
                AND q.status='queued' AND q.cancel=0 AND q.available_at<=UTC_TIMESTAMP(6) ORDER BY q.priority DESC,q.id LIMIT 1)
              WHERE COALESCE(a.count,0)<?
              AND NOT EXISTS(SELECT 1 FROM download_backoff b WHERE b.source_id=s.source_id AND b.until>?)
              AND NOT EXISTS(SELECT 1 FROM jobs same WHERE same.kind='download' AND same.status='running'
                AND same.download_post_id=j.download_post_id)
              ORDER BY COALESCE(a.count,0),COALESCE(d.last_started,0),j.id LIMIT 1""",
              (DOWNLOADS_PER_SITE,time.time())).fetchone()
        else:
            job = db.execute("""SELECT j.* FROM jobs j JOIN searches s ON s.id=j.search_id JOIN sources src ON src.id=s.source_id AND src.deleted_at IS NULL WHERE j.status='queued' AND j.cancel=0 AND j.available_at<=UTC_TIMESTAMP(6) AND j.kind=?
              AND NOT EXISTS(SELECT 1 FROM download_backoff b WHERE b.source_id=s.source_id AND b.until>?)
              AND NOT EXISTS(SELECT 1 FROM jobs running WHERE running.status='running'
                AND running.kind=j.kind AND running.search_id=j.search_id)
              ORDER BY j.id LIMIT 1""", (kind,time.time())).fetchone()
        if job:
            db.raw_execute("UPDATE jobs SET status='running',started_at=UTC_TIMESTAMP(6),finished_at=NULL,attempt_count=attempt_count+1 WHERE id=%s",(job['id'],))
            db.raw_execute('INSERT INTO job_attempts(job_id,attempt_number,started_at) SELECT id,attempt_count,started_at FROM jobs WHERE id=%s',(job['id'],))
            if kind == 'download':
                db.set_dispatch(job['search_id'],time.time())
        return job


class RateLimited(Exception):
    pass


def retry_after_seconds(value, now=None):
    now = time.time() if now is None else now
    try:
        seconds = float(value)
        return max(0,seconds) if math.isfinite(seconds) and seconds <= 31536000 else 0
    except (TypeError,ValueError):
        try:
            return max(0,parsedate_to_datetime(str(value)).timestamp()-now)
        except (TypeError,ValueError,OverflowError):
            return 0


def respect_download_backoff(job, client):
    with database() as db:
        row = db.execute('SELECT until FROM download_backoff WHERE source_id=? AND until>?',(client.id,time.time())).fetchone()
        if row:
            db.execute("UPDATE jobs SET status=CASE WHEN cancel=1 THEN 'stopped' ELSE 'queued' END,message=? WHERE id=?",
                       (f'{client.name}: waiting for site rate-limit cooldown.',job['id']))
    if row:
        raise RateLimited()


def defer_rate_limited_download(job, client, retry_after):
    now = time.time()
    with database() as db:
        old = db.execute('SELECT failures,until FROM download_backoff WHERE source_id=?',(client.id,)).fetchone()
        failures = (old['failures'] if old else 0)+1
        wait = max(min(900,30*2**min(failures-1,5)),retry_after_seconds(retry_after,now))
        until = max(now+wait,old['until'] if old else 0)
        db.set_backoff(client.id,until,failures)
        db.execute("UPDATE jobs SET status=CASE WHEN cancel=1 THEN 'stopped' ELSE 'queued' END,message=? WHERE id=?",
                   (f'{client.name}: rate limited; retrying after {time.strftime("%H:%M:%S UTC",time.gmtime(until))}. Other sites continue.',job['id']))
    raise RateLimited()


def defer_site_job(job, until):
    with database() as db:
        db.execute('BEGIN IMMEDIATE')
        source = db.execute('SELECT source_id FROM searches WHERE id=?',(job['search_id'],)).fetchone()[0]
        old = db.execute('SELECT until FROM download_backoff WHERE source_id=?',(source,)).fetchone()
        until = max(until,old[0] if old else 0)
        db.set_backoff(source,until,1)
        db.execute("UPDATE jobs SET status=CASE WHEN cancel=1 THEN 'stopped' ELSE 'queued' END,message=? WHERE id=?",
                   (f'Site cooldown; automatically retrying after {time.strftime("%H:%M:%S UTC",time.gmtime(until))}. Other sites continue.',job['id']))


def worker(kind):
    while True:
        try:
            job = claim_job(kind)
            if not job:
                WAKE.wait(2)
                WAKE.clear()
                continue
            try:
                with database() as db:
                    payload = db.job_payload(job)
                (crawl if kind == 'crawl' else download)(job, payload)
                if kind != 'download':
                    check_stop(job['id'])
                    update(job['id'], status='done')
            except Deferred as exc:
                defer_site_job(job,exc.until)
            except RateLimited:
                pass
            except Stopped:
                update(job['id'], status='stopped', message='Stopped; saved work retained')
            except Exception as exc:
                update(job['id'], status='error', message=str(exc)[:500])
            finally:
                with database() as db:
                    db.raw_execute("UPDATE jobs SET finished_at=IF(status IN ('done','error','stopped'),UTC_TIMESTAMP(6),NULL) WHERE id=%s",(job['id'],))
                    db.raw_execute("UPDATE job_attempts a JOIN jobs j ON j.id=a.job_id AND j.attempt_count=a.attempt_number SET a.finished_at=UTC_TIMESTAMP(6),a.outcome=j.status,a.error_message=IF(j.status='error',j.message,NULL) WHERE j.id=%s",(job['id'],))
                clear_transfer_progress(job['id'])
        except Exception as exc:
            app.logger.exception('Queue worker error: %s', exc)
            time.sleep(3)


if os.environ.get('DISABLE_WORKER') != '1':
    threading.Thread(target=search_purge_worker,name='search-purge',daemon=True).start()
    threading.Thread(target=archived_source_cleanup_worker,name='archived-site-cleanup',daemon=True).start()
    threading.Thread(target=tag_index.run,args=(database,app.logger),name='saved-tag-index',daemon=True).start()
    for kind, count in [('crawl', SEARCH_WORKERS), ('download', DOWNLOAD_WORKERS)]:
        for index in range(count):
            threading.Thread(target=worker, args=(kind,), name=f'{kind}-worker-{index+1}', daemon=True).start()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)), threaded=True)
