"""Typed persistence. JSON is only a network format; never a storage format."""
import hashlib
import os
from pathlib import Path
from datetime import datetime, timezone
from rate_groups import group_sources

SOURCE_FIELDS = ('api_url','api_path','post_path','download_subdir','user_agent','min_delay',
    'download_interval','requires_credentials','auth_mode','api_key_name','response_path',
    'query_param','page_param','limit_param','page_base','notes')
SECRET_FIELDS = ('username','api_key','cookie')

def tag_names(text,api='',raw=None):
    if raw and isinstance(raw.get('tag_names'),list):
        names=[str(x.get('name_en') or x.get('name') or '') if isinstance(x,dict) else str(x) for x in raw['tag_names']]
    else:names=str(text).split(',') if api=='philomena' else str(text).split()
    return {name.strip().casefold() for name in names if name.strip()}

def tag_categories(raw):
    return {name.casefold():category for category in ('artist','character','copyright','general','meta','model') for name in str(raw.get('tag_string_'+category) or '').split()}

def utc(value):
    if value is None or value == '': return None
    if isinstance(value,dict): value=value.get('s') or value.get('seconds')
    try:
        if isinstance(value,(float,int)) or str(value).replace('.','',1).isdigit():
            value=float(value)
            if value>100000000000: value/=1000
            return datetime.fromtimestamp(value,timezone.utc).replace(tzinfo=None) if value>0 else None
        parsed=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return parsed.replace(tzinfo=timezone.utc).astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo is None else parsed.astimezone(timezone.utc).replace(tzinfo=None)
    except (ValueError,TypeError,OverflowError,OSError): return None

def post_value(row):
    """Rebuild the API response from typed columns, without decoding stored code."""
    d=dict(row)
    remote=d.get('remote_id','')
    remote=int(remote) if str(remote).isdigit() and len(str(remote))<19 else remote
    result={k:d.get(k) for k in ('post_url','rating','score','title','description','metadata_note',
        'file_url','preview_url','sample_url','file_ext','width','height','file_size','md5',
        'checksum_algorithm','checksum','mime_type')}
    result.update(id=remote,tags=d.get('tags',d.get('tags_text','')) or '',
                  source=d.get('source_text') or '',_sort=d.get('sort_key') or 0)
    for k in ('file_url','preview_url','sample_url','file_ext','md5','checksum_algorithm','checksum'):
        result[k]=result[k] or ''
    for k in ('width','height','file_size','score'):result[k]=result[k] or 0
    if d.get('duration_ms') is not None:result['duration']=d['duration_ms']/1000
    if d.get('favorite_count') is not None:result['fav_count']=d['favorite_count']
    return result

