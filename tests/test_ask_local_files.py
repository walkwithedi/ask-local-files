#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ask_local_files.py 的服务端测试，只用标准库：python3 -m unittest discover -s ask-local-files/tests"""

import base64
import importlib.util
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.error
import urllib.parse
import urllib.request
import zipfile

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
fa.LOCAL_SERVERS = ()     # 默认不探测本机端口，LocalModelTest 里指向测试用的模型接口


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
        write(os.path.join(self.docs, '资料包.zip'), 'x')
        self.config = fa.Config(os.path.join(self.root, 'config.json'))
        self.config.folders = [self.docs]
        self.config.ignore_md = False       # 各测试照常列出 .md；「忽略 Markdown 文件」另有测试
        self.config.custom = {'base': MODEL_BASE, 'model': self.model, 'key': 'k'}
        self.config.model = 'custom'
        self.index = fa.FileIndex(self.config, os.path.join(self.root, 'index.json'))
        self.index.refresh(wait=True)
        self.server = fa.ThreadingHTTPServer(('127.0.0.1', 0), fa.Handler)
        self.memory = fa.ask_memory.Memory(os.path.join(self.root, 'memory', 'memory.sqlite'))
        self.st = fa.WebState(self.server.server_address[1], 'tok', self.config, self.index, self.memory)
        fa.Handler.state = self.st
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = 'http://127.0.0.1:%d' % self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.memory.close()
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

    def head(self, name='季度总结.txt'):
        """取出的文字开头的文件信息有多少字。"""
        path = os.path.join(self.docs, name)
        return len(fa.file_info(path, os.stat(path)))

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

    def test_in_folder_matches_relative_path(self):
        # 在文件夹里搜：只看其中各层，关键字只和文件夹以下的相对路径比，上级文件夹名不算命中
        sub = os.path.join(self.docs, '子目录')
        write(os.path.join(sub, '更深', '子目录说明.txt'), 'x')
        self.index.refresh(full=True, wait=True)
        code, r = self.call('/api/search?q=%s&dir=%s' % (q('子目录'), q(sub)))
        self.assertEqual(code, 200)
        self.assertEqual([x['name'] for x in r['results']], ['子目录说明.txt'])
        self.assertEqual(r['dir'], sub)
        self.assertEqual([c['name'] for c in r['crumbs']], ['资料', '子目录'])
        r = self.call('/api/search?q=&ext=md&dir=%s' % q(sub))[1]
        self.assertEqual([x['name'] for x in r['results']], ['会议记录.md'])
        r = self.call('/api/search?q=&ext=&dir=%s' % q(sub))[1]
        self.assertEqual(r['total'], 2)
        r = self.call('/api/search?re=1&q=%s&dir=%s' % (q('^更深/'), q(sub)))[1]
        self.assertEqual([x['name'] for x in r['results']], ['子目录说明.txt'])
        # 全部：照旧从所添加文件夹的名字起比
        r = self.call('/api/search?q=' + q('子目录'))[1]
        self.assertEqual(r['total'], 2)
        self.assertEqual((r['dir'], r['crumbs']), ('', []))

    def test_in_folder_sort_before_limit(self):
        sub = os.path.join(self.docs, '批量')
        for i in range(5):
            write(os.path.join(sub, '文件%d.txt' % i), 'x')
            os.utime(os.path.join(sub, '文件%d.txt' % i), (1000 + i, 1000 + i))
        self.index.refresh(full=True, wait=True)
        found, total = self.index.snap.search(['文件'], set(), limit=2, order='time_asc', folder=sub)
        self.assertEqual(([os.path.basename(e[0]) for e in found], total), (['文件0.txt', '文件1.txt'], 5))

    def test_folder_outside(self):
        code, r = self.call('/api/search?q=&dir=%s' % q(self.root))
        self.assertEqual((code, r['error']), (200, fa.FOLDER_GONE))


class IgnoreMarkdownTest(ServerCase):
    def setUp(self):
        super().setUp()
        write(os.path.join(self.docs, '说明.MARKDOWN'), 'x')
        write(os.path.join(self.docs, '大批', '会议纪要.md'), 'x')
        self.index.refresh(full=True, wait=True)

    def names(self, r, key='results'):
        return sorted(x['name'] for x in r[key])

    def test_default_on_and_saved(self):
        self.assertTrue(fa.Config(os.path.join(self.root, 'new.json')).ignore_md)
        self.assertFalse(self.call('/api/state')[1]['ignore_md'])
        self.call('/api/settings', {'ignore_md': True})
        self.assertTrue(fa.Config(self.config.path).ignore_md)
        # 没传 md 时按设置
        self.assertEqual(self.names(self.call('/api/search?q=')[1]), ['季度总结.txt', '资料包.zip'])

    def test_search(self):
        r = self.call('/api/search?q=&md=1')[1]
        self.assertEqual((self.names(r), r['total']), (['季度总结.txt', '资料包.zip'], 2))
        self.assertEqual(self.names(self.call('/api/search?q=&md=0')[1]),
                         ['会议纪要.md', '会议记录.md', '季度总结.txt', '说明.MARKDOWN', '资料包.zip'])
        # 扩展名框明确填了 md 或 markdown 时照列
        self.assertEqual(self.names(self.call('/api/search?q=&ext=md&md=1')[1]), ['会议纪要.md', '会议记录.md'])
        self.assertEqual(self.names(self.call('/api/search?q=&ext=txt%20markdown&md=1')[1]),
                         ['季度总结.txt', '说明.MARKDOWN'])

    def test_browse_lists_and_counts(self):
        top = self.call('/api/browse?dir=&md=1')[1]
        self.assertEqual([d['files'] for d in top['dirs']], [2])
        r = self.call('/api/browse?md=1&dir=' + q(self.docs))[1]
        self.assertEqual(r['dirs'], [])     # 只有 Markdown 文件的子文件夹不列
        self.assertEqual((self.names(r, 'files'), r['here']), (['季度总结.txt', '资料包.zip'], 2))
        self.assertEqual((self.names(r, 'tree'), r['tree_total']), (['季度总结.txt'], 1))
        r = self.call('/api/browse?md=0&dir=' + q(self.docs))[1]
        self.assertEqual(r['tree_total'], 4)


class RegexSearchTest(ServerCase):
    def search(self, query, ext=''):
        return self.call('/api/search?re=1&q=%s&ext=%s' % (q(query), q(ext)))

    def test_regex_matches_display_path(self):
        write(os.path.join(self.docs, '需求说明v2.docx'), 'x')
        write(os.path.join(self.docs, '需求说明.txt'), 'x')
        self.index.refresh(full=True, wait=True)
        code, r = self.search(r'需求.*V\d')
        self.assertEqual(code, 200)
        self.assertEqual([x['name'] for x in r['results']], ['需求说明v2.docx'])
        self.assertEqual(r['total'], 1)
        r = self.search(r'^资料/子目录/')[1]
        self.assertEqual([x['name'] for x in r['results']], ['会议记录.md'])

    def test_regex_with_ext(self):
        r = self.search('季度|会议', 'md')[1]
        self.assertEqual([x['name'] for x in r['results']], ['会议记录.md'])

    def test_bad_regex(self):
        code, r = self.search('季度(')
        self.assertEqual(code, 400)
        self.assertTrue(r['error'].startswith('正则表达式有误：'))

    def test_without_flag_is_plain(self):
        r = self.call('/api/search?q=' + q('季度|会议'))[1]
        self.assertEqual(r['results'], [])

    def test_setting_saved(self):
        self.assertFalse(self.call('/api/state')[1]['regex'])
        code, r = self.call('/api/settings', {'regex': True})
        self.assertEqual(code, 200)
        self.assertTrue(r['regex'])
        self.assertTrue(fa.Config(self.config.path).regex)
        self.assertTrue(self.call('/api/state')[1]['regex'])


class BrowseTest(ServerCase):
    def browse(self, folder=''):
        return self.call('/api/browse?dir=' + urllib.request.quote(folder))[1]

    def test_top_level_lists_added_folders(self):
        r = self.browse()
        self.assertEqual(r['dir'], '')
        self.assertEqual([(d['path'], d['files']) for d in r['dirs']], [(self.docs, 3)])

    def test_level_lists_subfolders_and_files(self):
        write(os.path.join(self.docs, '子目录', '深一层', '纪要.txt'), '丙')
        self.index.refresh(full=True, wait=True)
        r = self.browse(self.docs)
        self.assertEqual([(d['name'], d['files']) for d in r['dirs']], [('子目录', 2)])
        # 列出的文件与在该文件夹里空关键字搜索相同：含各层子文件夹
        self.assertEqual(sorted(f['name'] for f in r['files']), ['会议记录.md', '季度总结.txt', '纪要.txt', '资料包.zip'])
        self.assertEqual(r['here'], 4)
        self.assertEqual([c['name'] for c in r['crumbs']], ['资料'])
        # 全部加入含子文件夹，只算读得了的
        self.assertEqual(r['tree_total'], 3)
        self.assertEqual(sorted(f['name'] for f in r['tree']), ['会议记录.md', '季度总结.txt', '纪要.txt'])
        sub = self.browse(os.path.join(self.docs, '子目录'))
        self.assertEqual([c['name'] for c in sub['crumbs']], ['资料', '子目录'])
        self.assertEqual([d['name'] for d in sub['dirs']], ['深一层'])

    def test_tree_capped_at_twice_max_files_plus_one(self):
        for i in range(fa.TREE_SHOWN + 4):
            write(os.path.join(self.docs, '大批', '%d.txt' % i), 'x')
        self.index.refresh(full=True, wait=True)
        r = self.browse(os.path.join(self.docs, '大批'))
        self.assertEqual(r['tree_total'], fa.TREE_SHOWN + 4)
        self.assertEqual(len(r['tree']), fa.MAX_FILES * 2 + 1)

    def test_folder_outside_rejected(self):
        self.assertIn('error', self.browse(self.root))
        self.assertIn('error', self.browse(self.docs + '-其他'))


class SortTest(ServerCase):
    """搜索与浏览按 sort 排序，先排序再截取；不认识的取值按最新在前。"""

    def setUp(self):
        super().setUp()
        for i, name in enumerate(['b.txt', 'C.txt', 'a.txt']):
            path = os.path.join(self.docs, '排序', name)
            write(path, 'x')
            os.utime(path, (1700000000 + i * 100, 1700000000 + i * 100))
        self.index.refresh(full=True, wait=True)

    def names(self, path, sort):
        return [x['name'] for x in self.call(path + '&sort=' + sort)[1][path.startswith('/api/search') and 'results' or 'files']]

    def test_search_orders(self):
        path = '/api/search?q=' + q('排序')
        self.assertEqual(self.names(path, 'time_desc'), ['a.txt', 'C.txt', 'b.txt'])
        self.assertEqual(self.names(path, 'time_asc'), ['b.txt', 'C.txt', 'a.txt'])
        self.assertEqual(self.names(path, 'name_asc'), ['a.txt', 'b.txt', 'C.txt'])
        self.assertEqual(self.names(path, 'name_desc'), ['C.txt', 'b.txt', 'a.txt'])
        self.assertEqual(self.names(path, 'bogus'), ['a.txt', 'C.txt', 'b.txt'])

    def test_browse_files_and_tree_sorted(self):
        path = '/api/browse?dir=' + q(os.path.join(self.docs, '排序'))
        self.assertEqual(self.names(path, 'name_asc'), ['a.txt', 'b.txt', 'C.txt'])
        r = self.call(path + '&sort=time_asc')[1]
        self.assertEqual([x['name'] for x in r['tree']], ['b.txt', 'C.txt', 'a.txt'])

    def test_browse_sorts_all_layers(self):
        # 子文件夹里的文件与本层的一起排序
        path = os.path.join(self.docs, '排序', '下层', 'm.txt')
        write(path, 'x')
        os.utime(path, (1700000050, 1700000050))
        self.index.refresh(full=True, wait=True)
        path = '/api/browse?dir=' + q(os.path.join(self.docs, '排序'))
        self.assertEqual(self.names(path, 'time_desc'), ['a.txt', 'C.txt', 'm.txt', 'b.txt'])
        self.assertEqual(self.names(path, 'time_asc'), ['b.txt', 'm.txt', 'C.txt', 'a.txt'])
        self.assertEqual(self.names(path, 'name_asc'), ['a.txt', 'b.txt', 'C.txt', 'm.txt'])
        r = self.call(path + '&sort=time_desc')[1]
        self.assertEqual(r['here'], 4)
        self.assertEqual([d['name'] for d in r['dirs']], ['下层'])

    def test_same_mtime_ordered_by_path(self):
        for name in ('y.txt', 'x.txt', 'z.txt'):
            path = os.path.join(self.docs, '同时', name)
            write(path, 'x')
            os.utime(path, (1600000000, 1600000000))
        self.index.refresh(full=True, wait=True)
        for path in ('/api/browse?dir=' + q(os.path.join(self.docs, '同时')), '/api/search?q=' + q('同时')):
            self.assertEqual(self.names(path, 'time_desc'), ['x.txt', 'y.txt', 'z.txt'])
            self.assertEqual(self.names(path, 'time_asc'), ['x.txt', 'y.txt', 'z.txt'])

    def test_sort_before_limit(self):
        for i in range(5):
            write(os.path.join(self.docs, '大批', '%d.txt' % i), 'x')
        self.index.refresh(full=True, wait=True)
        found, total = self.index.snap.search(['大批'], set(), limit=2, order='name_desc')
        self.assertEqual(total, 5)
        self.assertEqual([os.path.basename(e[0]) for e in found], ['4.txt', '3.txt'])
        found, total = self.index.snap.search(['大批'], {'md'}, limit=2, order='name_asc')
        self.assertEqual((found, total), ([], 0))


class PagesTest(ServerCase):
    def test_ping_reports_open_pages(self):
        self.assertEqual(self.call('/api/ping', {'page': 'a'})[1]['pages'], 1)
        self.assertEqual(self.call('/api/ping', {'page': 'b'})[1]['pages'], 2)
        self.call('/api/bye', {'t': 'tok', 'page': 'b'})
        self.assertEqual(self.call('/api/ping', {'page': 'a'})[1]['pages'], 1)

    def test_tabs_in_one_window_count_as_one_page(self):
        self.assertEqual(self.call('/api/ping', {'page': 'a'})[1]['pages'], 1)
        self.assertEqual(self.call('/api/ping', {'page': 'a2', 'shell': 'a'})[1]['pages'], 1)
        self.assertEqual(self.call('/api/ping', {'page': 'a3', 'shell': 'a'})[1]['pages'], 1)
        self.assertEqual(self.call('/api/ping', {'page': 'b'})[1]['pages'], 2)
        self.call('/api/bye', {'t': 'tok', 'page': 'a2'})
        self.assertEqual(self.call('/api/ping', {'page': 'a'})[1]['pages'], 2)


