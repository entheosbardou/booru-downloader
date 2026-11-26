"""Isolated loading regressions: no production database, workers, or network.

Route handlers run through a tiny registrar with injected request/JSON boundaries;
this checks handler behavior, not Flask HTTP integration or MySQL query plans.
"""
import importlib.util
import io
import sqlite3
import sys
import threading
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from adapters import BooruSource
from relational import Relational


def route_module(name):
    flask = types.ModuleType('flask')
    flask.jsonify = lambda *args, **kw: args[0] if args else kw
    flask.request = types.SimpleNamespace(args={},get_json=lambda: {})
    flask.abort = lambda status: (_ for _ in ()).throw(ValueError(str(status)))
    flask.Response = lambda body=None, **kw: types.SimpleNamespace(body=body,**kw)
    spec = importlib.util.spec_from_file_location('loading_test_'+name,Path(__file__).with_name(name+'.py'))
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules,{'flask':flask}):
        spec.loader.exec_module(module)
    return module


class Registrar:
    def __init__(self): self.routes={}
    def get(self,path): return self.register('GET',path)
    def post(self,path): return self.register('POST',path)
    def register(self,method,path):
        def decorator(fn): self.routes[method,path]=fn;return fn
        return decorator


class SearchIsolationTests(unittest.TestCase):
    def test_bad_site_does_not_cancel_other_searches(self):
        from search_sites import prepare_sites
        def source(db,sid):
            if sid==2:raise ValueError('credentials unavailable')
            return BooruSource(dict(id=sid,name=str(sid),api='json',url='https://example.test'))
        clients,unavailable=prepare_sites(None,[1,2,3],source,tags='test')
        self.assertEqual(set(clients)-set(unavailable),{1,3})
        self.assertEqual(unavailable,{2:'credentials unavailable'})


class CatalogTests(unittest.TestCase):
    def test_catalog_is_five_queries_and_options_remain_typed(self):
        db=Relational()
        db.raw_execute=Mock(side_effect=[
            Mock(description=[('id',),('api',)],fetchall=lambda:[(i,'json') for i in range(1,151)]),
            Mock(fetchall=lambda:[(1,'id','post.id')]),
            Mock(fetchall=lambda:[(1,'limit',23,'int'),(2,'enabled','1','bool')]),
            Mock(fetchall=lambda:[(1,'cdn.example.test')]),
            Mock(fetchall=lambda:[(1,1,'ok',100,0)]),
        ])
        result=db.source_configs()
        self.assertEqual(len(result),150)
        self.assertEqual(db.raw_execute.call_count,5)
        self.assertEqual(result[1]['extra_params']['limit'],23)
        self.assertIs(result[2]['extra_params']['enabled'],True)
        self.assertEqual(result[1]['file_hosts'],['cdn.example.test'])
        self.assertFalse(result[1]['last_test']['nsfw_available'])

    def test_one_unreadable_credential_does_not_hide_other_sites(self):
        db=Relational()
        db.raw_execute=Mock(side_effect=[Mock(description=[('id',),('api',)],fetchall=lambda:[(1,'json'),(2,'json')])]+[Mock(fetchall=lambda:[]) for _ in range(4)])
        def credentials(cfg):
            if cfg['id']==1: raise PermissionError('private filesystem detail')
        with patch('relational.load_source_credentials',side_effect=credentials):
            configs=db.source_configs()
        self.assertIn('credential_error',configs[1])
        self.assertNotIn('private filesystem detail',configs[1]['credential_error'])
        self.assertNotIn('credential_error',configs[2])


