#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ask_memory.py 的测试，只用标准库：python3 -m unittest discover -s ask-local-files/tests"""

import json
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
        # 原句不在了的结论留待更新时由模型改写，先不拿去参考；文件排进更新队列
        self.assertEqual(self.mem.stats()['facts'], 2)
        self.assertEqual(self.mem.plan_update()[0], ('file', self.file))

    def test_deleted_file_forgotten(self):
        self.remember()
        self.mem.remember_answer('a1', '问', ANSWER, {1: self.file})
        os.remove(self.file)
        self.assertEqual(self.recall('预算'), [])
        self.assertEqual(self.mem.stats(), {'files': 0, 'facts': 0, 'topics': 0, 'queued': 0, 'extract': 0, 'bytes': 0,
                                            'max_bytes': am.MAX_BYTES})

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


def reply(**plan):
    return '```json\n' + json.dumps(plan, ensure_ascii=False) + '\n```'


class UpdateTest(MemoryCase):
    """更新：用写好的回答代替模型。"""

    def setUp(self):
        super().setUp()
        self.remember()
        self.mem.remember_answer('a1', '问', ANSWER, {1: self.file})
        self.ids = {f['quote']: f['id'] for f in self.mem.listing()['facts']}

    def batch(self, full=False):
        units = self.mem.plan_update(full)
        self.assertTrue(units)
        payload, ctx = self.mem.update_batch(units[0], 100000)
        return json.loads(payload), ctx

    def topic(self):
        return {'id': 'new', 'title': '项目安排', 'points': [
            {'text': '第一阶段三月底验收', 'file': 1, 'quote': '第一阶段在三月底完成验收'},
            {'text': '预算一百万', 'file': 1, 'quote': '预算总额为一百万元'}]}

    def test_queue_and_topic_page(self):
        self.assertEqual(self.mem.pending(), 2)
        payload, ctx = self.batch()
        self.assertEqual(payload['files'], {'1': self.file})
        self.assertEqual({c['quote'] for c in payload['conclusions']}, set(self.ids))
        run, counts, changes = self.mem.apply_update(ctx, reply(topics=[self.topic()]), 'manual')
        self.assertEqual(counts, {'topics_new': 1})
        self.assertEqual(am.summary(counts), '新建主题页 1 页')
        self.assertEqual(changes, ['新建主题页：项目安排'])
        self.assertEqual(self.mem.pending(), 0)
        topics = self.mem.listing()['topics']
        self.assertEqual([t['title'] for t in topics], ['项目安排'])
        self.assertEqual(len(topics[0]['points']), 2)
        # 提问时主题页要点排在最前，同一原句不重复给结论
        got = self.recall('第一阶段验收')
        self.assertEqual(got[0]['facts'][0]['conclusion'], '项目安排：第一阶段三月底验收')
        self.assertEqual(len([f for f in got[0]['facts'] if f['quote'] == '第一阶段在三月底完成验收']), 1)
        self.assertEqual(self.mem.listing('一百万')['topics'][0]['title'], '项目安排')
        self.assertEqual(self.mem.stats()['topics'], 1)

    def test_quote_check_and_rejected(self):
        payload, ctx = self.batch()
        bad = {'id': 'new', 'title': '编的', 'points': [{'text': '编的', 'file': 1, 'quote': '文件里没有这句'}]}
        run, counts, _ = self.mem.apply_update(ctx, reply(topics=[bad]), 'manual')
        self.assertEqual((run, counts), (None, {}))
        self.assertEqual(self.mem.history(), [])
        # 「不对」过的结论不会经由主题页写回
        self.mem.wrong('a1')
        self.remember()
        self.mem.remember_answer('a9', '问', '预算为一百万元（文件 1：「预算总额为一百万元」）', {1: self.file})
        payload, ctx = self.batch()
        page = {'id': 'new', 'title': '验收', 'points': [
            {'text': '第一阶段三月底验收', 'file': 1, 'quote': '第一阶段在三月底完成验收'}]}
        self.assertEqual(self.mem.apply_update(ctx, reply(topics=[page]), 'auto')[1], {})
        with self.assertRaises(ValueError):
            self.mem.apply_update(ctx, '我看不懂', 'auto')

    def test_merge_replace_delete_and_undo(self):
        self.mem.remember_answer('a2', '问', '验收在三月底（文件 1：「第一阶段在三月底完成验收」）', {1: self.file})
        other = os.path.join(self.root, '新计划.txt')
        self.write('第一阶段推迟到五月完成验收。', other)
        self.remember(other)
        self.mem.remember_answer('a3', '问', '验收推迟到五月（文件 1：「推迟到五月完成验收」）', {1: other})
        self.mem.remember_answer('a4', '问', '另有一条（文件 1：「由财务部负责」）', {1: self.file})
        facts = {f['quote']: f['id'] for f in self.mem.listing()['facts']}
        before = self.mem.listing()
        payload, ctx = self.batch()
        f = lambda q: 'F%d' % facts[q]
        plan = {'conclusions': [{'id': f('第一阶段在三月底完成验收'), 'action': 'replace', 'into': f('推迟到五月完成验收')},
                                {'id': f('由财务部负责'), 'action': 'delete'},
                                {'id': f('预算总额为一百万元'), 'action': 'rewrite', 'conclusion': '不改', 'file': 1,
                                 'quote': '预算总额为一百万元'}]}
        run, counts, changes = self.mem.apply_update(ctx, reply(**plan), 'manual')
        # 没改过的文件不改写
        self.assertEqual(counts, {'replaced': 1, 'deleted': 1})
        self.assertIn('以新代旧：验收在三月底 → 验收推迟到五月', changes)
        self.assertEqual(set(f['quote'] for f in self.mem.listing()['facts']), {'预算总额为一百万元', '推迟到五月完成验收'})
        hist = self.mem.history()
        self.assertEqual([(h['id'], h['mode'], h['counts']) for h in hist], [(run, 'manual', counts)])
        self.assertFalse(self.mem.undo_update(run + 1))
        self.assertTrue(self.mem.undo_update(run))
        after = self.mem.listing()
        key = lambda r: sorted((x['id'], x['conclusion'], x['count']) for x in r['facts'])
        self.assertEqual(key(after), key(before))
        self.assertEqual(self.mem.history(), [])
        # 合并：次数相加
        payload, ctx = self.batch(full=True)
        self.mem.apply_update(ctx, reply(conclusions=[{'id': f('推迟到五月完成验收'), 'action': 'merge',
                                                       'into': f('预算总额为一百万元')}]), 'manual')
        merged = {x['quote']: x for x in self.mem.listing()['facts']}
        self.assertEqual(merged['预算总额为一百万元']['count'], 2)

    def test_changed_file_rewritten_or_dropped(self):
        self.mem.apply_update(self.batch()[1], reply(topics=[self.topic()]), 'manual')
        self.write(PLAN.replace('三月底', '五月'))
        self.recall('第一阶段')         # 取用时发现文件改了
        payload, ctx = self.batch()
        self.assertEqual(ctx['kind'], 'file')
        self.assertEqual([c['quote'] for c in payload['conclusions']], ['第一阶段在三月底完成验收'])
        self.assertTrue(all(c.get('changed') for c in payload['conclusions']))
        self.assertIn('第一阶段在五月完成验收', payload['texts']['1'])
        fid = payload['conclusions'][0]['id']
        run, counts, _ = self.mem.apply_update(ctx, reply(conclusions=[
            {'id': fid, 'action': 'rewrite', 'conclusion': '第一阶段五月验收', 'file': 1, 'quote': '第一阶段在五月完成验收'}]),
            'auto')
        # 改写了结论；主题页里原句不在了的要点没改写，删掉
        self.assertEqual(counts, {'rewritten': 1})
        self.assertIn('第一阶段五月验收', [f['conclusion'] for f in self.mem.listing()['facts']])
        self.assertEqual([len(t['points']) for t in self.mem.listing()['topics']], [1])
        self.assertEqual(self.mem.plan_update()[0][0], 'facts')     # 改写后的结论再整理进主题页
        self.assertTrue(self.mem.undo_update(run))
        self.assertEqual([len(t['points']) for t in self.mem.listing()['topics']], [2])
        # 模型改写不了：删除
        self.write(PLAN.replace('三月底', '六月'))
        self.recall('第一阶段')
        payload, ctx = self.batch()
        run, counts, _ = self.mem.apply_update(ctx, reply(), 'auto')
        self.assertEqual(counts, {'deleted': 1})

    def test_budget_and_history_limit(self):
        for i in range(30):
            self.mem.remember_answer('b%d' % i, '问', '预算%d（文件 1：「预算总额为一百万元」）' % i, {1: self.file})
        payload, ctx = self.mem.update_batch(('lint', list(self.ids.values())), 300)
        self.assertLessEqual(len(payload), 300)
        for i in range(am.MAX_UPDATES + 1):
            self.mem.forget_topic(0)
            page = dict(self.topic(), title='页%d' % i)
            self.mem.apply_update(self.batch(full=True)[1], reply(topics=[page]), 'auto')
        hist = self.mem.history()
        self.assertEqual(len(hist), am.MAX_UPDATES)
        self.assertFalse(self.mem.undo_update(hist[1]['id']))       # 只能从最近一次往回撤销
        for h in hist:
            self.assertTrue(self.mem.undo_update(h['id']))
        self.assertEqual([t['title'] for t in self.mem.listing()['topics']], ['页0'])

    def test_clear_and_forget_topic(self):
        self.mem.apply_update(self.batch()[1], reply(topics=[self.topic()]), 'manual')
        tid = self.mem.listing()['topics'][0]['id']
        self.assertTrue(self.mem.forget_topic(tid))
        self.assertEqual(self.mem.listing()['topics'], [])
        self.mem.apply_update(self.batch(full=True)[1], reply(topics=[self.topic()]), 'manual')
        self.mem.forget_file(self.file)
        self.assertEqual(self.mem.stats()['topics'], 0)        # 出处文件不在了，要点删光，主题页一并删
        self.remember()
        self.mem.remember_answer('a1', '问', ANSWER, {1: self.file})
        self.mem.clear()
        self.assertEqual((self.mem.pending(), self.mem.history()), (0, []))


