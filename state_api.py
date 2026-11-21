"""Independent library and activity reads; no worker startup side effects."""
from flask import jsonify, request
from adapters import BooruSource, ADAPTERS
from library_groups import query_key
from rate_groups import group
import tag_index


def register_state_routes(app, *, database, source_client, search_display_name,
                          read_setting, root, progress_guard, transfer_progress):
    def load_activity(db,clients):
        jobs = [dict(row) for row in db.execute('''SELECT j.id,j.kind,j.search_id,j.status,j.progress,j.total,j.message,j.cancel,s.source_id,
          CASE WHEN j.kind='download' THEN (SELECT remote_id FROM posts WHERE id=j.download_post_id) END AS post_id
          FROM jobs j JOIN searches s ON s.id=j.search_id
          JOIN (
            SELECT id FROM jobs WHERE status IN ('queued','running')
            UNION ALL
            SELECT id FROM (
              SELECT recent.id FROM jobs recent JOIN searches rs ON rs.id=recent.search_id
              WHERE recent.status NOT IN ('queued','running','cleared') AND rs.deleted_at IS NULL
              ORDER BY recent.id DESC LIMIT 20
            ) AS recent_finished
          ) AS visible_jobs ON visible_jobs.id=j.id
          WHERE s.deleted_at IS NULL
          ORDER BY CASE j.status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END, j.id DESC''')]
        with progress_guard:
            for job in jobs:
                if job['status'] != 'running':
                    break
                if job['id'] in transfer_progress:
                    job['message'] = transfer_progress[job['id']]
        for job in jobs:
            client = clients[job['source_id']]
            job['site_name'] = client.name
            job['post_url'] = client.post_url(job['post_id']) if job['post_id'] else None
        revision = list(db.execute("SELECT (SELECT count(*) FROM file_locations WHERE status='present'), (SELECT max(downloaded_at) FROM file_locations WHERE status='present'), count(*), sum(CASE WHEN kind='download' AND status IN ('queued','running') THEN 1 ELSE 0 END) FROM jobs").fetchone())
        activity = {row['status']: row['count'] for row in db.execute('''SELECT status,count(*) AS count
          FROM jobs WHERE status!='cleared' AND search_id IN (SELECT id FROM searches WHERE deleted_at IS NULL) GROUP BY status''')}
        return dict(jobs=jobs,activity=activity,download_revision=revision)


    def load_sites(db):
        source_rows = list(db.execute('SELECT * FROM sources ORDER BY id'))
        configs = db.source_configs()
        peers = {}
        for row in source_rows:
            if row['enabled'] and row['deleted_at'] is None:
                cfg = configs[row['id']]
                peers.setdefault(group(cfg.get('api_url') or row['url']),[]).append((row['id'],cfg.get('min_delay')))
        public = {}
        for row in source_rows:
            sid = row['id']
            try:
                client = source_client(db,row,configs[sid],
                         peers.get(group(configs[sid].get('api_url') or row['url']),[]) if row['enabled'] and row['deleted_at'] is None else [])
                public[sid] = client.public()
            except (ValueError,OSError):
                # A malformed profile must not hide the remaining site catalog.
                public[sid] = dict(id=sid,name=row['name'],url=row['url'],api=row['api'],
                                  enabled=False,needs_setup=True,needs_credentials=False,
                                  config_error='Site settings could not be loaded. Edit this profile to repair it.')
        archived = {row['id'] for row in source_rows if row['deleted_at'] is not None}
        configured_default = read_setting(db, 'default_source_id', '')
        default_id = int(configured_default) if configured_default.isdigit() and int(configured_default) in public else next(iter(public),None)
        return dict(source=public.get(default_id),sources=[site for sid,site in public.items() if sid not in archived],
                    archived_sources=[site for sid,site in public.items() if sid in archived],
                    default_source_id=default_id,adapters=ADAPTERS)

    def load_library(db):
        # No credentials or filesystem calls on the initial saved-library path.
        source_rows = {row['id']:row for row in db.execute('SELECT id,name,enabled,deleted_at FROM sources')}
        searches = [dict(row) for row in db.execute("""WITH pending AS (
          SELECT j.search_id,count(DISTINCT j.download_post_id) AS total FROM jobs j
          WHERE j.kind='download' AND j.status IN ('queued','running')
          AND EXISTS(SELECT 1 FROM matches m WHERE m.search_id=j.search_id AND m.post_id=j.download_post_id)
          GROUP BY j.search_id)
          SELECT s.*,COALESCE(c.total,0) AS count,max(0,COALESCE(c.total,0)-COALESCE(p.total,0)) AS available
          FROM searches s LEFT JOIN search_counts c ON c.search_id=s.id LEFT JOIN pending p ON p.search_id=s.id
          WHERE s.deleted_at IS NULL ORDER BY s.id DESC""")]
        latest_crawls = {row['search_id']:row for row in db.execute("SELECT j.search_id,j.status,j.message FROM jobs j JOIN (SELECT search_id,max(id) AS id FROM jobs WHERE kind='crawl' GROUP BY search_id) latest ON latest.id=j.id")}
        busy_crawls = {row[0] for row in db.execute("SELECT DISTINCT search_id FROM jobs WHERE kind='crawl' AND status IN ('queued','running')")}
        for search in searches:
            search['display_name'] = search_display_name(search)
            search['site_name'] = source_rows[search['source_id']]['name']
            recent = latest_crawls.get(search['id'])
            search['last_crawl'] = dict(recent) if recent else None
            search['busy'] = search['id'] in busy_crawls
            search['enabled'] = bool(source_rows[search['source_id']]['enabled']) and source_rows[search['source_id']]['deleted_at'] is None
        collections = []
        folders = {row['query_key']:row['folder_name'] for row in db.execute('SELECT query_key,folder_name FROM search_roots')}
        children_by_parent = {}
        for child in searches:
            children_by_parent.setdefault(child['collection_id'],[]).append(child)
        for parent in db.execute('SELECT * FROM collections WHERE deleted_at IS NULL ORDER BY id DESC'):
            children = children_by_parent.get(parent['id'],[])
            if not children:
                continue
            collections.append(dict(parent, display_name=search_display_name(parent), folder_name=folders.get(query_key(parent['tags']),parent['folder_name']), children=[child['id'] for child in children],
                                    count=sum(child['count'] for child in children), available=sum(child['available'] for child in children),
                                    complete=all(child['complete'] for child in children), busy=any(child['busy'] for child in children),
                                    exhausted=sum(bool(child['complete']) for child in children)))
        return dict(tag_index=tag_index.status(db),searches=searches,collections=collections,settings={
                    'default_path':read_setting(db,'default_path'),'download_root':str(root),
                    'delay':float(read_setting(db,'delay','2'))})

    @app.get('/api/library')
    def library_state():
        with database() as db:
            return jsonify(load_library(db))

    @app.get('/api/sites')
    def site_state():
        with database() as db:
            return jsonify(load_sites(db))

    @app.get('/api/storage')
    def storage_state():
        return jsonify(root_available=root.is_dir())

    @app.get('/api/state')
    def state():
        # Preserve the aggregate endpoint for existing API clients.
        with database() as db:
            payload=load_library(db)
            payload.update(load_sites(db))
            payload['settings']['root_available']=root.is_dir()
            if request.args.get('activity') != '0':
                payload.update(load_activity(db,activity_clients(db)))
            return jsonify(payload)

    def activity_clients(db):
        return {row['id']:BooruSource(dict(row)) for row in db.execute('SELECT id,name,url,api,api_url,post_path FROM sources')}

    @app.get('/api/activity')
    def activity_state():
        with database() as db:
            clients = activity_clients(db)
            return jsonify(load_activity(db,clients))


