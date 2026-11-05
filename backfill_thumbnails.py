"""Run manually once to prepare cached previews for existing downloads."""
import argparse
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from mysql_store import connect
import thumbnail_cache
from thumbnail_cache import generate, valid_original


def saved_paths(after_post_id, batch_size):
    db = connect()
    try:
        with db:
            ids = [row[0] for row in db.raw_execute("""
                SELECT DISTINCT m.post_id
                FROM media_files mf
                JOIN post_media m ON m.id=mf.media_id
                JOIN file_locations l ON l.file_id=mf.file_id AND l.status='present'
                WHERE m.post_id>%s AND m.media_role='original' AND m.position=0
                ORDER BY m.post_id LIMIT %s
            """, (after_post_id, batch_size)).fetchall()]
            if not ids:
                return []
            placeholders = ','.join(['%s'] * len(ids))
            return db.raw_execute(f"""
                SELECT m.post_id, CONCAT(TRIM(TRAILING '/' FROM r.configured_path),'/',l.relative_path) AS path
                FROM media_files mf
                JOIN post_media m ON m.id=mf.media_id
                JOIN file_locations l ON l.file_id=mf.file_id AND l.status='present'
                JOIN storage_roots r ON r.id=l.storage_root_id
                WHERE m.post_id IN ({placeholders}) AND m.media_role='original' AND m.position=0
                ORDER BY m.post_id,l.downloaded_at,l.relative_path
            """, ids).fetchall()
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workers', type=int, default=max(1, int(os.environ.get('THUMBNAIL_WORKERS', '4'))), help='Parallel thumbnail jobs (default: 4)')
    parser.add_argument('--limit', type=int, default=0, help='Stop after this many posts; 0 means all')
    args = parser.parse_args()
    if args.workers < 1 or args.limit < 0:
        parser.error('workers must be positive and limit must be nonnegative')
    thumbnail_cache.GENERATION_SLOTS = threading.BoundedSemaphore(args.workers)
    cursor = scanned = created = cached = skipped = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        while True:
            rows = saved_paths(cursor, 250)
            if not rows:
                break
            grouped = {}
            for post_id, name in rows:
                grouped.setdefault(post_id, []).append(name)
            candidates = []
            for post_id, names in grouped.items():
                cursor = post_id
                scanned += 1
                path = next((Path(name) for name in names if valid_original(Path(name))), None)
                if path is None:
                    skipped += 1
                else:
                    candidates.append(path)
                if args.limit and scanned >= args.limit:
                    break
            for result, made in pool.map(generate, candidates):
                if result is None:
                    skipped += 1
                elif made:
                    created += 1
                else:
                    cached += 1
            if scanned % 100 == 0 or args.limit and scanned >= args.limit:
                print(f'scanned={scanned} created={created} cached={cached} skipped={skipped}', flush=True)
            if args.limit and scanned >= args.limit:
                break
    print(f'finished scanned={scanned} created={created} cached={cached} skipped={skipped}', flush=True)


if __name__ == '__main__':
    main()