class IngestTest(MemoryCase):
    """自动记入：记入段落、排进「从文件抽结论」，分批抽结论并核对原句；容量不足、忘掉、删除。"""

    def ingest(self, path=None):
        path = path or self.file
        return self.mem.ingest_file(path, os.stat(path), read(path))

    def extract_all(self, budget, answer):
        """把「从文件抽结论」一批批做完，answer(payload) 给出每批的结论；返回各批的 payload。"""
        sent = []
        for unit in self.mem.plan_update():
            if unit[0] != 'extract':
                continue
            payload, ctx = self.mem.update_batch(unit, budget)
            reply = None
            if payload is not None:
                sent.append(json.loads(payload))
                reply = json.dumps({'conclusions': answer(sent[-1])}, ensure_ascii=False)
            self.mem.apply_update(ctx, reply, 'auto')
        return sent

    def test_ingest_and_extract(self):
        self.assertEqual(self.ingest(), 'done')
        st = os.stat(self.file)
        self.assertEqual(self.mem.ingest_rows()[self.file], (st.st_size, st.st_mtime_ns, 'done', ''))
        self.assertEqual(self.mem.stats()['extract'], 1)
        self.assertEqual(self.recall('预算')[0]['paras'], ['预算总额为一百万元，由财务部负责。'])     # 马上能参考
        self.assertEqual(self.ingest(), 'done')     # 没变：不重复排队
        self.assertEqual(self.mem.pending(), 1)
        sent = self.extract_all(5000, lambda p: [
            {'conclusion': '第一阶段三月底验收', 'quote': '第一阶段在三月底完成验收'},
            {'conclusion': '编造', 'quote': '原文里没有这句'},
            {'conclusion': '', 'quote': '预算总额为一百万元'},
            {'conclusion': '第一阶段三月底验收', 'quote': '第一阶段在三月底完成验收'}])
        self.assertEqual(len(sent), 1)
        self.assertIn('预算总额为一百万元', sent[0]['text'])
        facts = self.mem.listing()['facts']
        self.assertEqual([(f['conclusion'], f['quote']) for f in facts], [('第一阶段三月底验收', '第一阶段在三月底完成验收')])
        h = self.mem.history()[0]
        self.assertEqual((h['counts'], h['changes']), ({'extracted': 1}, ['从文件抽出：第一阶段三月底验收']))
        self.assertEqual(am.summary(h['counts']), '从文件抽出结论 1 条')
        # 抽出的结论排进队列，接着整理进主题页
        self.assertEqual([u[0] for u in self.mem.plan_update()], ['facts'])
        self.assertTrue(self.mem.undo_update(h['id']))
        self.assertEqual(self.mem.stats()['facts'], 0)

    def test_extract_in_batches(self):
        text = '\n\n'.join('第%d条规定的内容写在这里。' % i for i in range(60))
        self.write(text)
        self.ingest()
        units = self.mem.plan_update()
        self.assertEqual(units, [('extract', self.file)])
        sent = []
        for _ in range(100):      # 估计的批数不够时（每批字数比估计的少），下次接着做
            sent += self.extract_all(300, lambda p: [{'conclusion': '有规定', 'quote': p['text'].split('\n\n')[0]}])
            if not self.mem.stats()['extract']:
                break
        self.assertGreater(len(sent), 1)
        self.assertTrue(all(len(json.dumps(p, ensure_ascii=False)) <= 300 for p in sent))
        joined = '\n\n'.join(p['text'] for p in sent)
        self.assertEqual(joined.split('\n\n'), text.split('\n\n'))     # 每段恰好一次
        self.assertEqual(self.mem.stats()['facts'], len(sent))

    def test_changed_file_extracts_again(self):
        self.ingest()
        self.extract_all(5000, lambda p: [])
        self.assertEqual(self.mem.pending(), 0)
        self.write(PLAN + '\n\n新增一段。')
        self.ingest()
        self.assertEqual(self.mem.stats()['extract'], 1)
        sent = self.extract_all(5000, lambda p: [])
        self.assertIn('项目计划', sent[0]['text'])      # 从头再抽

    def test_failed_evicted_forgotten_and_gone(self):
        st = os.stat(self.file)
        self.mem.ingest_failed(self.file, st, '没有取出文字')
        self.assertEqual(self.mem.ingest_rows()[self.file][2:], ('failed', '没有取出文字'))
        cands = [(self.file, st.st_size, st.st_mtime_ns)]
        self.assertEqual(self.mem.ingest_summary(cands)['skipped'], [{'path': self.file, 'state': 'failed',
                                                                       'reason': '没有取出文字'}])
        other = os.path.join(self.root, '大文件.txt')
        self.write('大' * 300, other)
        self.remember()     # 提问时选过的文件，最近用过
        self.mem.max_bytes = 900
        self.assertEqual(self.ingest(other), 'evicted')     # 自动记入的按从没用过算，先被忘掉
        self.assertEqual(self.mem.ingest_rows()[other][2:], ('evicted', am.EVICTED))
        self.assertEqual(self.mem.stats()['files'], 1)
        self.mem.set_max_bytes(10 ** 6)        # 调大上限：可以重新记入
        self.assertNotIn(other, self.mem.ingest_rows())
        self.assertEqual(self.ingest(other), 'done')
        self.assertTrue(self.mem.forget_file(other))       # 使用者忘掉的：改过之前不再记入
        self.assertEqual(self.mem.ingest_rows()[other][2:], ('forgotten', am.FORGOTTEN))
        self.assertEqual(self.mem.stats()['extract'], 0)
        self.ingest()
        os.remove(self.file)
        self.assertEqual(self.mem.ingest_gone([self.file]), 1)
        self.assertNotIn(self.file, self.mem.ingest_rows())
        self.assertEqual(self.mem.stats()['files'], 0)
        self.mem.clear()
        self.assertEqual(self.mem.ingest_rows(), {})

    def test_summary(self):
        other = os.path.join(self.root, '新.txt')
        self.write('新的内容。', other)
        self.ingest()
        a, b = os.stat(self.file), os.stat(other)
        got = self.mem.ingest_summary([(self.file, a.st_size, a.st_mtime_ns), (other, b.st_size, b.st_mtime_ns)])
        self.assertEqual(got, {'total': 2, 'done': 1, 'waiting': 1, 'skipped': [], 'skipped_total': 0})


