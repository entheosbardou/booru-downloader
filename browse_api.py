"""Paged saved results and visible download status, independent of Activity."""
import re
from flask import jsonify, request
from relational import post_value
import tag_index


def fast_page_query(ids, sort, limit, offset):
    field = {'post_newest':'post_sort','post_oldest':'post_sort','added_newest':'added_key','added_oldest':'added_key','score':'score','resolution':'pixels'}[sort]
    direction = 'ASC' if sort in ('post_oldest','added_oldest') else 'DESC'
    order = field+' '+direction+('' if field=='added_key' else ',post_id '+direction+',added_key '+direction)
    # Each site contributes at most the requested window, then merge tiny lists.
    pieces = [f'SELECT * FROM (SELECT post_id,search_id,added_key,{field} AS sort_value FROM result_order INDEXED BY result_order_{field} WHERE search_id=? ORDER BY {order} LIMIT ?) AS site_{i}' for i,_ in enumerate(ids)]
    sql = 'SELECT * FROM ('+' UNION ALL '.join(pieces)+') AS site_windows ORDER BY sort_value '+direction+',post_id '+direction+',added_key '+direction+' LIMIT ? OFFSET ?'
    params = [value for sid in ids for value in (sid,limit+offset)]
    return sql,params+[limit,offset]


def register_browse_routes(app, *, database, scoped_searches, get_source, positive):
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
        lookup_mode='tags'
        lookup_value=term
        if term[:3].casefold()=='id:':
            lookup_mode='remote_id';lookup_value=term[3:].strip()
        elif term[:4].casefold()=='job:':
            lookup_mode='job';lookup_value=term[4:].strip()
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
            ids = [row['id'] for row in searches]
            slots = ','.join('?' for _ in ids)
            where = f"m.search_id IN ({slots})"
            params = list(ids)
            lookup_job=None
            if lookup_mode=='remote_id':
                filtered_sql='''SELECT ro.* FROM result_order ro JOIN posts p ON p.id=ro.post_id
                    WHERE ro.search_id IN ('''+slots+') AND p.remote_id=?'
                filtered_params=[*ids,lookup_value]
                total=db.execute('SELECT count(*) FROM ('+filtered_sql+') AS filtered',filtered_params).fetchone()[0]
            elif lookup_mode=='job':
                if not re.fullmatch(r'[0-9]{1,19}',lookup_value):
                    raise ValueError('Use job: followed by a download job number.')
                lookup_job=db.execute("""SELECT j.id,j.kind,j.status,j.message,j.created,j.search_id,j.download_post_id,
                    s.tags,src.name AS site_name,p.remote_id,p.post_url FROM jobs j
                    JOIN searches s ON s.id=j.search_id JOIN sources src ON src.id=s.source_id
                    LEFT JOIN posts p ON p.id=j.download_post_id
                    WHERE j.id=? AND j.kind='download' AND j.search_id IN ("""+slots+")",(int(lookup_value),*ids)).fetchone()
                if lookup_job and lookup_job['download_post_id']:
                    filtered_sql='SELECT * FROM result_order WHERE search_id=? AND post_id=?'
                    filtered_params=[lookup_job['search_id'],lookup_job['download_post_id']]
                else:
                    filtered_sql='SELECT * FROM result_order WHERE 1=0'
                    filtered_params=[]
                total=db.execute('SELECT count(*) FROM ('+filtered_sql+') AS filtered',filtered_params).fetchone()[0]
            elif term:
                filtered_sql, filtered_params = tag_index.filter_query(db,ids,term)
                total = db.execute('SELECT count(*) FROM ('+filtered_sql+') AS filtered',filtered_params).fetchone()[0]
            elif not term:
                total = db.execute('SELECT COALESCE(sum(total),0) FROM search_counts WHERE search_id IN ('+slots+')',ids).fetchone()[0]
            total_pages = max(1, (total+per_page-1)//per_page)
            page = min(page,total_pages)
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
              EXISTS(SELECT 1 FROM post_media saved_media JOIN media_files saved_link ON saved_link.media_id=saved_media.id JOIN file_locations saved_location ON saved_location.file_id=saved_link.file_id WHERE saved_media.post_id=p.id AND saved_location.status='present') AS downloaded,
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
                post['rating'] = client.default_post_rating or post['rating']
                result.append(dict(post,record_id=row['record_id'],search_id=row['search_id'],source_id=client.id,site_name=client.name,
                                   post_url=client.post_url(post['id']),downloaded=bool(row['downloaded']),queued=bool(row['queued'])))
            index_status = tag_index.status(db)
        job_value=dict(lookup_job) if lookup_job else None
        return jsonify(tag_index=index_status, posts=result,page=page,per_page=per_page,total=total,total_pages=total_pages,job=job_value,
                       page_length=min(per_page,max(0,total-(page-1)*per_page)),
                       more_on_page=chunk_offset+len(result)<min(per_page,max(0,total-(page-1)*per_page)))


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
            rows = db.execute("""SELECT p.id AS record_id,
              EXISTS(SELECT 1 FROM post_media saved_media JOIN media_files saved_link ON saved_link.media_id=saved_media.id JOIN file_locations saved_location ON saved_location.file_id=saved_link.file_id WHERE saved_media.post_id=p.id AND saved_location.status='present') AS downloaded,
              EXISTS(SELECT 1 FROM jobs j WHERE j.kind='download'
                AND j.status IN ('queued','running') AND j.download_post_id=p.id) AS queued
              FROM posts p JOIN matches m ON m.post_id=p.id WHERE m.search_id IN ("""+
              ','.join('?' for _ in search_ids)+') AND p.id IN ('+','.join('?' for _ in ids)+')',
              (*search_ids,*ids)).fetchall()
        return jsonify(posts=[dict(record_id=r['record_id'],downloaded=bool(r['downloaded']),queued=bool(r['queued'])) for r in rows])


