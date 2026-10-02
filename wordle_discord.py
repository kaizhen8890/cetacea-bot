"""Discord UI for Wordle; only explicit AI play commands call a model."""
import asyncio
import hashlib
import io
import logging
import time
from collections import defaultdict,deque
from pathlib import Path
import discord
from engine import safe_text
from wordle import WordleStore,RULES,board_text,render_board
from wordle_avatars import AvatarCache
from wordle_player import WordlePlayer

LOG=logging.getLogger('cetacea')


def is_wordle(command):
    return command.strip().split(maxsplit=1)[0].lower()=='wordle' if command.strip() else False


class GuessModal(discord.ui.Modal):
    def __init__(self,service,game):
        super().__init__(title=f'Wordle · {game["size"]} 字母',timeout=300)
        self.service,self.rid=service,game['id']
        self.word=discord.ui.TextInput(label=f'猜一个 {game["size"]} 字母的英文单词',
            placeholder='cartoon' if game['size']==7 else 'crane',
            min_length=game['size'],max_length=game['size'])
        self.add_item(self.word)

    async def on_submit(self,interaction):
        await self.service.interact(interaction,'猜',self.word.value,expected=self.rid)

    async def on_error(self,interaction,error):
        await self.service.error(interaction,error)


class WordleView(discord.ui.View):
    def __init__(self,service,game):
        super().__init__(timeout=None)
        self.service=service
        rid=game['id']
        ended=game['state']!='playing'
        for label,action in (('提交猜测','guess'),('查看规则','rules'),('棋盘状态','status')):
            button=discord.ui.Button(label=label,custom_id=f'whale:wordle:{rid}:{action}',
                                     style=discord.ButtonStyle.primary if action=='guess' else discord.ButtonStyle.secondary,
                                     disabled=ended)
            def callback_for(kind):
                async def callback(interaction):
                    if not service.allowed(interaction.guild_id,interaction.channel):
                        await interaction.response.send_message('此频道尚未启用鲸鱼娘。',ephemeral=True)
                        return
                    game=service.db.get(rid,interaction.guild_id,interaction.channel_id)
                    if str(interaction.message.id)!=game['message']:
                        await interaction.response.send_message('请使用本局的原棋盘消息。',ephemeral=True)
                        return
                    if kind=='guess':
                        if not service.bot.local.enabled(interaction.guild_id,'wordle'):
                            await interaction.response.send_message('本服务器的 Wordle 已关闭。',ephemeral=True)
                            return
                        current=service.db.latest(interaction.guild_id,interaction.channel_id)
                        if not current or current['id']!=rid or game['state']!='playing':
                            await interaction.response.send_message('这局已结束，请使用最新棋盘。',ephemeral=True)
                            return
                        await interaction.response.send_modal(GuessModal(service,game))
                    else:
                        await service.interact(interaction,'规则' if kind=='rules' else '状态',expected=rid)
                return callback
            button.callback=callback_for(action)
            self.add_item(button)
        if ended:
            self.stop()

    async def on_error(self,interaction,error,item):
        await self.service.error(interaction,error)


