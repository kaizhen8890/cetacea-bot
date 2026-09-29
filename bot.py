import asyncio
import contextlib
import logging
import logging.handlers
import re
import socket
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
import aiohttp
import discord
from settings import ROOT, load_settings
from storage import Store, BudgetExceeded
from engine import Context, Policy, ConversationTracker, LLM, APIError, clip, safe_text, explanation_request, choose_emote, EMOTE_NAMES
from memory import record as record_memory, mark_engaged, retrieve as retrieve_memory, erase_message, erase_user, status as memory_status, next_batch, pending_fold
from memory_worker import model_ready, resource_ready

LOG=logging.getLogger('cetacea')
HELP=('🐳 我是 DeepSeek 鲸鱼娘。@我或回复我开始聊天；我会接上两分钟内的自然追问。平时也会偶尔插话、用群内表情。\n'
      '本地命令（不调用模型）：\n'
      '`!鲸鱼 状态` · `!鲸鱼 帮助`\n'
      '`!鲸鱼 记住 称呼=小明`（同名覆盖，可用于纠正）\n'
      '`!鲸鱼 记忆` · `!鲸鱼 记忆状态` · `!鲸鱼 忘记 称呼` · `!鲸鱼 清空记忆`\n'
      '`!鲸鱼 关闭记忆` · `!鲸鱼 开启记忆`\n'
      '管理员：`!鲸鱼 暂停`、`!鲸鱼 恢复`、`!鲸鱼 清空上下文`\n'
      '自动记忆在同一服务器的公开频道间共享；受限频道只留在本频道。关闭记忆会清除你在本服务器的已存发言与摘要。\n'
      '回复时会将当前发言、少量频道上下文和相关记忆发送至配置的模型供应商。')

