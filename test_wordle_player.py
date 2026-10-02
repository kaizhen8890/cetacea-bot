import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
import discord
from bot import Whale
from engine import ModelReply,MessageChanged,APIError
from storage import Store,BudgetExceeded
from test_bot import config,message
from wordle import score,vocabulary
from wordle_player import public_board,choices_for,guess_messages,selected_word,LOW_THINKING


class SolverTests(unittest.TestCase):
    def test_hidden_answer_and_users_never_enter_public_projection(self):
        class SecretGuard(dict):
            def __getitem__(self,key):
                if key=='answer':
                    raise AssertionError('Read hidden answer')
                return super().__getitem__(key)
        game=SecretGuard(id=1,size=7,max_tries=12,answer='secret',owner='private',
            guesses=[{'word':'cartoon','marks':score('balloon','cartoon'),'name':'PRIVATE NAME','user':'private'}])
        public=public_board(game)
        prompt=str(guess_messages(public,choices_for(public)))
        self.assertNotIn('secret',prompt)
        self.assertNotIn('PRIVATE NAME',prompt)
        self.assertNotIn('private',prompt)
        self.assertEqual(set(public),{'round','size','remaining','guesses'})

    def test_candidates_follow_duplicate_letter_feedback(self):
        board={'round':1,'size':7,'remaining':10,'guesses':[
            {'word':w,'marks':score('balloon',w)} for w in ('cartoon','bassoon')]}
        choices=choices_for(board)
        self.assertIn('balloon',choices['possible_answers'])
        self.assertTrue(all(all(score(w,g['word'])==g['marks'] for g in board['guesses'])
            for w in choices['possible_answers']))
        self.assertLessEqual(len(choices['possible_answers'])+len(choices['probe_words']),24)

    def test_deduction_uses_only_feedback_and_dictionary(self):
        for size in (5,7):
            choices=choices_for({'size':size,'guesses':[]})
            self.assertTrue(set(choices['possible_answers']+choices['probe_words'])<=vocabulary(size)[0])
            self.assertEqual(choices['remaining_candidates'],len(vocabulary(size)[1]))

    def test_invalid_and_thought_only_outputs_are_not_guesses(self):
        choices={'possible_answers':['crane'],'probe_words':['slate']}
        self.assertEqual(selected_word(ModelReply(' CRANE '),choices,5),'crane')
        for reply in ('hello','crane because','<think>crane</think>',ModelReply('crane',True)):
            with self.assertRaises(ValueError):
                selected_word(reply,choices,5)


class PlayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store=Store(':memory:')
        self.bot=Whale(config(),self.store)
        self.bot._connection.user=SimpleNamespace(id=50,name='鲸鱼娘')
        self.allowed=patch('bot.discord.TextChannel',SimpleNamespace)
        self.allowed.start()
        self.channel=SimpleNamespace(id=2,guild=SimpleNamespace(id=1,me=None),
            send=AsyncMock(return_value=SimpleNamespace(id=900)),
            get_partial_message=lambda mid:SimpleNamespace(edit=self.edit))
        self.edit=AsyncMock(return_value=SimpleNamespace(id=900))
        self.bot.llm=SimpleNamespace(chat=AsyncMock(side_effect=self.choose))
        # Advance only the test guesses' timestamps, preserving the real cooldown
        # tests in test_wordle without waiting three seconds for every AI row.
        guess=self.bot.wordle.db.guess
        def fast_guess(*args,**kwargs):
            import time
            with self.store.db:
                self.store.db.execute('UPDATE wordle_guesses SET created=?',(time.time()-10,))
            kwargs.setdefault('now',time.time()-5)
            return guess(*args,**kwargs)
        self.fast=patch.object(self.bot.wordle.db,'guess',side_effect=fast_guess)
        self.fast.start()

    async def asyncTearDown(self):
        await self.bot.close()
        self.fast.stop(); self.allowed.stop(); self.store.close()

    async def choose(self,messages,*args,**kwargs):
        payload=json.loads(messages[-1]['content'])
        return ModelReply(payload['possible_answers'][0])

    async def start_game(self,answer='balloon',mode='超级'):
        with patch('wordle.random.choice',return_value=answer):
            await self.bot.wordle.run(1,self.channel,3,'群友','开始',mode,'start')
        return self.bot.wordle.db.latest(1,2)

    async def start_player(self,scope='一步'):
        result=await self.bot.wordle.run(1,self.channel,4,'群友','自己玩','当前 '+scope,'play')
        session=self.bot.wordle.player.sessions.get((1,2))
        return result,session

    async def test_one_step_uses_low_thinking_and_no_chat_memory(self):
        await self.start_game()
        result,session=await self.start_player()
        self.assertIn('thinking low',result[0])
        await session.task
        self.bot.llm.chat.assert_awaited_once()
        kwargs=self.bot.llm.chat.call_args.kwargs
        self.assertEqual(kwargs['extra_body'],LOW_THINKING)
        self.assertEqual(kwargs['max_tokens'],4096)
        self.assertEqual(self.bot.llm.chat.call_args.args[1],'wordle_ai')
        game=self.bot.wordle.db.latest(1,2)
        self.assertEqual(len(game['guesses']),1)
        self.assertEqual(game['guesses'][0]['user'],'50')
        self.assertEqual(game['message'],'900')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM journal').fetchone()[0],0)
        self.assertEqual(dict(self.bot.context.items),{})
        self.assertFalse(self.bot.wordle.player.sessions)

    async def test_autoplay_continues_with_feedback_until_win(self):
        await self.start_game()
        self.bot.wordle.db.guess(1,2,3,'群友','cartoon','human')
        _,session=await self.start_player('整局')
        await session.task
        game=self.bot.wordle.db.latest(1,2)
        self.assertEqual(game['state'],'won')
        self.assertLessEqual(self.bot.llm.chat.await_count,11)
        self.assertGreater(self.bot.llm.chat.await_count,1)
        payload=json.loads(self.bot.llm.chat.call_args.args[0][-1]['content'])
        self.assertEqual(len(payload['board']['guesses']),len(game['guesses'])-1)
        self.assertTrue(all(g['user']=='50' for g in game['guesses'][1:]))

    async def test_default_starts_new_game_and_duplicate_start_does_not_make_two_players(self):
        entered=asyncio.Event(); release=asyncio.Event()
        async def waiting(*args,**kwargs):
            entered.set(); await release.wait()
            return await self.choose(*args,**kwargs)
        self.bot.llm.chat.side_effect=waiting
        with patch('wordle.random.choice',return_value='crane'):
            _,session=await self.start_player()
        await entered.wait()
        result=await self.bot.wordle.run(1,self.channel,5,'另一位','自己玩','','again')
        self.assertIn('已经在猜',result[0])
        self.assertEqual(len(self.bot.wordle.player.sessions),1)
        release.set(); await session.task
        self.bot.llm.chat.assert_awaited_once()

    async def test_stopping_inflight_call_keeps_game_and_does_not_submit(self):
        await self.start_game()
        entered=asyncio.Event()
        async def waiting(*args,**kwargs):
            entered.set(); await asyncio.Event().wait()
        self.bot.llm.chat.side_effect=waiting
        _,session=await self.start_player('整局')
        await entered.wait()
        denied=await self.bot.wordle.run(1,self.channel,5,'别人','停止代玩','','stop')
        self.assertIn('只有',denied[0])
        self.assertIn((1,2),self.bot.wordle.player.sessions)
        import time
        self.bot.wordle.requests[1,4].extend([time.monotonic()]*6)
        stopped=await self.bot.wordle.run(1,self.channel,4,'发起人','停止代玩','','stop2')
        self.assertIn('已停止',stopped[0])
        await asyncio.gather(session.task,return_exceptions=True)
        game=self.bot.wordle.db.latest(1,2)
        self.assertEqual(game['state'],'playing')
        self.assertEqual(game['guesses'],[])

    async def test_human_guess_during_model_call_discards_stale_choice(self):
        await self.start_game()
        async def changing(*args,**kwargs):
            self.bot.wordle.db.guess(1,2,5,'群友','cartoon','human')
            return await self.choose(*args,**kwargs)
        self.bot.llm.chat.side_effect=changing
        _,session=await self.start_player()
        await session.task
        game=self.bot.wordle.db.latest(1,2)
        self.assertEqual(len(game['guesses']),1)
        self.assertEqual(game['guesses'][0]['user'],'5')
        self.assertIn('过时',self.channel.send.call_args.args[0])

    async def test_board_change_while_waiting_for_cloud_does_not_trigger_retry(self):
        await self.start_game()
        self.bot.llm.chat.side_effect=MessageChanged()
        _,session=await self.start_player()
        await session.task
        self.bot.llm.chat.assert_awaited_once()
        self.assertEqual(self.bot.wordle.db.latest(1,2)['guesses'],[])

    async def test_failed_board_update_pauses_and_keeps_accepted_guess(self):
        await self.start_game()
        self.edit.side_effect=[SimpleNamespace(id=900),discord.Forbidden(SimpleNamespace(status=403,reason='Forbidden'),'')]
        _,session=await self.start_player('整局')
        await session.task
        self.bot.llm.chat.assert_awaited_once()
        self.assertEqual(len(self.bot.wordle.db.latest(1,2)['guesses']),1)
        self.assertFalse(self.bot.wordle.player.sessions)
        self.assertIn('代玩已暂停',self.channel.send.call_args.args[0])

    async def test_budget_api_or_invalid_response_stops_without_spending_chances(self):
        await self.start_game()
        for outcome in (BudgetExceeded('额度不足'),APIError('接口不支持 low'),ModelReply('不是一个单词')):
            self.bot.wordle.requests.clear()
            self.bot.llm.chat.side_effect=outcome if isinstance(outcome,Exception) else None
            self.bot.llm.chat.return_value=outcome
            _,session=await self.start_player('整局')
            await session.task
            self.assertFalse(self.bot.wordle.player.sessions)
            self.assertEqual(self.bot.wordle.db.latest(1,2)['guesses'],[])

    async def test_disabled_pause_scope_and_mode_guards_prevent_model_calls(self):
        self.bot.local.db.toggle(1,'wordle',False)
        result,_=await self.bot.wordle.run(1,self.channel,4,'群友','自己玩','','a')
        self.assertIn('已关闭',result)
        self.bot.local.db.toggle(1,'wordle',True)
        self.bot.c['chat_enabled']=False
        result,_=await self.bot.wordle.run(1,self.channel,4,'群友','自己玩','','b')
        self.assertIn('云端聊天已关闭',result)
        self.bot.c['chat_enabled']=True
        self.store.set_pref('paused:2','1')
        result,_=await self.bot.wordle.run(1,self.channel,4,'群友','自己玩','','c')
        self.assertIn('已暂停',result)
        self.store.set_pref('paused:2','0')
        await self.start_game()
        self.bot.wordle.requests.clear()
        result,_=await self.bot.wordle.run(1,self.channel,4,'群友','自己玩','普通','d')
        self.assertIn('模式不同',result)
        other=SimpleNamespace(id=9,guild=self.channel.guild)
        with self.assertRaises(ValueError):
            await self.bot.wordle.player.start(1,other,4)
        self.bot.llm.chat.assert_not_awaited()

    async def test_prefix_routes_without_chat_pipeline_and_close_cancels_player(self):
        entered=asyncio.Event()
        async def waiting(*args,**kwargs):
            entered.set(); await asyncio.Event().wait()
        self.bot.llm.chat.side_effect=waiting
        m=message(777,'!鲸鱼 wordle 自己玩 普通 一步')
        m.channel=self.channel
        with patch('wordle.random.choice',return_value='crane'):
            await self.bot.on_message(m)
        await entered.wait()
        session=self.bot.wordle.player.sessions[1,2]
        self.assertFalse(self.bot.workers)
        await self.bot.wordle.player.close()
        self.assertTrue(session.task.cancelled())
        self.assertFalse(self.bot.wordle.player.sessions)
        self.assertEqual(self.bot.wordle.db.latest(1,2)['guesses'],[])

    async def test_end_game_cancels_player_and_new_game_is_not_touched(self):
        await self.start_game()
        entered=asyncio.Event()
        async def waiting(*args,**kwargs):
            entered.set(); await asyncio.Event().wait()
        self.bot.llm.chat.side_effect=waiting
        _,session=await self.start_player()
        await entered.wait()
        await self.bot.wordle.run(1,self.channel,3,'发起人','结束','','stop')
        await asyncio.gather(session.task,return_exceptions=True)
        self.assertEqual(self.bot.wordle.db.latest(1,2)['state'],'stopped')
        self.assertFalse(self.bot.wordle.player.sessions)

    async def test_slash_choices_expose_mode_scope_and_stop(self):
        group=self.bot.tree.get_command('鲸鱼').get_command('wordle')
        cmd=group.get_command('自己玩')
        self.assertEqual([c.value for c in cmd.parameters[0].choices],['当前','普通','超级'])
        self.assertEqual([c.value for c in cmd.parameters[1].choices],['整局','一步'])
        self.assertIsNotNone(group.get_command('停止代玩'))

    async def test_slot_reserved_before_upload_and_stop_during_upload_cannot_start_model(self):
        entered=asyncio.Event(); release=asyncio.Event()
        async def uploading(*args):
            entered.set(); await release.wait()
            return 'https://example.com/board'
        with patch.object(self.bot.wordle,'update',side_effect=uploading):
            task=asyncio.create_task(self.bot.wordle.player.start(1,self.channel,4))
            await entered.wait()
            self.assertIn((1,2),self.bot.wordle.player.sessions)
            self.bot.wordle.player.stop(1,2,4)
            release.set()
            with self.assertRaisesRegex(ValueError,'已停止'):
                await task
        self.bot.llm.chat.assert_not_awaited()
        self.assertFalse(self.bot.wordle.player.sessions)


if __name__=='__main__':
    unittest.main()
