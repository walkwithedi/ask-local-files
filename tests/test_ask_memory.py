#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ask_memory.py 的测试，只用标准库：python3 -m unittest discover -s ask-local-files/tests"""

import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import ask_memory as am  # noqa: E402

PLAN = '项目计划\n\n第一阶段在三月底完成验收。\n\n预算总额为一百万元，由财务部负责。'
ANSWER = ('第一阶段三月底验收（文件 1：「第一阶段在三月底完成验收」）。\n'
          '- 预算一百万元（文件 1：「预算总额为一百万元」）')


def read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


class TextTest(unittest.TestCase):
    def test_keywords(self):
        self.assertEqual(am.keywords('第一阶段什么时候验收？'), ['第一阶段', '验收'])
        self.assertEqual(am.keywords('Project Atlas 的预算是多少'), ['project', 'atlas', '预算'])
        self.assertEqual(am.keywords('的'), [])
        self.assertEqual(am.keywords('项目进度安排说明'), ['项目', '目进', '进度', '度安', '安排', '排说', '说明'])
        self.assertEqual(am.keywords('甲 乙'), ['甲', '乙'])

    def test_tokens(self):
        self.assertEqual(am.tokens('验收ABC 完成'), ['验收', 'abc', '完成'])

    def test_split_paras(self):
        self.assertEqual(am.split_paras('一\n\n\n二\n三\n\n  \n'), ['一', '二\n三'])
        long = '\n'.join(['行' * 500] * 3)
        self.assertTrue(all(len(p) <= am.PARA_CHARS for p in am.split_paras(long)))
        one = '句子。' * 600
        parts = am.split_paras(one)
        self.assertTrue(all(len(p) <= am.PARA_CHARS for p in parts))
        self.assertEqual(''.join(parts), one)

    def test_conclusions(self):
        got = am.conclusions(ANSWER)
        self.assertEqual([(n, q, c) for _, n, q, c in got],
                         [(1, '第一阶段在三月底完成验收', '第一阶段三月底验收'), (1, '预算总额为一百万元', '预算一百万元')])
        two = am.conclusions('先说明。负责人是张三（文件 2：「张三负责」；文件 3：「由张三牵头」）')
        self.assertEqual([(n, c) for _, n, _, c in two], [(2, '负责人是张三'), (3, '负责人是张三')])


class MemoryCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='ask-memory-test-')
        self.file = os.path.join(self.root, '计划.txt')
        self.write(PLAN)
        self.mem = am.Memory(os.path.join(self.root, 'data', 'memory.sqlite'))

    def tearDown(self):
        self.mem.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, text, path=None):
        path = path or self.file
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
        st = os.stat(path)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1000000000))    # 保证修改时间与上次不同

    def remember(self, path=None):
        path = path or self.file
        return self.mem.remember_file(path, os.stat(path), read(path))

    def recall(self, q, budget=10000, exclude=()):
        return self.mem.recall(q, exclude, budget, read)