class OpenPageTest(unittest.TestCase):
    """优先用 Chromium 系浏览器的应用窗口模式打开，找不到或启动失败时用默认浏览器。"""

    def which(self, found):
        return lambda name: '/usr/bin/' + name if name in found else None

    def test_app_window_with_first_found_browser(self):
        with mock.patch.object(fa.shutil, 'which', self.which({'google-chrome', 'microsoft-edge'})), \
                mock.patch.object(fa.subprocess, 'Popen') as popen, mock.patch.object(fa.webbrowser, 'open') as wb:
            self.assertTrue(fa.open_page('http://127.0.0.1:1/?t=x'))
        self.assertEqual(popen.call_args[0][0], ['/usr/bin/google-chrome', '--app=http://127.0.0.1:1/?t=x'])
        wb.assert_not_called()

    def test_falls_back_to_default_browser(self):
        with mock.patch.object(fa.shutil, 'which', self.which(set())), \
                mock.patch.object(fa, 'desktop_dirs', return_value=[]), \
                mock.patch.object(fa.subprocess, 'Popen') as popen, \
                mock.patch.object(fa.webbrowser, 'open', return_value=True) as wb:
            self.assertTrue(fa.open_page('http://x/'))
        popen.assert_not_called()
        wb.assert_called_once_with('http://x/')

    def desktop_env(self, root):
        return mock.patch.dict(os.environ, {'XDG_DATA_HOME': os.path.join(root, 'home'),
                                            'XDG_DATA_DIRS': os.path.join(root, 'sys')})

    def test_cn_browser_on_path_after_others(self):
        with mock.patch.object(fa.shutil, 'which', self.which({'qaxbrowser-safe-stable', 'browser360-cn-stable'})), \
                mock.patch.object(fa.subprocess, 'Popen') as popen:
            fa.open_page('http://x/')
        self.assertEqual(popen.call_args[0][0], ['/usr/bin/qaxbrowser-safe-stable', '--app=http://x/'])
        with mock.patch.object(fa.shutil, 'which', self.which({'qaxbrowser-safe-stable', 'chromium'})), \
                mock.patch.object(fa.subprocess, 'Popen') as popen:
            fa.open_page('http://x/')
        self.assertEqual(popen.call_args[0][0][0], '/usr/bin/chromium')

    def test_browser_from_desktop_file(self):
        root = tempfile.mkdtemp(dir=SANDBOX)
        self.addCleanup(shutil.rmtree, root, True)
        exe = os.path.join(root, 'qax', 'qaxbrowser')
        write(exe, '#!/bin/sh\n')
        os.chmod(exe, 0o700)
        write(os.path.join(root, 'sys', 'applications', 'qaxbrowser-safe.desktop'),
              '[Desktop Entry]\nName=x\nExec=env A=1 %s %%U\n[Desktop Action new]\nExec=/bin/false\n' % exe)
        with self.desktop_env(root), mock.patch.object(fa.shutil, 'which', self.which(set())), \
                mock.patch.object(fa.subprocess, 'Popen') as popen:
            fa.open_page('http://x/')
        self.assertEqual(popen.call_args[0][0], [exe, '--app=http://x/'])

    def test_desktop_file_with_missing_program_skipped(self):
        root = tempfile.mkdtemp(dir=SANDBOX)
        self.addCleanup(shutil.rmtree, root, True)
        write(os.path.join(root, 'home', 'applications', 'browser360-cn.desktop'),
              '[Desktop Entry]\nExec=%s %%U\n' % os.path.join(root, 'gone'))
        with self.desktop_env(root), mock.patch.object(fa.shutil, 'which', self.which(set())), \
                mock.patch.object(fa.subprocess, 'Popen') as popen, \
                mock.patch.object(fa.webbrowser, 'open', return_value=True) as wb:
            self.assertTrue(fa.open_page('http://x/'))
        popen.assert_not_called()
        wb.assert_called_once_with('http://x/')

    def test_falls_back_when_launch_fails(self):
        with mock.patch.object(fa.shutil, 'which', self.which({'chromium'})), \
                mock.patch.object(fa.subprocess, 'Popen', side_effect=OSError('x')), \
                mock.patch.object(fa.webbrowser, 'open', return_value=True) as wb:
            self.assertTrue(fa.open_page('http://x/'))
        wb.assert_called_once_with('http://x/')

    def test_no_app_uses_default_browser(self):
        with mock.patch.object(fa.shutil, 'which', self.which({'chromium'})), \
                mock.patch.object(fa.subprocess, 'Popen') as popen, \
                mock.patch.object(fa.webbrowser, 'open', return_value=True) as wb:
            fa.open_page('http://x/', app=False)
        popen.assert_not_called()
        wb.assert_called_once_with('http://x/')

    def test_no_app_option(self):
        with mock.patch.object(fa, 'serve', return_value=0) as serve:
            fa.main(['--no-app'])
            self.assertEqual(serve.call_args[1], {'open_browser': True, 'app': False, 'port': None,
                                                  'idle': fa.IDLE_TIMEOUT})
            fa.main(['--port', '50001', '--idle-timeout', '0'])
            self.assertEqual(serve.call_args[1], {'open_browser': True, 'app': True, 'port': 50001, 'idle': 0})
        for bad in (['--port', '0'], ['--port', 'x'], ['--idle-timeout', '-1']):
            with self.assertRaises(SystemExit), mock.patch('sys.stderr', io.StringIO()):
                fa.main(bad)


class TokenTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(dir=SANDBOX)
        self.path = os.path.join(self.root, 'cfg', 'token')

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_created_private_and_reused(self):
        t = fa.load_token(self.path)
        self.assertRegex(t, fa.TOKEN_RE)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        self.assertEqual(fa.load_token(self.path), t)
        t2 = fa.load_token(self.path, new=True)
        self.assertNotEqual(t2, t)
        self.assertEqual(fa.load_token(self.path), t2)

    def test_malformed_replaced(self):
        os.makedirs(os.path.dirname(self.path))
        write(self.path, 'a b')
        t = fa.load_token(self.path)
        self.assertRegex(t, fa.TOKEN_RE)
        self.assertEqual(fa.load_token(self.path), t)

    def test_port_in_config_kept_on_save(self):
        path = os.path.join(self.root, 'config.json')
        write(path, json.dumps({'port': 50002}))
        c = fa.Config(path)
        self.assertEqual(c.port, 50002)
        c.save()
        with open(path, encoding='utf-8') as f:
            self.assertEqual(json.load(f)['port'], 50002)
        for bad in (0, 70000, True, '50002'):
            write(path, json.dumps({'port': bad}))
            self.assertIsNone(fa.Config(path).port)
        fa.Config(path).save()
        with open(path, encoding='utf-8') as f:
            self.assertNotIn('port', json.load(f))


class FixedUrlTest(ServerCase):
    def test_hello_and_state_url(self):
        self.assertEqual(self.call('/api/hello'), (200, {'app': 'ask-local-files', 'version': fa.VERSION,
                                                        'dir': os.path.realpath(fa.HERE), 'page': True}))
        self.assertEqual(self.call('/api/hello', token='bad')[0], 403)
        self.assertEqual(self.call('/api/state')[1]['url'], self.base + '/?t=tok')

    def test_probe(self):
        port = self.server.server_address[1]
        self.assertEqual(fa.probe_running(port, 'tok'), 'self')
        self.assertEqual(fa.probe_running(port, 'bad'), 'other')
        class Quiet(fa.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass
        other = fa.ThreadingHTTPServer(('127.0.0.1', 0), Quiet)
        threading.Thread(target=other.serve_forever, daemon=True).start()
        try:
            self.assertEqual(fa.probe_running(other.server_address[1], 'tok'), 'other')
        finally:
            other.shutdown()
            other.server_close()
        free = fa.ThreadingHTTPServer(('127.0.0.1', 0), fa.BaseHTTPRequestHandler)
        free_port = free.server_address[1]
        free.server_close()
        self.assertEqual(fa.probe_running(free_port, 'tok'), 'free')

    def test_probe_stale(self):
        port = self.server.server_address[1]
        here = os.path.realpath(fa.HERE)
        replies = [{'app': 'ask-local-files', 'version': '0.1', 'dir': here, 'page': True},
                   {'app': 'ask-local-files', 'version': fa.VERSION, 'dir': here + '-old', 'page': True},
                   {'app': 'ask-local-files', 'version': fa.VERSION}]    # 旧版本不报目录
        class Old(fa.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass
            def do_GET(self):
                body = json.dumps(self.server.reply).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        old = fa.ThreadingHTTPServer(('127.0.0.1', 0), Old)
        threading.Thread(target=old.serve_forever, daemon=True).start()
        try:
            for reply in replies:
                old.reply = reply
                self.assertEqual(fa.probe_running(old.server_address[1], 'tok'), 'stale', reply)
        finally:
            old.shutdown()
            old.server_close()
        with mock.patch.object(fa, 'PAGE_FILE', os.path.join(SANDBOX, 'gone.html')):
            self.assertEqual(fa.probe_running(port, 'tok'), 'stale')
            code, body = self.call('/')
            self.assertEqual(code, 404)
            self.assertIn('「资料管家」', body['error'])

    def test_replace_running(self):
        port = self.server.server_address[1]
        quit_called = threading.Event()
        def stop_old():
            self.st.quit.wait(5)
            quit_called.set()
            self.server.shutdown()
            self.server.server_close()
        threading.Thread(target=stop_old, daemon=True).start()
        new = fa.replace_running(port, 'tok', timeout=5)
        self.assertIsNotNone(new)
        self.assertTrue(quit_called.is_set())
        self.assertEqual(new.server_address[1], port)
        self.server = new        # 由 tearDown 关闭
        threading.Thread(target=new.serve_forever, daemon=True).start()

    def test_replace_running_waits_for_tasks(self):
        self.st.chats = 1
        try:
            self.assertIsNone(fa.replace_running(self.server.server_address[1], 'tok', timeout=1))
        finally:
            self.st.chats = 0

    def serve_on(self, port, token):
        out = io.StringIO()
        with mock.patch.object(fa, 'load_token', return_value=token), \
                mock.patch.object(fa, 'log_file_path', return_value=os.path.join(self.root, 'x.log')), \
                mock.patch.object(fa, 'open_page', return_value=True) as op, \
                mock.patch.object(fa, 'tell_user') as tell, mock.patch('sys.stdout', out):
            code = fa.serve(port=port)
        for h in list(logging.getLogger().handlers):
            if isinstance(h, logging.FileHandler):
                logging.getLogger().removeHandler(h)
                h.close()
        return code, out.getvalue(), op, tell

    def test_serve_reuses_running(self):
        port = self.server.server_address[1]
        code, out, op, tell = self.serve_on(port, 'tok')
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), '网页界面：' + self.base + '/?t=tok')
        op.assert_called_once_with(self.base + '/?t=tok', True)
        tell.assert_not_called()

    def test_serve_port_taken_by_other(self):
        port = self.server.server_address[1]
        code, out, op, tell = self.serve_on(port, 'other-token-xxxxxxxx')
        self.assertEqual(code, 1)
        op.assert_not_called()
        self.assertIn('端口 %d 已被别的程序占用' % port, tell.call_args[0][0])
        self.assertIn('--port', tell.call_args[0][0])

    def test_print_url_and_new_token(self):
        port = self.server.server_address[1]
        out = io.StringIO()
        with mock.patch.object(fa, 'TOKEN_FILE', os.path.join(self.root, 'token')), mock.patch('sys.stdout', out):
            self.assertEqual(fa.main(['--print-url', '--port', str(port)]), 0)
            first = out.getvalue().strip()
            self.assertRegex(first, r'^网页界面：http://127\.0\.0\.1:%d/\?t=[\w-]{16,}$' % port)
            fa.main(['--print-url', '--port', str(port)])
            self.assertEqual(out.getvalue().split('\n')[1], first)    # 令牌沿用
            self.assertEqual(fa.main(['--new-token', '--port', str(port)]), 0)     # 端口上的服务令牌对不上，不算本工具在运行
            self.assertNotEqual(out.getvalue().split('\n')[2], first)
            write(fa.TOKEN_FILE, 'tok-tok-tok-tok-tok\n')
            self.st.token = 'tok-tok-tok-tok-tok'
            err = io.StringIO()
            with mock.patch('sys.stderr', err):
                self.assertEqual(fa.main(['--new-token', '--port', str(port)]), 1)
            self.assertIn('本工具正在运行', err.getvalue())
            self.assertEqual(fa.load_token(), 'tok-tok-tok-tok-tok')


class DataDirTest(ServerCase):
    """保存位置：知识库、设置与令牌放在位置文件记的目录，可移动或改用已有的。"""

    TOKEN = 'cur-token-cur-token-1'

    def setUp(self):
        super().setUp()
        self.cur = os.path.join(self.root, 'cur')
        self.dest = os.path.join(self.root, 'dest')
        os.makedirs(self.cur)
        os.makedirs(self.dest)
        self.pointer = os.path.join(self.root, 'tool', 'data-dir.txt')
        os.makedirs(os.path.dirname(self.pointer))
        for name in ('CONFIG_FILE', 'MEMORY_FILE', 'TOKEN_FILE'):
            p = mock.patch.object(fa, name, getattr(fa, name))
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(fa, 'DATA_DIR_FILE', self.pointer)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.dict(fa.DATA)
        p.start()
        self.addCleanup(p.stop)
        fa.write_data_dir(self.cur, self.pointer)
        fa.init_data_dir()
        self.config.path = fa.CONFIG_FILE
        self.config.save()
        write(fa.TOKEN_FILE, self.TOKEN + '\n')
        self.st.token = self.TOKEN
        self.st.url = '%s/?t=%s' % (self.st.origin, self.TOKEN)
        self.memory.close()
        self.memory = self.st.memory = fa.ask_memory.Memory(fa.MEMORY_FILE)
        path = os.path.join(self.docs, '季度总结.txt')
        self.memory.remember_file(path, os.stat(path), '甲' * 100)

    def tearDown(self):
        self.memory = self.st.memory
        super().tearDown()

    def api(self, body):
        return self.call('/api/data-dir', body, token=self.st.token)

    def test_state_shows_location(self):
        d = self.call('/api/state', token=self.TOKEN)[1]['data']
        self.assertEqual((d['path'], d['custom'], d['ok']), (self.cur, True, True))

    def test_move_to_empty_folder(self):
        code, r = self.api({'path': self.dest})
        self.assertEqual(code, 200)
        self.assertEqual(r['plan']['mode'], 'move')
        self.assertEqual(sorted(i['name'] for i in r['plan']['items']), ['config.json', 'memory.sqlite', 'token'])
        self.assertTrue(os.path.isfile(os.path.join(self.cur, 'memory.sqlite')))      # 只检查不动
        code, r = self.api({'path': self.dest, 'confirm': True})
        self.assertEqual(code, 200, r)
        self.assertEqual(r['mode'], 'move')
        self.assertFalse(r['url_changed'])
        for n in fa.DATA_NAMES:
            self.assertTrue(os.path.isfile(os.path.join(self.dest, n)), n)
            self.assertFalse(os.path.exists(os.path.join(self.cur, n)), n)
        self.assertEqual(oct(os.stat(os.path.join(self.dest, 'memory.sqlite')).st_mode & 0o777), '0o600')
        self.assertEqual(fa.read_data_dir(self.pointer), os.path.realpath(self.dest))
        self.assertEqual(self.st.memory.stats()['files'], 1)
        self.assertEqual(self.st.config.folders, [self.docs])
        self.assertEqual(self.call('/api/state', token=self.TOKEN)[1]['data']['path'], os.path.realpath(self.dest))

    def test_use_existing_data(self):
        other = fa.Config(os.path.join(self.dest, 'config.json'))
        other.folders = [os.path.join(self.docs, '子目录')]
        other.save()
        write(os.path.join(self.dest, 'token'), 'new-token-new-token-2\n')
        code, r = self.api({'path': self.dest})
        self.assertEqual(r['plan']['mode'], 'use')
        self.assertTrue(r['plan']['url_change'])
        self.assertEqual(sorted(i['name'] for i in r['plan']['items']), ['config.json', 'token'])
        code, r = self.api({'path': self.dest, 'confirm': True})
        self.assertEqual(code, 200, r)
        self.assertTrue(r['url_changed'])
        self.assertEqual(self.st.token, 'new-token-new-token-2')
        self.assertTrue(r['url'].endswith('?t=new-token-new-token-2'))
        self.assertEqual(self.st.config.folders, [os.path.join(self.docs, '子目录')])
        for n in fa.DATA_NAMES:      # 当前位置的保留不动，不合并
            self.assertTrue(os.path.isfile(os.path.join(self.cur, n)), n)
        self.assertEqual(self.st.memory.stats()['files'], 0)

    def test_refused(self):
        self.assertEqual(self.api({'path': 'relative/dir'})[0], 400)
        self.assertIn('不存在', self.api({'path': os.path.join(self.root, 'nope')})[1]['error'])
        self.assertIn('当前', self.api({'path': self.cur})[1]['error'])
        self.st.chats = 1
        code, r = self.api({'path': self.dest, 'confirm': True})
        self.st.chats = 0
        self.assertEqual(code, 409)
        self.assertIn('回答正在生成', r['error'])
        self.assertTrue(os.path.isfile(os.path.join(self.cur, 'memory.sqlite')))

    def test_pointer_write_fails_rolls_back(self):
        with mock.patch.object(fa, 'write_data_dir', side_effect=OSError(13, 'Permission denied')):
            code, r = self.api({'path': self.dest, 'confirm': True})
        self.assertEqual(code, 500)
        self.assertIn('位置文件', r['error'])
        for n in fa.DATA_NAMES:
            self.assertTrue(os.path.isfile(os.path.join(self.cur, n)), n)
            self.assertFalse(os.path.exists(os.path.join(self.dest, n)), n)
        self.assertEqual(fa.read_data_dir(self.pointer), self.cur)
        self.assertEqual(self.st.memory.stats()['files'], 1)

    def test_copy_fails_rolls_back(self):
        real = fa.copy_private
        calls = []

        def flaky(src, dst):
            calls.append(dst)
            if len(calls) == 2:
                raise OSError(28, 'No space left on device')
            real(src, dst)
        self.st.memory.close()
        with mock.patch.object(fa, 'copy_private', flaky):
            with self.assertRaises(OSError):
                fa.change_data_dir(os.path.realpath(self.dest), self.pointer)
        self.assertEqual(os.listdir(self.dest), [])
        for n in fa.DATA_NAMES:
            self.assertTrue(os.path.isfile(os.path.join(self.cur, n)), n)

    def test_unavailable_location(self):
        fa.write_data_dir(os.path.join(self.root, 'unmounted'), self.pointer)
        fa.init_data_dir()
        self.assertFalse(fa.DATA['ok'])
        self.assertIn('不存在', fa.DATA['error'])
        code, r = self.call('/api/settings', {'memory': True}, token=self.TOKEN)
        self.assertEqual(code, 409)
        err = io.StringIO()
        with mock.patch('sys.stderr', err):
            self.assertEqual(fa.print_url(), 1)
        self.assertIn('保存位置不可用', err.getvalue())

    def test_unavailable_serve_keeps_nothing(self):
        fa.write_data_dir(os.path.join(self.root, 'unmounted'), self.pointer)
        fa.init_data_dir()
        seen = {}

        class Stop(Exception):
            pass

        def fake_state(port, token, config, index, memory=None, idle=0):
            seen.update(token=token, config=config, memory=memory)
            raise Stop()
        with mock.patch.object(fa, 'WebState', fake_state), \
                mock.patch.object(fa, 'log_file_path', return_value=os.path.join(self.root, 'x.log')), \
                mock.patch.object(fa, 'ThreadingHTTPServer'), \
                mock.patch.object(fa.FileIndex, 'refresh'), mock.patch('sys.stdout', io.StringIO()):
            with self.assertRaises(Stop):
                fa.serve(open_browser=False)
        for h in list(logging.getLogger().handlers):
            if isinstance(h, logging.FileHandler):
                logging.getLogger().removeHandler(h)
                h.close()
        cfg = seen['config']
        self.assertTrue(cfg.readonly)
        self.assertFalse(cfg.memory or cfg.use_memory or cfg.memory_auto)
        self.assertNotEqual(seen['token'], self.TOKEN)
        self.assertNotIn('unmounted', seen['memory'].path)
        cfg.folders = ['/x']
        cfg.save()
        self.assertFalse(os.path.exists(os.path.join(self.root, 'unmounted')))

    def test_cli_data_dir(self):
        self.st.memory.close()
        err = io.StringIO()
        with mock.patch('sys.stderr', err), mock.patch('sys.stdout', io.StringIO()), \
                mock.patch.object(fa, 'probe_running', return_value='free'):
            self.assertEqual(fa.main(['--data-dir', self.dest, '--print-url', '--port', '1']), 0)
            self.assertIn('移到', err.getvalue())
            self.assertTrue(os.path.isfile(os.path.join(self.dest, 'memory.sqlite')))
            self.assertEqual(fa.read_data_dir(self.pointer), os.path.realpath(self.dest))
            self.assertEqual(fa.main(['--data-dir', self.dest, '--print-url', '--port', '1']), 0)     # 已是当前位置
            self.assertEqual(fa.main(['--data-dir', 'relative', '--print-url']), 1)
        self.st.memory = fa.ask_memory.Memory(fa.MEMORY_FILE)

    def test_cli_refuses_while_running(self):
        err = io.StringIO()
        with mock.patch('sys.stderr', err), mock.patch.object(fa, 'probe_running', return_value='self'):
            self.assertEqual(fa.main(['--data-dir', self.dest, '--print-url', '--port', '1']), 1)
        self.assertIn('正在运行', err.getvalue())
        self.assertEqual(os.listdir(self.dest), [])

    def test_cli_pointer_not_writable(self):
        self.st.memory.close()
        err = io.StringIO()
        with mock.patch('sys.stderr', err), mock.patch('sys.stdout', io.StringIO()), \
                mock.patch.object(fa, 'probe_running', return_value='free'), \
                mock.patch.object(fa, 'write_data_dir', side_effect=OSError(30, 'Read-only file system')):
            self.assertEqual(fa.main(['--data-dir', self.dest, '--print-url', '--port', '1']), 0)
        self.assertIn('没能记住', err.getvalue())
        self.assertEqual(fa.MEMORY_FILE, os.path.join(os.path.realpath(self.dest), 'memory.sqlite'))
        self.st.memory = fa.ask_memory.Memory(fa.MEMORY_FILE)


