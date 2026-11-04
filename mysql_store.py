"""MySQL connection boundary for the existing application's parameterized queries."""
import os,re,ssl,queue
from datetime import datetime, timezone
from relational import Relational
from query_shapes import READ_SHAPES
_POOL=queue.LifoQueue(maxsize=24)
from decimal import Decimal
from pathlib import Path

class Row:
    def __init__(self,columns,values):
        self.columns=['offset' if c=='page_offset' else c for c in columns]
        self.values=tuple(v.replace(tzinfo=timezone.utc).timestamp() if isinstance(v,datetime) else int(v) if isinstance(v,Decimal) and v==int(v) else float(v) if isinstance(v,Decimal) else v for v in values)
    def keys(self):return self.columns
    def __len__(self):return len(self.values)
    def __iter__(self):return iter(self.values)
    def __getitem__(self,key):return self.values[key if isinstance(key,int) else self.columns.index(key)]

class Cursor:
    def __init__(self,cursor=None,rows=None):self.cursor=cursor;self.rows=rows;self.rowcount=cursor.rowcount if cursor else 0;self.lastrowid=cursor.lastrowid if cursor else None
    def fetchall(self):
        if self.rows is not None:rows,self.rows=self.rows,[];return rows
        cols=[d[0] for d in self.cursor.description or []]
        return [Row(cols,r) for r in self.cursor.fetchall()]
    def fetchone(self):
        if self.rows is not None:return self.rows.pop(0) if self.rows else None
        row=self.cursor.fetchone();return Row([d[0] for d in self.cursor.description],row) if row is not None else None
    def __iter__(self):return iter(self.fetchall())

def translate(sql):
    sql=re.sub(r'\bINSERT OR IGNORE\b','INSERT IGNORE',sql,flags=re.I)
    sql=re.sub(r'\bINSERT OR REPLACE\b','REPLACE',sql,flags=re.I)
    sql=re.sub(r'((?:INSERT IGNORE|INSERT|REPLACE) INTO (?:matches|collection_posts))\s+(?=VALUES|SELECT)',r'\1(search_id,post_id) ',sql,flags=re.I)
    sql=re.sub(r'((?:INSERT IGNORE|INSERT|REPLACE) INTO downloads)\s+(?=VALUES|SELECT)',r'\1(post_id,path,md5,completed) ',sql,flags=re.I)
    if re.search(r'\b(?:json_extract|json_object|json_array)\s*\(',sql,re.I):
        raise ValueError('JSON database operations are not supported by the relational schema.')
    sql=re.sub(r'\bAS REAL\b','AS DOUBLE',sql,flags=re.I)
    sql=re.sub(r'\bAS INTEGER\b','AS SIGNED',sql,flags=re.I)
    sql=re.sub(r'\bAS TEXT\b','AS CHAR',sql,flags=re.I)
    sql=re.sub(r'\bmax\(0,','GREATEST(0,',sql,flags=re.I)
    sql=re.sub(r'\bINDEXED BY (\w+)',r'USE INDEX (\1)',sql)
    sql=re.sub(r'\bON CONFLICT\([^)]*\) DO UPDATE SET\b','ON DUPLICATE KEY UPDATE',sql,flags=re.I)
    sql=re.sub(r'\bexcluded\.(\w+)',r'VALUES(\1)',sql,flags=re.I)
    # Identifier replacements must never modify quoted strings or JSON paths.
    tokens=re.split(r"('(?:''|\\.|[^'])*')",sql)
    for i in range(0,len(tokens),2):
        part=tokens[i];part=re.sub(r'(?<!`)\bkey\b(?!`)','`key`',part);part=re.sub(r'(?<!`)\bcursor\b(?!`)','`cursor`',part)
        part=re.sub(r'(?<!`)\buntil\b(?!`)','`until`',part);part=re.sub(r'\boffset\b','page_offset',part);part=re.sub(r'\browid\b','added_key',part)
        tokens[i]=part
    return ''.join(tokens)

