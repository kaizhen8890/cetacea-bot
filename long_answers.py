"""Offline long-answer detection, lossless delivery plans and saved task state."""
import json
import re
import time
import unicodedata


def writing_request(text):
    return bool(re.search(r'写(?:一[篇封份段首个]|篇|封|份|作|作业)|帮我写|给我写|范文|作文|文章|翻译|译成|译为|改写|扩写|续写|润色|整理成|生成.*(?:代码|报告|文案)|写.*(?:代码|程序)|rumusan|karangan|\b(?:write|essay|translate|summari[sz]e)\b',text,re.I))


def continue_request(text):
    return bool(re.fullmatch(r'\s*(?:继续(?:写|讲|说|生成|发送)?|接着(?:写|讲|说)|续写|写完|补发(?:剩余)?|continue)[。.!！?？\s]*',text,re.I))


def units(text):
    # Conservative for Discord, including astral emoji and all formatting markers.
    return len(text.encode('utf-16-le'))//2


def _clusters(text):
    cluster=''
    for ch in text:
        mark=(unicodedata.category(ch) in ('Mn','Mc','Me') or ch=='\u200d'
              or '\U0001f3fb'<=ch<='\U0001f3ff')
        flag=bool(cluster and len(cluster)==1 and '\U0001f1e6'<=cluster<='\U0001f1ff' and '\U0001f1e6'<=ch<='\U0001f1ff')
        if cluster and not (mark or cluster.endswith('\u200d') or flag):
            yield cluster
            cluster=''
        cluster+=ch
    if cluster:
        yield cluster


def _fence_after(text,fence=None):
    for line in text.splitlines():
        if fence and re.fullmatch(r' {0,3}'+re.escape(fence[1][0])+'{'+str(len(fence[1]))+r',}\s*',line):
            fence=None
        elif not fence:
            match=re.fullmatch(r' {0,3}(`{3,8}|~{3,8})([\w+.-]{0,40})\s*',line)
            if match:
                fence=(line.strip(),match[1])
    return fence


def split_text(text,limit=1900):
    """Split paragraphs/sentences/words, closing and reopening fenced code blocks."""
    if units(text)<=limit:
        fence=_fence_after(text)
        closed=text+('\n'+fence[1] if fence else '')
        if units(closed)<=limit:
            return [closed] if text else []
    chunks=[]
    remaining=text
    fence=None
    while remaining:
        prefix=(fence[0]+'\n') if fence else ''
        room=limit-units(prefix)-40  # Numbering and closing fences are included.
        count=end=0
        boundaries=[]
        for cluster in _clusters(remaining):
            width=units(cluster)
            if count+width>room:
                break
            count+=width; end+=len(cluster)
            boundaries.append(end)
        if not end:
            raise ValueError('单个字符组合过长，请使用文本附件。')
        if end<len(remaining):
            sample=remaining[:end]
            paragraph=sample.rfind('\n\n')
            lines=sample.rfind('\n')
            breaks=[m.end() for m in re.finditer(r'[。！？.!?](?:\s|$)|\s+',sample)]
            preferred=paragraph+2 if paragraph>=end//3 else lines+1 if lines>=end//3 else (breaks[-1] if breaks and breaks[-1]>=end//3 else end)
            end=max((boundary for boundary in boundaries if boundary<=preferred),default=end)
        raw,remaining=remaining[:end],remaining[end:]
        fence=_fence_after(raw,fence)
        suffix=('\n'+fence[1]) if fence else ''
        chunks.append(prefix+raw+suffix)
    total=len(chunks)
    result=[f'（{i}/{total}）\n'+chunk for i,chunk in enumerate(chunks,1)]
    assert all(units(chunk)<=limit for chunk in result)
    return result


def merge_continuation(body,new):
    if not body:
        return new
    if new.startswith(body):
        return new
    for size in range(min(len(body),len(new),2000),11,-1):
        if body[-size:]==new[:size]:
            return body+new[size:]
    previous=re.search(r'([A-Za-z]{3,})$',body)
    following=re.match(r'[A-Za-z]+',new)
    if previous and following and following[0].startswith(previous[0]):
        return body[:-len(previous[0])]+new
    separator=' ' if re.search(r'[A-Za-z0-9]$',body) and re.match(r'[A-Za-z0-9]',new) else '\n\n' if body.endswith(('.', '。','!','！','?','？','```')) else ''
    return body+separator+new