class Whale(discord.Client):
    def __init__(self,c,store):
        intents=discord.Intents.none()
        intents.guilds=True
        intents.guild_messages=True
        intents.message_content=True
        super().__init__(intents=intents,allowed_mentions=discord.AllowedMentions.none(),
                         max_messages=100,member_cache_flags=discord.MemberCacheFlags.none())
        self.c,self.store=c,store
        self.allowed_channels={entry['guild_id']:frozenset(entry['channel_ids'])
                               for entry in c['servers']}
        self.context=Context(c)
        self.policy=Policy(c)
        self.dialogue=ConversationTracker(c['conversation_followup_seconds'])
        self.pending={}
        self.workers={}
        self.kinds={}
        self.channel_locks=defaultdict(asyncio.Lock)
        self.last_notice={}
        self.available_emotes={}
        self.session=None
        self.memory_task=None

    async def setup_hook(self):
        self.session=aiohttp.ClientSession()
        self.llm=LLM(self.c,self.store,self.session)
        if self.c['auto_memory_enabled']:
            self.memory_task=asyncio.create_task(self.memory_scheduler())

    async def close(self):
        if self.memory_task:
            self.memory_task.cancel()
            await asyncio.gather(self.memory_task,return_exceptions=True)
        for task in list(self.workers.values()):
            task.cancel()
        if self.workers:
            await asyncio.gather(*self.workers.values(),return_exceptions=True)
        if self.session:
            await self.session.close()
        await super().close()

    async def on_ready(self):
        LOG.info('鲸鱼娘已上线：%s；允许服务器数：%s；允许频道数：%s',self.user,
                 len(self.allowed_channels),sum(len(ids) for ids in self.allowed_channels.values()))
        self.available_emotes={}
        for gid,channel_ids in self.allowed_channels.items():
            guild=self.get_guild(gid)
            if guild is None:
                LOG.warning('配置服务器不可见：%s',gid)
                continue
            try:
                available=await guild.fetch_emojis()
                self.available_emotes[gid]={emoji.name:str(emoji) for emoji in available
                    if emoji.name in EMOTE_NAMES and emoji.available}
                LOG.info('服务器 %s 可用的群内表情：%s 个',gid,len(self.available_emotes[gid]))
            except discord.HTTPException:
                self.available_emotes[gid]={}
                LOG.warning('服务器 %s 暂时无法读取群内表情；文字聊天照常运行',gid)
            for cid in channel_ids:
                channel=self.get_channel(cid)
                if channel is None or channel.guild.id!=gid:
                    LOG.warning('配置频道不可见或不属于指定服务器：%s/%s',gid,cid)
        print('鲸鱼娘已上线。在指定频道 @机器人，或发送 !鲸鱼 帮助。',flush=True)

    async def on_error(self,event,*args,**kwargs):
        # Do not log raw Discord messages, tokens, or provider response bodies.
        LOG.error('事件处理异常：%s；未记录聊天内容',event)

    def allowed(self,m):
        return (m.guild is not None and m.channel.id in self.allowed_channels.get(m.guild.id,())
                and isinstance(m.channel,discord.TextChannel)
                and not m.author.bot and not m.webhook_id)

    def admin(self,m):
        return (m.author.id==int(self.c['owner_id']) or m.author.guild_permissions.manage_guild)

    def memory_key(self,m):
        return f'memory_off:{m.guild.id}:{m.author.id}'

    def memory_on(self,m):
        return (self.c['memory_enabled'] and self.store.pref(self.memory_key(m))!='1'
                and self.store.pref(f'memory_off:{m.guild.id}:{m.channel.id}:{m.author.id}')!='1')

    def shareable(self,channel):
        return bool(channel.permissions_for(channel.guild.default_role).view_channel)

    def personal_memories(self,m):
        rows=[]
        for r in self.store.guild_memories(m.guild.id,m.author.id):
            source=self.get_channel(int(r['channel']))
            if r['channel']==str(m.channel.id) or (source and self.shareable(source)):
                rows.append({'key':r['key'],'value':r['value']})
        return rows[:5]

    async def memory_scheduler(self):
        await asyncio.sleep(90)
        while True:
            try:
                defer=float(self.store.pref('memory_defer_until','0'))
                due=next_batch(self.store.db) or any(
                    len(pending_fold(self.store.db,gid))>=10 for gid in self.allowed_channels)
                if time.time()>=defer and due and (
                        await asyncio.to_thread(model_ready,self.c['local_memory_model'])) and (
                        await asyncio.to_thread(resource_ready)):
                    code=0
                    if self.c['auto_memory_prompt']:
                        proc=await asyncio.create_subprocess_exec(sys.executable,str(ROOT/'memory_worker.py'),
                            '--prompt',cwd=str(ROOT),stdout=asyncio.subprocess.DEVNULL,
                            stderr=asyncio.subprocess.DEVNULL)
                        code=await proc.wait()
                    delays={1:1800,2:7200,3:86400}
                    if code in delays:
                        self.store.set_pref('memory_defer_until',str(time.time()+delays[code]))
                    elif code==0 and await asyncio.to_thread(resource_ready):
                        proc=await asyncio.create_subprocess_exec(sys.executable,str(ROOT/'memory_worker.py'),
                            '--run',self.c['local_memory_model'],cwd=str(ROOT),
                            stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
                        if await proc.wait()!=0:
                            LOG.warning('本地记忆整理暂时失败；原始记录保留待重试')
                            self.store.set_pref('memory_defer_until',str(time.time()+3600))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.warning('本地记忆调度暂停：%s',type(exc).__name__)
            await asyncio.sleep(300)

    def history_key(self,m,uid=None):
        return f'history_after:{m.guild.id}:{m.channel.id}:{m.author.id if uid is None else uid}'

    def context_key(self,m):
        return f'context_after:{m.guild.id}:{m.channel.id}'

    def _historical_allowed(self,m,old):
        if old.channel.id!=m.channel.id or not old.content.strip():
            return False
        own_reset=self.store.pref(f'memory_reset_at:{m.guild.id}:{m.author.id}')
        if own_reset and getattr(old,'created_at',None) and old.created_at.timestamp()<=float(own_reset):
            return False
        if old.author.bot and old.author.id!=self.user.id:
            return False
        if old.content.strip().startswith('!鲸鱼'):
            return False
        if not old.author.bot and (self.store.pref(
                f'memory_off:{m.guild.id}:{old.author.id}')=='1' or self.store.pref(
                f'memory_off:{m.guild.id}:{m.channel.id}:{old.author.id}')=='1'):
            return False
        if not old.author.bot:
            reset=self.store.pref(f'memory_reset_at:{m.guild.id}:{old.author.id}')
            if reset and getattr(old,'created_at',None) and old.created_at.timestamp()<=float(reset):
                return False
        for key in (self.context_key(m),self.history_key(m,old.author.id)):
            value=self.store.pref(key)
            if value and value!='off' and old.id<=int(value):
                return False
        created=getattr(old,'created_at',None)
        if created and (datetime.now(timezone.utc)-created).total_seconds()>self.c['context_ttl_seconds']:
            return False
        return True

    async def recent_history(self,m,before):
        rows=[]
        try:
            async for old in m.channel.history(limit=max(20,self.c['context_messages']*4),before=before):
                if not self._historical_allowed(m,old):
                    continue
                rows.append(dict(mid=old.id,uid=old.author.id,name=clip(old.author.display_name,32),
                    text=clip(safe_text(old.content),self.c['context_chars_per_message']),
                    role='assistant' if old.author.id==self.user.id else 'user',time=0))
                if len(rows)>=self.c['context_messages']:
                    break
            rows.reverse()
            return rows
        except (discord.HTTPException,AttributeError):
            LOG.warning('无法读取近期频道消息，改用内存中的上下文')
            return None

    async def reply_target(self,m):
        ref=m.reference
        if not ref or not ref.message_id or (ref.channel_id and ref.channel_id!=m.channel.id):
            return None
        if isinstance(ref.resolved,discord.Message):
            return ref.resolved
        try:
            return await m.channel.fetch_message(ref.message_id)
        except (discord.HTTPException,AttributeError):
            return None

    async def send(self,m,text,reference=True):
        # Limit output and suppress all generated mention notifications, including @everyone.
        return await m.channel.send(clip(text,1850),
            reference=m.to_reference(fail_if_not_exists=False) if reference else None,
            allowed_mentions=discord.AllowedMentions.none())

    async def notice(self,m,text,interval=20):
        now=asyncio.get_running_loop().time()
        key=(m.channel.id,m.author.id)
        if now-self.last_notice.get(key,float('-inf'))<interval:
            return
        self.last_notice[key]=now
        await self.send(m,text)

    async def command(self,m,text):
        if not text.startswith('!鲸鱼'):
            return False
        self.dialogue.clear(m.channel.id)
        cmd=text[len('!鲸鱼'):].strip()
        scope=(m.guild.id,m.channel.id,m.author.id)
        if cmd in ('','帮助'):
            reply=HELP
        elif cmd=='状态':
            u=self.store.usage()
            paused=self.store.pref(f'paused:{m.channel.id}')=='1'
            reply=(f'🐳 {"暂停聊天" if paused else "正在听群友聊天"}。\n'
                   f'今日估算 ¥{u["cost"]:.4f} / ¥{self.c["daily_budget_rmb"]:.2f}；'
                   f'调用 {u["calls"]} 次，其中插话 {u["casual_calls"]} 次。\n'
                   f'普通插话：上次回复后至少 {self.c["casual_min_messages"]} 条消息、'
                   f'{self.c["casual_cooldown_seconds"]//60} 分钟冷却；之后每条约 '
                   f'{self.c["casual_probability"]:.0%} 概率。\n'
                   f'续聊窗口：上次回答后 {self.c["conversation_followup_seconds"]//60} 分钟内，'
                   '同一位群友的自然追问无需再次 @。\n'
                   '按 UTC+8 零点换日，实际账单以供应商为准。')
        elif cmd=='记忆':
            rows=self.personal_memories(m)
            reply='本服务器里你主动保存且本频道可用的记忆：\n'+('\n'.join(f'{r["key"]}={r["value"]}' for r in rows) or '暂无。')
        elif cmd=='记忆状态':
            info=memory_status(self.store.db)
            reply=(f'本地已整理 {info["summaries"]} 条摘要；还有 {info["pending"]} 条消息待整理。'
                   '整理仅使用本地模型，电脑忙或模型未安装时会延后。')
        elif cmd.startswith('记住 '):
            if not self.memory_on(m):
                reply='记忆已关闭，先用 !鲸鱼 开启记忆。'
            elif '=' not in cmd:
                reply='格式：!鲸鱼 记住 称呼=小明'
            else:
                key,value=cmd[3:].split('=',1)
                try:
                    self.store.remember(*scope,safe_text(key),safe_text(value),self.c['memory_max_items'])
                    reply='记住啦；公开频道的记忆在本服务器里也能用到。'
                except ValueError as exc:
                    reply=str(exc)
        elif cmd.startswith('忘记 '):
            self.store.forget(*scope,cmd[3:].strip())
            self.context.forget_user(m.channel.id,m.author.id)
            self.store.set_pref(self.history_key(m),str(m.id))
            reply='这条记忆已删除，也清掉了你的近期发言上下文。'
        elif cmd in ('清空记忆','关闭记忆'):
            erase_user(self.store.db,m.guild.id,m.author.id)
            self.store.set_pref(f'memory_reset_at:{m.guild.id}:{m.author.id}',str(time.time()))
            for channel_id in self.allowed_channels.get(m.guild.id,()):
                self.context.forget_user(channel_id,m.author.id)
            if self.dialogue.active.get(m.channel.id,(None,))[0]==m.author.id:
                self.dialogue.clear(m.channel.id)
            self.store.set_pref(self.history_key(m),str(m.id))
            # Discard pending replies that might already carry deleted content.
            task=self.workers.get((m.channel.id,m.author.id))
            if task:
                task.cancel()
            if cmd=='关闭记忆':
                self.store.set_pref(self.memory_key(m),'1')
            reply='你在本服务器的本地记忆和记录已清空。'+('记忆已关闭。' if cmd=='关闭记忆' else '')
        elif cmd=='开启记忆':
            if not self.c['memory_enabled']:
                reply='管理员在本机关闭了全部记忆。'
            else:
                self.store.set_pref(self.memory_key(m),'0')
                for channel_id in self.allowed_channels.get(m.guild.id,()):
                    self.store.set_pref(f'memory_off:{m.guild.id}:{channel_id}:{m.author.id}','0')
                reply='记忆已开启，可以用 !鲸鱼 记住 名称=内容 保存。'
        elif cmd in ('暂停','恢复','清空上下文'):
            if not self.admin(m):
                reply='这个命令需要服务器管理权限或本机指定的主人身份。'
            else:
                if cmd=='清空上下文':
                    self.context.items.pop(m.channel.id,None)
                    self.dialogue.clear(m.channel.id)
                    self.store.set_pref(self.context_key(m),str(m.id))
                else:
                    self.store.set_pref(f'paused:{m.channel.id}','1' if cmd=='暂停' else '0')
                    if cmd=='暂停':
                        self.dialogue.clear(m.channel.id)
                if cmd in ('暂停','清空上下文'):
                    for key,task in list(self.workers.items()):
                        if key[0]==m.channel.id:
                            task.cancel()
                reply={'暂停':'好啦，我先潜水。','恢复':'鲸鱼娘回来了。','清空上下文':'本频道短期上下文已清空。'}[cmd]
        else:
            reply='不认识这个命令，发送 !鲸鱼 帮助 查看用法。'
        await self.notice(m,reply,interval=0)
        return True

    async def on_message(self,m):
        if not self.allowed(m):
            return
        text=m.content.strip()
        if await self.command(m,text):
            return
        if not text or self.store.pref(f'paused:{m.channel.id}')=='1':
            return
        assert self.user is not None
        direct=any(u.id==self.user.id for u in m.mentions)
        target=await self.reply_target(m) if m.reference else None
        if target is not None:
            direct=direct or target.author.id==self.user.id
        clean=re.sub(rf'<@!?{self.user.id}>','',text).strip() or '鲸鱼娘，在吗？'
        followup=False
        if direct:
            active=self.dialogue.active.get(m.channel.id)
            if active and active[0]!=m.author.id:
                self.dialogue.clear(m.channel.id)
        else:
            followup=self.dialogue.is_followup(m.channel.id,m.author.id,clean,
                reply_to_other=m.reference is not None,mention_other=bool(m.mentions))
        if self.c['auto_memory_enabled'] and self.memory_on(m):
            record_memory(self.store.db,m.id,m.guild.id,m.channel.id,m.author.id,
                m.author.display_name,safe_text(text),engaged=direct or followup,
                public=self.shareable(m.channel))
        # Short-term context is volatile. Opt-out also excludes future passive caching.
        if self.memory_on(m):
            self.context.add(m.channel.id,m.id,m.author.id,m.author.display_name,clean)
        self.policy.observe(m.channel.id)
        key=(m.channel.id,m.author.id)
        if key in self.workers:
            if direct:
                self.kinds[key]='mention'
            elif followup and self.kinds.get(key)=='casual':
                self.kinds[key]='continuation'
            batch=self.pending[key]
            if len(batch)<8:
                batch.append((m,clean,target))
            elif direct:
                await self.notice(m,'消息太密啦，等我回完这一轮再聊。')
            return
        if not direct and not followup:
            if any(worker_channel==m.channel.id for worker_channel,_ in self.workers) or not self.policy.eligible(m.channel.id,clean):
                return
        if len(self.workers)>=8:
            if direct or followup:
                await self.notice(m,'现在有点忙，稍后再叫我吧。')
            return
        self.pending[key]=[(m,clean,target)]
        kind='mention' if direct else 'continuation' if followup else 'casual'
        self.kinds[key]=kind
        self.workers[key]=asyncio.create_task(self.respond(key,kind))

    async def respond(self,key,kind):
        try:
            await asyncio.sleep(self.c['reply_delay_seconds'])
            # A short bounded queue serializes replies per channel; API calls are globally serial.
            async with self.channel_locks[key[0]]:
                rounds=0
                while self.pending.get(key) and rounds<3:
                    kind=self.kinds.get(key,kind)
                    batch=self.pending[key][:]
                    self.pending[key].clear()
                    m=batch[-1][0]
                    if self.store.pref(f'paused:{m.channel.id}')=='1':
                        return
                    memories=self.personal_memories(m) if self.memory_on(m) else []
                    recent=await self.recent_history(m,batch[0][0])
                    target=next((target for _,_,target in reversed(batch) if target is not None),None)
                    reference=None
                    if target is not None and self._historical_allowed(m,target):
                        reference={'群友':clip(target.author.display_name,32),
                            '发言':clip(safe_text(target.content),self.c['context_chars_per_message'])}
                        # Discord replies may form a chain: the bot's short answer often
                        # points back to the question the user is following up on.
                        parent=await self.reply_target(target)
                        if parent is not None and parent.id!=target.id and self._historical_allowed(m,parent):
                            reference['上一级引用']={'群友':clip(parent.author.display_name,32),
                                '发言':clip(safe_text(parent.content),self.c['context_chars_per_message'])}
                    current='\n'.join(t for _,t,_ in batch)
                    retrieval_query=current+' '+ ' '.join(r['text'] for r in (recent or [])[-2:])
                    auto_memories=(retrieve_memory(self.store.db,m.guild.id,m.channel.id,
                        m.author.id,retrieval_query,limit=3) if self.c['auto_memory_enabled'] and self.memory_on(m) else [])
                    explain=explanation_request(current)
                    excluded={x.id for x,_,_ in batch}
                    if reference is not None:
                        excluded.add(target.id)
                    messages=self.context.build(m.channel.id,excluded,m.author.display_name,
                        current,memories,m.author.id==int(self.c['owner_id']),recent=recent,
                        reference=reference,explain=explain,continuation=kind=='continuation',
                        auto_memories=auto_memories)
                    async with m.channel.typing():
                        result=await self.llm.chat(messages,kind,
                            max_tokens=self.c['explanation_max_output_tokens'] if explain else self.c['max_output_tokens'])
                    if self.store.pref(f'paused:{m.channel.id}')=='1':
                        return
                    if kind=='continuation' and result.strip()=='[[NO_REPLY]]':
                        self.dialogue.clear(m.channel.id)
                        rounds+=1
                        continue
                    emote='' if explain else choose_emote(current+' '+result,
                        self.available_emotes.get(m.guild.id,{}),
                        self.c['emoji_probability'])
                    reply=result+(' '+emote if emote else '')
                    sent=await self.send(m,reply,reference=kind!='casual')
                    if self.c['auto_memory_enabled'] and self.memory_on(m):
                        mark_engaged(self.store.db,[x.id for x,_,_ in batch])
                        if getattr(sent,'id',None):
                            record_memory(self.store.db,sent.id,m.guild.id,m.channel.id,
                                self.user.id,'鲸鱼娘',reply,role='assistant',engaged=True,
                                public=self.shareable(m.channel),subject=m.author.id)
                    if self.memory_on(m):
                        self.context.add(m.channel.id,getattr(sent,'id',0),self.user.id,'鲸鱼娘',reply,'assistant')
                    if kind!='casual':
                        self.dialogue.mark_reply(m.channel.id,m.author.id,getattr(sent,'id',0))
                    self.policy.replied(m.channel.id)
                    rounds+=1
                if self.pending.get(key):
                    await self.notice(self.pending[key][-1][0],'这轮消息有点多，后面的内容请再叫我一次。')
        except (BudgetExceeded,APIError) as exc:
            if kind!='casual':
                await self.notice(m,str(exc))
            LOG.info('回复未完成：%s',type(exc).__name__)
        except discord.HTTPException:
            LOG.warning('Discord 消息发送失败；不会重新请求模型')
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.error('回复异常：%s',type(exc).__name__)
        finally:
            self.pending.pop(key,None)
            self.workers.pop(key,None)
            self.kinds.pop(key,None)

    async def on_raw_message_delete(self,payload):
        if self.c['auto_memory_enabled']:
            erase_message(self.store.db,payload.message_id)
        self.context.remove(payload.channel_id,payload.message_id)
        self.dialogue.remove_reply(payload.channel_id,payload.message_id)
        for key,batch in list(self.pending.items()):
            if key[0]==payload.channel_id:
                self.pending[key]=[(m,t,r) for m,t,r in batch if m.id!=payload.message_id]

    async def on_raw_message_edit(self,payload):
        # Edited messages do not trigger a paid call, and old text is not reused.
        await self.on_raw_message_delete(payload)

async def run():
    c=load_settings(require_discord=True)
    (ROOT/'data').mkdir(exist_ok=True)
    # OS releases the lock on process exit, including crashes. No stale PID file.
    guard=socket.socket()
    if hasattr(socket,'SO_EXCLUSIVEADDRUSE'):
        guard.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
    try:
        guard.bind(('127.0.0.1',47831))
    except OSError:
        raise ValueError('机器人已在运行，或本机 47831 端口被占用。') from None
    store=Store(ROOT/'data/whale.sqlite3')
    try:
        async with Whale(c,store) as client:
            await client.start(c['discord_token'])
    finally:
        store.close()
        guard.close()

if __name__=='__main__':
    (ROOT/'data').mkdir(exist_ok=True)
    handler=logging.handlers.RotatingFileHandler(ROOT/'data/bot.log',maxBytes=300000,backupCount=2,encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    LOG.setLevel(logging.INFO)
    LOG.addHandler(handler)
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print('鲸鱼娘已下线。')
    except discord.LoginFailure:
        print('Discord Token 无效，请打开配置窗口检查。')
    except discord.PrivilegedIntentsRequired:
        print('请在 Discord 开发者后台 Bot 页面开启 Message Content Intent。')
    except ValueError as exc:
        print(str(exc))
    except Exception as exc:
        print(f'启动失败：{type(exc).__name__}；请检查网络连接和配置。')