class DataDirDefaultTest(unittest.TestCase):
    def test_no_pointer_keeps_defaults(self):
        root = tempfile.mkdtemp(dir=SANDBOX)
        before = (fa.CONFIG_FILE, fa.MEMORY_FILE, fa.TOKEN_FILE)
        with mock.patch.dict(fa.DATA):
            fa.init_data_dir(os.path.join(root, 'none.txt'))
            self.assertEqual((fa.DATA['dir'], fa.DATA['ok']), (None, True))
            self.assertEqual((fa.CONFIG_FILE, fa.MEMORY_FILE, fa.TOKEN_FILE), before)
            self.assertFalse(fa.data_public()['custom'])
        write(os.path.join(root, 'blank.txt'), '\n')
        self.assertIsNone(fa.read_data_dir(os.path.join(root, 'blank.txt')))
        shutil.rmtree(root, ignore_errors=True)


class ExitRuleTest(ServerCase):
    def test_closing_pages_does_not_exit(self):
        self.st.idle = 100
        self.st.ping('p1')
        self.st.bye('p1')
        self.assertIsNone(self.st.should_exit())
        self.st.empty_since -= 101
        self.assertEqual(self.st.should_exit(), 'idle for 100 seconds')

    def test_never_opened_counts_from_start(self):
        self.st.idle = 100
        self.assertIsNone(self.st.should_exit())
        self.st.empty_since -= 101
        self.assertEqual(self.st.should_exit(), 'idle for 100 seconds')

    def test_open_page_or_task_keeps_running(self):
        self.st.idle = 100
        self.st.ping('p1')
        self.assertIsNone(self.st.should_exit())
        self.st.bye('p1')
        self.st.empty_since -= 101
        self.st.chat_begin()
        self.assertIsNone(self.st.should_exit())
        self.st.chat_end()
        self.assertIsNone(self.st.should_exit())       # 空闲从任务结束时重新算起

    def test_heartbeat_lost_starts_idle(self):
        self.st.idle = 100
        self.st.ping('p1')
        self.st.pages['p1'] = (time.monotonic() - fa.PING_TIMEOUT - 1, 'p1')
        self.assertIsNone(self.st.should_exit())
        self.assertNotIn('p1', self.st.pages)
        self.st.empty_since -= 101
        self.assertEqual(self.st.should_exit(), 'idle for 100 seconds')

    def test_zero_never_idles_out(self):
        self.st.idle = 0
        self.st.empty_since -= 10 ** 6
        self.assertIsNone(self.st.should_exit())
        self.st.quit.set()
        self.assertEqual(self.st.should_exit(), 'quit requested')


class MaxFilesTest(ServerCase):
    def test_fifty_files_accepted(self):
        self.assertEqual(fa.MAX_FILES, 50)
        self.assertEqual(self.call('/api/state')[1]['max_files'], 50)
        paths = []
        for i in range(fa.MAX_FILES):
            paths.append(os.path.join(self.docs, '批量', '%02d.txt' % i))
            write(paths[-1], '丁')
        self.index.refresh(full=True, wait=True)
        code, lines = self.chat([{'role': 'user', 'content': '总结'}], files=paths)
        self.assertEqual(code, 200)
        code, r = self.chat([{'role': 'user', 'content': '总结'}], files=paths + [os.path.join(self.docs, '季度总结.txt')])
        self.assertEqual(code, 400)


class TextTest(ServerCase):
    def test_text_chars(self):
        code, r = self.call('/api/text?path=' + urllib.request.quote(os.path.join(self.docs, '季度总结.txt')))
        self.assertEqual(r['chars'], 100 + self.head())

    def test_file_info_first(self):
        path = os.path.join(self.docs, '季度总结.txt')
        os.utime(path, (1772600767, 1772600767))
        code, r = self.call('/api/text?preview=1&path=' + urllib.request.quote(path))
        when = time.strftime('%Y-%m-%d %H:%M', time.localtime(1772600767))
        self.assertEqual(r['text'], '路径：%s\n文件名：季度总结.txt\n修改时间：%s\n\n%s' % (path, when, '甲' * 100))

    def test_file_info_authors(self):
        path = os.path.join(self.docs, 'a.txt')
        write(path, 'x')
        st = os.stat(path)
        for authors, lines in ((('张三', '李四'), ['作者：张三', '最后修改者：李四']), (('张三', '张三'), ['作者：张三']),
                               (('', '李四'), ['作者：李四']), (('', ''), [])):
            with mock.patch.object(fa.doc2kb_core, 'doc_authors', return_value=authors):
                info = fa.file_info(path, st).rstrip('\n').split('\n')
            self.assertEqual(info[3:], lines)

    def test_archive_not_read(self):
        code, r = self.call('/api/text?path=' + urllib.request.quote(os.path.join(self.docs, '资料包.zip')))
        self.assertEqual(r['error'], fa.ARCHIVE_REASON)

    def test_old_office_by_doc2kb(self):
        seen = []

        def convert(path, ext, out, report, save_images=True):
            seen.append(ext)
            return '表格内容', []
        for name, ext in (('a.doc', '.doc'), ('b.xls', '.xls'), ('c.ppt', '.ppt'), ('d.wps', '.doc'), ('e.et', '.xls'),
                          ('f.dps', '.ppt')):
            write(os.path.join(self.docs, name), 'x')
            with mock.patch.object(fa.doc2kb_core, 'convert', side_effect=convert):
                text, _ = fa.TextCache().get(os.path.join(self.docs, name))
            self.assertTrue(text.endswith('\n\n表格内容'))
            self.assertEqual(seen[-1], ext)

    def test_outside_folder_rejected(self):
        outside = os.path.join(self.root, 'x.txt')
        write(outside, 'x')
        self.assertEqual(self.call('/api/text?path=' + urllib.request.quote(outside))[0], 400)

    def test_broken_old_office(self):
        write(os.path.join(self.docs, '旧文件.wps'), 'x')
        self.index.refresh(full=True, wait=True)
        code, r = self.call('/api/text?path=' + urllib.request.quote(os.path.join(self.docs, '旧文件.wps')))
        self.assertTrue(r['error'].startswith('读取失败：'))
        self.assertTrue(r['error'].endswith('请先另存为 docx、xlsx 或 pptx'))


class ChatTest(ServerCase):
    def test_stream(self):
        code, lines = self.chat([{'role': 'user', 'content': '总结'}])
        self.assertEqual(code, 200)
        self.assertTrue(lines[0]['start'])
        self.assertEqual(''.join(m.get('t', '') for m in lines), '你好，世界')
        self.assertTrue(lines[-1].get('done'))
        sent = json.loads(urllib.request.urlopen(MODEL_BASE + '/last').read())
        # system 只有规则，文件在最新一问里
        self.assertNotIn('甲' * 100, sent['messages'][0]['content'])
        self.assertIn('甲' * 100, sent['messages'][-1]['content'])

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

    def test_archive_marked_unsupported(self):
        write(os.path.join(self.docs, '旧表格.xls'), 'x')
        write(os.path.join(self.docs, '备份.tar.gz'), 'x')
        self.index.refresh(full=True, wait=True)
        r = self.names(self.call('/api/search?q=')[1])
        for name in ('资料包.zip', '备份.tar.gz'):
            self.assertFalse(r[name]['supported'])
            self.assertEqual(r[name]['reason'], fa.ARCHIVE_REASON)
        self.assertTrue(r['旧表格.xls']['supported'])
        self.assertTrue(r['季度总结.txt']['supported'])
        self.assertEqual(r['季度总结.txt']['reason'], '')

    def test_pdf_without_pdftotext(self):
        write(os.path.join(self.docs, '扫描.pdf'), 'x')
        self.index.refresh(full=True, wait=True)
        with mock.patch.object(fa.shutil, 'which', return_value=None):
            r = self.names(self.call('/api/search?q=pdf')[1])
        self.assertFalse(r['扫描.pdf']['supported'])
        self.assertIn('pdftotext', r['扫描.pdf']['reason'])


class OpenFolderTest(ServerCase):
    @staticmethod
    def which(*have):
        return lambda c: '/usr/bin/' + c if c in have else None

    def test_show_items_selects_file(self):
        path = os.path.join(self.docs, '子目录', '会议记录.md')
        with mock.patch.object(fa.shutil, 'which', side_effect=self.which('gdbus', 'xdg-open')), \
                mock.patch.object(fa.subprocess, 'run', return_value=mock.Mock(returncode=0)) as run, \
                mock.patch.object(fa.subprocess, 'Popen') as popen:
            code, r = self.call('/api/open-folder', {'path': path})
        self.assertEqual((code, r['selected']), (200, True))
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[0], 'gdbus')
        self.assertIn("['file://%s']" % urllib.parse.quote(path), cmd)
        popen.assert_not_called()

    def test_dbus_send_when_no_gdbus(self):
        path = os.path.join(self.docs, '季度总结.txt')
        with mock.patch.object(fa.shutil, 'which', side_effect=self.which('dbus-send')), \
                mock.patch.object(fa.subprocess, 'run', return_value=mock.Mock(returncode=0)) as run:
            self.assertTrue(self.call('/api/open-folder', {'path': path})[1]['selected'])
        self.assertIn('array:string:file://' + urllib.parse.quote(path), run.call_args[0][0])

    def test_falls_back_to_file_manager_select(self):
        path = os.path.join(self.docs, '季度总结.txt')
        with mock.patch.object(fa.shutil, 'which', side_effect=self.which('gdbus', 'dbus-send', 'nautilus', 'xdg-open')), \
                mock.patch.object(fa.subprocess, 'run', return_value=mock.Mock(returncode=1)) as run, \
                mock.patch.object(fa.subprocess, 'Popen') as popen:
            code, r = self.call('/api/open-folder', {'path': path})
        self.assertEqual((code, r['selected']), (200, True))
        self.assertEqual(run.call_count, 1)
        self.assertEqual(popen.call_args[0][0], ['nautilus', '--select', 'file://' + urllib.parse.quote(path)])

    def test_dbus_timeout_falls_back_to_folder(self):
        path = os.path.join(self.docs, '季度总结.txt')
        with mock.patch.object(fa.shutil, 'which', side_effect=self.which('gdbus', 'xdg-open')), \
                mock.patch.object(fa.subprocess, 'run', side_effect=fa.subprocess.TimeoutExpired('gdbus', 5)), \
                mock.patch.object(fa.subprocess, 'Popen') as popen:
            code, r = self.call('/api/open-folder', {'path': path})
        self.assertEqual((code, r['selected']), (200, False))
        self.assertEqual(popen.call_args[0][0], ['xdg-open', self.docs])

    def test_opens_containing_folder(self):
        path = os.path.join(self.docs, '子目录', '会议记录.md')
        with mock.patch.object(fa.shutil, 'which', side_effect=lambda c: '/usr/bin/' + c if c == 'gio' else None), \
                mock.patch.object(fa.subprocess, 'Popen') as popen:
            code, r = self.call('/api/open-folder', {'path': path})
        self.assertEqual((code, r['folder'], r['selected']), (200, os.path.join(self.docs, '子目录'), False))
        self.assertEqual(popen.call_args[0][0], ['gio', 'open', os.path.join(self.docs, '子目录')])

    def test_no_opener(self):
        path = os.path.join(self.docs, '资料包.zip')
        with mock.patch.object(fa.shutil, 'which', return_value=None):
            code, r = self.call('/api/open-folder', {'path': path})
        self.assertEqual(code, 500)
        self.assertEqual(r['folder'], self.docs)
        self.assertIn('xdg-open', r['error'])

    def test_outside_rejected(self):
        outside = os.path.join(self.root, 'x.zip')
        write(outside, 'x')
        with mock.patch.object(fa.subprocess, 'Popen') as popen:
            self.assertEqual(self.call('/api/open-folder', {'path': outside})[0], 400)
            self.assertEqual(self.call('/api/open-folder', {'path': ''})[0], 400)
        popen.assert_not_called()



