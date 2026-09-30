import asyncio
import io
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch
import discord
from PIL import Image
from bot import Whale
from storage import Store
from test_bot import config,message
from wordle import WordleStore,score,vocabulary,board_text,render_board
from wordle_discord import WordleView,GuessModal
from wordle_avatars import AvatarCache,normalize_avatar


def avatar_png(color='#ff4433',size=(64,64)):
    buffer=io.BytesIO()
    Image.new('RGBA',size,color).save(buffer,format='PNG')
    return buffer.getvalue()


def avatar_member(user=4,color='#ff4433',url='https://cdn.discordapp.com/avatars/4/avatar.png?size=64'):
    asset=SimpleNamespace(url=url,read=AsyncMock(return_value=avatar_png(color)))
    asset.replace=Mock(return_value=asset)
    return SimpleNamespace(id=user,display_name='群友',display_avatar=asset,bot=False,
                           guild_permissions=SimpleNamespace(manage_guild=False))


class WordleRulesTests(unittest.TestCase):
    def test_repeated_letters_are_counted_after_exact_matches(self):
        self.assertEqual(score('balloon','balance'),[2,2,2,0,1,0,0])
        self.assertEqual(score('balloon','cartoon'),[0,2,0,0,2,2,2])
        self.assertEqual(score('apple','allee'),[2,1,0,0,2])
        self.assertEqual(score('abbey','bobby'),[1,0,2,0,2])
        self.assertEqual(score('balloon','balloon'),[2]*7)
        with self.assertRaises(ValueError):
            score('apple','banana')

    def test_bundled_answer_pools_are_valid_and_offline(self):
        for size in (5,7):
            allowed,answers=vocabulary(size)
            self.assertGreater(len(answers),100)
            self.assertGreater(len(allowed),len(answers))
            self.assertTrue(set(answers).issubset(allowed))
            self.assertTrue(all(len(word)==size and word.isascii() and word.islower() and word.isalpha()
                                for word in allowed))


