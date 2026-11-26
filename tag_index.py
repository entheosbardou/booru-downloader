"""Resumable background indexing and literal substring search of saved tags."""
import hashlib
import re
import time

SETTING = 'saved_tag_index_v1'
# Scopes at or below this many saved posts are filtered directly instead of through the trigram index.
DIRECT_SCOPE_LIMIT = 50000
# Bounds that keep every planning probe cheap regardless of library size.
POSTING_LIMIT = 100000
TERM_LIMIT = 5000
GRAM_PROBE_LIMIT = 100001
_GRAM_HITS = {}


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


def filter_query(db, search_ids, text):
    """Narrow via indexed tag trigrams, then preserve the original literal substring semantics."""
    text = text.casefold()
    anchor = max(re.findall(r'\S+',text),key=len)
    # The trigram route first collects every post in the whole database that has a matching tag,
    # then intersects with the selected scope. That cost does not depend on scope size, so a
    # collection of a few dozen posts can still time out. When the scope is small, checking its
    # own rows directly is far cheaper and gives identical results.
    scope = db.execute('SELECT COALESCE(sum(total),0) FROM search_counts WHERE search_id IN ('+
                       ','.join('?' for _ in search_ids)+')',search_ids).fetchone()[0]
    if len(anchor) < 3 or scope <= DIRECT_SCOPE_LIMIT:
        sql = ('SELECT ro.* FROM result_order ro JOIN post_tag_text txt ON txt.post_id=ro.post_id '
               'WHERE ro.search_id IN ('+','.join('?' for _ in search_ids)+') AND instr(txt.tags,?)>0')
        return sql, [*search_ids,text]
    term_ids = matching_term_ids(db, anchor)
    slots = ','.join('?' for _ in search_ids)
    if term_ids is None:
        return legacy_query(db, search_ids, text, anchor)
    if not term_ids:
        return 'SELECT * FROM result_order WHERE 1=0', []
    marks = ','.join('?' for _ in term_ids)
    # Bounded probe: a rare tag is cheaper to drive from its postings, a common tag from the scope.
    postings = db.execute('SELECT count(*) FROM (SELECT 1 FROM post_tags WHERE tag_id IN ('+marks+') LIMIT '+
                          str(POSTING_LIMIT+1)+') AS bounded', term_ids).fetchone()[0]
    if postings <= POSTING_LIMIT:
        sql = ('SELECT ro.* FROM (SELECT DISTINCT post_id FROM post_tags WHERE tag_id IN ('+marks+')) AS candidates '
               'JOIN result_order ro ON ro.post_id=candidates.post_id '
               'JOIN post_tag_text txt ON txt.post_id=ro.post_id WHERE ro.search_id IN ('+slots+') AND instr(txt.tags,?)>0')
        return sql, [*term_ids,*search_ids,text]
    sql = ('SELECT ro.* FROM result_order ro JOIN post_tag_text txt ON txt.post_id=ro.post_id '
           'WHERE ro.search_id IN ('+slots+') AND EXISTS (SELECT 1 FROM post_tags pt WHERE pt.post_id=ro.post_id '
           'AND pt.tag_id IN ('+marks+')) AND instr(txt.tags,?)>0')
    return sql, [*search_ids,*term_ids,text]


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
    ranked = sorted(set(positions), key=lambda gram: gram_hits(db, gram))[:3]
    # Every trigram of the anchor must occur in a matching term. Probe the extra trigrams by primary key
    # so only terms containing all of them reach the substring check.
    joins = ''.join(' JOIN tag_grams g%d ON g%d.tag_id=g0.tag_id AND g%d.gram=?' % (i,i,i) for i in range(1,len(ranked)))
    sql = ('SELECT g0.tag_id FROM tag_grams g0'+joins+' JOIN tag_terms t ON t.id=g0.tag_id '
           'WHERE g0.gram=? AND instr(t.name,?)>0 LIMIT '+str(TERM_LIMIT+1))
    ids = [row[0] for row in db.execute(sql, [*ranked[1:],ranked[0],anchor]).fetchall()]
    return None if len(ids) > TERM_LIMIT else ids


def legacy_query(db, search_ids, text, anchor):
    """Original global route; only used when an anchor matches too many distinct tags to list."""
    pieces = sorted(grams(anchor))
    rare = db.execute('SELECT gram,count(*) AS hits FROM tag_grams WHERE gram IN ('+
                      ','.join('?' for _ in pieces)+') GROUP BY gram ORDER BY hits LIMIT 1',pieces).fetchone()
    if not rare:
        return 'SELECT * FROM result_order WHERE 1=0', []
    sql = ('SELECT ro.* FROM (SELECT DISTINCT pt.post_id FROM tag_grams g JOIN tag_terms t ON t.id=g.tag_id'
           ' JOIN post_tags pt ON pt.tag_id=t.id WHERE g.gram=? AND instr(t.name,?)>0) AS candidates '
           'JOIN result_order ro ON ro.post_id=candidates.post_id '
           'JOIN post_tag_text txt ON txt.post_id=ro.post_id WHERE ro.search_id IN ('+
           ','.join('?' for _ in search_ids)+') AND instr(txt.tags,?)>0')
    return sql, [rare[0],anchor,*search_ids,text]