class WordleService:
    def __init__(self,bot):
        self.bot=bot
        self.db=WordleStore(bot.store)
        database=next(row['file'] for row in bot.store.db.execute('PRAGMA database_list') if row['name']=='main')
        self.avatars=AvatarCache(Path(database).parent/'wordle-avatars' if database else None)
        self.locks=defaultdict(asyncio.Lock)
        self.requests=defaultdict(deque)
        self.player=WordlePlayer(self)

    def restore(self):
        self.db.prune()
        self.avatars.prune(self.db.avatar_keys())
        for row in self.db.active_boards():
            gid,cid=int(row['guild']),int(row['channel'])
            if cid in self.bot.allowed_channels.get(gid,()):
                self.bot.add_view(WordleView(self,self.db.get(row['id'],gid,cid)),message_id=int(row['message']))

    async def prune(self):
        self.db.prune()
        await asyncio.to_thread(self.avatars.prune,self.db.avatar_keys())

    def allowed(self,guild,channel):
        return (guild is not None and isinstance(channel,discord.TextChannel) and
                channel.id in self.bot.allowed_channels.get(guild,()))

    async def error(self,interaction,error):
        LOG.warning('Wordle 操作暂未完成：%s',type(error).__name__)
        try:
            if interaction.response.is_done():
                await interaction.followup.send('操作暂未完成；用 /鲸鱼 wordle 状态 查看已保存进度。',ephemeral=True)
            else:
                await interaction.response.send_message('操作暂未完成，请稍后重试。',ephemeral=True)
        except discord.HTTPException:
            pass

    @staticmethod
    def can_attach(channel):
        member=getattr(channel.guild,'me',None)
        return bool(member and channel.permissions_for(member).attach_files)

    async def capture_avatars(self,channel,game,member=None):
        """Capture accepted guesses; cached guild members can fill legacy rows too."""
        members={str(member.id):member} if member is not None else {}
        get_member=getattr(channel.guild,'get_member',None)
        missing={g['user'] for g in game['guesses'] if not g.get('avatar')}
        for user in missing-members.keys():
            candidate=get_member(int(user)) if get_member else None
            if candidate:
                members[user]=candidate
        users=[user for user in missing if user in members]
        keys=await asyncio.gather(*(self.avatars.snapshot(members[user]) for user in users))
        snapshots=dict(zip(users,keys))
        for guess in game['guesses']:
            key=snapshots.get(guess['user'])
            if key and not guess.get('avatar'):
                self.db.avatar(guess['id'],game['guild'],game['channel'],key)
                guess['avatar']=key

    async def image(self,channel,game):
        if not self.can_attach(channel):
            return None
        try:
            def render():
                return render_board(game,self.avatars.images(game))
            return await asyncio.to_thread(render)
        except Exception as exc:
            LOG.warning('Wordle 图片暂不可用，使用文字棋盘：%s',type(exc).__name__)
            return None

    @staticmethod
    def file(game,data):
        return discord.File(io.BytesIO(data),filename=f'wordle-{game["id"]}-{len(game["guesses"])}-{game["state"]}.png',
                            description=board_text(game,full=True)[:1024]) if data else None

    async def post(self,channel,game,data):
        file=self.file(game,data)
        kwargs={'view':WordleView(self,game),'allowed_mentions':discord.AllowedMentions.none(),
                'nonce':hashlib.sha256(f'wordle-board:{game["id"]}:{game["message"] or "new"}'.encode()).hexdigest()[:20]}
        if file:
            kwargs['file']=file
        try:
            try:
                sent=await channel.send(board_text(game,full=data is None),**kwargs)
            except discord.HTTPException:
                if not file:
                    raise
                LOG.warning('Wordle 图片上传未完成，使用文字棋盘')
                kwargs.pop('file')
                sent=await channel.send(board_text(game,full=True),**kwargs)
            mid=sent.id
        finally:
            if file:
                file.close()
        self.db.message(game['id'],mid)
        return mid

    async def update(self,channel,game):
        data=await self.image(channel,game)
        mid=game['message']
        if mid:
            file=self.file(game,data)
            kwargs={'content':board_text(game,full=data is None),'attachments':[file] if file else [],
                    'view':WordleView(self,game),'allowed_mentions':discord.AllowedMentions.none()}
            try:
                try:
                    await channel.get_partial_message(int(mid)).edit(**kwargs)
                except discord.NotFound:
                    mid=await self.post(channel,game,data)
                except discord.HTTPException:
                    if not file:
                        raise
                    LOG.warning('Wordle 图片更新未完成，使用文字棋盘')
                    kwargs.update(content=board_text(game,full=True),attachments=[])
                    try:
                        await channel.get_partial_message(int(mid)).edit(**kwargs)
                    except discord.NotFound:
                        mid=await self.post(channel,game,None)
            finally:
                if file:
                    file.close()
        else:
            mid=await self.post(channel,game,data)
        game['message']=str(mid)
        self.bot.local.db.remember_reply(mid,game['guild'],game['channel'],'wordle 状态')
        return f'https://discord.com/channels/{game["guild"]}/{game["channel"]}/{mid}'

    async def run(self,guild,channel,user,name,action,value,request,admin=False,expected=None,member=None):
        if action=='停止代玩':
            try:
                return self.player.stop(guild,channel.id,user,admin),False
            except ValueError as exc:
                return str(exc),False
        now=time.monotonic()
        recent=self.requests[(guild,user)]
        while recent and now-recent[0]>=10:
            recent.popleft()
        if len(recent)>=6:
            return '操作太密啦，等十秒再来；此次不扣猜测次数。',False
        recent.append(now)
        if action in ('规则','帮助'):
            return RULES,False
        if action in ('自己玩','代玩','猜一步'):
            try:
                parts=value.split()
                mode=parts[0] if parts and parts[0] in ('当前','普通','超级') else '当前'
                if parts and parts[0]==mode:
                    parts.pop(0)
                scope=parts.pop(0) if parts else ('一步' if action=='猜一步' else '整局')
                if parts:
                    raise ValueError('用法：wordle 自己玩 [当前/普通/超级] [整局/一步]。')
                if expected is not None:
                    game=self.db.latest(guild,channel.id)
                    if not game or game['id']!=expected:
                        raise ValueError('这是旧棋盘，请使用最新一局。')
                return await self.player.start(guild,channel,user,mode,scope,request),False
            except (ValueError,discord.HTTPException) as exc:
                return str(exc) if isinstance(exc,ValueError) else '棋盘暂时无法同步，尚未开始代玩；用 wordle 状态 重试。',False
        if action not in ('开始','猜','状态','结束'):
            return '用法：wordle 开始 普通/超级 · wordle 猜 单词 · wordle 状态 · wordle 结束 · wordle 自己玩 · wordle 停止代玩。',False
        if action in ('开始','猜') and not self.bot.local.enabled(guild,'wordle'):
            return '本服务器的 Wordle 已关闭。',False
        async with self.locks[(guild,channel.id)]:
            try:
                if expected is not None:
                    previous=self.db.latest(guild,channel.id)
                    if not previous or previous['id']!=expected:
                        raise ValueError('这是旧棋盘或旧输入框，请使用最新一局。')
                if action=='开始':
                    game=self.db.start(guild,channel.id,user,value or '普通')
                elif action=='猜':
                    game=self.db.guess(guild,channel.id,user,safe_text(name),value,request,expected)
                elif action=='结束':
                    game=self.db.stop(guild,channel.id,user,admin)
                    self.player.halt(guild,channel.id)
                else:
                    game=self.db.latest(guild,channel.id)
                    if not game:
                        raise ValueError('本频道还没有 Wordle；用 /鲸鱼 wordle 开始 开一局。')
                if self.can_attach(channel):
                    await self.capture_avatars(channel,game,member)
                created=not game['message']
                link=await self.update(channel,game)
                state={'playing':'继续一起猜吧。','won':'全频道猜中啦！','lost':'机会用完啦，答案已揭晓。','stopped':'本局已结束。'}[game['state']]
                if (guild,channel.id) in self.player.sessions:
                    state+='鲸鱼娘正在代玩，群友仍可参与。'
                return f'🐳 已用 {len(game["guesses"])}/{game["max_tries"]} 次，{state} [查看棋盘]({link})',created
            except ValueError as exc:
                return str(exc),False
            except discord.HTTPException as exc:
                LOG.warning('Wordle 棋盘暂未同步：%s',type(exc).__name__)
                return '进度已保存在本机，但暂时无法发送或更新棋盘；检查发送权限后用 wordle 状态 重试。',False

    async def interact(self,interaction,action,value='',expected=None):
        if not self.allowed(interaction.guild_id,interaction.channel):
            await interaction.response.send_message('此频道尚未启用鲸鱼娘。',ephemeral=True)
            return
        await interaction.response.defer(thinking=True,ephemeral=True)
        user=interaction.user
        admin=user.id==int(self.bot.c['owner_id']) or user.guild_permissions.manage_guild
        text,_=await self.run(interaction.guild_id,interaction.channel,user.id,user.display_name,
                              action,value,interaction.id,admin,expected,member=user)
        await interaction.followup.send(text,ephemeral=True,allowed_mentions=discord.AllowedMentions.none())

    async def text(self,message,command,expected=None):
        self.bot.local.db.remember_input(message.id,message.guild.id,message.channel.id)
        tail=command.strip().split(maxsplit=1)
        parts=tail[1].split(maxsplit=1) if len(tail)>1 else []
        action=parts[0] if parts else '帮助'
        value=parts[1] if len(parts)>1 else ''
        text,created=await self.run(message.guild.id,message.channel,message.author.id,
            message.author.display_name,action,value,message.id,self.bot.admin(message),expected,member=message.author)
        if not created:
            sent=await self.bot.send(message,text)
            self.bot.local.db.remember_reply(getattr(sent,'id',None),message.guild.id,message.channel.id,'wordle 状态')


