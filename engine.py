import asyncio
import json
import random
import re
import time
from collections import defaultdict, deque
import aiohttp
from provider import provider_settings,api_headers,chat_payload

def clip(text, size):
    return text if len(text) <= size else text[:size] + '…[已截短]'

def safe_text(text):
    text = re.sub(r'sk-[A-Za-z0-9_-]{16,}', '[密钥已隐藏]', text)
    return text

def join_fragments(parts):
    """Rejoin tiny Chinese fragments without flattening separate full sentences."""
    result=[]
    previous=''
    for part in parts:
        part=part.strip()
        if not part:
            continue
        joined=(len(previous)<=2 and len(part)<=2 and
                re.search(r'[\u3400-\u9fff]$',previous) and
                re.match(r'[\u3400-\u9fff]',part))
        if result and not joined:
            result.append('\n')
        result.append(part)
        previous=part
    return ''.join(result)


def compact_history(rows,max_chars,gap_seconds=8):
    """One context slot per contiguous, recent utterance by the same speaker."""
    groups=[]
    for row in rows:
        previous=groups[-1] if groups else None
        if (previous and row['role']=='user' and previous['role']=='user'
                and row['uid']==previous['uid'] and not row.get('break_before',False)
                and 0<=row['time']-previous['time']<=gap_seconds
                and len(previous['parts'])<32
                and len(join_fragments(previous['parts']+[row['text']]))<=max_chars):
            previous['parts'].append(row['text'])
            previous['time']=row['time']
        else:
            groups.append(dict(row,parts=[row['text']]))
    return [dict(row,text=join_fragments(row['parts'])) for row in groups]


class MessageChanged(Exception):
    """More text arrived while this request was waiting for the cloud-call lock."""


class MessageBurst:
    """A bounded per-speaker queue that waits for silence, with a maximum delay."""
    def __init__(self,c):
        self.c=c
        self.items=[]
        self.changed=asyncio.Event()
        self.started=self.updated=0.0
        self.version=0
        self.known=set()
        self.deleted=set()
        self.inflight=set()

    def append(self,item):
        if len(self.items)+len(self.inflight)>=self.c.get('reply_batch_max_messages',32):
            return False
        now=time.monotonic()
        if not self.items:
            self.started=now
        self.updated=now
        self.items.append(item)
        self.known.add(item[0].id)
        self.version+=1
        self.changed.set()
        return True

    async def wait(self):
        while self.items:
            # Single words/characters are more likely to have another fragment coming.
            quiet=self.c['reply_delay_seconds']*(1.5 if len(self.items[-1][1])<=4 else 1)
            deadline=min(self.started+self.c.get('reply_batch_max_wait_seconds',12),
                         self.updated+quiet)
            remaining=deadline-time.monotonic()
            if remaining<=0:
                return
            self.changed.clear()
            try:
                await asyncio.wait_for(self.changed.wait(),remaining)
            except TimeoutError:
                continue

    def take(self):
        items,self.items=self.items,[]
        self.inflight={item[0].id for item in items}
        return items

    def restore(self,items):
        self.items=[item for item in items+self.items if item[0].id not in self.deleted]
        self.inflight.clear()
        self.changed.set()

    def complete(self):
        self.inflight.clear()

    def remove(self,mid):
        if mid in self.known:
            self.deleted.add(mid)
            self.inflight.discard(mid)
            self.items=[item for item in self.items if item[0].id!=mid]
            self.version+=1
            self.changed.set()