class ExportTest(ServerCase):
    def test_same_as_model_system_message(self):
        files = [os.path.join(self.docs, '季度总结.txt'), os.path.join(self.docs, '子目录', '会议记录.md')]
        code, r = self.call('/api/export', {'files': files})
        self.assertEqual(code, 200)
        self.assertEqual((r['files'], r['skipped'], r['chars']), (2, 0, len(r['text'])))
        # 与发给模型的一致：规则、文件、重申，没有问题
        self.assertTrue(r['text'].startswith(fa.LANG_RULE + '\n\n' + fa.CITE_RULE))
        self.assertIn('使用者选了下面 2 个文件', r['text'])
        self.assertTrue(r['text'].endswith(fa.CITE_REMINDER + '先给结论，再分点。'))
        self.assertNotIn('使用者的问题', r['text'])
        # 路径从所添加文件夹的名字起写，不带上级部分
        self.assertIn('<document index="1">\n<source>资料/季度总结.txt</source>\n<document_content>\n'
                      '路径：资料/季度总结.txt\n', r['text'])
        self.assertIn('<document index="2">\n<source>资料/子目录/会议记录.md</source>', r['text'])
        self.assertNotIn(self.root, r['text'])
        self.assertIn('甲' * 100, r['text'])
        self.assertNotIn('skill', r['text'])

    def test_no_model_needed(self):
        self.config.custom = {'base': '', 'model': '', 'key': ''}
        with mock.patch.object(fa, 'opencode_config', return_value={}):
            code, r = self.call('/api/export', {'files': [os.path.join(self.docs, '季度总结.txt')]})
        self.assertEqual((code, r['files']), (200, 1))

    def test_skips_unreadable(self):
        good, bad = os.path.join(self.docs, '季度总结.txt'), os.path.join(self.docs, '资料包.zip')
        real = self.st.texts.get

        def get(path):
            if path == bad:
                raise ValueError('坏文件')
            return real(path)
        with mock.patch.object(self.st.texts, 'get', side_effect=get):
            code, r = self.call('/api/export', {'files': [bad, good]})
            self.assertEqual((code, r['files'], r['skipped']), (200, 1, 1))
            self.assertIn('<document index="1">\n<source>资料/季度总结.txt</source>', r['text'])
            self.assertEqual(self.call('/api/export', {'files': [bad]})[0], 400)

    def test_only_indexed_paths(self):
        # 在已添加的文件夹里、但还没进索引的文件，以及写法不同的同一路径，都要先刷新索引
        fresh = os.path.join(self.docs, '新文件.txt')
        write(fresh, 'x')
        for path in (fresh, os.path.join(self.docs, '子目录', '..', '季度总结.txt')):
            self.assertEqual(self.call('/api/export', {'files': [path]})[0], 400)
            self.assertEqual(self.call('/api/text?path=' + urllib.request.quote(path))[0], 400)
            self.assertEqual(self.call('/api/open-folder', {'path': path})[0], 400)
        self.index.refresh(full=True, wait=True)
        self.assertEqual(self.call('/api/export', {'files': [fresh]})[0], 200)

    def test_rejects_bad_requests(self):
        outside = os.path.join(self.root, 'x.txt')
        write(outside, 'x')
        self.assertEqual(self.call('/api/export', {'files': [outside]})[0], 400)
        self.assertEqual(self.call('/api/export', {'files': []})[0], 400)
        self.assertEqual(self.call('/api/export', {'files': 'x'})[0], 400)
        self.assertEqual(self.call('/api/export', {'files': [os.path.join(self.docs, '季度总结.txt')]}, token='bad')[0], 403)

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


def closed_port():
    """一个当前没有程序监听的本机端口。"""
    import socket
    with socket.socket() as sk:
        sk.bind(('127.0.0.1', 0))
        return sk.getsockname()[1]


class LocalModelTest(ServerCase):
    """自动发现本机的 llama-server 与 Ollama，按上下文折算上限，不带 AK。"""

    def setUp(self):
        super().setUp()
        self.port = MODEL_SERVER.server_address[1]
        self.config.custom = {'base': '', 'model': '', 'key': ''}
        self.config.model = ''
        self.addCleanup(setattr, fake_model.FakeModel, 'serving', None)

    def serve(self, kind, **local):
        fake_model.FakeModel.serving = local
        p = mock.patch.object(fa, 'LOCAL_SERVERS', ((kind, self.port, {'llama': 'llama-server'}.get(kind, 'Ollama')),))
        p.start()
        self.addCleanup(p.stop)

    def models(self):
        code, r = self.call('/api/local-models', {})
        self.assertEqual(code, 200)
        return r, {m['id']: m for m in r['models']}

    def test_llama_reads_n_ctx(self):
        self.serve('llama', models=['qwen'], props={'default_generation_settings': {'n_ctx': 8192}})
        r, ms = self.models()
        m = ms['local:llama/qwen']
        self.assertEqual(r['found'], {'llama': 1})
        self.assertEqual((m['limit'], m['loopback'], m['auth']), (6144, True, False))
        self.assertIn('qwen（本机 llama-server）', m['label'])
        self.assertIn('8192', m['limit_note'])
        self.assertEqual(r['default'], 'local:llama/qwen')

    def test_ollama_loaded_uses_running_context(self):
        self.serve('ollama', models=['qwen2.5:3b'],
                   ps={'models': [{'name': 'qwen2.5:3b', 'model': 'qwen2.5:3b', 'context_length': 16384}]})
        m = self.models()[1]['local:ollama/qwen2.5:3b']
        self.assertEqual(m['limit'], 12288)

    def test_ollama_not_loaded_estimates(self):
        self.serve('ollama', models=['qwen2.5:3b'], ps={'models': []})
        with mock.patch.dict(os.environ, {'OLLAMA_CONTEXT_LENGTH': ''}):
            m = self.models()[1]['local:ollama/qwen2.5:3b']
        self.assertEqual(m['limit'], 3072)
        self.assertIn('OLLAMA_CONTEXT_LENGTH', m['limit_note'])
        with mock.patch.dict(os.environ, {'OLLAMA_CONTEXT_LENGTH': '8192'}):
            self.assertEqual(self.models()[1]['local:ollama/qwen2.5:3b']['limit'], 6144)

    def test_state_probes_on_page_open(self):
        self.serve('llama', models=['qwen'])
        code, s = self.call('/api/state')
        m = {m['id']: m for m in s['models']}['local:llama/qwen']
        self.assertEqual(m['limit'], 0)     # 读不到上下文时只按设置值

    def test_nothing_running(self):
        with mock.patch.object(fa, 'LOCAL_SERVERS', (('llama', closed_port(), 'llama-server'),)):
            r, ms = self.models()
        self.assertEqual((r['found'], ms), ({'llama': 0}, {}))
        code, r = self.chat([{'role': 'user', 'content': '总结'}])
        self.assertEqual(code, 400)
        self.assertIn('重新查找', r['error'])

    def test_chat_limit_follows_context_and_sends_no_key(self):
        self.serve('llama', models=['ok'], props={'default_generation_settings': {'n_ctx': 100}})
        self.models()
        body = {'files': [os.path.join(self.docs, '季度总结.txt')], 'messages': [{'role': 'user', 'content': '总结'}],
                'model': 'local:llama/ok'}
        code, r = self.call('/api/chat', body)
        self.assertEqual((code, r['error'], r['limit']), (413, 'too_long', 75))
        self.serve('llama', models=['ok'], props={'default_generation_settings': {'n_ctx': 8192}})
        self.models()
        code, raw = self.call('/api/chat', body)
        self.assertIn('你好，', raw)
        self.assertIsNone(fake_model.FakeModel.auth)

    def test_stopped_service_says_so(self):
        self.st.local_models.found = {'local:llama/ok': {
            'base': 'http://127.0.0.1:%d/v1' % closed_port(), 'key': '', 'model': 'ok', 'headers': {},
            'label': 'ok（本机 llama-server）', 'host': '127.0.0.1', 'discovered': True, 'ctx': 0, 'ctx_note': ''}}
        code, lines = self.chat([{'role': 'user', 'content': '总结'}], model='local:llama/ok')
        self.assertEqual(lines[-1]['error'], fa.LOCAL_DOWN)


class NoKeyTest(ServerCase):
    """自定义接口不填 AK 也可选用，不发 Authorization；填了照常发。"""

    def test_custom_without_key(self):
        self.config.custom['key'] = ''
        code, s = self.call('/api/state')
        m = {m['id']: m for m in s['models']}['custom']
        self.assertFalse(m['auth'])
        code, lines = self.chat([{'role': 'user', 'content': '总结'}])
        self.assertTrue(lines[-1].get('done'))
        self.assertIsNone(fake_model.FakeModel.auth)

    def test_custom_with_key(self):
        self.chat([{'role': 'user', 'content': '总结'}])
        self.assertEqual(fake_model.FakeModel.auth, 'Bearer k')

    def test_timeout_by_host(self):
        self.assertEqual(fa.model_timeout({'base': 'http://127.0.0.1:8080/v1'}), fa.LOCAL_TIMEOUT)
        self.assertEqual(fa.model_timeout({'base': 'http://localhost:11434/v1'}), fa.LOCAL_TIMEOUT)
        self.assertEqual(fa.model_timeout({'base': 'https://api.example.com/v1'}), fa.TIMEOUT)

    def test_local_tool_error_mentions_jinja(self):
        self.assertIn('--jinja', fa.http_error_text(400, 'tools not supported', True, True))
        self.assertNotIn('--jinja', fa.http_error_text(400, 'tools not supported', True, False))


class HistoryLimitTest(ServerCase):
    history = [{'role': 'user', 'content': '问' * 10}, {'role': 'assistant', 'content': '答' * 10},
               {'role': 'user', 'content': '追' * 10}]

    def test_history_counts_toward_limit(self):
        h = self.head()
        self.config.char_limit = 120 + h
        code, r = self.chat(self.history)
        self.assertEqual(code, 413)
        self.assertEqual((r['error'], r['files'], r['history'], r['total']), ('too_long', 100 + h, 30, 130 + h))

    def test_truncate_leaves_room_for_history(self):
        self.config.char_limit = 120 + self.head()
        code, lines = self.chat(self.history, truncate=True)
        self.assertTrue(lines[0]['truncated'])
        sent = json.loads(urllib.request.urlopen(MODEL_BASE + '/last').read())
        self.assertEqual(sent['messages'][-1]['content'].count('甲'), 90)

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

    def test_rules_before_files_and_repeated_after(self):
        self.chat([{'role': 'user', 'content': '先问'}, {'role': 'assistant', 'content': '先答'},
                   {'role': 'user', 'content': '总结'}])
        msgs = json.loads(urllib.request.urlopen(MODEL_BASE + '/last').read())['messages']
        self.assertIn('不超过 %d 字' % fa.QUOTE_MAX, fa.CITE_RULE)
        # system 只有规则；历史只有文字；最新一问依次为文件、重申、问题
        self.assertEqual([m['role'] for m in msgs], ['system', 'user', 'assistant', 'user'])
        system, last = msgs[0]['content'], msgs[-1]['content']
        for part in (fa.LANG_RULE, fa.CITE_RULE, fa.FORMAT_RULE, fa.CITE_EXAMPLE, fa.DATA_NOTE):
            self.assertIn(part, system)
        self.assertNotIn('<document index', system)
        self.assertEqual((msgs[1]['content'], msgs[2]['content']), ('先问', '先答'))
        first, again, q = last.index('<document index="1">'), last.index(fa.CITE_REMINDER), last.index('使用者的问题：')
        self.assertTrue(first < again < q)
        self.assertTrue(last.endswith(fa.CITE_REMINDER + '先给结论，再分点。\n\n使用者的问题：\n总结'))
        self.assertIn('<source>资料/季度总结.txt</source>', last)

    def test_supplement_not_checked(self):
        self.config.custom['model'] = 'supp'
        code, lines = self.chat([{'role': 'user', 'content': '总结'}])
        self.assertEqual((lines[-1]['quotes'], lines[-1]['missing'], lines[-1]['uncited']), (1, [], False))
        self.assertEqual(fa.main_part('正文\n' + fa.SUPPLEMENT + '补充'), '正文\n')
        self.assertEqual(fa.main_part('正文'), '正文')

    def test_quotes_checked_against_full_text_when_truncated(self):
        self.config.char_limit = 60
        code, lines = self.chat([{'role': 'user', 'content': '总结'}], truncate=True)
        self.assertTrue(lines[0]['truncated'])
        self.assertEqual(len(lines[-1]['missing']), 1)


class MemoryChatTest(ServerCase):
    model = 'memo'

    def setUp(self):
        super().setUp()
        self.plan = os.path.join(self.docs, '项目计划.txt')
        write(self.plan, '项目计划\n\n第一阶段在三月底完成验收。\n\n预算另行说明。')
        self.index.refresh(full=True, wait=True)     # 增量扫描按目录修改时间，同一时钟刻内新建的文件会漏掉

    def ask(self, question, files=(), memory=True):
        """不带默认文件的提问，返回 (状态码, 各行或错误)。"""
        code, raw = self.call('/api/chat', {'files': list(files), 'messages': [{'role': 'user', 'content': question}],
                                            'model': 'custom', 'memory': memory})
        if code != 200:
            return code, raw
        return code, [json.loads(line) for line in raw.splitlines() if line.strip()]

    def sent(self):
        msgs = json.loads(urllib.request.urlopen(MODEL_BASE + '/last').read())['messages']
        return '\n\n'.join(m['content'] for m in msgs)

    def test_remember_then_recall_without_files(self):
        code, lines = self.ask('第一阶段什么时候验收', [self.plan])
        self.assertEqual(code, 200)
        done = lines[-1]
        self.assertEqual(done['remembered'], 1)
        self.assertTrue(done['answer_id'])
        self.assertEqual(lines[0]['memory']['files'], [])      # 选中的文件不再从知识库里重复给
        code, r = self.call('/api/memory')
        self.assertEqual(r['stats']['facts'], 1)
        self.assertEqual(r['facts'][0]['conclusion'], '第一阶段三月底验收')

        code, lines = self.ask('第一阶段验收')
        self.assertEqual(code, 200)
        mem = lines[0]['memory']
        self.assertEqual((mem['facts'], mem['files'][0]['n'], mem['files'][0]['path']), (1, 1, self.plan))
        text = self.sent()
        self.assertIn('来自知识库', text)
        self.assertIn('第一阶段三月底验收（原文：「第一阶段在三月底完成验收」）', text)
        self.assertIn(fa.MEMORY_NOTE, text)
        self.assertEqual(lines[-1]['missing'], [])
        self.assertEqual(self.call('/api/memory')[1]['facts'][0]['count'], 2)

    def test_memory_after_selected_files(self):
        self.ask('第一阶段什么时候验收', [self.plan])
        code, lines = self.ask('第一阶段验收', [os.path.join(self.docs, '季度总结.txt')])
        self.assertEqual(lines[0]['memory']['files'][0]['n'], 2)
        shown = os.path.relpath(self.plan, os.path.dirname(self.docs))
        self.assertEqual(lines[0]['memory']['files'][0]['shown'], shown)
        self.assertIn('<document index="2">\n<source>%s（来自知识库，节选）</source>' % shown, self.sent())
        self.assertNotIn(self.root, self.sent())

    def test_no_files_and_nothing_found(self):
        code, r = self.ask('毫不相干的问题')
        self.assertEqual((code, r['error']), (400, fa.NO_MEMORY_HIT))

    def test_use_memory_unchecked(self):
        self.ask('第一阶段什么时候验收', [self.plan])
        code, r = self.ask('第一阶段验收', memory=False)
        self.assertEqual(code, 400)
        code, lines = self.ask('第一阶段验收', [os.path.join(self.docs, '季度总结.txt')], memory=False)
        self.assertEqual(lines[0]['memory']['files'], [])
        self.assertNotIn('来自知识库', self.sent())

    def test_memory_switched_off(self):
        self.call('/api/settings', {'memory': False})
        code, lines = self.ask('第一阶段什么时候验收', [self.plan])
        self.assertEqual((lines[-1]['remembered'], lines[-1]['answer_id']), (0, ''))
        self.assertEqual(self.call('/api/state')[1]['memory_stats']['files'], 0)
        self.assertEqual(self.ask('第一阶段验收')[0], 400)
        self.assertFalse(fa.Config(self.config.path).memory)

    def test_wrong(self):
        lines = self.ask('第一阶段什么时候验收', [self.plan])[1]
        code, r = self.call('/api/memory', {'action': 'wrong', 'answer_id': lines[-1]['answer_id']})
        self.assertEqual((code, r['count'], r['stats']['facts']), (200, 1, 0))
        lines = self.ask('第一阶段什么时候验收', [self.plan])[1]
        self.assertEqual(lines[-1]['remembered'], 0)

    def test_changed_file(self):
        self.ask('第一阶段什么时候验收', [self.plan])
        write(self.plan, '项目计划\n\n第一阶段推迟到五月完成验收。')
        st = os.stat(self.plan)
        os.utime(self.plan, ns=(st.st_atime_ns, st.st_mtime_ns + 10 ** 9))
        code, lines = self.ask('第一阶段验收')
        self.assertEqual(lines[0]['memory']['facts'], 0)
        self.assertIn('第一阶段推迟到五月完成验收。', self.sent())
        self.assertEqual(len(lines[-1]['missing']), 1)

    def test_manage(self):
        self.ask('第一阶段什么时候验收', [self.plan])
        r = self.call('/api/memory?q=' + q('三月'))[1]
        self.assertEqual(len(r['facts']), 1)
        self.assertEqual(self.call('/api/memory', {'action': 'fact', 'id': r['facts'][0]['id']})[1]['count'], 1)
        self.assertEqual(self.call('/api/memory', {'action': 'file', 'path': self.plan})[1]['stats']['files'], 0)
        self.ask('第一阶段什么时候验收', [self.plan])
        self.assertEqual(self.call('/api/memory', {'action': 'clear'})[1]['stats'], self.memory.stats())
        self.assertEqual(self.memory.stats()['files'], 0)
        self.assertEqual(self.call('/api/memory', {'action': 'nope'})[0], 400)
        self.assertEqual(self.call('/api/memory', {'action': 'fact', 'id': True})[0], 400)

    def test_settings_and_notice(self):
        s = self.call('/api/state')[1]
        self.assertEqual((s['memory'], s['use_memory'], s['memory_notice']), (True, True, False))
        self.call('/api/settings', {'use_memory': False, 'memory_notice': True})
        c = fa.Config(self.config.path)
        self.assertEqual((c.memory, c.use_memory, c.memory_notice), (True, False, True))

    def test_broken_memory_does_not_stop_chat(self):
        def broken(*a, **k):
            raise sqlite3.OperationalError('disk I/O error')
        with mock.patch.object(self.memory, 'remember_file', broken), \
                mock.patch.object(self.memory, 'recall', broken), \
                mock.patch.object(self.memory, 'remember_answer', broken):
            code, lines = self.ask('第一阶段什么时候验收', [self.plan])
        self.assertEqual(code, 200)
        self.assertEqual(lines[-1]['remembered'], 0)


