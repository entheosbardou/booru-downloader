"""Stable query roots and idempotent consolidation of equivalent library entries."""
import time
from urllib.parse import urlparse


def query_key(tags):
    return ' '.join(tags.split()).casefold()


def register_collection(db, parent):
    key = query_key(parent['tags'])
    db.execute('UPDATE collections SET query_key=? WHERE id=?', (key,parent['id']))
    db.raw_execute('INSERT IGNORE INTO library_folders(query_key,folder_name) VALUES(%s,%s)',(key,parent['folder_name']))
    exists=db.raw_execute('SELECT 1 FROM library_folders WHERE canonical_query=%s',(key,)).fetchone()
    if not exists:db.raw_execute('UPDATE library_folders SET is_canonical=1,canonical_query=%s WHERE folder_name=%s',(key,parent['folder_name']))


def shared_folder(db, parent):
    row = db.execute('SELECT folder_name FROM search_roots WHERE query_key=?', (query_key(parent['tags']),)).fetchone()
    return row[0] if row else parent['folder_name']


def migrate_library_groups(db):
    if db.execute("SELECT 1 FROM settings WHERE key='shared_query_collections_v1'").fetchone():
        return
    parents = db.execute('SELECT * FROM collections ORDER BY id').fetchall()
    for parent in parents:
        register_collection(db,parent)
    groups = {}
    for parent in parents:
        if parent['deleted_at'] is None:
            groups.setdefault((query_key(parent['tags']),parent['rating']),[]).append(parent)
    merged_parents = merged_children = 0
    now = time.time()
    for parents in groups.values():
        canonical = parents[0]['id']
        ids = [p['id'] for p in parents]
        rows = db.execute('SELECT * FROM searches WHERE deleted_at IS NULL AND collection_id IN ('+
                          ','.join('?' for _ in ids)+') ORDER BY id',ids).fetchall()
        sites = {}
        for row in rows:
            sites.setdefault(row['source_id'],[]).append(row)
        for children in sites.values():
            keep = children[0]
            best = max(children,key=lambda r:(r['complete'],r['offset'],r['id']))
            db.execute('UPDATE searches SET collection_id=?,cursor=?,offset=?,page_signature=?,complete=? WHERE id=?',
                       (canonical,best['cursor'],best['offset'],best['page_signature'],best['complete'],keep['id']))
            for child in children[1:]:
                db.execute('INSERT OR IGNORE INTO matches SELECT ?,post_id FROM matches WHERE search_id=?',(keep['id'],child['id']))
                db.execute('UPDATE jobs SET search_id=? WHERE search_id=?',(keep['id'],child['id']))
                db.execute('UPDATE searches SET deleted_at=? WHERE id=?',(now,child['id']))
                merged_children += 1
        for parent in parents[1:]:
            db.execute('UPDATE collections SET deleted_at=? WHERE id=?',(now,parent['id']))
            merged_parents += 1
    for row in db.execute("""SELECT j.*,c.tags,c.folder_name,so.url,so.id AS source_id FROM jobs j
        JOIN searches s ON s.id=j.search_id JOIN collections c ON c.id=s.collection_id
        JOIN sources so ON so.id=s.source_id WHERE j.kind='download' AND j.status IN ('queued','running')""").fetchall():
        payload=db.job_payload(row)
        payload['collection_name']=shared_folder(db,row)
        payload['source_subdir']=payload.get('source_subdir') or db.source_config(row['source_id']).get('download_subdir') or urlparse(row['url']).hostname
        db.update_job_payload(row['id'],payload)
    db.execute("INSERT INTO settings VALUES ('shared_query_collections_v1','1')")
