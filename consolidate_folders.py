"""Offline, journaled moves within the download mount; never overwrites a file."""
import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse
from adapters import BooruSource
from library_groups import query_key


def digest(path):
    h = hashlib.md5()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):
            h.update(chunk)
    return h.hexdigest()


def checked(root,path):
    path = Path(path)
    path.resolve().relative_to(root.resolve())
    if path.is_symlink():
        raise ValueError('A consolidation path is a symbolic link: '+str(path))
    return path


def consolidate(db, root, journal):
    root = Path(root).resolve()
    journal = Path(journal)
    roots = dict(db.execute('SELECT query_key,folder_name FROM search_roots'))
    aliases = {r['folder_name']:r['query_key'] for r in db.execute('SELECT * FROM collection_folders')}
    searches = {r['id']:dict(r) for r in db.execute('SELECT id,tags FROM searches')}
    clients = {r['id']:BooruSource(dict(json.loads(r['config']),id=r['id'],url=r['url'],api=r['api'],name=r['name']))
               for r in db.execute('SELECT * FROM sources')}
    history = {}
    for row in db.execute("SELECT search_id,payload,created FROM jobs WHERE kind='download' ORDER BY created"):
        p = json.loads(row['payload'])
        history.setdefault(p['id'],[]).append((row['search_id'],p,row['created']))
    rows = db.execute('SELECT d.*,p.payload,p.source_id FROM downloads d JOIN posts p ON p.id=d.post_id ORDER BY d.completed,d.path').fetchall()
    moves, updates, missing, unresolved = [],[],[],[]
    reserved, tracked, target_roots, source_dirs = {},set(),set(),set()
    backup_root = root/('_consolidation-backup-'+journal.name)
    hashes = {}
    physical_sources = {}

    def hash_file(p):
        if p not in hashes:
            hashes[p] = digest(p)
        return hashes[p]

    def plan_file(src,target):
        checked(root,src);checked(root,target)
        identity=(src.stat().st_dev,src.stat().st_ino)
        if identity in physical_sources:
            return physical_sources[identity]
        if src == target or (target.exists() and src.samefile(target)):
            physical_sources[identity]=target
            return target
        canonical = target
        other = reserved.get(target) or (target if target.exists() else None)
        if other is not None:
            if src.stat().st_size == other.stat().st_size and hash_file(src) == hash_file(other):
                target = backup_root/src.relative_to(root)
            else:
                suffix = hash_file(src)[:12]
                target = canonical.with_name(canonical.stem+'_conflict_'+suffix+canonical.suffix)
                counter = 1
                while target.exists() or target in reserved:
                    counter += 1
                    target = canonical.with_name(canonical.stem+'_conflict_'+suffix+'_'+str(counter)+canonical.suffix)
                canonical = target
        if target.exists() or target in reserved:
            raise ValueError('Consolidation destination is already occupied: '+str(target))
        reserved[target] = src
        moves.append(dict(source=str(src),destination=str(target),size=src.stat().st_size))
        source_dirs.update(src.parents)
        physical_sources[identity]=canonical
        return canonical

    for row in rows:
        virtual = PurePosixPath(row['path'].replace('\\','/'))
        try:
            rel = virtual.relative_to('/downloads')
        except ValueError:
            unresolved.append(row['path']);continue
        src = checked(root,root.joinpath(*rel.parts))
        tracked.add(src)
        if not src.is_file():
            missing.append(row['path']);continue
        key = aliases.get(rel.parts[0]) if rel.parts else None
        legacy = re.match(r'^search-(\d+)-',rel.parts[0]) if rel.parts else None
        if key is None and legacy and int(legacy[1]) in searches:
            key = query_key(searches[int(legacy[1])]['tags'])
        if key is None:
            candidates = [j for j in history.get(row['post_id'],[]) if j[0] in searches and j[2]<=row['completed']+1]
            if candidates:
                key = query_key(searches[candidates[-1][0]]['tags'])
        if key is None:
            keys = {query_key(r[0]) for r in db.execute('SELECT s.tags FROM matches m JOIN searches s ON s.id=m.search_id WHERE m.post_id=?',(row['post_id'],))}
            if len(keys)==1:
                key = keys.pop()
        if key not in roots:
            unresolved.append(row['path']);continue
        client = clients[row['source_id']]
        post = json.loads(row['payload'])
        subdir = client.subdir or urlparse(client.url).hostname
        target_root = root/roots[key]
        target_roots.add(target_root)
        target = target_root/subdir/client.rating_directory(post.get('rating'))/src.name
        canonical = plan_file(src,target)
        new_path = str(PurePosixPath('/downloads').joinpath(*canonical.relative_to(root).parts))
        if new_path != row['path']:
            updates.append(dict(post_id=row['post_id'],old=row['path'],new=new_path,md5=row['md5'],completed=row['completed']))

    # Preserve untracked files too, including interrupted-transfer files, in known duplicate roots.
    for alias,key in aliases.items():
        old = checked(root,root/alias)
        target_root = root/roots[key]
        if not old.is_dir() or old==target_root or (target_root.exists() and old.samefile(target_root)):
            continue
        for src in old.rglob('*'):
            if src.is_file() and src not in tracked:
                target_roots.add(target_root)
                plan_file(src,target_root/src.relative_to(old))
    markers = [str(p/'_download-info') for p in source_dirs if p!=root and p.is_relative_to(root) and (p/'_download-info').is_dir()]
    created_dirs = set()
    for move in moves:
        for directory in Path(move['destination']).parents:
            if directory.is_relative_to(root) and directory!=root and not directory.exists():
                created_dirs.add(directory)
    for folder in target_roots:
        if not (folder/'_download-info').exists():
            created_dirs.add(folder/'_download-info')
        if not folder.exists():
            created_dirs.add(folder)
    plan = dict(root=str(root),moves=moves,updates=updates,markers=markers,missing=missing,unresolved=unresolved,
                created_dirs=[str(p) for p in created_dirs])
    journal.mkdir(parents=True,exist_ok=True)
    with (journal/'file-plan.json').open('x',encoding='utf8') as out:
        json.dump(plan,out);out.flush();os.fsync(out.fileno())
    if unresolved:
        raise ValueError(f'{len(unresolved)} existing download paths need review; no files were moved. See the file plan.')
    for folder in target_roots:
        checked(root,folder/'_download-info').mkdir(parents=True,exist_ok=True)
    with (journal/'file-moves.jsonl').open('a',encoding='utf8') as log:
        for i,move in enumerate(moves):
            src=checked(root,move['source']);dst=checked(root,move['destination'])
            if dst.exists():
                raise ValueError('A destination appeared after planning: '+str(dst))
            dst.parent.mkdir(parents=True,exist_ok=True)
            src.rename(dst)
            if dst.stat().st_size!=move['size']:
                raise ValueError('File size changed during rename.')
            log.write(json.dumps(move)+'\n');log.flush()
            if (i+1)%250==0:
                print('Consolidated files',i+1,'of',len(moves),flush=True)
    with db:
        for row in updates:
            db.execute('DELETE FROM downloads WHERE post_id=? AND path=?',(row['post_id'],row['old']))
            db.execute('INSERT INTO downloads VALUES (?,?,?,?) ON CONFLICT(post_id,path) DO UPDATE SET completed=max(completed,excluded.completed)',
                       (row['post_id'],row['new'],row['md5'],row['completed']))
    for marker in markers:
        if Path(marker).parent not in target_roots:
            try:
                checked(root,marker).rmdir()
            except OSError:
                pass
    for directory in sorted(source_dirs,key=lambda p:len(p.parts),reverse=True):
        if directory==root or not directory.is_relative_to(root) or directory in target_roots:
            continue
        try:
            checked(root,directory).rmdir()
        except OSError:
            pass
    for folder in target_roots:
        (folder/'_download-info').mkdir(parents=True,exist_ok=True)
    summary=dict(moved=len(moves),paths_updated=len(updates),missing=len(missing),unresolved=len(unresolved),
                 duplicate_copies=sum(Path(m['destination']).is_relative_to(backup_root) for m in moves),backup=str(backup_root))
    (journal/'consolidation-result.json').write_text(json.dumps(summary),encoding='utf8')
    return summary


def undo_moves(root,journal):
    root=Path(root).resolve();planfile=Path(journal)/'file-plan.json'
    if not planfile.exists():
        return
    plan=json.loads(planfile.read_text(encoding='utf8'))
    if Path(plan['root']).resolve()!=root:
        raise ValueError('Move journal belongs to a different root.')
    for move in reversed(plan['moves']):
        src=checked(root,move['source']);dst=checked(root,move['destination'])
        if not dst.exists():
            continue
        if src.exists():
            raise ValueError('Cannot undo without overwriting '+str(src))
        if dst.stat().st_size!=move['size']:
            raise ValueError('Cannot undo a file changed since consolidation.')
        src.parent.mkdir(parents=True,exist_ok=True)
        dst.rename(src)
    for marker in plan['markers']:
        checked(root,marker).mkdir(parents=True,exist_ok=True)

    for directory in sorted(map(Path,plan.get('created_dirs',[])),key=lambda p:len(p.parts),reverse=True):
        try:
            checked(root,directory).rmdir()
        except OSError:
            pass