TIME_COLUMNS={'created','deleted_at','completed','until','last_started','available_at','started_at','finished_at'}
INSERT_COLUMNS={'collections':['tags','rating','created','folder_name','deleted_at'],
    'searches':['tags','created','source_id','rating','collection_id'],
    'jobs':[]}

def storage_sql(sql):
    # Legacy API names are translated in application code; no database views exist.
    for old,new in [('searches','collection_sources'),('matches','collection_posts'),('tag_terms','tags')]:
        sql=re.sub(r'\b'+old+r'\b',new,sql)
    for name in ('downloads','post_records','download_backoff','download_dispatch','search_roots','collection_folders'):
        query=READ_SHAPES[name]
        def replace(m):
            alias=m[2]
            if alias and alias.upper() not in {'WHERE','JOIN','LEFT','RIGHT','INNER','ORDER','GROUP','LIMIT','ON','UNION','SET','HAVING'}:
                return m[1]+' ('+query+') '+alias
            return m[1]+' ('+query+') '+name+(' '+alias if alias else '')
        sql=re.sub(r'\b(FROM|JOIN)\s+'+name+r'\b(?:\s+(\w+))?',replace,sql,flags=re.I)
    # The Python-facing API uses Unix seconds; the actual stored values are UTC DATETIME.
    sql=re.sub(r'\b(created|deleted_at|completed|until|last_started|available_at|started_at|finished_at)\s*(=|<=|>=|<|>)\s*\?',r'\1\2FROM_UNIXTIME(?)',sql)
    sql=re.sub(r'COALESCE\((deleted_at),\?\)',r'COALESCE(\1,FROM_UNIXTIME(?))',sql)
    insert=re.search(r'INSERT(?: IGNORE)? INTO (\w+)\s*\(([^)]+)\)\s*VALUES\s*\(([^)]+)\)',sql,re.I)
    if insert:
        fields=[x.strip(' `') for x in insert[2].split(',')];values=[x.strip() for x in insert[3].split(',')]
        if len(fields)==len(values):
            values=['FROM_UNIXTIME(?)' if f in TIME_COLUMNS and v=='?' else v for f,v in zip(fields,values)]
            sql=sql[:insert.start(3)]+','.join(values)+sql[insert.end(3):]
    return sql

