"""Small deterministic tools. No network requests or model dependencies."""
import ast
import math
import operator
import random
import re
from datetime import datetime,timedelta,timezone

FEATURE_LABELS={'random':'随机工具','calculator':'计算换算','reminders':'提醒计时','interaction':'鲸鱼互动'}
FEATURE_LABELS.update(polls='投票',rice='米饭签到',games='小游戏',notes='便签群规',dates='日期提醒')
FEATURE_ALIASES={'随机':'random','随机工具':'random','掷骰':'random','抽签':'random','选择':'random',
                 '计算':'calculator','换算':'calculator','计算换算':'calculator',
                 '提醒':'reminders','计时':'reminders','提醒计时':'reminders',
                 '互动':'interaction','鲸鱼互动':'interaction'}
FEATURE_ALIASES.update(投票='polls',签到='rice',米饭='rice',米饭签到='rice',
                       游戏='games',小游戏='games',便签='notes',群规='notes',便签群规='notes',
                       生日='dates',纪念日='dates',日期='dates',日期提醒='dates')


def annual_date(text):
    match=re.fullmatch(r'(\d{1,2})[-/](\d{1,2})',text.strip())
    if not match:
        raise ValueError('日期请写成 月-日，例如 10-01；生日只记录月日。')
    month,day=map(int,match.groups())
    try:
        datetime(2000,month,day)
    except ValueError:
        raise ValueError('这个月日不存在，请检查日期。') from None
    return month,day


def identifier(text):
    if not text.isdigit() or len(text)>19 or not 0<int(text)<=2**63-1:
        raise ValueError('编号应是有效的正整数，可用列表指令查看。')
    return int(text)


def next_annual(month,day,after):
    zone=timezone(timedelta(hours=8))
    current=datetime.fromtimestamp(after,zone)
    for year in range(current.year,current.year+9):
        try:
            date=datetime(year,month,day,9,tzinfo=zone)
        except ValueError:
            continue
        if date.timestamp()>after:
            return date.timestamp()
    raise ValueError('找不到下一次有效日期。')


def configured_features(config):
    result={name:True for name in FEATURE_LABELS}
    raw=config.get('local_features',{})
    if not isinstance(raw,dict) or any(k not in result or type(v) is not bool for k,v in raw.items()):
        raise ValueError('本地功能开关必须使用支持的功能名称及 true/false。')
    result.update(raw)
    return result


def dice(spec='1d6'):
    spec=spec.strip().lower() or '1d6'
    match=re.fullmatch(r'(\d{0,2})d(\d{1,6})([+-]\d{1,6})?',spec)
    if not match:
        raise ValueError('格式：掷骰 2d6，或掷骰 1d20+3；最多 20 颗骰子。')
    count,sides=int(match[1] or '1'),int(match[2])
    bonus=int(match[3] or '0')
    if not 1<=count<=20 or not 2<=sides<=100000:
        raise ValueError('一次可掷 1—20 颗骰子，每颗 2—100000 面。')
    values=[random.randint(1,sides) for _ in range(count)]
    suffix=f'，修正 {bonus:+d}' if bonus else ''
    return f'🎲 {spec}：'+'、'.join(map(str,values))+suffix+f' → {sum(values)+bonus}'


def options(text):
    separator=r'[,，、|｜\n]+' if re.search(r'[,，、|｜\n]',text) else r'\s+'
    values=[v.strip() for v in re.split(separator,text.strip()) if v.strip()]
    values=list(dict.fromkeys(values))
    if not 2<=len(values)<=30 or any(len(v)>40 for v in values):
        raise ValueError('请给 2—30 个不同选项，用空格或逗号分隔；每项最多 40 字。')
    return values


def number(value):
    if not math.isfinite(value) or abs(value)>1e100:
        raise ValueError('结果太大或无效，请缩小数字。')
    return format(value,'.12g')


def calculate(expression):
    expression=expression.strip().replace('×','*').replace('÷','/').replace('^','**')
    if not expression or len(expression)>180:
        raise ValueError('算式请控制在 180 字内，例如 (12+8)*3。')
    try:
        tree=ast.parse(expression,mode='eval')
    except (SyntaxError,ValueError,RecursionError):
        raise ValueError('算式格式有误；支持四则运算、括号、幂、sqrt、abs、sin、cos、tan、log、pi、e。') from None
    if len(list(ast.walk(tree)))>64:
        raise ValueError('算式太复杂，请分开计算。')
    binary={ast.Add:operator.add,ast.Sub:operator.sub,ast.Mult:operator.mul,
            ast.Div:operator.truediv,ast.FloorDiv:operator.floordiv,ast.Mod:operator.mod}
    functions={'sqrt':math.sqrt,'abs':abs,'sin':math.sin,'cos':math.cos,'tan':math.tan,'log':math.log}
    def evaluate(node):
        if isinstance(node,ast.Constant) and type(node.value) in (int,float):
            result=float(node.value)
        elif isinstance(node,ast.Name) and node.id in ('pi','e'):
            result=getattr(math,node.id)
        elif isinstance(node,ast.UnaryOp) and type(node.op) in (ast.UAdd,ast.USub):
            result=evaluate(node.operand)*(1 if isinstance(node.op,ast.UAdd) else -1)
        elif isinstance(node,ast.BinOp):
            left,right=evaluate(node.left),evaluate(node.right)
            if isinstance(node.op,ast.Pow):
                if abs(right)>100:
                    raise ValueError('指数绝对值最多 100。')
                result=math.pow(left,right)
            elif type(node.op) in binary:
                result=binary[type(node.op)](left,right)
            else:
                raise ValueError('这个运算暂不支持。')
        elif (isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and
              node.func.id in functions and len(node.args)==1 and not node.keywords):
            result=functions[node.func.id](evaluate(node.args[0]))
        else:
            raise ValueError('只支持数学算式和列出的函数。')
        number(result)
        return result
    try:
        return number(evaluate(tree.body))
    except ZeroDivisionError:
        raise ValueError('除数不能是 0。') from None
    except (OverflowError,RecursionError):
        raise ValueError('数字或算式太大，请缩小后计算。') from None
    except ValueError as exc:
        if str(exc)=='math domain error':
            raise ValueError('超出实数范围，例如负数开平方或非正数取对数。') from None
        raise


