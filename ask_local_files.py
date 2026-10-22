#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ask_local_files  --  找文件问 AI

纯 Python 3 标准库实现。双击「双击运行」即启动本程序：只监听 127.0.0.1 的随机端口，
网址带一次性令牌，用浏览器打开同目录的 ask_local_files.html。页面全部关闭约 5 秒后自动退出；
有回答正在生成时，退出等它结束。

功能:
    - 找文件：只索引使用者添加的文件夹，按文件名和路径匹配，多个关键字空格分开、全部命中，
      可按扩展名筛选。全部路径拼成一个字符串在内存里查找；索引缓存在
      ~/.cache/ask-local-files/index.json，启动即可搜索，后台只重扫修改时间变了的文件夹。
    - 取文字：txt、md、csv 等纯文本直接读取；docx、xlsx、pptx、eml、pdf 用同目录的
      doc2kb_core.py 转换（pdf 需要 pdftotext）。取出的文字只留在内存里。
    - 问 AI：把选中文件的文字和对话发给 OpenAI 兼容的 /chat/completions 接口，流式返回。
      指令要求每个结论附原文摘句、文件里没有的写明未找到；回答结束后核对摘句是否真在原文里。
      接口地址、模型和 AK 先读 OpenCode 配置（~/.config/opencode/opencode.json[c]），
      没有时用本工具设置里填的（~/.config/ask-local-files/config.json，权限 600）。AK 不发给页面，
      不写日志。

用法:
    python3 ask_local_files.py [--no-browser] [--log-level LEVEL]
