"""Durable budget reservations and channel-scoped, user-authored memories."""
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from memory import install as install_memory

LOCAL_ZONE = timezone(timedelta(hours=8))

def day_key(now=None):
    return (now or datetime.now(LOCAL_ZONE)).astimezone(LOCAL_ZONE).date().isoformat()

class BudgetExceeded(Exception):
    pass

class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            PRAGMA secure_delete=ON;
            CREATE TABLE IF NOT EXISTS calls (
                id INTEGER PRIMARY KEY, day TEXT NOT NULL, kind TEXT NOT NULL,
                charged REAL NOT NULL, status TEXT NOT NULL, input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS memories (
                guild TEXT, channel TEXT, user TEXT, key TEXT, value TEXT,
                PRIMARY KEY(guild,channel,user,key));
            CREATE TABLE IF NOT EXISTS prefs (key TEXT PRIMARY KEY, value TEXT);
        ''')
        install_memory(self.db)

    def close(self):
        self.db.close()

    def usage(self, day=None):
        row = self.db.execute('''SELECT coalesce(sum(charged),0) cost, count(*) calls,
            coalesce(sum(CASE WHEN kind='casual' THEN charged ELSE 0 END),0) casual_cost,
            coalesce(sum(CASE WHEN kind='casual' THEN 1 ELSE 0 END),0) casual_calls
            FROM calls WHERE day=?''', (day or day_key(),)).fetchone()
        return dict(row)

    def reserve(self, amount, kind, config, day=None):
        if not math.isfinite(amount) or amount < 0:
            raise ValueError('invalid reservation')
        day = day or day_key()
        self.db.execute('BEGIN IMMEDIATE')
        try:
            u = self.usage(day)
            if (u['cost'] + amount > config['daily_budget_rmb'] * config['budget_usable_fraction']
                    or u['calls'] >= config['daily_call_limit']):
                raise BudgetExceeded('今天的聊天额度用完啦，明天再聊；本地记忆命令仍可用。')
            if kind == 'casual' and (u['casual_calls'] >= config['casual_daily_call_limit']
                    or u['casual_cost'] + amount > config['casual_budget_rmb']):
                raise BudgetExceeded('插话额度用完')
            row = self.db.execute('INSERT INTO calls(day,kind,charged,status) VALUES(?,?,?,?)',
                                  (day, kind, amount, 'reserved'))
            self.db.commit()
            return row.lastrowid
        except BaseException:
            self.db.rollback()
            raise

    def finish(self, call_id, actual=None, input_tokens=0, output_tokens=0, status='ok'):
        # Missing usage, network errors and crashes retain the full reservation.
        with self.db:
            if actual is not None:
                if not math.isfinite(actual) or actual < 0:
                    raise ValueError('invalid cost')
                self.db.execute('UPDATE calls SET charged=?,status=?,input_tokens=?,output_tokens=? WHERE id=?',
                                (actual, status, input_tokens, output_tokens, call_id))
            else:
                self.db.execute('UPDATE calls SET status=? WHERE id=?', (status, call_id))

    def pref(self, key, default=''):
        r = self.db.execute('SELECT value FROM prefs WHERE key=?', (key,)).fetchone()
        return r['value'] if r else default

    def set_pref(self, key, value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO prefs VALUES(?,?)', (key, value))

    def memories(self, guild, channel, user):
        return [dict(r) for r in self.db.execute(
            'SELECT key,value FROM memories WHERE guild=? AND channel=? AND user=? ORDER BY key',
            (str(guild), str(channel), str(user)))]

    def guild_memories(self,guild,user):
        return [dict(r) for r in self.db.execute(
            'SELECT channel,key,value FROM memories WHERE guild=? AND user=? ORDER BY key',
            (str(guild),str(user)))]

    def remember(self, guild, channel, user, key, value, limit=12):
        key, value = key.strip(), value.strip()
        if not key or not value or len(key) > 24 or len(value) > 160:
            raise ValueError('格式：!鲸鱼 记住 称呼=小明；名称最多24字，内容最多160字。')
        old = self.memories(guild, channel, user)
        if len(old) >= limit and key not in {r['key'] for r in old}:
            raise ValueError('记忆已满，请先忘记一条。')
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO memories VALUES(?,?,?,?,?)',
                            (str(guild), str(channel), str(user), key, value))

    def forget(self, guild, channel, user, key=None):
        args = (str(guild), str(channel), str(user))
        with self.db:
            if key is None:
                self.db.execute('DELETE FROM memories WHERE guild=? AND channel=? AND user=?', args)
            else:
                self.db.execute('DELETE FROM memories WHERE guild=? AND channel=? AND user=? AND key=?', args + (key,))
        self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