class Connection(Relational):
    is_mysql=True
    def __init__(self,raw):self.raw=raw;self.locked=False
    def execute(self,sql,params=()):
        original=sql
        pragma=re.fullmatch(r'PRAGMA table_info\((\w+)\)',sql.strip(),re.I)
        if pragma:
            name={'searches':'collection_sources','matches':'collection_posts','tag_terms':'tags'}.get(pragma[1],pragma[1])
            cur=self.raw.cursor();cur.execute('SELECT ORDINAL_POSITION,COLUMN_NAME,COLUMN_TYPE,IS_NULLABLE,COLUMN_DEFAULT,COLUMN_KEY FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION',(name,))
            return Cursor(rows=[Row(['cid','name','type','notnull','dflt_value','pk'],(r[0]-1,'offset' if r[1]=='page_offset' else r[1],r[2],r[3]=='NO',r[4],r[5]=='PRI')) for r in cur.fetchall()])
        if re.match(r'CREATE (?:UNIQUE )?(?:TABLE|INDEX) IF NOT EXISTS',sql,re.I):return Cursor(rows=[])
        if sql.strip().upper()=='BEGIN IMMEDIATE':
            cur=self.raw.cursor();cur.execute("SELECT GET_LOCK(CONCAT(DATABASE(),':dispatch'),30)");assert cur.fetchone()[0]==1,'Queue dispatcher lock timed out';self.locked=True;self.raw.begin();return Cursor(cur)
        sql=storage_sql(sql)
        parts=re.split(r"('(?:''|\\.|[^'])*')",translate(sql).replace('%','%%'))
        sql=''.join(part if i%2 else part.replace('?','%s') for i,part in enumerate(parts))
        cur=self.raw.cursor();cur.execute(sql,tuple(params));result=Cursor(cur)
        if re.match(r'INSERT OR IGNORE INTO matches\b',original,re.I) and cur.rowcount:
            search_id=params[0]
            self.raw_execute('INSERT INTO search_counts VALUES(%s,%s) ON DUPLICATE KEY UPDATE total=total+VALUES(total)',(search_id,cur.rowcount))
            condition=' AND m.post_id=%s' if 'VALUES' in original.upper() else ''
            self.raw_execute("INSERT IGNORE INTO result_order SELECT m.search_id,m.post_id,m.added_key,p.sort_key,COALESCE(p.score,0),COALESCE(pm.width,0)*COALESCE(pm.height,0) FROM collection_posts m JOIN posts p ON p.id=m.post_id JOIN post_media pm ON pm.post_id=p.id AND pm.media_role='original' AND pm.position=0 WHERE m.search_id=%s"+condition,(search_id,params[1]) if condition else (search_id,))
        # Conditional uniqueness is represented by ordinary values maintained here.
        if not getattr(self,'defer_active_keys',False):
            for legacy,table,column,value in (('collections','collections','active_query','IF(deleted_at IS NULL,query_key,NULL)'),('searches','collection_sources','active_collection','IF(deleted_at IS NULL,collection_id,NULL)')):
                if re.match(r'INSERT INTO '+legacy+r'\b',original,re.I):
                    self.raw_execute(f'UPDATE {table} SET {column}={value} WHERE id=%s',(cur.lastrowid,))
                elif re.match(r'UPDATE '+legacy+r'\b',original,re.I):
                    if re.search(r'WHERE\s+id\s*=\s*\?',original,re.I):
                        self.raw_execute(f'UPDATE {table} SET {column}={value} WHERE id=%s',(params[-1],))
                    elif legacy=='searches' and re.search(r'WHERE\s+collection_id\s*=\s*\?',original,re.I):
                        self.raw_execute(f'UPDATE {table} SET {column}={value} WHERE collection_id=%s',(params[-2],))
                    else:
                        raise ValueError('Collection write needs a scoped active-key update.')
        return result
    def commit(self):self.raw.commit();self.release()
    def rollback(self):self.raw.rollback();self.release()
    def release(self):
        if self.locked:
            cur=self.raw.cursor();cur.execute("SELECT RELEASE_LOCK(CONCAT(DATABASE(),':dispatch'))");self.locked=False
    def close(self):
        try:
            self.release();self.raw.rollback()
            _POOL.put_nowait(self.raw)
        except Exception:self.raw.close()
    def __enter__(self):return self
    def __exit__(self,typ,value,tb):self.rollback() if typ else self.commit()

def connect():
    import pymysql
    try:
        raw=_POOL.get_nowait();raw.ping(reconnect=False);return Connection(raw)
    except Exception:
        if 'raw' in locals():raw.close()
    password=Path(os.environ['MYSQL_PASSWORD_FILE']).read_text().strip() if os.environ.get('MYSQL_PASSWORD_FILE') else os.environ.get('MYSQL_PASSWORD','')
    ca=os.environ.get('MYSQL_SSL_CA')
    if not ca:raise RuntimeError('MYSQL_SSL_CA is required for verified MySQL TLS')
    context=ssl.create_default_context(cafile=ca)
    raw=pymysql.connect(host=os.environ['MYSQL_HOST'],port=int(os.environ.get('MYSQL_PORT','3306')),user=os.environ.get('MYSQL_USER','booru_app'),password=password,database=os.environ.get('MYSQL_DATABASE','booru'),charset='utf8mb4',autocommit=False,connect_timeout=10,read_timeout=120,write_timeout=120,ssl=context,init_command="SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
    with raw.cursor() as c:c.execute("SET time_zone='+00:00'")
    return Connection(raw)

def startup():
    db=connect()
    try:
        with db:
            if not db.execute("SELECT 1 FROM schema_migrations WHERE version='relational_v2'").fetchone():raise RuntimeError('Relational migration is incomplete; refusing to start workers')
            db.execute("UPDATE jobs SET status=CASE WHEN cancel=1 THEN 'stopped' ELSE 'queued' END,message='Recovered after restart' WHERE status='running'")
    finally:db.close()
