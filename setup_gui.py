"""Small local-only configuration window. Never renders a secret in logs."""
import json
import math
import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
import urllib.request
import urllib.error
import webbrowser
from tkinter import ttk,messagebox
from dotenv import dotenv_values,set_key
from settings import ROOT, configured_servers
from provider import (provider_settings,normalize_api_base,local_api,
                      default_extra_body,fetch_model_ids)
from local_tools import configured_features,FEATURE_LABELS


def provider_form_settings(config,values,extra_text):
    """Validate editable provider fields before writing any configuration or secrets."""
    c=dict(config)
    c.update(api_base=values['api_base'],model=values['model'],
             pricing_currency={'美元 (USD)':'USD','人民币 (CNY)':'CNY'}.get(
                 values['pricing_currency'],values['pricing_currency']))
    try:
        for field in ('input_price_per_million','output_price_per_million',
                      'usd_to_rmb','daily_budget_rmb'):
            c[field]=float(values[field])
    except (ValueError,TypeError):
        raise ValueError('价格、汇率和每日预算请填写数字。') from None
    try:
        c['api_extra_body']=json.loads(extra_text.strip() or '{}')
    except json.JSONDecodeError:
        raise ValueError('附加参数格式有误，请填写 JSON 对象，例如 {}。') from None
    c=provider_settings(c)
    if not math.isfinite(c['daily_budget_rmb']) or c['daily_budget_rmb']<=0:
        raise ValueError('每日预算必须是大于 0 的人民币金额。')
    api=values['api_key'].strip()
    if any(x in api for x in '\r\n') or (c.get('chat_enabled',True) and not api and not local_api(c['api_base'])):
        raise ValueError('请填写单行模型 API 密钥；本机接口可以留空。')
    c['local_features']=configured_features(c)
    c.pop('input_usd_per_million',None)
    c.pop('output_usd_per_million',None)
    c.pop('api_key',None)
    c.pop('discord_token',None)
    return c


def restart_bot():
    """Restart only this installation, including its existing logon task."""
    if os.name=='nt':
        script_path=str(ROOT/'bot.py').replace("'","''")
        root_path=str(ROOT).replace("'","''")
        script=f"""$ErrorActionPreference='Stop'
$botPath='{script_path}'
$botRoot='{root_path}'
$task=Get-ScheduledTask -TaskName 'DeepSeek鲸鱼娘' -ErrorAction SilentlyContinue
$managed=$task -and @($task.Actions | Where-Object {{ $_.WorkingDirectory -eq $botRoot }}).Count -gt 0
if($managed){{ Stop-ScheduledTask -TaskName 'DeepSeek鲸鱼娘'; Start-Sleep -Seconds 2 }}
Get-CimInstance Win32_Process | Where-Object {{
    $_.Name -in @('python.exe','pythonw.exe') -and $_.CommandLine -and
    $_.CommandLine.IndexOf($botPath,[StringComparison]::OrdinalIgnoreCase) -ge 0
}} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}
if($managed){{ Start-ScheduledTask -TaskName 'DeepSeek鲸鱼娘'; exit 10 }}
"""
        result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',script],
                              capture_output=True,timeout=30,
                              creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode==10:
            return
        if result.returncode!=0:
            raise ValueError('无法重启旧实例。请关闭旧机器人，再用 start.cmd 启动。')
    executable=sys.executable
    if os.name=='nt':
        pythonw=ROOT/'.venv/Scripts/pythonw.exe'
        if pythonw.exists():
            executable=str(pythonw)
    subprocess.Popen([executable,'-X','utf8',str(ROOT/'bot.py')],cwd=ROOT,
                     creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))

def format_server_lines(servers):
    return '\n'.join(f'{entry["guild_id"]}: '+', '.join(map(str,entry['channel_ids']))
                     for entry in servers)

