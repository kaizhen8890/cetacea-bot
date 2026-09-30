"""Local command routing and results. Never falls back to an LLM."""
import random
import re
import time
from collections import defaultdict,deque
from dataclasses import dataclass
from datetime import datetime
from engine import safe_text
from feature_store import FeatureStore
from local_tools import FEATURE_LABELS,FEATURE_ALIASES,configured_features,dice,options,calculate,convert,reminder_input,annual_date,identifier
from storage import LOCAL_ZONE

TOOL_HELP=('🐳 本地工具，不消耗大模型 token：\n'
           '`!鲸鱼 掷骰 2d6` · `!鲸鱼 抽签 小明 小红 小蓝`\n'
           '`!鲸鱼 选一个 米饭 面条 饺子`\n'
           '`!鲸鱼 计算 (12+8)*3` · `!鲸鱼 换算 100 cm m`\n'
           '`!鲸鱼 提醒我 20分钟后 喝水` · `!鲸鱼 倒计时 5分钟`\n'
           '`!鲸鱼 提醒列表` · `!鲸鱼 取消提醒 编号`\n'
           '`!鲸鱼 摸摸` · `!鲸鱼 投喂`\n'
           '`!鲸鱼 签到` · `!鲸鱼 饭碗`\n'
           '`!鲸鱼 猜数字` · `!鲸鱼 猜拳 石头`\n'
           '`!鲸鱼 wordle 开始 超级` · `/鲸鱼 wordle` 合作猜词与图片棋盘\n'
           '`!鲸鱼 投票 今晚吃什么 | 米饭 | 面条`\n'
           '`!鲸鱼 投票结果 编号` · `!鲸鱼 结束投票 编号`\n'
           '`!鲸鱼 便签 保存 名称=内容` · `!鲸鱼 便签 名称`\n'
           '`!鲸鱼 便签列表` · `!鲸鱼 删除便签 名称` · `!鲸鱼 群规 关键词`\n'
           '`!鲸鱼 生日 10-01` · `!鲸鱼 纪念日 名称=10-01`\n'
           '`!鲸鱼 取消生日` · `!鲸鱼 取消纪念日 名称`\n'
           '也可使用 `/鲸鱼` 命令；提醒在原频道发送，电脑离线时会延迟。\n'
           '管理员：`!鲸鱼 开启功能 提醒` / `!鲸鱼 关闭功能 提醒`。\n'
           '`!鲸鱼 功能` 查看本服务器开关。')

ROUTES={'掷骰':'random','骰子':'random','抽签':'random','选一个':'random','选择':'random',
        '计算':'calculator','换算':'calculator','提醒我':'reminders','提醒':'reminders',
        '倒计时':'reminders','计时':'reminders','提醒列表':'reminders','取消提醒':'reminders',
        '摸摸':'interaction','投喂':'interaction','喂饭':'interaction','喂米饭':'interaction',
        '工具':None,'本地帮助':None,'功能':None,'开启功能':None,'关闭功能':None}
ROUTES.update(投票='polls',投票结果='polls',结束投票='polls',签到='rice',饭碗='rice',
              猜数字='games',猜拳='games',退出游戏='games',便签='notes',便签列表='notes',删除便签='notes',群规='notes',
              生日='dates',纪念日='dates',取消生日='dates',取消纪念日='dates')


def poll_text(poll):
    total=sum(poll['counts'])
    state='进行中，点击选项投票' if poll['status']=='open' else '已结束'
    return f'📊 投票 #{poll["id"]} · {state}\n{poll["question"]}\n'+'\n'.join(
        f'{i+1}. {choice} — {n}票'+(f'（{n/total:.0%}）' if total else '')
        for i,(choice,n) in enumerate(zip(poll['options'],poll['counts'])))+f'\n共 {total} 人；每人一票，可改选。'


@dataclass
class LocalResult:
    text:str
    category:str|None=None
    command:str=''
    buttons:tuple=()
    asset:str|None=None
    private:bool=False
    poll:int|None=None


