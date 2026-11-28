"""Resumable background indexing and literal substring search of saved tags."""
import hashlib
import re
import threading
import time
from collections import OrderedDict

SETTING = 'saved_tag_index_v1'
# Bounds that keep every planning probe cheap regardless of library size.
POSTING_CAP = 1000000       # most posting rows the planner will count when choosing a route
TERM_LIMIT = 5000           # most distinct tags a filter word may expand to
GRAM_PROBE_LIMIT = 100001   # cap when ranking trigrams by rarity
MAX_ANCHORS = 3             # filter words used to narrow candidates
# A single-word filter is answered from the tag index alone. Multi-word filters still verify the
# literal phrase against the stored tag text, because word order and adjacency matter there.
VERIFY_SINGLE_WORD = False
_GRAM_HITS = {}
_TERM_CACHE = OrderedDict()
_TERM_CACHE_LOCK = threading.Lock()
TERM_CACHE_SIZE = 64
PLAN_LIMIT_MS = 7000


class PlanningTimeout(Exception):
    pass


class PlanningReads:
    """Share a deadline across the read-only statements used to choose a plan."""

    def __init__(self, db):
        self.db = db
        self.deadline = time.monotonic() + PLAN_LIMIT_MS / 1000

    def execute(self, sql, params=()):
        from pymysql.err import OperationalError

        remaining = int((self.deadline - time.monotonic()) * 1000)
        if remaining <= 0:
            raise PlanningTimeout
        sql = sql.replace('SELECT ', f'SELECT /*+ MAX_EXECUTION_TIME({remaining}) */ ', 1)
        try:
            return self.db.execute(sql, params)
        except OperationalError as exc:
            if exc.args[0] == 3024:
                raise PlanningTimeout from exc
            raise


def status(db):
    row=db.execute("SELECT ready,completed,total,cursor,target,updated,error FROM index_progress WHERE name='saved_tags'").fetchone()
    return dict(row) if row else dict(ready=False,completed=0,total=0,cursor=0)


def bulk(db, prefix, rows, width):
    rows = list(rows)
    for start in range(0, len(rows), 200):
        part = rows[start:start+200]
        db.execute(prefix + ','.join('('+','.join('?' for _ in range(width))+')' for row in part),
                   [value for row in part for value in row])


def grams(text):
    return {text[i:i+3] for i in range(len(text)-2)}


def index_batch(db, size=100):
    progress = status(db)
    previously_ready = progress.get('ready',False)
    if progress.get('target') is None:
        row = db.execute('SELECT COALESCE(max(id),0),count(*) FROM posts').fetchone()
        progress.update(target=row[0], total=row[1], cursor=0, completed=0, ready=False)
    # Revision comparison prevents a concurrent post update from losing its dirty marker.
    dirty = db.execute('SELECT post_id,revision FROM tag_index_dirty ORDER BY post_id LIMIT ?', (size,)).fetchall()
    old = [] if progress['ready'] else db.execute('SELECT id,tags_text FROM posts WHERE id>? AND id<=? ORDER BY id LIMIT ?',
                                                 (progress['cursor'], progress['target'], size)).fetchall()
    records = {r['id']:r['tags_text'] for r in old}
    if dirty:
        ids = [r['post_id'] for r in dirty]
        records.update({r['id']:r['tags_text'] for r in db.execute('SELECT id,tags_text FROM posts WHERE id IN ('+
                       ','.join('?' for _ in ids)+')', ids).fetchall()})
    texts = {}
    tokens = {}
    for post_id, payload in records.items():
        tags = payload or ''
        text = (' '.join(map(str,tags)) if isinstance(tags,list) else str(tags)).casefold()
        texts[post_id] = text
        for name in text.split():
            signature = hashlib.sha256(name.encode('utf-8')).hexdigest()
            tokens[signature] = name
    term_ids = {}
    signatures = list(tokens)
    for start in range(0,len(signatures),200):
        part = signatures[start:start+200]
        term_ids.update({r['signature']:r['id'] for r in db.execute('SELECT id,signature FROM tag_terms WHERE signature IN ('+
                         ','.join('?' for _ in part)+')',part).fetchall()})
    missing = {signature:name for signature,name in tokens.items() if signature not in term_ids}
    bulk(db, 'INSERT OR IGNORE INTO tag_terms(signature,name) VALUES ', missing.items(), 2)
    for start in range(0,len(signatures),200):
        part = signatures[start:start+200]
        term_ids.update({r['signature']:r['id'] for r in db.execute('SELECT id,signature FROM tag_terms WHERE signature IN ('+
                         ','.join('?' for _ in part)+')',part).fetchall()})
    bulk(db, 'INSERT OR IGNORE INTO tag_grams(gram,tag_id) VALUES ',
         ((gram,term_ids[sig]) for sig,name in missing.items() for gram in grams(name)), 2)
    changed = set(records) | {r['post_id'] for r in dirty}
    for start in range(0,len(changed),200):
        part = list(changed)[start:start+200]
        slots = ','.join('?' for _ in part)
        db.execute('DELETE FROM post_tags WHERE post_id IN ('+slots+')', part)
        db.execute('DELETE FROM post_tag_text WHERE post_id IN ('+slots+')', part)
    bulk(db, 'INSERT INTO post_tag_text(post_id,tags) VALUES ', texts.items(), 2)
    bulk(db, 'INSERT OR IGNORE INTO post_tags(post_id,tag_id) VALUES ',
         ((pid,term_ids[hashlib.sha256(name.encode('utf-8')).hexdigest()]) for pid,text in texts.items() for name in set(text.split())), 2)
    for row in dirty:
        db.execute('DELETE FROM tag_index_dirty WHERE post_id=? AND revision=?',(row['post_id'],row['revision']))
    if old:
        progress['cursor'] = old[-1]['id']
        progress['completed'] = min(progress['total'],progress['completed']+len(old))
    if not old or progress['cursor']>=progress['target']:
        progress['ready'] = True
        progress['completed'] = progress['total']
    if not changed and previously_ready and not progress.get('error'):
        return False,progress
    progress.update(updated=time.time(),error=None)
    db.save_index_progress(progress)
    return bool(changed), progress


