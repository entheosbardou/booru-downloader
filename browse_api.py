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


SORT_FIELDS = {'post_newest':'post_sort','post_oldest':'post_sort','added_newest':'added_key','added_oldest':'added_key','score':'score','resolution':'pixels'}

STATUS_COLUMNS = """,
              EXISTS(SELECT 1 FROM post_media saved_media JOIN media_files saved_link ON saved_link.media_id=saved_media.id JOIN file_locations saved_location ON saved_location.file_id=saved_link.file_id WHERE saved_media.post_id=p.id AND saved_location.status='present') AS downloaded,
              EXISTS(SELECT 1 FROM jobs j WHERE j.kind='download' AND j.status IN ('queued','running') AND j.download_post_id=p.id) AS queued"""


def browse_args(positive):
    """Request arguments shared by the page, count and combined endpoints."""
    per_page = positive(request.args.get('per_page', 48), 500)
    chunk_size = min(per_page,positive(request.args.get('chunk_size',per_page),500))
    chunk_offset = int(request.args.get('chunk_offset',0))
    if not 0 <= chunk_offset < per_page:
        raise ValueError('Choose a valid page chunk offset.')
    sort = request.args.get('sort','post_newest')
    if sort not in SORT_FIELDS:
        raise ValueError('Choose a supported result sort order.')
    return dict(page=positive(request.args.get('page', 1), 1000000), per_page=per_page, chunk_size=chunk_size,
                chunk_offset=chunk_offset, term=request.args.get('filter', '').strip(), sort=sort,
                direction='ASC' if sort in ('post_oldest','added_oldest') else 'DESC')


def build_filter(db, ids, term):
    """SQL for the result_order rows a saved-result filter keeps; (None, None, None) when unfiltered."""
    if not term:
        return None, None, None
    slots = ','.join('?' for _ in ids)
    if term[:3].casefold() == 'id:':
        return ('SELECT ro.* FROM result_order ro JOIN posts p ON p.id=ro.post_id WHERE ro.search_id IN ('+slots+') AND p.remote_id=?',
                [*ids,term[3:].strip()], None)
    if term[:4].casefold() == 'job:':
        number = term[4:].strip()
        if not re.fullmatch(r'[0-9]{1,19}',number):
            raise ValueError('Use job: followed by a download job number.')
        job = db.execute("""SELECT j.id,j.kind,j.status,j.message,j.created,j.search_id,j.download_post_id,
            s.tags,src.name AS site_name,p.remote_id,p.post_url FROM jobs j
            JOIN searches s ON s.id=j.search_id JOIN sources src ON src.id=s.source_id
            LEFT JOIN posts p ON p.id=j.download_post_id
            WHERE j.id=? AND j.kind='download' AND j.search_id IN ("""+slots+")",(int(number),*ids)).fetchone()
        if job and job['download_post_id']:
            return 'SELECT * FROM result_order WHERE search_id=? AND post_id=?', [job['search_id'],job['download_post_id']], job
        return 'SELECT * FROM result_order WHERE 1=0', [], job
    sql, params = tag_index.filter_query(db,ids,term)
    return sql, params, None


def fetch_page(db, *, ids, args, filtered_sql, filtered_params, limit, offset, with_status, with_total=False):
    """One page of saved posts. Status and total are optional so callers pay only for what they show."""
    field, direction = SORT_FIELDS[args['sort']], args['direction']
    if filtered_sql is None:
        page_sql, page_params = fast_page_query(ids,args['sort'],limit,offset)
        extra_select = ''
    else:
        counted = ',COUNT(*) OVER() AS total_rows' if with_total else ''
        extra_select = ',page.total_rows' if with_total else ''
        page_sql = f"SELECT post_id,search_id,added_key,{field} AS sort_value{counted} FROM ({filtered_sql}) AS filtered ORDER BY sort_value {direction},post_id {direction},added_key {direction} LIMIT ? OFFSET ?"
        page_params = [*filtered_params,limit,offset]
    return db.execute(f"""WITH page AS ({page_sql})
      SELECT p.*,p.id AS record_id,page.search_id{extra_select}{STATUS_COLUMNS if with_status else ''}
      FROM page JOIN post_records p ON p.id=page.post_id
      ORDER BY page.sort_value {direction},page.post_id {direction},page.added_key {direction}""",page_params).fetchall()


def build_posts(db, rows, get_source, with_status):
    clients = {}
    result = []
    for row in rows:
        if row['source_id'] not in clients:
            clients[row['source_id']] = get_source(db,row['source_id'])
        client = clients[row['source_id']]
        post = post_value(row)
        post['rating'] = client.default_post_rating or post['rating']
        result.append(dict(post,record_id=row['record_id'],search_id=row['search_id'],source_id=client.id,site_name=client.name,
                           post_url=client.post_url(post['id']),
                           downloaded=bool(row['downloaded']) if with_status else False,
                           queued=bool(row['queued']) if with_status else False,
                           status_pending=not with_status))
    return result


