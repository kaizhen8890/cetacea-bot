import asyncio
import json
import random
import re
import time
from collections import defaultdict, deque
import aiohttp

def clip(text, size):
    return text if len(text) <= size else text[:size] + '…[已截短]'

def safe_text(text):
    text = re.sub(r'sk-[A-Za-z0-9_-]{16,}', '[密钥已隐藏]', text)
    return text

def explanation_request(text):
    """Choose a longer answer only for an explicit request to learn or explain."""
    return bool(re.search(r'解释|讲解|讲讲|教我|科普|原理|为什么|怎么理解|什么意思|如何理解|怎么做|怎么解|什么是|是什么|详细说', text))

EMOTE_NAMES = ('tang_love', 'saya_ok', 'tang_ku', 'mao_tounaofengbao', 'tang_ha')

def choose_emote(text, available, probability, roll=None):
    """Pick at most one vetted server emoji without spending a model call."""
    if not available or (random.random() if roll is None else roll) >= probability:
        return ''
    if any(word in text for word in ('自杀','轻生','去世','死亡','重病','住院','癌症',
            '抑郁','焦虑','报警','受伤','事故','火灾','家暴','分手','失恋','难过','伤心','痛苦')):
        return ''
    if any(word in text for word in ('喜欢','开心','好耶','可爱','谢谢','好棒','爱你')):
        names=('tang_love','saya_ok')
    elif any(word in text for word in ('哭','呜','累死')):
        names=('tang_ku',)
    elif any(word in text for word in ('懵','惊','不懂','怎么会','真的假的')):
        names=('mao_tounaofengbao','tang_ha')
    else:
        names=('saya_ok','tang_ha')
    return next((available[name] for name in names if name in available),'')

def cost_rmb(c, prompt, completion):
    return ((prompt*c['input_usd_per_million'] + completion*c['output_usd_per_million'])
            / 1_000_000 * c['usd_to_rmb'] * c['cost_margin'])

class APIError(Exception):
    pass

class Context:
    def __init__(self, c):
        self.c = c
        self.items = defaultdict(lambda: deque(maxlen=c['context_messages']))

    def add(self, channel, mid, uid, name, text, role='user', now=None):
        self.items[channel].append(dict(mid=mid, uid=uid, name=clip(name,32),
            text=clip(safe_text(text),self.c['context_chars_per_message']), role=role,
            time=time.monotonic() if now is None else now))

    def remove(self, channel, mid):
        self.items[channel] = deque((r for r in self.items[channel] if r['mid'] != mid),
                                   maxlen=self.c['context_messages'])

    def forget_user(self, channel, uid):
        self.items[channel] = deque((r for r in self.items[channel] if r['uid'] != uid),
                                   maxlen=self.c['context_messages'])

    def build(self, channel, current_ids, name, text, memories, owner=False,
              recent=None, reference=None, explain=False, continuation=False,
              auto_memories=None):
        c = self.c
        now = time.monotonic()
        guidance = ('\n本次是解释或教学请求：认真回答问题，必要时分段说明，可以写长一些。'
                    if explain else '\n本次是日常聊天：尽量只回一句，约3—20个汉字。')
        if continuation:
            guidance += ('\n本次只是短时间内同一位群友的新消息，未必在和你说话。'
                         '只有明显接续与你的对话、向你提问或请你做事时才回答；'
                         '同一人紧接你上一句发“物理”这样的简短主题词，应视为续聊并回答。'
                         '给别人说的话、对全群的告知、'
                         '自言自语或无关新话题都只输出 [[NO_REPLY]]，不要添加别的字。')
        messages = [{'role':'system', 'content':c['persona'] +
                    ('\n当前发言者身份：主人。' if owner else '\n当前发言者身份：普通群友。') + guidance}]
        for row in (self.items[channel] if recent is None else recent):
            if row['mid'] in current_ids or (recent is None and now-row['time'] > c['context_ttl_seconds']):
                continue
            body = row['text'] if row['role']=='assistant' else json.dumps(
                {'群友':row['name'],'发言':row['text']}, ensure_ascii=False)
            messages.append({'role':row['role'],'content':body})
        payload = {'群友':clip(name,32),'本服务器中该群友主动保存且当前可用的记忆':memories,
                   '当前发言':clip(safe_text(text),c['input_chars'])}
        if auto_memories:
            payload['相关旧事']=[{'来源':'亲自交谈' if x['kind']!='glance' else '旁观印象',
                              '内容':clip(x['text'],200)} for x in auto_memories]
        if reference:
            payload['正在回复的消息'] = reference
        if continuation:
            payload['会话状态'] = '同一位群友在你上一句之后再次发言，尚未确定是否在和你说话'
        messages.append({'role':'user','content':json.dumps(payload,ensure_ascii=False)})
        while prompt_bytes(messages) > c['max_prompt_bytes'] and len(messages)>2:
            messages.pop(1)
        if prompt_bytes(messages)>c['max_prompt_bytes'] and '相关旧事' in payload:
            payload.pop('相关旧事')
            messages[-1]['content']=json.dumps(payload,ensure_ascii=False)
        if prompt_bytes(messages)>c['max_prompt_bytes']:
            payload['本服务器中该群友主动保存且当前可用的记忆'] = []
            messages[-1]['content']=json.dumps(payload,ensure_ascii=False)
        if prompt_bytes(messages)>c['max_prompt_bytes']:
            raise APIError('人设或消息太长，请缩短后重试。')
        return messages

