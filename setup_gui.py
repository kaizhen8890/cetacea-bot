"""Small local-only configuration window. Never renders a secret in logs."""
import json
import re
import subprocess
import sys
import tkinter as tk
import urllib.request
import urllib.error
import webbrowser
from tkinter import ttk,messagebox
from dotenv import dotenv_values,set_key
from settings import ROOT, configured_servers

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
    c=json.loads((config_path if config_path.exists() else template_path).read_text(encoding='utf-8-sig'))
    secrets=dotenv_values(ROOT/'.env')
    root=tk.Tk()
    root.title('鲸鱼娘 · 本机配置')
    root.geometry('800x760')
    root.minsize(750,700)
    frame=ttk.Frame(root,padding=22)
    frame.pack(fill='both',expand=True)
    ttk.Label(frame,text='🐳 DeepSeek 鲸鱼娘',font=('Microsoft YaHei UI',19,'bold')).pack(anchor='w')
    ttk.Label(frame,text='密钥仅保存在本机 .env 文件，不会显示在日志里。保存后重启机器人生效。').pack(anchor='w',pady=(8,18))
    form=ttk.Frame(frame)
    form.pack(fill='x')
    form.columnconfigure(1,weight=1)
    fields={}
    specs=[('discord_token','Bot 页面 Token（非 Public Key）',secrets.get('DISCORD_TOKEN',''),True),
           ('api_key','模型 API 密钥',secrets.get('LLM_API_KEY',''),True),
           ('model','模型名称',c['model'],False),
           ('owner_id','主人用户 ID（可留空）',str(c['owner_id']) if c['owner_id'] else '',False)]
    for i,(key,label,value,secret) in enumerate(specs):
        ttk.Label(form,text=label).grid(row=i,column=0,sticky='w',pady=7,padx=(0,12))
        var=tk.StringVar(value=value)
        ttk.Entry(form,textvariable=var,show='●' if secret else '').grid(row=i,column=1,sticky='ew')
        fields[key]=var
    ttk.Label(frame,text='允许聊天的服务器与文字频道（每行一个服务器；推荐用下方按钮勾选）').pack(anchor='w',pady=(16,5))
    servers_box=tk.Text(frame,height=5,wrap='word')
    servers_box.pack(fill='x')
    servers_box.insert('1.0',format_server_lines(configured_servers(c)))
    ttk.Label(frame,text='手动格式：服务器ID: 频道ID, 频道ID',foreground='#777777').pack(anchor='w',pady=(3,0))
    auto_memory=tk.BooleanVar(value=c.get('auto_memory_enabled',False))
    ttk.Checkbutton(frame,text='记录已选频道消息，并用本地 Qwen 定时整理记忆（需另行安装 Ollama 模型）',
                    variable=auto_memory).pack(anchor='w',pady=(9,0))
    status=tk.StringVar(value='每天 ¥2 预算由所有所选服务器共用，保留 10% 余量；保存后重启生效。')
    ttk.Label(frame,textvariable=status,wraplength=690).pack(anchor='w',pady=16)

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
            if not token or not api or any('\n' in x or '\r' in x for x in (token,api)):
                raise ValueError('请填写单行 Bot Token 和模型密钥。')
            servers=parse_server_lines(servers_box.get('1.0','end'))
            if not servers:
                raise ValueError('请至少选择一个服务器中的一个文字频道。')
            owner=fields['owner_id'].get().strip()
            if owner and not owner.isdigit():
                raise ValueError('主人 ID 应为数字，也可以留空。')
            model=fields['model'].get().strip()
            if model!=c['model']:
                raise ValueError('更换模型前，请在 config.json 同时核对该模型价格。')
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
            c.update(servers=servers,owner_id=int(owner or '0'),
                     auto_memory_enabled=auto_memory.get())
            c.pop('guild_id',None)
            c.pop('channel_ids',None)
            set_key(str(ROOT/'.env'),'DISCORD_TOKEN',token)
            set_key(str(ROOT/'.env'),'LLM_API_KEY',api)
            (ROOT/'config.json').write_text(json.dumps(c,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            status.set('已保存。关闭旧的机器人窗口后，点击“启动机器人”。')
            return True
        except ValueError as exc:
            messagebox.showerror('配置未保存',str(exc))
            return False

    def start():
        if save():
            subprocess.Popen([sys.executable,'-X','utf8',str(ROOT/'bot.py')],cwd=ROOT,
                             creationflags=getattr(subprocess,'CREATE_NEW_CONSOLE',0))
    buttons=ttk.Frame(frame)
    buttons.pack(fill='x',pady=5)
    for text,fn in [('打开开发者后台',lambda:webbrowser.open('https://discord.com/developers/applications')),
                    ('自动读取服务器和频道',discover),('保存配置',save),('启动机器人',start)]:
        ttk.Button(buttons,text=text,command=fn).pack(side='left',padx=(0,8))
    ttk.Separator(frame).pack(fill='x',pady=18)
    ttk.Label(frame,text='首次使用\n1. 在开发者后台新建 Application，进入 Bot 页面生成 Token。\n'
                        '2. 开启 Message Content Intent，将机器人安装到想使用的服务器。\n'
                        '3. 将 Token 粘贴到这里，点击“自动读取服务器和频道”，跨服务器勾选频道。\n'
                        '4. 保存并重启机器人，在选中的频道 @鲸鱼娘。\n\n'
                        '日志：data/bot.log    人设：persona.txt    费用与频率：config.json',
              justify='left',wraplength=690).pack(anchor='w')
    root.mainloop()

if __name__=='__main__':
    main()
