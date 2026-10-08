import asyncio
import contextlib
import hashlib
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
from engine import Context, Policy, ConversationTracker, LLM, APIError, MessageChanged, MessageBurst, join_fragments, compact_history, clip, safe_text, explanation_request, choose_emote, EMOTE_NAMES, teaching_options
from memory import record as record_memory, mark_engaged, retrieve as retrieve_memory, erase_message, erase_user, status as memory_status, next_batch, pending_fold
from memory_worker import model_ready, resource_ready
from local_features import LocalFeatures,LocalResult,TOOL_HELP,natural_command,poll_text
from local_discord import LocalCommandTree,LocalActionView,LocalPollView,command_group
from wordle_discord import WordleService,is_wordle
from long_answers import writing_request,continue_request
from long_discord import LongAnswers

LOG=logging.getLogger('cetacea')
HELP=('🐳 我是 DeepSeek 鲸鱼娘。@我或回复我开始聊天；我会接上两分钟内的自然追问。平时也会偶尔插话、用群内表情。\n'
      '本地命令（不调用模型）：\n'
      '`!鲸鱼 状态` · `!鲸鱼 帮助`\n'
      '`!鲸鱼 工具` 查看掷骰、选择、计算、换算、提醒和鲸鱼互动；也可用 `/鲸鱼`。\n'
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
        self.poll_locks=defaultdict(asyncio.Lock)
        self.last_notice={}
        self.available_emotes={}
        self.session=None
        self.memory_task=None
        self.reminder_task=None
        self.llm=None
        self.local=LocalFeatures(c,store)
        self.wordle=WordleService(self)
        self.long_answers=LongAnswers(self)
        self.tree=LocalCommandTree(self)
        self.tree.add_command(command_group(self))

    async def setup_hook(self):
        self.session=aiohttp.ClientSession()
        self.llm=LLM(self.c,self.store,self.session) if self.c.get('chat_enabled',True) else None
        self.local.db.recover()
        self.local.db.prune()
        self.wordle.restore()
        self.long_answers.restore()
        for row in self.local.db.open_polls():
            gid,cid=int(row['guild']),int(row['channel'])
            if cid in self.allowed_channels.get(gid,()):
                poll=self.local.db.poll(row['id'],gid,cid)
                self.add_view(LocalPollView(self,poll),message_id=int(row['message']))
        self.reminder_task=asyncio.create_task(self.reminder_scheduler())
        for gid in self.allowed_channels:
            guild=discord.Object(id=gid)
            self.tree.copy_global_to(guild=guild)
            try:
                await self.tree.sync(guild=guild)
                LOG.info('服务器 %s 的 /鲸鱼 本地命令已同步',gid)
            except discord.HTTPException as exc:
                LOG.warning('服务器 %s 斜杠命令暂未同步：%s；文字指令仍可用',gid,type(exc).__name__)
        if self.c['auto_memory_enabled']:
            self.memory_task=asyncio.create_task(self.memory_scheduler())

    async def close(self):
        if self.reminder_task:
            self.reminder_task.cancel()
            await asyncio.gather(self.reminder_task,return_exceptions=True)
        if self.memory_task:
            self.memory_task.cancel()
            await asyncio.gather(self.memory_task,return_exceptions=True)
        for task in list(self.workers.values()):
            task.cancel()
        if self.workers:
            await asyncio.gather(*self.workers.values(),return_exceptions=True)
        await self.wordle.player.close()
        await self.long_answers.close()
        if self.session:
            await self.session.close()
        await super().close()

    async def on_ready(self):
        LOG.info('鲸鱼娘已上线：%s；允许服务器数：%s；允许频道数：%s',self.user,
                 len(self.allowed_channels),sum(len(ids) for ids in self.allowed_channels.values()))
        LOG.info('聊天模式：%s；本地工具已就绪','云端聊天开启' if self.c.get('chat_enabled',True) else '纯本地')
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

    def _historical_allowed(self,m,old,*,explicit=False):
        if old.channel.id!=m.channel.id or not old.content.strip():
            return False
        own_reset=self.store.pref(f'memory_reset_at:{m.guild.id}:{m.author.id}')
        if own_reset and getattr(old,'created_at',None) and old.created_at.timestamp()<=float(own_reset):
            return False
        if old.author.bot and old.author.id!=self.user.id:
            return False
        if old.content.strip().startswith('!鲸鱼'):
            return False
        if self.local.db.reply_command(old.id,m.guild.id,m.channel.id) is not None:
            return False
        if self.local.db.is_input(old.id,m.guild.id,m.channel.id):
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
        # A deliberate reply selects its own context, even after passive history expires.
        # All scope, deletion/reset, opt-out and local-tool exclusions above still apply.
        if not explicit and created and (datetime.now(timezone.utc)-created).total_seconds()>self.c['context_ttl_seconds']:
            return False
        return True

    async def recent_history(self,m,before):
        rows=[]
        seen_answers=set()
        try:
            async for old in m.channel.history(limit=max(40,self.c['context_messages']*8),before=before):
                if not self._historical_allowed(m,old):
                    continue
                answer=self.long_answers.by_message(old.id,m.guild.id,m.channel.id,self.memory_on(m))
                text=old.content
                if answer and old.author.id==self.user.id:
                    token=(answer['volatile'],answer['id'])
                    if token in seen_answers:
                        continue
                    seen_answers.add(token)
                    text=self.long_answers.summary(answer)
                rows.append(dict(mid=old.id,uid=old.author.id,name=clip(old.author.display_name,32),
                    text=clip(safe_text(text),self.c['context_chars_per_message']),
                    role='assistant' if old.author.id==self.user.id else 'user',
                    time=old.created_at.timestamp() if getattr(old,'created_at',None) else 0,
                    break_before=bool(old.reference or old.mentions)))
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
        return await self.send(m,text)

    def reaction_file(self,asset):
        if asset not in ('feed','pat'):
            return None
        for suffix in ('.gif','.png','.jpg','.jpeg','.webp'):
            path=ROOT/'data/reactions'/(asset+suffix)
            try:
                if path.is_file() and path.stat().st_size<=4*1024*1024:
                    return discord.File(path)
            except OSError:
                LOG.warning('本地互动图片暂时无法读取；使用文字和群内表情')
        return None

    def local_presentation(self,result,guild,channel):
        text=result.text
        if result.category=='interaction' and text:
            emote=choose_emote(text,self.available_emotes.get(guild,{}),self.c['emoji_probability'])
            if emote:
                text+=' '+emote
        kwargs={'allowed_mentions':discord.AllowedMentions.none()}
        if result.poll:
            kwargs['view']=LocalPollView(self,self.local.db.poll(result.poll,guild,channel))
        elif result.buttons:
            kwargs['view']=LocalActionView(self,result.buttons,guild,channel)
        file=self.reaction_file(result.asset)
        if file:
            kwargs['file']=file
        return clip(text,1850),kwargs,file

    async def send_local(self,m,result):
        self.local.db.remember_input(m.id,m.guild.id,m.channel.id)
        if not result.text:
            return
        text,kwargs,file=self.local_presentation(result,m.guild.id,m.channel.id)
        try:
            sent=await m.channel.send(text,reference=m.to_reference(fail_if_not_exists=False),**kwargs)
            self.local.db.remember_reply(getattr(sent,'id',None),m.guild.id,m.channel.id,result.command)
            if result.poll:
                self.local.db.poll_message(result.poll,getattr(sent,'id',None))
                await self.update_closed_poll(result.poll,m.channel)
        finally:
            if file:
                file.close()

    async def local_interaction(self,interaction,command):
        guild,channel=interaction.guild_id,interaction.channel_id
        if (guild is None or channel not in self.allowed_channels.get(guild,()) or
                not isinstance(interaction.channel,discord.TextChannel)):
            await interaction.response.send_message('此频道尚未启用鲸鱼娘，请在已选文字频道使用。',ephemeral=True)
            return
        user=interaction.user
        is_admin=user.id==int(self.c['owner_id']) or user.guild_permissions.manage_guild
        public=self.shareable(interaction.channel) if command.startswith('群规 ') else False
        result=self.local.execute(guild,channel,user.id,command,admin=is_admin,public=public)
        if result is None:
            result=LocalResult('不认识这个本地指令，请用 /鲸鱼 工具 查看用法。',private=True)
        if not result.text:
            result=LocalResult('等十秒再来玩嘛。',private=True)
        text,kwargs,file=self.local_presentation(result,guild,channel)
        try:
            if file:
                await interaction.response.defer(thinking=True,ephemeral=result.private)
                sent=await interaction.followup.send(text,ephemeral=result.private,wait=True,**kwargs)
            else:
                await interaction.response.send_message(text,ephemeral=result.private,**kwargs)
                sent=await interaction.original_response()
            if not result.private:
                self.local.db.remember_reply(getattr(sent,'id',None),guild,channel,result.command)
            if result.poll:
                self.local.db.poll_message(result.poll,getattr(sent,'id',None))
                await self.update_closed_poll(result.poll,interaction.channel)
        finally:
            if file:
                file.close()

    async def update_closed_poll(self,pid,channel):
        poll=self.local.db.poll(pid,channel.guild.id,channel.id)
        if poll['status']=='closed' and poll['message']:
            try:
                await channel.get_partial_message(int(poll['message'])).edit(content=poll_text(poll),
                    view=LocalPollView(self,poll),allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                LOG.warning('投票 #%s 已结束，原消息暂时无法更新',pid)

    async def local_vote(self,interaction,pid,choice):
        gid,cid=interaction.guild_id,interaction.channel_id
        if (gid is None or cid not in self.allowed_channels.get(gid,()) or
                not isinstance(interaction.channel,discord.TextChannel) or not self.local.enabled(gid,'polls')):
            await interaction.response.send_message('本频道的投票功能未开启。',ephemeral=True)
            return
        # Acknowledge first; serialization also prevents older edits hiding newer votes.
        await interaction.response.defer()
        async with self.poll_locks[pid]:
            try:
                poll=self.local.db.poll(pid,gid,cid)
                if str(interaction.message.id)!=poll['message']:
                    raise ValueError('请在原投票消息上操作。')
                poll=self.local.db.vote(pid,gid,cid,interaction.user.id,choice)
                await interaction.message.edit(content=poll_text(poll),view=LocalPollView(self,poll),
                                               allowed_mentions=discord.AllowedMentions.none())
            except ValueError as exc:
                await interaction.followup.send(str(exc),ephemeral=True)

    async def deliver_reminders(self,now=None):
        now=time.time() if now is None else now
        scopes=[(gid,cid,self.local.enabled(gid,'reminders'),self.local.enabled(gid,'dates'))
                for gid,ids in self.allowed_channels.items()
                if self.local.enabled(gid,'reminders') or self.local.enabled(gid,'dates') for cid in ids]
        for row in self.local.db.due(now,scopes):
            if not self.local.db.claim(row['id']):
                continue
            try:
                channel=self.get_channel(int(row['channel']))
                if not isinstance(channel,discord.TextChannel) or channel.guild.id!=int(row['guild']):
                    raise ValueError('原频道暂不可用')
                late='（已到期，补发提醒）' if now-row['due']>30 else ''
                text=f'<@{row["user"]}> 🐳 提醒 #{row["id"]}{late}：{safe_text(row["body"])}'
                nonce=hashlib.sha256(f'{row["id"]}:{row["due"]}'.encode()).hexdigest()[:20]
                sent=await asyncio.wait_for(channel.send(text,nonce=nonce,
                    allowed_mentions=discord.AllowedMentions(everyone=False,roles=False,
                        users=[discord.Object(id=int(row['user']))],replied_user=False)),timeout=20)
                self.local.db.finish(row['id'],getattr(sent,'id',None),now)
                self.local.db.remember_reply(getattr(sent,'id',None),row['guild'],row['channel'],'提醒列表')
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.local.db.retry(row['id'],now)
                LOG.warning('提醒 #%s 暂未发送：%s',row['id'],type(exc).__name__)

    async def reminder_scheduler(self):
        await self.wait_until_ready()
        prune_at=time.time()+3600
        while not self.is_closed():
            try:
                await self.deliver_reminders()
                if time.time()>=prune_at:
                    self.local.db.prune()
                    await self.wordle.prune()
                    self.long_answers.prune()
                    prune_at=time.time()+3600
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.warning('提醒调度暂时失败：%s',type(exc).__name__)
            await asyncio.sleep(5)

    async def command(self,m,text):
        if self.user:
            text=re.sub(rf'<@!?{self.user.id}>','',text).strip()
        if not text.startswith('!鲸鱼'):
            return False
        self.local.db.remember_input(m.id,m.guild.id,m.channel.id)
        cmd=text[len('!鲸鱼'):].strip()
        if is_wordle(cmd):
            await self.wordle.text(m,cmd)
            return True
        public=self.shareable(m.channel) if cmd.startswith('群规 ') else False
        result=self.local.execute(m.guild.id,m.channel.id,m.author.id,cmd,admin=self.admin(m),public=public)
        if result is not None:
            await self.send_local(m,result)
            return True
        self.dialogue.clear(m.channel.id)
        scope=(m.guild.id,m.channel.id,m.author.id)
        if cmd in ('','帮助'):
            reply=HELP
            if not self.c.get('chat_enabled',True):
                reply='🐳 当前是纯本地模式，云端聊天已关闭。\n'+TOOL_HELP
        elif cmd=='状态':
            u=self.store.usage()
            paused=self.store.pref(f'paused:{m.channel.id}')=='1'
            reply=(f'🐳 {"暂停聊天" if paused else "正在听群友聊天"}。\n'
                   f'聊天模型：{"启用" if self.c.get("chat_enabled",True) else "关闭（纯本地模式）"}；本地工具不占 API 额度。\n'
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
            info=memory_status(self.store.db,m.guild.id)
            reply=(f'本服务器已整理 {info["summaries"]} 条摘要；还有 {info["pending"]} 条消息待整理。'
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
            self.long_answers.erase_user(m.guild.id,m.author.id)
            self.store.set_pref(self.history_key(m),str(m.id))
            reply='这条记忆已删除，也清掉了你的近期发言上下文。'
        elif cmd in ('清空记忆','关闭记忆'):
            self.long_answers.erase_user(m.guild.id,m.author.id)
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
                    self.long_answers.erase_channel(m.guild.id,m.channel.id)
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
        sent=await self.notice(m,reply,interval=0)
        self.local.db.remember_reply(getattr(sent,'id',None),m.guild.id,m.channel.id,cmd)
        return True

    async def on_message(self,m):
        if not self.allowed(m):
            return
        text=m.content.strip()
        if await self.command(m,text):
            return
        if not text:
            return
        assert self.user is not None
        direct=any(u.id==self.user.id for u in m.mentions)
        target=await self.reply_target(m) if m.reference else None
        if target is not None:
            direct=direct or target.author.id==self.user.id
        clean=re.sub(rf'<@!?{self.user.id}>','',text).strip() or '鲸鱼娘，在吗？'
        if direct and is_wordle(clean):
            await self.wordle.text(m,clean)
            return
        if m.reference and (not m.reference.channel_id or m.reference.channel_id==m.channel.id):
            rid=self.wordle.db.board_round(m.reference.message_id,m.guild.id,m.channel.id)
            if rid is not None:
                command='wordle 猜 '+clean if re.fullmatch(r'[A-Za-z]{5}|[A-Za-z]{7}',clean) else 'wordle 帮助'
                await self.wordle.text(m,command,expected=rid)
                return
        if direct and self.local.split_command(clean) is not None:
            public=self.shareable(m.channel) if clean.startswith('群规 ') else False
            result=self.local.execute(m.guild.id,m.channel.id,m.author.id,clean,admin=self.admin(m),public=public)
            await self.send_local(m,result)
            return
        action=natural_command(text,self.user.id,getattr(self.user,'name','鲸鱼娘'))
        if action and not any(u.id!=self.user.id for u in m.mentions) and (not m.reference or direct):
            result=self.local.execute(m.guild.id,m.channel.id,m.author.id,action,natural=True)
            await self.send_local(m,result)
            return
        ref=m.reference
        if (clean.isdigit() and len(clean)<=3 and not any(u.id!=self.user.id for u in m.mentions) and
                (not ref or direct) and self.local.db.game(m.guild.id,m.channel.id,m.author.id)):
            result=self.local.execute(m.guild.id,m.channel.id,m.author.id,'猜数字 '+clean)
            await self.send_local(m,result)
            return
        if ref and (not ref.channel_id or ref.channel_id==m.channel.id):
            previous=self.local.db.reply_command(ref.message_id,m.guild.id,m.channel.id)
            if previous is not None:
                result=LocalResult('这是本地工具回复；发送 !鲸鱼 工具 查看用法。想聊天可以另发新消息 @我。')
                if clean in ('再来一次','再来','再掷一次','再选一次','再摸一下','再喂一口'):
                    parsed=self.local.split_command(previous)
                    if parsed and parsed[0] in ('掷骰','骰子','抽签','选择','选一个','摸摸','投喂','喂饭','喂米饭'):
                        result=self.local.execute(m.guild.id,m.channel.id,m.author.id,previous)
                await self.send_local(m,result)
                return
        if self.store.pref(f'paused:{m.channel.id}')=='1':
            return
        if not self.c.get('chat_enabled',True):
            if self.c['auto_memory_enabled'] and self.memory_on(m):
                record_memory(self.store.db,m.id,m.guild.id,m.channel.id,m.author.id,
                    m.author.display_name,safe_text(text),public=self.shareable(m.channel))
            if direct:
                await self.notice(m,'云端聊天已关闭，可以用 !鲸鱼 工具 和我玩。')
            return
        if continue_request(clean) and self.long_answers.find(m):
            direct=True
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
            self.context.add(m.channel.id,m.id,m.author.id,m.author.display_name,clean,
                             break_before=bool(m.reference or m.mentions))
        self.policy.observe(m.channel.id)
        key=(m.channel.id,m.author.id)
        if key in self.workers:
            # A new message addressed to someone else is a separate conversation.
            if not direct and (m.reference is not None or m.mentions):
                return
            if direct:
                self.kinds[key]='mention'
            elif followup and self.kinds.get(key)=='casual':
                self.kinds[key]='continuation'
            batch=self.pending[key]
            if not batch.append((m,clean,target)) and direct:
                await self.notice(m,'消息太密啦，等我回完这一轮再聊。')
            return
        if not direct and not followup:
            if any(worker_channel==m.channel.id for worker_channel,_ in self.workers) or not self.policy.eligible(m.channel.id,clean):
                return
        if len(self.workers)>=8:
            if direct or followup:
                await self.notice(m,'现在有点忙，稍后再叫我吧。')
            return
        self.pending[key]=MessageBurst(self.c)
        self.pending[key].append((m,clean,target))
        kind='mention' if direct else 'continuation' if followup else 'casual'
        self.kinds[key]=kind
        self.workers[key]=asyncio.create_task(self.respond(key,kind))

    async def respond(self,key,kind):
        try:
            burst=self.pending[key]
            await burst.wait()
            # A short bounded queue serializes replies per channel; API calls are globally serial.
            async with self.channel_locks[key[0]]:
                rounds=0
                while burst.items and rounds<3:
                    await burst.wait()
                    kind=self.kinds.get(key,kind)
                    version=burst.version
                    batch=burst.take()
                    if not batch:
                        break
                    m=batch[-1][0]
                    if self.store.pref(f'paused:{m.channel.id}')=='1':
                        return
                    current=join_fragments(t for _,t,_ in batch)
                    answer=self.long_answers.find(m) if continue_request(current) else None
                    if answer:
                        try:
                            await self.long_answers.resume(m,answer,is_current=lambda:burst.version==version)
                        except MessageChanged:
                            burst.restore(batch)
                            continue
                        burst.complete()
                        rounds+=1
                        continue
                    memories=self.personal_memories(m) if self.memory_on(m) else []
                    recent=await self.recent_history(m,batch[0][0])
                    # Use the newest explicit reply, including an unreadable one, so
                    # a failed fetch cannot silently reuse an earlier reply's target.
                    quoted=next((item for item in reversed(batch) if item[0].reference),None)
                    target=quoted[2] if quoted else None
                    reference={'状态':'引用内容不可用'} if quoted else None
                    quoted_ids=set()
                    if target is not None and self._historical_allowed(m,target,explicit=True):
                        reference={'群友':clip(target.author.display_name,32),
                            '发言':clip(safe_text(target.content),self.c['context_chars_per_message'])}
                        quoted_ids.add(target.id)
                        article=self.long_answers.by_message(target.id,m.guild.id,m.channel.id,self.memory_on(m))
                        if article:
                            reference['所属长回答的原题']=clip(article['prompt'],500)
                        # Discord replies may form a chain: the bot's short answer often
                        # points back to the question the user is following up on.
                        parent=await self.reply_target(target)
                        if parent is not None and parent.id!=target.id and self._historical_allowed(m,parent,explicit=True):
                            reference['上一级引用']={'群友':clip(parent.author.display_name,32),
                                '发言':clip(safe_text(parent.content),self.c['context_chars_per_message'])}
                            quoted_ids.add(parent.id)
                    history=compact_history(recent or [],self.c['context_chars_per_message'])
                    if reference is not None:
                        # Retrieve memories about the selected conversation, rather
                        # than unrelated nearby messages when the input is an emoji.
                        retrieval_query=' '.join((current,reference.get('发言',''),
                            reference.get('上一级引用',{}).get('发言',''))).strip()
                    else:
                        retrieval_query=current+' '+ ' '.join(r['text'] for r in history[-2:])
                    auto_memories=(retrieve_memory(self.store.db,m.guild.id,m.channel.id,
                        m.author.id,retrieval_query,limit=3) if self.c['auto_memory_enabled'] and self.memory_on(m) else [])
                    explain=explanation_request(current)
                    teaching=explain and kind!='casual'
                    longform=(explain or writing_request(current) or (continue_request(current) and reference and
                        writing_request(str(reference)))) and kind!='casual'
                    excluded={x.id for x,_,_ in batch}|quoted_ids
                    messages=self.context.build(m.channel.id,excluded,m.author.display_name,
                        current,memories,m.author.id==int(self.c['owner_id']),recent=recent,
                        reference=reference,explain=explain,continuation=kind=='continuation',
                        auto_memories=auto_memories,longform=bool(longform))
                    # Rebuild for new fragments before a paid request, including while
                    # waiting behind another server's cloud call.
                    if burst.version!=version:
                        burst.restore(batch)
                        continue
                    try:
                        async with m.channel.typing():
                            options=teaching_options(self.c) if teaching else {'max_tokens':
                                self.c.get('long_output_tokens',2048) if longform else self.c['max_output_tokens']}
                            result=await self.llm.chat(messages,kind,
                                is_current=lambda:burst.version==version,**options)
                    except MessageChanged:
                        burst.restore(batch)
                        continue
                    rounds+=1
                    if burst.version!=version:
                        # The provider already answered an incomplete sentence. Hold
                        # that stale reply and use the whole utterance next round.
                        burst.restore(batch)
                        continue
                    burst.complete()
                    if self.store.pref(f'paused:{m.channel.id}')=='1':
                        return
                    if kind=='continuation' and result.strip()=='[[NO_REPLY]]':
                        self.dialogue.clear(m.channel.id)
                        continue
                    if longform or getattr(result,'truncated',False) or len(result.encode('utf-16-le'))//2>1800:
                        prompt=current
                        if reference:
                            prompt+='\n用户引用的内容：'+str(reference)[:1000]
                        await self.long_answers.begin(m,prompt,result,[x.id for x,_,_ in batch],
                            auto=bool(longform),is_current=lambda:burst.version==version,teaching=teaching)
                        if self.c['auto_memory_enabled'] and self.memory_on(m):
                            mark_engaged(self.store.db,[x.id for x,_,_ in batch])
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
                if burst.items:
                    await self.notice(burst.items[-1][0],'这轮消息有点多，后面的内容请再叫我一次。')
        except (BudgetExceeded,APIError) as exc:
            if kind!='casual':
                await self.notice(m,str(exc))
            LOG.info('回复未完成：%s',type(exc).__name__)
        except discord.HTTPException:
            LOG.warning('Discord 消息发送失败；不会重新请求模型')
            with contextlib.suppress(discord.HTTPException):
                await self.long_answers.delivery_notice(m)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.error('回复异常：%s',type(exc).__name__)
        finally:
            self.pending.pop(key,None)
            self.workers.pop(key,None)
            self.kinds.pop(key,None)

    async def on_raw_message_delete(self,payload):
        self.long_answers.erase_message(payload.message_id)
        if self.c['auto_memory_enabled']:
            erase_message(self.store.db,payload.message_id)
        self.context.remove(payload.channel_id,payload.message_id)
        self.dialogue.remove_reply(payload.channel_id,payload.message_id)
        for key,batch in list(self.pending.items()):
            if key[0]==payload.channel_id:
                batch.remove(payload.message_id)

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
