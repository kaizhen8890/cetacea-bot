"""Configuration and requests for user-selected Chat Completions compatible APIs."""
import ipaddress
import json
import math
import urllib.error
import urllib.request
from urllib.parse import urlsplit,urlunsplit


def local_api(base):
    host=urlsplit(base).hostname
    if host=='localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def normalize_api_base(value):
    if not isinstance(value,str) or not value.strip() or any(c.isspace() for c in value.strip()):
        raise ValueError('请填写完整的 API 地址，地址中不能包含空格或换行。')
    value=value.strip()
    try:
        url=urlsplit(value)
        _=url.port  # Parse and validate an explicitly supplied port.
    except ValueError:
        raise ValueError('API 地址格式或端口无效。') from None
    if (not url.hostname or url.scheme not in ('https','http') or
            url.username is not None or url.password is not None or url.query or url.fragment):
        raise ValueError('API 地址应包含 http(s)://，密钥请填写在密钥栏。')
    if url.scheme=='http' and not local_api(value):
        raise ValueError('远程 API 请使用 HTTPS；本机 localhost、127.0.0.1 或 ::1 可以使用 HTTP。')
    path=url.path.rstrip('/')
    if path.endswith('/chat/completions'):
        path=path[:-len('/chat/completions')]
    return urlunsplit((url.scheme,url.netloc,path,'',''))


def default_extra_body(base):
    return {'thinking':{'type':'disabled'}} if urlsplit(base).hostname=='api.inferera.com' else {}


def validate_extra_body(value):
    reserved={'model','messages','stream','max_tokens','max_completion_tokens'}
    if not isinstance(value,dict) or any(not isinstance(k,str) for k in value):
        raise ValueError('附加参数必须是 JSON 对象，留空时使用 {}。')
    if reserved.intersection(value):
        raise ValueError('附加参数不能覆盖模型、消息、流式开关或输出长度；这些由机器人控制。')
    try:
        json.dumps(value,allow_nan=False)
    except (ValueError,TypeError):
        raise ValueError('附加参数必须是有效 JSON，不能包含 NaN 或无限大。') from None
    return value


def provider_settings(config):
    c=dict(config)
    c['api_base']=normalize_api_base(c.get('api_base',''))
    model=c.get('model','')
    if not isinstance(model,str) or not model.strip() or len(model)>200 or any(x in model for x in '\r\n'):
        raise ValueError('请填写供应商提供的单行模型名称。')
    c['model']=model.strip()
    c.setdefault('pricing_currency','USD')
    if c['pricing_currency'] not in ('USD','CNY'):
        raise ValueError('计价币种请选择美元或人民币。')
    for name,legacy in (('input_price_per_million','input_usd_per_million'),
                        ('output_price_per_million','output_usd_per_million')):
        c.setdefault(name,c.get(legacy))
        value=c[name]
        if type(value) not in (int,float) or not math.isfinite(value) or value<0:
            raise ValueError('输入和输出单价必须是非负数字，免费接口可填 0。')
    if (type(c.get('usd_to_rmb')) not in (int,float) or
            not math.isfinite(c['usd_to_rmb']) or c['usd_to_rmb']<=0):
        raise ValueError('美元换人民币的估算汇率必须是正数。')
    c.setdefault('api_extra_body',default_extra_body(c['api_base']))
    c['api_extra_body']=validate_extra_body(c['api_extra_body'])
    return c


def api_headers(c):
    return {'Authorization':'Bearer '+c['api_key']} if c.get('api_key') else {}


def chat_payload(c,messages,max_tokens,extra_body=None):
    payload={'model':c['model'],'messages':messages,'max_tokens':max_tokens,
             'temperature':0.8,'stream':False}
    parameters=dict(validate_extra_body(c.get('api_extra_body',default_extra_body(c['api_base']))))
    if extra_body is not None:
        parameters.update(validate_extra_body(extra_body))
    for key,value in parameters.items():
        if value is None:
            payload.pop(key,None)
        else:
            payload[key]=value
    return payload


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        return None


def fetch_model_ids(base,key):
    base=normalize_api_base(base)
    req=urllib.request.Request(base+'/models',headers=api_headers({'api_key':key}))
    try:
        with urllib.request.build_opener(NoRedirect).open(req,timeout=15) as response:
            raw=response.read(1024*1024+1)
        if len(raw)>1024*1024:
            raise ValueError('模型列表过大，请直接手动填写模型名称。')
        data=json.loads(raw)
        rows=data.get('data') if isinstance(data,dict) else None
        if not isinstance(rows,list):
            raise ValueError('此接口没有返回兼容的模型列表；仍可手动填写模型名称并保存。')
        return sorted({r['id'] for r in rows if isinstance(r,dict) and isinstance(r.get('id'),str)})
    except urllib.error.HTTPError as exc:
        code=exc.code
        exc.close()
        if code in (404,405):
            raise ValueError('此接口不提供模型列表；可以手动填写模型名称并保存。') from None
        raise ValueError(f'模型列表读取失败（HTTP {code}），请检查地址和密钥。') from None
    except (OSError,urllib.error.URLError):
        raise ValueError('暂时无法连接模型接口，请检查地址和网络。') from None
    except (json.JSONDecodeError,UnicodeError):
        raise ValueError('此地址未返回 JSON 模型列表，请核对 API 地址或手动填写模型。') from None
