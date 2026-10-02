"""Budgeted AI Wordle player. The model sees only public feedback and word lists."""
import asyncio
import json
import logging
import re
import time
from collections import Counter
from dataclasses import dataclass
import discord
from engine import APIError,MessageChanged
from storage import BudgetExceeded
from wordle import vocabulary,score

LOG=logging.getLogger('cetacea')
LOW_THINKING={'thinking':{'type':'enabled'},'reasoning_effort':'low'}


def public_board(game):
    # Deliberately project by whitelist; neither the answer nor stored user data
    # is passed to candidate selection or the model.
    return {'round':game['id'],'size':game['size'],
            'remaining':game['max_tries']-len(game['guesses']),
            'guesses':[{'word':g['word'],'marks':list(g['marks'])} for g in game['guesses']]}


def choices_for(board):
    allowed,answers=vocabulary(board['size'])
    guessed={g['word'] for g in board['guesses']}
    candidates=[word for word in answers if word not in guessed and all(
        score(word,g['word'])==g['marks'] for g in board['guesses'])]
    if not candidates:
        raise ValueError('公开反馈与本地词库不一致，代玩已暂停；没有扣猜测次数。')
    letters=Counter(ch for word in candidates for ch in set(word))
    positions=Counter((i,ch) for word in candidates for i,ch in enumerate(word))
    total=len(candidates)
    def rank(word):
        return (sum((letters[ch]/total)*(1-letters[ch]/total) for ch in set(word))
                +0.5*sum((positions[i,ch]/total)*(1-positions[i,ch]/total) for i,ch in enumerate(word)))
    ranked=sorted(candidates,key=lambda w:(-rank(w),w))
    possible=ranked[:24 if total<=24 else 16]
    probes=[]
    if total>24:
        probes=sorted((w for w in allowed if w not in guessed and w not in possible),
                      key=lambda w:(-rank(w),w))[:8]
    return {'remaining_candidates':total,'possible_answers':possible,'probe_words':probes}


def guess_messages(board,choices):
    return [{'role':'system','content':
        '你是鲸鱼娘，正在公平地玩英文 Wordle。只根据公开棋盘反馈推理，不能读取隐藏答案。'
        'marks: 2=字母和位置正确，1=字母存在但位置不对，0=没有剩余该字母；重复字母按实际数量计算。'
        'possible_answers 是仍满足所有反馈的候选答案，probe_words 是可用来排除候选的探测词。'
        '两个词表已在本机按信息量降序排列；不必重新计算频率、枚举假想答案或逐词长篇分析。'
        '简短判断后输出：机会少时优先候选答案，候选很多时可选探测词。'
        '必须从提供的两个词表中选择一个，仅输出一个小写英文单词，不加解释、标点或代码块。'},
        {'role':'user','content':json.dumps({'board':board,**choices},ensure_ascii=False,separators=(',',':'))}]


def selected_word(result,choices,size):
    word=str(result).strip().lower()
    if (getattr(result,'truncated',False) or not re.fullmatch(rf'[a-z]{{{size}}}',word)
            or word not in choices['possible_answers']+choices['probe_words']):
        raise ValueError('模型没有返回可用的候选词，代玩已暂停；本次没有扣猜测次数，也不会自动重试 API。')
    return word


@dataclass
class PlaySession:
    guild:int
    channel:object
    owner:int
    round:int
    request:str
    steps:int
    task:object=None


