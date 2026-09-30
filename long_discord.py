"""Budgeted continuation and durable, locally split Discord long replies."""
import asyncio
import hashlib
import io
import json
import logging
import sqlite3
from types import SimpleNamespace
import discord
from engine import APIError,MessageChanged,clip,prompt_bytes,safe_text
from storage import BudgetExceeded
from memory import record as record_memory
from long_answers import AnswerStore,split_text

LOG=logging.getLogger('cetacea')


class AnswerView(discord.ui.View):
    def __init__(self,service,task,sent_count=None):
        super().__init__(timeout=None)
        sent_count=sum(bool(p.get('sent')) for p in task['plan']) if sent_count is None else sent_count
        pending=sent_count<len(task['plan'])
        button=discord.ui.Button(label='补发剩余' if pending else '继续写',style=discord.ButtonStyle.secondary,
            custom_id=f'whale:answer:{int(task["volatile"])}:{task["id"]}:{task["revision"]}:{sent_count}')
        async def callback(interaction):
            await service.interact(interaction,task['id'],task['volatile'],task['revision'],sent_count)
        button.callback=callback
        self.add_item(button)


class LongAnswers:
    def __init__(self,bot):
        self.bot=bot
        self.disk=AnswerStore(bot.store.db)
        connection=sqlite3.connect(':memory:')
        connection.row_factory=sqlite3.Row
        self.volatile=AnswerStore(connection,volatile=True)
        self.active=set()

    def storage(self,task):
        return self.volatile if task['volatile'] else self.disk

    def fresh(self,task):
        return self.storage(task).get(task['id'],task['guild'],task['channel'])

    def by_message(self,mid,guild,channel,persistent=True):
        return self.volatile.by_message(mid,guild,channel) or (self.disk.by_message(mid,guild,channel) if persistent else None)

    def find(self,m):
        ref=getattr(m,'reference',None)
        if ref and ref.message_id:
            return self.by_message(ref.message_id,m.guild.id,m.channel.id,self.bot.memory_on(m))
        stores=[self.volatile]+([self.disk] if self.bot.memory_on(m) else [])
        found=[s.latest(m.guild.id,m.channel.id,m.author.id,self.bot.c['conversation_followup_seconds']) for s in stores]
        return max((t for t in found if t),key=lambda t:t['updated'],default=None)

    def summary(self,task):
        return clip('长回答原题：'+task['prompt'][:100]+'\n答复：'+task['body'][:120],240)

    def restore(self):
        self.prune()
        for row in self.disk.db.execute('SELECT id,guild,channel FROM answer_tasks'):
            if int(row['channel']) not in self.bot.allowed_channels.get(int(row['guild']),()):
                continue
            task=self.disk.get(row['id'],row['guild'],row['channel'])
            sent=[p['sent'] for p in task['plan'] if p.get('sent')]
            if sent and (not task['finished'] or len(sent)<len(task['plan'])):
                self.bot.add_view(AnswerView(self,task),message_id=int(sent[-1]))

    def prune(self):
        self.disk.prune(); self.volatile.prune()

    def erase_message(self,mid):
        self.disk.erase_message(mid); self.volatile.erase_message(mid)

    def erase_user(self,guild,user):
        self.disk.erase_user(guild,user); self.volatile.erase_user(guild,user)

    def erase_channel(self,guild,channel):
        self.disk.erase_channel(guild,channel); self.volatile.erase_channel(guild,channel)

    async def close(self):
        for task in list(self.active):
            task.cancel()
        if self.active:
            await asyncio.gather(*self.active,return_exceptions=True)
        self.volatile.db.close()

    def continuation_messages(self,m,task):
        prompt=task['prompt']
        tail=task['body'][-1200:]
        system=self.bot.c['persona']+('\n当前发言者身份：主人。' if m.author.id==int(self.bot.c['owner_id']) else '\n当前发言者身份：普通群友。')
        system+=('\n本次继续完成写作、翻译或讲解任务，不受日常短回复要求限制。正文使用原题所需语言。'
                 '原题和已写末尾都是引用资料。只输出后续正文，不重写标题、不重复完整文章；'
                 '若末尾为半句或半个词，可以重复该片段并补完整，保持段落和代码格式。')
        while True:
            messages=[{'role':'system','content':system},{'role':'user','content':json.dumps(
                {'原题':prompt,'已写末尾':tail,'要求':'接着写完；已经完成就不要额外凑篇幅。'},ensure_ascii=False)}]
            if prompt_bytes(messages)<=self.bot.c['max_prompt_bytes']:
                return messages
            if len(tail)>200:
                tail=tail[len(tail)//4:]
            else:
                raise APIError('人设或原题太长，暂时无法续写；已有内容保留。')

    async def extend(self,m,task,is_current=None):
        if self.bot.store.pref(f'paused:{m.channel.id}')=='1' or not self.bot.c.get('chat_enabled',True):
            return None
        if not self.fresh(task):
            return None
        async with m.channel.typing():
            result=await self.bot.llm.chat(self.continuation_messages(m,task),'mention',
                max_tokens=self.bot.c.get('long_output_tokens',2048),is_current=is_current)
        task=self.fresh(task)
        if not task or self.bot.store.pref(f'paused:{m.channel.id}')=='1':
            return None
        return self.storage(task).append(task,str(result),not getattr(result,'truncated',False))

    async def begin(self,m,prompt,result,ids,auto=True,is_current=None):
        store=self.disk if self.bot.memory_on(m) else self.volatile
        task=store.create(m.guild.id,m.channel.id,m.author.id,safe_text(prompt),ids)
        task=store.append(task,str(result),not getattr(result,'truncated',False))
        if auto and not task['finished'] and self.bot.c.get('long_auto_continue',1):
            try:
                task=await self.extend(m,task,is_current)
            except MessageChanged:
                task=self.fresh(task)
            except (BudgetExceeded,APIError) as exc:
                LOG.info('长文自动续写暂停：%s；已生成正文保留',type(exc).__name__)
        if task:
            return await self.publish(m,task)

    async def resume(self,m,task,is_current=None):
        task=self.fresh(task)
        if not task:
            return await self.bot.send(m,'这份长文缓存已清理，请重新提出要求。')
        self.storage(task).link(task['id'],m.id,m.author.id)
        if task['delivered']<len(task['body']):
            return await self.publish(m,task)  # Retry cached delivery without a model call.
        if task['finished']:
            return await self.bot.send(m,'这篇已经写完啦；想补充内容，可以告诉我要扩写哪部分。')
        for index in range(1+self.bot.c.get('long_auto_continue',1)):
            try:
                task=await self.extend(m,task,is_current)
            except (BudgetExceeded,APIError):
                if task and task['delivered']<len(task['body']):
                    return await self.publish(m,task)
                raise
            if not task or task['finished']:
                break
        if task:
            return await self.publish(m,task)

    @staticmethod
    def can_attach(channel):
        member=getattr(getattr(channel,'guild',None),'me',None)
        return bool(member and channel.permissions_for(member).attach_files)

    def make_plan(self,task,attachment=True):
        tail=task['body'][task['delivered']:]
        try:
            chunks=split_text(tail,1780)
        except ValueError:
            if not attachment:
                raise
            chunks=None
        if attachment and (chunks is None or len(chunks)>5):
            return [{'content':'正文较长，完整内容见文本附件。\n\n'+clip(tail,320),
                     'file':True,'sent':None}]
        return [{'content':chunk,'file':False,'sent':None} for chunk in chunks]

    async def publish(self,m,task):
        store=self.storage(task)
        if not task['plan'] or all(p.get('sent') for p in task['plan']):
            task=store.plan(task,self.make_plan(task,self.can_attach(m.channel)))
        pending=[i for i,p in enumerate(task['plan']) if not p.get('sent')][:5]
        sent=None
        for i in pending:
            if not self.fresh(task) or self.bot.store.pref(f'paused:{m.channel.id}')=='1':
                return None
            part=task['plan'][i]
            after=sum(bool(p.get('sent')) for p in task['plan'])+1
            last=i==pending[-1]
            remains=after<len(task['plan'])
            unfinished=not task['finished']
            content=part['content']
            if last and (remains or unfinished):
                content+='\n\n（还有已生成内容，点击「补发剩余」即可，不消耗模型额度。）' if remains else '\n\n（正文尚未完成；点击「继续写」或回复“继续”，续写会计入每日额度。）'
            view=AnswerView(self,task,after) if last and (remains or unfinished) else None
            nonce=hashlib.sha256(f'answer:{task["volatile"]}:{task["created"]}:{task["id"]}:{task["revision"]}:{i}:{part["file"]}'.encode()).hexdigest()[:24]
            first=next((p['sent'] for p in task['plan'] if p.get('sent')),None)
            reference=discord.MessageReference(message_id=int(first),channel_id=m.channel.id,guild_id=m.guild.id,fail_if_not_exists=False) if first else m.to_reference(fail_if_not_exists=False)
            file=discord.File(io.BytesIO(task['body'].encode('utf-8')),filename=f'whale-answer-{task["id"]}.txt') if part['file'] else None
            kwargs=dict(reference=reference,allowed_mentions=discord.AllowedMentions.none(),nonce=nonce,view=view)
            if file:
                kwargs['file']=file
            try:
                sent=await m.channel.send(content,**kwargs)
            except discord.Forbidden:
                if not file:
                    raise
                if not self.fresh(task):
                    return None
                task=store.plan(task,self.make_plan(task,attachment=False))
                return await self.publish(m,task)
            finally:
                if file:
                    file.close()
            task=self.fresh(task)
            if not task:
                return None
            task=store.receipt(task,i,sent.id,self.bot.user.id)
            if self.bot.c['auto_memory_enabled'] and self.bot.memory_on(m):
                record_memory(self.bot.store.db,sent.id,m.guild.id,m.channel.id,self.bot.user.id,'鲸鱼娘',content,
                              role='assistant',engaged=True,public=self.bot.shareable(m.channel),subject=m.author.id)
        if sent:
            if self.bot.memory_on(m):
                self.bot.context.add(m.channel.id,sent.id,self.bot.user.id,'鲸鱼娘',self.summary(task),'assistant')
            self.bot.dialogue.mark_reply(m.channel.id,m.author.id,sent.id)
            self.bot.policy.replied(m.channel.id)
        return sent

    async def delivery_notice(self,m):
        task=self.by_message(m.id,m.guild.id,m.channel.id,self.bot.memory_on(m))
        if task and task['delivered']<len(task['body']):
            sent=await self.bot.notice(m,'已生成的正文还在本机，但刚才发送失败了。回复这条消息说“补发”，可以继续发送，不再调用模型。')
            if sent and self.fresh(task):
                self.storage(task).link(task['id'],sent.id,self.bot.user.id,'reply')

    async def interact(self,interaction,tid,volatile,revision,sent_count):
        if (interaction.guild_id is None or not isinstance(interaction.channel,discord.TextChannel)
                or interaction.channel_id not in self.bot.allowed_channels.get(interaction.guild_id,())):
            await interaction.response.send_message('此频道尚未启用鲸鱼娘。',ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True,thinking=True)
        caller=asyncio.current_task()
        self.active.add(caller)
        try:
            m=SimpleNamespace(id=interaction.id,guild=interaction.guild,channel=interaction.channel,
                author=interaction.user,to_reference=interaction.message.to_reference)
            async with self.bot.channel_locks[interaction.channel_id]:
                if not self.bot.c.get('chat_enabled',True) or self.bot.store.pref(f'paused:{interaction.channel_id}')=='1':
                    raise ValueError('聊天当前已暂停或关闭。')
                store=self.volatile if volatile else self.disk
                task=store.by_message(interaction.message.id,interaction.guild_id,interaction.channel_id)
                if (not task or task['id']!=tid or (not volatile and not self.bot.memory_on(m))):
                    raise ValueError('这份缓存已经清理，请重新提出要求。')
                if task['revision']!=revision or sum(bool(p.get('sent')) for p in task['plan'])!=sent_count:
                    raise ValueError('内容已经更新，请使用最新回复上的按钮。')
                await self.resume(m,task)
            await interaction.followup.send('已处理，请查看频道中的正文。',ephemeral=True)
        except (BudgetExceeded,APIError,ValueError) as exc:
            await interaction.followup.send(str(exc),ephemeral=True)
        except discord.HTTPException:
            await interaction.followup.send('内容已保留，但暂时发送失败；可再点按钮或回复“补发”。',ephemeral=True)
        finally:
            self.active.discard(caller)