# (dimension, factor to SI/common base unit); currency needs live rates and is omitted.
UNITS={
    'mm':('length',.001),'cm':('length',.01),'m':('length',1),'km':('length',1000),
    'in':('length',.0254),'ft':('length',.3048),'mi':('length',1609.344),
    'mg':('mass',.000001),'g':('mass',.001),'kg':('mass',1),'lb':('mass',.45359237),
    'ml':('volume',.001),'l':('volume',1),
    's':('time',1),'min':('time',60),'h':('time',3600),'day':('time',86400),
    'b':('data',1),'kb':('data',1000),'mb':('data',1e6),'gb':('data',1e9),
    'kib':('data',1024),'mib':('data',1024**2),'gib':('data',1024**3),
}
UNIT_ALIASES={'毫米':'mm','厘米':'cm','米':'m','公里':'km','千米':'km','英寸':'in','英尺':'ft','英里':'mi',
              '毫克':'mg','克':'g','千克':'kg','公斤':'kg','磅':'lb','毫升':'ml','升':'l',
              '秒':'s','分钟':'min','小时':'h','天':'day','字节':'b',
              '摄氏度':'c','摄氏':'c','℃':'c','°c':'c','华氏度':'f','华氏':'f','℉':'f','°f':'f','开尔文':'k'}


def convert(text):
    match=re.fullmatch(r'\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?)\s*([^\s]+?)\s*'
                       r'(?:到|转|换成|=|\s)\s*([^\s]+)\s*',text,re.I)
    if not match:
        raise ValueError('格式：换算 100 cm m，或换算 32 摄氏度 华氏度。')
    value=float(match[1])
    number(value)
    source=UNIT_ALIASES.get(match[2].lower(),match[2].lower())
    target=UNIT_ALIASES.get(match[3].lower(),match[3].lower())
    temperatures={'c','f','k'}
    if source in temperatures and target in temperatures:
        kelvin=value+273.15 if source=='c' else (value-32)*5/9+273.15 if source=='f' else value
        if kelvin<0:
            raise ValueError('温度不能低于绝对零度。')
        result=kelvin-273.15 if target=='c' else (kelvin-273.15)*9/5+32 if target=='f' else kelvin
    elif source in UNITS and target in UNITS and UNITS[source][0]==UNITS[target][0]:
        result=value*UNITS[source][1]/UNITS[target][1]
    else:
        raise ValueError('单位不支持或种类不同；支持长度、质量、容量、时间、温度和数据大小。')
    return f'{match[1]} {match[2]} = {number(result)} {match[3]}'


def duration(text):
    """Relative durations only; explicit units make reminder time unambiguous."""
    pattern=r'(\d+(?:\.\d+)?)\s*(秒钟|分钟|小时|秒|分|时|天|s|m|h|d)'
    parts=list(re.finditer(pattern,text.strip(),re.I))
    if not parts or re.sub(pattern,'',text.strip(),flags=re.I).strip():
        raise ValueError('时间请写成 20分钟、1小时30分钟 或 10秒；最长 365天。')
    factors={'秒钟':1,'秒':1,'s':1,'分钟':60,'分':60,'m':60,'小时':3600,'时':3600,'h':3600,'天':86400,'d':86400}
    total=sum(float(m[1])*factors[m[2].lower()] for m in parts)
    if not math.isfinite(total) or not 1<=total<=365*86400:
        raise ValueError('提醒时间应为 1秒—365天。')
    return total


def reminder_input(text,timer=False):
    match=re.fullmatch(r'\s*((?:\d+(?:\.\d+)?\s*(?:秒钟|分钟|小时|秒|分|时|天|s|m|h|d)\s*)+)'
                       r'(?:后\s*|\s+)?(.*?)\s*',text,re.I)
    if not match:
        raise ValueError('格式：提醒我 20分钟后 喝水；也可用 倒计时 5分钟。')
    seconds=duration(match[1])
    body=match[2].strip()
    if timer and not body:
        body='倒计时结束啦。'
    if not body or len(body)>200:
        raise ValueError('请填写 1—200 字的提醒内容。')
    return seconds,body
