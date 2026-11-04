#!/usr/bin/env python3
"""Resumable yande.re metadata search crawler; Python standard library only."""
import argparse
import csv
import json
import sqlite3
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, HTTPRedirectHandler, build_opener
from source import DEFAULT_SOURCE_URL, SOURCE_URL, SOURCE_API, POSTS_PATH, api_headers, normalize_post, post_url


class SourceRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlparse(newurl)
        if target.scheme != 'https' or target.hostname != urlparse(SOURCE_URL).hostname or target.port not in (None, 443):
            raise ValueError('API redirect leaves the configured source; check SOURCE_URL.')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


urlopen = build_opener(SourceRedirects()).open

FIELDS = ['id', 'post_url', 'tags', 'rating', 'width', 'height', 'score', 'source', 'file_url', 'preview_url', 'md5']


def fetch(tags, limit, delay, before_request=None):
    url = SOURCE_URL + POSTS_PATH + '?' + urlencode({'tags': tags, 'limit': limit})
    for attempt in range(5):
        if before_request:
            before_request()
        else:
            time.sleep(delay)
        try:
            req = Request(url, headers=api_headers())
            with urlopen(req, timeout=45) as response:
                data = json.load(response)
            if not isinstance(data, list) or any(not isinstance(p, dict) or not isinstance(p.get('id'), int) for p in data):
                raise ValueError('Unexpected API response; checkpoint was not advanced.')
            return [normalize_post(post) for post in data]
        except HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 4:
                raise
            retry = exc.headers.get('Retry-After', '')
            wait = max(2 ** (attempt + 1), int(retry) if retry.isdigit() else 0)
            print(f'HTTP {exc.code}; retrying in {wait}s.', flush=True)
            time.sleep(wait)
        except (URLError, TimeoutError):
            if attempt == 4:
                raise
            time.sleep(2 ** (attempt + 1))


def export(db, folder):
    rows = [dict(json.loads(row[0]), post_url=post_url(row[1])) for row in db.execute('SELECT payload,id FROM posts ORDER BY id DESC')]
    for name in ('results.json', 'results.csv'):
        temp = folder / (name + '.tmp')
        with temp.open('w', encoding='utf-8-sig' if name.endswith('csv') else 'utf-8', newline='') as out:
            if name.endswith('json'):
                json.dump(rows, out, ensure_ascii=False, indent=2)
            else:
                writer = csv.DictWriter(out, FIELDS, extrasaction='ignore')
                writer.writeheader()
                for row in rows:
                    # Prevent spreadsheet applications from interpreting remote strings as formulas.
                    writer.writerow({k: ("'" + v if isinstance(v, str) and v.startswith(('=', '+', '-', '@', '\t', '\r')) else v) for k, v in row.items()})
        temp.replace(folder / name)
    return len(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('tags', nargs='?', help='Site tags, e.g. "landscape rating:s"; prompted if omitted')
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parent / 'search-results')
    parser.add_argument('--max-pages', type=int, default=10, help='Batches this run (default 10); run again to continue')
    parser.add_argument('--limit', type=int, default=100, help='Posts per batch, 1-100')
    parser.add_argument('--delay', type=float, default=2, help='Seconds between requests, minimum 1')
    parser.add_argument('--restart', action='store_true', help='Scan from newest again while retaining saved records')
    args = parser.parse_args()
    tags = (args.tags if args.tags is not None else input('Search tags (example: landscape rating:s): ')).strip()
    if not tags:
        parser.error('Enter at least one search tag.')
    if any(t.startswith(('order:', 'id:')) for t in tags.split()):
        parser.error('order: and id: are reserved for reliable resume; use other search tags.')
    if not 1 <= args.limit <= 100 or args.max_pages < 1 or args.delay < 1:
        parser.error('Use limit 1-100, max-pages >= 1, and delay >= 1.')
    args.output.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(args.output / 'crawl.sqlite3', timeout=1)
    db.execute('CREATE TABLE IF NOT EXISTS posts (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    state = dict(db.execute('SELECT key,value FROM state'))
    old_source = state.get('source_url', DEFAULT_SOURCE_URL if state else SOURCE_URL)
    if old_source != SOURCE_URL:
        parser.error('This output folder belongs to a different source; select another --output folder.')
    if state.get('source_api', 'moebooru' if state else SOURCE_API) != SOURCE_API:
        parser.error('This output folder belongs to a different API type; select another --output folder.')
    with db:
        db.execute('INSERT OR IGNORE INTO state VALUES (?,?)', ('source_url', SOURCE_URL))
        db.execute('INSERT OR IGNORE INTO state VALUES (?,?)', ('source_api', SOURCE_API))
    if state.get('tags', tags) != tags:
        parser.error('This folder belongs to a different search. Select a different --output folder.')
    def save(key, value):
        db.execute('INSERT OR REPLACE INTO state VALUES (?,?)', (key, str(value)))
    if args.restart:
        with db:
            db.execute("DELETE FROM state WHERE key IN ('cursor','complete')")
        state = dict(db.execute('SELECT key,value FROM state'))
    cursor = int(state['cursor']) if 'cursor' in state else None
    code = 0
    try:
        if state.get('complete') == 'yes':
            print('Search already complete. Use --restart to check for newer posts.')
        else:
            for batch in range(args.max_pages):
                query = tags + ' order:id_desc' + (f' id:<{cursor}' if cursor else '')
                posts = fetch(query, args.limit, args.delay)
                ids = [p['id'] for p in posts]
                if ids != sorted(set(ids), reverse=True) or (cursor and any(i >= cursor for i in ids)):
                    raise ValueError('API did not honor ID ordering/cursor; checkpoint was not advanced.')
                with db:
                    save('tags', tags)
                    for post in posts:
                        db.execute('INSERT OR REPLACE INTO posts VALUES (?,?)', (post['id'], json.dumps(post, ensure_ascii=False)))
                    if posts:
                        cursor = min(ids)
                        save('cursor', cursor)
                    else:
                        save('complete', 'yes')
                print(f'Batch {batch + 1}: {len(posts)} posts saved.', flush=True)
                if not posts:
                    break
    except KeyboardInterrupt:
        print('\nStopped. Run the same command to resume.')
        code = 130
    except Exception as exc:
        print(f'Crawl stopped: {exc}. Run the same command to retry.', file=sys.stderr)
        code = 1
    finally:
        count = export(db, args.output)
        db.close()
        print(f'{count} unique results exported to {args.output.resolve()}')
    return code

if __name__ == '__main__':
    sys.exit(main())