def prompt_bytes(messages):
    # Deliberately conservative bound for byte-level tokenizers, plus framing allowance.
    return sum(len(m['content'].encode('utf-8'))+64 for m in messages)+128

class Policy:
    def __init__(self,c):
        self.c=c
        self.last=defaultdict(lambda:float('-inf'))
        self.count=defaultdict(int)

    def observe(self,channel):
        self.count[channel]+=1

    def eligible(self,channel,text,now=None,roll=None):
        now=time.monotonic() if now is None else now
        if self.count[channel]<self.c['casual_min_messages'] or now-self.last[channel]<self.c['casual_cooldown_seconds']:
            return False
        if len(text)<2 or text.startswith(('!','/','http')):
            return False
        probability=self.c['casual_probability']
        if any(w in text for w in ('鲸鱼','米饭','吃什么','好饿')):
            probability=min(probability*3,0.25)
        return (random.random() if roll is None else roll)<probability

    def replied(self,channel,now=None):
        self.last[channel]=time.monotonic() if now is None else now
        self.count[channel]=0

class ConversationTracker:
    """A brief, per-channel dialogue with the person the bot just answered."""
    ACKS={'嗯','哦','噢','好','好的','行','可以','谢谢','谢了','收到','知道了',
          '哈哈','哈哈哈','hhh','ok','okay','666','啊'}

    def __init__(self,seconds):
        self.seconds=seconds
        self.active={}

    def mark_reply(self,channel,uid,mid=0,now=None):
        self.active[channel]=(uid,time.monotonic() if now is None else now,mid)

    def clear(self,channel):
        self.active.pop(channel,None)

    def remove_reply(self,channel,mid):
        row=self.active.get(channel)
        if row and row[2]==mid:
            self.clear(channel)

    def is_followup(self,channel,uid,text,now=None,reply_to_other=False,mention_other=False):
        row=self.active.get(channel)
        if not row:
            return False
        now=time.monotonic() if now is None else now
        if now-row[1]>self.seconds or uid!=row[0] or reply_to_other or mention_other:
            self.clear(channel)
            return False
        clean=text.strip().lower().strip('。！!？?~～ ')
        if clean.startswith(('!','/','http')):
            self.clear(channel)
            return False
        if re.search(r'^(?:大家|各位|群友们)[，,:：]|(?:跟|和|对)大家说',clean):
            self.clear(channel)
            return False
        if (not clean or clean in self.ACKS or
                re.fullmatch(r'<a?:[^>]+:\d+>',clean) or not re.search(r'\w',clean)):
            return False
        return True

class LLM:
    def __init__(self,c,store,session):
        self.c,self.store,self.session=c,store,session
        self.lock=asyncio.Lock()
        self.blocked_until=0.0
        self.failures=0

    async def chat(self,messages,kind='mention',max_tokens=None):
        async with self.lock:
            if time.monotonic()<self.blocked_until:
                raise APIError('接口暂时休息中，稍后再叫我吧。')
            c=self.c
            max_tokens=c['max_output_tokens'] if max_tokens is None else max_tokens
            if not isinstance(max_tokens,int) or not 0<max_tokens<=1024:
                raise APIError('输出长度设置无效。')
            bound=prompt_bytes(messages)
            if bound>c['max_prompt_bytes']:
                raise APIError('本次上下文超过节省模式限制。')
            call_id=self.store.reserve(cost_rmb(c,bound,max_tokens),kind,c)
            try:
                async with self.session.post(c['api_base'].rstrip('/')+'/chat/completions',
                    headers={'Authorization':'Bearer '+c['api_key']},
                    json={'model':c['model'],'messages':messages,'max_tokens':max_tokens,
                          'temperature':0.8,'stream':False,'thinking':{'type':'disabled'}},
                    timeout=aiohttp.ClientTimeout(total=c['api_timeout_seconds']),
                    allow_redirects=False) as response:
                    if response.status!=200:
                        raise APIError(f'模型接口返回 HTTP {response.status}，本次不自动重试。')
                    data=await response.json()
                usage=data.get('usage') or {}
                pi,po=usage.get('prompt_tokens'),usage.get('completion_tokens')
                valid=lambda v: type(v) is int and v>=0
                if valid(pi) and valid(po):
                    self.store.finish(call_id,cost_rmb(c,pi,po),pi,po)
                else:
                    self.store.finish(call_id,status='usage_missing')
                choice=data['choices'][0]
                content=choice['message'].get('content')
                if not isinstance(content,str) or not content.strip():
                    raise APIError('模型没有返回可显示的文字，本次不自动重试。')
                content=re.sub(r'<think>.*?</think>','',content,flags=re.S).strip()
                if '<think>' in content:
                    content=content.split('<think>')[0].strip()
                if not content:
                    raise APIError('模型只返回了思考内容，请检查非思考模式设置。')
                self.failures=0
                if choice.get('finish_reason')=='length':
                    content+='\n（这次触及短回复上限啦，可以让我继续。）'
                return safe_text(content)
            except BaseException as exc:
                self.store.finish(call_id,status='failed')
                if isinstance(exc,asyncio.CancelledError):
                    raise
                self.failures+=1
                self.blocked_until=time.monotonic()+min(300,30*2**min(self.failures-1,4))
                if isinstance(exc,APIError):
                    raise
                raise APIError('模型接口暂时无法使用，本次不自动重试。') from None
