"""Local checks and explicitly requested, metered provider smoke test."""
import argparse
import asyncio
import aiohttp
from provider import fetch_model_ids
from settings import ROOT,load_settings
from storage import Store
from engine import LLM

async def check(live=False):
    c=load_settings()
    print('模型：'+c['model'])
    print('接口：'+c['api_base'])
    if c['discord_token'] and c['servers']:
        print('Discord 配置：%s 个服务器、%s 个文字频道' %
              (len(c['servers']),sum(len(entry['channel_ids']) for entry in c['servers'])))
    else:
        print('Discord 配置：等待 Bot Token、服务器与频道')
    (ROOT/'data').mkdir(exist_ok=True)
    store=Store(ROOT/'data/whale.sqlite3')
    try:
        async with aiohttp.ClientSession() as session:
            enabled=c.get('chat_enabled',True)
            if enabled:
                try:
                    models=await asyncio.to_thread(fetch_model_ids,c['api_base'],c['api_key'])
                    print('模型列表验证：'+('通过' if c['model'] in models else
                          '列表中未找到；仍可使用手动填写的模型名称'))
                except ValueError as exc:
                    print(str(exc))
            else:
                print('纯本地模式：不连接模型接口，也不发送计费测试。')
            if live and enabled:
                text=await LLM(c,store,session).chat([
                    {'role':'system','content':c['persona']},
                    {'role':'user','content':'这是机器人接入测试，请用一句中文打招呼。'}],'test')
                print('真实接口回复：'+text)
            print('今日本机估算费用：¥'+format(store.usage()['cost'],'.6f'))
    finally:
        store.close()

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--live',action='store_true',help='发送一次计费测试消息')
    args=parser.parse_args()
    try:
        asyncio.run(check(args.live))
    except Exception as exc:
        print('检查失败：'+type(exc).__name__)
        raise SystemExit(1)