class LibraryTests(unittest.TestCase):
    def test_library_does_not_read_credentials_storage_or_activity(self):
        module=route_module('state_api'); app=Registrar();db=Mock()
        db.execute.side_effect=[
            [dict(id=1,name='Fixture',enabled=1,deleted_at=None)],
            [dict(id=7,source_id=1,collection_id=3,tags='test',rating='',count=11,available=11,complete=0)],
            [],[],[dict(query_key='test',folder_name='shared')],
            [dict(id=3,tags='test',rating='',folder_name='test')],
        ]
        @contextmanager
        def database():yield db
        root=Mock();root.is_dir.side_effect=AssertionError('NAS check blocked library')
        source_client=Mock(side_effect=AssertionError('credentials blocked library'))
        module.register_state_routes(app,database=database,source_client=source_client,
            search_display_name=lambda row:row['tags'],read_setting=lambda db,key,default='':default,
            root=root,progress_guard=threading.Lock(),transfer_progress={})
        with patch.object(module.tag_index,'status',return_value=dict(ready=True)):
            result=app.routes['GET','/api/library']()
        self.assertEqual(result['collections'][0]['count'],11)
        self.assertEqual(result['collections'][0]['folder_name'],'shared')
        db.source_configs.assert_not_called();source_client.assert_not_called();root.is_dir.assert_not_called()
        self.assertNotIn('jobs',result)

    def test_bad_site_does_not_hide_valid_profile(self):
        module=route_module('state_api');app=Registrar();db=Mock()
        rows=[dict(id=i,name=str(i),url='https://example.test',api='json',enabled=1,deleted_at=None) for i in (1,2)]
        db.execute.return_value=rows;db.source_configs.return_value={1:{},2:{}}
        @contextmanager
        def database():yield db
        def source_client(db,row,*args):
            if row['id']==1: raise ValueError('invalid profile')
            return BooruSource(row)
        module.register_state_routes(app,database=database,source_client=source_client,
            search_display_name=lambda row:row['tags'],read_setting=lambda db,key,default='':default,
            root=Mock(),progress_guard=threading.Lock(),transfer_progress={})
        result=app.routes['GET','/api/sites']()
        self.assertEqual(len(result['sources']),2)
        self.assertTrue(result['sources'][0]['config_error'])
        self.assertTrue(result['sources'][1]['enabled'])


class BrowseTests(unittest.TestCase):
    def setUp(self):
        self.module=route_module('browse_api');self.app=Registrar()
        self.db=sqlite3.connect(':memory:');self.db.row_factory=sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.executescript("""
          CREATE TABLE result_order(post_id,search_id,added_key,post_sort,score,pixels);
          CREATE INDEX result_order_post_sort ON result_order(search_id,post_sort,post_id,added_key);
          CREATE INDEX result_order_added_key ON result_order(search_id,added_key,post_id);
          CREATE INDEX result_order_score ON result_order(search_id,score,post_id,added_key);
          CREATE INDEX result_order_pixels ON result_order(search_id,pixels,post_id,added_key);
          CREATE TABLE post_records(id,source_id,remote_id,tags,sort_key);
          CREATE TABLE post_media(id,post_id);
          CREATE TABLE media_files(media_id,file_id);
          CREATE TABLE file_locations(file_id,status);
          CREATE TABLE jobs(kind,status,download_post_id);
          CREATE TABLE search_counts(search_id,total);
          INSERT INTO result_order VALUES(1,7,1,10,1,20),(2,7,2,20,2,30),(3,8,3,30,3,40),(4,99,4,40,4,50);
          INSERT INTO post_records VALUES(1,1,'100','one',10),(2,1,'200','two',20),(3,1,'300','three',30),(4,1,'400','outside',40);
          INSERT INTO post_media VALUES(12,2); INSERT INTO media_files VALUES(12,22);
          INSERT INTO file_locations VALUES(22,'present');
          INSERT INTO jobs VALUES('download','queued',3);
          INSERT INTO search_counts VALUES(7,2),(8,1),(99,1);
        """)
        @contextmanager
        def database():yield self.db
        client=BooruSource(dict(id=1,name='Fixture',api='json',url='https://example.test'))
        self.module.register_browse_routes(self.app,database=database,
            scoped_searches=lambda db,sid,gid:[dict(id=7),dict(id=8)],get_source=lambda *args:client,
            positive=lambda value,maximum:int(value))
        self.index_patch=patch.object(self.module.tag_index,'status',return_value=dict(ready=True))
        self.index_patch.start();self.addCleanup(self.index_patch.stop)

    def browse(self,**args):
        self.module.request.args=args
        return self.app.routes['GET','/api/collections/<int:gid>/posts'](gid=1)

    def test_every_sort_and_download_state_stay_scoped(self):
        for sort in ('post_newest','post_oldest','added_newest','added_oldest','score','resolution'):
            result=self.browse(sort=sort)
            self.assertEqual(result['total'],3)
            self.assertEqual({p['record_id'] for p in result['posts']},{1,2,3})
            by_id={p['record_id']:p for p in result['posts']}
            self.assertTrue(by_id[2]['downloaded']);self.assertTrue(by_id[3]['queued'])
            self.assertEqual(result['posts'][0]['record_id'],1 if sort.endswith('oldest') else 3)

    def test_chunked_page_merges_site_windows_without_skips(self):
        first=self.browse(per_page=3,chunk_size=2)
        second=self.browse(per_page=3,chunk_size=2,chunk_offset=2)
        self.assertTrue(first['more_on_page']);self.assertFalse(second['more_on_page'])
        self.assertEqual([p['record_id'] for p in first['posts']+second['posts']],[3,2,1])
        page=self.browse(per_page=2,page=2)
        self.assertEqual([p['record_id'] for p in page['posts']],[1])


