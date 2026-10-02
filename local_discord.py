"""Discord slash commands and buttons for the local feature service."""
import logging
import discord
from wordle_discord import add_commands as add_wordle_commands

LOG=logging.getLogger('cetacea')


class LocalCommandTree(discord.app_commands.CommandTree):
    async def on_error(self,interaction,error):
        LOG.warning('本地斜杠命令未完成：%s',type(error).__name__)
        text='这个指令没完成，请检查参数，或发送 !鲸鱼 工具 查看用法。'
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text,ephemeral=True)
            else:
                await interaction.response.send_message(text,ephemeral=True)
        except discord.HTTPException:
            pass


class LocalActionView(discord.ui.View):
    def __init__(self,bot,buttons,guild,channel):
        super().__init__(timeout=180)
        for label,command in buttons:
            item=discord.ui.Button(label=label,style=discord.ButtonStyle.secondary)
            def callback_for(cmd):
                async def callback(interaction):
                    if interaction.guild_id!=guild or interaction.channel_id!=channel:
                        await interaction.response.send_message('请在原频道使用这个按钮。',ephemeral=True)
                        return
                    await bot.local_interaction(interaction,cmd)
                return callback
            item.callback=callback_for(command)
            self.add_item(item)

    async def on_error(self,interaction,error,item):
        LOG.warning('本地按钮未完成：%s',type(error).__name__)
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message('按钮暂时不可用，请改用 !鲸鱼 指令。',ephemeral=True)
        except discord.HTTPException:
            pass


