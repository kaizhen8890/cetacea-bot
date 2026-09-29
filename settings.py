import json
import math
import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent

def configured_servers(c):
    """Validate the whitelist and accept the original single-server format."""
    raw=c.get('servers')
    if raw is None:
        guild_id=c.get('guild_id')
        channel_ids=c.get('channel_ids',[])
        raw=[] if not guild_id and not channel_ids else [
            {'guild_id':guild_id,'channel_ids':channel_ids}]
    if not isinstance(raw,list):
        raise ValueError('服务器频道白名单必须是列表')
    servers=[]
    seen_guilds=set()
    seen_channels=set()
    for entry in raw:
        if not isinstance(entry,dict):
            raise ValueError('服务器频道配置格式无效')
        gid=entry.get('guild_id')
        ids=entry.get('channel_ids')
        if type(gid) is not int or gid<=0 or gid in seen_guilds:
            raise ValueError('服务器 ID 必须是唯一的正整数')
        if not isinstance(ids,list) or not ids or any(type(cid) is not int or cid<=0 for cid in ids):
            raise ValueError('每个服务器至少要有一个有效文字频道 ID')
        if len(ids)!=len(set(ids)) or seen_channels.intersection(ids):
            raise ValueError('频道 ID 不能重复配置')
        servers.append({'guild_id':gid,'channel_ids':ids[:]})
        seen_guilds.add(gid)
        seen_channels.update(ids)
    return servers

def load_settings(require_discord=False):
    load_dotenv(ROOT / '.env')
    c = json.loads((ROOT / 'config.json').read_text(encoding='utf-8-sig'))
    for name in ('daily_budget_rmb', 'input_usd_per_million', 'output_usd_per_million',
                 'usd_to_rmb', 'cost_margin', 'api_timeout_seconds'):
        if not isinstance(c[name], (int, float)) or not math.isfinite(c[name]) or c[name] <= 0:
            raise ValueError(f'{name} 必须为正数')
    if (not 0 < c['budget_usable_fraction'] <= 1 or
            not 0 <= c['casual_probability'] <= 1 or
            not 0 <= c['emoji_probability'] <= 1):
        raise ValueError('预算比例、插话概率或表情概率无效')
    for name in ('max_output_tokens','explanation_max_output_tokens','max_prompt_bytes','daily_call_limit','context_messages',
                 'conversation_followup_seconds',
                 'context_chars_per_message','input_chars','memory_max_items'):
        if not isinstance(c[name], int) or c[name] <= 0:
            raise ValueError(f'{name} 必须为正整数')
    if max(c['max_output_tokens'],c['explanation_max_output_tokens']) > 1024 or c['max_prompt_bytes'] > 20000:
        raise ValueError('本程序的节省模式限制输出最多1024 tokens、输入20000字节')
    if not c['api_base'].startswith('https://'):
        raise ValueError('模型接口必须使用 HTTPS')
    c['api_key'] = os.getenv('LLM_API_KEY', '').strip()
    c['discord_token'] = os.getenv('DISCORD_TOKEN', '').strip()
    c['persona'] = (ROOT / 'persona.txt').read_text(encoding='utf-8').strip()
    c['servers'] = configured_servers(c)
    c.pop('guild_id',None)
    c.pop('channel_ids',None)
    if not c['api_key']:
        raise ValueError('请先填写模型 API 密钥')
    if require_discord and (not c['discord_token'] or not c['servers']):
        raise ValueError('请先完成本机配置：Discord Token 和至少一个允许聊天的服务器频道')
    return c