class RelatedAndExportTest(MemoryCase):
    def setUp(self):
        super().setUp()
        self.remember()
        self.mem.remember_answer('a1', '问', ANSWER, {1: self.file})

    def update(self, topics):
        payload, ctx = self.mem.update_batch(self.mem.plan_update()[0], 10000)
        return self.mem.apply_update(ctx, json.dumps({'topics': topics}, ensure_ascii=False), 'manual')

    def pages(self):
        return {t['title']: [r['title'] for r in t['related']] for t in self.mem.listing()['topics']}

    def test_related_only_existing(self):
        pt = lambda text, quote: {'text': text, 'file': 1, 'quote': quote}
        self.update([{'id': 'new', 'title': '验收', 'points': [pt('三月底验收', '第一阶段在三月底完成验收')],
                      'related': ['预算', '不存在的主题', '验收']},
                     {'id': 'new', 'title': '预算', 'points': [pt('一百万元', '预算总额为一百万元')], 'related': ['验收']}])
        self.assertEqual(self.pages(), {'验收': ['预算'], '预算': ['验收']})
        payload, _ = self.mem.update_batch(('lint', [r['id'] for r in self.mem.listing()['facts']]), 10000)
        sent = json.loads(payload)
        self.assertEqual({t['title']: t['related'] for t in sent['topics']}, {'验收': ['预算'], '预算': ['验收']})
        # 只改相关主题也算一次更新，可撤销
        vid = [t['id'] for t in self.mem.listing()['topics'] if t['title'] == '验收'][0]
        _, ctx = self.mem.update_batch(('lint', [r['id'] for r in self.mem.listing()['facts']]), 10000)
        run, counts, changes = self.mem.apply_update(ctx, json.dumps({'topics': [
            {'id': 'T%d' % vid, 'title': '验收', 'points': [pt('三月底验收', '第一阶段在三月底完成验收')], 'related': []}]},
            ensure_ascii=False), 'manual')
        self.assertEqual((counts, changes), ({'topics_updated': 1}, ['更新主题页（相关主题）：验收']))
        self.assertEqual(self.pages()['验收'], [])
        self.assertTrue(self.mem.undo_update(run))
        self.assertEqual(self.pages()['验收'], ['预算'])
        self.mem.forget_topic([t['id'] for t in self.mem.listing()['topics'] if t['title'] == '预算'][0])
        self.assertEqual(self.pages(), {'验收': []})       # 删掉的主题不再列为相关

    def test_md_name(self):
        used = set()
        self.assertEqual(am.md_name('a/b:c*?', used), 'a b c')
        self.assertEqual(am.md_name('A B C', used), 'A B C (2)')
        self.assertEqual(am.md_name(' .. ', used), '未命名')
        self.assertEqual(am.md_name('[[链接]]#^|', used), '链接')
        self.assertLessEqual(len(am.md_name('长' * 200, used)), 80)

    def test_export_markdown(self):
        import io
        import zipfile
        empty = am.Memory(os.path.join(self.root, 'other', 'm.sqlite'))
        self.assertIsNone(empty.export_markdown())
        empty.close()
        pt = {'text': '三月底验收', 'file': 1, 'quote': '第一阶段在三月底完成验收'}
        self.update([{'id': 'new', 'title': '验收/安排', 'points': [pt], 'related': []}])
        z = zipfile.ZipFile(io.BytesIO(self.mem.export_markdown()))
        self.assertEqual(sorted(z.namelist()), ['主题/验收 安排.md', '文件/计划.txt.md', '索引.md'])
        topic = z.read('主题/验收 安排.md').decode('utf-8')
        self.assertIn('- 三月底验收（出处：[[文件/计划.txt|计划.txt]]）\n  > 第一阶段在三月底完成验收', topic)
        page = z.read('文件/计划.txt.md').decode('utf-8')
        self.assertIn('原路径：`%s`' % self.file, page)
        self.assertIn('- 预算一百万元\n  > 预算总额为一百万元', page)
        self.assertIn('[[主题/验收 安排|验收 安排]]', page)
        index = z.read('索引.md').decode('utf-8')
        self.assertIn('主题页 1 页、结论 2 条、文件 1 个。', index)
        self.assertNotIn('由财务部负责', topic + page + index)     # 不含段落全文


