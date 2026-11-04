"""Bounded, restartable removal of a deleted search's database records."""
from pathlib import Path

BATCH = 25


def pending(directory, search_id):
    return Path(directory) / f'{int(search_id)}.pending'


def request(directory, search_ids):
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    for search_id in search_ids:
        pending(root, search_id).touch(exist_ok=True)


def step(db, search_id):
    """Commit one small unit; return True when the search is fully removed."""
    row = db.raw_execute('SELECT collection_id,deleted_at FROM collection_sources WHERE id=%s', (search_id,)).fetchone()
    if not row:
        return True
    if row[1] is None:
        return False
    if db.raw_execute("SELECT 1 FROM jobs WHERE search_id=%s AND status='running' LIMIT 1", (search_id,)).fetchone():
        db.raw_execute("UPDATE jobs SET cancel=1 WHERE search_id=%s AND status='running'", (search_id,))
        return False

    jobs = [r[0] for r in db.raw_execute('SELECT id FROM jobs WHERE search_id=%s ORDER BY id LIMIT %s', (search_id, BATCH)).fetchall()]
    if jobs:
        db.raw_execute('DELETE FROM jobs WHERE id IN (' + ','.join(['%s'] * len(jobs)) + ')', jobs)
        return False

    posts = [r[0] for r in db.raw_execute('SELECT post_id FROM collection_posts WHERE search_id=%s ORDER BY post_id LIMIT %s', (search_id, BATCH)).fetchall()]
    if posts:
        marks = ','.join(['%s'] * len(posts))
        args = (search_id, *posts)
        db.raw_execute(f'DELETE FROM result_order WHERE search_id=%s AND post_id IN ({marks})', args)
        db.raw_execute(f'DELETE FROM collection_posts WHERE search_id=%s AND post_id IN ({marks})', args)
        orphan = [r[0] for r in db.raw_execute(
            f'SELECT p.id FROM posts p WHERE p.id IN ({marks}) '
            'AND NOT EXISTS (SELECT 1 FROM collection_posts cp WHERE cp.post_id=p.id) '
            'AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.download_post_id=p.id) '
            'AND NOT EXISTS (SELECT 1 FROM post_media pm JOIN download_jobs dj ON dj.media_id=pm.id WHERE pm.post_id=p.id)', posts).fetchall()]
        if orphan:
            marks = ','.join(['%s'] * len(orphan))
            files = [r[0] for r in db.raw_execute(
                f'SELECT DISTINCT mf.file_id FROM post_media pm JOIN media_files mf ON mf.media_id=pm.id WHERE pm.post_id IN ({marks})', orphan).fetchall()]
            db.raw_execute(f'DELETE mf FROM media_files mf JOIN post_media pm ON pm.id=mf.media_id WHERE pm.post_id IN ({marks})', orphan)
            db.raw_execute(f'DELETE FROM posts WHERE id IN ({marks})', orphan)
            if files:
                marks = ','.join(['%s'] * len(files))
                unused = [r[0] for r in db.raw_execute(
                    f'SELECT f.id FROM files f WHERE f.id IN ({marks}) '
                    'AND NOT EXISTS (SELECT 1 FROM media_files mf WHERE mf.file_id=f.id)', files).fetchall()]
                if unused:
                    marks = ','.join(['%s'] * len(unused))
                    db.raw_execute(f'DELETE FROM file_locations WHERE file_id IN ({marks})', unused)
                    db.raw_execute(f'DELETE FROM files WHERE id IN ({marks})', unused)
        return False

    db.raw_execute('DELETE FROM result_order WHERE search_id=%s', (search_id,))
    db.raw_execute('DELETE FROM search_counts WHERE search_id=%s', (search_id,))
    db.raw_execute('DELETE FROM collection_sources WHERE id=%s', (search_id,))
    if not db.raw_execute('SELECT 1 FROM collection_sources WHERE collection_id=%s LIMIT 1', (row[0],)).fetchone():
        db.raw_execute('DELETE FROM collections WHERE id=%s', (row[0],))
    return True
