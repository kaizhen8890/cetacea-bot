"""Local, provenance-aware memory journal and retrieval. No network calls here."""
import json
import re
import time


class StaleSource(Exception):
    """The user deleted or changed source data while a local summary was running."""


def terms(text):
    words=set(w.lower() for w in re.findall(r'[A-Za-z][A-Za-z0-9_]{2,}',text))
    for run in re.findall(r'[\u3400-\u9fff]{2,}',text):
        words.update(run[i:i+2] for i in range(len(run)-1))
    words.difference_update({'今天','我们','你们','他们','这个','那个','什么','怎么',
                             '可以','现在','还是','一下','知道','已经','还有','后来',
                             '时候','是不是','真的','感觉'})
    return words


def install(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS journal (
            id TEXT PRIMARY KEY, guild TEXT NOT NULL, channel TEXT NOT NULL,
            author TEXT NOT NULL, name TEXT NOT NULL, body TEXT NOT NULL,
            subject TEXT NOT NULL DEFAULT '',
            created REAL NOT NULL, role TEXT NOT NULL, engaged INTEGER NOT NULL,
            public INTEGER NOT NULL, processed INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS journal_pending ON journal(processed,created);
        CREATE INDEX IF NOT EXISTS journal_scope ON journal(guild,channel,author);
        CREATE TABLE IF NOT EXISTS auto_memories (
            id INTEGER PRIMARY KEY, guild TEXT NOT NULL, channel TEXT NOT NULL,
            kind TEXT NOT NULL, body TEXT NOT NULL, keywords TEXT NOT NULL,
            participants TEXT NOT NULL, source_ids TEXT NOT NULL,
            public INTEGER NOT NULL, created REAL NOT NULL,
            folded INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS auto_memory_scope ON auto_memories(guild,kind,created);
    ''')


def record(db, mid, guild, channel, author, name, body, role='user', engaged=False,
           public=True, created=None, subject=None):
    if not body or not body.strip() or body.strip().startswith('!鲸鱼'):
        return
    with db:
        db.execute('''INSERT OR IGNORE INTO journal
            (id,guild,channel,author,name,body,subject,created,role,engaged,public)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
            (str(mid),str(guild),str(channel),str(author),name[:32],body[:2000],
             str(subject or ''),
             time.time() if created is None else created,role,int(engaged),int(public)))


def mark_engaged(db, ids):
    ids=[str(x) for x in ids]
    if ids:
        with db:
            db.executemany('UPDATE journal SET engaged=1 WHERE id=?',[(x,) for x in ids])


def next_batch(db, idle_seconds=900, now=None, limit=40):
    cutoff=(time.time() if now is None else now)-idle_seconds
    groups=db.execute('''SELECT guild,channel,engaged,min(created) oldest
        FROM journal WHERE processed=0 AND created<=?
        GROUP BY guild,channel,engaged ORDER BY oldest''',(cutoff,)).fetchall()
    for group in groups:
        all_rows=[dict(r) for r in db.execute('''SELECT * FROM journal
            WHERE processed=0 AND guild=? AND channel=? AND engaged=? AND created<=?
            ORDER BY created LIMIT ?''',
            (group['guild'],group['channel'],group['engaged'],cutoff,limit*3))]
        segments=[]
        for row in all_rows:
            if not segments or row['created']-segments[-1][-1]['created']>900 or len(segments[-1])>=limit:
                segments.append([])
            segments[-1].append(row)
        for rows in segments:
            if len(rows)>=(2 if group['engaged'] else 10) or (
                    rows and (time.time() if now is None else now)-rows[-1]['created']>=86400):
                return rows
    return []


def save_summary(db, rows, summary):
    if not rows or not summary.get('body'):
        return False
    kind='conversation' if rows[0]['engaged'] else 'glance'
    body=summary['body'].strip()[:220 if kind=='conversation' else 55]
    if not body:
        return False
    ids=[r['id'] for r in rows]
    participants=sorted({r['author'] for r in rows if r['role']=='user'})
    keywords=summary.get('keywords',[])
    if not isinstance(keywords,list):
        keywords=[]
    public=all(r['public'] for r in rows)
    db.execute('BEGIN IMMEDIATE')
    try:
        placeholders=','.join('?' for _ in ids)
        found=db.execute(f'SELECT count(*) n FROM journal WHERE id IN ({placeholders})',ids).fetchone()['n']
        if found!=len(ids):
            raise StaleSource()
        db.execute('''INSERT INTO auto_memories
            (guild,channel,kind,body,keywords,participants,source_ids,public,created)
            VALUES(?,?,?,?,?,?,?,?,?)''',
            (rows[0]['guild'],rows[0]['channel'],kind,body,
             json.dumps([str(x)[:24] for x in keywords[:8]],ensure_ascii=False),
             json.dumps(participants),json.dumps(ids),int(public),time.time()))
        db.executemany('UPDATE journal SET processed=1 WHERE id=?',[(x,) for x in ids])
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return True


def pending_fold(db, guild, limit=10):
    rows=[dict(r) for r in db.execute('''SELECT * FROM auto_memories
        WHERE guild=? AND kind IN ('conversation','glance') AND folded=0
        ORDER BY created''',(str(guild),))]
    groups={}
    for row in rows:
        key=('public',) if row['public'] else ('private',row['channel'])
        groups.setdefault(key,[]).append(row)
    return next((group[:limit] for group in groups.values() if len(group)>=limit),[])


def save_long(db, rows, items):
    if not rows or not isinstance(items,list) or not items:
        return False
    all_ids=[r['id'] for r in rows]
    allowed={str(x) for r in rows for x in json.loads(r['source_ids'])}
    covered=set()
    db.execute('BEGIN IMMEDIATE')
    try:
        placeholders=','.join('?' for _ in all_ids)
        found=db.execute(f'SELECT count(*) n FROM auto_memories WHERE id IN ({placeholders})',all_ids).fetchone()['n']
        if found!=len(all_ids):
            raise StaleSource()
        for item in items[:10]:
            if not isinstance(item,dict) or not isinstance(item.get('body'),str):
                continue
            used=[x for x in item.get('source_summary_ids',[]) if type(x) is int and x in all_ids]
            source_rows=[r for r in rows if r['id'] in used]
            if not source_rows:
                continue
            source_ids=sorted({str(x) for r in source_rows for x in json.loads(r['source_ids'])})
            if not set(source_ids)<=allowed:
                continue
            participants=sorted({str(x) for r in source_rows for x in json.loads(r['participants'])})
            keywords=item.get('keywords',[])
            if not isinstance(keywords,list):
                keywords=[]
            body=item['body'].strip()[:180]
            if not body:
                continue
            db.execute('''INSERT INTO auto_memories
                (guild,channel,kind,body,keywords,participants,source_ids,public,created)
                VALUES(?,?,?,?,?,?,?,?,?)''',
                (source_rows[0]['guild'],source_rows[0]['channel'],'long',body,
                 json.dumps([str(x)[:24] for x in keywords[:8]],ensure_ascii=False),
                 json.dumps(participants),json.dumps(source_ids),
                 int(all(r['public'] for r in source_rows)),time.time()))
            covered.update(used)
        # Fold only source summaries actually represented by a validated long entry.
        db.executemany('UPDATE auto_memories SET folded=1 WHERE id=?',[(x,) for x in covered])
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return bool(covered)


def retrieve(db,guild,channel,user,query,limit=4):
    needles=terms(query)
    if not needles:
        return []
    scope=(str(guild),str(channel))
    # Keep the short-memory search bounded, but let an old long-term fact surface.
    candidates=list(db.execute('''SELECT * FROM auto_memories WHERE guild=? AND
        (public=1 OR channel=?) AND kind!='long' AND folded=0
        ORDER BY created DESC LIMIT 300''',scope))
    candidates.extend(db.execute('''SELECT * FROM auto_memories WHERE guild=? AND
        (public=1 OR channel=?) AND kind='long' ORDER BY created DESC''',scope))
    scored=[]
    for row in candidates:
        if row['folded']:
            continue
        hay=terms(row['body']+' '+ ' '.join(json.loads(row['keywords'])))
        overlap=len(needles & hay)
        participants=json.loads(row['participants'])
        if not overlap:
            continue
        score=overlap*3+(4 if str(user) in participants else 0)+(1 if row['kind']=='long' else 0)
        scored.append((score,row['created'],row))
    scored.sort(reverse=True,key=lambda x:(x[0],x[1]))
    return [{'kind':r['kind'],'text':r['body'],'channel':r['channel']}
            for _,_,r in scored[:limit]]


def _invalidate(db, source_ids):
    ids={str(x) for x in source_ids}
    if not ids:
        return
    affected=[]
    reconsider=set()
    long_sources=set()
    rows=list(db.execute('SELECT id,kind,source_ids,folded FROM auto_memories'))
    for row in rows:
        source=set(json.loads(row['source_ids']))
        if ids.intersection(source):
            affected.append((row['id'],))
            if row['kind']=='long':
                long_sources.update(source)
            else:
                reconsider.update(source-ids)
    if affected:
        db.executemany('DELETE FROM auto_memories WHERE id=?',affected)
        # Surviving short entries regain visibility if their folded long entry vanished.
        affected_ids={item[0] for item in affected}
        db.executemany('UPDATE auto_memories SET folded=0 WHERE id=?',
            [(row['id'],) for row in rows if row['id'] not in affected_ids
             and row['folded'] and long_sources.intersection(json.loads(row['source_ids']))])
        db.executemany('UPDATE journal SET processed=0 WHERE id=?',[(x,) for x in reconsider])


def erase_message(db,mid):
    with db:
        _invalidate(db,[mid])
        db.execute('DELETE FROM journal WHERE id=?',(str(mid),))


def erase_user(db,guild,user):
    scope=(str(guild),str(user))
    ids=[r['id'] for r in db.execute('SELECT id FROM journal WHERE guild=? AND (author=? OR subject=?)',
                                     (scope[0],scope[1],scope[1]))]
    with db:
        _invalidate(db,ids)
        db.execute('DELETE FROM journal WHERE guild=? AND (author=? OR subject=?)',
                   (scope[0],scope[1],scope[1]))
        db.execute('DELETE FROM memories WHERE guild=? AND user=?',scope)
    db.execute('PRAGMA wal_checkpoint(TRUNCATE)')


def status(db,guild):
    pending=db.execute('''SELECT count(*) n,max(created) newest FROM journal
        WHERE guild=? AND processed=0''',(str(guild),)).fetchone()
    summary=db.execute('''SELECT count(*) n,max(created) newest FROM auto_memories
        WHERE guild=?''',(str(guild),)).fetchone()
    return {'pending':pending['n'],'summaries':summary['n'],'last_summary':summary['newest']}
