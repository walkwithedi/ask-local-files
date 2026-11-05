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
        self.assertEqual(self.mem.remember_answer('a3', '问', ANSWER, {1: '/没有收录的文件'}), 0)

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
        self.assertEqual(self.mem.stats(), {'files': 0, 'facts': 0, 'topics': 0, 'points': 0, 'queued': 0, 'extract': 0,
                                            'reextract': 0, 'xfailed': 0, 'bytes': 0, 'max_bytes': am.MAX_BYTES})

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
        self.assertEqual(am.summary(counts), '新建主题 1 个')
        self.assertEqual(changes, ['新建主题：项目安排'])
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


class BatchTest(MemoryCase):
    """按文件分批抽结论：按文件夹分组、从旧到新、字数与文件数上限、标题列表只追加、重新整理全部、失败的文件。"""

    def add(self, rel, text, mtime):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
        os.utime(path, (mtime, mtime))
        self.mem.ingest_file(path, os.stat(path), text)
        return path

    def test_grouped_by_folder_old_to_new(self):
        b2 = self.add('乙/新.txt', '乙的新文件内容。', 3000)
        a1 = self.add('甲/旧.txt', '甲的旧文件内容。', 1000)
        b1 = self.add('乙/旧.txt', '乙的旧文件内容。', 2000)
        a2 = self.add('甲/新.txt', '甲的新文件内容。', 4000)
        units = [u for u in self.mem.plan_update() if u[0] == 'xbatch']
        self.assertEqual(units, [('xbatch', [a1, a2]), ('xbatch', [b1, b2])])
        summary = self.mem.plan_summary(units)
        self.assertEqual([(os.path.basename(g['folder']), g['files'], g['batches']) for g in summary],
                         [('甲', 2, 1), ('乙', 2, 1)])

    def test_limits(self):
        paths = [self.add('甲/%02d.txt' % i, '第%d份内容写在这里。' % i, 1000 + i) for i in range(5)]
        units = self.mem.plan_update(files=2)
        self.assertEqual([u[1] for u in units if u[0] == 'xbatch'], [paths[0:2], paths[2:4], paths[4:5]])
        big = self.add('甲/大.txt', '\n\n'.join('第%d条规定写在这里。' % i for i in range(1000)), 999)
        units = self.mem.plan_update(chars=5000)
        self.assertIn(('extract', big), units)
        self.assertTrue(all(big not in u[1] for u in units if u[0] == 'xbatch'))
        for u in units:
            if u[0] == 'xbatch':
                payload, ctx = self.mem.update_batch(u, 10 ** 6, chars=5000)
                self.assertLessEqual(len(payload), 5000)

    def test_batch_reply_by_file_and_known(self):
        a = self.add('甲/一.txt', '张三负责接口联调。', 1000)
        b = self.add('甲/二.txt', '验收推迟到四月底。', 2000)
        payload, ctx = self.mem.update_batch(('xbatch', [a, b]), 10 ** 6)
        sent = json.loads(payload)
        self.assertEqual([f['file'] for f in sent['files']], [1, 2])
        reply = json.dumps({'conclusions': [{'file': 2, 'conclusion': '验收推迟到四月底', 'quote': '验收推迟到四月底'},
                                            {'file': 1, 'conclusion': '错放的', 'quote': '验收推迟到四月底'},
                                            {'file': 1, 'conclusion': '张三负责联调', 'quote': '张三负责接口联调'}]},
                           ensure_ascii=False)
        self.mem.apply_update(ctx, reply, 'auto')
        facts = {(f['path'], f['conclusion']) for f in self.mem.listing()['facts']}
        self.assertEqual(facts, {(b, '验收推迟到四月底'), (a, '张三负责联调')})     # 摘句不在所注文件里的不写
        self.assertEqual(len(ctx['new_facts']), 2)
        self.assertEqual(self.mem.stats()['extract'], 0)
        # 大文件第 2 批起附前面已得出的结论
        big = self.add('甲/大.txt', '\n\n'.join('第%d条规定写在这里。' % i for i in range(60)), 999)
        payload, ctx = self.mem.update_batch(('extract', big), 300, chars=300)
        first = json.loads(payload)['text'].split('\n\n')[0]
        self.mem.apply_update(ctx, json.dumps({'conclusions': [{'conclusion': '有第一条', 'quote': first}]},
                                              ensure_ascii=False), 'auto')
        payload, ctx = self.mem.update_batch(('extract', big), 300, chars=300)
        self.assertEqual(json.loads(payload)['known'], ['有第一条'])

    def test_title_only_topic_gets_points_appended(self):
        a = self.add('甲/一.txt', '张三负责接口联调。\n\n验收推迟到四月底。', 1000)
        self.mem.apply_update(self.mem.update_batch(('xbatch', [a]), 10 ** 6)[1], json.dumps({'conclusions': [
            {'file': 1, 'conclusion': '张三负责联调', 'quote': '张三负责接口联调'},
            {'file': 1, 'conclusion': '验收推迟', 'quote': '验收推迟到四月底'}]}, ensure_ascii=False), 'auto')
        ids = {f['conclusion']: f['id'] for f in self.mem.listing()['facts']}
        payload, ctx = self.mem.update_batch(('facts', [ids['张三负责联调']]), 10 ** 6)
        self.mem.apply_update(ctx, json.dumps({'topics': [{'id': 'new', 'title': '项目安排', 'points': [
            {'text': '张三负责联调', 'file': 1, 'quote': '张三负责接口联调'}]}]}, ensure_ascii=False), 'auto')
        # 下一批只给了标题：照抄标题只追加要点，原有要点不动
        payload, ctx = self.mem.update_batch(('facts', [ids['验收推迟']]), 10 ** 6)
        self.assertEqual(json.loads(payload)['topics'], [])
        self.assertEqual(json.loads(payload)['other_topics'], ['项目安排'])
        self.mem.apply_update(ctx, json.dumps({'topics': [{'id': 'new', 'title': '项目安排', 'points': [
            {'text': '验收推迟', 'file': 1, 'quote': '验收推迟到四月底'}]}]}, ensure_ascii=False), 'auto')
        topics = self.mem.listing()['topics']
        self.assertEqual(len(topics), 1)
        self.assertEqual(sorted(p['text'] for p in topics[0]['points']), ['张三负责联调', '验收推迟'])

    def test_lint_groups_only_touched(self):
        a = self.add('甲/一.txt', '张三负责接口联调。', 1000)
        db = self.mem._conn()
        for title in ('接口联调安排', '接口联调进度', '预算'):
            tid = db.execute('INSERT INTO topics (title, updated) VALUES (?, 0)', (title,)).lastrowid
            self.mem._put_point(db, [None, tid, a, title, '张三负责接口联调', am.norm('张三负责接口联调'), '', 0])
        db.commit()
        t = {r[1]: r[0] for r in db.execute('SELECT id, title FROM topics')}
        groups = self.mem.lint_groups([t['接口联调安排']])
        self.assertEqual(groups, [('tlint', [t['接口联调安排'], t['接口联调进度']])])
        payload, ctx = self.mem.update_batch(groups[0], 10 ** 6)
        self.assertEqual(sorted(x['title'] for x in json.loads(payload)['topics']), ['接口联调安排', '接口联调进度'])
        self.assertEqual(self.mem.lint_groups([t['预算']]), [])       # 没有相近的不必体检

    def test_reorganize_all_and_retry(self):
        a = self.add('甲/一.txt', '张三负责接口联调。', 1000)
        b = self.add('甲/二.txt', '验收推迟到四月底。', 2000)
        unit = self.mem.plan_update()[0]
        self.mem.apply_update(self.mem.update_batch(unit, 10 ** 6)[1], json.dumps({'conclusions': [
            {'file': 1, 'conclusion': '张三负责联调', 'quote': '张三负责接口联调'}]}, ensure_ascii=False), 'auto')
        self.mem.mark_xfail([b])
        st = self.mem.stats()
        self.assertEqual((st['xfailed'], st['extract']), (1, 0))
        self.assertEqual(self.mem.xfail_list(), [b])
        counts = self.mem.reorganize_counts()
        self.assertEqual((counts['facts'], counts['files'], counts['batches']), (1, 2, 1))
        self.assertEqual(self.mem.reorganize_all(), 2)
        st = self.mem.stats()
        self.assertEqual((st['facts'], st['topics'], st['xfailed'], st['extract'], st['files']), (0, 0, 0, 2, 2))
        self.assertEqual(self.mem.history(), [])
        self.mem.mark_xfail([a])
        self.assertEqual(self.mem.retry_xfail(), 1)
        self.assertEqual(self.mem.stats()['extract'], 2)

    def test_changed_file_drops_stale_without_review(self):
        a = self.add('甲/一.txt', '张三负责接口联调。\n\n验收推迟到四月底。', 1000)
        self.mem.apply_update(self.mem.update_batch(('xbatch', [a]), 10 ** 6)[1], json.dumps({'conclusions': [
            {'file': 1, 'conclusion': '张三负责联调', 'quote': '张三负责接口联调'},
            {'file': 1, 'conclusion': '验收推迟', 'quote': '验收推迟到四月底'}]}, ensure_ascii=False), 'auto')
        self.add('甲/一.txt', '张三负责接口联调。\n\n验收改到五月。', 2000)
        facts = [f['conclusion'] for f in self.mem.listing()['facts']]
        self.assertEqual(facts, ['张三负责联调'])          # 原句不在了的直接删除
        kinds = {u[0] for u in self.mem.plan_update()}
        self.assertNotIn('file', kinds)                    # 不逐条复核
        self.assertIn('xbatch', kinds)