def run(database, logger):
    while True:
        try:
            with database() as db:
                worked, progress = index_batch(db, size=20)
            time.sleep(0.5 if worked else 3)
        except Exception as exc:
            logger.exception('Saved tag index batch failed; will retry')
            try:
                with database() as db:
                    progress=status(db);progress.update(error=str(exc)[:200])
                    db.save_index_progress(progress)
            except Exception:
                pass
            time.sleep(10)


def scope_size(db, search_ids):
    return db.execute('SELECT COALESCE(sum(total),0) FROM search_counts WHERE search_id IN ('+
                      ','.join('?' for _ in search_ids)+')', search_ids).fetchone()[0]


def posting_count(db, term_ids, cap):
    """Posting rows carrying any of these tags, counted only up to cap + 1 so the probe stays cheap."""
    return db.execute('SELECT count(*) FROM (SELECT 1 FROM post_tags WHERE tag_id IN ('+
                      ','.join('?' for _ in term_ids)+') LIMIT '+str(int(cap)+1)+') AS bounded', term_ids).fetchone()[0]


def filter_query(db, search_ids, text, *, scoped_page=None):
    """Rows of result_order matching the filter, planned so no step scales with the whole library.

    Resolve each filter word to the tag ids containing it, then walk whichever is smaller: the tag's
    posting list (sequential, covering index) or the selected collection. Matching is a literal
    substring of the saved tag text, as before.
    """
    text = text.casefold()
    tokens = re.findall(r'\S+', text)
    slots = ','.join('?' for _ in search_ids)

    def scoped(join, predicate, args):
        if scoped_page is not None:
            page = scoped_page(join, predicate, args)
            if page is not None:
                return page
        return ('SELECT ro.* FROM result_order ro'+join+' WHERE ro.search_id IN ('+slots+')'+predicate,
                [*search_ids, *args])

    def direct():
        return scoped(' JOIN post_tag_text txt ON txt.post_id=ro.post_id', ' AND instr(txt.tags,?)>0', [text])

    words = sorted({t for t in tokens if len(t) >= 3}, key=lambda t: (-len(t), t))[:MAX_ANCHORS]
    if not words:
        return direct()
    mysql = getattr(db, 'is_mysql', False)
    planner = PlanningReads(db) if mysql else db
    revision = status(planner).get('updated') if mysql else None
    terms = []
    for word in words:
        ids = cached_term_ids(planner, word, revision)
        if ids is None:              # too common to narrow with; the text check still applies
            continue
        if not ids:                  # no tag contains this word, so nothing can match
            return 'SELECT * FROM result_order WHERE 1=0', []
        terms.append(ids)
    if not terms:
        return direct()
    cap = POSTING_CAP
    try:
        cap = min(scope_size(planner, search_ids) or POSTING_CAP, POSTING_CAP)
        groups = [(posting_count(planner, ids, cap), ids) for ids in terms]
    except PlanningTimeout:
        # A posting count only affects which route we take, so probe the collection instead.
        groups = [(cap+1, ids) for ids in terms]
    groups.sort(key=lambda group: group[0])
    verify = len(tokens) > 1 or VERIFY_SINGLE_WORD
    text_join = ' JOIN post_tag_text txt ON txt.post_id=ro.post_id' if verify else ''
    text_test = ' AND instr(txt.tags,?)>0' if verify else ''
    text_args = [text] if verify else []
    rarest_count, rarest_ids = groups[0]

    def has_tag(group_list):
        # A scalar probe can stop at the first matching tag and stays on the post's postings in MySQL.
        sql, args = '', []
        for number, (_, ids) in enumerate(group_list):
            alias = f'px{number}'
            index = ' FORCE INDEX (PRIMARY)' if mysql else ''
            slots = ','.join('?' for _ in ids)
            sql += (f' AND (SELECT 1 FROM post_tags {alias}{index} WHERE {alias}.post_id=ro.post_id'
                    f' AND {alias}.tag_id IN ({slots}) LIMIT 1)=1')
            args += ids
        return sql, args

    # The same predicate can also check a small, sorted slice before using either full route.
    extra_sql, extra_args = has_tag(groups)
    scoped_result = scoped(text_join, extra_sql+text_test, [*extra_args, *text_args])
    if rarest_count <= cap:
        # The rarest tag has no more posts than the collection has, so read its posting list (a covering
        # index scan) and keep the posts that are in the collection.
        extra_sql, extra_args = has_tag(groups[1:])
        sql = ('SELECT ro.* FROM (SELECT DISTINCT post_id FROM post_tags WHERE tag_id IN ('+
               ','.join('?' for _ in rarest_ids)+')) AS candidates '
               'JOIN result_order ro ON ro.post_id=candidates.post_id'+text_join+
               ' WHERE ro.search_id IN ('+slots+')'+extra_sql+text_test)
        return sql, [*rarest_ids, *search_ids, *extra_args, *text_args]
    # Every filter word is more common than the collection is large: check the collection's own posts.
    return scoped_result


