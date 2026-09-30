import asyncio
import io
import json
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime,timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
import discord
from bot import Whale
from engine import ModelReply,APIError,prompt_bytes
from storage import Store,BudgetExceeded
from test_bot import config,message
from long_answers import AnswerStore,continue_request,writing_request,split_text,units,merge_continuation


class SplitTests(unittest.TestCase):
    def test_writing_and_continuation_detection(self):
        for text in ('写一篇rumusan范文给我','帮我翻译成马来文','给我写封邮件','write an essay','请润色这段话'):
            self.assertTrue(writing_request(text),text)
        for text in ('你好','今天吃什么','鲸鱼好胖'):
            self.assertFalse(writing_request(text),text)
        self.assertTrue(continue_request('继续！'))
        self.assertFalse(continue_request('我继续打游戏了'))

    def test_full_text_requests_and_retries_are_long_tasks(self):
        for text in ('把你映像中的孔雀东南飞全文输出出来','妈妈帮我输出一下，我需要帮忙',
                     '请帮我输出孔雀东南飞全文','给我发来全诗','把全文发给我',
                     '请背诵孔雀东南飞','默写这首诗','show me the complete poem'):
            self.assertTrue(writing_request(text),text)
        for text in ('老师今天罚我背诵了','全文太长了吧','我在看整本书'):
            self.assertFalse(writing_request(text),text)

    def test_unicode_round_trip_and_budget_including_numbering(self):
        for source in ('一段说明。\n\n'*900,('A paragraph with some words.\n\n'*200),
                       ('👨‍👩‍👧‍👦👍🏿🇲🇾e\u0301 '*500),'x'*4000):
            chunks=split_text(source)
            self.assertTrue(all(units(p)<=1900 for p in chunks))
            stripped=[re.sub(r'^（\d+/\d+）\n','',p) for p in chunks]
            self.assertEqual(''.join(stripped),source)
            self.assertTrue(all(not p.startswith(('\u200d','🏿','\u0301')) for p in stripped))

    def test_code_fences_are_balanced_on_each_message(self):
        code=''.join(f'print({i})\n' for i in range(1000))
        chunks=split_text('说明\n```python\n'+code+'```\n结束')
        self.assertGreater(len(chunks),2)
        for chunk in chunks:
            self.assertEqual(chunk.count('```')%2,0)
            self.assertLessEqual(units(chunk),1900)
        extracted=''.join(re.findall(r'print\(\d+\)\n',''.join(chunks)))
        self.assertEqual(extracted,code)
        self.assertTrue(split_text('```python\nprint(1)')[0].endswith('```'))

    def test_continuation_removes_overlap_and_completes_partial_word(self):
        self.assertEqual(merge_continuation('Kesan utama ialah pencapai','pencapaian akademik menurun.'),
                         'Kesan utama ialah pencapaian akademik menurun.')
        self.assertEqual(merge_continuation('这是已经生成的一段完整正文。','这是已经生成的一段完整正文。接着解释。'),
                         '这是已经生成的一段完整正文。接着解释。')
        self.assertEqual(merge_continuation('academic','performance'),'academic performance')

    def test_store_persists_receipts_and_keeps_scopes_separate(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'answers.sqlite3'
            store=Store(path); db=AnswerStore(store.db)
            task=db.create(1,2,3,'写一篇文章',[10])
            task=db.append(task,'正文',False)
            task=db.plan(task,[{'content':'正文','sent':None,'file':False}])
            task=db.receipt(task,0,20,50)
            store.close()
            store=Store(path); db=AnswerStore(store.db)
            self.assertEqual(db.by_message(20,1,2)['body'],'正文')
            self.assertEqual(db.by_message(10,1,2)['delivered'],2)
            self.assertIsNone(db.by_message(20,5,2))
            self.assertIsNone(db.by_message(20,1,4))
            db.link(task['id'],11,4)
            db.erase_user(1,4)
            self.assertIsNone(db.by_message(20,1,2))
            store.close()


class LongFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store=Store(':memory:')
        self.bot=Whale(config(),self.store)
        self.bot._connection.user=SimpleNamespace(id=50)
        self.bot.llm=SimpleNamespace(chat=AsyncMock(return_value=ModelReply('完整正文。')))
        self.allowed=patch('bot.discord.TextChannel',SimpleNamespace)
        self.allowed.start()
        self.history=[]; self.sent=[]; self.messages={}; self.next_id=100
        self.channel=message().channel
        self.channel.guild=SimpleNamespace(id=1,me=SimpleNamespace(id=50),default_role=0)
        self.channel.permissions_for=lambda member:SimpleNamespace(attach_files=True,view_channel=True)
        async def history(**kwargs):
            for old in reversed(self.history):
                yield old
        self.channel.history=history
        self.channel.fetch_message=AsyncMock(side_effect=lambda mid:self.messages[mid])
        self.channel.send=AsyncMock(side_effect=self.send)

    async def asyncTearDown(self):
        await self.bot.close()
        self.allowed.stop()
        self.store.close()

    async def send(self,content,**kwargs):
        self.next_id+=1
        item={'content':content,**kwargs}
        if kwargs.get('file'):
            item['file_bytes']=kwargs['file'].fp.read()
        self.sent.append(item)
        result=self.make(self.next_id,content,uid=50,bot=True)
        result.reference=kwargs.get('reference')
        self.history.append(result)
        return result

    def make(self,mid,text,uid=3,bot=False,reference=None):
        m=message(mid,text,uid,bot)
        m.channel=self.channel; m.guild=self.channel.guild
        m.created_at=datetime.now(timezone.utc)
        if reference:
            m.reference=discord.MessageReference(message_id=reference,channel_id=2,guild_id=1)
        m.to_reference=lambda **kwargs:discord.MessageReference(message_id=m.id,channel_id=2,guild_id=1,**kwargs)
        self.messages[mid]=m
        return m

    async def handle(self,m):
        await self.bot.on_message(m)
        await asyncio.gather(*self.bot.workers.values())

    def task(self,source=10):
        return self.bot.long_answers.by_message(source,1,2)

    async def test_writing_is_generated_once_and_split_without_losing_text(self):
        body=('Kesan ponteng sekolah adalah serius.\n\n'*100)+'Kesimpulannya, pelajar perlu hadir ke sekolah.'
        self.bot.llm.chat.return_value=ModelReply(body)
        await self.handle(self.make(10,'<@50> 写一篇rumusan范文给我'))
        self.bot.llm.chat.assert_awaited_once()
        self.assertEqual(self.bot.llm.chat.call_args.kwargs['max_tokens'],2048)
        system=self.bot.llm.chat.call_args.args[0][0]['content']
        self.assertNotIn('本次是日常聊天',system)
        self.assertIn('外语作文',system)
        self.assertGreater(len(self.sent),1)
        rebuilt=''.join(re.sub(r'^（\d+/\d+）\n','',p['content']) for p in self.sent)
        self.assertEqual(rebuilt,body)
        self.assertTrue(all(units(p['content'])<=1900 for p in self.sent))
        self.assertEqual(self.task()['body'],body)
        self.assertEqual(self.task()['delivered'],len(body))
        self.assertEqual(self.sent[0]['reference'].message_id,10)
        self.assertEqual(self.sent[1]['reference'].message_id,101)

    async def test_full_text_retry_overrides_old_persona_refusal(self):
        request=self.make(8,'把孔雀东南飞全文输出出来')
        refused=self.make(9,'背不动啦，那么长，我记得开头几句而已。',uid=50,bot=True,reference=8)
        self.history.extend((request,refused))
        self.bot.llm.chat.return_value=ModelReply('孔雀东南飞，五里一徘徊。\n完整原文测试样例。')
        await self.handle(self.make(10,'妈妈帮我输出一下，我需要帮忙',reference=9))
        self.assertEqual(self.bot.llm.chat.call_args.kwargs['max_tokens'],2048)
        messages=self.bot.llm.chat.call_args.args[0]
        system=messages[0]['content']
        self.assertNotIn('本次是日常聊天',system)
        self.assertIn('此前回复若',system)
        self.assertIn('不编造原文',system)
        self.assertIn('孔雀东南飞全文',str(messages))
        self.assertIsNotNone(self.task())

    async def test_truncated_writing_automatically_continues_once_and_manual_resume_inherits_topic(self):
        self.bot.llm.chat.side_effect=[ModelReply('第一段正文。',True),ModelReply('第二段正文。',True),ModelReply('最后的结论。')]
        await self.handle(self.make(10,'<@50> 写一篇作文，主题是考试'))
        self.assertEqual(self.bot.llm.chat.await_count,2)
        self.assertFalse(self.task()['finished'])
        self.assertIsNotNone(self.sent[-1]['view'])
        first_body=self.task()['body']
        await self.handle(self.make(11,'继续',reference=101))
        self.assertEqual(self.bot.llm.chat.await_count,3)
        self.assertTrue(self.task()['finished'])
        self.assertTrue(self.task()['body'].startswith(first_body))
        self.assertNotIn('第一段',self.sent[-1]['content'])
        request=self.bot.llm.chat.call_args.args[0]
        self.assertIn('考试',str(request))
        self.assertEqual(self.bot.llm.chat.call_args.kwargs['max_tokens'],2048)

    async def test_very_long_answer_attaches_full_utf8_text(self):
        body='说明这件事。\n\n'*2500
        self.bot.llm.chat.return_value=ModelReply(body)
        await self.handle(self.make(10,'<@50> 写一份报告'))
        self.assertEqual(len(self.sent),1)
        self.assertEqual(self.sent[0]['file_bytes'].decode('utf-8'),body)
        self.assertLess(units(self.sent[0]['content']),1900)

    async def test_no_attachment_permission_pages_cached_body_without_more_api_calls(self):
        body='请认真阅读这一段话。\n\n'*1200
        self.channel.permissions_for=lambda member:SimpleNamespace(attach_files=False,view_channel=True)
        self.bot.llm.chat.return_value=ModelReply(body)
        await self.handle(self.make(10,'<@50> 写一份报告'))
        self.assertEqual(len(self.sent),5)
        self.assertLess(self.task()['delivered'],len(body))
        await self.handle(self.make(11,'补发',reference=105))
        self.bot.llm.chat.assert_awaited_once()
        self.assertEqual(self.task()['delivered'],len(body))

    async def test_failed_delivery_retries_only_unsent_cached_parts(self):
        body=('Kesan ponteng sekolah adalah serius.\n\n'*100)+'完。'
        self.bot.llm.chat.return_value=ModelReply(body)
        count=0
        async def fail_second(content,**kwargs):
            nonlocal count
            count+=1
            if count==2:
                raise discord.HTTPException(SimpleNamespace(status=500,reason='Server Error'),'')
            return await self.send(content,**kwargs)
        self.channel.send.side_effect=fail_second
        await self.handle(self.make(10,'<@50> 写一篇范文'))
        self.assertEqual(len(self.sent),2)
        self.assertIn('发送失败',self.sent[-1]['content'])
        first=self.sent[0]['content']
        await self.handle(self.make(11,'补发',reference=101))
        self.bot.llm.chat.assert_awaited_once()
        self.assertEqual(sum(x['content']==first for x in self.sent),1)
        self.assertEqual(self.task()['delivered'],len(body))

    async def test_auto_continuation_budget_failure_preserves_partial_and_button(self):
        self.bot.llm.chat.side_effect=[ModelReply('第一段。',True),BudgetExceeded('额度不足')]
        await self.handle(self.make(10,'<@50> 写一篇范文'))
        self.assertEqual(self.task()['body'],'第一段。')
        self.assertFalse(self.task()['finished'])
        self.assertIsNotNone(self.sent[0]['view'])

    async def test_opt_out_uses_only_volatile_task_and_clear_removes_it(self):
        self.store.set_pref('memory_off:1:3','1')
        await self.handle(self.make(10,'<@50> 写一篇范文'))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM answer_tasks').fetchone()[0],0)
        self.assertTrue(self.task()['volatile'])
        await self.handle(self.make(11,'!鲸鱼 清空记忆'))
        self.assertIsNone(self.task())

    async def test_reply_to_any_part_keeps_original_topic_and_history_is_compacted(self):
        self.bot.llm.chat.return_value=ModelReply('考试说明。\n\n'*450)
        await self.handle(self.make(10,'<@50> 写一份报告，主题是考试'))
        self.bot.llm.chat.return_value=ModelReply('补充解释。')
        current=self.make(11,'这句是什么意思',reference=102)
        recent=await self.bot.recent_history(current,current)
        self.assertEqual(len(recent),1)
        await self.handle(current)
        payload=json.loads(self.bot.llm.chat.call_args.args[0][-1]['content'])
        self.assertIn('考试',payload['正在回复的消息']['所属长回答的原题'])

    async def test_deletion_during_continuation_does_not_resurrect_cache(self):
        async def response(*args,**kwargs):
            self.bot.long_answers.erase_message(10)
            return ModelReply('不能重新保存这段内容。')
        self.bot.llm.chat.side_effect=[ModelReply('第一段。',True)]
        self.bot.c['long_auto_continue']=0
        await self.handle(self.make(10,'<@50> 写一篇范文'))
        before=len(self.sent)
        self.bot.llm.chat.side_effect=response
        await self.handle(self.make(11,'继续',reference=101))
        self.assertIsNone(self.task())
        self.assertEqual(len(self.sent),before)

    async def test_continuation_prompt_stays_within_budget(self):
        m=self.make(10,'<@50> 写作文')
        task=self.bot.long_answers.disk.create(1,2,3,'original requirement '*140,[10])
        task=self.bot.long_answers.disk.append(task,'中文正文。'*5000,False)
        messages=self.bot.long_answers.continuation_messages(m,task)
        self.assertLessEqual(prompt_bytes(messages),self.bot.c['max_prompt_bytes'])
        self.assertLess(len(str(messages)),len(task['body']))
        self.assertEqual(json.loads(messages[-1]['content'])['原题'],task['prompt'])

    async def test_translation_input_is_not_silently_cut_to_chat_length(self):
        source='Please translate this passage. '+('The students attended school. '*70)+' THE END'
        await self.handle(self.make(10,'<@50> 翻译这段话：'+source))
        payload=json.loads(self.bot.llm.chat.call_args.args[0][-1]['content'])
        self.assertTrue(payload['当前发言'].endswith('THE END'))

    def interaction(self,mid=101,iid=800):
        return SimpleNamespace(id=iid,guild_id=1,channel_id=2,guild=self.channel.guild,
            channel=self.channel,user=self.make(20,'继续').author,message=self.messages[mid],
            response=SimpleNamespace(defer=AsyncMock(),send_message=AsyncMock()),followup=SimpleNamespace(send=AsyncMock()))

    async def test_concurrent_button_clicks_cannot_duplicate_generation(self):
        self.bot.c['long_auto_continue']=0
        self.bot.llm.chat.side_effect=[ModelReply('未完成。',True),ModelReply('已完成。')]
        await self.handle(self.make(10,'<@50> 写一篇范文'))
        view=self.sent[0]['view']
        first,second=self.interaction(),self.interaction(iid=801)
        await asyncio.gather(view.children[0].callback(first),view.children[0].callback(second))
        self.assertEqual(self.bot.llm.chat.await_count,2)
        self.assertIn('更新',second.followup.send.call_args.args[0])

    async def test_persistent_continue_buttons_are_restored_and_pausing_blocks_them(self):
        self.bot.c['long_auto_continue']=0
        self.bot.llm.chat.return_value=ModelReply('未完成。',True)
        await self.handle(self.make(10,'<@50> 写一篇范文'))
        with patch.object(self.bot,'add_view') as add:
            self.bot.long_answers.restore()
        self.assertEqual(add.call_args.kwargs['message_id'],101)
        self.assertTrue(add.call_args.args[0].is_persistent())
        self.store.set_pref('paused:2','1')
        await self.sent[0]['view'].children[0].callback(self.interaction())
        self.bot.llm.chat.assert_awaited_once()

    async def test_file_permission_failure_falls_back_to_paged_cached_text(self):
        self.bot.llm.chat.return_value=ModelReply('测试正文。\n\n'*2000)
        async def fail_file(content,**kwargs):
            if kwargs.get('file'):
                raise discord.Forbidden(SimpleNamespace(status=403,reason='Forbidden'),'')
            return await self.send(content,**kwargs)
        self.channel.send.side_effect=fail_file
        await self.handle(self.make(10,'<@50> 写一份报告'))
        self.assertEqual(len(self.sent),5)
        self.assertTrue(all('file' not in part for part in self.sent))
        self.assertIsNotNone(self.sent[-1]['view'])
        self.bot.llm.chat.assert_awaited_once()

    async def test_deletion_during_delivery_does_not_leave_orphaned_receipts(self):
        async def deleting(content,**kwargs):
            self.bot.long_answers.erase_message(10)
            return await self.send(content,**kwargs)
        self.channel.send.side_effect=deleting
        self.bot.llm.chat.return_value=ModelReply('长篇内容。\n\n'*800)
        await self.handle(self.make(10,'<@50> 写一篇范文'))
        self.assertIsNone(self.task())
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM answer_links').fetchone()[0],0)
        self.assertEqual(len(self.sent),1)

    async def test_explicit_clear_context_erases_whole_long_task(self):
        await self.handle(self.make(10,'<@50> 写一篇范文'))
        await self.handle(self.make(11,'!鲸鱼 清空上下文',uid=9))
        self.assertIsNone(self.task())

    async def test_chat_stays_short_and_untruncated_metadata_does_not_create_task(self):
        await self.handle(self.make(10,'<@50> 今天吃什么'))
        self.assertEqual(self.bot.llm.chat.call_args.kwargs['max_tokens'],80)
        self.assertIsNone(self.task())
        self.assertEqual(len(self.sent),1)


if __name__=='__main__':
    unittest.main()