class WordleStoreTests(unittest.TestCase):
    def setUp(self):
        self.store=Store(':memory:')
        self.db=WordleStore(self.store)

    def tearDown(self):
        self.store.close()

    def start(self,mode='超级',guild=1,channel=2):
        with patch('wordle.random.choice',return_value='balloon' if mode=='超级' else 'crane'):
            return self.db.start(guild,channel,3,mode,now=10)

    def test_shared_guesses_and_both_modes(self):
        game=self.start()
        self.assertEqual((game['size'],game['max_tries']),(7,12))
        self.db.guess(1,2,3,'甲','CARTOON','a',now=20)
        game=self.db.guess(1,2,4,'乙','balance','b',now=21)
        self.assertEqual(len(game['guesses']),2)
        self.assertEqual([g['name'] for g in game['guesses']],['甲','乙'])
        self.assertEqual(game['keyboard']['a'],2)
        ordinary=self.start('普通',channel=9)
        self.assertEqual((ordinary['size'],ordinary['max_tries']),(5,6))

    def test_invalid_duplicate_and_repeat_delivery_do_not_consume_tries(self):
        self.start()
        for word in ('apple','abcdefg','1234567','balloon extra','ＢＡＬＬＯＯＮ'):
            with self.subTest(word=word),self.assertRaises(ValueError):
                self.db.guess(1,2,3,'甲',word,word,now=20)
        self.assertEqual(len(self.db.latest(1,2)['guesses']),0)
        self.db.guess(1,2,3,'甲','cartoon','same-request',now=20)
        self.db.guess(1,2,3,'甲','cartoon','same-request',now=20)
        with self.assertRaises(ValueError):
            self.db.guess(1,2,4,'乙','CARTOON','new-request',now=21)
        self.assertEqual(len(self.db.latest(1,2)['guesses']),1)

    def test_user_cooldown_and_server_channel_isolation(self):
        self.start()
        self.start(guild=5,channel=2)
        self.start(channel=9)
        self.db.guess(1,2,3,'甲','cartoon',1,now=20)
        with self.assertRaises(ValueError):
            self.db.guess(1,2,3,'甲','balance',2,now=21)
        self.db.guess(1,2,4,'乙','balance',3,now=21)
        self.assertEqual(len(self.db.latest(5,2)['guesses']),0)
        self.assertEqual(len(self.db.latest(1,9)['guesses']),0)
        with self.assertRaises(ValueError):
            self.db.get(self.db.latest(1,2)['id'],5,2)

    def test_existing_round_cannot_be_overwritten_and_end_requires_permission(self):
        first=self.start()
        with self.assertRaises(ValueError):
            self.start('普通')
        with self.assertRaises(ValueError):
            self.db.stop(1,2,4)
        self.assertEqual(self.db.latest(1,2)['id'],first['id'])
        self.assertEqual(self.db.stop(1,2,4,admin=True)['state'],'stopped')
        self.assertNotEqual(self.start()['id'],first['id'])

    def test_last_chance_win_takes_precedence_over_loss(self):
        self.start()
        words=sorted(vocabulary(7)[0]-{'balloon'})[:11]
        for i,word in enumerate(words):
            self.db.guess(1,2,3,'甲',word,i,now=20+4*i)
        game=self.db.guess(1,2,4,'乙','balloon','win',now=90)
        self.assertEqual(game['state'],'won')
        self.assertEqual(len(game['guesses']),12)
        with self.assertRaises(ValueError):
            self.db.guess(1,2,5,'丙','balance','late',now=99)

    def test_limit_ends_round_and_hides_answer_until_end(self):
        self.start('普通')
        self.assertNotIn('CRANE',board_text(self.db.latest(1,2),full=True))
        for i,word in enumerate(sorted(vocabulary(5)[0]-{'crane'})[:6]):
            game=self.db.guess(1,2,3,'甲',word,i,now=20+4*i)
        self.assertEqual(game['state'],'lost')
        self.assertIn('CRANE',board_text(game))
        self.assertIn('甲',board_text(game,full=True))
        self.assertLess(len(board_text(game,full=True)),1851)

    def test_stale_modal_cannot_consume_next_round(self):
        first=self.start()
        self.db.stop(1,2,3)
        second=self.start()
        with self.assertRaises(ValueError):
            self.db.guess(1,2,4,'乙','balloon','late',expected=first['id'],now=20)
        self.assertEqual(len(self.db.get(second['id'],1,2)['guesses']),0)

    def test_replayed_old_message_cannot_consume_next_round(self):
        self.start()
        self.db.guess(1,2,4,'乙','cartoon','old-request',now=20)
        self.db.stop(1,2,3)
        self.start()
        with self.assertRaises(ValueError):
            self.db.guess(1,2,4,'乙','cartoon','old-request',now=40)
        self.assertEqual(len(self.db.latest(1,2)['guesses']),0)

    def test_round_and_original_board_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'wordle.sqlite3'
            store=Store(path); db=WordleStore(store)
            with patch('wordle.random.choice',return_value='balloon'):
                game=db.start(1,2,3,'超级')
            db.message(game['id'],900)
            game=db.guess(1,2,4,'乙','cartoon','request')
            db.avatar(game['guesses'][0]['id'],1,2,'a'*64)
            store.close()
            store=Store(path)
            try:
                db=WordleStore(store)
                game=db.latest(1,2)
                self.assertEqual(game['message'],'900')
                self.assertEqual(len(game['guesses']),1)
                self.assertEqual(game['guesses'][0]['avatar'],'a'*64)
                self.assertEqual(db.board_round(900,1,2),game['id'])
                self.assertIsNone(db.board_round(900,5,2))
                self.assertEqual(len(db.active_boards()),1)
            finally:
                store.close()

    def test_legacy_schema_migration_keeps_existing_guesses(self):
        self.start()
        self.db.guess(1,2,4,'乙','cartoon','request',now=20)
        with self.store.db:
            self.store.db.execute('ALTER TABLE wordle_guesses DROP COLUMN avatar')
        migrated=WordleStore(self.store)
        migrated=WordleStore(self.store)  # Repeated startup must be harmless.
        guess=migrated.latest(1,2)['guesses'][0]
        self.assertEqual(guess['word'],'cartoon')
        self.assertIsNone(guess['avatar'])

    def test_avatar_snapshot_is_scoped_and_never_replaced(self):
        self.start()
        game=self.db.guess(1,2,4,'乙','cartoon','request',now=20)
        rid=game['guesses'][0]['id']
        self.db.avatar(rid,5,2,'a'*64)
        self.assertIsNone(self.db.latest(1,2)['guesses'][0]['avatar'])
        self.db.avatar(rid,1,2,'a'*64)
        self.db.avatar(rid,1,2,'b'*64)
        self.assertEqual(self.db.latest(1,2)['guesses'][0]['avatar'],'a'*64)
        with self.assertRaises(ValueError):
            self.db.avatar(rid,1,2,'../avatar')

    def test_avatars_are_circular_and_each_guess_uses_its_own_snapshot(self):
        for mode in ('普通','超级'):
            channel=5 if mode=='普通' else 7
            self.start(mode,channel=channel)
            self.db.guess(1,channel,4,'甲','slate' if mode=='普通' else 'balance',1,now=20)
            game=self.db.guess(1,channel,5,'乙','adieu' if mode=='普通' else 'cartoon',2,now=21)
            for guess,key in zip(game['guesses'],('a'*64,'b'*64)):
                guess['avatar']=key
            data=render_board(game,{'a'*64:avatar_png('#ff0000'),'b'*64:avatar_png('#0000ff')})
            with Image.open(io.BytesIO(data)) as im:
                center_x,center_y,stride=(54,143,89) if mode=='普通' else (50,135,73)
                self.assertEqual(im.getpixel((center_x,center_y)),(255,0,0))
                self.assertEqual(im.getpixel((center_x,center_y+stride)),(0,0,255))
                self.assertNotEqual(im.getpixel((28,center_y-(26 if mode=='普通' else 22))),(255,0,0))

    def test_missing_or_corrupt_avatar_does_not_break_board(self):
        self.start()
        game=self.db.guess(1,2,4,'群友','balance',1,now=20)
        game['guesses'][0]['avatar']='a'*64
        for avatars in ({},{'a'*64:b'broken PNG'}):
            with Image.open(io.BytesIO(render_board(game,avatars))) as im:
                self.assertEqual(im.size,(560,1170))
                self.assertEqual(im.getpixel((112,115)),(32,134,108))

    def test_png_dimensions_colors_and_file_size(self):
        for mode in ('普通','超级'):
            game=self.start(mode,channel=5 if mode=='普通' else 7)
            channel=int(game['channel'])
            word='slate' if mode=='普通' else 'balance'
            game=self.db.guess(1,channel,4,'参与群友',word,1,now=20)
            png=render_board(game)
            with Image.open(io.BytesIO(png)) as im:
                self.assertEqual(im.format,'PNG')
                self.assertEqual(im.width,560)
                self.assertGreater(im.height,600)
                if mode=='超级':
                    self.assertEqual(im.getpixel((112,115)),(32,134,108))
            self.assertLess(len(png),300_000)


class WordleAvatarCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.folder=Path(self.directory.name)/'avatars'
        self.cache=AvatarCache(self.folder)

    async def asyncTearDown(self):
        self.directory.cleanup()

    async def test_concurrent_snapshots_share_one_download_and_survive_restart(self):
        member=avatar_member()
        keys=await asyncio.gather(*(self.cache.snapshot(member) for _ in range(4)))
        self.assertEqual(len(set(keys)),1)
        member.display_avatar.read.assert_awaited_once()
        member.display_avatar.replace.assert_called_with(size=64,format='png',static_format='png')
        restored=AvatarCache(self.folder)
        self.assertEqual(await restored.snapshot(member),keys[0])
        member.display_avatar.read.assert_awaited_once()
        game={'guesses':[{'avatar':keys[0]}]}
        with Image.open(io.BytesIO(restored.images(game)[keys[0]])) as im:
            self.assertEqual(im.size,(96,96))

    async def test_failed_download_has_backoff_and_is_optional(self):
        member=avatar_member()
        member.display_avatar.read.side_effect=OSError('offline')
        self.assertIsNone(await self.cache.snapshot(member))
        self.assertIsNone(await self.cache.snapshot(member))
        member.display_avatar.read.assert_awaited_once()
        self.assertIsNone(await self.cache.snapshot(SimpleNamespace(id=4)))

    async def test_slow_download_times_out_without_blocking_next_operation(self):
        member=avatar_member()
        async def blocked():
            await asyncio.Event().wait()
        member.display_avatar.read.side_effect=blocked
        started=time.monotonic()
        self.assertIsNone(await self.cache.snapshot(member))
        self.assertLess(time.monotonic()-started,4)
        self.assertFalse(self.cache.pending)

    async def test_avatar_change_uses_new_file_but_keeps_old_snapshot(self):
        first=await self.cache.snapshot(avatar_member())
        second=await self.cache.snapshot(avatar_member(color='#0000ff',url='https://cdn.discordapp.com/avatars/4/new.png'))
        self.assertNotEqual(first,second)
        self.assertTrue((self.folder/(first+'.png')).exists())
        self.assertTrue((self.folder/(second+'.png')).exists())

    async def test_corrupt_disk_cache_can_be_repaired_and_bad_keys_are_ignored(self):
        member=avatar_member()
        key=await self.cache.snapshot(member)
        (self.folder/(key+'.png')).write_bytes(b'broken PNG')
        restored=AvatarCache(self.folder)
        self.assertEqual(restored.images({'guesses':[{'avatar':'../outside'},{'avatar':key}]}),{})
        self.assertEqual(await restored.snapshot(member),key)
        self.assertEqual(member.display_avatar.read.await_count,2)

    async def test_pruning_preserves_referenced_or_recent_avatars(self):
        key=await self.cache.snapshot(avatar_member())
        for other in ('b'*64,'c'*64):
            (self.folder/(other+'.png')).write_bytes(avatar_png())
        old=time.time()-31*86400
        os.utime(self.folder/(key+'.png'),(old,old))
        os.utime(self.folder/('b'*64+'.png'),(old,old))
        self.cache.prune({key})
        self.assertTrue((self.folder/(key+'.png')).exists())
        self.assertFalse((self.folder/('b'*64+'.png')).exists())
        self.assertTrue((self.folder/('c'*64+'.png')).exists())

    async def test_oversized_or_invalid_image_is_rejected(self):
        for data in (b'broken PNG',b'x'*1_000_001,avatar_png(size=(513,64))):
            with self.assertRaises((ValueError,OSError)):
                normalize_avatar(data)

class WordleDiscordTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store=Store(':memory:')
        self.bot=Whale(config(),self.store)
        self.bot._connection.user=SimpleNamespace(id=50,name='鲸鱼娘')
        self.bot.llm=SimpleNamespace(chat=AsyncMock(side_effect=AssertionError('Wordle called LLM')))
        self.allowed=patch('bot.discord.TextChannel',SimpleNamespace)
        self.allowed.start()
        self.channel=SimpleNamespace(id=2,guild=SimpleNamespace(id=1,me=None),
            send=AsyncMock(return_value=SimpleNamespace(id=900)),
            get_partial_message=lambda mid:SimpleNamespace(edit=self.edit))
        self.edit=AsyncMock(return_value=SimpleNamespace(id=900))

    async def asyncTearDown(self):
        await self.bot.close()
        self.allowed.stop()
        self.store.close()

    async def start(self):
        with patch('wordle.random.choice',return_value='balloon'):
            await self.bot.wordle.run(1,self.channel,3,'甲','开始','超级','start')
        return self.bot.wordle.db.latest(1,2)

    def interaction(self,user=4,mid=900):
        return SimpleNamespace(id=1000+user,guild_id=1,channel_id=2,channel=self.channel,
            user=SimpleNamespace(id=user,display_name='群友',guild_permissions=SimpleNamespace(manage_guild=False)),
            message=SimpleNamespace(id=mid),
            response=SimpleNamespace(defer=AsyncMock(),send_message=AsyncMock(),send_modal=AsyncMock(),is_done=lambda:False),
            followup=SimpleNamespace(send=AsyncMock()))

    async def test_shared_board_edits_same_message_and_uploads_png(self):
        with patch.object(self.bot.wordle,'can_attach',return_value=True):
            await self.start()
            self.assertTrue(self.channel.send.call_args.kwargs['file'].filename.endswith('.png'))
            await self.bot.wordle.interact(self.interaction(),'猜','cartoon')
        self.channel.send.assert_awaited_once()
        self.edit.assert_awaited_once()
        self.assertEqual(len(self.edit.call_args.kwargs['attachments']),1)
        self.assertEqual(self.bot.wordle.db.latest(1,2)['message'],'900')
        self.bot.llm.chat.assert_not_awaited()
        self.assertEqual(self.store.usage()['calls'],0)

    async def test_slash_guess_saves_avatar_and_invalid_word_does_not_download(self):
        interaction=self.interaction()
        interaction.user=avatar_member()
        with patch.object(self.bot.wordle,'can_attach',return_value=True):
            await self.start()
            await self.bot.wordle.interact(interaction,'猜','invalid!')
            interaction.user.display_avatar.read.assert_not_awaited()
            await self.bot.wordle.interact(interaction,'猜','cartoon')
            key=self.bot.wordle.db.latest(1,2)['guesses'][0]['avatar']
            self.assertIsNotNone(key)
            await self.bot.wordle.interact(interaction,'状态')
        interaction.user.display_avatar.read.assert_awaited_once()
        self.bot.llm.chat.assert_not_awaited()

    async def test_avatar_failure_does_not_lose_guess_or_png(self):
        interaction=self.interaction()
        interaction.user=avatar_member()
        interaction.user.display_avatar.read.side_effect=OSError('offline')
        with patch.object(self.bot.wordle,'can_attach',return_value=True):
            await self.start()
            await self.bot.wordle.interact(interaction,'猜','cartoon')
        guess=self.bot.wordle.db.latest(1,2)['guesses'][0]
        self.assertEqual(guess['word'],'cartoon')
        self.assertIsNone(guess['avatar'])
        self.assertTrue(self.edit.call_args.kwargs['attachments'][0].filename.endswith('.png'))

    async def test_prefix_guess_passes_member_and_legacy_rows_use_cached_members(self):
        with patch.object(self.bot.wordle,'can_attach',return_value=True):
            await self.start()
            m=message(777,'!鲸鱼 wordle 猜 cartoon',uid=4)
            m.channel=self.channel
            m.author=avatar_member()
            await self.bot.on_message(m)
            self.assertIsNotNone(self.bot.wordle.db.latest(1,2)['guesses'][0]['avatar'])
            self.bot.wordle.db.guess(1,2,5,'乙','balance','legacy')
            second=avatar_member(user=5,color='#0000ff',url='https://cdn.discordapp.com/avatars/5/avatar.png')
            self.channel.guild.get_member=lambda uid:second if uid==5 else None
            await self.bot.wordle.interact(self.interaction(),'状态')
        guesses=self.bot.wordle.db.latest(1,2)['guesses']
        self.assertTrue(all(g['avatar'] for g in guesses))
        self.assertNotEqual(guesses[0]['avatar'],guesses[1]['avatar'])
        self.bot.llm.chat.assert_not_awaited()

    async def test_no_attachment_permission_uses_text_and_keeps_progress(self):
        await self.start()
        self.assertNotIn('file',self.channel.send.call_args.kwargs)
        await self.bot.wordle.interact(self.interaction(),'猜','balance')
        self.assertIn('🟩🟩🟩⬛🟨⬛⬛',self.edit.call_args.kwargs['content'])
        self.assertEqual(self.edit.call_args.kwargs['attachments'],[])
        self.assertEqual(len(self.bot.wordle.db.latest(1,2)['guesses']),1)

    async def test_failed_image_upload_falls_back_to_text(self):
        response=SimpleNamespace(status=403,reason='Forbidden')
        self.channel.send.side_effect=[discord.Forbidden(response,''),SimpleNamespace(id=900)]
        with patch.object(self.bot.wordle,'can_attach',return_value=True):
            await self.start()
        self.assertEqual(self.channel.send.await_count,2)
        self.assertNotIn('file',self.channel.send.call_args.kwargs)
        self.assertEqual(self.bot.wordle.db.latest(1,2)['message'],'900')

    async def test_failed_image_edit_removes_attachment_and_updates_text(self):
        await self.start()
        response=SimpleNamespace(status=403,reason='Forbidden')
        self.edit.side_effect=[discord.Forbidden(response,''),SimpleNamespace(id=900)]
        with patch.object(self.bot.wordle,'can_attach',return_value=True):
            await self.bot.wordle.interact(self.interaction(),'猜','cartoon')
        self.assertEqual(self.edit.await_count,2)
        self.assertEqual(self.edit.call_args.kwargs['attachments'],[])
        self.assertIn('CARTOON',self.edit.call_args.kwargs['content'])

    async def test_deleted_board_is_recreated_without_resetting_round(self):
        await self.start()
        response=SimpleNamespace(status=404,reason='Not Found')
        self.edit.side_effect=discord.NotFound(response,'')
        self.channel.send.return_value=SimpleNamespace(id=901)
        await self.bot.wordle.interact(self.interaction(),'猜','cartoon')
        game=self.bot.wordle.db.latest(1,2)
        self.assertEqual(game['message'],'901')
        self.assertEqual(len(game['guesses']),1)

    async def test_concurrent_winning_guesses_do_not_double_consume(self):
        await self.start()
        await asyncio.gather(self.bot.wordle.interact(self.interaction(4),'猜','balloon'),
                             self.bot.wordle.interact(self.interaction(5),'猜','balloon'))
        game=self.bot.wordle.db.latest(1,2)
        self.assertEqual(game['state'],'won')
        self.assertEqual(len(game['guesses']),1)
        self.assertTrue(all(b.disabled for b in self.edit.call_args.kwargs['view'].children))

    async def test_button_opens_modal_and_checks_original_message(self):
        game=await self.start()
        view=WordleView(self.bot.wordle,game)
        interaction=self.interaction()
        await view.children[0].callback(interaction)
        modal=interaction.response.send_modal.call_args.args[0]
        self.assertIsInstance(modal,GuessModal)
        self.assertEqual((modal.word.min_length,modal.word.max_length),(7,7))
        bad=self.interaction(mid=901)
        await view.children[0].callback(bad)
        bad.response.send_modal.assert_not_awaited()
        bad.response.send_message.assert_awaited_once()

    async def test_modal_submission_stays_local_and_old_modal_is_rejected(self):
        game=await self.start()
        modal=GuessModal(self.bot.wordle,game)
        modal.word._value='cartoon'
        await modal.on_submit(self.interaction())
        self.assertEqual(len(self.bot.wordle.db.latest(1,2)['guesses']),1)
        self.bot.wordle.db.stop(1,2,3)
        with patch('wordle.random.choice',return_value='balloon'):
            self.bot.wordle.db.start(1,2,3,'超级')
        await modal.on_submit(self.interaction(5))
        self.assertEqual(len(self.bot.wordle.db.latest(1,2)['guesses']),0)
        self.bot.llm.chat.assert_not_awaited()

    async def test_feature_off_and_unselected_channel_cannot_start_or_guess(self):
        self.bot.local.db.toggle(1,'wordle',False)
        await self.bot.wordle.interact(self.interaction(),'开始','超级')
        self.assertIsNone(self.bot.wordle.db.latest(1,2))
        interaction=self.interaction(); interaction.channel.id=99; interaction.channel_id=99
        await self.bot.wordle.interact(interaction,'开始','超级')
        interaction.response.send_message.assert_awaited_once()
        self.channel.send.assert_not_awaited()

    async def test_disabled_feature_keeps_status_and_owner_stop_available(self):
        await self.start()
        self.bot.local.db.toggle(1,'wordle',False)
        await self.bot.wordle.interact(self.interaction(),'猜','balloon')
        self.assertEqual(len(self.bot.wordle.db.latest(1,2)['guesses']),0)
        await self.bot.wordle.interact(self.interaction(),'状态')
        self.edit.assert_awaited_once()
        await self.bot.wordle.interact(self.interaction(3),'结束')
        self.assertEqual(self.bot.wordle.db.latest(1,2)['state'],'stopped')

    async def test_status_spam_is_limited_without_spending_a_guess(self):
        await self.start()
        for _ in range(7):
            text,_=await self.bot.wordle.run(1,self.channel,4,'乙','状态','','status')
        self.assertIn('太密',text)
        self.assertEqual(self.edit.await_count,6)
        self.assertEqual(len(self.bot.wordle.db.latest(1,2)['guesses']),0)
        self.bot.llm.chat.assert_not_awaited()

    async def test_prefix_and_board_reply_are_excluded_from_ai_and_memory(self):
        await self.start()
        m=message(777,'!鲸鱼 wordle 猜 cartoon')
        m.channel=self.channel
        await self.bot.on_message(m)
        self.assertTrue(self.bot.local.db.is_input(777,1,2))
        later=message(778,'balance',uid=8)
        later.channel=self.channel
        later.reference=SimpleNamespace(message_id=900,channel_id=2,resolved=None)
        self.channel.fetch_message=AsyncMock(return_value=message(900,'棋盘',uid=50,bot=True))
        await self.bot.on_message(later)
        self.assertEqual(len(self.bot.wordle.db.latest(1,2)['guesses']),2)
        self.assertTrue(self.bot.local.db.is_input(778,1,2))
        self.assertFalse(self.bot.workers)
        self.bot.llm.chat.assert_not_awaited()
        self.assertEqual(self.store.usage()['calls'],0)

    async def test_persistent_board_buttons_are_restored(self):
        game=await self.start()
        with patch.object(self.bot,'add_view') as add:
            self.bot.wordle.restore()
        self.assertEqual(add.call_args.kwargs['message_id'],900)
        self.assertTrue(add.call_args.args[0].is_persistent())

    async def test_slash_group_fits_and_exposes_real_mode_choices(self):
        group=self.bot.tree.get_commands()[0]
        self.assertEqual(len(group.commands),25)
        wordle=group.get_command('wordle')
        self.assertEqual({cmd.name for cmd in wordle.commands},{'开始','猜','状态','结束','规则'})
        self.assertEqual([choice.value for choice in wordle.get_command('开始').parameters[0].choices],['普通','超级'])
        await wordle.get_command('规则').callback(self.interaction())
        self.bot.llm.chat.assert_not_awaited()


if __name__=='__main__':
    unittest.main()