class LocalPollView(discord.ui.View):
    def __init__(self,bot,poll):
        super().__init__(timeout=None)
        closed=poll['status']!='open'
        for index,option in enumerate(poll['options']):
            item=discord.ui.Button(label=option,custom_id=f'whale:poll:{poll["id"]}:{index}',
                                   row=index//4,disabled=closed)
            def callback_for(choice):
                async def callback(interaction):
                    await bot.local_vote(interaction,poll['id'],choice)
                return callback
            item.callback=callback_for(index)
            self.add_item(item)
        if closed:
            self.stop()

    async def on_error(self,interaction,error,item):
        LOG.warning('投票按钮未完成：%s',type(error).__name__)
        try:
            if interaction.response.is_done():
                await interaction.followup.send('投票暂时未完成，请稍后再点一次。',ephemeral=True)
            else:
                await interaction.response.send_message('投票暂时未完成，请稍后再点一次。',ephemeral=True)
        except discord.HTTPException:
            pass


def command_group(bot):
    group=discord.app_commands.Group(name='鲸鱼',description='本地工具与鲸鱼互动；Wordle AI 代玩消耗模型额度',guild_only=True)

    @group.command(name='工具',description='查看本地工具用法')
    async def tools(interaction:discord.Interaction):
        await bot.local_interaction(interaction,'工具')

    @group.command(name='掷骰',description='掷骰子，例如 2d6 或 1d20+3')
    async def roll(interaction:discord.Interaction,骰子:str='1d6'):
        await bot.local_interaction(interaction,'掷骰 '+骰子)

    @group.command(name='抽签',description='从提供的名单里抽一人，使用空格或逗号分隔')
    async def draw(interaction:discord.Interaction,名单:str):
        await bot.local_interaction(interaction,'抽签 '+名单)

    @group.command(name='选一个',description='从多个选项中选择一个，使用空格或逗号分隔')
    async def choose(interaction:discord.Interaction,选项:str):
        await bot.local_interaction(interaction,'选一个 '+选项)

    @group.command(name='计算',description='计算数学算式，例如 (12+8)*3 或 sqrt(9)')
    async def calc(interaction:discord.Interaction,算式:str):
        await bot.local_interaction(interaction,'计算 '+算式)

    @group.command(name='换算',description='单位换算，例如 100 cm m 或 32 摄氏度 华氏度')
    async def units(interaction:discord.Interaction,内容:str):
        await bot.local_interaction(interaction,'换算 '+内容)

    @group.command(name='提醒',description='在本频道提醒你；离线时会延后')
    async def reminder(interaction:discord.Interaction,时间:str,内容:str):
        await bot.local_interaction(interaction,f'提醒我 {时间}后 {内容}')

    @group.command(name='倒计时',description='设置倒计时，例如 5分钟 或 1小时30分钟')
    async def timer(interaction:discord.Interaction,时间:str):
        await bot.local_interaction(interaction,'倒计时 '+时间)

    @group.command(name='提醒列表',description='查看你在本频道的待办提醒')
    async def reminders(interaction:discord.Interaction):
        await bot.local_interaction(interaction,'提醒列表')

    @group.command(name='取消提醒',description='取消你在本频道的一条提醒')
    async def cancel(interaction:discord.Interaction,编号:int):
        await bot.local_interaction(interaction,f'取消提醒 {编号}')

    @group.command(name='摸摸',description='摸摸鲸鱼娘，不调用大模型')
    async def pat(interaction:discord.Interaction):
        await bot.local_interaction(interaction,'摸摸')

    @group.command(name='投喂',description='给鲸鱼娘喂米饭，不调用大模型')
    async def feed(interaction:discord.Interaction):
        await bot.local_interaction(interaction,'投喂')

    features_group=discord.app_commands.Group(name='功能',description='查看或调整本服务器功能',parent=group)

    @features_group.command(name='查看',description='查看本服务器的本地功能开关')
    async def features(interaction:discord.Interaction):
        await bot.local_interaction(interaction,'功能')

    @features_group.command(name='开启',description='管理员开启本服务器的某类本地功能')
    async def enable(interaction:discord.Interaction,功能:str):
        await bot.local_interaction(interaction,'开启功能 '+功能)

    @features_group.command(name='关闭',description='管理员关闭本服务器的某类本地功能')
    async def disable(interaction:discord.Interaction,功能:str):
        await bot.local_interaction(interaction,'关闭功能 '+功能)

    @group.command(name='投票',description='发起单选投票，每人一票，可改选')
    @discord.app_commands.describe(问题='投票问题，最多 100 字',
        选项1='第一个选项，最多 40 字',选项2='第二个选项，最多 40 字',
        选项3='第三个选项，可留空',选项4='第四个选项，可留空',
        选项5='第五个选项，可留空',选项6='第六个选项，可留空',
        选项7='第七个选项，可留空',选项8='第八个选项，可留空')
    async def poll(interaction:discord.Interaction,问题:str,选项1:str,选项2:str,
                   选项3:str|None=None,选项4:str|None=None,选项5:str|None=None,
                   选项6:str|None=None,选项7:str|None=None,选项8:str|None=None):
        choices=[选项1,选项2]+[x for x in (选项3,选项4,选项5,选项6,选项7,选项8)
                              if x is not None and x.strip()]
        if any('|' in x or '｜' in x for x in [问题]+choices):
            await interaction.response.send_message('请分别填写每个选项；问题和单个选项不能包含 | 或 ｜。',ephemeral=True)
            return
        await bot.local_interaction(interaction,'投票 '+' | '.join([问题]+choices))

    @group.command(name='投票结果',description='查看本频道投票结果')
    async def poll_result(interaction:discord.Interaction,编号:int):
        await bot.local_interaction(interaction,f'投票结果 {编号}')

    @group.command(name='结束投票',description='发起人或管理员结束投票')
    async def end_poll(interaction:discord.Interaction,编号:int):
        await bot.local_interaction(interaction,f'结束投票 {编号}')

    @group.command(name='签到',description='每日领取 10 粒米饭，UTC+8 零点换日')
    async def signin(interaction:discord.Interaction):
        await bot.local_interaction(interaction,'签到')

    @group.command(name='饭碗',description='查看自己的米饭数量')
    async def rice(interaction:discord.Interaction):
        await bot.local_interaction(interaction,'饭碗')

    @group.command(name='猜数字',description='猜 1—100 的整数；留空开始新游戏')
    async def guess(interaction:discord.Interaction,数字:str=''):
        await bot.local_interaction(interaction,('猜数字 '+数字).strip())

    @group.command(name='猜拳',description='石头剪刀布；留空显示按钮')
    async def rps(interaction:discord.Interaction,出拳:str=''):
        await bot.local_interaction(interaction,('猜拳 '+出拳).strip())

    @group.command(name='退出游戏',description='退出你在本频道的猜数字游戏')
    async def quit_game(interaction:discord.Interaction):
        await bot.local_interaction(interaction,'退出游戏')

    note_group=discord.app_commands.Group(name='便签',description='本人在当前频道使用的便签',parent=group)

    @note_group.command(name='保存',description='保存或更新自己的便签')
    async def save_note(interaction:discord.Interaction,名称:str,内容:str):
        await bot.local_interaction(interaction,f'便签 保存 {名称}={内容}')

    @note_group.command(name='查看',description='查看自己的便签')
    async def get_note(interaction:discord.Interaction,名称:str):
        await bot.local_interaction(interaction,'便签 '+名称)

    @note_group.command(name='列表',description='查看自己的便签名称')
    async def list_notes(interaction:discord.Interaction):
        await bot.local_interaction(interaction,'便签列表')

    @note_group.command(name='删除',description='删除自己的便签')
    async def delete_note(interaction:discord.Interaction,名称:str):
        await bot.local_interaction(interaction,'删除便签 '+名称)

    @group.command(name='群规',description='按关键词查询已录入群规，管理员可录入或删除')
    async def rules(interaction:discord.Interaction,内容:str=''):
        await bot.local_interaction(interaction,('群规 '+内容).strip())

    date_group=discord.app_commands.Group(name='日期',description='本人主动设置的生日和纪念日',parent=group)

    @date_group.command(name='生日',description='设置自己的生日，只记录月日，如 10-01')
    async def birthday(interaction:discord.Interaction,日期:str):
        await bot.local_interaction(interaction,'生日 '+日期)

    @date_group.command(name='纪念日',description='设置每年在原频道提醒自己的纪念日')
    async def anniversary(interaction:discord.Interaction,名称:str,日期:str):
        await bot.local_interaction(interaction,f'纪念日 {名称}={日期}')

    @date_group.command(name='取消生日',description='取消本人在本频道的生日提醒')
    async def cancel_birthday(interaction:discord.Interaction):
        await bot.local_interaction(interaction,'取消生日')

    @date_group.command(name='取消纪念日',description='取消本人在本频道的某个纪念日提醒')
    async def cancel_anniversary(interaction:discord.Interaction,名称:str):
        await bot.local_interaction(interaction,'取消纪念日 '+名称)

    add_wordle_commands(group,bot)
    return group
