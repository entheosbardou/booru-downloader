"""Bounded remote preview requests, isolated from saved-result queries."""
import re
import threading
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, build_opener
from flask import Response, abort
from adapters import Redirects
from rate_limits import governor
from relational import post_value


def register_media_routes(app, *, database, get_source, read_setting, refresh_expiring_media):
    PREVIEW_SLOTS = threading.BoundedSemaphore(2)

    @app.get('/api/posts/<int:record_id>/preview')
    def proxy_preview(record_id):
        if not PREVIEW_SLOTS.acquire(blocking=False):
            return Response(status=503,headers={'Retry-After':'1','X-Preview-Busy':'1','Cache-Control':'no-store'})
        try:
            return fetch_preview(record_id)
        finally:
            PREVIEW_SLOTS.release()


    def fetch_preview(record_id):
        with database() as db:
            record = db.execute('SELECT * FROM post_records WHERE id=?',(record_id,)).fetchone()
            if not record:
                abort(404)
            client = get_source(db,record['source_id'])
            post = post_value(record)
            media_interval = client.download_interval or float(read_setting(db, 'delay', '2'))
        post = refresh_expiring_media(record_id, client, post, 'preview_url')
        for attempt in range(2):
            try:
                return read_preview(client,post,media_interval)
            except HTTPError as exc:
                if attempt == 0 and client.api == 'sankaku' and exc.code in (401,403):
                    post = refresh_expiring_media(record_id,client,post,'preview_url',force=True)
                    continue
                raise ValueError('The source preview is currently unavailable.') from None


    def read_preview(client,post,media_interval):
        url = client.valid_file_url(client.absolute(post.get('preview_url') or ''))
        req = Request(url,headers=client.preview_headers(url))
        governor.before(client.url,url,api=False,interval=media_interval)
        try:
            with build_opener(Redirects(client.valid_file_url)).open(req,timeout=12) as response:
                governor.observe(client.url,url,response.headers,api=False)
                kind = response.headers.get('Content-Type','').split(';')[0].lower()
                untyped_rule34hentai = (not kind and client.web_post_site == 'rule34hentai.net'
                                        and re.fullmatch(r'/_thumbs/[a-f0-9]{32}/thumb\.jpg', urlparse(response.geturl()).path)
                                        and urlparse(response.geturl()).hostname == client.web_post_site)
                if not kind.startswith('image/') and not untyped_rule34hentai:
                    raise ValueError('The source did not provide an image preview.')
                body = response.read(4*1024*1024+1)
                if len(body)>4*1024*1024 or not body:
                    raise ValueError('The preview is empty or too large.')
                if untyped_rule34hentai:
                    if not body.startswith(b'\xff\xd8\xff'):
                        raise ValueError('The source did not provide a JPEG preview.')
                    kind = 'image/jpeg'
                cache = 'no-store' if 'no-store' in response.headers.get('Cache-Control','') else 'private, max-age=300'
        except HTTPError as exc:
            try:
                governor.observe(client.url,url,exc.headers,exc.code,api=False)
            finally:
                exc.close()
            raise
        except OSError:
            raise ValueError('The source preview is currently unavailable.') from None
        return Response(body,content_type=kind,headers={'Cache-Control':cache,'X-Content-Type-Options':'nosniff'})


