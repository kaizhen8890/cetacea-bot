import asyncio
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from local_tools import dice,options,calculate,convert,duration,reminder_input,annual_date,next_annual
from local_features import LocalFeatures,natural_command
from feature_store import FeatureStore
from storage import Store
from test_bot import config,message
from bot import Whale
from datetime import datetime,timezone,timedelta
import settings
import check


class ToolTests(unittest.TestCase):
    def test_dice_choice_and_limits(self):
        with patch('local_tools.random.randint',return_value=4):
            self.assertIn('→ 11',dice('2d6+3'))
        self.assertEqual(options('小明，小红，小明'),['小明','小红'])
        self.assertEqual(options('New York, Kuala Lumpur'),['New York','Kuala Lumpur'])
        for spec in ('1000d6','20d1','1d999999','hello'):
            with self.subTest(spec=spec),self.assertRaises(ValueError):
                dice(spec)

    def test_calculation_math_and_disallowed_code(self):
        self.assertEqual(calculate('(12+8)*3'),'60')
        self.assertEqual(calculate('sqrt(9)+2^3'),'11')
        self.assertEqual(calculate('sin(pi/2)'),'1')
        for expression in ('1/0','sqrt(-1)','9**999999','__import__("os").getcwd()',
                           '[1,2][0]','(lambda: 5)()','True','sum([1,2])'):
            with self.subTest(expression=expression),self.assertRaises(ValueError):
                calculate(expression)

    def test_units_and_reminder_duration(self):
        self.assertEqual(convert('100 cm m'),'100 cm = 1 m')
        self.assertEqual(convert('32 华氏度 摄氏度'),'32 华氏度 = 0 摄氏度')
        self.assertEqual(convert('1GiB到MiB'),'1 GiB = 1024 MiB')
        self.assertEqual(duration('1小时30分钟'),5400)
        self.assertEqual(reminder_input('20分钟后 喝水'),(1200,'喝水'))
        self.assertEqual(reminder_input('10秒',timer=True),(10,'倒计时结束啦。'))
        for value in ('0秒','500天','20','明天','1秒其他'):
            with self.subTest(value=value),self.assertRaises(ValueError):
                duration(value)
        with self.assertRaises(ValueError):
            convert('10 kg m')

    def test_only_explicit_natural_actions_trigger(self):
        self.assertEqual(natural_command('摸摸鲸鱼娘',50),'摸摸')
        self.assertEqual(natural_command('<@50> 喂米饭',50),'喂米饭')
        for text in ('大家吃饭了吗','摸摸','我要投喂朋友','鲸鱼娘解释物理'):
            self.assertIsNone(natural_command(text,50))

    def test_annual_dates_and_leap_years(self):
        self.assertEqual(annual_date('02-29'),(2,29))
        after=datetime(2026,3,1,tzinfo=timezone(timedelta(hours=8))).timestamp()
        date=datetime.fromtimestamp(next_annual(2,29,after),timezone(timedelta(hours=8)))
        self.assertEqual((date.year,date.month,date.day,date.hour),(2028,2,29,9))
        with self.assertRaises(ValueError):
            annual_date('02-30')


class FeaturePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'features.sqlite'
        self.store=Store(self.path)
        self.db=FeatureStore(self.store)
        self.features=LocalFeatures(config(),self.store)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_reminder_survives_restart_and_cancel_is_scoped(self):
        rid=self.db.add_reminder(1,2,3,'喝水',10,now=1)
        self.store.close()
        self.store=Store(self.path)
        self.db=FeatureStore(self.store)
        self.assertEqual(self.db.due(20)[0]['id'],rid)
        for scope in ((5,2,3),(1,4,3),(1,2,9)):
            self.assertFalse(self.db.cancel(rid,*scope))
        self.assertTrue(self.db.cancel(rid,1,2,3))
        self.assertEqual(self.db.due(20),[])

    def test_disabled_scopes_cannot_starve_active_reminders(self):
        for user in range(12):
            self.db.add_reminder(1,2,user,'暂停频道',10,now=1)
        rid=self.db.add_reminder(5,6,3,'活动频道',12,now=1)
        self.assertEqual([r['id'] for r in self.db.due(20,[(5,6)])],[rid])

    def test_claim_finish_recover_and_retry(self):
        rid=self.db.add_reminder(1,2,3,'喝水',10,now=1)
        self.assertTrue(self.db.claim(rid))
        self.assertFalse(self.db.claim(rid))
        self.db.recover()
        self.assertEqual(self.db.due(20)[0]['id'],rid)
        self.db.claim(rid)
        self.db.retry(rid,20)
        self.assertEqual(self.db.due(21),[])
        self.assertEqual(self.db.due(81)[0]['id'],rid)
        self.db.claim(rid)
        self.db.finish(rid,100)
        self.assertEqual(self.db.due(100),[])

    def test_per_server_feature_switch_and_reminder_cancel_when_disabled(self):
        self.features.execute(1,2,3,'关闭功能 随机',admin=True)
        self.assertFalse(self.features.enabled(1,'random'))
        self.assertTrue(self.features.enabled(5,'random'))
        denied=self.features.execute(5,6,3,'关闭功能 随机')
        self.assertIn('管理权限',denied.text)
        rid=self.db.add_reminder(1,2,3,'喝水',10,now=1)
        self.db.toggle(1,'reminders',False)
        self.assertIn('已取消',self.features.execute(1,2,3,f'取消提醒 {rid}').text)

    def test_local_reply_marker_survives_restart_and_is_channel_scoped(self):
        self.db.remember_reply(100,1,2,'掷骰 1d6')
        self.store.close()
        self.store=Store(self.path)
        self.db=FeatureStore(self.store)
        self.assertEqual(self.db.reply_command(100,1,2),'掷骰 1d6')
        self.assertIsNone(self.db.reply_command(100,5,2))
        self.assertIsNone(self.db.reply_command(100,1,4))

    def test_poll_changes_one_vote_and_restores_after_restart(self):
        pid=self.db.create_poll(1,2,3,'吃什么',['米饭','面条'])
        self.db.poll_message(pid,100)
        self.db.vote(pid,1,2,4,0)
        result=self.db.vote(pid,1,2,4,1)
        self.assertEqual(result['counts'],[0,1])
        with self.assertRaises(ValueError):
            self.db.vote(pid,5,6,4,0)
        with self.assertRaises(ValueError):
            self.db.end_poll(pid,1,2,9)
        self.store.close()
        self.store=Store(self.path)
        self.db=FeatureStore(self.store)
        self.assertEqual(self.db.open_polls()[0]['message'],'100')
        self.db.end_poll(pid,1,2,3)
        with self.assertRaises(ValueError):
            self.db.vote(pid,1,2,4,0)

    def test_rice_is_per_server_and_signin_is_once_per_day(self):
        now=datetime(2026,9,30,23,59,tzinfo=timezone(timedelta(hours=8))).timestamp()
        self.assertEqual(self.db.signin(1,3,now),13)
        with self.assertRaises(ValueError):
            self.db.signin(1,3,now+30)
        self.assertEqual(self.db.feed(1,3),12)
        self.assertEqual(self.db.rice(5,3)['balance'],3)
        self.assertEqual(self.db.signin(1,3,now+120),22)

    def test_game_scope_and_expiry(self):
        self.db.start_game(1,2,3,50,now=10)
        self.assertIsNone(self.db.game(5,2,3,now=20))
        self.assertIsNone(self.db.game(1,4,3,now=20))
        self.assertIn('小了一点',self.db.guess(1,2,3,40,now=20))
        self.assertIn('猜中',self.db.guess(1,2,3,50,now=21))
        self.assertIsNone(self.db.game(1,2,3,now=22))
        self.db.start_game(1,2,3,50,now=10)
        self.assertIsNone(self.db.game(1,2,3,now=611))

    def test_notes_are_personal_and_restricted_rules_cannot_be_shared(self):
        self.db.save_note(1,2,3,'书名','电磁学')
        self.assertEqual(self.db.notes(1,2,3)[0]['value'],'电磁学')
        for scope in ((5,2,3),(1,4,3),(1,2,9)):
            self.assertEqual(self.db.notes(*scope),[])
        result=self.features.execute(1,2,3,'群规 添加 礼貌=好好说话',admin=True,public=False)
        self.assertIn('公开频道',result.text)
        self.assertEqual(self.db.rules(1),[])
        self.features.execute(1,2,3,'群规 添加 礼貌=好好说话',admin=True,public=True)
        self.assertEqual(self.db.rules(1,'礼貌')[0]['value'],'好好说话')
        self.assertEqual(self.db.rules(5),[])

    def test_yearly_reminders_update_and_reschedule_without_duplicate(self):
        now=datetime(2026,9,30,tzinfo=timezone(timedelta(hours=8))).timestamp()
        rid=self.db.annual(1,2,3,'birthday',10,1,'生日快乐',now)
        self.assertEqual(self.db.annual(1,2,3,'birthday',10,1,'更新内容',now),rid)
        row=self.db.reminders(1,2,3)[0]
        self.assertEqual(row['body'],'更新内容')
        self.db.claim(rid)
        self.db.finish(rid,101,now=row['due'])
        updated=self.db.reminders(1,2,3)[0]
        year=datetime.fromtimestamp(updated['due'],timezone(timedelta(hours=8))).year
        self.assertEqual(year,2027)
        self.assertEqual(self.db.due(row['due']+1),[])
        self.assertFalse(self.db.cancel_annual(1,2,9,'birthday'))
        self.assertTrue(self.db.cancel_annual(1,2,3,'birthday'))


class LocalDiscordTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store=Store(':memory:')
        self.bot=Whale(config(),self.store)
        self.bot._connection.user=SimpleNamespace(id=50,name='鲸鱼娘')
        self.bot.llm=SimpleNamespace(chat=AsyncMock(side_effect=AssertionError('local feature called LLM')))
        self.allowed=patch('bot.discord.TextChannel',SimpleNamespace)
        self.allowed.start()

    async def asyncTearDown(self):
        await self.bot.close()
        self.allowed.stop()
        self.store.close()

    async def test_commands_natural_actions_and_bad_inputs_never_call_model(self):
        cases=('!鲸鱼 掷骰 2d6','!鲸鱼 选一个 米饭 面条','!鲸鱼 抽签 小明 小红',
               '!鲸鱼 计算 (12+8)*3','!鲸鱼 换算 100 cm m',
               '!鲸鱼 提醒我 20分钟后 喝水','!鲸鱼 摸摸','摸摸鲸鱼娘',
               '<@50> 投喂','!鲸鱼 计算 1/0','!鲸鱼 不存在的指令',
               '!鲸鱼 签到','!鲸鱼 饭碗','!鲸鱼 猜拳 石头','!鲸鱼 猜数字',
               '!鲸鱼 便签 保存 书名=电磁学','!鲸鱼 便签列表',
               '!鲸鱼 生日 10-01','!鲸鱼 纪念日 相识=10-01',
               '!鲸鱼 取消提醒 '+('9'*100))
        for uid,text in enumerate(cases,100):
            m=message(uid,text,uid=uid)
            await self.bot.on_message(m)
            m.channel.send.assert_awaited_once()
        self.bot.llm.chat.assert_not_awaited()
        self.assertEqual(self.store.usage()['calls'],0)
        self.assertFalse(self.bot.workers)

    async def test_pausing_chat_keeps_local_tools_available(self):
        self.store.set_pref('paused:2','1')
        m=message(text='!鲸鱼 掷骰')
        await self.bot.on_message(m)
        m.channel.send.assert_awaited_once()
        self.bot.llm.chat.assert_not_awaited()

    async def test_local_inputs_and_outputs_do_not_enter_cloud_context(self):
        m=message(100,'<@50> !鲸鱼 计算 2+3')
        m.channel.send.return_value=SimpleNamespace(id=101)
        await self.bot.on_message(m)
        later=message(102)
        original=message(100,'<@50> !鲸鱼 计算 2+3')
        original.channel=later.channel
        reply=message(101,'结果：5',uid=50,bot=True)
        reply.channel=later.channel
        self.assertFalse(self.bot._historical_allowed(later,original))
        self.assertFalse(self.bot._historical_allowed(later,reply))

    async def test_reply_to_local_output_never_becomes_paid_chat(self):
        self.bot.local.db.remember_reply(90,1,2,'掷骰 1d6')
        m=message(100,'再来一次')
        m.reference=SimpleNamespace(message_id=90,channel_id=2,resolved=None)
        m.channel.fetch_message=AsyncMock(return_value=message(90,'骰好了',uid=50,bot=True))
        await self.bot.on_message(m)
        self.assertIn('🎲',m.channel.send.call_args.args[0])
        self.bot.llm.chat.assert_not_awaited()
        self.assertFalse(self.bot.workers)

    async def test_overdue_reminder_sends_once_and_mentions_only_owner(self):
        rid=self.bot.local.db.add_reminder(1,2,3,'喝水 @everyone <@9>',10,now=1)
        channel=SimpleNamespace(id=2,guild=SimpleNamespace(id=1),send=AsyncMock(return_value=SimpleNamespace(id=101)))
        with patch.object(self.bot,'get_channel',return_value=channel):
            await self.bot.deliver_reminders(100)
            await self.bot.deliver_reminders(110)
        channel.send.assert_awaited_once()
        self.assertIn('补发',channel.send.call_args.args[0])
        mentions=channel.send.call_args.kwargs['allowed_mentions']
        self.assertEqual([u.id for u in mentions.users],[3])
        self.assertFalse(mentions.everyone)
        self.assertFalse(mentions.roles)
        self.assertEqual(self.bot.local.db.reminders(1,2,3),[])
        self.assertEqual(self.store.usage()['calls'],0)

    async def test_disabled_and_unselected_channels_never_send_reminders(self):
        self.bot.local.db.add_reminder(1,2,3,'停用',10,now=1)
        self.bot.local.db.add_reminder(1,99,3,'未选择',10,now=1)
        self.bot.local.db.toggle(1,'reminders',False)
        with patch.object(self.bot,'get_channel') as get_channel:
            await self.bot.deliver_reminders(100)
        get_channel.assert_not_called()

    async def test_slash_and_button_actions_never_call_model(self):
        interaction=SimpleNamespace(guild_id=1,channel_id=2,channel=SimpleNamespace(id=2),
            user=SimpleNamespace(id=3,guild_permissions=SimpleNamespace(manage_guild=False)),
            response=SimpleNamespace(send_message=AsyncMock()),original_response=AsyncMock(return_value=SimpleNamespace(id=123)))
        await self.bot.local_interaction(interaction,'投喂')
        kwargs=interaction.response.send_message.call_args.kwargs
        view=kwargs['view']
        interaction.response.send_message.reset_mock()
        await view.children[0].callback(interaction)
        interaction.response.send_message.assert_awaited_once()
        self.bot.llm.chat.assert_not_awaited()
        self.assertEqual(self.store.usage()['calls'],0)

    async def test_pure_local_setup_does_not_initialize_llm(self):
        self.bot.c['chat_enabled']=False
        with patch('bot.LLM',side_effect=AssertionError('LLM initialized')),\
                patch.object(self.bot.tree,'sync',new=AsyncMock()),\
                patch.object(self.bot,'reminder_scheduler',new=AsyncMock()):
            await self.bot.setup_hook()
        self.assertIsNone(self.bot.llm)
        m=message(text='<@50> 聊天')
        await self.bot.on_message(m)
        self.assertFalse(self.bot.workers)

    async def test_number_game_continues_without_tag_or_model(self):
        with patch('local_features.random.randint',return_value=50):
            start=message(text='!鲸鱼 猜数字')
            await self.bot.on_message(start)
        answer=message(2,'50')
        await self.bot.on_message(answer)
        self.assertIn('猜中',answer.channel.send.call_args.args[0])
        self.bot.llm.chat.assert_not_awaited()

    async def test_poll_button_records_vote_without_model(self):
        pid=self.bot.local.db.create_poll(1,2,3,'吃什么',['米饭','面条'])
        self.bot.local.db.poll_message(pid,123)
        interaction=SimpleNamespace(guild_id=1,channel_id=2,channel=SimpleNamespace(id=2),
            user=SimpleNamespace(id=4),message=SimpleNamespace(id=123,edit=AsyncMock()),
            response=SimpleNamespace(defer=AsyncMock(),send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()))
        await self.bot.local_vote(interaction,pid,1)
        self.assertEqual(self.bot.local.db.poll(pid,1,2)['counts'],[0,1])
        interaction.message.edit.assert_awaited_once()
        self.bot.llm.chat.assert_not_awaited()
        self.assertEqual(self.store.usage()['calls'],0)

    async def test_date_and_timer_switches_do_not_block_each_other(self):
        now=datetime(2026,9,30,tzinfo=timezone(timedelta(hours=8))).timestamp()
        self.bot.local.db.annual(1,2,3,'birthday',10,1,'生日快乐',now)
        self.bot.local.db.add_reminder(1,2,3,'计时',now+1,now=now)
        self.bot.local.db.toggle(1,'reminders',False)
        channel=SimpleNamespace(id=2,guild=SimpleNamespace(id=1),send=AsyncMock(return_value=SimpleNamespace(id=101)))
        due=datetime(2026,10,1,9,tzinfo=timezone(timedelta(hours=8))).timestamp()
        with patch.object(self.bot,'get_channel',return_value=channel):
            await self.bot.deliver_reminders(due)
        channel.send.assert_awaited_once()
        self.assertIn('生日快乐',channel.send.call_args.args[0])
        self.assertEqual(len(self.bot.local.db.reminders(1,2,3)),2)

    async def test_slash_registration_fits_discord_limits(self):
        group=self.bot.tree.get_commands()[0]
        self.assertLessEqual(len(group.commands),25)
        self.assertIn('投票',[cmd.name for cmd in group.commands])
        self.assertIn('日期',[cmd.name for cmd in group.commands])

    async def test_slash_poll_exposes_separate_required_and_optional_choices(self):
        command=self.bot.tree.get_commands()[0].get_command('投票')
        params=command.parameters
        self.assertEqual([p.name for p in params],['问题']+[f'选项{i}' for i in range(1,9)])
        self.assertEqual([p.required for p in params],[True]*3+[False]*6)

    async def test_slash_poll_creates_two_or_eight_choices_without_model(self):
        command=self.bot.tree.get_commands()[0].get_command('投票')
        for choices in (['米饭','面条'],['New York','Kuala Lumpur','炒饭，配汤','面条','饺子','粥','鱼','虾']):
            with self.subTest(choices=choices):
                interaction=SimpleNamespace(guild_id=1,channel_id=2,channel=SimpleNamespace(id=2,guild=SimpleNamespace(id=1)),
                    user=SimpleNamespace(id=3,guild_permissions=SimpleNamespace(manage_guild=False)),
                    response=SimpleNamespace(send_message=AsyncMock()),
                    original_response=AsyncMock(return_value=SimpleNamespace(id=123)))
                await command.callback(interaction,'今晚吃什么',*choices)
                view=interaction.response.send_message.call_args.kwargs['view']
                self.assertEqual([button.label for button in view.children],choices)
                self.assertFalse(interaction.response.send_message.call_args.kwargs['ephemeral'])
        self.bot.llm.chat.assert_not_awaited()
        self.assertEqual(self.store.usage()['calls'],0)

    async def test_slash_poll_does_not_treat_embedded_separators_as_extra_choices(self):
        command=self.bot.tree.get_commands()[0].get_command('投票')
        interaction=SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))
        for value in ('米饭 | 面条','米饭｜面条'):
            await command.callback(interaction,'今晚吃什么',value,'饺子')
            self.assertTrue(interaction.response.send_message.call_args.kwargs['ephemeral'])
        self.assertEqual(self.bot.local.db.open_polls(),[])
        self.bot.llm.chat.assert_not_awaited()


