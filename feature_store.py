"""Persistent local feature data, isolated by server, channel and owner."""
import time
import json
from datetime import datetime,timedelta,timezone
from local_tools import next_annual


def install(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS local_reminders (
            id INTEGER PRIMARY KEY, guild TEXT NOT NULL, channel TEXT NOT NULL,
            user TEXT NOT NULL, body TEXT NOT NULL, due REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
            retry_at REAL NOT NULL DEFAULT 0, created REAL NOT NULL, message TEXT);
        CREATE INDEX IF NOT EXISTS reminders_due ON local_reminders(status,retry_at,due);
        CREATE TABLE IF NOT EXISTS local_replies (
            message TEXT PRIMARY KEY, guild TEXT NOT NULL, channel TEXT NOT NULL,
            command TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS local_inputs (
            message TEXT PRIMARY KEY, guild TEXT NOT NULL, channel TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS local_polls (
            id INTEGER PRIMARY KEY AUTOINCREMENT, guild TEXT NOT NULL, channel TEXT NOT NULL,
            user TEXT NOT NULL, question TEXT NOT NULL, options TEXT NOT NULL,
            message TEXT, status TEXT NOT NULL DEFAULT 'open', created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS local_votes (
            poll INTEGER NOT NULL, user TEXT NOT NULL, choice INTEGER NOT NULL, PRIMARY KEY(poll,user));
        CREATE TABLE IF NOT EXISTS rice_accounts (
            guild TEXT NOT NULL, user TEXT NOT NULL, balance INTEGER NOT NULL DEFAULT 3,
            signin_day TEXT, PRIMARY KEY(guild,user));
        CREATE TABLE IF NOT EXISTS local_games (
            guild TEXT NOT NULL, channel TEXT NOT NULL, user TEXT NOT NULL,
            target INTEGER NOT NULL, tries INTEGER NOT NULL DEFAULT 0, expires REAL NOT NULL,
            PRIMARY KEY(guild,channel,user));
        CREATE TABLE IF NOT EXISTS local_notes (
            guild TEXT NOT NULL, channel TEXT NOT NULL, user TEXT NOT NULL,
            key TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY(guild,channel,user,key));
        CREATE TABLE IF NOT EXISTS local_rules (
            guild TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY(guild,key));
    ''')
    columns={r['name'] for r in db.execute('PRAGMA table_info(local_reminders)')}
    for name,sql_type in (('annual_key','TEXT'),('annual_month','INTEGER'),('annual_day','INTEGER')):
        if name not in columns:
            db.execute(f'ALTER TABLE local_reminders ADD COLUMN {name} {sql_type}')
    db.commit()


class FeatureStore:
    def __init__(self,store):
        self.store=store
        self.db=store.db

    def enabled(self,guild,feature,default=True):
        return self.store.pref(f'feature:{guild}:{feature}','1' if default else '0')=='1'

    def toggle(self,guild,feature,enabled):
        self.store.set_pref(f'feature:{guild}:{feature}','1' if enabled else '0')

    def remember_reply(self,mid,guild,channel,command):
        if type(mid) is not int or mid<=0:
            return
        parts=command.split(maxsplit=1)
        if parts and parts[0] not in ('掷骰','骰子','抽签','选一个','选择','摸摸','投喂','喂饭','喂米饭'):
            command=parts[0]  # Mark local results without copying personal note/reminder text.
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO local_replies VALUES(?,?,?,?,?)',
                            (str(mid),str(guild),str(channel),command[:1000],time.time()))

    def reply_command(self,mid,guild,channel):
        row=self.db.execute('SELECT command FROM local_replies WHERE message=? AND guild=? AND channel=?',
                            (str(mid),str(guild),str(channel))).fetchone()
        return row['command'] if row else None

    def remember_input(self,mid,guild,channel):
        if type(mid) is int and mid>0:
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO local_inputs VALUES(?,?,?,?)',
                                (str(mid),str(guild),str(channel),time.time()))

    def is_input(self,mid,guild,channel):
        return self.db.execute('SELECT 1 FROM local_inputs WHERE message=? AND guild=? AND channel=?',
                               (str(mid),str(guild),str(channel))).fetchone() is not None

    def add_reminder(self,guild,channel,user,body,due,now=None,annual_key=None,annual_month=None,annual_day=None):
        now=time.time() if now is None else now
        active=self.db.execute("SELECT count(*) FROM local_reminders WHERE guild=? AND user=? AND status IN ('pending','sending')",
                               (str(guild),str(user))).fetchone()[0]
        if active>=20:
            raise ValueError('你在本服务器已有 20 条待办提醒，请先取消一条。')
        with self.db:
            row=self.db.execute('INSERT INTO local_reminders(guild,channel,user,body,due,created,annual_key,annual_month,annual_day) VALUES(?,?,?,?,?,?,?,?,?)',
                                (str(guild),str(channel),str(user),body,due,now,annual_key,annual_month,annual_day))
        return row.lastrowid

    def reminders(self,guild,channel,user):
        return [dict(r) for r in self.db.execute("SELECT * FROM local_reminders WHERE guild=? AND channel=? AND user=? AND status IN ('pending','sending','failed') ORDER BY due LIMIT 20",
                                               (str(guild),str(channel),str(user)))]

    def cancel(self,rid,guild,channel,user):
        with self.db:
            row=self.db.execute("UPDATE local_reminders SET status='cancelled' WHERE id=? AND guild=? AND channel=? AND user=? AND status IN ('pending','failed')",
                                (rid,str(guild),str(channel),str(user)))
        return row.rowcount>0

    def due(self,now=None,scopes=None):
        now=time.time() if now is None else now
        clause=''
        params=[now,now]
        if scopes is not None:
            if not scopes:
                return []
            clauses=[]
            for scope in scopes:
                guild,channel=scope[:2]
                item='(guild=? AND channel=?'
                if len(scope)==4:
                    reminders,dates=scope[2:]
                    if not reminders and not dates:
                        continue
                    if not dates:
                        item+=' AND annual_key IS NULL'
                    elif not reminders:
                        item+=' AND annual_key IS NOT NULL'
                params.extend((str(guild),str(channel)))
                clauses.append(item+')')
            if not clauses:
                return []
            clause=' AND ('+' OR '.join(clauses)+')'
        return [dict(r) for r in self.db.execute("SELECT * FROM local_reminders WHERE status='pending' AND due<=? AND retry_at<=?"+clause+' ORDER BY due LIMIT 10',params)]

    def claim(self,rid):
        with self.db:
            row=self.db.execute("UPDATE local_reminders SET status='sending', attempts=attempts+1 WHERE id=? AND status='pending'",(rid,))
        return row.rowcount>0

    def finish(self,rid,mid=None,now=None):
        now=time.time() if now is None else now
        with self.db:
            row=self.db.execute("SELECT * FROM local_reminders WHERE id=? AND status='sending'",(rid,)).fetchone()
            if row is None:
                return
            if row['annual_key']:
                due=next_annual(row['annual_month'],row['annual_day'],max(now,row['due']))
                self.db.execute("UPDATE local_reminders SET status='pending',message=?,due=?,attempts=0,retry_at=0 WHERE id=?",
                                (str(mid) if mid else None,due,rid))
            else:
                self.db.execute("UPDATE local_reminders SET status='sent', message=? WHERE id=?",(str(mid) if mid else None,rid))

    def annual(self,guild,channel,user,key,month,day,body,now=None):
        now=time.time() if now is None else now
        due=next_annual(month,day,now)
        row=self.db.execute("SELECT id FROM local_reminders WHERE guild=? AND channel=? AND user=? AND annual_key=? AND status IN ('pending','failed')",
                            (str(guild),str(channel),str(user),key)).fetchone()
        if row:
            with self.db:
                rid=row['id']
                self.db.execute("UPDATE local_reminders SET body=?,due=?,annual_month=?,annual_day=?,status='pending',attempts=0,retry_at=0 WHERE id=?",
                                (body,due,month,day,rid))
        else:
            rid=self.add_reminder(guild,channel,user,body,due,now,key,month,day)
        return rid

    def cancel_annual(self,guild,channel,user,key):
        with self.db:
            row=self.db.execute("UPDATE local_reminders SET status='cancelled' WHERE guild=? AND channel=? AND user=? AND annual_key=? AND status IN ('pending','failed')",
                                (str(guild),str(channel),str(user),key))
        return row.rowcount>0

    def create_poll(self,guild,channel,user,question,choices,now=None):
        now=time.time() if now is None else now
        active=self.db.execute("SELECT count(*) FROM local_polls WHERE guild=? AND channel=? AND status='open'",(str(guild),str(channel))).fetchone()[0]
        if active>=10:
            raise ValueError('本频道已有 10 个进行中的投票，请先结束一个。')
        with self.db:
            row=self.db.execute('INSERT INTO local_polls(guild,channel,user,question,options,created) VALUES(?,?,?,?,?,?)',
                                (str(guild),str(channel),str(user),question,json.dumps(choices,ensure_ascii=False),now))
        return row.lastrowid

    def poll(self,pid,guild,channel):
        row=self.db.execute('SELECT * FROM local_polls WHERE id=? AND guild=? AND channel=?',
                            (pid,str(guild),str(channel))).fetchone()
        if not row:
            raise ValueError('本频道没有这个投票。')
        result=dict(row)
        if result['status']=='open' and time.time()-result['created']>7*86400:
            with self.db:
                self.db.execute("UPDATE local_polls SET status='closed' WHERE id=?",(pid,))
            result['status']='closed'
        result['options']=json.loads(result['options'])
        counts={r['choice']:r['n'] for r in self.db.execute('SELECT choice,count(*) n FROM local_votes WHERE poll=? GROUP BY choice',(pid,))}
        result['counts']=[counts.get(i,0) for i in range(len(result['options']))]
        return result

    def poll_message(self,pid,mid):
        if type(mid) is int:
            with self.db:
                self.db.execute('UPDATE local_polls SET message=? WHERE id=? AND message IS NULL',(str(mid),pid))

    def open_polls(self):
        return [dict(r) for r in self.db.execute("SELECT id,guild,channel,message FROM local_polls WHERE status='open' AND message IS NOT NULL")]

    def vote(self,pid,guild,channel,user,choice):
        poll=self.poll(pid,guild,channel)
        if poll['status']!='open':
            raise ValueError('这个投票已经结束。')
        if type(choice) is not int or not 0<=choice<len(poll['options']):
            raise ValueError('投票选项无效。')
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO local_votes VALUES(?,?,?)',(pid,str(user),choice))
        return self.poll(pid,guild,channel)

    def end_poll(self,pid,guild,channel,user,admin=False):
        poll=self.poll(pid,guild,channel)
        if poll['user']!=str(user) and not admin:
            raise ValueError('只有发起人或管理员可以结束这个投票。')
        with self.db:
            self.db.execute("UPDATE local_polls SET status='closed' WHERE id=?",(pid,))
        return self.poll(pid,guild,channel)

    def rice(self,guild,user):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO rice_accounts(guild,user) VALUES(?,?)',(str(guild),str(user)))
        return dict(self.db.execute('SELECT * FROM rice_accounts WHERE guild=? AND user=?',(str(guild),str(user))).fetchone())

    def signin(self,guild,user,now=None):
        now=time.time() if now is None else now
        day=datetime.fromtimestamp(now,timezone(timedelta(hours=8))).date().isoformat()
        account=self.rice(guild,user)
        if account['signin_day']==day:
            raise ValueError(f'今天签过到啦，饭碗里还有 {account["balance"]} 粒米饭。')
        with self.db:
            self.db.execute('UPDATE rice_accounts SET balance=balance+10, signin_day=? WHERE guild=? AND user=?',
                            (day,str(guild),str(user)))
        return self.rice(guild,user)['balance']

    def feed(self,guild,user):
        account=self.rice(guild,user)
        if account['balance']<1:
            raise ValueError('米饭用完啦，用 !鲸鱼 签到 领一些。')
        with self.db:
            self.db.execute('UPDATE rice_accounts SET balance=balance-1 WHERE guild=? AND user=?',(str(guild),str(user)))
        return account['balance']-1

    def start_game(self,guild,channel,user,target,now=None):
        now=time.time() if now is None else now
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO local_games VALUES(?,?,?,?,?,?)',
                            (str(guild),str(channel),str(user),target,0,now+600))

    def game(self,guild,channel,user,now=None):
        now=time.time() if now is None else now
        row=self.db.execute('SELECT * FROM local_games WHERE guild=? AND channel=? AND user=? AND expires>?',
                            (str(guild),str(channel),str(user),now)).fetchone()
        return dict(row) if row else None

    def guess(self,guild,channel,user,value,now=None):
        row=self.game(guild,channel,user,now)
        if row is None:
            raise ValueError('先用 !鲸鱼 猜数字 开始；每局有效十分钟。')
        tries=row['tries']+1
        won=value==row['target']
        with self.db:
            if won or tries>=10:
                self.db.execute('DELETE FROM local_games WHERE guild=? AND channel=? AND user=?',(str(guild),str(channel),str(user)))
            else:
                self.db.execute('UPDATE local_games SET tries=? WHERE guild=? AND channel=? AND user=?',
                                (tries,str(guild),str(channel),str(user)))
        if won:
            return f'猜中啦！用了 {tries} 次。🐳'
        if tries>=10:
            return f'十次用完啦，答案是 {row["target"]}。再开一局嘛。'
        return ('大了一点。' if value>row['target'] else '小了一点。')+f'还剩 {10-tries} 次。'

    def quit_game(self,guild,channel,user):
        with self.db:
            self.db.execute('DELETE FROM local_games WHERE guild=? AND channel=? AND user=?',
                            (str(guild),str(channel),str(user)))

    def save_note(self,guild,channel,user,key,value):
        if not key or not value or len(key)>24 or len(value)>300:
            raise ValueError('便签名称最多 24 字，内容最多 300 字。')
        args=(str(guild),str(channel),str(user))
        rows=self.notes(guild,channel,user)
        if len(rows)>=20 and key not in {r['key'] for r in rows}:
            raise ValueError('本频道的便签已满 20 条，请先删除一条。')
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO local_notes VALUES(?,?,?,?,?)',(*args,key,value))

    def notes(self,guild,channel,user):
        return [dict(r) for r in self.db.execute('SELECT key,value FROM local_notes WHERE guild=? AND channel=? AND user=? ORDER BY key',
                                               (str(guild),str(channel),str(user)))]

    def delete_note(self,guild,channel,user,key):
        with self.db:
            row=self.db.execute('DELETE FROM local_notes WHERE guild=? AND channel=? AND user=? AND key=?',
                                (str(guild),str(channel),str(user),key))
        return row.rowcount>0

    def rules(self,guild,keyword=''):
        rows=[dict(r) for r in self.db.execute('SELECT key,value FROM local_rules WHERE guild=? ORDER BY key',(str(guild),))]
        return [r for r in rows if keyword.lower() in (r['key']+' '+r['value']).lower()][:5]

    def save_rule(self,guild,key,value):
        if not key or not value or len(key)>24 or len(value)>300:
            raise ValueError('群规名称最多 24 字，内容最多 300 字。')
        count=self.db.execute('SELECT count(*) FROM local_rules WHERE guild=?',(str(guild),)).fetchone()[0]
        old=self.db.execute('SELECT 1 FROM local_rules WHERE guild=? AND key=?',(str(guild),key)).fetchone()
        if count>=30 and not old:
            raise ValueError('本服务器群规已满 30 条，请先删除一条。')
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO local_rules VALUES(?,?,?)',(str(guild),key,value))

    def delete_rule(self,guild,key):
        with self.db:
            row=self.db.execute('DELETE FROM local_rules WHERE guild=? AND key=?',(str(guild),key))
        return row.rowcount>0

    def retry(self,rid,now=None):
        now=time.time() if now is None else now
        with self.db:
            self.db.execute("UPDATE local_reminders SET status=CASE WHEN attempts>=5 THEN 'failed' ELSE 'pending' END, retry_at=? WHERE id=? AND status='sending'",
                            (now+60,rid))

    def recover(self):
        with self.db:
            self.db.execute("UPDATE local_reminders SET status='pending' WHERE status='sending'")

    def prune(self,now=None):
        now=time.time() if now is None else now
        with self.db:
            self.db.execute("DELETE FROM local_reminders WHERE status IN ('sent','cancelled') AND created<?",(now-30*86400,))
            self.db.execute('DELETE FROM local_replies WHERE created<?',(now-30*86400,))
            self.db.execute('DELETE FROM local_inputs WHERE created<?',(now-30*86400,))
            self.db.execute('DELETE FROM local_games WHERE expires<=?',(now,))
            self.db.execute("UPDATE local_polls SET status='closed' WHERE status='open' AND created<?",(now-7*86400,))
            self.db.execute("DELETE FROM local_votes WHERE poll IN (SELECT id FROM local_polls WHERE status='closed' AND created<?)",(now-30*86400,))
            self.db.execute("DELETE FROM local_polls WHERE status='closed' AND created<?",(now-30*86400,))
