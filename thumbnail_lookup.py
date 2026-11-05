"""Look up saved posts and thumbnail status by post ID or image pixel count."""
import argparse
from pathlib import Path

from mysql_store import connect
from thumbnail_cache import cached_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    key = parser.add_mutually_exclusive_group(required=True)
    key.add_argument('--pixels', type=int, help='Pixel count shown in a Pillow warning')
    key.add_argument('--post-id', type=int, help='Internal saved post ID')
    args = parser.parse_args()
    if (args.pixels is not None and args.pixels < 1) or (args.post_id is not None and args.post_id < 1):
        parser.error('the lookup value must be positive')
    condition = 'm.width*m.height=%s' if args.pixels is not None else 'm.post_id=%s'
    value = args.pixels if args.pixels is not None else args.post_id
    db = connect()
    try:
        rows = db.raw_execute(f"""
            SELECT m.post_id,m.width,m.height,
                   CONCAT(TRIM(TRAILING '/' FROM r.configured_path),'/',l.relative_path)
            FROM post_media m
            JOIN media_files mf ON mf.media_id=m.id
            JOIN file_locations l ON l.file_id=mf.file_id AND l.status='present'
            JOIN storage_roots r ON r.id=l.storage_root_id
            WHERE m.media_role='original' AND {condition}
            ORDER BY m.post_id,l.downloaded_at,l.relative_path
        """, (value,)).fetchall()
    finally:
        db.close()
    if not rows:
        print('No saved post matched the lookup value.')
    for post_id, width, height, name in rows:
        path = Path(name)
        present = path.is_file()
        cached = cached_path(path).is_file() if present else False
        print(f'post={post_id} dimensions={width}x{height} file_present={present} thumbnail_cached={cached} path={path}')


if __name__ == '__main__':
    main()