class LocalModeSettingsTests(unittest.TestCase):
    def test_no_model_key_required_when_cloud_chat_off(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            c=config()
            c.update(chat_enabled=False)
            root.joinpath('config.json').write_text(json.dumps(c),encoding='utf-8')
            root.joinpath('persona.txt').write_text('鲸鱼娘',encoding='utf-8')
            with patch.object(settings,'ROOT',root),patch.dict(os.environ,{},clear=True):
                actual=settings.load_settings()
                self.assertEqual(actual['api_key'],'')
                self.assertFalse(actual['chat_enabled'])

    def test_local_gui_save_accepts_empty_model_key(self):
        from setup_gui import provider_form_settings
        c=config()
        values={'api_base':c['api_base'],'model':c['model'],'api_key':'','pricing_currency':'USD',
                'input_price_per_million':'1','output_price_per_million':'2','usd_to_rmb':'7','daily_budget_rmb':'2'}
        actual=provider_form_settings(dict(c,chat_enabled=False),values,'{}')
        self.assertFalse(actual['chat_enabled'])
        self.assertNotIn('api_key',actual)


class LocalCheckTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_check_contacts_no_provider_even_with_live_flag(self):
        with tempfile.TemporaryDirectory() as directory:
            c=dict(config(),chat_enabled=False,discord_token='')
            with patch.object(check,'ROOT',Path(directory)),patch.object(check,'load_settings',return_value=c),\
                    patch.object(check,'fetch_model_ids',side_effect=AssertionError('model request')),\
                    patch.object(check,'LLM',side_effect=AssertionError('LLM initialized')),\
                    contextlib.redirect_stdout(io.StringIO()) as output:
                await check.check(live=True)
            self.assertIn('纯本地',output.getvalue())


if __name__=='__main__':
    unittest.main()