class UpgradeTest(MemoryCase):
    def test_schema_1_kept(self):
        os.makedirs(os.path.dirname(self.mem.path))
        db = sqlite3.connect(self.mem.path)
        am.Memory._create(db)
        db.close()
        self.assertTrue(self.remember())
        self.mem.remember_answer('a1', '问', ANSWER, {1: self.file})
        self.mem.close()
        # 降回版本 1 的库：去掉新表与新列，再打开
        db = sqlite3.connect(self.mem.path)
        db.executescript('DROP TABLE points_fts; DROP TABLE points; DROP TABLE topics; DROP TABLE queue; '
                         'DROP TABLE updates; DROP TABLE ingest; ALTER TABLE facts DROP COLUMN stale; '
                         'PRAGMA user_version = 1;')
        db.close()
        self.assertEqual(self.mem.stats()['facts'], 2)
        self.assertEqual(self.recall('预算')[0]['facts'][0]['quote'], '预算总额为一百万元')
        self.assertEqual([n for n in os.listdir(os.path.dirname(self.mem.path)) if '.broken-' in n], [])
        with sqlite3.connect(self.mem.path) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], am.SCHEMA)


class UpgradeFrom2Test(MemoryCase):
    def test_schema_2_kept(self):
        self.remember()
        self.mem.remember_answer('a1', '问', ANSWER, {1: self.file})
        self.mem.close()
        db = sqlite3.connect(self.mem.path)
        db.executescript('DROP TABLE ingest; ALTER TABLE topics DROP COLUMN related; PRAGMA user_version = 2;')
        db.execute("INSERT INTO updates (time, mode, counts, changes, undo) VALUES (1, 'manual', '{}', '[]', ?)",
                   (json.dumps([[{'t': 'topic', 'id': 7, 'row': [7, '旧页', 1.0]}]]),))
        db.commit()
        db.close()
        self.assertEqual(self.mem.stats()['facts'], 2)
        self.assertEqual(self.mem.ingest_rows(), {})
        self.assertTrue(self.mem.undo_update(self.mem.history()[0]['id']))     # 升级前的撤销记录照样能用
        with sqlite3.connect(self.mem.path) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], am.SCHEMA)


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