class WordlePlayer:
    def __init__(self,service):
        self.service=service
        self.bot=service.bot
        self.sessions={}
        self.closed=False

    def guard(self,guild,channel):
        if self.closed or not self.service.allowed(guild,channel):
            raise ValueError('此频道尚未启用鲸鱼娘。')
        if not self.bot.local.enabled(guild,'wordle'):
            raise ValueError('本服务器的 Wordle 已关闭。')
        if not self.bot.c.get('chat_enabled',True) or self.bot.llm is None:
            raise ValueError('云端聊天已关闭；群友仍可以本地猜 Wordle。')
        if self.bot.store.pref(f'paused:{channel.id}')=='1':
            raise ValueError('本频道聊天已暂停；恢复后再让鲸鱼娘代玩。')

    async def start(self,guild,channel,user,mode='当前',scope='整局',request=''):
        self.guard(guild,channel)
        if mode not in ('当前','普通','超级') or scope not in ('整局','一步'):
            raise ValueError('用法：wordle 自己玩 [当前/普通/超级] [整局/一步]。')
        key=(guild,channel.id)
        async with self.service.locks[key]:
            self.guard(guild,channel)
            if key in self.sessions:
                raise ValueError('我已经在猜这局啦；可以用 wordle 停止代玩 暂停。')
            if len(self.sessions)>=2:
                raise ValueError('鲸鱼娘正在其他频道猜词，等一局结束再来。')
            game=self.service.db.latest(guild,channel.id)
            if game and game['state']=='playing':
                if mode!='当前' and game['size']!=(5 if mode=='普通' else 7):
                    raise ValueError('当前局模式不同；请先完成或结束当前局。')
            else:
                game=self.service.db.start(guild,channel.id,user,'普通' if mode=='当前' else mode)
            steps=1 if scope=='一步' else game['max_tries']-len(game['guesses'])
            session=PlaySession(guild,channel,user,game['id'],str(request),steps)
            # Reserve a slot before the first await, including across channels.
            self.sessions[key]=session
            try:
                link=await self.service.update(channel,game)
                if self.sessions.get(key) is not session:
                    raise ValueError('代玩已停止，棋盘进度保留。')
                self.guard(guild,channel)
                session.task=asyncio.create_task(self.play(session))
            except BaseException:
                if self.sessions.get(key) is session:
                    self.sessions.pop(key,None)
                raise
            return (f'🐳 我来猜！thinking low · {"只猜一步" if scope=="一步" else "一直猜到本局结束"}。'
                    f'最多调用模型 {steps} 次，计入每日 API 预算，群友仍共用剩余机会。'
                    f'用 `!鲸鱼 wordle 停止代玩` 可停止。[查看棋盘]({link})')

    def valid(self,session,board=None):
        key=(session.guild,session.channel.id)
        if self.sessions.get(key) is not session:
            return False
        try:
            self.guard(session.guild,session.channel)
        except ValueError:
            return False
        game=self.service.db.latest(*key)
        return bool(game and game['id']==session.round and game['state']=='playing'
                    and (board is None or public_board(game)==board))

    def halt(self,guild,channel):
        session=self.sessions.pop((guild,channel),None)
        if session and session.task:
            session.task.cancel()
        return session

    def stop(self,guild,channel,user,admin=False):
        session=self.sessions.get((guild,channel))
        if not session:
            return '本频道没有正在进行的 AI 代玩。'
        game=self.service.db.latest(guild,channel)
        if not admin and user!=session.owner and (not game or str(user)!=game['owner']):
            raise ValueError('只有代玩发起人、本局发起人或管理员可以停止代玩。')
        self.halt(guild,channel)
        return '代玩已停止，棋盘进度保留，群友可继续猜。已经发出的 API 请求仍可能计费。'

    async def notice(self,session,text):
        try:
            sent=await session.channel.send(text,allowed_mentions=discord.AllowedMentions.none())
            self.bot.local.db.remember_reply(sent.id,session.guild,session.channel.id,'wordle 状态')
        except discord.HTTPException:
            LOG.warning('Wordle AI 提示暂未送达；棋盘进度保留')

    async def play(self,session):
        key=(session.guild,session.channel.id)
        accepted=stale=0
        try:
            for attempt in range(session.steps):
                if not self.valid(session):
                    break
                game=self.service.db.latest(*key)
                board=public_board(game)
                previous=next((g for g in reversed(game['guesses']) if g['user']==str(self.bot.user.id)),None)
                wait=max(0,3.05-(time.time()-previous['created'])) if previous else 0
                if wait:
                    await asyncio.sleep(wait)
                choices=await asyncio.to_thread(choices_for,board)
                if not self.valid(session,board):
                    stale+=1
                    continue
                try:
                    result=await self.bot.llm.chat(guess_messages(board,choices),'wordle_ai',
                        max_tokens=self.bot.c.get('wordle_ai_max_tokens',4096),
                        extra_body=self.bot.c.get('wordle_ai_extra_body',LOW_THINKING),
                        is_current=lambda:self.valid(session,board))
                except MessageChanged:
                    stale+=1
                    continue
                async with self.service.locks[key]:
                    if not self.valid(session,board):
                        stale+=1
                        continue
                    word=selected_word(result,choices,board['size'])
                    game=self.service.db.guess(*key,self.bot.user.id,'鲸鱼娘 · AI',word,
                        f'ai:{session.request}:{session.round}:{attempt}',expected=session.round)
                    accepted+=1
                    if self.service.can_attach(session.channel):
                        await self.service.capture_avatars(session.channel,game,
                            getattr(session.channel.guild,'me',None) or self.bot.user)
                    await self.service.update(session.channel,game)
                if game['state']!='playing':
                    break
            game=self.service.db.latest(*key)
            state=game['state'] if game and game['id']==session.round else 'changed'
            text={'won':'这局猜中啦！','lost':'机会用完啦，答案已在棋盘揭晓。',
                  'playing':'本次代玩已结束，群友可继续，或再叫我自己玩。',
                  'stopped':'这局已结束。','changed':'棋盘已换成新的一局，本次代玩结束。'}[state]
            await self.notice(session,f'🐳 已提交 {accepted} 次猜测。{text}'+
                ('期间棋盘有更新，过时的猜测已丢弃。' if stale else ''))
        except (APIError,BudgetExceeded,ValueError) as exc:
            await self.notice(session,'🐳 代玩暂停：'+str(exc)+' 棋盘进度保留。')
        except discord.HTTPException:
            await self.notice(session,'🐳 棋盘暂时无法同步，代玩已暂停；进度保留，用 wordle 状态 重试。')
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.warning('Wordle AI 暂停：%s',type(exc).__name__)
            await self.notice(session,'🐳 代玩暂未完成，已暂停；棋盘进度保留。')
        finally:
            if self.sessions.get(key) is session:
                self.sessions.pop(key,None)

    async def close(self):
        self.closed=True
        tasks=[s.task for s in self.sessions.values() if s.task]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks,return_exceptions=True)
        self.sessions.clear()
