#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ask_local_files.py 的服务端测试，只用标准库：python3 -m unittest discover -s ask-local-files/tests"""

import importlib.util
import io
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import fake_model  # noqa: E402

# ask_local_files 在导入时按环境变量定下配置与缓存位置，先指向临时目录
SANDBOX = tempfile.mkdtemp(prefix='ask-local-files-test-')
os.environ['HOME'] = SANDBOX
os.environ['XDG_CONFIG_HOME'] = os.path.join(SANDBOX, '.config')
os.environ['XDG_CACHE_HOME'] = os.path.join(SANDBOX, '.cache')
sys.path.insert(0, TOOL)
_spec = importlib.util.spec_from_file_location('ask_local_files', os.path.join(TOOL, 'ask_local_files.py'))
fa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fa)

logging.disable(logging.WARNING)     # 拒绝访问等预期内的告警不打到测试输出
MODEL_SERVER, MODEL_BASE = fake_model.start()


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)


class ServerCase(unittest.TestCase):
    """每个测试一套独立的设置、索引与网页服务。"""

    model = 'ok'

    def setUp(self):
        self.root = tempfile.mkdtemp(dir=SANDBOX)
        self.docs = os.path.join(self.root, '资料')
        write(os.path.join(self.docs, '季度总结.txt'), '甲' * 100)
        write(os.path.join(self.docs, '子目录', '会议记录.md'), '乙' * 50)
        write(os.path.join(self.docs, '旧文件.doc'), 'x')
        self.config = fa.Config(os.path.join(self.root, 'config.json'))
        self.config.folders = [self.docs]
        self.config.custom = {'base': MODEL_BASE, 'model': self.model, 'key': 'k'}
        self.config.model = 'custom'
        self.index = fa.FileIndex(self.config, os.path.join(self.root, 'index.json'))
        self.index.refresh(wait=True)
        self.server = fa.ThreadingHTTPServer(('127.0.0.1', 0), fa.Handler)
        self.st = fa.WebState(self.server.server_address[1], 'tok', self.config, self.index)
        fa.Handler.state = self.st
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = 'http://127.0.0.1:%d' % self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.root, ignore_errors=True)

    def call(self, path, body=None, token='tok', headers=None):
        """返回 (状态码, 解析后的 JSON 或原始文本)。"""
        h = {'X-Token': token} if token is not None else {}
        h.update(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode('utf-8')
            h['Content-Type'] = 'application/json'
        req = urllib.request.Request(self.base + path, data=data, headers=h, method='POST' if data else 'GET')
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                code, raw = r.status, r.read().decode('utf-8')
        except urllib.error.HTTPError as e:
            code, raw = e.code, e.read().decode('utf-8')
        try:
            return code, json.loads(raw)
        except ValueError:
            return code, raw

    def chat(self, messages, files=None, **extra):
        body = {'files': files or [os.path.join(self.docs, '季度总结.txt')], 'messages': messages, 'model': 'custom'}
        body.update(extra)
        code, raw = self.call('/api/chat', body)
        if code != 200:
            return code, raw
        return code, [json.loads(line) for line in raw.splitlines() if line.strip()]

    def wait_index(self):
        for _ in range(100):
            if not self.index.scanning:
                return
            time.sleep(0.05)


class AccessTest(ServerCase):
    def test_api_needs_token(self):
        self.assertEqual(self.call('/api/state', token=None)[0], 403)
        self.assertEqual(self.call('/api/state', token='bad')[0], 403)
        self.assertEqual(self.call('/api/state')[0], 200)

    def test_foreign_origin_rejected(self):
        self.assertEqual(self.call('/api/state', headers={'Origin': 'http://example.com'})[0], 403)

    def test_foreign_host_rejected(self):
        self.assertEqual(self.call('/api/state', headers={'Host': 'example.com'})[0], 403)


class FolderTest(ServerCase):
    def test_add_missing_folder(self):
        code, r = self.call('/api/folders', {'add': os.path.join(self.root, '没有')})
        self.assertEqual(code, 400)
        self.assertIn('找不到', r['error'])

    def test_add_and_remove(self):
        other = os.path.join(self.root, '其他')
        os.makedirs(other)
        code, r = self.call('/api/folders', {'add': other})
        self.assertEqual(code, 200)
        self.assertIn(other, r['folders'])
        code, r = self.call('/api/folders', {'remove': other})
        self.assertNotIn(other, r['folders'])


class SearchTest(ServerCase):
    def test_keywords_all_must_match(self):
        code, r = self.call('/api/search?q=' + urllib.request.quote('资料 会议'))
        self.assertEqual([x['name'] for x in r['results']], ['会议记录.md'])

    def test_ext_filter(self):
        code, r = self.call('/api/search?q=&ext=txt')
        self.assertEqual([x['name'] for x in r['results']], ['季度总结.txt'])


class TextTest(ServerCase):
    def test_text_chars(self):
        code, r = self.call('/api/text?path=' + urllib.request.quote(os.path.join(self.docs, '季度总结.txt')))
        self.assertEqual(r['chars'], 100)

    def test_outside_folder_rejected(self):
        outside = os.path.join(self.root, 'x.txt')
        write(outside, 'x')
        self.assertEqual(self.call('/api/text?path=' + urllib.request.quote(outside))[0], 400)

    def test_old_office_unsupported(self):
        code, r = self.call('/api/text?path=' + urllib.request.quote(os.path.join(self.docs, '旧文件.doc')))
        self.assertIn('另存为', r['error'])


class ChatTest(ServerCase):
    def test_stream(self):
        code, lines = self.chat([{'role': 'user', 'content': '总结'}])
        self.assertEqual(code, 200)
        self.assertTrue(lines[0]['start'])
        self.assertEqual(''.join(m.get('t', '') for m in lines), '你好，世界')
        self.assertTrue(lines[-1].get('done'))
        sent = json.loads(urllib.request.urlopen(MODEL_BASE + '/last').read())
        self.assertIn('甲' * 100, sent['messages'][0]['content'])

    def test_too_long(self):
        self.config.char_limit = 60
        code, r = self.chat([{'role': 'user', 'content': '总结'}])
        self.assertEqual(code, 413)
        self.assertEqual(r['error'], 'too_long')

    def test_truncate(self):
        self.config.char_limit = 60
        code, lines = self.chat([{'role': 'user', 'content': '总结'}], truncate=True)
        self.assertTrue(lines[0]['truncated'])


def q(s):
    return urllib.request.quote(s)


class SupportFlagTest(ServerCase):
    def names(self, r):
        return {x['name']: x for x in r['results']}

    def test_old_office_marked_unsupported(self):
        r = self.names(self.call('/api/search?q=')[1])
        self.assertFalse(r['旧文件.doc']['supported'])
        self.assertIn('另存为', r['旧文件.doc']['reason'])
        self.assertTrue(r['季度总结.txt']['supported'])
        self.assertEqual(r['季度总结.txt']['reason'], '')

    def test_pdf_without_pdftotext(self):
        write(os.path.join(self.docs, '扫描.pdf'), 'x')
        self.index.refresh(full=True, wait=True)
        with mock.patch.object(fa.shutil, 'which', return_value=None):
            r = self.names(self.call('/api/search?q=pdf')[1])
        self.assertFalse(r['扫描.pdf']['supported'])
        self.assertIn('pdftotext', r['扫描.pdf']['reason'])


class FolderMergeTest(ServerCase):
    def test_parent_reports_merged_children(self):
        code, r = self.call('/api/folders', {'add': self.root})
        self.assertEqual(code, 200)
        self.assertEqual(r['merged'], [self.docs])
        self.assertEqual(r['folders'], [self.root])

    def test_plain_add_merges_nothing(self):
        other = os.path.join(self.root, '其他')
        os.makedirs(other)
        self.assertEqual(self.call('/api/folders', {'add': other})[1]['merged'], [])


class UnreadableTest(ServerCase):
    def test_unreadable_paths_listed(self):
        bad = os.path.join(self.docs, '子目录')
        real = os.scandir

        def scandir(d):
            if d == bad:
                raise PermissionError(13, 'denied')
            return real(d)
        with mock.patch.object(fa.os, 'scandir', side_effect=scandir):
            os.utime(bad, ns=(1, 1))        # 改修改时间，迫使重新列目录
            self.index.refresh(wait=True)
        st = self.call('/api/state')[1]['index']
        self.assertEqual(st['unreadable'], [bad])
        self.assertIn('1 个文件夹无法读取', st['error'])


class DirsTest(ServerCase):
    def test_lists_subfolders_by_prefix(self):
        os.makedirs(os.path.join(self.docs, '.隐藏'))
        os.makedirs(os.path.join(self.docs, '子目录二'))
        code, r = self.call('/api/dirs?prefix=' + q(os.path.join(self.docs, '子')))
        self.assertEqual(r['dirs'], [os.path.join(self.docs, '子目录'), os.path.join(self.docs, '子目录二')])

    def test_trailing_slash_lists_children_without_hidden(self):
        os.makedirs(os.path.join(self.docs, '.隐藏'))
        code, r = self.call('/api/dirs?prefix=' + q(self.docs + os.sep))
        self.assertEqual(r['dirs'], [os.path.join(self.docs, '子目录')])

    def test_tilde_prefix_keeps_tilde(self):
        os.makedirs(os.path.join(SANDBOX, '文档'), exist_ok=True)
        code, r = self.call('/api/dirs?prefix=' + q('~/文'))
        self.assertIn('~/文档', r['dirs'])

    def test_places_only_existing(self):
        os.makedirs(os.path.join(SANDBOX, '文档'), exist_ok=True)
        code, r = self.call('/api/dirs?prefix=')
        paths = [p['path'] for p in r['places']]
        self.assertIn('~', paths)
        self.assertIn('~/文档', paths)
        self.assertNotIn('~/下载', paths)


class TestModelTest(ServerCase):
    def test_ok(self):
        code, r = self.call('/api/test-model', {'model': 'custom'})
        self.assertTrue(r['ok'])

    def test_failure_reason(self):
        self.config.custom['model'] = 'err401'
        code, r = self.call('/api/test-model', {'model': 'custom'})
        self.assertFalse(r['ok'])
        self.assertIn('AK', r['error'])


class HistoryLimitTest(ServerCase):
    history = [{'role': 'user', 'content': '问' * 10}, {'role': 'assistant', 'content': '答' * 10},
               {'role': 'user', 'content': '追' * 10}]

    def test_history_counts_toward_limit(self):
        self.config.char_limit = 120
        code, r = self.chat(self.history)
        self.assertEqual(code, 413)
        self.assertEqual((r['error'], r['files'], r['history'], r['total']), ('too_long', 100, 30, 130))

    def test_truncate_leaves_room_for_history(self):
        self.config.char_limit = 120
        code, lines = self.chat(self.history, truncate=True)
        self.assertTrue(lines[0]['truncated'])
        sent = json.loads(urllib.request.urlopen(MODEL_BASE + '/last').read())
        self.assertEqual(sent['messages'][0]['content'].count('甲'), 90)

    def test_history_alone_over_limit(self):
        self.config.char_limit = 1000
        long_history = [{'role': 'user', 'content': '问' * 1001}]
        code, r = self.chat(long_history, truncate=True)
        self.assertEqual(code, 413)
        self.assertEqual(r['error'], 'history_too_long')


class ModelErrorTest(ServerCase):
    def error_for(self, model):
        self.config.custom['model'] = model
        code, lines = self.chat([{'role': 'user', 'content': '总结'}])
        return lines[-1]['error']

    def test_401(self):
        e = self.error_for('err401')
        self.assertIn('401', e)
        self.assertIn('AK', e)

    def test_404(self):
        self.assertIn('模型名', self.error_for('err404'))

    def test_context_length(self):
        self.assertIn('长度', self.error_for('toolong'))

    def test_429(self):
        self.assertIn('稍后', self.error_for('err429'))

    def test_other_keeps_detail(self):
        self.assertIn('HTTP 500', self.error_for('err500'))


class TextErrorHintTest(ServerCase):
    def test_missing_file_suggests_refresh(self):
        gone = os.path.join(self.docs, '季度总结.txt')
        os.remove(gone)
        code, r = self.call('/api/text?path=' + q(gone))
        self.assertIn('刷新', r['error'])

    def test_binary_suggests_format(self):
        with open(os.path.join(self.docs, '图.bin'), 'wb') as f:
            f.write(b'\0\1\2')
        code, r = self.call('/api/text?path=' + q(os.path.join(self.docs, '图.bin')))
        self.assertIn('请', r['error'])



class CiteTest(ServerCase):
    model = 'cite'

    def test_rule_sent_and_missing_quote_reported(self):
        code, lines = self.chat([{'role': 'user', 'content': '总结'}])
        sent = json.loads(urllib.request.urlopen(MODEL_BASE + '/last').read())
        self.assertIn(fa.CITE_RULE, sent['messages'][0]['content'])
        done = lines[-1]
        self.assertEqual(done['quotes'], 2)
        self.assertEqual(done['missing'], [{'file': 1, 'quote': '编造的话'}])
        self.assertFalse(done['uncited'])

    def test_answer_without_quotes_flagged(self):
        self.config.custom['model'] = 'ok'
        code, lines = self.chat([{'role': 'user', 'content': '总结'}])
        self.assertTrue(lines[-1]['uncited'])

    def test_not_found_answer_not_flagged(self):
        self.config.custom['model'] = 'notfound'
        code, lines = self.chat([{'role': 'user', 'content': '总结'}])
        self.assertFalse(lines[-1]['uncited'])
        self.assertEqual(lines[-1]['missing'], [])

    def test_quotes_checked_against_full_text_when_truncated(self):
        self.config.char_limit = 60
        code, lines = self.chat([{'role': 'user', 'content': '总结'}], truncate=True)
        self.assertTrue(lines[0]['truncated'])
        self.assertEqual(len(lines[-1]['missing']), 1)


class CheckQuotesTest(unittest.TestCase):
    def test_whitespace_and_ellipsis(self):
        texts = ['第一条 规定\n如下：每月 5 日前提交。其余事项另行通知。']
        n, missing = fa.check_quotes('甲（文件 1：「第一条规定如下」）乙（文件1:「每月 5 日前……另行通知」）', texts)
        self.assertEqual((n, missing), (2, []))

    def test_wrong_file_number(self):
        n, missing = fa.check_quotes('（文件 2：「规定」）', ['规定'])
        self.assertEqual(missing, [{'file': 2, 'quote': '规定'}])

    def test_reworded_quote(self):
        n, missing = fa.check_quotes('（文件 1：「每月五日前交」）', ['每月 5 日前提交'])
        self.assertEqual(len(missing), 1)


class ClearStartsNewChatTest(ServerCase):
    def test_only_messages_sent_are_used(self):
        self.chat([{'role': 'user', 'content': '旧问题'}])
        self.chat([{'role': 'user', 'content': '新问题'}])
        sent = json.loads(urllib.request.urlopen(MODEL_BASE + '/last').read())
        self.assertEqual([m['content'] for m in sent['messages'][1:]], ['新问题'])




class PageVersionTest(unittest.TestCase):
    def test_footer_default_matches_version(self):
        with open(os.path.join(TOOL, 'ask_local_files.html'), encoding='utf-8') as f:
            self.assertIn('<span id="ver">找文件问 AI v%s</span>' % fa.VERSION, f.read())


if __name__ == '__main__':
    unittest.main()
