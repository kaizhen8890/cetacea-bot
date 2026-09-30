import sqlite3
import unittest
import json
from unittest.mock import patch

from memory import install,record,next_batch,save_summary,retrieve,erase_user,erase_message,save_long,pending_fold,status
from memory_worker import summarize


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:')
        self.db.row_factory=sqlite3.Row
        install(self.db)
        self.db.execute('CREATE TABLE memories(guild TEXT,channel TEXT,user TEXT,key TEXT,value TEXT)')

    def tearDown(self):
        self.db.close()

    def test_conversation_summary_retrieval_and_server_private_separation(self):
        record(self.db,1,10,100,7,'小明','我在学习电磁感应',engaged=True,created=100)
        record(self.db,2,10,100,99,'鲸鱼娘','磁通量变化会产生感应电流',role='assistant',
               engaged=True,subject=7,created=101)
        rows=next_batch(self.db,now=2000)
        self.assertEqual(len(rows),2)
        save_summary(self.db,rows,{'body':'小明在学习电磁感应，关注磁通量变化。','keywords':['电磁感应']})
        self.assertEqual(len(retrieve(self.db,10,200,7,'电磁感应还有什么例子')),1)
        self.assertEqual(retrieve(self.db,11,200,7,'电磁感应'),[])
        record(self.db,3,10,300,7,'小明','我想换工作',engaged=True,public=False,created=100)
        record(self.db,4,10,300,99,'鲸鱼娘','先想想目标',role='assistant',
               engaged=True,public=False,subject=7,created=101)
        save_summary(self.db,next_batch(self.db,now=2000),
                     {'body':'小明在考虑换工作','keywords':['换工作']})
        self.assertEqual(retrieve(self.db,10,200,7,'换工作'),[])
        self.assertEqual(len(retrieve(self.db,10,300,7,'换工作')),1)

    def test_forgetting_removes_derived_summary_and_assistant_reply(self):
        record(self.db,1,10,100,7,'小明','喜欢米饭',engaged=True,created=100)
        record(self.db,2,10,100,99,'鲸鱼娘','我也是',role='assistant',
               engaged=True,subject=7,created=101)
        save_summary(self.db,next_batch(self.db,now=2000),
                     {'body':'小明喜欢米饭','keywords':['米饭']})
        erase_user(self.db,10,7)
        self.assertEqual(self.db.execute('SELECT count(*) FROM journal').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM auto_memories').fetchone()[0],0)

    def test_long_memory_uses_validated_sources_only(self):
        for i in range(10):
            record(self.db,100+i,10,100,7,'小明',f'物理问题{i}',engaged=True,created=i*100)
            record(self.db,200+i,10,100,99,'鲸鱼娘','解释',role='assistant',
                   engaged=True,subject=7,created=i*100+1)
            rows=[dict(r) for r in self.db.execute('SELECT * FROM journal WHERE id IN (?,?)',
                                                   (str(100+i),str(200+i)))]
            save_summary(self.db,rows,{'body':f'小明研究物理{i}','keywords':['物理']})
        rows=pending_fold(self.db,10)
        self.assertEqual(len(rows),10)
        self.assertTrue(save_long(self.db,rows,[{'body':'小明持续研究物理',
            'keywords':['物理'],'source_summary_ids':[r['id'] for r in rows]}]))
        self.assertEqual(len(retrieve(self.db,10,200,7,'物理问题')),1)
        erase_message(self.db,100)
        self.assertEqual(self.db.execute(
            "SELECT count(*) FROM auto_memories WHERE kind='long'").fetchone()[0],0)
        self.assertEqual(self.db.execute(
            'SELECT count(*) FROM auto_memories WHERE folded=0').fetchone()[0],9)
        self.assertTrue(retrieve(self.db,10,200,7,'物理问题'))

    def test_old_long_memory_survives_many_new_short_memories(self):
        self.db.execute('''INSERT INTO auto_memories
            (guild,channel,kind,body,keywords,participants,source_ids,public,created)
            VALUES('10','100','long','小明喜欢看鲸鱼','["鲸鱼"]','["7"]','[]',1,1)''')
        for i in range(301):
            self.db.execute('''INSERT INTO auto_memories
                (guild,channel,kind,body,keywords,participants,source_ids,public,created)
                VALUES('10','100','glance','大家聊晚饭','["晚饭"]','[]','[]',1,?)''',(i+2,))
        self.assertEqual(retrieve(self.db,10,200,7,'鲸鱼有什么好看的'),[
            {'kind':'long','text':'小明喜欢看鲸鱼','channel':'100'}])

    def test_status_only_counts_current_server(self):
        record(self.db,1,10,100,7,'小明','第一个服务器',created=100)
        record(self.db,2,20,200,8,'小红','第二个服务器',created=100)
        self.assertEqual(status(self.db,10)['pending'],1)
        self.assertEqual(status(self.db,20)['pending'],1)

    def test_local_summary_sees_whole_utterance_and_keeps_raw_sources(self):
        for mid,text in enumerate(['电','磁','感','应','是','什','么'],1):
            record(self.db,mid,10,100,7,'小明',text,engaged=True,created=100+mid)
        rows=next_batch(self.db,now=2000)
        result={'body':'小明询问电磁感应','keywords':['电磁感应']}
        with patch('memory_worker.local_request',return_value={
                'message':{'content':json.dumps(result,ensure_ascii=False)}}) as local:
            summary=summarize('local-test-model','conversation',rows)
        payload=json.loads(local.call_args.args[1]['messages'][1]['content'])
        self.assertEqual(payload,[{'speaker':'小明','role':'user','text':'电磁感应是什么'}])
        save_summary(self.db,rows,summary)
        sources=json.loads(self.db.execute('SELECT source_ids FROM auto_memories').fetchone()[0])
        self.assertEqual(len(sources),7)


if __name__=='__main__':
    unittest.main()
