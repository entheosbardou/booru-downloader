"""External services whose public domains share an API budget."""
from urllib.parse import urlparse

GROUPS = {
    'danbooru.donmai.us': 'danbooru',
    'safebooru.donmai.us': 'danbooru',
    'shima.donmai.us': 'danbooru',
    'konachan.com': 'konachan',
    'konachan.net': 'konachan',
    'e621.net': 'e621',
    'e926.net': 'e621',
}


def group(url):
    host = (urlparse(url).hostname or '').lower()
    return GROUPS.get(host, host)


def group_sources(db, source_id):
    rows = db.raw_execute('SELECT id,url,api_url,min_delay FROM sources WHERE deleted_at IS NULL AND enabled=1').fetchall()
    current = next((row for row in rows if row[0] == source_id), None)
    if current is None:
        return [(source_id, None)]
    key = group(current[2] or current[1])
    return [(row[0], row[3]) for row in rows if group(row[2] or row[1]) == key]
