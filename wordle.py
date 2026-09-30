"""Offline shared Wordle rounds, duplicate-letter scoring, and PNG boards."""
import io
import json
import random
import re
import time
from collections import Counter
from functools import lru_cache
from pathlib import Path

ROOT=Path(__file__).resolve().parent
MARKS=('⬛','🟨','🟩')
RULES=('🐳 全频道共用一局：普通 5 字母 / 6 次，超级 7 字母 / 12 次。\n'
       '🟩 字母与位置正确；🟨 字母存在但位置不对；⬛ 没有剩余的该字母可匹配。\n'
       '重复字母按答案中的实际数量判断。合法的新单词才扣次数；无效或重复猜测不扣。\n'
       '使用「提交猜测」按钮，或 /鲸鱼 wordle 猜。普通聊天不会被当成猜测。\n'
       '发起人或管理员可提前结束；进度在本机保存，不调用模型。')


@lru_cache(maxsize=2)
def vocabulary(size):
    folder=ROOT/'wordlists'
    allowed=frozenset((folder/f'allowed-{size}.txt').read_text(encoding='utf-8').splitlines())
    answers=tuple((folder/f'answers-{size}.txt').read_text(encoding='utf-8').splitlines())
    if not answers or not set(answers).issubset(allowed):
        raise ValueError('本地 Wordle 词库缺失或不完整。')
    return allowed,answers


def score(answer,guess):
    """Consume exact matches first, then only remaining occurrences left to right."""
    if len(answer)!=len(guess):
        raise ValueError('猜测与答案长度不一致。')
    marks=[2 if a==g else 0 for a,g in zip(answer,guess)]
    remaining=Counter(a for a,m in zip(answer,marks) if m!=2)
    for i,letter in enumerate(guess):
        if marks[i]!=2 and remaining[letter]>0:
            marks[i]=1
            remaining[letter]-=1
    return marks


