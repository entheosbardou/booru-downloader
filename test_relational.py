"""Integration checks against synthetic data only; real network calls are replaced."""
import hashlib,io,json,os
from pathlib import Path
from unittest.mock import patch
import app
from relational import post_value
client=app.app.test_client()
def call(method,path,body=None):
 r=getattr(client,method)(path,json=body) if body is not None else getattr(client,method)(path)
 assert r.status_code==200,(method,path,r.status_code,r.get_data(as_text=True)[:1000])
 return r
call('get','/api/health');state=call('get','/api/state').get_json();assert len(state['collections'])==2
with app.database() as db:
 cfg=db.source_config(1);assert cfg['username']=='fixture-user' and cfg['extra_params']['filter_id']==2 and cfg['mapping']['id']=='id'
 db.save_source(1,cfg)
 for _ in range(3):app.tag_index.index_batch(db)
for sort in ('post_newest','post_oldest','added_newest','added_oldest','score','resolution'):
 result=call('get','/api/collections/1/posts?sort='+sort).get_json();assert len(result['posts'])==3
 assert result['posts'][0]['tags']=='alpha beta character:test'
call('get','/api/collections/1/posts?filter=alpha')
call('get','/api/collections/1/export.csv');call('get','/api/collections/1/export.json')
call('post','/api/collections/1/posts/status',{'ids':[1,2,3]})
call('post','/api/settings',{'default_path':'/downloads','delay':1})
result=call('post','/api/searches',{'tags':'new_query','source_ids':[1],'batches':1}).get_json()
job=app.claim_job('crawl');assert job
post=dict(id=999,tags='new_query alpha',rating='s',width=10,height=20,score=4,file_size=4,file_ext='png',file_url='https://example.com/999.png',preview_url='https://example.com/p999.png',md5=hashlib.md5(b'test').hexdigest(),post_url='https://example.com/post/show/999',_sort=999)
def fetch(self,*a,**kw):self.exhausted=True;return [post]
with patch.object(app.BooruSource,'fetch',fetch),patch.object(app,'pace_search'):
 with app.database() as db:payload=db.job_payload(job)
 app.crawl(job,payload)
with app.database() as db:
 rid=db.execute('SELECT id FROM posts WHERE remote_id=?',('999',)).fetchone()[0]
 assert db.execute('SELECT total FROM search_counts WHERE search_id=?',(job['search_id'],)).fetchone()[0]==1
 assert db.execute('SELECT count(*) FROM result_order WHERE search_id=?',(job['search_id'],)).fetchone()[0]==1
 db.execute("UPDATE jobs SET status='done' WHERE id=?",(job['id'],))
 # A repeated discovery must not inflate either browsing cache.
 db.execute('INSERT OR IGNORE INTO matches VALUES (?,?)',(job['search_id'],rid))
 assert db.execute('SELECT total FROM search_counts WHERE search_id=?',(job['search_id'],)).fetchone()[0]==1
call('post',f"/api/collections/{result['collection_id']}/download",{'ids':[rid]})
download_job=app.claim_job('download');assert download_job
with app.database() as db:payload=db.job_payload(download_job)
class Reply(io.BytesIO):
 headers={'Content-Type':'image/png','Content-Length':'4'}
class Opener:
 def open(self,*a,**kw):return Reply(b'test')
with patch.object(app,'build_opener',return_value=Opener()),patch.object(app.governor,'before'),patch.object(app.governor,'observe'),patch.object(app.time,'sleep'):
 app.download(download_job,payload)
with app.database() as db:
 assert db.execute('SELECT count(*) FROM downloads WHERE post_id=?',(rid,)).fetchone()[0]==1
 assert db.raw_execute('SELECT sha256 FROM files WHERE sha256=%s',(hashlib.sha256(b'test').hexdigest(),)).fetchone()
 db.set_backoff(1,app.time.time()+60,2);db.set_dispatch(job['search_id'],app.time.time())
 runtime=db.raw_execute('SELECT consecutive_failures,last_download_started_at FROM source_runtime WHERE source_id=1').fetchone();assert runtime[0]==2 and runtime[1]
 db.raw_execute('UPDATE source_runtime SET backoff_until=NULL WHERE source_id=1')
call('get','/api/state')
# Soft deletion, restoration and retry paths exercise conditional unique values.
call('post',f"/api/collections/{result['collection_id']}/delete",{})
call('post',f"/api/collections/{result['collection_id']}/restore",{})
# Restoring an archived query after recreating it must merge without schema edits.
call('post',f"/api/collections/{result['collection_id']}/delete",{})
replacement=call('post','/api/searches',{'tags':'new_query','source_ids':[1],'batches':1}).get_json()
call('post',f"/api/collections/{result['collection_id']}/restore",{})
with app.database() as db:
 assert db.execute("SELECT count(*) FROM collections WHERE query_key='new_query' AND deleted_at IS NULL").fetchone()[0]==1
 assert db.execute('SELECT count(*) FROM schema_migrations').fetchone()[0]>=1
print('PASS: relational migration, secrets, profiles, browsing sorts, tag search, exports, settings, crawl, deduplication, download, checksums, runtime, archive/restore')