class IngestTest(MemoryCase):
    """自动收录：收录段落、排进「从文件抽结论」，分批抽结论并核对原句；容量不足、移出、删除。"""

    def ingest(self, path=None):
        path = path or self.file
        return self.mem.ingest_file(path, os.stat(path), read(path))

    def extract_all(self, budget, answer):
        """把「从文件抽结论」一批批做完（每批至多 budget 字），answer(payload) 给出每份文件的结论；返回各份的 payload
        （一批多份文件时每份一个 {path, text}）。"""
        sent = []
        for unit in self.mem.plan_update(chars=budget):
            if unit[0] not in ('extract', 'xbatch'):
                continue
            payload, ctx = self.mem.update_batch(unit, budget, chars=budget)
            reply = None
            if payload is not None:
                p = json.loads(payload)
                found = []
                for f in p.get('files') or [dict(p, file=1)]:
                    sent.append(f if 'files' in p else p)
                    found += [dict(c, file=f['file']) for c in answer(sent[-1])]
                reply = json.dumps({'conclusions': found}, ensure_ascii=False)
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
        units = self.mem.plan_update(chars=300)
        self.assertEqual(units[0], ('extract', self.file))
        self.assertEqual(self.mem.plan_update(), [('xbatch', [self.file])])     # 不超过每批字数时与别的文件拼成一批
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
        # 第 1 批不带文件开头，之后各批带，是文件开头的前缀，且不超过限度
        self.assertNotIn('head', sent[0])
        for p in sent[1:]:
            self.assertTrue(p['head'])
            self.assertTrue(text.startswith(p['head']))
            self.assertLessEqual(len(p['head']), min(am.EXTRACT_HEAD, 300 // 5))

    def test_prompt_examples(self):
        # 提示词里的示例：每对「输入：」「输出：」都是合法 JSON，输出的每条摘句都能在对应输入的 text 里核对通过
        lines = am.EXTRACT_PROMPT.splitlines()
        ins = [json.loads(x[3:]) for x in lines if x.startswith('输入：')]
        outs = [json.loads(x[3:]) for x in lines if x.startswith('输出：')]
        self.assertEqual(len(ins), 3)
        self.assertEqual(len(ins), len(outs))
        for i, o in zip(ins, outs):
            flat = am.norm(i['text'])
            for c in o['conclusions']:
                self.assertTrue(am.quote_in(c['quote'], flat), c['quote'])
                self.assertLessEqual(len(c['quote']), 80)
        self.assertEqual(outs[-1], {'conclusions': []})
        self.assertIn('head', ins[0])

    def test_batch_prompt_examples(self):
        # 多文件抽结论提示词的示例：输入、输出都是合法 JSON，每条摘句都能在所指文件的 text 里核对通过
        lines = am.EXTRACT_BATCH_PROMPT.splitlines()
        ins = [json.loads(x[3:]) for x in lines if x.startswith('输入：')]
        outs = [json.loads(x[3:]) for x in lines if x.startswith('输出：')]
        self.assertEqual(len(ins), 2)
        self.assertEqual(len(ins), len(outs))
        for i, o in zip(ins, outs):
            texts = {f['file']: am.norm(f['text']) for f in i['files']}
            for c in o['conclusions']:
                self.assertTrue(am.quote_in(c['quote'], texts[c['file']]), c['quote'])
                self.assertLessEqual(len(c['quote']), 80)
        self.assertEqual(outs[-1], {'conclusions': []})

    def test_prompt_output_keys(self):
        # 提示词约定的输出键与 apply_update、抽结论读取的键一致
        for key in ('"conclusions"', '"topics"', '"delete_topics"', '"action"', '"into"', '"related"', '"points"'):
            self.assertIn(key, am.UPDATE_PROMPT)
        for key in ('"conclusion"', '"quote"'):
            self.assertIn(key, am.EXTRACT_PROMPT)
            self.assertIn(key, am.EXTRACT_BATCH_PROMPT)
        self.assertIn('"file"', am.EXTRACT_BATCH_PROMPT)

    def test_parse_plan_skips_think(self):
        # 思考段里的花括号不影响解析
        reply = '<think>先看 {"topics": ["T9"]} 是否合适……</think>\n```json\n{"topics": ["T3"]}\n```'
        self.assertEqual(am.parse_plan(reply), {'topics': ['T3']})

    def test_update_prompt_language_rule(self):
        self.assertIn('主题页的标题和要点用其中结论的语言写；一页里语言不一时用多数结论的语言。', am.UPDATE_PROMPT)
        self.assertTrue(am.UPDATE_PROMPT.startswith('你在维护使用者本机的一份「知识库」'))

    def test_reextract_with_new_rules(self):
        self.ingest()
        self.extract_all(5000, lambda p: [
            {'conclusion': '第一阶段三月底验收', 'quote': '第一阶段在三月底完成验收'},
            {'conclusion': '预算一百万', 'quote': '预算总额为一百万元'},
            {'conclusion': '有项目计划', 'quote': '项目计划'},
            {'conclusion': '财务部负责', 'quote': '由财务部负责'}])
        self.assertEqual(self.mem.stats()['facts'], 4)
        # 问答得来的结论
        self.assertEqual(self.mem.remember_answer('a9', '谁管预算', '财务部管预算（文件 1：「一百万元，由财务部」）。',
                                                  {1: self.file}), 1)
        db = self.mem._conn()
        db.execute("UPDATE facts SET count = 3 WHERE qkey = ?", (am.norm('第一阶段在三月底完成验收'),))
        db.execute("UPDATE facts SET answers = '[\"a1\"]' WHERE qkey = ?", (am.norm('项目计划'),))
        db.execute('INSERT INTO rejected (path, qkey, ckey) VALUES (?, ?, ?)',
                   (self.file, am.norm('预算总额为一百万元'), am.norm('新预算结论')))
        db.execute('UPDATE ingest SET ver = 1')
        db.commit()
        before = {f['quote']: f for f in self.mem.listing()['facts']}
        self.assertEqual(self.mem.stats()['reextract'], 0)      # 规则版本旧的不再自动重抽
        self.assertEqual(self.mem.requeue_reextract(), 0)
        db.execute('UPDATE ingest SET done_n = 0')               # 重试或重新整理时从头再抽
        self.mem._enqueue(db, 'extract', self.file)
        db.commit()
        self.assertEqual(self.mem.stats()['extract'], 1)
        sent = self.extract_all(5000, lambda p: [
            {'conclusion': '第一阶段于三月底完成验收（新写法）', 'quote': '第一阶段在三月底完成验收'},
            {'conclusion': '新预算结论', 'quote': '预算总额为一百万元'}])
        self.assertEqual(len(sent), 1)
        after = {f['quote']: f for f in self.mem.listing()['facts']}
        a = after['第一阶段在三月底完成验收']
        self.assertEqual(a['conclusion'], '第一阶段于三月底完成验收（新写法）')       # 同一原句：改写
        self.assertEqual(a['count'], 3)
        self.assertEqual(a['id'], before['第一阶段在三月底完成验收']['id'])
        self.assertEqual(after['预算总额为一百万元']['conclusion'], '预算一百万')    # 「不对」过的组合不写入，保留旧的
        self.assertEqual(after['项目计划']['conclusion'], '有项目计划')               # 被回答引用过：不动
        self.assertEqual(after['一百万元，由财务部']['conclusion'], '财务部管预算')    # 问答得来：不动
        self.assertNotIn('由财务部负责', after)                                     # 没再抽出：删除
        h = self.mem.history()[0]
        self.assertEqual(h['counts'], {'rewritten': 1, 'deleted': 1})
        self.assertIn('按新规则改写：第一阶段于三月底完成验收（新写法）', h['changes'])
        self.assertIn('重抽后删除：财务部负责', h['changes'])
        self.assertEqual(self.mem._conn().execute('SELECT ver FROM ingest').fetchone()[0], am.EXTRACT_VERSION)
        self.assertEqual(self.mem.stats()['reextract'], 0)
        self.assertTrue(self.mem.undo_update(h['id']))
        undone = {f['quote']: (f['conclusion'], f['count']) for f in self.mem.listing()['facts']}
        self.assertEqual(undone, {q: (f['conclusion'], f['count']) for q, f in before.items()})

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
        self.assertEqual(self.ingest(other), 'evicted')     # 自动收录的按从没用过算，先被移出
        self.assertEqual(self.mem.ingest_rows()[other][2:], ('evicted', am.EVICTED))
        self.assertEqual(self.mem.stats()['files'], 1)
        self.mem.set_max_bytes(10 ** 6)        # 调大上限：可以重新收录
        self.assertNotIn(other, self.mem.ingest_rows())
        self.assertEqual(self.ingest(other), 'done')
        self.assertTrue(self.mem.forget_file(other))       # 使用者移出的：改过之前不再收录
        self.assertEqual(self.mem.ingest_rows()[other][2:], ('forgotten', am.FORGOTTEN))
        self.assertEqual(self.mem.stats()['extract'], 0)
        self.ingest()
        os.remove(self.file)
        self.assertEqual(self.mem.ingest_gone([self.file]), 1)
        self.assertNotIn(self.file, self.mem.ingest_rows())
        self.assertEqual(self.mem.stats()['files'], 0)
        self.mem.clear()
        self.assertEqual(self.mem.ingest_rows(), {})

    def queue(self):
        return sorted(self.mem._conn().execute("SELECT kind, ref FROM queue WHERE kind IN ('extract', 'file')").fetchall())

    def test_park_and_resume(self):
        inner = os.path.join(self.root, '甲')
        os.makedirs(inner)
        other = os.path.join(inner, '长文件.txt')
        self.write('\n\n'.join('第%d条规定的内容写在这里。' % i for i in range(40)), other)
        self.ingest()
        self.ingest(other)
        # 计划.txt 抽完；长文件抽了一批
        unit = ('extract', self.file)
        payload, ctx = self.mem.update_batch(unit, 5000)
        self.mem.apply_update(ctx, json.dumps({'conclusions': [
            {'conclusion': '第一阶段三月底验收', 'quote': '第一阶段在三月底完成验收'}]}, ensure_ascii=False), 'auto')
        payload, ctx = self.mem.update_batch(('extract', other), 300)
        self.mem.apply_update(ctx, json.dumps({'conclusions': []}), 'auto')
        db = self.mem._conn()
        done_n = db.execute('SELECT done_n FROM ingest WHERE path = ?', (other,)).fetchone()[0]
        self.assertGreater(done_n, 0)
        # 计划.txt 的结论待复核
        db.execute('UPDATE facts SET stale = 1 WHERE path = ?', (self.file,))
        self.mem._enqueue(db, 'file', self.file)
        db.commit()
        self.assertEqual(self.queue(), [('extract', other), ('file', self.file)])
        self.assertEqual(self.mem.queued_under(inner), 1)
        self.assertEqual(self.mem.queued_under(self.root), 2)
        # 只勾选「甲」：计划.txt 撤出复核，长文件不动
        self.assertEqual(self.mem.park_outside([inner]), 1)
        self.assertEqual(self.queue(), [('extract', other)])
        st = self.mem.stats([inner])
        self.assertEqual((st['files'], st['facts'], st['outside']), (2, 1, 1))
        # 什么都不勾选：全部撤出，段落、结论与进度保留
        self.assertEqual(self.mem.park_outside([]), 1)
        self.assertEqual(self.queue(), [])
        self.assertEqual(self.mem.queued_under(self.root), 0)
        self.assertEqual(db.execute('SELECT done_n FROM ingest WHERE path = ?', (other,)).fetchone()[0], done_n)
        self.assertEqual((self.mem.stats()['files'], self.mem.stats()['facts']), (2, 1))
        self.assertEqual(self.mem.stats([])['outside'], 2)
        # 重新勾选「甲」：长文件接着抽，计划.txt 不在里面不排
        self.assertEqual(self.mem.resume_inside([inner]), 1)
        self.assertEqual(self.mem.resume_inside([inner]), 0)      # 已在队里，不重复排
        self.assertEqual(self.queue(), [('extract', other)])
        payload, ctx = self.mem.update_batch(('extract', other), 300)
        self.assertNotIn('第0条', json.loads(payload)['text'])       # 接着上次的段落，不从头抽
        # 勾选上级：计划.txt 有待复核的排进复核；它已抽完，不重抽
        self.assertEqual(self.mem.resume_inside([self.root]), 1)
        self.assertEqual(self.queue(), [('extract', other), ('file', self.file)])

    def test_reextract_only_inside(self):
        self.ingest()
        self.extract_all(5000, lambda p: [])
        db = self.mem._conn()
        db.execute('UPDATE ingest SET ver = 1')
        db.commit()
        elsewhere = os.path.join(self.root, '别处')
        self.assertEqual(self.mem.stats([elsewhere])['reextract'], 0)
        self.assertEqual(self.mem.stats([self.root])['reextract'], 0)      # 规则改了不再自动重抽
        self.assertEqual(self.mem.requeue_reextract([self.root]), 0)
        self.assertEqual(self.mem.resume_inside([elsewhere]), 0)
        self.mem.close()
        self.assertEqual(self.mem.stats()['extract'], 0)

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
        self.assertEqual((counts, changes), ({'topics_updated': 1}, ['更新主题（相关主题）：验收']))
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


class UpgradeFrom3Test(MemoryCase):
    def test_schema_3_requeues_extracted_files(self):
        self.mem.ingest_file(self.file, os.stat(self.file), read(self.file))
        for unit in self.mem.plan_update():
            payload, ctx = self.mem.update_batch(unit, 5000)
            self.mem.apply_update(ctx, json.dumps({'conclusions': [
                {'conclusion': '第一阶段三月底验收', 'quote': '第一阶段在三月底完成验收'}]}, ensure_ascii=False), 'auto')
        self.assertEqual(self.mem.stats()['extract'], 0)
        self.mem.close()
        db = sqlite3.connect(self.mem.path)
        db.executescript('ALTER TABLE ingest DROP COLUMN ver; PRAGMA user_version = 3;')
        db.close()
        st = self.mem.stats()
        self.assertEqual((st['facts'], st['reextract'], st['extract']), (1, 0, 0))
        self.assertEqual(self.mem.requeue_reextract([self.root]), 0)    # 不再自动重抽
        db = self.mem._conn()
        self.assertEqual(db.execute('SELECT ver FROM ingest').fetchone(), (1,))
        self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], am.SCHEMA)