def install(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS wordle_rounds (
            id INTEGER PRIMARY KEY AUTOINCREMENT, guild TEXT NOT NULL, channel TEXT NOT NULL,
            owner TEXT NOT NULL, answer TEXT NOT NULL, size INTEGER NOT NULL,
            max_tries INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'playing',
            message TEXT, created REAL NOT NULL, updated REAL NOT NULL);
        CREATE UNIQUE INDEX IF NOT EXISTS wordle_active ON wordle_rounds(guild,channel)
            WHERE state='playing';
        CREATE TABLE IF NOT EXISTS wordle_guesses (
            id INTEGER PRIMARY KEY AUTOINCREMENT, round INTEGER NOT NULL,
            user TEXT NOT NULL, name TEXT NOT NULL, word TEXT NOT NULL, marks TEXT NOT NULL,
            request TEXT NOT NULL, created REAL NOT NULL, avatar TEXT,
            UNIQUE(round,word), UNIQUE(round,request));
    ''')
    if 'avatar' not in {row['name'] for row in db.execute('PRAGMA table_info(wordle_guesses)')}:
        with db:
            db.execute('ALTER TABLE wordle_guesses ADD COLUMN avatar TEXT')


class WordleStore:
    def __init__(self,store):
        self.db=store.db
        install(self.db)

    def latest(self,guild,channel):
        row=self.db.execute('SELECT id FROM wordle_rounds WHERE guild=? AND channel=? ORDER BY id DESC LIMIT 1',
                            (str(guild),str(channel))).fetchone()
        return self.get(row['id'],guild,channel) if row else None

    def get(self,rid,guild,channel):
        row=self.db.execute('SELECT * FROM wordle_rounds WHERE id=? AND guild=? AND channel=?',
                            (rid,str(guild),str(channel))).fetchone()
        if not row:
            raise ValueError('找不到本频道的这局 Wordle。')
        result=dict(row)
        result['guesses']=[dict(r) for r in self.db.execute(
            'SELECT * FROM wordle_guesses WHERE round=? ORDER BY id',(rid,))]
        result['keyboard']={}
        for guess in result['guesses']:
            guess['marks']=json.loads(guess['marks'])
            for letter,mark in zip(guess['word'],guess['marks']):
                result['keyboard'][letter]=max(result['keyboard'].get(letter,-1),mark)
        return result

    def start(self,guild,channel,user,mode='普通',now=None):
        modes={'普通':5,'超级':7,'5':5,'7':7,'normal':5,'super':7}
        size=modes.get(mode.lower())
        if size is None:
            raise ValueError('请选择普通（5 字母）或超级（7 字母）模式。')
        current=self.latest(guild,channel)
        if current and current['state']=='playing':
            raise ValueError('本频道已有进行中的 Wordle；请先完成，或由发起人/管理员结束。')
        _,answers=vocabulary(size)
        recent={r['answer'] for r in self.db.execute(
            'SELECT answer FROM wordle_rounds WHERE guild=? AND channel=? AND size=? ORDER BY id DESC LIMIT 20',
            (str(guild),str(channel),size))}
        answer=random.choice([word for word in answers if word not in recent] or answers)
        now=time.time() if now is None else now
        with self.db:
            row=self.db.execute('INSERT INTO wordle_rounds(guild,channel,owner,answer,size,max_tries,created,updated) VALUES(?,?,?,?,?,?,?,?)',
                (str(guild),str(channel),str(user),answer,size,6 if size==5 else 12,now,now))
        return self.get(row.lastrowid,guild,channel)

    def guess(self,guild,channel,user,name,word,request,expected=None,now=None):
        now=time.time() if now is None else now
        self.db.execute('BEGIN IMMEDIATE')
        try:
            current=self.latest(guild,channel)
            if not current:
                raise ValueError('先用 /鲸鱼 wordle 开始 开一局。')
            if expected is not None and current['id']!=expected:
                raise ValueError('这张棋盘或输入框属于旧的一局，请使用最新棋盘。')
            rid=current['id']
            old=self.db.execute('''SELECT g.round FROM wordle_guesses g JOIN wordle_rounds r ON r.id=g.round
                WHERE r.guild=? AND r.channel=? AND g.request=? LIMIT 1''',
                (str(guild),str(channel),str(request))).fetchone()
            if old and old['round']!=rid:
                raise ValueError('这次提交已经在旧的一局处理过；此次不扣次数。')
            if self.db.execute('SELECT 1 FROM wordle_guesses WHERE round=? AND request=?',(rid,str(request))).fetchone():
                self.db.commit()
                return current
            if current['state']!='playing':
                raise ValueError('本局已经结束，可以重新开局。')
            word=word.strip()
            size=current['size']
            if not re.fullmatch(rf'[A-Za-z]{{{size}}}',word):
                raise ValueError(f'请输入一个 {size} 字母的英文单词；此次不扣次数。')
            word=word.lower()
            if word not in vocabulary(size)[0]:
                raise ValueError('这个单词不在本地词库中；此次不扣次数。')
            if any(g['word']==word for g in current['guesses']):
                raise ValueError('大家已经猜过这个单词啦；此次不扣次数。')
            previous=next((g for g in reversed(current['guesses']) if g['user']==str(user)),None)
            if previous and now-previous['created']<3:
                raise ValueError('等三秒再猜嘛，给群友也留点讨论时间；此次不扣次数。')
            marks=score(current['answer'],word)
            count=len(current['guesses'])+1
            state='won' if word==current['answer'] else 'lost' if count>=current['max_tries'] else 'playing'
            self.db.execute('INSERT INTO wordle_guesses(round,user,name,word,marks,request,created) VALUES(?,?,?,?,?,?,?)',
                (rid,str(user),str(name)[:24],word,json.dumps(marks),str(request),now))
            self.db.execute('UPDATE wordle_rounds SET state=?,updated=? WHERE id=?',(state,now,rid))
            self.db.commit()
            return self.get(rid,guild,channel)
        except BaseException:
            self.db.rollback()
            raise

    def stop(self,guild,channel,user,admin=False):
        current=self.latest(guild,channel)
        if not current:
            raise ValueError('本频道还没有 Wordle。')
        if current['state']=='playing':
            if str(user)!=current['owner'] and not admin:
                raise ValueError('只有发起人或管理员可以提前结束。')
            with self.db:
                self.db.execute("UPDATE wordle_rounds SET state='stopped',updated=? WHERE id=?",(time.time(),current['id']))
        return self.get(current['id'],guild,channel)

    def message(self,rid,mid):
        with self.db:
            self.db.execute('UPDATE wordle_rounds SET message=? WHERE id=?',(str(mid),rid))

    def avatar(self,guess_id,guild,channel,key):
        """A snapshot is scoped to this channel and never replaces an older one."""
        if not isinstance(key,str) or not re.fullmatch(r'[0-9a-f]{64}',key):
            raise ValueError('Invalid avatar cache key')
        with self.db:
            self.db.execute('''UPDATE wordle_guesses SET avatar=? WHERE id=? AND avatar IS NULL
                AND round IN (SELECT id FROM wordle_rounds WHERE guild=? AND channel=?)''',
                (key,guess_id,str(guild),str(channel)))

    def avatar_keys(self):
        return {row['avatar'] for row in self.db.execute(
            'SELECT DISTINCT avatar FROM wordle_guesses WHERE avatar IS NOT NULL')}

    def board_round(self,mid,guild,channel):
        row=self.db.execute('SELECT id FROM wordle_rounds WHERE message=? AND guild=? AND channel=?',
                            (str(mid),str(guild),str(channel))).fetchone()
        return row['id'] if row else None

    def active_boards(self):
        return self.db.execute("SELECT * FROM wordle_rounds WHERE state='playing' AND message IS NOT NULL").fetchall()

    def prune(self,now=None):
        cutoff=(time.time() if now is None else now)-30*86400
        with self.db:
            self.db.execute("DELETE FROM wordle_guesses WHERE round IN (SELECT id FROM wordle_rounds WHERE state!='playing' AND updated<?)",(cutoff,))
            self.db.execute("DELETE FROM wordle_rounds WHERE state!='playing' AND updated<?",(cutoff,))


def board_text(game,full=False):
    mode='超级 Wordle' if game['size']==7 else 'Wordle'
    guesses=game['guesses']
    left=game['max_tries']-len(guesses)
    state={'playing':'进行中','won':'全频道猜中啦！','lost':'机会用完啦','stopped':'已提前结束'}[game['state']]
    text=f'🐳 {mode} #{game["id"]} · {state}\n{game["size"]} 字母 · 已用 {len(guesses)}/{game["max_tries"]} 次 · 全频道还剩 {left} 次。'
    if game['state']!='playing':
        text+=f'\n答案：**{game["answer"].upper()}**'
        names=list(dict.fromkeys(g['name'] for g in guesses))
        if names:
            text+='\n参与群友：'+'、'.join(names)
    elif guesses:
        text+=f'\n最近：{guesses[-1]["name"]} → **{guesses[-1]["word"].upper()}**'
    if full:
        for i,g in enumerate(guesses,1):
            text+=f'\n{i:02}. `{g["word"].upper()}` '+''.join(MARKS[m] for m in g['marks'])+' · '+g['name']
        if game['keyboard']:
            for mark,label in ((2,'位置正确'),(1,'存在'),(0,'无剩余匹配')):
                letters=' '.join(k.upper() for k,v in sorted(game['keyboard'].items()) if v==mark)
                if letters:
                    text+=f'\n{MARKS[mark]} {label}：{letters}'
    if game['state']=='playing':
        text+='\n点击「提交猜测」；🟩 位置正确 · 🟨 位置不同 · ⬛ 无剩余匹配。'
    return text[:1850]


def render_board(game,avatars=None):
    """Render only the visible state into a small PNG in memory, no AI or network."""
    from PIL import Image,ImageChops,ImageDraw,ImageFont,ImageOps
    chinese=False
    font_file=None
    for path in ('C:/Windows/Fonts/msyh.ttc','C:/Windows/Fonts/msyhbd.ttc',
                 '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'):
        if Path(path).is_file():
            font_file=path
            chinese='msyh' in path
            break
    def font(size):
        return ImageFont.truetype(font_file,size) if font_file else ImageFont.load_default(size=size)
    size,rows=game['size'],game['max_tries']
    tile=50 if size==7 else 66
    gap=6
    row_gap=23
    width=560
    header=110
    grid_height=rows*(tile+row_gap)-row_gap+17
    keyboard_y=header+grid_height+25
    height=keyboard_y+165
    im=Image.new('RGB',(width,height),'#142238')
    draw=ImageDraw.Draw(im)
    def centered(text,y,face,fill='#f3f7ff'):
        draw.text((width/2,y),text,font=face,fill=fill,anchor='mt')
    centered('SUPER WORDLE' if size==7 else 'WORDLE',16,font(29))
    used=len(game['guesses'])
    centered((f'全频道合作 · {size} 字母 · {used}/{rows} 次' if chinese else
              f'TEAM PLAY  /  {size} LETTERS  /  {used} OF {rows}'),58,font(17), '#c4d3e7')
    palette={-1:'#24334b',0:'#4c5668',1:'#b98924',2:'#20866c'}
    grid_x=(width-(size*tile+(size-1)*gap))/2
    avatar_size=44 if size==7 else 52
    # Larger mask first gives smooth circular edges after downsampling.
    mask=Image.new('L',(avatar_size*4,avatar_size*4),0)
    ImageDraw.Draw(mask).ellipse((0,0,mask.width-1,mask.height-1),fill=255)
    mask=mask.resize((avatar_size,avatar_size),Image.Resampling.LANCZOS)
    for row in range(rows):
        guess=game['guesses'][row] if row<used else None
        y=header+row*(tile+row_gap)
        if guess:
            draw.rounded_rectangle((23,y-5,width-23,y+tile+17),radius=12,fill='#1a2d46',
                outline='#3b526c' if row==used-1 else '#1a2d46',width=1)
        draw.text((13,y+tile/2),str(row+1),font=font(11),fill='#a8b8d0',anchor='mm')
        if guess:
            ax,ay=28,round(y+(tile-avatar_size)/2)
            thumb=None
            data=(avatars or {}).get(guess.get('avatar'))
            if data:
                try:
                    with Image.open(io.BytesIO(data)) as source:
                        if source.width<=512 and source.height<=512:
                            thumb=ImageOps.fit(source.convert('RGBA'),(avatar_size,avatar_size),
                                               method=Image.Resampling.LANCZOS)
                except Exception:
                    pass
            if thumb is None:
                # A neutral silhouette also works for old rows and failed downloads.
                thumb=Image.new('RGBA',(avatar_size,avatar_size),'#405775')
                icon=ImageDraw.Draw(thumb)
                d=avatar_size
                icon.ellipse((d*.34,d*.18,d*.66,d*.5),fill='#d8e4f4')
                icon.ellipse((d*.19,d*.57,d*.81,d*1.13),fill='#d8e4f4')
            # Preserve any source transparency within the round clip.
            alpha=ImageChops.multiply(thumb.getchannel('A'),mask)
            im.paste(thumb,(ax,ay),alpha)
            draw.ellipse((ax-1,ay-1,ax+avatar_size,ay+avatar_size),outline='#91aaca',width=1)
        for col in range(size):
            x=grid_x+col*(tile+gap)
            mark=guess['marks'][col] if guess else -1
            draw.rounded_rectangle((x,y,x+tile,y+tile),radius=7,fill=palette[mark],
                                   outline='#43526b' if not guess else palette[mark],width=2)
            if guess:
                draw.text((x+tile/2,y+tile/2-1),guess['word'][col].upper(),
                          font=font(27 if size==7 else 35),fill='white',anchor='mm')
        if guess:
            label=guess['name']
            caption=font(11)
            available=size*tile+(size-1)*gap
            while label and draw.textlength(label,font=caption)>available-16:
                label=label[:-1]
            draw.text((grid_x,y+tile+3),label,font=caption,fill='#c4d3e7')
    for row,letters in enumerate(('qwertyuiop','asdfghjkl','zxcvbnm')):
        key_width,key_gap=42,6
        x0=(width-(len(letters)*(key_width+key_gap)-key_gap))/2
        y=keyboard_y+row*35
        for i,letter in enumerate(letters):
            x=x0+i*(key_width+key_gap)
            draw.rounded_rectangle((x,y,x+key_width,y+29),radius=5,
                fill=palette[game['keyboard'].get(letter,-1)])
            draw.text((x+key_width/2,y+14),letter.upper(),font=font(16),fill='white',anchor='mm')
    state=game['state']
    footer=(f'还剩 {rows-used} 次 · 点击按钮提交猜测' if chinese else f'{rows-used} GUESSES LEFT  /  USE THE GUESS BUTTON')
    if state!='playing':
        title={'won':'合作成功','lost':'机会用完','stopped':'本局结束'}[state] if chinese else state.upper()
        footer=title+'  /  '+game['answer'].upper()
    centered(footer,keyboard_y+118,font(17))
    buffer=io.BytesIO()
    im.save(buffer,format='PNG',optimize=True)
    return buffer.getvalue()