"""

import argparse
import bisect
import hmac
import json
import logging
import os
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

LOG_FORMAT = '%(asctime)s [%(levelname)s] %(name)s.%(funcName)s:%(lineno)d - %(message)s'
log = logging.getLogger('ask_local_files')

VERSION = '1.5'
HERE = os.path.dirname(os.path.abspath(__file__))
PAGE_FILE = os.path.join(HERE, 'ask_local_files.html')
LOG_NAME = 'ask_local_files.log'
HOME = os.path.expanduser('~')
XDG_CONFIG = os.environ.get('XDG_CONFIG_HOME') or os.path.join(HOME, '.config')
XDG_CACHE = os.environ.get('XDG_CACHE_HOME') or os.path.join(HOME, '.cache')
CONFIG_FILE = os.path.join(XDG_CONFIG, 'ask-local-files', 'config.json')
CACHE_FILE = os.path.join(XDG_CACHE, 'ask-local-files', 'index.json')
OPENCODE_DIR = os.path.join(XDG_CONFIG, 'opencode')

PING_TIMEOUT = 600      # 页面停止心跳多少秒后视为已关闭（浏览器未发出关页通知时兜底）
OPEN_TIMEOUT = 120      # 页面始终未打开时多少秒退出
BYE_GRACE = 5           # 页面全部关闭后等多少秒退出，刷新页面时新页面在此期间重新登记
MAX_BODY = 8 * 1024 * 1024
CHECK_EVERY = 30        # 搜索时至多每多少秒校验一次文件夹变化
PUBLISH_EVERY = 2.0     # 首次扫描时每多少秒放出一次部分结果
RESULT_LIMIT = 300
MAX_FILES = 20          # 一次最多选几个文件
DEFAULT_CHAR_LIMIT = 100000
PREVIEW_CHARS = 20000
UNREADABLE_SHOWN = 5    # 状态里最多列出几个读不了的文件夹
DIRS_SHOWN = 50         # 路径补全最多列出几个子文件夹
PLACES = (('家目录', ('',)), ('文档', ('文档', 'Documents')), ('桌面', ('桌面', 'Desktop')),
          ('下载', ('下载', 'Downloads')))
MAX_TEXT_BYTES = 20 * 1024 * 1024   # 纯文本文件最多读这么多字节
TIMEOUT = 120           # 模型接口最多静默多少秒
TEST_TIMEOUT = 30       # 测试连接最多等多少秒
SKIP_DIRS = {'node_modules', '__pycache__', '$RECYCLE.BIN', 'System Volume Information', 'lost+found'}
TEXT_EXT = {'.txt', '.md', '.markdown', '.csv', '.tsv', '.log', '.json', '.xml', '.yaml', '.yml', '.ini',
            '.conf', '.cfg', '.toml', '.sql', '.py', '.sh', '.js', '.ts', '.java', '.c', '.h', '.cpp',
            '.go', '.rs', '.css', '.srt', '.rst', '.tex'}
HTML_EXT = {'.html', '.htm'}
DOC2KB_EXT = {'.docx', '.pptx', '.xlsx', '.xlsm', '.eml', '.pdf'}
OLD_OFFICE = {'.doc', '.xls', '.ppt', '.wps', '.et', '.dps'}
NO_MODEL = '没有可用的模型：OpenCode 配置里找不到带接口地址和 AK 的模型，请在「模型设置」里填写自定义接口'
MISSING_FILE = '文件已不存在或不在已添加的文件夹里，请点「刷新」后重新搜索'

try:
    import doc2kb_core
    doc2kb_core.set_lang('zh')
except Exception as e:      # 缺文件或版本不符时只影响 Office/pdf，纯文本照常
    doc2kb_core = None
    log.warning('doc2kb_core unavailable: %s', e)


# ---------------------------------------------------------------- 配置

def load_json(path, default):
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, type(default)) else default
    except (OSError, ValueError):
        return default


def write_private(path, data):
    """原子写入 JSON，权限 600（含 AK 与文件路径）。"""
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = '%s.%d.tmp' % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


class Config:
    """本工具的设置：索引的文件夹、字数上限、自定义模型接口。"""

    def __init__(self, path=CONFIG_FILE):
        self.path = path
        self.lock = threading.Lock()
        data = load_json(path, {})
        self.folders = [f for f in data.get('folders', []) if isinstance(f, str)]
        limit = data.get('char_limit')
        self.char_limit = limit if isinstance(limit, int) and limit > 0 else DEFAULT_CHAR_LIMIT
        custom = data.get('custom') if isinstance(data.get('custom'), dict) else {}
        self.custom = {k: str(custom.get(k) or '') for k in ('base', 'model', 'key')}
        self.model = str(data.get('model') or '')

    def save(self):
        write_private(self.path, {'folders': self.folders, 'char_limit': self.char_limit,
                                  'custom': self.custom, 'model': self.model})

    def public(self):
        return {'folders': list(self.folders), 'char_limit': self.char_limit, 'model': self.model,
                'custom': {'base': self.custom['base'], 'model': self.custom['model'],
                           'has_key': bool(self.custom['key'])},
                }




def norm_folder(value):
    """文件夹须为绝对路径或 ~/ 开头且存在；返回规范化的绝对路径，不合法时抛 ValueError。"""
    value = (value or '').strip()
    if value.startswith('~'):
        value = os.path.expanduser(value)
    if not value or not os.path.isabs(value):
        raise ValueError('文件夹须为绝对路径或以 ~/ 开头')
    value = os.path.normpath(value)
    if not os.path.isdir(value):
        raise ValueError('找不到这个文件夹')
    return value


def list_dirs(prefix):
    """路径补全：列出 prefix 所在文件夹里以其末段开头的子文件夹（跳过以 . 开头的）。
    prefix 以 ~ 开头时结果也写成 ~ 开头。"""
    prefix = (prefix or '').strip()
    tilde = prefix.startswith('~')
    full = os.path.expanduser(prefix) if tilde else prefix
    if not full or not os.path.isabs(full):
        return []
    if full.endswith(os.sep):
        parent, stem = full, ''
    else:
        parent, stem = os.path.dirname(full), os.path.basename(full)
    try:
        with os.scandir(parent) as it:
            names = sorted(e.name for e in it if not e.name.startswith('.') and e.name.startswith(stem)
                           and e.is_dir())
    except OSError:
        return []
    out = [os.path.join(parent, n) for n in names[:DIRS_SHOWN]]
    home = HOME.rstrip(os.sep) + os.sep
    return ['~/' + p[len(home):] if tilde and p.startswith(home) else p for p in out]


def common_places():
    """本机实际存在的常用位置：家目录、文档、桌面、下载。"""
    out = []
    for label, names in PLACES:
        for n in names:
            if os.path.isdir(os.path.join(HOME, n)):
                out.append({'label': label, 'path': '~/' + n if n else '~'})
                break
    return out


# ---------------------------------------------------------------- 文件名索引

class Snapshot:
    """一次索引结果：全部路径小写后用换行拼成一个字符串，查找交给 str.find。"""

    def __init__(self, entries):
        self.entries = entries      # [(路径, 显示名, 大小, 修改时间)]，按修改时间倒序
        lines = [e[1].lower() for e in entries]
        self.blob = '\n'.join(lines)
        self.starts = []
        pos = 0
        for line in lines:
            self.starts.append(pos)
            pos += len(line) + 1

    def search(self, words, exts, limit=RESULT_LIMIT):
        """(命中条目, 命中总数)。没有关键字时按扩展名列出最近修改的文件。"""
        words = [w.lower() for w in words if w]
        if words:
            anchor = max(words, key=len)
            others = [w for w in words if w is not anchor]
            hits, pos, blob = [], 0, self.blob
            while True:
                pos = blob.find(anchor, pos)
                if pos < 0:
                    break
                i = bisect.bisect_right(self.starts, pos) - 1
                end = self.starts[i + 1] - 1 if i + 1 < len(self.starts) else len(blob)
                line = blob[self.starts[i]:end]
                if all(w in line for w in others):
                    hits.append(i)
                pos = end + 1
            pool = (self.entries[i] for i in hits)      # starts 有序，hits 仍按修改时间倒序
        else:
            pool = iter(self.entries)
        out, total = [], 0
        for e in pool:
            if exts and os.path.splitext(e[0])[1].lower().lstrip('.') not in exts:
                continue
            total += 1
            if len(out) < limit:
                out.append(e)
        return out, total


class FileIndex:
    """只索引使用者添加的文件夹。dirs 记录每个文件夹的修改时间、文件和子文件夹；文件夹修改时间
    没变就沿用记录，不再列目录，所以增量刷新只需对每个文件夹 stat 一次。"""

    def __init__(self, config, cache_path=CACHE_FILE):
        self.config = config
        self.cache_path = cache_path
        self.lock = threading.Lock()
        self.dirs = {}
        self.snap = Snapshot([])
        self.scanning = False
        self.last_check = 0.0
        self.error = ''
        self.unreadable = []        # 读不了的文件夹，最多列 UNREADABLE_SHOWN 个
        self.scanned_at = ''
        self.built_roots = None     # 当前 snap 对应的文件夹清单
        cache = load_json(cache_path, {})
        if cache.get('v') == 1 and isinstance(cache.get('dirs'), dict):
            self.dirs = cache['dirs']
            self.snap = self.build(self.dirs, self.config.folders)
            self.built_roots = list(self.config.folders)
            log.info('index cache loaded: %d files', len(self.snap.entries))

    @staticmethod
    def build(dirs, roots):
        entries, seen = [], set()
        for root in roots:
            parent = os.path.dirname(root.rstrip(os.sep)) or os.sep
            prefix = root.rstrip(os.sep) + os.sep
            for d, rec in dirs.items():
                if d != root and not d.startswith(prefix):
                    continue
                for name, size, mtime in rec[1]:
                    path = os.path.join(d, name)
                    if path in seen:
                        continue
                    seen.add(path)
                    entries.append((path, os.path.relpath(path, parent), size, mtime))
        entries.sort(key=lambda e: e[3], reverse=True)
        return Snapshot(entries)

    def status(self):
        return {'scanning': self.scanning, 'files': len(self.snap.entries), 'error': self.error,
                'unreadable': list(self.unreadable), 'scanned_at': self.scanned_at}

    def refresh(self, full=False, wait=False):
        """后台增量刷新；full 时丢弃记录重扫。已在扫描时不重复启动。"""
        with self.lock:
            if self.scanning:
                return False
            self.scanning = True
            self.last_check = time.monotonic()
        t = threading.Thread(target=self._refresh, args=(full,), daemon=True)
        t.start()
        if wait:
            t.join()
        return True

    def maybe_refresh(self):
        if time.monotonic() - self.last_check >= CHECK_EVERY:
            self.refresh()

    def _refresh(self, full):
        old = {} if full else self.dirs
        new = {}
        roots = list(self.config.folders)
        last_pub = time.monotonic()
        errors = []
        changed = full or roots != self.built_roots
        try:
            for root in roots:
                stack = [root]
                while stack:
                    d = stack.pop()
                    rec = self._scan_dir(d, old, errors)
                    if rec is None:
                        continue
                    new[d] = rec
                    changed = changed or rec is not old.get(d)
                    stack.extend(os.path.join(d, s) for s in rec[2])
                    if not self.dirs and time.monotonic() - last_pub > PUBLISH_EVERY:
                        self.snap = self.build(new, roots)
                        last_pub = time.monotonic()
            changed = changed or len(new) != len(old)
            if not changed:     # 没有文件夹变化：不重建、不写缓存
                with self.lock:
                    self.scanned_at = time.strftime('%H:%M:%S')
                    self.set_unreadable(errors)
                return
            snap = self.build(new, roots)
            with self.lock:
                self.dirs, self.snap, self.built_roots = new, snap, roots
                self.set_unreadable(errors)
                self.scanned_at = time.strftime('%H:%M:%S')
            try:
                write_private(self.cache_path, {'v': 1, 'dirs': new})
            except OSError as e:
                log.warning('cannot write index cache: %s', e)
            log.info('index refreshed: %d folders, %d files, %d unreadable',
                     len(new), len(snap.entries), len(errors))
        except Exception as e:  # 后台线程出错不能静默卡在扫描中
            log.exception('index refresh failed')
            self.error = '索引失败：%s' % e
        finally:
            with self.lock:
                self.scanning = False

    def set_unreadable(self, errors):
        self.unreadable = errors[:UNREADABLE_SHOWN]
        self.error = '有 %d 个文件夹无法读取，已跳过' % len(errors) if errors else ''

    @staticmethod
    def _scan_dir(d, old, errors):
        try:
            mtime = os.stat(d).st_mtime_ns
        except OSError:
            return None
        rec = old.get(d)
        if rec and rec[0] == mtime:
            return rec
        files, subs = [], []
        try:
            with os.scandir(d) as it:
                for e in it:
                    if e.name.startswith('.'):
                        continue
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if e.name not in SKIP_DIRS:
                                subs.append(e.name)
                        elif e.is_file():
                            st = e.stat()
                            files.append([e.name, st.st_size, int(st.st_mtime)])
                    except OSError:
                        continue
        except OSError as e:
            log.debug('cannot list %s: %s', d, e)
            errors.append(d)
            return None
        return [mtime, files, subs]

    def search(self, query, exts):
        self.maybe_refresh()
        words = query.split()
        exts = {x.lower().lstrip('.') for x in re.split(r'[\s,，、;；]+', exts or '') if x.strip('. ')}
        found, total = self.snap.search(words, exts)
        reasons = {}
        out = []
        for e in found:
            ext = os.path.splitext(e[0])[1].lower()
            if ext not in reasons:
                reasons[ext] = unsupported_reason(ext)
            out.append({'path': e[0], 'name': os.path.basename(e[0]), 'rel': e[1], 'size': e[2],
                        'mtime': time.strftime('%Y-%m-%d %H:%M', time.localtime(e[3])),
                        'supported': not reasons[ext], 'reason': reasons[ext]})
        return {'results': out, 'total': total, 'status': self.status()}

    def contains(self, path):
        """路径须在某个已添加的文件夹里（按真实路径判断，链接出去的不算）。"""
        real = os.path.realpath(path)
        for root in self.config.folders:
            r = os.path.realpath(root)
            if real == r or real.startswith(r.rstrip(os.sep) + os.sep):
                return True
        return False


# ---------------------------------------------------------------- 取文字

def read_text_file(path):
    with open(path, 'rb') as f:
        raw = f.read(MAX_TEXT_BYTES + 1)
    note = '文件超过 %d MB，只取了前面部分' % (MAX_TEXT_BYTES // 1048576) if len(raw) > MAX_TEXT_BYTES else ''
    raw = raw[:MAX_TEXT_BYTES]
    if b'\0' in raw[:8192]:
        raise ValueError('看起来不是文本文件')
    for enc in ('utf-8-sig', 'gb18030'):
        try:
            return raw.decode(enc), note
        except UnicodeDecodeError:
            continue
    return raw.decode('utf-8', 'replace'), note


def html_text(html):
    html = re.sub(r'(?is)<(script|style)\b.*?</\1>', ' ', html)
    html = re.sub(r'(?i)<br\s*/?>|</(p|div|li|tr|h[1-6])>', '\n', html)
    text = re.sub(r'<[^>]+>', ' ', html)
    for a, b in (('&nbsp;', ' '), ('&lt;', '<'), ('&gt;', '>'), ('&quot;', '"'), ('&#39;', "'"), ('&amp;', '&')):
        text = text.replace(a, b)
    return re.sub(r'[ \t]+\n', '\n', re.sub(r'\n{3,}', '\n\n', text)).strip()


def unsupported_reason(ext):
    """按扩展名判断读不了的原因；读得了（或要读了才知道）时返回空字符串。"""
    if ext in OLD_OFFICE:
        return '不支持旧版 %s 格式，请先另存为 docx、xlsx 或 pptx' % ext
    if ext in DOC2KB_EXT and doc2kb_core is None:
        return '缺少同目录的 doc2kb_core.py，无法读取 %s' % ext
    if ext == '.pdf' and not shutil.which('pdftotext'):
        return '读取 pdf 需要 pdftotext（poppler-utils），本机没有安装'
    return ''


def extract(path):
    """(文字, 说明)。不支持或读不出时抛 ValueError，写明原因。"""
    ext = os.path.splitext(path)[1].lower()
    reason = unsupported_reason(ext)
    if reason:
        raise ValueError(reason)
    if ext in TEXT_EXT:
        return read_text_file(path)
    if ext in HTML_EXT:
        text, note = read_text_file(path)
        return html_text(text), note
    if ext in DOC2KB_EXT:
        report = doc2kb_core.Report()
        with tempfile.TemporaryDirectory(prefix='ask-local-files-') as tmp:
            md, _ = doc2kb_core.convert(path, ext, tmp, report, save_images=False)
        if not md.strip():
            raise ValueError('没有取出文字（可能是扫描件或图片），请换用能复制文字的版本')
        return md, ''
    try:
        return read_text_file(path)
    except ValueError:
        raise ValueError('不支持 %s 格式，请转成 txt、docx、pdf 等支持的格式' % (ext or '无扩展名'))


class TextCache:
    """取出的文字只放内存，按路径、大小、修改时间失效。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.items = {}

    def get(self, path):
        st = os.stat(path)
        key = (path, st.st_size, st.st_mtime_ns)
        with self.lock:
            hit = self.items.get(path)
            if hit and hit[0] == key:
                return hit[1], hit[2]
        text, note = extract(path)
        with self.lock:
            self.items[path] = (key, text, note)
        return text, note