def explanation_request(text):
    """Recognize teaching intent locally, including vocabulary and study follow-ups."""
    return bool(re.search(
        r'解释|讲解|讲讲|教(?:我|我们|大家|一下|一教)|教导|教学|辅导|指导|请教|求教|科普|原理|解题|解答|求解|语法|造句|为什么|为何|什么意思|什么是|是什么'
        r'|(?:怎么|怎样|如何).{0,40}(?:理解|做|解|用|算|写|读|念|区别|区分|判断|表达|证明|推导|求|学习|学会)'
        r'|详细(?:说|讲|点|些|一点)|说清楚|讲清楚|展开(?:说|讲)|举(?:个|一)?例(?:子)?'
        r'|(?:不太|不|没|没听|没看)(?:懂|理解|明白)|同义词|近义词|反义词|词义|释义|用法'
        r'|(?:有什么|有哪些|还有哪些|有没有).{0,60}(?:代替|替代|取代|词|说法|方法|语法)'
        r'|(?:可以|能否|能不能|能用|可不可以).{0,60}(?:代替|替代|取代)'
        r'|(?:有什么|有何|什么|哪些).{0,40}(?:区别|差别|不同|含义|意思)'
        r'|(?:帮我|给我|请|能否|可以).{0,40}(?:解题|解这|解一下|(?:做|解).{0,6}题|分析|推导|证明|计算|检查|纠错|批改|答疑|学习|讲|步骤|题怎么)'
        r'|(?:这题|这道题|这个句子|这句|这一步|这个答案).{0,30}(?:对吗|正确吗|错了|错在哪|有错吗|怎么|如何)'
        r'|\b(?:explain|teach|meaning|synonym|antonym|define|definition|example)\b'
        r'|\b(?:how\s+(?:do|does|can|to)|what\s+(?:is|are))\b',text,re.I))

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
    input_price=c.get('input_price_per_million',c.get('input_usd_per_million'))
    output_price=c.get('output_price_per_million',c.get('output_usd_per_million'))
    exchange=c['usd_to_rmb'] if c.get('pricing_currency','USD')=='USD' else 1
    return ((prompt*input_price + completion*output_price)
            / 1_000_000 * exchange * c['cost_margin'])

class APIError(Exception):
    pass


class ModelReply(str):
    def __new__(cls,text,truncated=False):
        value=super().__new__(cls,text)
        value.truncated=truncated
        return value