def parse_server_lines(text):
    servers=[]
    for line in text.splitlines():
        line=line.strip()
        if not line:
            continue
        parts=re.split(r'[:：]',line,maxsplit=1)
        if len(parts)!=2 or not parts[0].strip().isdigit():
            raise ValueError('每行请填写“服务器ID: 频道ID, 频道ID”，或使用自动读取。')
        ids=[item for item in re.split(r'[,，\s]+',parts[1].strip()) if item]
        if not ids or not all(item.isdigit() for item in ids):
            raise ValueError('频道 ID 必须是数字，多个频道用逗号分开。')
        servers.append({'guild_id':int(parts[0].strip()),
                        'channel_ids':[int(item) for item in ids]})
    return configured_servers({'servers':servers})

def main():
    config_path=ROOT/'config.json'
    template_path=ROOT/'config.example.json'
    c=provider_settings(json.loads((config_path if config_path.exists() else template_path).read_text(encoding='utf-8-sig')))
    secrets=dotenv_values(ROOT/'.env')
    root=tk.Tk()
    root.title('鲸鱼娘 · 功能与模型设置')
    root.geometry('840x690')
    root.minsize(790,650)
    frame=ttk.Frame(root,padding=22)
    frame.pack(fill='both',expand=True)
    ttk.Label(frame,text='🐳 DeepSeek 鲸鱼娘',font=('Microsoft YaHei UI',19,'bold')).pack(anchor='w')
    ttk.Label(frame,text='设置模型、本地功能和频道。密钥仅保存在本机，保存并重启后生效。').pack(anchor='w',pady=(8,14))
    footer=ttk.Frame(frame)
    footer.pack(side='bottom',fill='x')
    notebook=ttk.Notebook(frame)
    notebook.pack(fill='both',expand=True)
    api_tab=ttk.Frame(notebook,padding=16)
    discord_tab=ttk.Frame(notebook,padding=16)
    advanced_tab=ttk.Frame(notebook,padding=16)
    local_tab=ttk.Frame(notebook,padding=16)
    for tab,label in ((api_tab,'模型与预算'),(local_tab,'本地功能'),(discord_tab,'Discord 频道'),(advanced_tab,'附加参数')):
        notebook.add(tab,text=label)
    fields={}
    widgets={}
    def entry(form,row,key,label,value,secret=False,choices=None):
        form.columnconfigure(1,weight=1)
        ttk.Label(form,text=label).grid(row=row,column=0,sticky='w',pady=7,padx=(0,12))
        var=tk.StringVar(value=value)
        widget=(ttk.Combobox(form,textvariable=var,values=choices,state='readonly') if choices
                else ttk.Entry(form,textvariable=var,show='●' if secret else ''))
        widget.grid(row=row,column=1,sticky='ew')
        fields[key]=var
        widgets[key]=widget
        return widget
    api_form=ttk.Frame(api_tab)
    api_form.pack(fill='x')
    entry(api_form,0,'api_base','API 地址',c['api_base'])
    entry(api_form,1,'api_key','API 密钥',secrets.get('LLM_API_KEY',''),True)
    model_entry=entry(api_form,2,'model','模型名称',c['model'],choices=[c['model']])
    model_entry.configure(state='normal')
    ttk.Label(api_tab,text='支持 Chat Completions 兼容接口。按供应商给出的地址填写，保留 /v1 等路径；\n'
                          '也可以粘贴完整的 /chat/completions 地址。模型名称可直接手动输入。',
              foreground='#555555',wraplength=710).pack(anchor='w',pady=(7,8))
    model_button=ttk.Button(api_tab,text='读取模型列表（可选）')
    model_button.pack(anchor='w')
    ttk.Separator(api_tab).pack(fill='x',pady=12)
    budget_form=ttk.Frame(api_tab)
    budget_form.pack(fill='x')
    currency='美元 (USD)' if c['pricing_currency']=='USD' else '人民币 (CNY)'
    entry(budget_form,0,'pricing_currency','单价币种',currency,choices=['人民币 (CNY)','美元 (USD)'])
    entry(budget_form,1,'input_price_per_million','输入价格 / 百万 tokens',str(c['input_price_per_million']))
    entry(budget_form,2,'output_price_per_million','输出价格 / 百万 tokens',str(c['output_price_per_million']))
    exchange_entry=entry(budget_form,3,'usd_to_rmb','1 美元约等于多少人民币',str(c['usd_to_rmb']))
    entry(budget_form,4,'daily_budget_rmb','每日预算（人民币）',str(c['daily_budget_rmb']))
    def currency_changed(*args):
        exchange_entry.configure(state='normal' if fields['pricing_currency'].get()=='美元 (USD)' else 'disabled')
    fields['pricing_currency'].trace_add('write',currency_changed)
    currency_changed()
    ttk.Label(api_tab,text='价格请按供应商当前单价填写；免费接口填 0。预算由全部服务器共用，\n'
                          '仍保留费用余量和每日调用次数限制。本机估算不等于供应商账单。',
              foreground='#555555',wraplength=710).pack(anchor='w',pady=(10,0))
    chat_enabled=tk.BooleanVar(value=c.get('chat_enabled',True))
    ttk.Checkbutton(local_tab,text='开启云端聊天（被 @、自然续聊和偶尔插嘴会调用模型）',
                    variable=chat_enabled).pack(anchor='w',pady=(0,8))
    ttk.Label(local_tab,text='取消勾选即为纯本地模式：无需大模型密钥，以下功能仍可使用。\n'
              'Discord Bot Token 仍用于连接服务器；本地记忆整理使用独立的 Ollama 设置。',
              foreground='#555555',wraplength=710).pack(anchor='w',pady=(0,14))
    ttk.Separator(local_tab).pack(fill='x',pady=(0,14))
    feature_checks={}
    defaults=configured_features(c)
    descriptions={'random':'掷骰、抽签、帮忙选一个','calculator':'数学计算、常用单位换算',
                  'reminders':'提醒与倒计时','interaction':'摸摸、投喂与表情包',
                  'polls':'按钮投票与结果','rice':'每日签到、饭碗余额',
                  'games':'猜数字、石头剪刀布','notes':'本人便签、关键词查群规','dates':'生日和纪念日提醒',
                  'wordle':'5/7 字母合作猜词、图片棋盘'}
    feature_grid=ttk.Frame(local_tab)
    feature_grid.pack(fill='x')
    for column in (0,1):
        feature_grid.columnconfigure(column,weight=1)
    for index,(name,label) in enumerate(FEATURE_LABELS.items()):
        var=tk.BooleanVar(value=defaults[name])
        feature_checks[name]=var
        ttk.Checkbutton(feature_grid,text=label+'\n'+descriptions[name],variable=var).grid(
            row=index//2,column=index%2,sticky='w',pady=3,padx=(0,12))
    ttk.Label(local_tab,text='各服务器管理员可用 !鲸鱼 开启功能 提醒 / 关闭功能 提醒 调整。\n'
              '本地工具不占聊天 token，暂停聊天后仍可用。\n'
              '自备图片：data/reactions/feed.gif（投喂）、pat.png（摸摸）。\n'
              '支持 GIF、PNG、JPG、WEBP，每张最多 4MB；没有图片时用文字和群内表情。',
              wraplength=710,foreground='#555555').pack(anchor='w',pady=10)
    def mode_changed(*args):
        enabled=chat_enabled.get()
        for name in ('api_base','api_key','model','pricing_currency','input_price_per_million',
                     'output_price_per_million','daily_budget_rmb'):
            state='readonly' if name=='pricing_currency' else 'normal'
            widgets[name].configure(state=state if enabled else 'disabled')
        exchange_entry.configure(state='normal' if enabled and fields['pricing_currency'].get()=='美元 (USD)' else 'disabled')
        model_button.configure(state='normal' if enabled else 'disabled')
    chat_enabled.trace_add('write',mode_changed)
    mode_changed()
    discord_form=ttk.Frame(discord_tab)
    discord_form.pack(fill='x')
    entry(discord_form,0,'discord_token','Bot Token（非 Public Key）',secrets.get('DISCORD_TOKEN',''),True)
    entry(discord_form,1,'owner_id','主人用户 ID（可留空）',str(c['owner_id']) if c['owner_id'] else '')
    ttk.Label(discord_tab,text='允许聊天的服务器与文字频道（每行一个服务器）').pack(anchor='w',pady=(16,5))
    servers_box=tk.Text(discord_tab,height=6,wrap='word')
    servers_box.pack(fill='x')
    servers_box.insert('1.0',format_server_lines(configured_servers(c)))
    ttk.Label(discord_tab,text='手动格式：服务器ID: 频道ID, 频道ID',foreground='#777777').pack(anchor='w',pady=(3,8))
    auto_memory=tk.BooleanVar(value=c.get('auto_memory_enabled',False))
    ttk.Checkbutton(discord_tab,text='记录已选频道消息，并用本地 Qwen 定时整理记忆（需另行安装 Ollama 模型）',
                    variable=auto_memory).pack(anchor='w',pady=(9,0))
    ttk.Label(advanced_tab,text='供应商有特殊要求时，可在这里填写额外的请求参数。通常保持默认即可。',
              wraplength=710).pack(anchor='w',pady=(0,10))
    extra_box=tk.Text(advanced_tab,height=10,wrap='word')
    extra_box.pack(fill='x')
    extra_box.insert('1.0',json.dumps(c['api_extra_body'],ensure_ascii=False,indent=2))
    ttk.Label(advanced_tab,text='格式：JSON 对象。普通兼容接口默认 {}；Inferera 默认关闭思考以节省输出。\n'
              '例如 {"temperature": 0.6}；{"temperature": null} 会移除该参数。\n'
              '模型、消息、流式开关和输出长度由机器人控制，不能在这里覆盖。',
              foreground='#555555',wraplength=710).pack(anchor='w',pady=12)
    previous_base={'value':c['api_base']}
    def base_changed(*args):
        try:
            base=normalize_api_base(fields['api_base'].get())
            body=json.loads(extra_box.get('1.0','end').strip() or '{}')
        except ValueError:
            return
        if body==default_extra_body(previous_base['value']):
            extra_box.delete('1.0','end')
            extra_box.insert('1.0',json.dumps(default_extra_body(base),ensure_ascii=False,indent=2))
        previous_base['value']=base
    fields['api_base'].trace_add('write',base_changed)
    status=tk.StringVar(value='可自由填写接口地址与模型；读取模型列表不是必需步骤。')
    ttk.Label(footer,textvariable=status,wraplength=750).pack(anchor='w',pady=(12,8))
    completions=queue.Queue()
    def run_background(action,done):
        def worker():
            try:
                result,error=action(),None
            except Exception as exc:
                result,error=None,exc
            completions.put((done,result,error))
        threading.Thread(target=worker,daemon=True).start()
    def poll_results():
        while True:
            try:
                callback,result,error=completions.get_nowait()
            except queue.Empty:
                break
            callback(result,error)
        root.after(100,poll_results)
    root.after(100,poll_results)
    def read_models():
        try:
            base=normalize_api_base(fields['api_base'].get())
            key=fields['api_key'].get().strip()
            if any(x in key for x in '\r\n'):
                raise ValueError('模型密钥必须为单行文本。')
        except ValueError as exc:
            messagebox.showerror('读取失败',str(exc))
            return
        model_button.configure(state='disabled')
        status.set('正在读取模型列表；不会发送聊天请求。')
        def done(models,error):
            model_button.configure(state='normal' if chat_enabled.get() else 'disabled')
            if fields['api_base'].get().strip().rstrip('/') not in (base,base+'/chat/completions'):
                status.set('接口地址已改变，请重新读取列表。')
                return
            if error:
                status.set(str(error) if isinstance(error,ValueError) else '模型列表暂时无法读取，仍可手动填写。')
            else:
                model_entry.configure(values=models)
                status.set(f'读取到 {len(models)} 个模型；可下拉选择，也可直接输入。')
        run_background(lambda:fetch_model_ids(base,key),done)
    model_button.configure(command=read_models)

    def request(path):
        token=fields['discord_token'].get().strip()
        if not token:
            raise ValueError('请先粘贴 Bot Token。')
        req=urllib.request.Request('https://discord.com/api/v10'+path,
                                   headers={'Authorization':'Bot '+token,'User-Agent':'CetaceaBot/1.0'})
        try:
            with urllib.request.urlopen(req,timeout=15) as r:
                return json.load(r)
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise ValueError('Discord Bot Token 无效。请填 Bot 页面里的 Token，不是 Public Key。') from None
            if exc.code == 403:
                raise ValueError('机器人无权访问这个服务器或频道，请检查安装权限。') from None
            raise ValueError(f'Discord 接口返回 HTTP {exc.code}。') from None
        except urllib.error.URLError:
            raise ValueError('暂时无法连接 Discord，请检查网络。') from None

    def discover():
        try:
            selected={(entry['guild_id'],cid) for entry in parse_server_lines(
                servers_box.get('1.0','end')) for cid in entry['channel_ids']}
            guilds=request('/users/@me/guilds')
            if not guilds:
                messagebox.showinfo('还没有服务器','请先用 Discord 安装链接将机器人加入测试服务器。')
                return
            catalog=[]
            for guild in guilds:
                channels=[item for item in request('/guilds/'+guild['id']+'/channels') if item['type']==0]
                catalog.append((guild,sorted(channels,key=lambda item:(item.get('position',0),item['name']))))
            win=tk.Toplevel(root)
            win.title('勾选机器人活动的频道')
            win.geometry('620x560')
            ttk.Label(win,text='可在多个服务器勾选文字频道；未勾选的频道不会回复。').pack(anchor='w',padx=16,pady=12)
            area=ttk.Frame(win)
            area.pack(fill='both',expand=True,padx=16)
            canvas=tk.Canvas(area,highlightthickness=0)
            bar=ttk.Scrollbar(area,orient='vertical',command=canvas.yview)
            canvas.configure(yscrollcommand=bar.set)
            bar.pack(side='right',fill='y')
            canvas.pack(side='left',fill='both',expand=True)
            inner=ttk.Frame(canvas)
            window_id=canvas.create_window((0,0),window=inner,anchor='nw')
            inner.bind('<Configure>',lambda event:canvas.configure(scrollregion=canvas.bbox('all')))
            canvas.bind('<Configure>',lambda event:canvas.itemconfigure(window_id,width=event.width))
            checks=[]
            for guild,channels in catalog:
                ttk.Label(inner,text=guild['name'],font=('Microsoft YaHei UI',11,'bold')).pack(anchor='w',pady=(12,3))
                if not channels:
                    ttk.Label(inner,text='（没有可用的文字频道）').pack(anchor='w',padx=20)
                for channel in channels:
                    var=tk.BooleanVar(value=(int(guild['id']),int(channel['id'])) in selected)
                    ttk.Checkbutton(inner,text='#'+channel['name'],variable=var).pack(anchor='w',padx=20,pady=2)
                    checks.append((int(guild['id']),int(channel['id']),var))
            def choose():
                by_guild={}
                for gid,cid,var in checks:
                    if var.get():
                        by_guild.setdefault(gid,[]).append(cid)
                if not by_guild:
                    messagebox.showerror('尚未选择','至少勾选一个文字频道。',parent=win)
                    return
                servers=[{'guild_id':gid,'channel_ids':ids} for gid,ids in by_guild.items()]
                servers_box.delete('1.0','end')
                servers_box.insert('1.0',format_server_lines(servers))
                status.set(f'已选 {len(servers)} 个服务器、{sum(map(len,by_guild.values()))} 个频道；点击“保存配置”后生效。')
                win.destroy()
            ttk.Button(win,text='使用勾选的频道',command=choose).pack(pady=12)
        except ValueError as exc:
            messagebox.showerror('读取失败',str(exc))

    def save():
        try:
            token=fields['discord_token'].get().strip()
            api=fields['api_key'].get().strip()
            if not token or any('\n' in x or '\r' in x for x in (token,api)):
                raise ValueError('请填写单行 Bot Token 和模型密钥。')
            form_config=dict(c,chat_enabled=chat_enabled.get(),
                             local_features={k:v.get() for k,v in feature_checks.items()})
            next_config=provider_form_settings(form_config,{k:v.get() for k,v in fields.items()},
                                               extra_box.get('1.0','end'))
            servers=parse_server_lines(servers_box.get('1.0','end'))
            if not servers:
                raise ValueError('请至少选择一个服务器中的一个文字频道。')
            owner=fields['owner_id'].get().strip()
            if owner and not owner.isdigit():
                raise ValueError('主人 ID 应为数字，也可以留空。')
            me=request('/users/@me')
            if not me.get('bot'):
                raise ValueError('请使用机器人的 Bot Token。')
            guilds=request('/users/@me/guilds')
            joined={g['id'] for g in guilds}
            for entry in servers:
                guild_id=str(entry['guild_id'])
                if guild_id not in joined:
                    raise ValueError(f'机器人尚未加入服务器 {guild_id}；请先通过邀请链接安装。')
                channels=request('/guilds/'+guild_id+'/channels')
                visible={x['id'] for x in channels if x['type']==0}
                if not {str(cid) for cid in entry['channel_ids']}.issubset(visible):
                    raise ValueError(f'机器人看不到服务器 {guild_id} 的某个选定文字频道。')
                for channel_id in entry['channel_ids']:
                    request('/channels/'+str(channel_id)+'/messages?limit=1')
            next_config.update(servers=servers,owner_id=int(owner or '0'),
                               auto_memory_enabled=auto_memory.get())
            next_config.pop('guild_id',None)
            next_config.pop('channel_ids',None)
            set_key(str(ROOT/'.env'),'DISCORD_TOKEN',token)
            set_key(str(ROOT/'.env'),'LLM_API_KEY',api)
            config_path.write_text(json.dumps(next_config,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            c.clear()
            c.update(next_config)
            status.set('已保存配置。点击“保存并重启机器人”即可应用。')
            return True
        except ValueError as exc:
            messagebox.showerror('配置未保存',str(exc))
            return False
        except OSError:
            messagebox.showerror('配置未保存','无法写入本机配置文件，请确认文件夹可写。')
            return False

    def start():
        if save():
            start_button.configure(state='disabled')
            status.set('配置已保存，正在重启机器人……')
            def done(result,error):
                start_button.configure(state='normal')
                if error:
                    messagebox.showerror('重启失败',str(error) if isinstance(error,ValueError) else
                                         '机器人重启失败，请用 start.cmd 手动启动。')
                    status.set('配置已保存，仍需手动重启机器人。')
                else:
                    status.set('已启动机器人。等待联网后，在所选频道 @她测试。')
            run_background(restart_bot,done)
    discord_buttons=ttk.Frame(discord_tab)
    discord_buttons.pack(fill='x',pady=(16,10))
    ttk.Button(discord_buttons,text='自动读取服务器和频道',command=discover).pack(side='left')
    ttk.Button(discord_buttons,text='打开 Discord 开发者后台',command=lambda:
               webbrowser.open('https://discord.com/developers/applications')).pack(side='left',padx=8)
    ttk.Label(discord_tab,text='首次使用：在开发者后台创建 Bot，开启 Message Content Intent，\n'
              '将机器人安装到服务器，再粘贴 Token 并选择频道。',wraplength=710).pack(anchor='w')
    buttons=ttk.Frame(footer)
    buttons.pack(fill='x',pady=(3,0))
    ttk.Button(buttons,text='保存配置',command=save).pack(side='left',padx=(0,10))
    start_button=ttk.Button(buttons,text='保存并重启机器人',command=start)
    start_button.pack(side='left')
    ttk.Button(buttons,text='关闭',command=root.destroy).pack(side='right')
    root.mainloop()

if __name__=='__main__':
    main()