class MemoryTest(MemoryCase):
    def test_private_file(self):
        self.remember()
        self.assertEqual(os.stat(self.mem.path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(os.path.dirname(self.mem.path)).st_mode & 0o777, 0o700)

    def test_remember_file_once(self):
        self.assertTrue(self.remember())
        self.assertFalse(self.remember())
        self.assertEqual(self.mem.stats()['files'], 1)

    def test_recall_paragraphs(self):
        self.remember()
        got = self.recall('预算多少')
        self.assertEqual(got, [{'path': self.file, 'facts': [], 'paras': ['预算总额为一百万元，由财务部负责。']}])
        self.assertEqual(self.recall('毫不相干'), [])
        self.assertEqual(self.recall('预算', exclude=[self.file]), [])
        self.assertEqual(self.recall('预算', budget=0), [])

    def test_recall_facts_first_with_their_paragraph(self):
        self.remember()
        self.assertEqual(self.mem.remember_answer('a1', '问', ANSWER, {1: self.file}), 2)
        got = self.recall('第一阶段验收')
        self.assertEqual(got[0]['facts'][0]['conclusion'], '第一阶段三月底验收')
        self.assertIn('第一阶段在三月底完成验收。', got[0]['paras'])

    def test_budget(self):
        self.remember()
        self.mem.remember_answer('a1', '问', ANSWER, {1: self.file})
        self.assertEqual(self.recall('第一阶段验收', budget=10), [])
        got = self.recall('预算', budget=len('预算总额为一百万元，由财务部负责。'))
        self.assertEqual(got[0]['facts'], [])

    def test_missing_and_unknown_quotes_not_kept(self):
        self.remember()
        n = self.mem.remember_answer('a1', '问', ANSWER, {1: self.file},
                                     missing=[{'file': 1, 'quote': '预算总额为一百万元'}])
        self.assertEqual(n, 1)
        self.assertEqual(self.mem.remember_answer('a2', '问', ANSWER, {2: self.file}), 0)
        self.assertEqual(self.mem.remember_answer('a3', '问', ANSWER, {1: '/没有记住的文件'}), 0)

    def test_merge_counts(self):
        self.remember()
        self.mem.remember_answer('a1', '问', ANSWER, {1: self.file})
        self.mem.remember_answer('a2', '再问', ANSWER.replace('三月底验收', '三月底验收完成'), {1: self.file})
        facts = {f['quote']: f for f in self.mem.listing()['facts']}
        self.assertEqual(len(facts), 2)
        self.assertEqual(facts['第一阶段在三月底完成验收']['count'], 2)
        self.assertEqual(facts['第一阶段在三月底完成验收']['conclusion'], '第一阶段三月底验收完成')
        self.mem.remember_answer('a2', '再问', ANSWER, {1: self.file})     # 同一条回答不重复计数
        self.assertEqual({f['quote']: f for f in self.mem.listing()['facts']}['预算总额为一百万元']['count'], 2)

    def test_wrong_forgets_and_rejects(self):
        self.remember()
        self.mem.remember_answer('a1', '问', ANSWER, {1: self.file})
        self.assertEqual(self.mem.wrong('a1'), 2)
        self.assertEqual(self.mem.stats()['facts'], 0)
        self.assertEqual(self.mem.remember_answer('a2', '问', ANSWER, {1: self.file}), 0)
        other = '第一阶段已经验收（文件 1：「第一阶段在三月底完成验收」）'
        self.assertEqual(self.mem.remember_answer('a3', '问', other, {1: self.file}), 1)

    def test_changed_file_rechecked(self):
        self.remember()
        self.mem.remember_answer('a1', '问', ANSWER, {1: self.file})
        self.write(PLAN.replace('三月底', '五月'))
        got = self.recall('第一阶段 预算')
        quotes = [f['quote'] for b in got for f in b['facts']]
        self.assertEqual(quotes, ['预算总额为一百万元'])
        self.assertIn('第一阶段在五月完成验收。', got[0]['paras'])
        self.assertEqual(self.mem.stats()['facts'], 1)

    def test_deleted_file_forgotten(self):
        self.remember()
        self.mem.remember_answer('a1', '问', ANSWER, {1: self.file})
        os.remove(self.file)
        self.assertEqual(self.recall('预算'), [])
        self.assertEqual(self.mem.stats(), {'files': 0, 'facts': 0, 'bytes': 0, 'max_bytes': am.MAX_BYTES})

    def test_unreadable_changed_file_forgotten(self):
        self.remember()
        self.write(PLAN + '\n\n补充')

        def broken(path):
            raise ValueError('读不出')
        self.assertEqual(self.mem.recall('预算', (), 1000, broken), [])
        self.assertEqual(self.mem.stats()['files'], 0)

    def test_cap_forgets_least_recently_used(self):
        other = os.path.join(self.root, '另一份.txt')
        self.write('预算另有说明。' * 10, other)
        self.mem.max_bytes = len(read(other).encode('utf-8')) + 10      # 只放得下一个
        self.remember()
        time.sleep(0.01)
        self.remember(other)
        self.assertEqual([f['path'] for f in self.mem.listing()['files']], [other])

    def test_forget_and_clear(self):
        self.remember()
        self.mem.remember_answer('a1', '问', ANSWER, {1: self.file})
        fid = self.mem.listing('预算')['facts'][0]['id']
        self.assertTrue(self.mem.forget_fact(fid))
        self.assertFalse(self.mem.forget_fact(fid))
        self.assertTrue(self.mem.forget_file(self.file))
        self.assertEqual(self.mem.stats()['facts'], 0)
        self.remember()
        self.mem.remember_answer('a1', '问', ANSWER, {1: self.file})
        self.mem.wrong('a1')
        self.mem.clear()
        self.assertEqual(self.mem.stats()['files'], 0)
        self.assertEqual(self.recall('预算'), [])
        self.remember()
        self.assertEqual(self.mem.remember_answer('a2', '问', ANSWER, {1: self.file}), 2)    # 清空也清了「不对」

    def test_listing_search(self):
        self.remember()
        self.mem.remember_answer('a1', '问', ANSWER, {1: self.file})
        self.assertEqual([f['conclusion'] for f in self.mem.listing('预算')['facts']], ['预算一百万元'])
        self.assertEqual(self.mem.listing('100%')['facts'], [])
        self.assertEqual(len(self.mem.listing('计划')['files']), 1)


class _NoFts:
    """建库时拒绝建虚拟表，模拟没有 FTS5 的 SQLite。"""

    def __init__(self, db):
        self.db = db

    def execute(self, sql, *args):
        if 'VIRTUAL' in sql:
            raise sqlite3.OperationalError('no such module: fts5')
        return self.db.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self.db, name)


class NoFtsTest(MemoryCase):
    def setUp(self):
        super().setUp()
        create = am.Memory._create
        self.mem._create = lambda db: create(_NoFts(db))

    def test_like_fallback(self):
        self.remember()
        self.assertFalse(self.mem.fts)
        self.mem.remember_answer('a1', '问', ANSWER, {1: self.file})
        self.assertEqual(self.recall('预算')[0]['facts'][0]['quote'], '预算总额为一百万元')
        self.mem.wrong('a1')
        self.mem.clear()
        self.assertEqual(self.recall('预算'), [])


class BrokenDatabaseTest(MemoryCase):
    def test_broken_file_kept_and_rebuilt(self):
        os.makedirs(os.path.dirname(self.mem.path))
        with open(self.mem.path, 'wb') as f:
            f.write(b'not a database' * 100)
        self.assertTrue(self.remember())
        kept = [n for n in os.listdir(os.path.dirname(self.mem.path)) if '.broken-' in n]
        self.assertEqual(len(kept), 1)
        self.assertEqual(self.recall('预算')[0]['path'], self.file)


if __name__ == '__main__':
    unittest.main()