def natural_command(text,bot_id,bot_name='鲸鱼娘'):
    """Only explicit actions directed at this bot; ordinary words are not triggers."""
    own=f'<@{bot_id}>' in text or f'<@!{bot_id}>' in text
    clean=re.sub(rf'<@!?{bot_id}>','',text).strip().rstrip('。！!~～')
    if own and clean in ('摸摸','投喂','喂饭','喂米饭'):
        return clean
    names={'鲸鱼娘','DeepSeek鲸鱼娘',bot_name}
    for name in names:
        if clean in (f'摸摸{name}',f'{name}摸摸'):
            return '摸摸'
        if clean in (f'投喂{name}',f'给{name}喂饭',f'给{name}喂米饭',f'给{name}投喂米饭'):
            return '投喂'
    return None


class LocalFeatures:
    def __init__(self,config,store):
        self.c=config
        self.db=FeatureStore(store)
        self.defaults=configured_features(config)
        self.requests=defaultdict(deque)
        self.throttled={}
        self.last_natural={}

    def enabled(self,guild,feature):
        return self.defaults[feature] and self.db.enabled(guild,feature)

    def split_command(self,text):
        parts=text.strip().split(maxsplit=1)
        if not parts or parts[0] not in ROUTES:
            return None
        return parts[0],parts[1].strip() if len(parts)>1 else ''

    def execute(self,guild,channel,user,command,admin=False,natural=False,now=None,public=False):
        if len(command)>1000:
            return LocalResult('本地指令最多 1000 字，请缩短后重试。',private=True)
        parsed=self.split_command(command)
        if parsed is None:
            return None
        name,args=parsed
        feature=ROUTES[name]
        command=safe_text(command)
        args=safe_text(args)
        now=time.time() if now is None else now
        key=(guild,user)
        if natural:
            last=self.last_natural.get(key,float('-inf'))
            if now-last<15:
                return LocalResult('')
            self.last_natural[key]=now
        recent=self.requests[key]
        while recent and now-recent[0]>=10:
            recent.popleft()
        if len(recent)>=6:
            if now-self.throttled.get(key,float('-inf'))<10:
                return LocalResult('')
            self.throttled[key]=now
            return LocalResult('太密啦，等十秒再玩嘛。')
        recent.append(now)
        # Cancellation and listing remain accessible when the reminder feature is paused.
        if feature and name not in ('取消提醒','提醒列表','取消生日','取消纪念日','删除便签','退出游戏','结束投票','投票结果') and not self.enabled(guild,feature):
            return LocalResult(f'本服务器的{FEATURE_LABELS[feature]}已关闭。',feature,command)
        try:
            buttons=()
            private=False
            asset=None
            poll_id=None
            if name in ('工具','本地帮助'):
                text=TOOL_HELP
                buttons=(('🎲 掷骰','掷骰 1d6'),('摸摸','摸摸'),('投喂米饭','投喂'))
            elif name=='功能':
                text='本服务器的本地功能：\n'+'\n'.join(
                    ('✅ ' if self.enabled(guild,k) else '⏸️ ')+v for k,v in FEATURE_LABELS.items())
            elif name in ('开启功能','关闭功能'):
                if not admin:
                    raise ValueError('更改开关需要服务器管理权限或主人身份。')
                target=FEATURE_ALIASES.get(args,args)
                if target not in FEATURE_LABELS:
                    raise ValueError('可选功能：随机、计算、提醒、互动、投票、签到、游戏、便签、日期、wordle。')
                if name=='开启功能' and not self.defaults[target]:
                    raise ValueError('此功能在电脑配置中已关闭，请先在桌面配置里开启。')
                self.db.toggle(guild,target,name=='开启功能')
                text=f'{FEATURE_LABELS[target]}已'+('开启。' if name=='开启功能' else '关闭；已有提醒保留，恢复后再发送。' if target in ('reminders','dates') else '关闭。')
            elif name in ('掷骰','骰子'):
                text=dice(args)
                buttons=(('再掷一次',f'掷骰 {args or "1d6"}'),)
            elif name in ('抽签','选一个','选择'):
                text=('抽到了：' if name=='抽签' else '我选：')+random.choice(options(args))+' 🐳'
                buttons=(('再选一次',command),)
            elif name=='计算':
                text='结果：'+calculate(args)
            elif name=='换算':
                text=convert(args)
            elif name in ('提醒我','提醒','倒计时','计时'):
                seconds,body=reminder_input(args,timer=name in ('倒计时','计时'))
                due=now+seconds
                rid=self.db.add_reminder(guild,channel,user,body,due,now)
                stamp=datetime.fromtimestamp(due,LOCAL_ZONE).strftime('%m-%d %H:%M:%S')
                text=f'记好啦，提醒 #{rid}：{body}\n{stamp}（UTC+8）在本频道提醒你；离线时延后。'
                buttons=(('取消这条提醒',f'取消提醒 {rid}'),)
            elif name=='提醒列表':
                rows=self.db.reminders(guild,channel,user)
                text='你在本频道的提醒：\n'+('\n'.join(
                    f'#{r["id"]} '+datetime.fromtimestamp(r['due'],LOCAL_ZONE).strftime('%m-%d %H:%M')+
                    (' [发送失败，可取消并重新设置]' if r['status']=='failed' else '')+' '+r['body'][:48]
                    for r in rows) or '暂无。')
                private=True
            elif name=='取消提醒':
                if not args.isdigit():
                    raise ValueError('格式：取消提醒 编号；用 提醒列表 查看编号。')
                text=('已取消这条提醒。' if self.db.cancel(identifier(args),guild,channel,user) else
                      '找不到你在本频道的这条待办提醒，或它已经发送。')
                private=True
            elif name=='投票':
                parts=[p.strip() for p in re.split(r'[|｜]',args)]
                if len(parts)<3 or not parts[0] or len(parts[0])>100:
                    raise ValueError('格式：投票 问题 | 选项一 | 选项二；问题最多 100 字。')
                choices=list(dict.fromkeys(parts[1:]))
                if not 2<=len(choices)<=8 or any(not p or len(p)>40 for p in choices):
                    raise ValueError('投票需 2—8 个不同选项，每项最多 40 字。')
                poll_id=self.db.create_poll(guild,channel,user,parts[0],choices,now)
                text=poll_text(self.db.poll(poll_id,guild,channel))
            elif name in ('投票结果','结束投票'):
                if not args.isdigit():
                    raise ValueError('请填写投票编号，例如 投票结果 1。')
                pid=identifier(args)
                poll=(self.db.end_poll(pid,guild,channel,user,admin) if name=='结束投票'
                      else self.db.poll(pid,guild,channel))
                text=poll_text(poll)
                if name=='结束投票':
                    poll_id=pid
            elif name=='签到':
                balance=self.db.signin(guild,user,now)
                text=f'签到啦，+10 粒米饭！饭碗里有 {balance} 粒。🐳'
            elif name=='饭碗':
                text=f'饭碗里有 {self.db.rice(guild,user)["balance"]} 粒米饭；投喂一次用 1 粒，每日签到领 10 粒。'
                private=True
            elif name=='猜数字':
                if not args or args=='重开':
                    self.db.start_game(guild,channel,user,random.randint(1,100),now)
                    text='我想好 1—100 的数字啦。十次机会，直接发数字或用 !鲸鱼 猜数字 50；十分钟内有效。'
                else:
                    if not args.isdigit() or not 1<=int(args)<=100:
                        raise ValueError('请猜 1—100 的整数，或用 猜数字 重开。')
                    text=self.db.guess(guild,channel,user,int(args),now)
                buttons=(('重开一局','猜数字 重开'),)
            elif name=='退出游戏':
                self.db.quit_game(guild,channel,user)
                text='这局先收起来啦。'
            elif name=='猜拳':
                moves=('石头','剪刀','布')
                if not args:
                    text='出哪一个？🐳'
                else:
                    if args not in moves:
                        raise ValueError('可选：石头、剪刀、布。')
                    move=random.choice(moves)
                    won=(moves.index(args)-moves.index(move))%3==2
                    outcome='平局，再来嘛。' if args==move else '你赢啦！' if won else '这次我赢啦。'
                    text=f'你出{args}，我出{move}。{outcome}'
                buttons=tuple((move,'猜拳 '+move) for move in moves)
            elif name=='便签':
                if args.startswith('保存 '):
                    body=args[3:].strip()
                    if '=' not in body:
                        raise ValueError('格式：便签 保存 名称=内容。')
                    key,value=(p.strip() for p in body.split('=',1))
                    self.db.save_note(guild,channel,user,key,value)
                    text='便签存好啦；仅保存给你在本频道使用。'
                else:
                    rows=[r for r in self.db.notes(guild,channel,user) if r['key']==args]
                    text=(rows[0]['key']+'：'+rows[0]['value'] if rows else '没有这条便签；用 便签列表 查看名称。')
                private=True
            elif name=='便签列表':
                text='你的本频道便签：'+('、'.join(r['key'] for r in self.db.notes(guild,channel,user)) or '暂无。')
                private=True
            elif name=='删除便签':
                text='便签已删除。' if self.db.delete_note(guild,channel,user,args) else '没有这条便签。'
                private=True
            elif name=='群规':
                if args.startswith(('添加 ','保存 ','删除 ')):
                    if not admin:
                        raise ValueError('录入或删除群规需要管理权限。')
                    action,body=args.split(maxsplit=1)
                    if action=='删除':
                        text='这条群规已删除。' if self.db.delete_rule(guild,body.strip()) else '没有这条群规。'
                    else:
                        if not public:
                            raise ValueError('共享群规请在公开频道录入，避免分享受限频道内容。')
                        if '=' not in body:
                            raise ValueError('格式：群规 添加 关键词=说明。')
                        key,value=(p.strip() for p in body.split('=',1))
                        self.db.save_rule(guild,key,value)
                        text='群规存好啦，在本服务器可以按关键词查询。'
                else:
                    rows=self.db.rules(guild,args)
                    text='\n'.join(r['key']+'：'+r['value'] for r in rows) or '没找到相关群规；这里只查已录入的关键词，不调用模型。'
            elif name in ('生日','纪念日'):
                key='birthday'
                label='生日'
                date=args
                if name=='纪念日':
                    if '=' not in args:
                        raise ValueError('格式：纪念日 名称=10-01。')
                    label,date=(p.strip() for p in args.split('=',1))
                    if not label or len(label)>24:
                        raise ValueError('纪念日名称请控制在 1—24 字。')
                    key='anniversary:'+label
                month,day=annual_date(date)
                body='生日快乐！今天要好好吃饭呀🐳' if name=='生日' else label+'纪念日到啦🐳'
                rid=self.db.annual(guild,channel,user,key,month,day,body,now)
                text=f'{label}记好啦：每年 {month:02d}-{day:02d} 09:00（UTC+8）在本频道提醒你。编号 #{rid}。'
                buttons=(('取消这条提醒',f'取消提醒 {rid}'),)
            elif name in ('取消生日','取消纪念日'):
                key='birthday' if name=='取消生日' else 'anniversary:'+args
                text='这个日期提醒已取消。' if self.db.cancel_annual(guild,channel,user,key) else '本频道没有这个日期提醒。'
                private=True
            elif name=='摸摸':
                text=random.choice(('哼，再摸一下也行。🐳','尾巴给你摸一下。','好啦，今天辛苦了。'))
                asset='pat'
                buttons=(('再摸一下','摸摸'),('投喂米饭','投喂'))
            else:
                text=random.choice(('米饭！收下啦🐳','吃饱再陪你玩。','好香，分你一口。'))
                if self.enabled(guild,'rice'):
                    text+=f'（剩 {self.db.feed(guild,user)} 粒）'
                asset='feed'
                buttons=(('再喂一口','投喂'),('摸摸','摸摸'))
            return LocalResult(text,feature,command,buttons,asset,private,poll_id)
        except ValueError as exc:
            return LocalResult(str(exc),feature,command,private=True)