class Relational:
    def raw_execute(self,sql,params=()):
        cur=self.raw.cursor();cur.execute(sql,params);return cur

    def source_config(self,source_id):
        cur=self.raw_execute('SELECT * FROM sources WHERE id=%s',(source_id,));row=cur.fetchone()
        if not row:raise ValueError('Site profile not found.')
        d=dict(zip([x[0] for x in cur.description],row));cfg={k:v for k,v in d.items() if v is not None}
        cfg.pop('deleted_at',None)
        for table,key,value,name in [('source_field_mappings','field_name','field_path','mapping'),('source_options','option_name','option_value','extra_params')]:
            cur=self.raw_execute(f'SELECT {key},{value}'+(',value_type' if table=='source_options' else '')+f' FROM {table} WHERE source_id=%s',(source_id,))
            cfg[name]={}
            for r in cur.fetchall():
                v=r[1]
                if table=='source_options':v={'int':int,'float':float,'bool':lambda x:x=='1','str':str,'null':lambda x:None}[r[2]](v)
                cfg[name][r[0]]=v
        cfg['file_hosts']=[r[0] for r in self.raw_execute('SELECT hostname FROM source_allowed_hosts WHERE source_id=%s',(source_id,)).fetchall()]
        ref=d.get('credential_ref')
        if ref:
            for key in SECRET_FIELDS:
                path=Path(os.environ.get('BOORU_CREDENTIAL_DIR','/run/booru-credentials'))/ref/key
                if path.exists():cfg[key]=path.read_text()
        cur=self.raw_execute('SELECT ok,message,UNIX_TIMESTAMP(checked_at),nsfw_available FROM source_checks WHERE source_id=%s',(source_id,));r=cur.fetchone()
        if r:
            cfg['last_test']=dict(ok=bool(r[0]),message=r[1],checked_at=float(r[2]))
            if r[3] is not None:cfg['last_test']['nsfw_available']=bool(r[3])
        return cfg

    def save_source(self,source_id,cfg):
        fields=['name','url','api','enabled']+list(SOURCE_FIELDS)
        vals=[cfg.get(k,True if k=='enabled' else None) for k in fields]
        if source_id:
            self.raw_execute('UPDATE sources SET '+','.join('`'+k+'`=%s' for k in fields)+',deleted_at=NULL WHERE id=%s',(*vals,source_id))
        else:
            source_id=self.raw_execute('INSERT INTO sources('+','.join('`'+k+'`' for k in fields)+') VALUES('+','.join(['%s']*len(fields))+')',vals).lastrowid
        # Each version has its own directory; a rollback cannot point to half-written secrets.
        if any(cfg.get(k) for k in SECRET_FIELDS):
            import uuid
            ref=f'source-{source_id}-{uuid.uuid4().hex}'
            path=Path(os.environ.get('BOORU_CREDENTIAL_DIR','/run/booru-credentials'))/ref
            path.mkdir(mode=0o700,parents=True)
            for key in SECRET_FIELDS:
                if cfg.get(key):
                    secret=path/key;secret.write_text(str(cfg[key]));secret.chmod(0o600)
            self.raw_execute('UPDATE sources SET credential_ref=%s WHERE id=%s',(ref,source_id))
        else:self.raw_execute('UPDATE sources SET credential_ref=NULL WHERE id=%s',(source_id,))
        for table in ('source_allowed_hosts','source_field_mappings','source_options'):
            self.raw_execute('DELETE FROM '+table+' WHERE source_id=%s',(source_id,))
        hosts=cfg.get('file_hosts') or []
        if isinstance(hosts,str):hosts=hosts.split(',')
        for host in set(hosts):self.raw_execute('INSERT INTO source_allowed_hosts VALUES(%s,%s)',(source_id,host))
        for k,v in (cfg.get('mapping') or {}).items():
            if not isinstance(v,str):raise ValueError('Field mapping paths must be text.')
            self.raw_execute('INSERT INTO source_field_mappings VALUES(%s,%s,%s)',(source_id,k,v))
        for k,v in (cfg.get('extra_params') or {}).items():
            if isinstance(v,(dict,list)):raise ValueError('Extra parameters must be individual scalar values.')
            typ='null' if v is None else type(v).__name__
            self.raw_execute('INSERT INTO source_options VALUES(%s,%s,%s,%s)',(source_id,k,typ,'1' if v is True else '0' if v is False else None if v is None else str(v)))
        if cfg.get('last_test'):self.save_source_check(source_id,cfg['last_test'])
        return source_id

    def save_source_check(self,source_id,result):
        self.raw_execute('INSERT INTO source_checks VALUES(%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE ok=VALUES(ok),message=VALUES(message),checked_at=VALUES(checked_at),nsfw_available=VALUES(nsfw_available)',
            (source_id,result['ok'],result['message'],utc(result['checked_at']),result.get('nsfw_available')))

    def save_post(self,post,source_id,sort_key=None,record_id=None,indexed_posts=None):
        created=False
        if record_id:
            existing=self.raw_execute('SELECT availability_status FROM posts WHERE id=%s',(record_id,)).fetchone()
        else:
            existing=self.raw_execute('SELECT availability_status FROM posts WHERE source_id=%s AND remote_id=%s',(source_id,str(post['id']))).fetchone()
        if existing and existing[0] in ('empty_redirect','skipped'):return None
        tags=post.get('tags') or ''
        if isinstance(tags,list):tags=' '.join(str(t) for t in tags)
        fields=('source_id','remote_id','sort_key','post_url','rating','score','favorite_count','title','description','tags_text','source_text','metadata_note','remote_created_at','remote_updated_at','availability_status')
        vals=(source_id,str(post['id']),sort_key if sort_key is not None else post.get('_sort') or 0,post.get('post_url'),post.get('rating'),post.get('score'),post.get('fav_count',post.get('favorites',post.get('faves'))),post.get('title',post.get('caption')),post.get('description'),tags,post.get('source'),post.get('metadata_note'),utc(post.get('created_at',post.get('datetime'))),utc(post.get('updated_at')),'deleted' if post.get('is_deleted') else 'available')
        columns=','.join(fields);marks=','.join(['%s']*len(fields))
        if record_id:
            setters=','.join(k+("=IF(availability_status IN ('empty_redirect','skipped'),availability_status,%s)" if k=='availability_status' else '=%s') for k in fields)
            self.raw_execute('UPDATE posts SET '+setters+',last_seen_at=UTC_TIMESTAMP(6) WHERE id=%s',(*vals,record_id))
            existing=self.raw_execute('SELECT availability_status FROM posts WHERE id=%s',(record_id,)).fetchone()
            if existing and existing[0] in ('empty_redirect','skipped'):return None
        else:
            cur=self.raw_execute(f'INSERT INTO posts({columns}) VALUES({marks}) ON DUPLICATE KEY UPDATE id=LAST_INSERT_ID(id),'+','.join(k+'=VALUES('+k+')' for k in fields[2:] if k!='availability_status')+",availability_status=IF(posts.availability_status IN ('empty_redirect','skipped'),posts.availability_status,VALUES(availability_status)),last_seen_at=UTC_TIMESTAMP(6)",vals)
            record_id=cur.lastrowid
            created=cur.rowcount==1
            existing=self.raw_execute('SELECT availability_status FROM posts WHERE id=%s',(record_id,)).fetchone()
            if existing and existing[0] in ('empty_redirect','skipped'):return None
        for role,key in [('original','file_url'),('preview','preview_url'),('sample','sample_url')]:
            prefix='' if role=='original' else role+'_'
            duration=post.get('duration') or post.get('video_duration')
            values=(record_id,role,0,post.get(key) or '',post.get('mime_type') if role=='original' else None,post.get('file_ext') if role=='original' else None,post.get(prefix+'width'),post.get(prefix+'height'),post.get('file_size' if role=='original' else role+'_file_size'),int(float(duration)*1000) if duration and role=='original' else None,post.get('md5') if role=='original' else None,post.get('checksum_algorithm') if role=='original' else None,post.get('checksum') if role=='original' else None)
            self.raw_execute('INSERT INTO post_media(post_id,media_role,position,url,mime_type,extension,width,height,file_size_bytes,duration_ms,remote_md5,checksum_algorithm,checksum) VALUES('+','.join(['%s']*13)+') ON DUPLICATE KEY UPDATE '+','.join(k+'=VALUES('+k+')' for k in ('url','mime_type','extension','width','height','file_size_bytes','duration_ms','remote_md5','checksum_algorithm','checksum')),values)
        self.raw_execute('DELETE FROM post_source_links WHERE post_id=%s',(record_id,))
        links=post.get('source_urls') or post.get('sources') or str(post.get('source') or '').splitlines()
        if isinstance(links,str):links=links.splitlines()
        for pos,link in enumerate(dict.fromkeys(str(x) for x in links if x)):
            self.raw_execute('INSERT INTO post_source_links VALUES(%s,%s,%s)',(record_id,pos,link))
        parent=post.get('parent_id')
        self.raw_execute("DELETE FROM post_relations WHERE post_id=%s AND relation_type='parent'",(record_id,))
        if parent:self.raw_execute('INSERT INTO post_relations VALUES(%s,%s,%s,%s)',(record_id,source_id,str(parent),'parent'))
        api=self.raw_execute('SELECT api FROM sources WHERE id=%s',(source_id,)).fetchone()[0]
        self.index_post(record_id,source_id,tags,api,post)
        self.raw_execute('UPDATE result_order r JOIN post_media m ON m.post_id=r.post_id AND m.media_role=\'original\' AND m.position=0 JOIN posts p ON p.id=r.post_id SET r.post_sort=p.sort_key,r.score=COALESCE(p.score,0),r.pixels=COALESCE(m.width,0)*COALESCE(m.height,0) WHERE r.post_id=%s',(record_id,))
        if indexed_posts is None:
            self.update_post_index_progress(int(created), record_id)
        else:
            indexed_posts.append((record_id, created))
        return record_id

    def update_post_index_progress(self, created, last_id):
        self.raw_execute(
            "UPDATE index_progress SET total=total+%s,completed=completed+%s,"
            "`cursor`=GREATEST(`cursor`,%s),target=GREATEST(target,%s),"
            "updated=UTC_TIMESTAMP(6) WHERE name='saved_tags'",
            (created, created, last_id, last_id),
        )

    def index_post(self,post_id,source_id,text,api,raw=None):
        names=tag_names(text,api,raw);categories=tag_categories(raw or {})
        self.raw_execute('DELETE FROM post_tags WHERE post_id=%s',(post_id,))
        self.raw_execute('INSERT INTO post_tag_text VALUES(%s,%s) ON DUPLICATE KEY UPDATE tags=VALUES(tags)',(post_id,text.casefold()))
        if not names:return
        signatures={hashlib.sha256(n.encode()).hexdigest():n for n in names}
        marks=','.join(['%s']*len(signatures))
        found=dict(self.raw_execute('SELECT signature,id FROM tags WHERE signature IN ('+marks+')',tuple(signatures)).fetchall())
        missing=set(signatures)-set(found)
        cur=self.raw.cursor()
        if missing:
            cur.executemany('INSERT IGNORE INTO tags(signature,name) VALUES(%s,%s)',[(sig,signatures[sig]) for sig in sorted(missing)])
            found=dict(self.raw_execute('SELECT signature,id FROM tags WHERE signature IN ('+marks+')',tuple(signatures)).fetchall())
            grams={(name[i:i+3],found[sig]) for sig in missing for name in [signatures[sig]] for i in range(len(name)-2)}
            if grams:cur.executemany('INSERT IGNORE INTO tag_grams VALUES(%s,%s)',list(grams))
        cur.executemany('INSERT IGNORE INTO post_tags VALUES(%s,%s)',[(post_id,found[sig]) for sig in signatures])
        for sig,name in signatures.items():
            if name in categories:self.raw_execute('INSERT INTO source_tags(source_id,tag_id,category) VALUES(%s,%s,%s) ON DUPLICATE KEY UPDATE category=VALUES(category)',(source_id,found[sig],categories[name]))

    def create_job(self,kind,search_id,payload,total):
        post_id=payload.get('id') if kind=='download' else None
        job=self.raw_execute('INSERT INTO jobs(kind,search_id,total,created,message,download_post_id) VALUES(%s,%s,%s,UTC_TIMESTAMP(6),%s,%s)',(kind,search_id,total,'',post_id)).lastrowid
        if kind=='crawl':self.raw_execute('INSERT INTO crawl_jobs VALUES(%s,%s)',(job,payload['batches']))
        else:
            media=self.raw_execute("SELECT id FROM post_media WHERE post_id=%s AND media_role='original' AND position=0",(post_id,)).fetchone()
            if not media:raise ValueError('Post has no original media record.')
            root=self.storage_root(payload['folder'])
            self.raw_execute('INSERT INTO download_jobs(job_id,media_id,storage_root_id,destination_folder,collection_name,source_subdir) VALUES(%s,%s,%s,%s,%s,%s)',(job,media[0],root,payload['folder'],payload.get('collection_name'),payload.get('source_subdir')))
        return job

    def job_payload(self,job):
        if job['kind']=='crawl':return dict(batches=self.raw_execute('SELECT batches FROM crawl_jobs WHERE job_id=%s',(job['id'],)).fetchone()[0])
        r=self.raw_execute('SELECT destination_folder,collection_name,source_subdir FROM download_jobs WHERE job_id=%s',(job['id'],)).fetchone()
        return dict(id=job['download_post_id'],folder=r[0],collection_name=r[1],source_subdir=r[2])

    def update_job_payload(self,job_id,payload):
        self.raw_execute('UPDATE download_jobs SET destination_folder=%s,collection_name=%s,source_subdir=%s WHERE job_id=%s',(payload['folder'],payload.get('collection_name'),payload.get('source_subdir'),job_id))

    def storage_root(self,folder):
        # Preserve the mounted root and all existing destinations below it.
        root=os.environ.get('DOWNLOAD_ROOT','/downloads')
        if folder!=root and not str(folder).startswith(root.rstrip('/')+'/'):raise ValueError('Download destination is outside the storage root.')
        cur=self.raw_execute('INSERT INTO storage_roots(name,configured_path) VALUES(%s,%s) ON DUPLICATE KEY UPDATE id=LAST_INSERT_ID(id)',('Downloads',root))
        return cur.lastrowid

    def record_download(self,post_id,path,md5,completed,sha256=None,size=None):
        root=self.storage_root(path);base=os.environ.get('DOWNLOAD_ROOT','/downloads').rstrip('/')+'/'
        relative=str(path)[len(base):];path_hash=hashlib.sha256(relative.encode()).digest()
        r=self.raw_execute('SELECT id FROM files WHERE sha256=%s',(sha256,)).fetchone() if sha256 else None
        if not r:
            r=self.raw_execute('SELECT f.id FROM file_locations l JOIN files f ON f.id=l.file_id WHERE l.storage_root_id=%s AND l.path_hash=%s AND f.md5=%s',(root,path_hash,md5)).fetchone()
        fid=r[0] if r else self.raw_execute('INSERT INTO files(sha256,md5,size_bytes,verified_at) VALUES(%s,%s,%s,%s) ON DUPLICATE KEY UPDATE id=LAST_INSERT_ID(id)',(sha256,md5,size,utc(completed) if sha256 else None)).lastrowid
        if sha256 and r:
            self.raw_execute('UPDATE files SET sha256=%s,md5=%s,size_bytes=%s,verified_at=%s WHERE id=%s',(sha256,md5,size,utc(completed),fid))
        self.raw_execute('INSERT INTO file_locations(file_id,storage_root_id,relative_path,path_hash,downloaded_at) VALUES(%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE file_id=VALUES(file_id),status=\'present\',downloaded_at=VALUES(downloaded_at)',(fid,root,relative,path_hash,utc(completed)))
        mid=self.raw_execute("SELECT id FROM post_media WHERE post_id=%s AND media_role='original' AND position=0",(post_id,)).fetchone()[0]
        self.raw_execute('INSERT IGNORE INTO media_files VALUES(%s,%s)',(mid,fid))

    def missing_download(self,post_id,path):
        self.raw_execute("UPDATE file_locations l JOIN storage_roots r ON r.id=l.storage_root_id JOIN media_files mf ON mf.file_id=l.file_id JOIN post_media m ON m.id=mf.media_id SET l.status='missing',l.last_checked_at=UTC_TIMESTAMP(6) WHERE m.post_id=%s AND CONCAT(TRIM(TRAILING '/' FROM r.configured_path),'/',l.relative_path)=%s",(post_id,path))

    def set_backoff(self,source_id,until,failures):
        for member_id,_ in group_sources(self,source_id):
            self.raw_execute('INSERT INTO source_runtime(source_id,backoff_until,consecutive_failures) VALUES(%s,%s,%s) ON DUPLICATE KEY UPDATE backoff_until=GREATEST(COALESCE(backoff_until,VALUES(backoff_until)),VALUES(backoff_until)),consecutive_failures=GREATEST(consecutive_failures,VALUES(consecutive_failures))',(member_id,utc(until),failures))

    def set_dispatch(self,search_id,when):
        self.raw_execute('INSERT INTO source_runtime(source_id,last_download_started_at) SELECT source_id,%s FROM collection_sources WHERE id=%s ON DUPLICATE KEY UPDATE last_download_started_at=VALUES(last_download_started_at)',(utc(when),search_id))

    def save_index_progress(self,progress):
        self.raw_execute('INSERT INTO index_progress(name,ready,completed,total,`cursor`,target,updated,error) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE ready=VALUES(ready),completed=VALUES(completed),total=VALUES(total),`cursor`=VALUES(`cursor`),target=VALUES(target),updated=VALUES(updated),error=VALUES(error)',('saved_tags',progress.get('ready',False),progress.get('completed',0),progress.get('total',0),progress.get('cursor',0),progress.get('target'),utc(progress.get('updated')),progress.get('error')))