class MemoryUpdateTest(MemoryChatTest):
    """知识库更新：假模型 memo 把结论整理成一页「验收安排」。"""

    def wait(self):
        for _ in range(100):
            st = self.call('/api/memory-update')[1]
            if not st['running']:
                return st
            time.sleep(0.05)
        raise AssertionError('更新没有结束')

    def test_manual_update_and_undo(self):
        st = self.call('/api/memory-update')[1]
        self.assertEqual((st['running'], st['pending'], st['auto'], st['history']), (False, 0, True, []))
        self.ask('第一阶段什么时候验收', [self.plan])
        self.assertEqual(self.call('/api/memory-update')[1]['pending'], 1)
        code, st = self.call('/api/memory-update', {'action': 'run'})
        self.assertEqual(code, 200)
        st = self.wait()
        self.assertEqual(st['result']['kind'], 'done')
        self.assertEqual(st['result']['text'], '知识库已更新：新建主题页 1 页。')
        self.assertEqual(st['pending'], 0)
        h = st['history'][0]
        self.assertEqual((h['mode'], h['summary'], h['changes']), ('manual', '新建主题页 1 页', ['新建主题页：验收安排']))
        self.assertIn('你在维护使用者本机的一份「知识库」', json.loads(
            urllib.request.urlopen(MODEL_BASE + '/last').read())['messages'][0]['content'])
        topics = self.call('/api/memory')[1]['topics']
        self.assertEqual([(t['title'], len(t['points'])) for t in topics], [('验收安排', 1)])
        # 提问时主题页要点排在最前
        code, lines = self.ask('第一阶段验收')
        self.assertIn('验收安排：第一阶段三月底验收（原文：「第一阶段在三月底完成验收」）', self.sent())
        # 撤销：只能撤最近一次
        self.assertEqual(self.call('/api/memory-update', {'action': 'undo', 'id': h['id'] + 1})[0], 409)
        code, st = self.call('/api/memory-update', {'action': 'undo', 'id': h['id']})
        self.assertEqual((code, st['history'], st['stats']['topics']), (200, [], 0))
        self.assertEqual(self.call('/api/memory-update', {'action': 'undo', 'id': True})[0], 400)
        self.assertEqual(self.call('/api/memory', {'action': 'topic', 'id': 1})[1]['count'], 0)

    def make_topic(self):
        self.ask('第一阶段什么时候验收', [self.plan])
        self.call('/api/memory-update', {'action': 'run'})
        self.assertEqual(self.wait()['result']['kind'], 'done')
        self.assertEqual(self.call('/api/state')[1]['toc_pick'], True)      # 默认开

    def test_toc_pick_adds_pages_keywords_miss(self):
        self.make_topic()
        fake_model.FakeModel.last_toc = {}
        # 关键字对不上任何内容：只靠目录挑页找到「验收安排」
        code, lines = self.ask('那件事定在哪天')
        self.assertEqual(code, 200)
        self.assertIn('验收安排：第一阶段三月底验收（原文：「第一阶段在三月底完成验收」）', self.sent())
        toc = fake_model.FakeModel.last_toc['messages']
        self.assertEqual(toc[0]['content'], fa.TOC_PROMPT)
        self.assertRegex(toc[1]['content'], r'主题页目录：\nT\d+ 验收安排：第一阶段三月底验收\n\n问题：那件事定在哪天$')
        # 关掉开关：不再先看目录，关键字对不上时找不到
        self.assertEqual(self.call('/api/settings', {'toc_pick': False})[1]['toc_pick'], False)
        self.assertFalse(fa.Config(self.config.path).toc_pick)
        fake_model.FakeModel.last_toc = {}
        code, r = self.ask('那件事定在哪天')
        self.assertEqual(code, 400)
        self.assertEqual(fake_model.FakeModel.last_toc, {})

    def test_toc_pick_failure_falls_back_to_keywords(self):
        self.make_topic()
        real = fa.stream_chat

        def chat(tg, msgs, *a, **k):
            if msgs[0]['content'] == fa.TOC_PROMPT:
                raise fa.ModelError('timeout')
            return real(tg, msgs, *a, **k)
        with mock.patch.object(fa, 'stream_chat', side_effect=chat):
            code, lines = self.ask('第一阶段验收')
            self.assertEqual(code, 200)
            self.assertIn('验收安排：第一阶段三月底验收', self.sent())
            code, r = self.ask('那件事定在哪天')
            self.assertEqual(code, 400)
        # 看不懂的回答、不认识的编号同样退回只用关键字
        with mock.patch.object(fa, 'stream_chat', return_value=iter(['{"topics": ["T999", "x"]}'])):
            self.assertEqual(self.handler_pick('问题'), [])
        with mock.patch.object(fa, 'stream_chat', return_value=iter(['看不懂'])):
            self.assertEqual(self.handler_pick('问题'), [])

    def handler_pick(self, question):
        h = fa.Handler.__new__(fa.Handler)
        h.state = self.st
        targets, default = fa.model_targets(self.st.config, self.st.local_models)
        return h.pick_topics(targets[default], question)

    def test_nothing_to_do_and_errors(self):
        self.call('/api/memory-update', {'action': 'run'})
        self.assertEqual(self.wait()['result']['text'], '没有要更新的内容。')
        self.ask('第一阶段什么时候验收', [self.plan])
        self.config.custom['model'] = 'err500'
        self.call('/api/memory-update', {'action': 'run'})
        st = self.wait()
        self.assertEqual(st['result']['kind'], 'error')
        self.assertTrue(st['result']['text'].startswith('更新知识库失败：'))
        self.assertEqual(st['pending'], 1)     # 没写入，留在队里
        self.config.custom['model'] = 'ok'     # 回答不是 JSON：这一批不写入，留待下次
        self.call('/api/memory-update', {'action': 'run'})
        self.assertIn('另有 2 批模型的回答看不懂', self.wait()['result']['text'])
        self.call('/api/settings', {'memory': False})
        self.assertEqual(self.call('/api/memory-update', {'action': 'run'})[0], 400)
        self.assertEqual(self.call('/api/memory-update', {'action': 'nope'})[0], 400)

    def test_auto_when_idle_and_yield_on_ask(self):
        up = self.st.updater
        up.idle = 0
        up.tick()
        self.assertFalse(up.status()['running'])       # 队里没东西
        self.ask('第一阶段什么时候验收', [self.plan])
        self.call('/api/settings', {'memory_auto': False})
        self.assertFalse(fa.Config(self.config.path).memory_auto)
        up.tick()
        self.assertFalse(up.status()['running'])
        self.call('/api/settings', {'memory_auto': True})
        up.idle = 3600
        up.tick()
        self.assertFalse(up.status()['running'])       # 还没空闲够
        # 空闲够了：开始自动更新；模型回答到一半时有人提问，这一批不写入
        up.idle = 0
        real = fa.stream_chat

        def asked_midway(tg, msgs, **kw):
            yield from real(tg, msgs, **kw)
            up.asked()
            yield ''
        with mock.patch.object(fa, 'stream_chat', asked_midway):
            up.tick()
            st = self.wait()
        self.assertEqual((st['mode'], st['result']['kind'], st['pending'], st['history']), ('auto', 'yielded', 1, []))
        up.last_try = 0
        up.tick()
        st = self.wait()
        self.assertEqual((st['result']['kind'], st['pending'], st['history'][0]['mode']), ('done', 0, 'auto'))

    def test_exit_waits_for_manual_only(self):
        self.st.quit.set()
        up = self.st.updater
        up.running, up.mode = True, 'manual'
        self.assertIsNone(self.st.should_exit())
        up.mode = 'auto'
        self.assertEqual(self.st.should_exit(), 'quit requested')
        self.assertTrue(up.halt.is_set())       # 自动更新随退出停下
        up.running = False


class TitleTest(ServerCase):
    def test_title_from_model(self):
        code, r = self.call('/api/title', {'question': '总结要点', 'answer': '你好，世界'})
        self.assertEqual((code, r), (200, {'ok': True, 'title': '季度要点'}))
        sent = fake_model.FakeModel.last_title['messages']
        self.assertEqual(len(sent), 1)
        self.assertIn('问：总结要点', sent[0]['content'])
        self.assertNotIn('甲', sent[0]['content'], '不带文件')

    def test_title_failures(self):
        self.config.custom['model'] = 'titlefail'
        self.assertEqual(self.call('/api/title', {'question': '问', 'answer': '答'})[1], {'ok': False})
        self.config.custom['model'] = 'err500'
        self.assertEqual(self.call('/api/title', {'question': '问', 'answer': '答'})[1], {'ok': False})
        self.assertEqual(self.call('/api/title', {'question': 1, 'answer': '答'})[1], {'ok': False})
        self.assertEqual(self.call('/api/title', {'question': '问', 'answer': '答'}, token='bad')[0], 403)

    def test_title_prompt_follows_question_language(self):
        self.call('/api/title', {'question': 'Summarize the plan', 'answer': 'ok'})
        sent = fake_model.FakeModel.last_title['messages'][0]['content']
        self.assertEqual(sent, fa.TITLE_PROMPT % (fa.TITLE_CHARS, 'Summarize the plan', 'ok'))
        self.assertTrue(fa.TITLE_PROMPT.startswith('把下面这次问答概括成'))
        self.assertIn('用提问的语言写', fa.TITLE_PROMPT)

    def test_clean_title(self):
        self.assertEqual(fa.clean_title('<think>想想</think>\n\n标题：《季度工作总结》。\n解释'), '季度工作总结')
        self.assertEqual(fa.clean_title('一二三四五六七八九十十一'), '一二三四五六七八九十')
        self.assertEqual(fa.clean_title('  \n"…"'), '')


class ShownPathTest(unittest.TestCase):
    def test_shown_path(self):
        roots = ['/home/user/盘/资料', '/home/user/文档/']
        self.assertEqual(fa.shown_path('/home/user/盘/资料/子目录/a.txt', roots), '资料/子目录/a.txt')
        self.assertEqual(fa.shown_path('/home/user/文档/b.md', roots), '文档/b.md')
        self.assertEqual(fa.shown_path('/home/user/盘/资料2/c.txt', roots), 'c.txt', '前缀相同的别的文件夹不算')
        self.assertEqual(fa.shown_path('/elsewhere/d.txt', roots), 'd.txt')
        text = '路径：/home/user/盘/资料/a.txt\n文件名：a.txt\n\n正文'
        self.assertEqual(fa.shown_text('/home/user/盘/资料/a.txt', text, roots), '路径：资料/a.txt\n文件名：a.txt\n\n正文')
        self.assertEqual(fa.shown_text('/home/user/盘/资料/a.txt', '正文', roots), '正文')


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
        self.assertEqual(len(sent['messages']), 2)
        self.assertTrue(sent['messages'][1]['content'].endswith('使用者的问题：\n新问题'))
        self.assertNotIn('旧问题', json.dumps(sent, ensure_ascii=False))




SKILL_MD = '---\nname: %s\ndescription: %s\n---\n\n%s\n'
ECHO_PY = '''import os, sys
print('files=' + os.environ.get('ASK_FILES', '').replace('\\n', '|'))
print('args=' + '|'.join(sys.argv[1:]))
with open(os.path.join(os.environ['ASK_OUT'], 'out.txt'), 'w', encoding='utf-8') as f:
    f.write('生成的内容')
'''
CAT_PY = '''import os, sys
work = os.environ['ASK_WORK']
print('work=' + work)
with open(os.path.join(work, sys.argv[1]), encoding='utf-8') as f:
    text = f.read()
print(text)
with open(os.path.join(os.environ['ASK_OUT'], '副本.md'), 'w', encoding='utf-8') as f:
    f.write(text)
'''


class FrontMatterTest(unittest.TestCase):
    def test_quotes_and_folded(self):
        meta, body = fa.parse_front_matter('---\nname: "a-b"\ndescription: >\n  第一行\n  第二行\nlicense: x\n---\n正文')
        self.assertEqual(meta['name'], 'a-b')
        self.assertEqual(meta['description'], '第一行 第二行')
        self.assertEqual(body, '正文')

    def test_no_front_matter(self):
        self.assertEqual(fa.parse_front_matter('正文')[0], {})
        self.assertEqual(fa.parse_front_matter('---\nname: a\n正文没有结束')[0], {})


class SkillCase(ServerCase):
    """预装与自建 skill 目录都指向临时目录。"""

    def setUp(self):
        super().setUp()
        self.bundled, self.user = os.path.join(self.root, 'bundled'), os.path.join(self.root, 'user')
        patcher = mock.patch.multiple(fa, BUNDLED_SKILLS=self.bundled, USER_SKILLS=self.user)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.make('echo', '运行脚本的测试', '按要求运行脚本。', {'scripts/echo.py': ECHO_PY, 'ref.md': '参考内容'})
        self.make('plain', '仅指令的测试', '用一句话回答。')
        write(os.path.join(self.root, 'secret.txt'), '目录外的内容')

    def make(self, name, desc, body, files=None, base=None):
        d = os.path.join(base or self.bundled, name)
        write(os.path.join(d, 'SKILL.md'), SKILL_MD % (name, desc, body))
        for rel, text in (files or {}).items():
            write(os.path.join(d, rel), text)
        return d

    def ask(self, model, skill='echo', files=None, **extra):
        self.config.custom['model'] = model
        return self.chat([{'role': 'user', 'content': '做吧'}], files=files, skill=skill, **extra)


class LoadSkillsTest(SkillCase):
    def test_user_overrides_bundled_and_bad_skipped(self):
        self.make('plain', '自建的同名 skill', '自建正文', base=self.user)
        write(os.path.join(self.user, 'bad', 'SKILL.md'), '---\nname: bad\n---\n没有说明')
        skills, problems = fa.load_skills()
        self.assertEqual(skills['plain']['source'], 'user')
        self.assertEqual(skills['plain']['body'], '自建正文')
        self.assertNotIn('bad', skills)
        self.assertEqual(len(problems), 1)

    def test_files_and_tools(self):
        skills = fa.load_skills()[0]
        self.assertEqual(skills['echo']['scripts'], ['scripts/echo.py'])
        self.assertEqual(skills['echo']['refs'], ['ref.md'])
        names = [t['function']['name'] for t in fa.skill_tools(skills['echo'])]
        self.assertEqual(names, ['read_skill_file', 'run_script', fa.WRITE_TOOL])
        self.assertIsNone(fa.skill_tools(skills['plain']))

    def test_too_long_flagged(self):
        self.make('big', '很长', '字' * (fa.SKILL_MAX_CHARS + 1))
        self.assertTrue(fa.load_skills()[0]['big']['too_long'])
        code, r = self.ask('ok', skill='big')
        self.assertEqual(code, 400)


    def test_api_lists_skills(self):
        code, r = self.call('/api/skills')
        self.assertEqual(code, 200)
        self.assertEqual([k['name'] for k in r['skills']], ['echo', 'plain'])
        self.assertEqual(r['user_dir'], self.user)