def cached_term_ids(db, word, revision):
    if revision is None:
        return matching_term_ids(db, word)
    key = (revision, word)
    with _TERM_CACHE_LOCK:
        if key in _TERM_CACHE:
            _TERM_CACHE.move_to_end(key)
            return _TERM_CACHE[key]
    ids = matching_term_ids(db, word)
    with _TERM_CACHE_LOCK:
        _TERM_CACHE[key] = ids
        if len(_TERM_CACHE) > TERM_CACHE_SIZE:
            _TERM_CACHE.popitem(last=False)
    return ids


def gram_hits(db, gram):
    """Bounded, cached posting-list size used only to rank trigrams by rarity."""
    hits = _GRAM_HITS.get(gram)
    if hits is None:
        hits = db.execute('SELECT count(*) FROM (SELECT 1 FROM tag_grams WHERE gram=? LIMIT '+str(GRAM_PROBE_LIMIT)+') AS bounded',
                          (gram,)).fetchone()[0]
        if len(_GRAM_HITS) > 20000:
            _GRAM_HITS.clear()
        _GRAM_HITS[gram] = hits
    return hits


def matching_term_ids(db, anchor):
    """Ids of every tag term containing anchor, or None when there are too many to list."""
    positions = [anchor[i:i+3] for i in range(len(anchor)-2)]
    if len(positions) > 8:
        positions = [positions[i*(len(positions)-1)//7] for i in range(8)]
    distinct = sorted(set(positions))
    # If every distinct gram fits in the join, MySQL can choose their order without extra probes.
    ranked = distinct if len(distinct) <= 3 else sorted(distinct, key=lambda gram: gram_hits(db, gram))[:3]
    # Every trigram of the anchor must occur in a matching term. Probe the extra trigrams by primary key
    # so only terms containing all of them reach the substring check.
    joins = ''.join(' JOIN tag_grams g%d ON g%d.tag_id=g0.tag_id AND g%d.gram=?' % (i,i,i) for i in range(1,len(ranked)))
    sql = ('SELECT g0.tag_id FROM tag_grams g0'+joins+' JOIN tag_terms t ON t.id=g0.tag_id '
           'WHERE g0.gram=? AND instr(t.name,?)>0 LIMIT '+str(TERM_LIMIT+1))
    ids = [row[0] for row in db.execute(sql, [*ranked[1:],ranked[0],anchor]).fetchall()]
    return None if len(ids) > TERM_LIMIT else ids