def register_browse_routes(app, *, database, scoped_searches, get_source, positive):
    @app.get('/api/collections/<int:gid>/posts/page')
    @app.get('/api/searches/<int:sid>/posts/page')
    def posts_page(sid=None, gid=None):
        """Posts only. The total, download state and index status load through their own requests."""
        args = browse_args(positive)
        limit = min(args['chunk_size'],args['per_page']-args['chunk_offset'])
        offset = (args['page']-1)*args['per_page']+args['chunk_offset']
        with database() as db:
            ids = [row['id'] for row in scoped_searches(db,sid,gid)]
            filtered_sql, filtered_params, lookup_job = build_filter(db,ids,args['term'])
            # One extra row says whether anything follows without counting the whole result set.
            rows = fetch_page(db,ids=ids,args=args,filtered_sql=filtered_sql,filtered_params=filtered_params,
                              limit=limit+1,offset=offset,with_status=False)
            more = len(rows) > limit
            posts = build_posts(db,rows[:limit],get_source,False)
        last_chunk = args['chunk_offset']+limit >= args['per_page']
        return jsonify(posts=posts,page=args['page'],per_page=args['per_page'],job=dict(lookup_job) if lookup_job else None,
                       more_on_page=more and not last_chunk,has_next=more and last_chunk)

    @app.get('/api/collections/<int:gid>/posts/count')
    @app.get('/api/searches/<int:sid>/posts/count')
    def posts_count(sid=None, gid=None):
        """Total matching posts only; slow filters cannot hold up the page itself."""
        per_page = positive(request.args.get('per_page', 48), 500)
        term = request.args.get('filter', '').strip()
        with database() as db:
            ids = [row['id'] for row in scoped_searches(db,sid,gid)]
            filtered_sql, filtered_params, _ = build_filter(db,ids,term)
            if filtered_sql is None:
                total = db.execute('SELECT COALESCE(sum(total),0) FROM search_counts WHERE search_id IN ('+
                                   ','.join('?' for _ in ids)+')',ids).fetchone()[0]
            else:
                total = db.execute('SELECT count(*) FROM ('+filtered_sql+') AS filtered',filtered_params).fetchone()[0]
        total = int(total)
        return jsonify(total=total,per_page=per_page,total_pages=max(1,(total+per_page-1)//per_page))

    @app.get('/api/collections/<int:gid>/posts')
    @app.get('/api/searches/<int:sid>/posts')
    def posts(sid=None, gid=None):
        """Combined response kept for existing clients: page, total, status and index status together."""
        args = browse_args(positive)
        page, per_page = args['page'], args['per_page']
        page_limit = min(args['chunk_size'],per_page-args['chunk_offset'])
        with database() as db:
            ids = [row['id'] for row in scoped_searches(db,sid,gid)]
            filtered_sql, filtered_params, lookup_job = build_filter(db,ids,args['term'])
            if filtered_sql is None:
                total = db.execute('SELECT COALESCE(sum(total),0) FROM search_counts WHERE search_id IN ('+
                                   ','.join('?' for _ in ids)+')',ids).fetchone()[0]
            elif args['term'][:3].casefold() in ('id:',) or args['term'][:4].casefold() == 'job:':
                total = db.execute('SELECT count(*) FROM ('+filtered_sql+') AS filtered',filtered_params).fetchone()[0]
            else:
                total = None
            rows = None
            if total is None:
                # COUNT(*) OVER() returns the total from the same evaluation, so the filter runs once.
                rows = fetch_page(db,ids=ids,args=args,filtered_sql=filtered_sql,filtered_params=filtered_params,
                                  limit=page_limit,offset=(page-1)*per_page+args['chunk_offset'],with_status=True,with_total=True)
                if rows:
                    total = int(rows[0]['total_rows'])
                else:
                    total = db.execute('SELECT count(*) FROM ('+filtered_sql+') AS filtered',filtered_params).fetchone()[0]
            total_pages = max(1, (total+per_page-1)//per_page)
            clamped = min(page,total_pages)
            if rows is None or clamped != page:
                page = clamped
                rows = fetch_page(db,ids=ids,args=args,filtered_sql=filtered_sql,filtered_params=filtered_params,
                                  limit=page_limit,offset=(page-1)*per_page+args['chunk_offset'],with_status=True)
            result = build_posts(db,rows,get_source,True)
            index_status = tag_index.status(db)
        for post in result:
            post.pop('status_pending',None)
        chunk_offset = args['chunk_offset']
        return jsonify(tag_index=index_status, posts=result,page=page,per_page=per_page,total=total,total_pages=total_pages,
                       job=dict(lookup_job) if lookup_job else None,
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