class SkillLabelTest(SkillCase):
    def test_bundled_known_names_get_chinese_label(self):
        self.make('clarify-needs', '需求澄清的测试', '正文')
        self.make('doc-compare', '自建的同名', '正文', base=self.user)
        labels = {k['name']: k['label'] for k in self.call('/api/skills')[1]['skills']}
        self.assertEqual(labels['clarify-needs'], '需求澄清')
        self.assertEqual(labels['doc-compare'], 'doc-compare', '自建 skill 显示原名')
        self.assertEqual(labels['echo'], 'echo')

    def test_every_bundled_skill_has_label(self):
        names = [n for n in os.listdir(os.path.join(TOOL, 'skills')) if not n.startswith('.')]
        self.assertEqual(sorted(names), sorted(fa.BUNDLED_LABELS))


class EmptyAskTest(SkillCase):
    def test_empty_question_with_skill_sends_fixed_ask(self):
        code, lines = self.chat([{'role': 'user', 'content': '  '}], files=[], skill='plain')
        self.assertEqual(code, 200)
        sent = json.loads(urllib.request.urlopen(MODEL_BASE + '/last').read())
        self.assertEqual(sent['messages'][-1]['role'], 'user')
        self.assertTrue(sent['messages'][-1]['content'].endswith('使用者的问题：\n' + fa.EMPTY_ASK))

    def test_empty_question_without_skill_rejected(self):
        code, r = self.chat([{'role': 'user', 'content': ''}])
        self.assertEqual(code, 400)
        self.assertIn('要问的内容', r['error'])


class BundledSkillsTest(unittest.TestCase):
    def test_all_bundled_skills_load(self):
        skills, problems = fa.load_skills(((os.path.join(TOOL, 'skills'), 'bundled'),))
        self.assertEqual(problems, [])
        for name in ('summarize-meeting', 'doc-compare', 'office-gen', 'prompt-optimize', 'clarify-needs'):
            self.assertIn(name, skills)
            self.assertFalse(skills[name]['too_long'])
        for name in ('summarize-meeting', 'prompt-optimize', 'clarify-needs'):
            self.assertEqual(skills[name]['refs'], [])


class SkillChatTest(SkillCase):
    def sent(self):
        return json.loads(urllib.request.urlopen(MODEL_BASE + '/last').read())

    def test_instruction_only_skill(self):
        code, lines = self.ask('ok', skill='plain')
        self.assertEqual(code, 200)
        self.assertEqual(lines[0]['skill'], 'plain')
        sent = self.sent()
        self.assertNotIn('tools', sent)
        system = sent['messages'][0]['content']
        self.assertLess(system.index('用一句话回答。'), system.index(fa.CITE_RULE))
        # 选了 skill 时格式按 skill 的说明
        self.assertNotIn(fa.FORMAT_RULE, system)
        self.assertTrue(sent['messages'][-1]['content'].endswith(fa.CITE_REMINDER + '\n\n使用者的问题：\n做吧'))

    def test_no_files_needs_skill(self):
        code, r = self.call('/api/chat', {'files': [], 'messages': [{'role': 'user', 'content': '做吧'}],
                                          'model': 'custom'})
        self.assertEqual(code, 400)
        code, raw = self.call('/api/chat', {'files': [], 'messages': [{'role': 'user', 'content': '做吧'}],
                                            'model': 'custom', 'skill': 'plain'})
        self.assertEqual(code, 200)
        lines = [json.loads(x) for x in raw.splitlines() if x.strip()]
        self.assertNotIn(fa.CITE_RULE, self.sent()['messages'][0]['content'])
        self.assertFalse(lines[-1]['uncited'])

    def test_unknown_skill(self):
        code, r = self.ask('ok', skill='nope')
        self.assertEqual(code, 400)
        self.assertIn('重新读取', r['error'])

    def test_skill_counts_toward_limit(self):
        self.config.char_limit = 110
        self.make('wordy', '长说明', '说' * 60)
        code, r = self.ask('ok', skill='wordy')
        self.assertEqual(code, 413)
        self.assertEqual(r['skill'], 60)

    def test_run_script_and_save_output(self):
        doc = os.path.join(self.docs, '季度总结.txt')
        code, lines = self.ask('call:run_script:{"script": "scripts/echo.py", "args": ["甲 乙"]}', files=[doc])
        self.assertEqual(code, 200)
        self.assertTrue(any('tool_start' in m for m in lines))
        tool = next(m['tool'] for m in lines if 'tool' in m)
        self.assertEqual(tool['exit'], 0)
        self.assertIn('files=' + doc, tool['output'])
        self.assertIn('args=甲 乙', tool['output'])
        self.assertEqual([f['name'] for f in tool['files']], ['out.txt'])
        answer = ''.join(m.get('t', '') for m in lines)
        self.assertIn('结果：退出码 0', answer)
        self.assertIn('out.txt', answer)
        h = {'X-Token': 'tok'}
        req = urllib.request.Request(self.base + '/api/output?id=' + tool['files'][0]['id'], headers=h)
        with urllib.request.urlopen(req) as r:
            self.assertEqual(r.read().decode('utf-8'), '生成的内容')
        code, r = self.call('/api/output?id=nope')
        self.assertEqual(code, 404)
        self.st.cleanup()
        self.assertFalse(os.path.exists(self.st.out_root))

    def test_read_skill_file(self):
        code, lines = self.ask('call:read_skill_file:{"path": "ref.md"}')
        tool = next(m['tool'] for m in lines if 'tool' in m)
        self.assertEqual(tool['output'], '参考内容')
        self.assertIn('结果：参考内容', ''.join(m.get('t', '') for m in lines))

    def test_paths_cannot_leave_skill(self):
        for args in ('{"path": "../../secret.txt"}', '{"path": "%s"}' % os.path.join(self.root, 'secret.txt')):
            code, lines = self.ask('call:read_skill_file:' + args)
            tool = next(m['tool'] for m in lines if 'tool' in m)
            self.assertIn('error', tool)
            self.assertNotIn('目录外的内容', json.dumps(lines, ensure_ascii=False))

    def test_only_scripts_run(self):
        code, lines = self.ask('call:run_script:{"script": "ref.md"}')
        tool = next(m['tool'] for m in lines if 'tool' in m)
        self.assertIn('.py', tool['error'])

    def test_bad_arguments(self):
        code, lines = self.ask('call:run_script:{"script": "scripts/echo.py", "args": "不是列表"}')
        tool = next(m['tool'] for m in lines if 'tool' in m)
        self.assertIn('args', tool['error'])
        code, lines = self.ask('call:nope:{}')
        tool = next(m['tool'] for m in lines if 'tool' in m)
        self.assertIn('没有这个工具', tool['error'])

    def test_errors_tell_model_how_to_fix(self):
        # 交给模型的报错写明原因和怎么改；页面上的运行记录只写原因
        cases = [('call:read_skill_file:{"path": "nope.md"}', ['skill 目录里没有这个文件：nope.md', '可读的文件：', 'ref.md']),
                 ('call:run_script:{"script": "ref.md"}', ['只能运行 .py 或 .sh 脚本', '可运行的脚本：scripts/echo.py']),
                 ('call:nope:{}', ['没有这个工具：nope', '只能用：read_skill_file、run_script、write_work_file']),
                 ('call:read_skill_file:{bad', ['参数不是合法的 JSON', '请按工具说明给出一个 JSON 对象'])]
        for model, want in cases:
            code, lines = self.ask(model)
            result = next(m['t'] for m in lines if m.get('t', '').startswith('结果：'))
            for w in want:
                self.assertIn(w, result, model)
            tool = next(m['tool'] for m in lines if 'tool' in m)
            self.assertNotIn('fix', tool)

    def test_tool_descriptions(self):
        tools = {t['function']['name']: t['function']['description'] for t in fa.skill_tools(
            {'refs': ['ref.md'], 'scripts': ['scripts/echo.py']})}
        self.assertEqual(tools['read_skill_file'], fa.READ_TOOL_DESC + 'ref.md')
        self.assertEqual(tools['run_script'], fa.RUN_TOOL_DESC + 'scripts/echo.py')
        self.assertEqual(tools[fa.WRITE_TOOL], fa.WRITE_TOOL_DESC)
        for d in tools.values():
            self.assertIn('{"', d)      # 每个工具都给了参数示例

    def test_tool_call_limit(self):
        code, lines = self.ask('loop:read_skill_file:{"path": "ref.md"}')
        self.assertEqual(sum(1 for m in lines if 'tool' in m), fa.MAX_TOOL_CALLS)
        self.assertTrue(lines[-1].get('done'))
        sent = self.sent()
        self.assertNotIn('tools', sent)
        self.assertEqual(sent['messages'][-1]['content'], fa.TOOL_LIMIT_NOTE)

    def seq(self, *steps, **kw):
        return self.ask('seq:' + json.dumps([list(s) for s in steps], ensure_ascii=False), **kw)

    def test_write_then_run_reads_work_dir(self):
        self.make('cat', '读工作目录', '运行', {'scripts/cat.py': CAT_PY})
        code, lines = self.seq(('write_work_file', {'path': '稿/正文.md', 'content': '第一段'}),
                               ('write_work_file', {'path': '稿/正文.md', 'content': '，第二段', 'append': True}),
                               ('run_script', {'script': 'scripts/cat.py', 'args': ['稿/正文.md']}), skill='cat')
        self.assertEqual(code, 200)
        tools = [m['tool'] for m in lines if 'tool' in m]
        self.assertEqual([t['kind'] for t in tools], ['write', 'write', 'run'])
        self.assertEqual((tools[0]['chars'], tools[0]['append']), (3, False))
        self.assertEqual((tools[1]['total'], tools[1]['append'], tools[1]['output']), (7, True, '，第二段'))
        self.assertIn('第一段，第二段', tools[2]['output'])
        self.assertEqual([f['name'] for f in tools[2]['files']], ['副本.md'])
        work = tools[2]['output'].split('work=')[1].split('\n')[0]
        self.assertTrue(work.startswith(self.st.out_root))
        self.assertFalse(os.path.exists(work), '回答结束后删除工作目录')
        self.assertIn('已追加到 稿/正文.md', ''.join(m.get('t', '') for m in lines))

    def test_write_paths_and_size(self):
        d = tempfile.mkdtemp(dir=self.root)
        os.symlink(self.root, os.path.join(d, 'out'))
        for rel in ('../x.md', '/tmp/x.md', 'out/x.md', '.', '', 'a/../../x.md'):
            with self.assertRaises(ValueError, msg=rel):
                fa.write_work_file(d, rel, '内容')
        with self.assertRaises(ValueError):
            fa.write_work_file(d, 'a.md', None)
        self.assertEqual(fa.write_work_file(d, 'a.md', '字' * 10), ('a.md', 10, 10))
        with mock.patch.object(fa, 'MAX_WORK_CHARS', 15):
            with self.assertRaises(ValueError):
                fa.write_work_file(d, 'a.md', '字' * 6, append=True)
            self.assertEqual(fa.write_work_file(d, 'a.md', '字' * 15)[2], 15)
        self.assertFalse(os.path.exists(os.path.join(self.root, 'x.md')))

    def test_write_counted_separately(self):
        code, lines = self.ask('loop:write_work_file:{"path": "a.md", "content": "x"}')
        tools = [m['tool'] for m in lines if 'tool' in m]
        self.assertEqual(len(tools), fa.MAX_WRITE_CALLS)
        self.assertTrue(lines[-1].get('done'))
        tool_msgs = [m['content'] for m in self.sent()['messages'] if m.get('role') == 'tool']
        self.assertEqual(tool_msgs.count(fa.WRITE_LIMIT_NOTE), fa.MAX_TOOL_CALLS)

    def test_write_needs_scripts(self):
        code, lines = self.ask('call:write_work_file:{"path": "a.md", "content": "x"}', skill='plain')
        self.assertFalse(any('tool' in m for m in lines))

    def test_model_without_tool_support(self):
        code, lines = self.ask('notools')
        self.assertIn('不支持 skill 工具', lines[-1]['error'])

    def test_script_timeout(self):
        d = self.make('slow', '慢脚本', '运行', {'scripts/slow.py': 'import time\ntime.sleep(30)\n'})
        skill = fa.load_skills()[0]['slow']
        t0 = time.monotonic()
        r = fa.run_skill_script(skill, 'scripts/slow.py', [], {}, tempfile.mkdtemp(dir=d), timeout=0.5)
        self.assertTrue(r['timed_out'])
        self.assertLess(time.monotonic() - t0, 10)

    def test_output_clipped(self):
        self.make('loud', '输出很多', '运行', {'scripts/loud.py': 'print("字" * 50000)\n'})
        skill = fa.load_skills()[0]['loud']
        r = fa.run_skill_script(skill, 'scripts/loud.py', [], {}, tempfile.mkdtemp(dir=self.root))
        self.assertLess(len(r['output']), fa.TOOL_OUTPUT_CHARS + 100)
        self.assertIn('已截断', r['output'])


class OfficeGenTest(SkillCase):
    """预装 office-gen：模型写入 Markdown/JSON 后运行脚本生成 docx/xlsx。"""

    def setUp(self):
        super().setUp()
        shutil.copytree(os.path.join(TOOL, 'skills', 'office-gen'), os.path.join(self.bundled, 'office-gen'))

    def test_docx_and_xlsx(self):
        rows = {'sheets': [{'name': '明细', 'rows': [['部门', '金额'], ['财务', 100]]}]}
        code, lines = self.seq(
            ('write_work_file', {'path': '报告.md', 'content': '# 标题\n\n正文'}),
            ('run_script', {'script': 'scripts/make_docx.py', 'args': ['报告.md', '--out', '季度报告']}),
            ('write_work_file', {'path': '数据.json', 'content': json.dumps(rows, ensure_ascii=False)}),
            ('run_script', {'script': 'scripts/make_xlsx.py', 'args': ['数据.json']}), skill='office-gen')
        self.assertEqual(code, 200)
        runs = [m['tool'] for m in lines if 'tool' in m and m['tool']['kind'] == 'run']
        self.assertEqual([r['exit'] for r in runs], [0, 0], runs)
        self.assertEqual([f['name'] for r in runs for f in r['files']], ['季度报告.docx', '数据.xlsx'])

    seq = SkillChatTest.seq