# ---------------------------------------------------------------- 模型接口

class ModelError(Exception):
    pass


def strip_jsonc(text):
    """去掉 // 和 /* */ 注释与结尾多余的逗号，字符串原样保留，供 json.loads 读 opencode.jsonc。"""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == '\\' else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith('//', i):
            j = text.find('\n', i)
            i = n if j < 0 else j
        elif text.startswith('/*', i):
            j = text.find('*/', i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    return re.sub(r',(\s*[}\]])', r'\1', ''.join(out))


def opencode_config():
    for name in ('opencode.jsonc', 'opencode.json'):
        path = os.path.join(OPENCODE_DIR, name)
        if os.path.isfile(path):
            try:
                with open(path, encoding='utf-8') as f:
                    data = json.loads(strip_jsonc(f.read()) or '{}')
            except (OSError, ValueError) as e:
                log.warning('cannot read %s: %s', path, e)
                return {}
            return data if isinstance(data, dict) else {}
    return {}


def resolve_key(value):
    """OpenCode 的 apiKey 写法：{env:变量}、{file:路径} 或直接写值。"""
    if not isinstance(value, str):
        return ''
    if value.startswith('{env:') and value.endswith('}'):
        return os.environ.get(value[5:-1], '')
    if value.startswith('{file:') and value.endswith('}'):
        try:
            with open(os.path.expanduser(value[6:-1]), encoding='utf-8') as f:
                return f.read().strip()
        except OSError:
            return ''
    return value


def host_of(base):
    return urllib.parse.urlsplit(base).netloc or base


def model_targets(config):
    """可用的模型：{id: {base, key, model, headers, label, host}}，以及默认 id。
    OpenCode 配置里每个服务商的每个模型一项（id 为 oc:服务商/模型），设置里填齐的自定义接口一项（custom）。"""
    out, default = {}, ''
    conf = opencode_config()
    provs = conf.get('provider') if isinstance(conf.get('provider'), dict) else {}
    for pid, prov in provs.items():
        if not isinstance(prov, dict):
            continue
        opts = prov.get('options') if isinstance(prov.get('options'), dict) else {}
        base = opts.get('baseURL')
        key = resolve_key(opts.get('apiKey'))
        if not isinstance(base, str) or not base.strip() or not key:
            continue
        headers = {str(k): str(v) for k, v in opts['headers'].items()} \
            if isinstance(opts.get('headers'), dict) else {}
        models = prov.get('models') if isinstance(prov.get('models'), dict) else {}
        for name in models:
            out['oc:%s/%s' % (pid, name)] = {
                'base': base.strip().rstrip('/'), 'key': key, 'model': name, 'headers': headers,
                'label': '%s（OpenCode：%s）' % (name, pid), 'host': host_of(base.strip())}
    mid = conf.get('model')
    if isinstance(mid, str) and 'oc:' + mid in out:
        default = 'oc:' + mid
    c = config.custom
    if c['base'] and c['model'] and c['key']:
        out['custom'] = {'base': c['base'].rstrip('/'), 'key': c['key'], 'model': c['model'], 'headers': {},
                         'label': '%s（自定义）' % c['model'], 'host': host_of(c['base'])}
    if config.model in out:
        default = config.model
    if not default and out:
        default = next(iter(out))
    return out, default


NOT_FOUND = '所选文件中未找到'
CITE_RULE = ('只依据下面这些文件的文字回答，不用常识或文件以外的知识补充。'
             '每个结论后面紧跟出处，写成（文件 N：「原文摘句」），摘句从原文逐字照抄，不改写、不概括，'
             '太长时只摘最关键的一句。所选文件中找不到依据的内容，直接写「' + NOT_FOUND + '」，不要推测。')
CITE_RE = re.compile(r'文件\s*(\d+)\s*[:：]\s*「([^「」]+)」')


def build_messages(files, history):
    """files: [(路径, 文字)]；history: [{role, content}]，最后一条是使用者的新问题。"""
    parts = [CITE_RULE, '使用者选了下面 %d 个文件，以下是从中取出的文字。' % len(files)]
    for i, (path, text) in enumerate(files, 1):
        parts.append('===== 文件 %d：%s =====\n%s' % (i, path, text))
    return [{'role': 'system', 'content': '\n\n'.join(parts)}] + history


def check_quotes(answer, texts):
    """核对回答里的（文件 N：「摘句」）是否真在文件 N 的原文里：忽略空白差异，摘句中的省略号两侧分开核对。
    返回 (摘句个数, 原文中找不到的 [{'file': N, 'quote': 摘句}])。"""
    flat = [re.sub(r'\s+', '', t) for t in texts]
    count, missing = 0, []
    for m in CITE_RE.finditer(answer):
        n, quote = int(m.group(1)), m.group(2)
        count += 1
        parts = [p for p in re.split(r'…+|\.{3,}', re.sub(r'\s+', '', quote)) if p]
        if not 1 <= n <= len(flat) or not parts or not all(p in flat[n - 1] for p in parts):
            missing.append({'file': n, 'quote': quote})
    return count, missing


def fit_texts(texts, limit):
    """总字数超过 limit 时按比例截断每个文件，返回 (截断后的文字列表, 是否截断)。"""
    total = sum(len(t) for t in texts)
    if total <= limit:
        return texts, False
    out = []
    for t in texts:
        keep = max(1, limit * len(t) // total)
        out.append(t[:keep] + ('\n……（以下已截断）' if keep < len(t) else ''))
    return out, True


def http_error_text(code, detail):
    """把接口的 HTTP 错误写成「原因：下一步怎么做」。"""
    if code in (401, 403):
        return '接口拒绝了请求（HTTP %d），多半是 AK 无效或过期：请检查 OpenCode 配置或「模型设置」里的 AK' % code
    if code == 404:
        return '接口地址或模型名不对（HTTP 404）：请检查接口地址和模型名'
    if code in (400, 413) and re.search(r'context|length|too long|token|长度|过长', detail, re.I):
        return '内容超过模型能接受的长度（HTTP %d）：请移除几个文件、调低字数上限或开始新对话' % code
    if code == 429:
        return '请求太频繁或额度已用完（HTTP 429）：请稍后再试'
    return '接口返回 HTTP %d%s' % (code, '：' + detail if detail else '')


def stream_chat(tg, msgs, timeout=TIMEOUT):
    """从 OpenAI 兼容接口逐段取回答；接口不流式时整段返回。出错抛 ModelError，写明原因。"""
    body = json.dumps({'model': tg['model'], 'messages': msgs, 'stream': True}).encode('utf-8')
    headers = {'Content-Type': 'application/json', 'Accept': 'text/event-stream',
               'Authorization': 'Bearer ' + tg['key']}
    headers.update(tg['headers'])
    req = urllib.request.Request(tg['base'] + '/chat/completions', data=body, method='POST', headers=headers)
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        try:
            detail = ' '.join(e.read(300).decode('utf-8', 'replace').split())
        except OSError:
            detail = ''
        raise ModelError(http_error_text(e.code, detail))
    except (socket.timeout, TimeoutError):
        raise ModelError('接口 %d 秒没有响应' % timeout)
    except urllib.error.URLError as e:
        raise ModelError('连不上接口：%s' % e.reason)
    except OSError as e:
        raise ModelError('连不上接口：%s' % e)
    try:
        with r:
            if 'event-stream' not in r.headers.get('Content-Type', ''):
                try:
                    text = json.loads(r.read())['choices'][0]['message'].get('content') or ''
                except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                    raise ModelError('接口返回的内容看不懂')
                if text:
                    yield text
                return
            for raw in r:
                line = raw.decode('utf-8', 'replace').strip()
                if not line.startswith('data:'):
                    continue
                data = line[5:].strip()
                if data == '[DONE]':
                    return
                try:
                    piece = (json.loads(data)['choices'][0].get('delta') or {}).get('content')
                except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                    continue
                if piece:
                    yield piece
    except (socket.timeout, TimeoutError):
        raise ModelError('接口 %d 秒没有新内容' % timeout)
    except OSError as e:
        raise ModelError('连接中断：%s' % e)




# ---------------------------------------------------------------- 网页服务

def log_file_path():
    """日志放工具目录，不可写时放 ~/.cache/ask-local-files/。"""
    if os.access(HERE, os.W_OK):
        return os.path.join(HERE, LOG_NAME)
    d = os.path.join(XDG_CACHE, 'ask-local-files')
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, LOG_NAME)


class WebState:
    def __init__(self, port, token, config, index):
        self.port = port
        self.token = token
        self.origin = 'http://127.0.0.1:%d' % port
        self.config = config
        self.index = index
        self.texts = TextCache()
        self.started = time.monotonic()
        self.lock = threading.Lock()
        self.pages = {}         # 页面编号 -> 最近一次心跳时间
        self.seen = False
        self.empty_since = None
        self.quit = threading.Event()
        self.chats = 0          # 正在生成的回答数

    def ping(self, page):
        with self.lock:
            self.pages[page] = time.monotonic()
            self.seen = True
            self.empty_since = None

    def bye(self, page):
        with self.lock:
            self.pages.pop(page, None)
            if self.seen and not self.pages and self.empty_since is None:
                self.empty_since = time.monotonic()

    def chat_begin(self):
        with self.lock:
            self.chats += 1

    def chat_end(self):
        with self.lock:
            self.chats -= 1

    def should_exit(self):
        """返回退出原因，不该退出时返回 None；有回答正在生成时一律不退出。"""
        now = time.monotonic()
        with self.lock:
            for page, t in list(self.pages.items()):
                if now - t > PING_TIMEOUT:
                    log.info('page %s heartbeat lost', page)
                    del self.pages[page]
                    if not self.pages:
                        self.empty_since = now - BYE_GRACE
            if self.chats:
                return None
            if self.quit.is_set():
                return 'quit requested'
            if not self.seen:
                return 'page never opened' if now - self.started > OPEN_TIMEOUT else None
            if not self.pages and self.empty_since is not None and now - self.empty_since >= BYE_GRACE:
                return 'all pages closed'
        return None


def tell_user(msg):
    """双击运行时没有终端，用 zenity 或 notify-send 提示；都没有时只写日志。"""
    log.warning('%s', msg)
    print(msg, flush=True)
    if sys.stdout.isatty():
        return
    for cmd in (['zenity', '--info', '--no-wrap', '--text=' + msg], ['notify-send', msg]):
        if not shutil.which(cmd[0]):
            continue
        try:
            subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return
        except OSError as e:
            log.debug('%s failed: %s', cmd[0], e)


def models_public(config):
    targets, default = model_targets(config)
    return {'models': [{'id': k, 'label': v['label'], 'host': v['host']} for k, v in targets.items()],
            'default': default}


class Handler(BaseHTTPRequestHandler):
    state = None

    def log_message(self, fmt, *args):
        log.debug('%s - %s', self.address_string(), fmt % args)

    def send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def allowed(self, api, token=None):
        """校验 Host、Origin；接口另需令牌（默认取 X-Token 头）。不通过时已回 403。"""
        st = self.state
        ok = self.headers.get('Host') == '127.0.0.1:%d' % st.port
        origin = self.headers.get('Origin')
        if ok and origin is not None:
            ok = origin == st.origin
        if ok and api:
            if token is None:
                token = self.headers.get('X-Token', '')
            ok = isinstance(token, str) and hmac.compare_digest(token, st.token)
        if not ok:
            log.warning('rejected %s %s', self.command, urlsplit(self.path).path)
            self.send_json(403, {'error': '拒绝访问'})
        return ok

    def read_json(self):
        try:
            n = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > MAX_BODY:
            return {}
        try:
            data = json.loads(self.rfile.read(n).decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def do_GET(self):
        url = urlsplit(self.path)
        api = url.path.startswith('/api/')
        if not self.allowed(api):
            return
        q = parse_qs(url.query)
        arg = lambda k: (q.get(k) or [''])[0]
        st = self.state
        if url.path in ('/', '/index.html'):
            try:
                with open(PAGE_FILE, 'rb') as f:
                    body = f.read()
            except OSError as e:
                log.error('page missing: %s', e)
                self.send_json(500, {'error': '找不到页面文件 ask_local_files.html'})
                return
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)
        elif url.path == '/api/state':
            out = st.config.public()
            out.update(models_public(st.config))
            out.update({'index': st.index.status(), 'version': VERSION, 'max_files': MAX_FILES,
                        'pdftotext': bool(shutil.which('pdftotext')), 'doc2kb': doc2kb_core is not None})
            self.send_json(200, out)
        elif url.path == '/api/dirs':
            self.send_json(200, {'dirs': list_dirs(arg('prefix')), 'places': common_places()})
        elif url.path == '/api/search':
            self.send_json(200, st.index.search(arg('q'), arg('ext')))
        elif url.path == '/api/text':
            path = arg('path')
            if not path or not st.index.contains(path) or not os.path.isfile(path):
                self.send_json(400, {'error': MISSING_FILE})
                return
            try:
                text, note = st.texts.get(path)
            except Exception as e:      # 转换器对损坏文件可能抛出任何异常
                log.info('extract failed: %s (%s)', path, e)
                self.send_json(200, {'path': path, 'error': str(e) or e.__class__.__name__})
                return
            out = {'path': path, 'chars': len(text), 'note': note}
            if arg('preview'):
                out['text'] = text[:PREVIEW_CHARS]
                out['more'] = max(0, len(text) - PREVIEW_CHARS)
            self.send_json(200, out)
        else:
            self.send_json(404, {'error': '没有这个地址'})

    def do_POST(self):
        url = urlsplit(self.path)
        st = self.state
        if url.path == '/api/bye':
            # 关页通知由 sendBeacon 发出，不能带请求头，令牌放在请求体里
            data = self.read_json()
            if not self.allowed(True, data.get('t')):
                return
            log.info('page %s closed', data.get('page'))
            st.bye(str(data.get('page')))
            self.send_json(200, {'ok': True})
            return
        if not self.allowed(True):
            return
        data = self.read_json()
        if url.path == '/api/ping':
            st.ping(str(data.get('page')))
            self.send_json(200, {'ok': True})
        elif url.path == '/api/quit':
            busy = st.chats > 0
            log.info('quit requested by page%s', ', waiting for answers' if busy else '')
            self.send_json(200, {'ok': True, 'waiting': busy})
            st.quit.set()
        elif url.path == '/api/folders':
            self.post_folders(data)
        elif url.path == '/api/settings':
            self.post_settings(data)
        elif url.path == '/api/refresh':
            started = st.index.refresh(full=bool(data.get('full')))
            self.send_json(200, {'ok': True, 'started': started, 'index': st.index.status()})
        elif url.path == '/api/chat':
            self.post_chat(data)
        elif url.path == '/api/test-model':
            self.post_test_model(data)
        else:
            self.send_json(404, {'error': '没有这个地址'})

    def post_folders(self, data):
        cfg, idx = self.state.config, self.state.index
        merged = []
        with cfg.lock:
            if data.get('add'):
                try:
                    folder = norm_folder(data['add'])
                except ValueError as e:
                    self.send_json(400, {'error': str(e)})
                    return
                for f in cfg.folders:
                    if folder == f or folder.startswith(f.rstrip(os.sep) + os.sep):
                        self.send_json(400, {'error': '已在索引范围内（%s）' % f})
                        return
                merged = [f for f in cfg.folders if f.startswith(folder.rstrip(os.sep) + os.sep)]
                cfg.folders = [f for f in cfg.folders if f not in merged]
                cfg.folders.append(folder)
            elif data.get('remove'):
                cfg.folders = [f for f in cfg.folders if f != data['remove']]
            cfg.save()
        idx.refresh()
        self.send_json(200, {'folders': list(cfg.folders), 'merged': merged, 'index': idx.status()})

    def post_settings(self, data):
        cfg = self.state.config
        with cfg.lock:
            if 'char_limit' in data:
                try:
                    limit = int(data['char_limit'])
                except (TypeError, ValueError):
                    limit = 0
                if not 1000 <= limit <= 2000000:
                    self.send_json(400, {'error': '字数上限须为 1000 到 2000000 的整数'})
                    return
                cfg.char_limit = limit
            if isinstance(data.get('custom'), dict):
                c = data['custom']
                base = str(c.get('base') or '').strip()
                if base and not re.match(r'^https?://', base):
                    self.send_json(400, {'error': '接口地址须以 http:// 或 https:// 开头'})
                    return
                cfg.custom['base'] = base
                cfg.custom['model'] = str(c.get('model') or '').strip()
                if c.get('key'):
                    cfg.custom['key'] = str(c['key']).strip()
                if c.get('clear_key'):
                    cfg.custom['key'] = ''
            if 'model' in data:
                cfg.model = str(data['model'] or '')
            cfg.save()
        out = cfg.public()
        out.update(models_public(cfg))
        self.send_json(200, out)


    def post_test_model(self, data):
        """向所选模型发一条极短的请求，拿到第一段回答即算连通。"""
        targets, default = model_targets(self.state.config)
        tg = targets.get(data.get('model') or default)
        if not tg:
            self.send_json(200, {'ok': False, 'error': NO_MODEL})
            return
        reply = ''
        try:
            for piece in stream_chat(tg, [{'role': 'user', 'content': '请只回复「好」。'}], timeout=TEST_TIMEOUT):
                reply = piece
                break
        except ModelError as e:
            self.send_json(200, {'ok': False, 'error': str(e)})
            return
        self.send_json(200, {'ok': True, 'model': tg['label'], 'reply': reply[:50]})

    def post_chat(self, data):
        st = self.state
        files, history = data.get('files'), data.get('messages')
        if not isinstance(files, list) or not files or len(files) > MAX_FILES:
            self.send_json(400, {'error': '请选 1 到 %d 个文件' % MAX_FILES})
            return
        if not isinstance(history, list) or not history or not all(
                isinstance(m, dict) and m.get('role') in ('user', 'assistant') and isinstance(m.get('content'), str)
                for m in history) or history[-1]['role'] != 'user' or not history[-1]['content'].strip():
            self.send_json(400, {'error': '请先写下要问的内容'})
            return
        targets, default = model_targets(st.config)
        tg = targets.get(data.get('model') or default)
        if not tg:
            self.send_json(400, {'error': NO_MODEL})
            return
        texts = []
        for path in files:
            if not isinstance(path, str) or not st.index.contains(path) or not os.path.isfile(path):
                self.send_json(400, {'error': '%s：%s' % (MISSING_FILE, path)})
                return
            try:
                texts.append(st.texts.get(path)[0])
            except Exception as e:
                self.send_json(400, {'error': '取不出文字：%s（%s）' % (path, e)})
                return
        # 字数上限管文件与对话的合计；截断只截文件，给对话留出位置
        limit = st.config.char_limit
        files_chars, history_chars = sum(len(t) for t in texts), sum(len(m['content']) for m in history)
        total = files_chars + history_chars
        if history_chars >= limit:
            self.send_json(413, {'error': 'history_too_long', 'history': history_chars, 'limit': limit})
            return
        if total > limit and not data.get('truncate'):
            self.send_json(413, {'error': 'too_long', 'total': total, 'files': files_chars,
                                 'history': history_chars, 'limit': limit})
            return
        full = texts
        texts, cut = fit_texts(texts, limit - history_chars)
        msgs = build_messages(list(zip(files, texts)), [{'role': m['role'], 'content': m['content']}
                                                       for m in history])
        log.info('chat: %d files, %d chars, model %s, host %s', len(files), total, tg['model'], tg['host'])
        self.send_response(200)
        self.send_header('Content-Type', 'application/x-ndjson; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        st.chat_begin()
        emit = lambda obj: (self.wfile.write((json.dumps(obj, ensure_ascii=False) + '\n').encode('utf-8')),
                            self.wfile.flush())
        try:
            emit({'start': True, 'truncated': cut, 'model': tg['model'], 'host': tg['host']})
            pieces = []
            for piece in stream_chat(tg, msgs):
                pieces.append(piece)
                emit({'t': piece})
            answer = ''.join(pieces)
            count, missing = check_quotes(answer, full)
            log.info('answer: %d quotes, %d not found in files', count, len(missing))
            emit({'done': True, 'quotes': count, 'missing': missing,
                  'uncited': count == 0 and NOT_FOUND not in answer})
        except ModelError as e:
            log.warning('chat failed: %s', e)
            try:
                emit({'error': str(e)})
            except OSError:
                pass
        except OSError as e:
            log.info('page went away during answer: %s', e)
        finally:
            st.chat_end()


def watchdog(server, st, interval=2):
    """退出按钮、页面全部关闭或页面始终未打开时关闭服务；有回答正在生成时等它结束再退出。"""
    while True:
        st.quit.wait(interval)
        reason = st.should_exit()
        if reason:
            log.info('%s, exiting', reason)
            break
        if st.quit.is_set():
            time.sleep(interval)    # 退出已请求但回答未结束，quit.wait 不再阻塞
    server.shutdown()


def serve(open_browser=True):
    if not os.path.isfile(PAGE_FILE):
        tell_user('找不到页面文件 %s，请重新解压完整的工具目录。' % PAGE_FILE)
        return 1
    fh = logging.FileHandler(log_file_path(), encoding='utf-8')
    fh.setFormatter(logging.Formatter(LOG_FORMAT))
    logging.getLogger().addHandler(fh)
    if logging.getLogger().level > logging.INFO:
        logging.getLogger().setLevel(logging.INFO)
    config = Config()
    index = FileIndex(config)
    index.refresh()
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    st = WebState(server.server_address[1], secrets.token_urlsafe(24), config, index)
    Handler.state = st
    url = '%s/?t=%s' % (st.origin, st.token)
    log.info('serving on %s', st.origin)
    print('网页界面：' + url, flush=True)
    threading.Thread(target=watchdog, args=(server, st), daemon=True).start()
    if open_browser and not webbrowser.open(url):
        tell_user('未能自动打开浏览器，请在浏览器中打开：' + url)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    log.info('server stopped')
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description='找文件问 AI：快速找出文件，交给 AI 提问、改写、总结（双击运行即此程序）。')
    ap.add_argument('--no-browser', action='store_true', help='不自动打开浏览器，只打印网址')
    ap.add_argument('--log-level', default=os.environ.get('LOG_LEVEL', 'WARNING').upper(),
                    choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
                    help='日志级别，默认 WARNING，可由环境变量 LOG_LEVEL 覆盖')
    args = ap.parse_args(argv)
    logging.basicConfig(level=args.log_level, format=LOG_FORMAT, stream=sys.stderr)
    return serve(open_browser=not args.no_browser)


if __name__ == '__main__':
    sys.exit(main())