class Context:
    def __init__(self, c):
        self.c = c
        self.raw_limit=c['context_messages']*32
        self.items = defaultdict(lambda: deque(maxlen=self.raw_limit))

    def add(self, channel, mid, uid, name, text, role='user', now=None,break_before=False):
        self.items[channel].append(dict(mid=mid, uid=uid, name=clip(name,32),
            text=clip(safe_text(text),self.c['context_chars_per_message']), role=role,
            time=time.monotonic() if now is None else now,break_before=break_before))

    def remove(self, channel, mid):
        self.items[channel] = deque((r for r in self.items[channel] if r['mid'] != mid),
                                   maxlen=self.raw_limit)

    def forget_user(self, channel, uid):
        self.items[channel] = deque((r for r in self.items[channel] if r['uid'] != uid),
                                   maxlen=self.raw_limit)

    def build(self, channel, current_ids, name, text, memories, owner=False,
              recent=None, reference=None, explain=False, continuation=False,
              auto_memories=None,longform=False):
        c = self.c
        now = time.monotonic()
        guidance = ('\n当前发言决定本次要回答的问题或完成的任务；历史发言、引用和记忆用于理解背景。'
                    '同一群友的连续分段消息应结合完整意思理解，单字拆开的词不要逐字回答。' +
                    ('\n本次是解释或教学请求：积极、耐心地完整教导，不受日常短回复要求限制。'
                     '先直接回答本轮新问题，再按需要说明理由、步骤或例子；简单问题简明回答，复杂问题讲完整。'
                     '不要机械复述上一轮答案，除非当前明确要求重复。'
                    if explain or longform else '\n本次是日常聊天：尽量只回一句，约3—20个汉字。'))
        if longform:
            guidance += ('\n本次是写作、翻译、全文输出或完整讲解任务：不受日常3—20字的要求限制，'
                         '按用户要求的长度完成正文；正文使用任务所需语言，外语作文和翻译保留外语。'
                         '直接完成任务，不凑篇幅、不添加无关人设玩笑。'
                         '任务优先于懒散和嘴硬的人设，不能用太长、懒得写、背不动等人设理由推掉请求；'
                         '此前回复若用这种理由推掉任务，应纠正并完成，不要重复该回复。'
                         '若确实无法确定原文或事实，说明具体的不确定之处，不编造原文、不把片段称为全文。')
        if reference:
            guidance += ('\n本次是明确回复：当前发言明确提出新问题或要求时，回答这个新问题，'
                         '即使话题相同，也不要把被引用的旧问题再答一遍。'
                         '只有当前发言为纯表情、简短反应或指代不明时，“正在回复的消息”才是理解反应的主要对象，'
                         '不要把对引用的反应套到其他频道话题上。上一级引用只说明来龙去脉，不是本轮待答的问题。'
                         '引用内容不可用且意思不明时，简短问清楚。')
        if continuation:
            guidance += ('\n本次只是短时间内同一位群友的新消息，未必在和你说话。'
                         '只有明显接续与你的对话、向你提问或请你做事时才回答；'
                         '同一人紧接你上一句发“物理”这样的简短主题词，应视为续聊并回答。'
                         '给别人说的话、对全群的告知、'
                         '自言自语或无关新话题都只输出 [[NO_REPLY]]，不要添加别的字。')
        messages = [{'role':'system', 'content':c['persona'] +
                    ('\n当前发言者身份：主人。' if owner else '\n当前发言者身份：普通群友。') + guidance}]
        raw=[row for row in (self.items[channel] if recent is None else recent)
             if row['mid'] not in current_ids and
             (recent is not None or now-row['time']<=c['context_ttl_seconds'])]
        for row in compact_history(raw,c['context_chars_per_message'])[-c['context_messages']:]:
            body = row['text'] if row['role']=='assistant' else json.dumps(
                {'群友':row['name'],'发言':row['text']}, ensure_ascii=False)
            messages.append({'role':row['role'],'content':body})
        payload = {'群友':clip(name,32),'本服务器中该群友主动保存且当前可用的记忆':memories}
        if auto_memories:
            payload['相关旧事']=[{'来源':'亲自交谈' if x['kind']!='glance' else '旁观印象',
                              '内容':clip(x['text'],200)} for x in auto_memories]
        if reference:
            payload['正在回复的消息'] = reference
        if continuation:
            payload['会话状态'] = '同一位群友在你上一句之后再次发言，尚未确定是否在和你说话'
        # Put the active request after background, so quoted old questions cannot
        # become the newest apparent instruction in the user message.
        payload['当前发言']=safe_text(text) if longform else clip(safe_text(text),c['input_chars'])
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
        self.c,self.store,self.session=provider_settings(c),store,session
        self.lock=asyncio.Lock()
        self.blocked_until=0.0
        self.failures=0

    async def chat(self,messages,kind='mention',max_tokens=None,is_current=None,extra_body=None):
        async with self.lock:
            if is_current is not None and not is_current():
                raise MessageChanged()
            if time.monotonic()<self.blocked_until:
                raise APIError('接口暂时休息中，稍后再叫我吧。')
            c=self.c
            max_tokens=c['max_output_tokens'] if max_tokens is None else max_tokens
            if not isinstance(max_tokens,int) or not 0<max_tokens<=8192:
                raise APIError('输出长度设置无效。')
            bound=prompt_bytes(messages)
            if bound>c['max_prompt_bytes']:
                raise APIError('本次上下文超过节省模式限制。')
            payload=chat_payload(c,messages,max_tokens,extra_body=extra_body)
            call_id=self.store.reserve(cost_rmb(c,bound,max_tokens),kind,c)
            try:
                async with self.session.post(c['api_base'].rstrip('/')+'/chat/completions',
                    headers=api_headers(c),
                    json=payload,
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
                    if choice['message'].get('reasoning_content'):
                        raise APIError('模型只返回了思考内容，没有最终答案；请提高该功能的输出额度，或检查接口的思考设置。本次不自动重试。')
                    raise APIError('模型没有返回可显示的文字，本次不自动重试。')
                content=re.sub(r'<think>.*?</think>','',content,flags=re.S).strip()
                if '<think>' in content:
                    content=content.split('<think>')[0].strip()
                if not content:
                    raise APIError('模型只返回了思考内容，请检查非思考模式设置。')
                self.failures=0
                return ModelReply(safe_text(content),truncated=choice.get('finish_reason')=='length')
            except BaseException as exc:
                self.store.finish(call_id,status='failed')
                if isinstance(exc,asyncio.CancelledError):
                    raise
                self.failures+=1
                self.blocked_until=time.monotonic()+min(300,30*2**min(self.failures-1,4))
                if isinstance(exc,APIError):
                    raise
                raise APIError('模型接口暂时无法使用，本次不自动重试。') from None