class BrowseFilterTests(BrowseTests):
    def setUp(self):
        super().setUp()
        self.db.executescript("""
          CREATE TABLE post_tag_text(post_id PRIMARY KEY,tags);
          CREATE TABLE tag_terms(id INTEGER PRIMARY KEY,signature,name);
          CREATE TABLE tag_grams(gram,tag_id,PRIMARY KEY(gram,tag_id));
          CREATE TABLE post_tags(post_id,tag_id,PRIMARY KEY(post_id,tag_id));
          INSERT INTO post_tag_text VALUES(1,'blue_hair red_eyes'),(2,'blue_hair'),(3,'green_eyes'),(4,'blue_hair');
          INSERT INTO tag_terms VALUES(1,'a','blue_hair'),(2,'b','red_eyes'),(3,'c','green_eyes');
          INSERT INTO post_tags VALUES(1,1),(1,2),(2,1),(3,3),(4,1);
        """)
        for tid,name in ((1,'blue_hair'),(2,'red_eyes'),(3,'green_eyes')):
            for i in range(len(name)-2):
                self.db.execute('INSERT OR IGNORE INTO tag_grams VALUES(?,?)',(name[i:i+3],tid))
        # Exercise both the direct small-scope route and the trigram route.
        self.addCleanup(setattr,self.module.tag_index,'DIRECT_SCOPE_LIMIT',self.module.tag_index.DIRECT_SCOPE_LIMIT)

    def each_route(self):
        for limit in (10**9,-1):
            self.module.tag_index.DIRECT_SCOPE_LIMIT=limit
            yield limit

    def test_filter_counts_and_pages_once_per_route(self):
        for _ in self.each_route():
            result=self.browse(filter='blue_hair')
            self.assertEqual(result['total'],2)
            self.assertEqual([p['record_id'] for p in result['posts']],[2,1])
            second=self.browse(filter='blue_hair',per_page=1,page=2)
            self.assertEqual([p['record_id'] for p in second['posts']],[1])
            self.assertEqual(second['total_pages'],2)

    def test_filter_clamps_page_past_the_end(self):
        for _ in self.each_route():
            result=self.browse(filter='blue_hair',per_page=1,page=9)
            self.assertEqual(result['page'],2);self.assertEqual(result['total'],2)
            self.assertEqual([p['record_id'] for p in result['posts']],[1])

    def test_filter_without_matches_and_literal_phrase(self):
        for _ in self.each_route():
            empty=self.browse(filter='purple_hair')
            self.assertEqual(empty['total'],0);self.assertEqual(empty['posts'],[])
            self.assertEqual(self.browse(filter='blue_hair red_eyes')['total'],1)
            self.assertEqual(self.browse(filter='red_eyes blue_hair')['total'],0)


class PreviewTests(unittest.TestCase):
    def test_rejected_signed_preview_renews_once(self):
        module=route_module('media_api');app=Registrar();db=Mock()
        db.execute.return_value.fetchone.return_value=dict(id=1,source_id=4,remote_id='23',preview_url='https://cdn.example.test/old.jpg')
        @contextmanager
        def database():yield db
        client=BooruSource(dict(id=4,url='https://example.test',api='sankaku',api_url='https://example.test',file_hosts=['cdn.example.test']))
        def renew(record_id,client,post,field,force=False):
            return dict(post,preview_url='https://cdn.example.test/fresh.jpg') if force else post
        renew=Mock(side_effect=renew)
        module.register_media_routes(app,database=database,get_source=lambda *args:client,
            read_setting=lambda *args:'0',refresh_expiring_media=renew)
        class Reply(io.BytesIO):
            headers={'Content-Type':'image/jpeg'}
        opener=Mock();opener.open.side_effect=[HTTPError('https://cdn.example.test/old.jpg',403,'expired',{},None),Reply(b'jpeg-fixture')]
        with patch.object(module,'build_opener',return_value=opener),patch.object(module.governor,'before'),patch.object(module.governor,'observe'):
            result=app.routes['GET','/api/posts/<int:record_id>/preview'](1)
        self.assertEqual(result.body,b'jpeg-fixture')
        self.assertEqual(opener.open.call_count,2)
        self.assertTrue(renew.call_args.kwargs['force'])
        self.assertTrue(opener.open.call_args.args[0].full_url.endswith('/fresh.jpg'))


if __name__=='__main__':unittest.main()