class SaveDocxTest(ServerCase):
    """「保存为 Word」：/api/docx 把 Markdown 转成 docx 直接返回。"""

    def post(self, body, token='tok'):
        req = urllib.request.Request(self.base + '/api/docx', data=json.dumps(body).encode('utf-8'),
                                     headers={'X-Token': token, 'Content-Type': 'application/json'}, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, r.headers.get('Content-Type'), r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get('Content-Type'), e.read()

    def test_converts(self):
        code, ctype, body = self.post({'markdown': '# 结论\n\n- 甲\n- 乙', 'title': '季度 & 总结'})
        self.assertEqual(code, 200)
        self.assertEqual(ctype, fa.DOCX_TYPE)
        doc = zipfile.ZipFile(io.BytesIO(body)).read('word/document.xml').decode('utf-8')
        for want in ('季度 &amp; 总结', 'Heading1', '结论', '乙'):
            self.assertIn(want, doc)

    def test_rejects(self):
        self.assertEqual(self.post({'markdown': '  '})[0], 400)
        self.assertEqual(self.post({'markdown': 'x'}, token='bad')[0], 403)
        with mock.patch.object(fa, 'DOCX_SCRIPT', os.path.join(self.root, 'none.py')), \
                mock.patch.object(fa, '_docx', []):
            code, _, body = self.post({'markdown': 'x'})
        self.assertEqual(code, 500)
        self.assertIn('找不到', json.loads(body)['error'])


class LocalPathTest(ServerCase):
    """文件夹输入框接受 file:/// 地址。"""

    def url(self, path):
        return 'file://' + urllib.parse.quote(path)

    def test_escapes_quotes_and_trailing_slash(self):
        self.assertEqual(fa.local_path(self.url(self.docs) + '/'), self.docs)
        self.assertEqual(fa.local_path('file:///home/user/%E6%96%87%E6%A1%A3'), '/home/user/文档')
        self.assertEqual(fa.norm_folder(' "%s/" ' % self.url(self.docs)), self.docs)
        self.assertEqual(fa.local_path('~/文档'), '~/文档')

    def test_host_or_bad_escape_rejected(self):
        for bad in ('file://localhost' + self.docs, 'file://server/share', 'file:/tmp',
                    'file:///tmp/%E6%96', 'file:///tmp/%zz', 'file:///tmp/%00'):
            with self.assertRaises(ValueError, msg=bad) as cm:
                fa.norm_folder(bad)
            self.assertEqual(str(cm.exception), fa.FILE_URL_ERROR)

    def test_add_folder_by_file_url(self):
        other = os.path.join(self.root, '文档')
        os.makedirs(other)
        code, r = self.call('/api/folders', {'add': self.url(other) + '/'})
        self.assertEqual(code, 200)
        self.assertIn(other, r['folders'])
        code, r = self.call('/api/folders', {'add': 'file://localhost' + other})
        self.assertEqual(code, 400)
        self.assertEqual(r['error'], fa.FILE_URL_ERROR)

    def test_completion_writes_plain_path(self):
        sub = os.path.join(self.docs, '子目录')
        for prefix in (self.url(self.docs) + '/', self.url(os.path.join(self.docs, '子'))):
            code, r = self.call('/api/dirs?prefix=' + q(prefix))
            self.assertEqual(r['dirs'], [sub], prefix)
        self.assertEqual(self.call('/api/dirs?prefix=' + q('file://h/x'))[1]['dirs'], [])

    def test_start_dir(self):
        self.assertEqual(fa.start_dir(self.url(self.docs)), self.docs)
        self.assertEqual(fa.start_dir(self.docs + '/'), self.docs)
        self.assertEqual(fa.start_dir(os.path.join(self.docs, '不存在')), fa.HOME)
        self.assertEqual(fa.start_dir('file://h/x'), fa.HOME)
        self.assertEqual(fa.start_dir(''), fa.HOME)


class SameNameTest(unittest.TestCase):
    def test_cases(self):
        with open(os.path.join(HERE, 'same_name_cases.json'), encoding='utf-8') as f:
            cases = json.load(f)
        for group in cases['same']:
            self.assertEqual(len({fa.same_name_key(n) for n in group}), 1, group)
        for a, b in cases['different']:
            self.assertNotEqual(fa.same_name_key(a), fa.same_name_key(b), (a, b))

    def test_keep_newest_then_shorter_path(self):
        entries = [('/a/报告 (1).txt', '', 1, 200), ('/a/长目录/报告.txt', '', 1, 100), ('/a/报告.txt', '', 1, 100),
                   ('/a/其他.txt', '', 1, 50), ('/a/长目录/其他 - 副本.txt', '', 1, 50), ('/a/其他 copy.txt', '', 1, 50)]
        out = [(e[0], [o[0] for o in others]) for e, others in fa.dedup_entries(entries)]
        self.assertEqual(out, [('/a/报告 (1).txt', ['/a/报告.txt', '/a/长目录/报告.txt']),
                               ('/a/其他.txt', ['/a/其他 copy.txt', '/a/长目录/其他 - 副本.txt'])])
        # 修改时间相同时留路径较短的，排在它原来的位置
        out = fa.dedup_entries([('/a/长目录/报告.txt', '', 1, 100), ('/a/报告.txt', '', 1, 100)])
        self.assertEqual([e[0] for e, _ in out], ['/a/报告.txt'])


class BrowseDedupTest(ServerCase):
    def setUp(self):
        super().setUp()
        for rel, mtime in (('报告.txt', 1000), ('报告 (1).txt', 2000), ('报告 - 副本.txt', 500),
                           (os.path.join('子目录', '报告_副本.txt'), 3000)):
            path = os.path.join(self.docs, rel)
            write(path, '丁')
            os.utime(path, (mtime, mtime))
        self.index.refresh(full=True, wait=True)

    def browse(self, dedup):
        return self.call('/api/browse?dir=%s&dedup=%s' % (q(self.docs), dedup))[1]

    def test_dedup_layer_and_tree(self):
        r = self.browse('1')
        self.assertEqual((r['here'], r['here_dedup']), (7, 4))
        # 跨层去重：留子目录里最新的那个
        rep = next(f for f in r['files'] if f['name'].startswith('报告'))
        self.assertEqual(rep['name'], '报告_副本.txt')
        self.assertEqual([d['name'] for d in rep['dups']], ['报告 (1).txt', '报告.txt', '报告 - 副本.txt'])
        # 含子文件夹时跨层去重：留子目录里最新的那个；读不了的资料包.zip 不算
        self.assertEqual(r['tree_total'], 3)
        self.assertEqual(sorted(f['name'] for f in r['tree']), ['会议记录.md', '季度总结.txt', '报告_副本.txt'])

    def test_off(self):
        r = self.browse('0')
        self.assertEqual((r['here'], r['here_dedup']), (7, 7))
        self.assertFalse(any(f.get('dups') for f in r['files']))
        self.assertEqual(r['tree_total'], 6)

    def test_default_follows_setting(self):
        self.assertEqual(self.call('/api/browse?dir=' + q(self.docs))[1]['here_dedup'], 4)
        self.config.dedup = False
        self.assertEqual(self.call('/api/browse?dir=' + q(self.docs))[1]['here_dedup'], 7)

    def test_search_results_carry_mtime(self):
        r = self.call('/api/search?q=' + q('报告'))[1]
        self.assertEqual({x['name']: x['ts'] for x in r['results']}['报告 (1).txt'], 2000)


class DedupSettingTest(ServerCase):
    def test_default_on_and_saved(self):
        self.assertTrue(self.call('/api/state')[1]['dedup'])
        code, r = self.call('/api/settings', {'dedup': False})
        self.assertEqual(code, 200)
        self.assertFalse(r['dedup'])
        self.assertFalse(fa.Config(self.config.path).dedup)
        self.assertTrue(fa.Config(os.path.join(self.root, '没有.json')).dedup)


class PickerTest(ServerCase):
    """原生目录选择：用 tests/fake_picker.sh 冒充 zenity、kdialog。"""

    def setUp(self):
        super().setUp()
        self.bin = os.path.join(self.root, 'bin')
        os.makedirs(self.bin)
        self.args = os.path.join(self.root, 'args.txt')
        self.env = mock.patch.dict(os.environ, {'PATH': self.bin + os.pathsep + os.environ.get('PATH', ''),
                                                'FAKE_PICK': self.docs, 'FAKE_PICK_ARGS': self.args})
        self.env.start()
        self.addCleanup(self.env.stop)

    def fake(self, name):
        os.symlink(os.path.join(HERE, 'fake_picker.sh'), os.path.join(self.bin, name))

    def pick(self, start=''):
        return self.call('/api/pick-folder', {'start': start, 'title': '选择文件夹', 'page': 'p1'})

    def test_detect(self):
        path = {'PATH': self.bin}
        self.assertEqual(fa.detect_picker(path), '')       # 没有图形环境
        with mock.patch.object(fa.importlib.util, 'find_spec', return_value=None):
            self.assertEqual(fa.detect_picker(dict(path, DISPLAY=':0')), '')
        with mock.patch.object(fa.importlib.util, 'find_spec', return_value=object()):
            self.assertEqual(fa.detect_picker(dict(path, DISPLAY=':0')), 'tkinter')
            self.assertEqual(fa.detect_picker(dict(path, WAYLAND_DISPLAY='w')), '')
            self.fake('kdialog')
            self.assertEqual(fa.detect_picker(dict(path, DISPLAY=':0')), 'kdialog')
            self.fake('zenity')
            self.assertEqual(fa.detect_picker(dict(path, WAYLAND_DISPLAY='w')), 'zenity')

    def test_state_reports_picker(self):
        self.st.picker = fa.FolderPicker('')
        self.assertFalse(self.call('/api/state')[1]['picker'])
        code, r = self.pick()
        self.assertFalse(r['ok'])
        self.st.picker = fa.FolderPicker('zenity')
        self.assertTrue(self.call('/api/state')[1]['picker'])

    def test_zenity_pick_from_existing_folder(self):
        self.fake('zenity')
        self.st.picker = fa.FolderPicker('zenity')
        code, r = self.pick('file://' + urllib.parse.quote(self.docs))
        self.assertEqual((code, r), (200, {'ok': True, 'path': self.docs}))
        with open(self.args, encoding='utf-8') as f:
            self.assertIn('--filename=' + self.docs + '/', f.read().splitlines())
        self.pick(os.path.join(self.docs, '不存在'))
        with open(self.args, encoding='utf-8') as f:
            self.assertIn('--filename=' + fa.HOME.rstrip('/') + '/', f.read().splitlines())

    def test_kdialog_cancel_and_failure(self):
        self.fake('kdialog')
        self.st.picker = fa.FolderPicker('kdialog')
        os.environ['FAKE_PICK'] = 'cancel'
        self.assertEqual(self.pick()[1], {'ok': True, 'path': None})
        os.environ['FAKE_PICK'] = 'fail'
        r = self.pick()[1]
        self.assertFalse(r['ok'])
        self.assertIn('cannot open display', r['error'])

    def start_slow(self, seconds):
        self.fake('zenity')
        self.st.picker = fa.FolderPicker('zenity')
        os.environ['FAKE_PICK_SLEEP'] = str(seconds)
        out = {}
        t = threading.Thread(target=lambda: out.update(r=self.pick()), daemon=True)
        t.start()
        for _ in range(100):
            if self.st.picker.busy():
                break
            time.sleep(0.05)
        self.assertTrue(self.st.picker.busy())
        return t, out

    def test_only_one_at_a_time_and_no_exit_while_open(self):
        t, out = self.start_slow(1)
        code, r = self.pick()
        self.assertEqual(code, 409)
        self.assertIn('已有选择窗口打开', r['error'])
        self.st.quit.set()
        self.assertIsNone(self.st.should_exit())
        t.join(10)
        self.assertEqual(out['r'], (200, {'ok': True, 'path': self.docs}))
        self.assertFalse(self.st.picker.busy())

    def test_page_closed_ends_picker(self):
        t, out = self.start_slow(30)
        t0 = time.monotonic()
        self.call('/api/bye', {'t': 'tok', 'page': 'p1'}, token=None)
        t.join(10)
        self.assertLess(time.monotonic() - t0, 5)
        self.assertEqual(out['r'][1], {'ok': True, 'path': None})

    def test_cancel_ends_picker(self):
        # 没有选择窗口时取消也返回 ok
        self.assertEqual(self.call('/api/pick-cancel', {'page': 'p1'}), (200, {'ok': True}))
        t, out = self.start_slow(30)
        # 令牌、Origin 不对时拒绝，选择窗口照旧开着
        self.assertEqual(self.call('/api/pick-cancel', {'page': 'p1'}, token='bad')[0], 403)
        self.assertEqual(self.call('/api/pick-cancel', {'page': 'p1'}, headers={'Origin': 'http://evil.example'})[0], 403)
        # 别的页面取消不影响这个页面打开的窗口
        self.assertEqual(self.call('/api/pick-cancel', {'page': 'p2'}), (200, {'ok': True}))
        time.sleep(0.5)
        self.assertTrue(self.st.picker.busy())
        t0 = time.monotonic()
        self.assertEqual(self.call('/api/pick-cancel', {'page': 'p1'}), (200, {'ok': True}))
        t.join(10)
        self.assertLess(time.monotonic() - t0, 5)
        self.assertEqual(out['r'], (200, {'ok': True, 'path': None}))
        self.assertFalse(self.st.picker.busy())


class PageVersionTest(unittest.TestCase):
    def test_footer_default_matches_version(self):
        with open(os.path.join(TOOL, 'ask_local_files.html'), encoding='utf-8') as f:
            self.assertIn('<span id="ver">资料管家 v%s</span>' % fa.VERSION, f.read())

    def test_help_matches_version(self):
        # 帮助页随行为一起更新：页脚版本号须与 VERSION 一致
        with open(os.path.join(TOOL, 'help.html'), encoding='utf-8') as f:
            page = f.read()
        self.assertIn('<span id="ver">资料管家 帮助 v%s</span>' % fa.VERSION, page)
        self.assertIn('<title>资料管家 帮助</title>', page)
        self.assertNotRegex(page, r'(?i)(src|href)=["\']https?://')      # 不加载网络资源

    def test_help_prompts_match_code(self):
        # 帮助页「原理与提示词」里的提示词全文与代码常量逐字一致
        import html
        with open(os.path.join(TOOL, 'help.html'), encoding='utf-8') as f:
            page = f.read()
        found = dict(re.findall(r'<pre data-prompt="(\w+)">(.*?)</pre>', page, re.S))
        want = fa.prompt_texts()
        self.assertEqual(sorted(found), sorted(want))
        for name, text in found.items():
            self.assertEqual(html.unescape(text), want[name], name)
        # 一个不漏：发给模型的提示词常量都列在帮助页（SCRIPT_NOTE 只是 SKILL_CITE_NOTE 里的一个词）
        consts = {k for k, v in vars(fa).items() if isinstance(v, str) and
                  re.search(r'_(RULE|NOTE|PROMPT|INTRO|REMINDER|DESC|EXAMPLE)$', k)} - {'SCRIPT_NOTE'}
        self.assertLessEqual(consts, set(want))
        self.assertEqual(want['TITLE_PROMPT'], fa.TITLE_PROMPT % (fa.TITLE_CHARS, '〈问题〉', '〈回答节选〉'))
        self.assertNotIn('%s', ''.join(want.values()))
        self.assertNotIn('%d', ''.join(want.values()))
        self.assertIn('id="principles"', page)
        self.assertIn('href="#principles"', page)

    def test_desktop_name(self):
        with open(os.path.join(TOOL, '资料管家.desktop'), encoding='utf-8') as f:
            self.assertIn('Name=资料管家\n', f.read())


class HelpPageTest(ServerCase):
    def test_help_served(self):
        code, page = self.call('/help.html', token=None)
        self.assertEqual(code, 200)
        self.assertIn('资料管家 帮助', page)
        self.assertEqual(self.call('/help.html', token=None, headers={'Origin': 'http://evil.example'})[0], 403)


class AutoIngestTest(ServerCase):
    """自动收录到知识库：勾选文件夹、后台收录、未收录清单、文件改动与删除、抽结论、容量与保存为 Markdown。"""

    model = 'memo'

    def setUp(self):
        super().setUp()
        write(os.path.join(self.docs, '计划.txt'), '第一阶段在三月底完成验收。\n\n预算总额为一百万元。')
        with open(os.path.join(self.docs, '坏文件.docx'), 'wb') as f:
            f.write(b'not a zip')
        # 文件夹刚扫过：同一时间刻度里写入时目录修改时间不变，增量扫描会沿用旧清单，所以全量重扫
        self.index.refresh(full=True, wait=True)

    def wait(self):
        for _ in range(200):
            st = self.call('/api/memory-ingest')[1]
            if not st['running']:
                return st
            time.sleep(0.05)
        raise AssertionError('自动收录没有结束')

    def wait_summary(self):
        # 扫描结束后才由索引线程开始收录：等到有收录结果
        for _ in range(200):
            st = self.wait()
            if st['summary'] is not None:
                return st['summary']
            time.sleep(0.05)
        raise AssertionError('没有开始自动收录')

    def turn_on(self):
        code, r = self.call('/api/folders', {'auto': self.docs, 'on': True})
        self.assertEqual((code, r['auto_folders']), (200, [self.docs]))
        return self.wait()

    def test_toggle_ingest_and_skipped(self):
        st = self.call('/api/memory-ingest')[1]
        self.assertEqual((st['running'], st['summary'], st['folders']), (False, None, []))
        self.assertEqual(self.call('/api/memory-ingest', {'action': 'run'})[0], 400)     # 还没勾选
        self.assertEqual(self.call('/api/folders', {'auto': '/nope', 'on': True})[0], 400)
        st = self.turn_on()
        self.assertEqual(fa.Config(self.config.path).auto_folders, [self.docs])
        sm = st['summary']
        # 季度总结.txt、会议记录.md、计划.txt、坏文件.docx；压缩包不算
        self.assertEqual((sm['total'], sm['done'], sm['waiting'], sm['skipped_total']), (4, 3, 0, 1))
        self.assertEqual(sm['skipped'][0]['path'], os.path.join(self.docs, '坏文件.docx'))
        self.assertEqual(sm['skipped'][0]['state'], 'failed')
        stats = self.call('/api/memory')[1]['stats']
        self.assertEqual((stats['files'], stats['extract']), (3, 3))
        self.assertIn('自动收录：新收录 3 个文件，1 个没有收录。', st['result']['text'])
        # 忽略 Markdown 文件：.md 不再算进来
        self.call('/api/settings', {'ignore_md': True})
        self.assertEqual(self.wait()['summary']['total'], 3)
        # 改了的文件重新收录；删掉的文件移出
        write(os.path.join(self.docs, '计划.txt'), '第一阶段改到四月底验收。')
        os.remove(os.path.join(self.docs, '季度总结.txt'))
        self.index.refresh(full=True, wait=True)
        self.wait()
        self.assertEqual(self.call('/api/memory-ingest', {'action': 'run'})[0], 200)
        sm = self.wait()['summary']
        self.assertEqual((sm['total'], sm['done']), (2, 1))
        paths = [f['path'] for f in self.call('/api/memory')[1]['files']]
        self.assertNotIn(os.path.join(self.docs, '季度总结.txt'), paths)
        # 取消勾选：不再收录，已收录的保留
        code, r = self.call('/api/folders', {'auto': self.docs, 'on': False})
        self.assertEqual((code, r['auto_folders'], r['ingest']['summary']), (200, [], None))
        self.assertGreater(self.call('/api/memory')[1]['stats']['files'], 0)
        # 知识库关闭时不能打开
        self.call('/api/settings', {'memory': False})
        self.assertEqual(self.call('/api/folders', {'auto': self.docs, 'on': True})[0], 400)

    def test_added_folder_defaults_to_auto(self):
        # 已有的文件夹保持原状态（setUp 里加的 docs 没有勾选），新添加的默认勾选并开始收录
        other = os.path.join(self.root, '新资料')
        os.makedirs(other)
        write(os.path.join(other, '安排.txt'), '第二阶段在六月底完成。')
        code, r = self.call('/api/folders', {'add': other})
        self.assertEqual((code, r['auto_folders']), (200, [other]))
        self.assertEqual(fa.Config(self.config.path).auto_folders, [other])
        self.index.refresh(wait=True)
        sm = self.wait_summary()
        self.assertEqual((sm['total'], sm['done']), (1, 1))
        self.assertNotIn(self.docs, self.call('/api/memory-ingest')[1]['folders'])

    def test_added_folder_auto_while_memory_off(self):
        # 知识库关闭时添加的文件夹同样记为勾选，但不收录；打开知识库后开始收录
        self.call('/api/settings', {'memory': False})
        other = os.path.join(self.root, '新资料')
        os.makedirs(other)
        write(os.path.join(other, '安排.txt'), '第二阶段在六月底完成。')
        code, r = self.call('/api/folders', {'add': other})
        self.assertEqual((code, r['auto_folders']), (200, [other]))
        self.index.refresh(wait=True)
        self.assertIsNone(self.wait()['summary'])
        self.assertEqual(self.call('/api/memory')[1]['stats']['files'], 0)
        self.call('/api/settings', {'memory': True})
        sm = self.wait_summary()
        self.assertEqual((sm['total'], sm['done']), (1, 1))

    def test_remove_folder_drops_auto(self):
        self.turn_on()
        code, r = self.call('/api/folders', {'remove': self.docs})
        self.assertEqual((code, r['auto_folders']), (200, []))

    def test_extract_then_topics_and_export(self):
        self.assertEqual(self.call('/api/memory-export')[0], 400)       # 还没有主题页和结论
        self.turn_on()
        self.call('/api/memory-update', {'action': 'run'})
        for _ in range(200):
            st = self.call('/api/memory-update')[1]
            if not st['running']:
                break
            time.sleep(0.05)
        self.assertEqual(st['result']['kind'], 'done')
        self.assertIn('从文件抽出结论', st['result']['text'])
        self.assertIn('新建主题页 1 页', st['result']['text'])       # 抽出的结论接着整理进主题页
        facts = self.call('/api/memory')[1]['facts']
        self.assertIn('第一阶段在三月底完成验收。', [f['quote'] for f in facts])
        self.assertEqual(st['pending'], 0)
        req = urllib.request.Request(self.base + '/api/memory-export', headers={'X-Token': 'tok'})
        with urllib.request.urlopen(req, timeout=20) as r:
            self.assertEqual(r.headers['Content-Type'], 'application/zip')
            disp = r.headers['Content-Disposition']
            self.assertRegex(urllib.parse.unquote(disp.split("''", 1)[1]), r'^知识库-\d{8}-\d{4}\.zip$')
            names = zipfile.ZipFile(io.BytesIO(r.read())).namelist()
        self.assertIn('索引.md', names)
        self.assertIn('主题/验收安排.md', names)
        self.assertIn('文件/计划.txt.md', names)

    def test_capacity_setting(self):
        self.assertEqual(self.call('/api/settings', {'memory_mb': 10})[0], 400)
        self.assertEqual(self.call('/api/settings', {'memory_mb': True})[0], 400)
        code, r = self.call('/api/settings', {'memory_mb': 2048})
        self.assertEqual((code, r['memory_mb']), (200, 2048))
        self.assertEqual(fa.Config(self.config.path).memory_mb, 2048)
        self.assertEqual(self.memory.max_bytes, 2048 * 1024 * 1024)
        self.assertEqual(fa.Config(os.path.join(self.root, 'none.json')).memory_mb, fa.MEMORY_MB)

    def test_exit_not_held(self):
        self.st.quit.set()
        self.st.ingest.running = True
        self.assertEqual(self.st.should_exit(), 'quit requested')     # 自动收录不拖住退出
        self.st.ingest.running = False


class OpencodeTest(ServerCase):
    """配置 OpenCode：只替换所选服务商的 AK，测试通过才写入，可回滚。"""

    def setUp(self):
        super().setUp()
        self.dir = os.path.join(self.root, 'opencode')
        os.makedirs(self.dir)
        self.patch = mock.patch.object(fa, 'OPENCODE_DIR', self.dir)
        self.patch.start()
        self.path = os.path.join(self.dir, 'opencode.jsonc')
        self.text = ('{\n  // 说明\n  "provider": {\n    "corp": {"options": {"baseURL": "%s", /* 注释 */ "apiKey": "old"},\n'
                     '             "models": {"ok": {}, "err401": {}}},\n    "nokey": {"options": {"baseURL": "%s"}, '
                     '"models": {"ok": {}}},\n  },\n  "model": "corp/ok"\n}\n') % (MODEL_BASE, MODEL_BASE)

    def tearDown(self):
        self.patch.stop()
        super().tearDown()

    def test_no_config(self):
        code, r = self.call('/api/opencode')
        self.assertEqual((code, r['exists']), (200, False))
        code, r = self.call('/api/opencode', {'action': 'set', 'provider': 'corp', 'key': 'k'})
        self.assertEqual(code, 400)
        self.assertIn('自定义接口', r['error'])
        self.assertFalse(os.listdir(self.dir))       # 不生成文件

    def test_set_and_rollback(self):
        write(self.path, self.text)
        os.chmod(self.path, 0o700)
        code, r = self.call('/api/opencode')
        self.assertEqual([(p['id'], p['has_key_field']) for p in r['providers']], [('corp', True), ('nokey', False)])
        self.assertFalse(r['backup'])
        self.assertEqual(self.call('/api/opencode', {'action': 'set', 'provider': 'corp', 'key': ''})[0], 400)
        self.assertEqual(self.call('/api/opencode', {'action': 'set', 'provider': 'corp', 'key': 'a\nb'})[0], 400)
        code, r = self.call('/api/opencode', {'action': 'set', 'provider': 'nokey', 'key': 'k'})
        self.assertIn('没有 apiKey', r['error'])
        code, r = self.call('/api/opencode', {'action': 'set', 'provider': 'corp', 'key': 'NEW-KEY'})
        self.assertEqual(code, 200, r)
        self.assertTrue(r['info']['backup'])
        with open(self.path, encoding='utf-8') as f:
            now = f.read()
        self.assertEqual(now, self.text.replace('"apiKey": "old"', '"apiKey": "NEW-KEY"'))     # 注释与格式保留
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(self.path + fa.OPENCODE_BACKUP).st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(urllib.request.urlopen(MODEL_BASE + '/last').read())['model'], 'ok')
        self.assertIn('oc:corp/ok', [m['id'] for m in r['models']])
        code, r = self.call('/api/opencode', {'action': 'rollback'})
        self.assertEqual((code, r['info']['backup']), (200, False))
        with open(self.path, encoding='utf-8') as f:
            self.assertEqual(f.read(), self.text)
        self.assertEqual(self.call('/api/opencode', {'action': 'rollback'})[0], 400)

    def test_test_failure_keeps_file(self):
        write(self.path, self.text.replace('"model": "corp/ok"', '"model": "corp/err401"'))
        code, r = self.call('/api/opencode', {'action': 'set', 'provider': 'corp', 'key': 'BAD'})
        self.assertEqual(code, 400)
        self.assertIn('用新 AK 测试没有通过', r['error'])
        with open(self.path, encoding='utf-8') as f:
            self.assertIn('"apiKey": "old"', f.read())
        self.assertFalse(os.path.exists(self.path + fa.OPENCODE_BACKUP))

    def test_broken_config(self):
        write(self.path, '{"provider": {"corp": {"options": {"apiKey": "x"}}')
        code, r = self.call('/api/opencode', {'action': 'set', 'provider': 'corp', 'key': 'k'})
        self.assertEqual(code, 400)
        self.assertIn('格式有误', r['error'])

    def test_jsonc_span(self):
        text = '{"a": {"b": [1, {"c": 2}], "k": "v\\"x"}, // c\n "z": null}'
        self.assertEqual(text[slice(*fa.jsonc_span(text, ['a', 'k']))], '"v\\"x"')
        self.assertIsNone(fa.jsonc_span(text, ['a', 'nope']))
        with self.assertRaises(ValueError):
            fa.jsonc_span('{"a": 1} x', ['a'])