class LocateAndMarksTest(MemoryCase):
    """v2.21：回答引用的跳转目标、文件列表的收录标记、分页与定位用的列表。"""
    batch, topic = UpdateTest.batch, UpdateTest.topic

    def setUp(self):
        super().setUp()
        self.remember()
        self.mem.remember_answer('a1', '问', ANSWER, {1: self.file})
        self.ids = {f['quote']: f['id'] for f in self.mem.listing()['facts']}

    def test_locate_fact_ok_merged_gone(self):
        fid = self.ids['预算总额为一百万元']
        self.assertEqual(self.mem.locate('fact', fid, self.file, '预算总额为一百万元'),
                         {'state': 'ok', 'kind': 'fact', 'id': fid, 'text': '预算一百万元'})
        # id 被别的结论占用时按路径与原句找
        self.assertEqual(self.mem.locate('fact', 99999, self.file, '预算总额为一百万元')['id'], fid)
        # 整理时合并：跳到合并后的那条；记录跟着链条走
        other = self.ids['第一阶段在三月底完成验收']
        payload, ctx = self.batch()
        self.mem.apply_update(ctx, reply(conclusions=[{'id': 'F%d' % other, 'action': 'merge', 'into': 'F%d' % fid}]),
                              'manual')
        got = self.mem.locate('fact', other, self.file, '第一阶段在三月底完成验收')
        self.assertEqual((got['state'], got['id'], got['text']), ('merged', fid, '预算一百万元'))
        # 合并后的那条再被移出：判为已移出
        self.mem.forget_fact(fid)
        self.assertEqual(self.mem.locate('fact', other, self.file, '第一阶段在三月底完成验收')['state'], 'gone')
        self.mem.clear()
        self.assertEqual(self.mem._conn().execute('SELECT COUNT(*) FROM redirects').fetchone()[0], 0)

    def test_locate_topic(self):
        self.mem.apply_update(self.batch()[1], reply(topics=[self.topic()]), 'manual')
        tid = self.mem.listing()['topics'][0]['id']
        self.assertEqual(self.mem.locate('topic', tid, title='项目安排')['state'], 'ok')
        # 主题被删、同一原句的要点归到别的主题：判为合并到那个主题
        db = self.mem._conn()
        db.execute("UPDATE topics SET title = '项目时间表' WHERE id = ?", (tid,))
        db.commit()
        got = self.mem.locate('topic', tid + 7, self.file, '第一阶段在三月底完成验收', '项目安排')
        self.assertEqual((got['state'], got['id'], got['text']), ('merged', tid, '项目时间表'))
        self.mem.forget_topic(tid)
        self.assertEqual(self.mem.locate('topic', tid, self.file, '第一阶段在三月底完成验收', '项目安排')['state'], 'gone')
        with self.assertRaises(ValueError):
            self.mem.locate('file', 1)

    def test_recall_points_carry_topic(self):
        self.mem.apply_update(self.batch()[1], reply(topics=[self.topic()]), 'manual')
        tid = self.mem.listing()['topics'][0]['id']
        point = self.recall('第一阶段验收')[0]['facts'][0]
        self.assertEqual((point['topic'], point['topic_title']), (tid, '项目安排'))

    def test_fact_wrong_rejects(self):
        fid = self.ids['预算总额为一百万元']
        self.assertTrue(self.mem.forget_fact(fid, wrong=True))
        self.assertEqual(self.mem.remember_answer('a5', '问', ANSWER, {1: self.file}), 1)   # 只记下另一条

    def test_answer_facts(self):
        self.assertEqual(sorted(self.mem.answer_facts('a1')), sorted(self.ids.values()))
        self.assertEqual(self.mem.answer_facts('a'), [])

    def test_listing_pages_ids_and_path(self):
        r = self.mem.listing(limit=1)
        self.assertEqual((len(r['facts']), r['facts_total'], r['topics_total']), (1, 2, 0))
        r2 = self.mem.listing(limit=1, offset=1)
        self.assertNotEqual(r2['facts'][0]['id'], r['facts'][0]['id'])
        self.assertEqual(r2['files'], [])
        fid = self.ids['预算总额为一百万元']
        self.assertEqual([f['id'] for f in self.mem.listing(ids=[fid])['facts']], [fid])
        self.assertEqual(self.mem.listing(ids=[])['facts'], [])
        self.assertEqual(self.mem.listing(path=self.file)['facts_total'], 2)
        self.assertEqual(self.mem.listing(path=self.file + 'x')['facts_total'], 0)

    def test_file_marks(self):
        st = os.stat(self.file)
        bad = os.path.join(self.root, '扫描.pdf')
        self.write('x', bad)
        bst = os.stat(bad)
        self.mem.ingest_failed(bad, bst, '取不出文字')
        new = os.path.join(self.root, '新.txt')
        marks = self.mem.file_marks([(self.file, st.st_size, int(st.st_mtime), False),
                                     (bad, bst.st_size, int(bst.st_mtime), True),
                                     (new, 1, 1, True), (os.path.join(self.root, '别处.txt'), 1, 1, False)])
        self.assertEqual(marks, {self.file: {'state': 'done', 'facts': 2},
                                 bad: {'state': 'failed', 'reason': '取不出文字'}, new: {'state': 'waiting'}})
        # 文件改过后算待收录
        self.assertEqual(self.mem.file_marks([(bad, bst.st_size + 1, int(bst.st_mtime), True)]),
                         {bad: {'state': 'waiting'}})
        many = [(os.path.join(self.root, '%d.txt' % i), 1, 1, False) for i in range(2000)] + \
            [(self.file, st.st_size, int(st.st_mtime), False)]
        self.assertEqual(self.mem.file_marks(many), {self.file: {'state': 'done', 'facts': 2}})

    def test_old_version_still_opens(self):
        """新表不升 user_version：退回旧版时库照常打开。"""
        self.mem.close()
        db = sqlite3.connect(self.mem.path)
        self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], am.SCHEMA)
        self.assertTrue(db.execute("SELECT 1 FROM sqlite_master WHERE name = 'redirects'").fetchone())
        db.close()


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