class AnswerStore:
    def __init__(self,db,volatile=False):
        self.db,self.volatile=db,volatile
        db.executescript('''
            CREATE TABLE IF NOT EXISTS answer_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,guild TEXT NOT NULL,channel TEXT NOT NULL,
                owner TEXT NOT NULL,prompt TEXT NOT NULL,body TEXT NOT NULL DEFAULT '',
                finished INTEGER NOT NULL DEFAULT 0,delivered INTEGER NOT NULL DEFAULT 0,
                plan TEXT NOT NULL DEFAULT '[]',revision INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL,updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS answer_links (
                message TEXT PRIMARY KEY,task INTEGER NOT NULL,author TEXT NOT NULL,kind TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS answer_scope ON answer_tasks(guild,channel,owner,updated);
        ''')

    def get(self,tid,guild,channel):
        row=self.db.execute('SELECT * FROM answer_tasks WHERE id=? AND guild=? AND channel=?',
                            (tid,str(guild),str(channel))).fetchone()
        if row is None:
            return None
        result=dict(row)
        result['plan']=json.loads(result['plan'])
        result['volatile']=self.volatile
        return result

    def by_message(self,mid,guild,channel):
        row=self.db.execute('SELECT task FROM answer_links WHERE message=?',(str(mid),)).fetchone()
        return self.get(row['task'],guild,channel) if row else None

    def latest(self,guild,channel,user,seconds=120):
        row=self.db.execute('''SELECT id FROM answer_tasks WHERE guild=? AND channel=? AND owner=?
            AND updated>? ORDER BY updated DESC,id DESC LIMIT 1''',
            (str(guild),str(channel),str(user),time.time()-seconds)).fetchone()
        return self.get(row['id'],guild,channel) if row else None

    def link(self,tid,mid,author,kind='request'):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO answer_links VALUES(?,?,?,?)',(str(mid),tid,str(author),kind))

    def create(self,guild,channel,user,prompt,ids):
        now=time.time()
        with self.db:
            row=self.db.execute('INSERT INTO answer_tasks(guild,channel,owner,prompt,created,updated) VALUES(?,?,?,?,?,?)',
                                (str(guild),str(channel),str(user),prompt,now,now))
        for mid in ids:
            self.link(row.lastrowid,mid,user)
        return self.get(row.lastrowid,guild,channel)

    def append(self,task,text,finished):
        body=merge_continuation(task['body'],text)
        with self.db:
            self.db.execute('UPDATE answer_tasks SET body=?,finished=?,revision=revision+1,updated=? WHERE id=?',
                            (body,int(finished),time.time(),task['id']))
        return self.get(task['id'],task['guild'],task['channel'])

    def plan(self,task,parts):
        with self.db:
            self.db.execute('UPDATE answer_tasks SET plan=?,updated=? WHERE id=?',
                            (json.dumps(parts,ensure_ascii=False),time.time(),task['id']))
        return self.get(task['id'],task['guild'],task['channel'])

    def receipt(self,task,index,mid,bot_id):
        parts=task['plan']
        parts[index]['sent']=str(mid)
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO answer_links VALUES(?,?,?,?)',(str(mid),task['id'],str(bot_id),'reply'))
            done=all(part.get('sent') for part in parts)
            delivered=len(task['body']) if done else task['delivered']
            self.db.execute('UPDATE answer_tasks SET plan=?,delivered=?,updated=? WHERE id=?',
                            (json.dumps(parts,ensure_ascii=False),delivered,time.time(),task['id']))
        return self.get(task['id'],task['guild'],task['channel'])

    def delete(self,ids):
        with self.db:
            for tid in ids:
                self.db.execute('DELETE FROM answer_links WHERE task=?',(tid,))
                self.db.execute('DELETE FROM answer_tasks WHERE id=?',(tid,))

    def erase_message(self,mid):
        self.delete([r['task'] for r in self.db.execute('SELECT task FROM answer_links WHERE message=?',(str(mid),))])

    def erase_user(self,guild,user):
        self.delete([r['id'] for r in self.db.execute('''SELECT id FROM answer_tasks WHERE guild=? AND
            (owner=? OR id IN (SELECT task FROM answer_links WHERE author=? AND kind='request'))''',
            (str(guild),str(user),str(user)))])

    def erase_channel(self,guild,channel):
        self.delete([r['id'] for r in self.db.execute('SELECT id FROM answer_tasks WHERE guild=? AND channel=?',(str(guild),str(channel)))])

    def prune(self):
        self.delete([r['id'] for r in self.db.execute('SELECT id FROM answer_tasks WHERE updated<?',(time.time()-7*86400,))])