def zip_bytes(entries, links=()):
    """{包内路径: 文字} 打成 .zip 的字节；links 里的路径写成链接。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for name, text in entries.items():
            zf.writestr(name, text)
        for name in links:
            info = zipfile.ZipInfo(name)
            info.external_attr = (0o120777 << 16)
            zf.writestr(info, '/etc')
    return buf.getvalue()


class SkillManageTest(SkillCase):
    """管理 skill：新建与安装（.zip、文件夹、回答下方的输出文件）。"""

    def install(self, **body):
        return self.call('/api/skill-install', body)[1]

    def zip64(self, entries, links=()):
        return base64.b64encode(zip_bytes(entries, links)).decode('ascii')

    def test_create_and_listed(self):
        r = self.call('/api/skill-create', {'name': 'weekly-report', 'description': '写周报："要点" 与下周计划',
                                            'body': '按周汇总。'})[1]
        self.assertTrue(r['ok'], r)
        self.assertEqual((r['name'], r['replaced']), ('weekly-report', False))
        k = fa.load_skills()[0]['weekly-report']
        self.assertEqual((k['source'], k['description'], k['body']), ('user', '写周报："要点" 与下周计划', '按周汇总。'))
        self.assertEqual(os.listdir(self.user), ['weekly-report'])     # 不留临时目录

    def test_create_rejects_bad_input(self):
        for body in ({'name': 'Bad Name', 'description': 'd', 'body': 'b'},
                     {'name': 'ok', 'description': '', 'body': 'b'},
                     {'name': 'ok', 'description': 'd' * (fa.SKILL_DESC_MAX + 1), 'body': 'b'},
                     {'name': 'ok', 'description': 'd', 'body': '字' * (fa.SKILL_MAX_CHARS + 1)}):
            r = self.call('/api/skill-create', body)[1]
            self.assertFalse(r['ok'])
            self.assertIn('error', r)
        self.assertFalse(os.path.isdir(self.user) and os.listdir(self.user))

    def test_replace_needs_confirm(self):
        self.make('plain', '旧的', '旧正文', {'a.md': '1', 'b.md': '2'}, base=self.user)
        body = {'name': 'plain', 'description': '新的', 'body': '新正文'}
        r = self.call('/api/skill-create', body)[1]
        self.assertFalse(r['ok'])
        c = r['confirm']
        self.assertEqual((c['replaced'], c['old_files'], c['bundled']), (True, 3, True))
        self.assertEqual(c['target'], os.path.join(self.user, 'plain'))
        self.assertEqual(fa.load_skills()[0]['plain']['body'], '旧正文')
        r = self.call('/api/skill-create', dict(body, confirm=True))[1]
        self.assertTrue(r['ok'] and r['replaced'] and r['bundled'])
        self.assertEqual(sorted(os.listdir(os.path.join(self.user, 'plain'))), ['SKILL.md'])
        self.assertEqual(fa.load_skills()[0]['plain']['body'], '新正文')

    def test_install_zip_with_top_folder(self):
        data = self.zip64({'mine/SKILL.md': SKILL_MD % ('mine', '我的', '正文'), 'mine/ref/说明.md': '参考'})
        r = self.install(zip=data)
        self.assertTrue(r['ok'], r)
        self.assertEqual(fa.load_skills()[0]['mine']['refs'], ['ref/说明.md'])

    def test_install_zip_with_scripts_confirms(self):
        data = self.zip64({'SKILL.md': SKILL_MD % ('tool', '带脚本', '正文'), 'scripts/a.py': 'print(1)',
                           'scripts/b.sh': 'echo 1'})
        r = self.install(zip=data)
        self.assertEqual((r['ok'], r['confirm']['scripts'], r['confirm']['replaced']), (False, 2, False))
        self.assertFalse(os.path.exists(os.path.join(self.user, 'tool')))
        self.assertTrue(self.install(zip=data, confirm=True)['ok'])
        self.assertTrue(os.access(os.path.join(self.user, 'tool', 'scripts', 'a.py'), os.X_OK))

    def test_install_zip_rejects_escape_link_and_missing(self):
        md = SKILL_MD % ('evil', '坏包', '正文')
        for entries, links in (({'SKILL.md': md, '../x.md': '跳出'}, ()),
                               ({'SKILL.md': md, '/abs.md': '绝对路径'}, ()),
                               ({'SKILL.md': md}, ('link',)),
                               ({'readme.md': '没有 SKILL.md'}, ()),
                               ({'SKILL.md': '---\nname: Evil Name\ndescription: d\n---\n正文'}, ())):
            r = self.install(zip=self.zip64(entries, links))
            self.assertFalse(r['ok'])
            self.assertIn('error', r, entries)
        self.assertFalse(self.install(zip='不是 base64')['ok'])
        self.assertEqual(self.install(zip=base64.b64encode(b'not zip').decode())['ok'], False)
        self.assertEqual(os.listdir(self.user), [])
        self.assertFalse(os.path.exists(os.path.join(self.root, 'x.md')))

    def test_install_zip_size_limits(self):
        md = SKILL_MD % ('big', '大包', '正文')
        with mock.patch.object(fa, 'SKILL_UNPACK_MAX', 1000):
            r = self.install(zip=self.zip64({'SKILL.md': md, 'a.md': '0' * 2000}))
        self.assertIn('超过', r['error'])
        with mock.patch.object(fa, 'SKILL_FILES_MAX', 2):
            r = self.install(zip=self.zip64({'SKILL.md': md, 'a.md': '1', 'b.md': '2'}))
        self.assertIn('超过', r['error'])
        self.assertEqual(os.listdir(self.user), [])

    def test_install_folder(self):
        src = self.make('fromdir', '从文件夹', '正文', {'ref.md': '参考'}, base=os.path.join(self.root, 'src'))
        r = self.install(dir=src)
        self.assertTrue(r['ok'], r)
        self.assertEqual(fa.load_skills()[0]['fromdir']['refs'], ['ref.md'])
        os.symlink(os.path.join(self.root, 'secret.txt'), os.path.join(src, 'link.md'))
        r = self.install(dir=src, confirm=True)
        self.assertIn('链接', r['error'])
        self.assertEqual(sorted(os.listdir(os.path.join(self.user, 'fromdir'))), ['SKILL.md', 'ref.md'])
        self.assertIn('error', self.install(dir=os.path.join(self.root, '没有')))
        self.assertIn('error', self.install(dir=os.path.join(self.user, 'fromdir')))
        self.assertIn('error', self.install(dir=''))

    def test_install_from_output(self):
        out = self.st.new_output_dir()
        with open(os.path.join(out, 'made.zip'), 'wb') as f:
            f.write(zip_bytes({'made/SKILL.md': SKILL_MD % ('made', 'AI 写的', '正文')}))
        with open(os.path.join(out, '表格.zip'), 'wb') as f:
            f.write(zip_bytes({'a.txt': 'x'}))
        listed = {x['name']: x for x in self.st.register_outputs(out)}
        self.assertTrue(listed['made.zip'].get('skill'))
        self.assertNotIn('skill', listed['表格.zip'])
        r = self.install(output=listed['made.zip']['id'])
        self.assertTrue(r['ok'], r)
        self.assertIn('made', fa.load_skills()[0])
        self.assertIn('error', self.install(output='没有这个'))



if __name__ == '__main__':
    unittest.main()