def add_commands(parent,bot):
    group=discord.app_commands.Group(name='wordle',description='合作猜词与图片棋盘；可让鲸鱼娘用 AI 代玩',parent=parent)

    @group.command(name='开始',description='开一局；普通 5 字母/6 次，超级 7 字母/12 次')
    @discord.app_commands.choices(模式=[discord.app_commands.Choice(name='普通 · 5 字母 / 6 次',value='普通'),
                                     discord.app_commands.Choice(name='超级 · 7 字母 / 12 次',value='超级')])
    async def start(interaction:discord.Interaction,模式:str='普通'):
        await bot.wordle.interact(interaction,'开始',模式)

    @group.command(name='猜',description='提交一个单词，全频道共用机会')
    async def guess(interaction:discord.Interaction,单词:str):
        await bot.wordle.interact(interaction,'猜',单词)

    @group.command(name='状态',description='查看或恢复当前频道的图片棋盘')
    async def status(interaction:discord.Interaction):
        await bot.wordle.interact(interaction,'状态')

    @group.command(name='结束',description='发起人或管理员提前结束本局')
    async def stop(interaction:discord.Interaction):
        await bot.wordle.interact(interaction,'结束')

    @group.command(name='规则',description='查看合作 Wordle 规则')
    async def rules(interaction:discord.Interaction):
        await bot.wordle.interact(interaction,'规则')

    @group.command(name='自己玩',description='鲸鱼娘用 thinking low 猜词，消耗每日 API 额度')
    @discord.app_commands.choices(
        模式=[discord.app_commands.Choice(name='沿用当前局；没有进行中的局就开普通',value='当前'),
              discord.app_commands.Choice(name='普通 · 5 字母 / 6 次',value='普通'),
              discord.app_commands.Choice(name='超级 · 7 字母 / 12 次',value='超级')],
        范围=[discord.app_commands.Choice(name='整局自动猜',value='整局'),
              discord.app_commands.Choice(name='只猜一步',value='一步')])
    async def play(interaction:discord.Interaction,模式:str='当前',范围:str='整局'):
        await bot.wordle.interact(interaction,'自己玩',模式+' '+范围)

    @group.command(name='停止代玩',description='停止鲸鱼娘猜词，保留棋盘让群友继续')
    async def stop_play(interaction:discord.Interaction):
        await bot.wordle.interact(interaction,'停止代玩')
