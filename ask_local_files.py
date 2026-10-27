#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ask_local_files  --  找文件问 AI

纯 Python 3 标准库实现。双击「双击运行」即启动本程序：只监听 127.0.0.1 的随机端口，
网址带一次性令牌，用浏览器打开同目录的 ask_local_files.html。页面全部关闭约 5 秒后自动退出；
有回答正在生成时，退出等它结束。

功能:
    - 找文件：只索引使用者添加的文件夹，按文件名和路径匹配，多个关键字空格分开、全部命中，
      可按扩展名筛选，也可按文件夹逐层浏览。全部路径拼成一个字符串在内存里查找；索引缓存在
      ~/.cache/ask-local-files/index.json，启动即可搜索，后台只重扫修改时间变了的文件夹。
    - 取文字：txt、md、csv 等纯文本直接读取；docx、xlsx、pptx、eml、pdf 和旧版 doc、xls、ppt
      （wps、et、dps 按同类格式试读）用同目录的 doc2kb_core.py 转换（pdf 需要 pdftotext）。
      文字开头加上文件的路径、文件名、修改时间和作者。
    - 读不了的文件（压缩包等）在列表里给「打开文件夹」，用 xdg-open 或 gio 打开所在文件夹。
    - 问 AI：把选中文件的文字和对话发给 OpenAI 兼容的 /chat/completions 接口，流式返回。
      指令要求每个结论附原文摘句、文件里没有的写明未找到；回答结束后核对摘句是否真在原文里。
      接口地址、模型和 AK 先读 OpenCode 配置（~/.config/opencode/opencode.json[c]），
      没有时用本工具设置里填的（~/.config/ask-local-files/config.json，权限 600）。AK 不发给页面，
      不写日志。
    - skill：可选一个 OpenCode 格式的 skill（工具目录 skills/ 里预装的，或 ~/.config/opencode/skills/
      里自建的），正文并进系统消息；模型可用函数调用读 skill 目录里的文件、运行其中的 .py/.sh 脚本
      （自动运行，单次 60 秒，每问最多 5 次），脚本写进临时输出目录的文件可在页面上保存。
    - 记忆（ask_memory.py，默认开，设置里可关）：提问时选过的文件按段落存进本机 SQLite 记忆
      （~/.local/share/ask-local-files/memory.sqlite，权限 600），回答里核对通过的摘句连同结论记下；
      之后提问时按关键字检索记忆，结论与原文段落排在已选文件之后一起发给模型，取用前按文件当前内容复核。
    - 文件夹输入框接受 file:/// 地址；可由本机服务弹出系统的目录选择框（zenity、kdialog 或 tkinter）。
    - 按文件名去重：同名（不区分大小写，去掉一个副本标记后相同也算）只列修改时间最新的一个。

用法:
    python3 ask_local_files.py [--no-browser] [--no-app] [--log-level LEVEL]
"""

import argparse
import bisect
import hmac
import importlib.util
import json
import logging
import os
import re
import secrets
import select
import shlex
import shutil
import signal
import socket
import sqlite3
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

VERSION = '2.1'
HERE = os.path.dirname(os.path.abspath(__file__))
PAGE_FILE = os.path.join(HERE, 'ask_local_files.html')
LOG_NAME = 'ask_local_files.log'
HOME = os.path.expanduser('~')
XDG_CONFIG = os.environ.get('XDG_CONFIG_HOME') or os.path.join(HOME, '.config')
XDG_CACHE = os.environ.get('XDG_CACHE_HOME') or os.path.join(HOME, '.cache')
XDG_DATA = os.environ.get('XDG_DATA_HOME') or os.path.join(HOME, '.local', 'share')
CONFIG_FILE = os.path.join(XDG_CONFIG, 'ask-local-files', 'config.json')
CACHE_FILE = os.path.join(XDG_CACHE, 'ask-local-files', 'index.json')
MEMORY_FILE = os.path.join(XDG_DATA, 'ask-local-files', 'memory.sqlite')
MEMORY_SHARE = 3        # 选了文件时，记忆至多占发送字数上限（扣除 skill 与对话后）的几分之一
OPENCODE_DIR = os.path.join(XDG_CONFIG, 'opencode')
USER_SKILLS = os.path.join(OPENCODE_DIR, 'skills')
BUNDLED_SKILLS = os.path.join(HERE, 'skills')
DOCX_SCRIPT = os.path.join(HERE, 'skills', 'office-gen', 'scripts', 'make_docx.py')   # 「保存为 Word」借用
DOCX_TYPE = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'

PING_TIMEOUT = 600      # 页面停止心跳多少秒后视为已关闭（浏览器未发出关页通知时兜底）
OPEN_TIMEOUT = 120      # 页面始终未打开时多少秒退出
BYE_GRACE = 5           # 页面全部关闭后等多少秒退出，刷新页面时新页面在此期间重新登记
MAX_BODY = 8 * 1024 * 1024
CHECK_EVERY = 30        # 搜索时至多每多少秒校验一次文件夹变化
PUBLISH_EVERY = 2.0     # 首次扫描时每多少秒放出一次部分结果
RESULT_LIMIT = 300
MAX_FILES = 50          # 一次最多选几个文件
SORTS = ('time_desc', 'time_asc', 'name_asc', 'name_desc')     # 找文件列表的排序，页面下拉框的取值
DEFAULT_SORT = 'time_desc'
TREE_SHOWN = MAX_FILES * 2 + 1      # 浏览时交给页面的「全部加入」候选最多几个
DEFAULT_CHAR_LIMIT = 100000
PREVIEW_CHARS = 20000
UNREADABLE_SHOWN = 5    # 状态里最多列出几个读不了的文件夹
DIRS_SHOWN = 50         # 路径补全最多列出几个子文件夹
PLACES = (('家目录', ('',)), ('文档', ('文档', 'Documents')), ('桌面', ('桌面', 'Desktop')),
          ('下载', ('下载', 'Downloads')))
MAX_TEXT_BYTES = 20 * 1024 * 1024   # 纯文本文件最多读这么多字节
TIMEOUT = 120           # 模型接口最多静默多少秒
LOCAL_TIMEOUT = 900     # 本机地址的模型接口最多静默多少秒：纯 CPU 读长文件要先算完整段输入才出字
TEST_TIMEOUT = 30       # 测试连接最多等多少秒
LOOPBACK = {'127.0.0.1', 'localhost', '::1'}
# 自动发现的本机模型服务：(类型, 端口, 名称)，探测各自的 /v1/models
LOCAL_SERVERS = (('llama', 8080, 'llama-server'), ('ollama', 11434, 'Ollama'))
PROBE_TIMEOUT = 1.5     # 探测本机模型服务最多等多少秒
OLLAMA_DEFAULT_CTX = 4096   # Ollama 模型未加载时按这个上下文估算，环境变量 OLLAMA_CONTEXT_LENGTH 优先
CTX_SHARE = 0.75        # 上下文（token，按 1 字计）留四分之一给回答，其余作为发送字数上限
LOCAL_DOWN = '本机模型服务没有运行：请先启动 llama-server 或 Ollama，再点「重新查找」'
SKILL_MAX_CHARS = 50000     # SKILL.md 超过这么多字不可选
SKILL_LIST_MAX = 100        # 每个 skill 最多列出几个脚本、几个参考文件
SCRIPT_TIMEOUT = 60         # skill 脚本单次最多运行多少秒
MAX_TOOL_CALLS = 5          # 每个问题最多调用几次工具
TOOL_OUTPUT_CHARS = 20000   # 工具结果最多交给模型多少字
MAX_OUTPUT_FILES = 20       # 每次运行最多列出几个输出文件
MAX_WRITE_CALLS = 10        # 每个问题最多写几次工作目录文件，不计入 MAX_TOOL_CALLS
MAX_WORK_CHARS = 500000     # 工作目录里单个文件最多多少字
WRITE_TOOL = 'write_work_file'
SCRIPT_EXT = {'.py', '.sh'}
# 预装 skill 在页面上显示的中文名；SKILL.md 的 name 不变，自建 skill 显示原名
BUNDLED_LABELS = {'doc-compare': '文档对比', 'summarize-meeting': '会议纪要', 'office-gen': '生成 Word 与 Excel',
                  'prompt-optimize': '提示词优化', 'clarify-needs': '需求澄清'}
EMPTY_ASK = '请按所选 skill 的说明处理已选文件。'   # 选了 skill、没写问题时代为提问
SKILL_META_FILES = {'skill.md', 'upstream.md'}   # 不算参考文件；另有 LICENSE*
SKIP_DIRS = {'node_modules', '__pycache__', '$RECYCLE.BIN', 'System Volume Information', 'lost+found'}
TEXT_EXT = {'.txt', '.md', '.markdown', '.csv', '.tsv', '.log', '.json', '.xml', '.yaml', '.yml', '.ini',
            '.conf', '.cfg', '.toml', '.sql', '.py', '.sh', '.js', '.ts', '.java', '.c', '.h', '.cpp',
            '.go', '.rs', '.css', '.srt', '.rst', '.tex'}
HTML_EXT = {'.html', '.htm'}
DOC2KB_EXT = {'.docx', '.pptx', '.xlsx', '.xlsm', '.eml', '.pdf'}
OLD_OFFICE = {'.doc', '.xls', '.ppt', '.wps', '.et', '.dps'}     # 旧版 Office 与 WPS 格式
# 旧版 Office 与 WPS 格式按哪种格式交给 doc2kb_core 解析
OLD_OFFICE_AS = {'.doc': '.doc', '.xls': '.xls', '.ppt': '.ppt', '.wps': '.doc', '.et': '.xls', '.dps': '.ppt'}
ARCHIVE_EXT = {'.zip', '.rar', '.7z', '.tar', '.gz', '.tgz', '.bz2', '.tbz2', '.xz', '.txz', '.zst', '.lz', '.cab'}
ARCHIVE_REASON = '压缩包请先解压，再加入解压出的文件'
OPENERS = (('xdg-open',), ('gio', 'open'))     # 打开文件夹用的命令，按顺序取第一个找得到的
# 在文件管理器里打开所在文件夹并选中文件：先走标准 D-Bus 接口 FileManager1.ShowItems，再试各文件管理器的选中参数
SHOW_ITEMS = (('gdbus', 'call', '--session', '--dest', 'org.freedesktop.FileManager1',
               '--object-path', '/org/freedesktop/FileManager1', '--method', 'org.freedesktop.FileManager1.ShowItems',
               "['{uri}']", ''),
              ('dbus-send', '--session', '--print-reply', '--dest=org.freedesktop.FileManager1',
               '/org/freedesktop/FileManager1', 'org.freedesktop.FileManager1.ShowItems',
               'array:string:{uri}', 'string:'))
SELECT_OPENERS = (('peony', '--show-items'), ('nautilus', '--select'), ('caja', '--select'))
REVEAL_TIMEOUT = 5     # 等 D-Bus 调用返回的秒数
FILE_URL_ERROR = '只支持本机 file:/// 地址。请改用以 / 开头的路径'
# 副本标记：扩展名前的「 (2)」「（2）」「 - 副本」「_副本」「 copy」「 (copy)」等，标记后可再带序号；
# 页面上的 sameNameKey 用同一个正则，两边须一致（tests/same_name_cases.json）
COPY_NUM = r' ?(?:\([0-9]+\)|（[0-9]+）)'
COPY_MARK_RE = re.compile(r'^(.+?)(?:' + COPY_NUM + r'|(?: - 副本|-副本|_副本| \(副本\)|副本| - copy| \(copy\)| copy)(?:'
                          + COPY_NUM + r')?)$')
NO_MODEL = ('没有可用的模型：OpenCode 配置里找不到带接口地址和 AK 的模型，本机也没有运行中的 llama-server 或 Ollama；'
            '请在「模型设置」里填写自定义接口，或启动本机模型服务后点「重新查找」')
MISSING_FILE = '文件已不存在或不在已添加的文件夹里，请点「刷新」后重新搜索'
# 能以应用窗口模式（--app=网址，不显示地址栏）打开的 Chromium 系浏览器，按顺序取第一个找得到的；
# 国产浏览器（奇安信、360、统信）排在后面
APP_BROWSERS = ('chromium', 'chromium-browser', 'google-chrome', 'google-chrome-stable',
                'microsoft-edge', 'microsoft-edge-stable', 'brave-browser',
                'qaxbrowser-safe-stable', 'qaxbrowser-safe', 'qaxbrowser',
                'browser360-cn-stable', 'browser360-cn', 'browser360', 'deepin-browser', 'uos-browser')
# 上面的命令都不在 PATH 里时，按这些桌面文件的 Exec 找浏览器程序（装在自带目录、不进 PATH 的浏览器）
APP_DESKTOP_FILES = ('qaxbrowser-safe.desktop', 'qaxbrowser.desktop', 'browser360-cn.desktop',
                     'com.360.browser-stable.desktop', 'browser360.desktop', 'deepin-browser.desktop',
                     'uos-browser.desktop')

import ask_memory   # noqa: E402  同目录的记忆模块

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
        self.dedup = data.get('dedup') is not False     # 按文件名去重，默认开
        self.regex = data.get('regex') is True          # 搜索框按正则表达式，默认关
        self.memory = data.get('memory') is not False   # 记忆总开关，默认开
        self.use_memory = data.get('use_memory') is not False   # 「问 AI」里的「参考记忆」，默认开
        self.memory_notice = data.get('memory_notice') is True  # 已提示过记忆默认打开

    def save(self):
        write_private(self.path, {'folders': self.folders, 'char_limit': self.char_limit, 'dedup': self.dedup,
                                  'regex': self.regex, 'memory': self.memory, 'use_memory': self.use_memory,
                                  'memory_notice': self.memory_notice,
                                  'custom': self.custom, 'model': self.model})

    def public(self):
        return {'folders': list(self.folders), 'char_limit': self.char_limit, 'model': self.model, 'dedup': self.dedup,
                'regex': self.regex, 'memory': self.memory, 'use_memory': self.use_memory,
                'memory_notice': self.memory_notice,
                'custom': {'base': self.custom['base'], 'model': self.custom['model'],
                           'has_key': bool(self.custom['key'])},
                }




def local_path(value, strip_slash=True):
    """去掉首尾空白与成对引号，把 file:/// 地址转成普通路径（还原 %XX 转义，按 UTF-8），其他写法原样返回。
    file:// 后带主机名（含 localhost）或转义不合法时抛 ValueError。"""
    value = (value or '').strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
        value = value[1:-1].strip()
    if not value[:5].lower() == 'file:':
        return value
    rest = value[7:] if value[:8].lower() == 'file:///' else ''
    if not rest or re.search(r'%(?![0-9A-Fa-f]{2})', rest):
        raise ValueError(FILE_URL_ERROR)
    try:
        path = urllib.parse.unquote(rest, errors='strict')
    except UnicodeDecodeError:
        raise ValueError(FILE_URL_ERROR)
    if '\0' in path:
        raise ValueError(FILE_URL_ERROR)
    return (path.rstrip('/') or '/') if strip_slash else path


def norm_folder(value):
    """文件夹须为绝对路径、~/ 开头或 file:/// 地址且存在；返回规范化的绝对路径，不合法时抛 ValueError。"""
    value = local_path(value)
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
    prefix 以 ~ 开头时结果也写成 ~ 开头；file:/// 地址的结果写成普通绝对路径。"""
    try:
        prefix = local_path(prefix, strip_slash=False)
    except ValueError:
        return []
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


def start_dir(value):
    """目录选择框的初始位置：输入框里是存在的文件夹（含 ~/ 与 file:/// 写法）时用它，否则用家目录。"""
    try:
        path = os.path.expanduser(local_path(value))
    except ValueError:
        return HOME
    return os.path.normpath(path) if os.path.isabs(path) and os.path.isdir(path) else HOME


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
        self.paths = {e[0]: e[0] for e in entries}      # 页面传来的路径只换成这里存的路径再使用
        lines = [e[1].lower() for e in entries]
        self.blob = '\n'.join(lines)
        self.starts = []
        pos = 0
        for line in lines:
            self.starts.append(pos)
            pos += len(line) + 1

    def search(self, words, exts, limit=RESULT_LIMIT, regex=None, order=DEFAULT_SORT):
        """(命中条目, 命中总数)，命中条目按 order 排序后取前 limit 个。没有关键字时按扩展名列出全部文件。
        给了 regex（已编译的正则）时不用 words，逐行对显示路径 re.search。"""
        words = [w.lower() for w in words if w]
        if regex is not None:
            pool = (e for e in self.entries if regex.search(e[1]))
        elif words:
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
        if exts:
            pool = (e for e in pool if os.path.splitext(e[0])[1].lower().lstrip('.') in exts)
        if order != DEFAULT_SORT:
            hits = sort_entries(list(pool), order)
            return hits[:limit], len(hits)
        out, total = [], 0
        for e in pool:
            total += 1
            if len(out) < limit:
                out.append(e)
        return out, total

    def browse(self, folder):
        """folder 这一层：(子文件夹 {名称: 含各层文件数}, 本层全部文件, 含子文件夹的全部条目)。
        条目沿用按修改时间倒序。"""
        prefix = folder.rstrip(os.sep) + os.sep
        subs, files, tree = {}, [], []
        for e in self.entries:
            if not e[0].startswith(prefix):
                continue
            tree.append(e)
            rest = e[0][len(prefix):]
            if os.sep in rest:
                name = rest.split(os.sep, 1)[0]
                subs[name] = subs.get(name, 0) + 1
            else:
                files.append(e)
        return subs, files, tree


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

    def search(self, query, exts, regex=False, order=DEFAULT_SORT):
        """regex 时整个 query 作为一个不区分大小写的正则，写错时抛 re.error。order 见 SORTS。"""
        self.maybe_refresh()
        words = query.split()
        pattern = re.compile(query.strip(), re.IGNORECASE) if regex and query.strip() else None
        exts = {x.lower().lstrip('.') for x in re.split(r'[\s,，、;；]+', exts or '') if x.strip('. ')}
        found, total = self.snap.search(words, exts, regex=pattern, order=order)
        reasons = {}
        return {'results': [entry_public(e, reasons) for e in found], 'total': total, 'status': self.status()}

    def browse(self, folder, dedup=False, order=DEFAULT_SORT):
        """按文件夹逐层浏览。folder 为空时列出已添加的文件夹；否则须是已添加的文件夹或其中的子文件夹。
        本层文件与含子文件夹的清单都按 order（见 SORTS）排序后再去重、截取。dedup 时本层文件与含子文件夹的清单各自按文件名去重（含子文件夹时跨层），每个文件带其余同名文件 dups。
        tree 为「全部加入」用的可读文件，最多 TREE_SHOWN 个：已选最多 MAX_FILES 个，扣掉已选的同名后
        仍能判断能否全部加入、算出个数。"""
        self.maybe_refresh()
        snap, roots = self.snap, list(self.config.folders)
        folder = os.path.normpath(folder) if folder else ''
        if not folder:
            dirs = [{'path': r, 'name': r, 'files': len(snap.browse(r)[2])} for r in roots]
            return {'dir': '', 'crumbs': [], 'dirs': dirs, 'files': [], 'here': 0, 'here_dedup': 0, 'tree': [],
                    'tree_total': 0, 'dedup': dedup, 'status': self.status()}
        root = next((r for r in roots if folder == r or folder.startswith(r.rstrip(os.sep) + os.sep)), None)
        if not root:
            return {'error': '这个文件夹不在已添加的文件夹里，请回到「全部」重新选择', 'status': self.status()}
        crumbs = [{'path': root, 'name': os.path.basename(root.rstrip(os.sep)) or root}]
        cur = root
        for part in os.path.relpath(folder, root).split(os.sep):
            if part != '.':
                cur = os.path.join(cur, part)
                crumbs.append({'path': cur, 'name': part})
        subs, files, tree = snap.browse(folder)
        files, tree = sort_entries(files, order), sort_entries(tree, order)
        reasons = {}
        layer = dedup_entries(files) if dedup else [(e, []) for e in files]
        kept = [e for e, _ in dedup_entries(tree)] if dedup else tree
        readable = [e for e in kept if not reason_of(e[0], reasons)]
        shown = []
        for e, others in layer[:RESULT_LIMIT]:
            item = entry_public(e, reasons)
            if others:
                item['dups'] = [entry_public(o, reasons) for o in others[:RESULT_LIMIT]]
            shown.append(item)
        return {'dir': folder, 'crumbs': crumbs,
                'dirs': [{'path': os.path.join(folder, n), 'name': n, 'files': subs[n]} for n in sorted(subs)],
                'files': shown, 'here': len(files), 'here_dedup': len(layer), 'dedup': dedup,
                'tree': [entry_public(e, reasons) for e in readable[:TREE_SHOWN]], 'tree_total': len(readable),
                'status': self.status()}

    def resolve(self, path):
        """页面传来的文件路径换成索引里存的同一路径；不在索引里、不在已添加的文件夹里或已不存在时返回 None。"""
        with self.lock:
            snap = self.snap
        found = snap.paths.get(path) if isinstance(path, str) else None
        if not found or not self.contains(found) or not os.path.isfile(found):
            return None
        return found

    def contains(self, path):
        """路径须在某个已添加的文件夹里（按真实路径判断，链接出去的不算）。"""
        real = os.path.realpath(path)
        for root in self.config.folders:
            r = os.path.realpath(root)
            if real == r or real.startswith(r.rstrip(os.sep) + os.sep):
                return True
        return False


def sort_entries(entries, order):
    """按 order 排序：time_desc 最新在前（索引本来的顺序），time_asc 最旧在前，name_asc、name_desc 按文件名
    （不区分大小写，同名再按路径）。"""
    if order == 'time_asc':
        return sorted(entries, key=lambda e: (e[3], e[0]))
    if order in ('name_asc', 'name_desc'):
        return sorted(entries, key=lambda e: (os.path.basename(e[0]).lower(), e[0]), reverse=order == 'name_desc')
    return list(entries)


def sort_arg(value):
    """页面传来的排序取值，不认识的按默认。"""
    return value if value in SORTS else DEFAULT_SORT


def same_name_key(name):
    """同名判断用的键：不区分大小写，扩展名不同不算同名，去掉扩展名前的一个副本标记。"""
    stem, ext = os.path.splitext(name.lower())
    m = COPY_MARK_RE.match(stem)
    return (m.group(1) if m else stem) + ext


def dedup_entries(entries):
    """按文件名分组，每组留修改时间最新的一个（相同时取路径较短的），返回 [(留下的条目, 其余同名条目)]：
    按留下的条目在 entries 里的顺序排列，其余的按修改时间倒序。"""
    groups = {}
    for i, e in enumerate(entries):
        groups.setdefault(same_name_key(os.path.basename(e[0])), []).append((i, e))
    out = []
    for g in groups.values():
        g.sort(key=lambda x: (-x[1][3], len(x[1][0]), x[1][0]))
        out.append((g[0][0], g[0][1], [e for _, e in g[1:]]))
    out.sort(key=lambda x: x[0])
    return [(e, others) for _, e, others in out]


def reason_of(path, reasons):
    ext = os.path.splitext(path)[1].lower()
    if ext not in reasons:
        reasons[ext] = unsupported_reason(ext)
    return reasons[ext]


def entry_public(e, reasons):
    """索引条目转成页面用的字典；reasons 缓存各扩展名读不了的原因。"""
    reason = reason_of(e[0], reasons)
    return {'path': e[0], 'name': os.path.basename(e[0]), 'rel': e[1], 'size': e[2],
            'mtime': time.strftime('%Y-%m-%d %H:%M', time.localtime(e[3])), 'ts': e[3],
            'supported': not reason, 'reason': reason}


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
    if ext in ARCHIVE_EXT:
        return ARCHIVE_REASON
    if (ext in DOC2KB_EXT or ext in OLD_OFFICE_AS) and doc2kb_core is None:
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
    if ext in OLD_OFFICE_AS:
        report = doc2kb_core.Report()
        try:
            with tempfile.TemporaryDirectory(prefix='ask-local-files-') as tmp:
                md, _ = doc2kb_core.convert(path, OLD_OFFICE_AS[ext], tmp, report, save_images=False)
        except Exception as e:      # 解析器对损坏或不兼容的文件可能抛出任何异常
            raise ValueError('读取失败：%s。请先另存为 docx、xlsx 或 pptx' % (str(e) or e.__class__.__name__))
        if not md.strip():
            raise ValueError('没有取出文字，请先另存为 docx、xlsx 或 pptx 再试')
        return md, ''
    try:
        return read_text_file(path)
    except ValueError:
        raise ValueError('不支持 %s 格式，请转成 txt、docx、pdf 等支持的格式' % (ext or '无扩展名'))


def file_info(path, st):
    """放在取出的文字开头的文件信息：路径、文件名、修改时间、作者（取不到的省略）。"""
    lines = ['路径：' + path, '文件名：' + os.path.basename(path),
             '修改时间：' + time.strftime('%Y-%m-%d %H:%M', time.localtime(st.st_mtime))]
    author, last = doc2kb_core.doc_authors(path) if doc2kb_core else ('', '')
    if author or last:
        lines.append('作者：' + (author or last))
        if author and last and last != author:
            lines.append('最后修改者：' + last)
    return '\n'.join(lines) + '\n\n'


class TextCache:
    """取出的文字只放内存，按路径、大小、修改时间失效。文字开头带 file_info() 的文件信息。"""

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
        text = file_info(path, st) + text
        with self.lock:
            self.items[path] = (key, text, note)
        return text, note


# ---------------------------------------------------------------- skill

def parse_front_matter(text):
    """拆出 SKILL.md 开头 --- 之间的前置信息（只认 key: value、带引号的值与 > | 多行值），返回 (字典, 正文)。"""
    lines = text.lstrip('﻿').split('\n')
    if not lines or lines[0].strip() != '---':
        return {}, text
    meta, key, block = {}, None, []
    for i, line in enumerate(lines[1:], 1):
        if line.strip() == '---':
            if key is not None:
                meta[key] = ' '.join(block).strip()
            return meta, '\n'.join(lines[i + 1:]).strip()
        m = re.match(r'^([A-Za-z_][\w-]*):\s*(.*)$', line)
        if m and not line[0].isspace():
            if key is not None:
                meta[key] = ' '.join(block).strip()
            key, value = m.group(1).lower(), m.group(2).strip()
            if value in ('>', '|', '>-', '|-'):
                block = []
            else:
                if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
                    value = value[1:-1]
                meta[key], key, block = value, None, []
        elif key is not None:
            block.append(line.strip())
    return {}, text


def skill_files(d):
    """skill 目录里的 (脚本, 参考文件)，相对路径；跳过隐藏文件、SKILL.md、UPSTREAM.md 与许可文件。"""
    scripts, refs = [], []
    for root, dirs, names in os.walk(d):
        dirs[:] = sorted(x for x in dirs if not x.startswith('.') and x not in SKIP_DIRS)
        for n in sorted(names):
            low = n.lower()
            if n.startswith('.') or low.endswith('.pyc'):
                continue
            rel = os.path.relpath(os.path.join(root, n), d)
            if rel.lower() in SKILL_META_FILES or (root == d and low.startswith(('license', 'licence'))):
                continue
            (scripts if os.path.splitext(low)[1] in SCRIPT_EXT else refs).append(rel)
    return scripts[:SKILL_LIST_MAX], refs[:SKILL_LIST_MAX]


def load_skills(dirs=None):
    """读取各来源的 skill，返回 ({名称: skill}, [跳过的原因])；后面的来源同名覆盖前面的。"""
    dirs = dirs or ((BUNDLED_SKILLS, 'bundled'), (USER_SKILLS, 'user'))
    skills, problems = {}, []
    for base, source in dirs:
        try:
            names = sorted(os.listdir(base))
        except OSError:
            continue
        for n in names:
            d = os.path.join(base, n)
            md = os.path.join(d, 'SKILL.md')
            if n.startswith('.') or not os.path.isfile(md):
                continue
            try:
                with open(md, encoding='utf-8-sig') as f:
                    text = f.read(SKILL_MAX_CHARS * 4 + 1)
            except (OSError, UnicodeDecodeError) as e:
                problems.append('%s：读不了 SKILL.md（%s）' % (d, e))
                continue
            meta, body = parse_front_matter(text)
            name, desc = meta.get('name', '').strip(), meta.get('description', '').strip()
            if not name or not desc:
                problems.append('%s：SKILL.md 开头缺少 name 或 description' % d)
                continue
            scripts, refs = skill_files(d)
            skills[name] = {'name': name, 'description': desc, 'dir': os.path.realpath(d), 'source': source,
                            'body': body, 'scripts': scripts, 'refs': refs,
                            'too_long': len(body) > SKILL_MAX_CHARS}
    for p in problems:
        log.info('skill skipped: %s', p)
    return skills, problems


def skill_label(skill):
    return BUNDLED_LABELS.get(skill['name'], skill['name']) if skill['source'] == 'bundled' else skill['name']


def skills_public(skills):
    return [{'name': s['name'], 'label': skill_label(s), 'description': s['description'], 'source': s['source'],
             'chars': len(s['body']),
             'scripts': len(s['scripts']), 'refs': len(s['refs']), 'too_long': s['too_long']}
            for s in sorted(skills.values(), key=lambda s: (s['source'] != 'bundled', s['name']))]


def skill_tools(skill):
    """skill 有脚本或参考文件时给模型的函数定义；都没有时返回 None（仅指令）。"""
    tools = []
    if skill['refs'] or skill['scripts']:
        tools.append({'type': 'function', 'function': {
            'name': 'read_skill_file',
            'description': '读取 skill 目录里的文件（相对路径），如参考资料、范例、脚本源码。可读的参考文件：'
                           + ('、'.join(skill['refs']) or '无'),
            'parameters': {'type': 'object', 'properties': {'path': {'type': 'string', 'description': '相对路径'}},
                           'required': ['path']}}})
    if skill['scripts']:
        tools.append({'type': 'function', 'function': {
            'name': 'run_script',
            'description': '运行 skill 目录里的脚本（.py 用 Python 3，.sh 用 bash），参数逐个给出，不经 shell。'
                           '脚本从环境变量拿到：ASK_FILES 已选文件的绝对路径（换行分隔），ASK_ROOTS 使用者添加的文件夹，'
                           'ASK_OUT 输出目录（写进去的文件使用者可以保存），ASK_WORK 本问的工作目录'
                           '（' + WRITE_TOOL + ' 写入的文件在这里）。限时 %d 秒。可用脚本：%s'
                           % (SCRIPT_TIMEOUT, '、'.join(skill['scripts'])),
            'parameters': {'type': 'object', 'properties': {
                'script': {'type': 'string', 'description': '脚本的相对路径'},
                'args': {'type': 'array', 'items': {'type': 'string'}, 'description': '命令行参数'}},
                'required': ['script']}}})
        tools.append({'type': 'function', 'function': {
            'name': WRITE_TOOL,
            'description': '把文字写进本问的工作目录（脚本从环境变量 ASK_WORK 拿到该目录），供随后运行的脚本读取，'
                           '如要转成 Word 的 Markdown、要转成 Excel 的 JSON。脚本参数里直接写文件名即可。'
                           '同名文件默认覆盖；append 为 true 时接在原内容后面，用于分几次写长内容。'
                           '单个文件最多 %d 字，每个问题最多写 %d 次。工作目录在回答结束后删除，使用者看不到也不能保存，'
                           '要给使用者的文件须由脚本写进 ASK_OUT。' % (MAX_WORK_CHARS, MAX_WRITE_CALLS),
            'parameters': {'type': 'object', 'properties': {
                'path': {'type': 'string', 'description': '工作目录内的相对路径，如 报告.md'},
                'content': {'type': 'string', 'description': '要写入的文字'},
                'append': {'type': 'boolean', 'description': '为 true 时追加到已有内容后面，默认覆盖'}},
                'required': ['path', 'content']}}})
    return tools or None


def inside(base, rel):
    """rel 为 base 内的相对路径时返回其绝对路径（解析链接后仍在 base 内），否则抛 ValueError。"""
    if not isinstance(rel, str) or not rel.strip() or os.path.isabs(rel) or '\0' in rel:
        raise ValueError('路径须为 skill 目录内的相对路径')
    path = os.path.realpath(os.path.join(base, rel.strip()))
    if not path.startswith(base.rstrip(os.sep) + os.sep):
        raise ValueError('路径超出 skill 目录：%s' % rel)
    if not os.path.isfile(path):
        raise ValueError('skill 目录里没有这个文件：%s' % rel)
    return path


def write_work_file(work, rel, content, append=False):
    """把文字写进工作目录，返回 (相对路径, 本次字数, 写后总字数)；不合法时抛 ValueError。"""
    if not isinstance(rel, str) or not rel.strip() or os.path.isabs(rel.strip()) or '\0' in rel:
        raise ValueError('path 须为工作目录内的相对路径')
    if not isinstance(content, str) or '\0' in content:
        raise ValueError('content 须为文字，不能含空字符')
    rel = os.path.normpath(rel.strip())
    base = os.path.realpath(work)
    path = os.path.join(base, rel)
    under = lambda p: os.path.realpath(p).startswith(base + os.sep)
    # 建目录前后各查一次：已有的链接不能把写入带出工作目录
    if rel == '.' or rel.split(os.sep)[0] == '..' or not under(path):
        raise ValueError('路径超出工作目录：%s' % rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not under(path) or os.path.islink(path) or os.path.isdir(path):
        raise ValueError('不能写入：%s' % rel)
    old = 0
    if append and os.path.isfile(path):
        with open(path, encoding='utf-8', errors='replace') as f:
            old = len(f.read())
    total = old + len(content)
    if total > MAX_WORK_CHARS:
        raise ValueError('单个文件最多 %d 字，写入后会有 %d 字；请拆成几个文件分别生成' % (MAX_WORK_CHARS, total))
    with open(path, 'a' if append else 'w', encoding='utf-8') as f:
        f.write(content)
    return rel, len(content), total


_docx = []


def markdown_docx(markdown, title=''):
    """用预装 office-gen 的 make_docx.py 把 Markdown 转成 docx 字节；找不到脚本时抛 OSError。"""
    if not _docx:
        spec = importlib.util.spec_from_file_location('make_docx', DOCX_SCRIPT)
        if spec is None or not os.path.isfile(DOCX_SCRIPT):
            raise OSError('找不到 %s' % os.path.relpath(DOCX_SCRIPT, HERE))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _docx.append(mod)
    return _docx[0].build_docx(markdown, title)[0]


def clip(text, limit=TOOL_OUTPUT_CHARS):
    if len(text) <= limit:
        return text
    return text[:limit] + '\n……（共 %d 字，以下已截断）' % len(text)


def read_skill_file(skill, rel):
    path = inside(skill['dir'], rel)
    text, note = read_text_file(path)
    return clip(text) + ('\n（%s）' % note if note else '')


def run_skill_script(skill, rel, args, env_extra, out_dir, gone=lambda: False, timeout=SCRIPT_TIMEOUT):
    """运行 skill 目录里的脚本，返回 {'exit', 'output', 'timed_out', 'stopped'}；参数不合法时抛 ValueError。"""
    path = inside(skill['dir'], rel)
    ext = os.path.splitext(path)[1].lower()
    if ext not in SCRIPT_EXT:
        raise ValueError('只能运行 .py 或 .sh 脚本：%s' % rel)
    if args is None:
        args = []
    if not isinstance(args, list) or len(args) > 50 or not all(
            isinstance(a, (str, int, float)) and not isinstance(a, bool) for a in args):
        raise ValueError('args 须为字符串列表，最多 50 个')
    args = [str(a) for a in args]
    if any('\0' in a or len(a) > 10000 for a in args):
        raise ValueError('参数不能含空字符，单个不超过 10000 字')
    cmd = ([sys.executable] if ext == '.py' else ['bash']) + [path] + args
    env = dict(os.environ)
    env.update(env_extra)
    env.update({'ASK_OUT': out_dir, 'PYTHONIOENCODING': 'utf-8', 'PYTHONDONTWRITEBYTECODE': '1'})
    proc = subprocess.Popen(cmd, cwd=skill['dir'], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, env=env, start_new_session=True)
    chunks, size = [], [0]

    def drain():
        for block in iter(lambda: proc.stdout.read(65536), b''):
            if size[0] < TOOL_OUTPUT_CHARS * 4:
                chunks.append(block)
                size[0] += len(block)

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    deadline, timed_out, stopped = time.monotonic() + timeout, False, False
    while proc.poll() is None:
        if time.monotonic() > deadline:
            timed_out = True
        elif gone():
            stopped = True
        if timed_out or stopped:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                proc.kill()
            break
        time.sleep(0.1)
    rc = proc.wait()
    reader.join(5)
    proc.stdout.close()
    out = b''.join(chunks).decode('utf-8', 'replace')
    log.info('skill %s ran %s: rc=%s timed_out=%s stopped=%s', skill['name'], rel, rc, timed_out, stopped)
    return {'exit': rc, 'output': clip(out), 'timed_out': timed_out, 'stopped': stopped}


def list_outputs(out_dir):
    """输出目录里的文件，[(相对路径, 大小)]，最多 MAX_OUTPUT_FILES 个。"""
    found = []
    for root, dirs, names in os.walk(out_dir):
        dirs.sort()
        for n in sorted(names):
            p = os.path.join(root, n)
            if os.path.isfile(p) and not os.path.islink(p):
                found.append((os.path.relpath(p, out_dir), os.path.getsize(p)))
    return found[:MAX_OUTPUT_FILES]


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


def is_loopback(base):
    try:
        return (urllib.parse.urlsplit(base).hostname or '') in LOOPBACK
    except ValueError:
        return False


# 本机地址的接口不走 http_proxy 等环境变量里的代理
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def open_url(req, timeout):
    return (DIRECT.open if is_loopback(req.full_url) else urllib.request.urlopen)(req, timeout=timeout)


def get_json(url, timeout=PROBE_TIMEOUT):
    """GET 一个 JSON 接口；连不上、出错或不是 JSON 时返回 None。"""
    try:
        with open_url(urllib.request.Request(url, headers={'Accept': 'application/json'}), timeout) as r:
            return json.loads(r.read(1024 * 1024))
    except (OSError, ValueError):
        return None


def positive_int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def llama_ctx(root):
    """llama-server 每个槽位的上下文（/props 的 n_ctx），读不到时为 0。"""
    props = get_json(root + '/props')
    if not isinstance(props, dict):
        return 0
    gen = props.get('default_generation_settings')
    return positive_int(gen.get('n_ctx')) if isinstance(gen, dict) and gen.get('n_ctx') else \
        positive_int(props.get('n_ctx'))


def ollama_ctx(root):
    """Ollama 已加载模型的上下文 {模型名: token 数}（/api/ps 的 context_length）。"""
    ps = get_json(root + '/api/ps')
    out = {}
    for m in (ps.get('models') if isinstance(ps, dict) and isinstance(ps.get('models'), list) else []):
        if isinstance(m, dict) and positive_int(m.get('context_length')):
            for k in ('name', 'model'):
                if isinstance(m.get(k), str):
                    out[m[k]] = m['context_length']
    return out


def discover_local(servers=None):
    """探测本机的 llama-server 与 Ollama，返回 {id: 模型接口}，id 为 local:类型/模型。
    每项另带 ctx（上下文 token 数，读不到为 0）与 ctx_note（上下文从哪来）。"""
    out = {}
    for kind, port, name in servers or LOCAL_SERVERS:
        root = 'http://127.0.0.1:%d' % port
        listed = get_json(root + '/v1/models')
        data = listed.get('data') if isinstance(listed, dict) else None
        ids = [m['id'] for m in data if isinstance(m, dict) and isinstance(m.get('id'), str) and m['id']] \
            if isinstance(data, list) else []
        if not ids:
            continue
        if kind == 'llama':
            n = llama_ctx(root)
            ctxs = {i: (n, '本机 llama-server 上下文 %d token' % n if n else '') for i in ids}
        else:
            loaded = ollama_ctx(root)
            env = positive_int(int(os.environ['OLLAMA_CONTEXT_LENGTH'])) \
                if os.environ.get('OLLAMA_CONTEXT_LENGTH', '').isdigit() else 0
            guess = env or OLLAMA_DEFAULT_CTX
            ctxs = {i: (loaded[i], '本机 Ollama 上下文 %d token' % loaded[i]) if i in loaded else
                    (guess, 'Ollama 模型尚未加载，按上下文 %d token 估算；需要更长时启动 Ollama 前设置 '
                            'OLLAMA_CONTEXT_LENGTH，加载后点「重新查找」' % guess) for i in ids}
        for i in ids:
            out['local:%s/%s' % (kind, i)] = {
                'base': root + '/v1', 'key': '', 'model': i, 'headers': {}, 'label': '%s（本机 %s）' % (i, name),
                'host': '127.0.0.1:%d' % port, 'discovered': True, 'ctx': ctxs[i][0], 'ctx_note': ctxs[i][1]}
    log.info('local models: %s', ', '.join(out) or '-')
    return out


class LocalModels:
    """最近一次探测到的本机模型；打开页面、展开「模型设置」或点「重新查找」时重新探测。"""

    def __init__(self, servers=None):
        self.servers = servers
        self.lock = threading.Lock()
        self.found = {}

    def refresh(self):
        found = discover_local(self.servers)
        with self.lock:
            self.found = found
        return found

    def get(self):
        with self.lock:
            return dict(self.found)


def model_limit(config, tg):
    """这个模型的发送字数上限：设置值与「上下文 × CTX_SHARE」中较小的。"""
    ctx = tg.get('ctx') or 0
    return min(config.char_limit, int(ctx * CTX_SHARE)) if ctx else config.char_limit


def model_targets(config, local=None):
    """可用的模型：{id: {base, key, model, headers, label, host}}，以及默认 id。
    OpenCode 配置里每个服务商的每个模型一项（id 为 oc:服务商/模型），探测到的本机模型各一项（local:类型/模型），
    设置里填了接口地址和模型名的自定义接口一项（custom，AK 可不填）。"""
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
    if local is not None:
        out.update(local.get())
    c = config.custom
    if c['base'] and c['model']:
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
SCRIPT_NOTE = '（脚本输出）'
MEMORY_NOTE = ('标「来自记忆」的文件是以前提问时用过的文件的节选，按这次的问题从本机记忆里找出，已按文件当前内容复核；'
               '其中「以前的结论」只是线索，引用时照样写（文件 N：「原文摘句」），摘句从该文件「原文节选」里逐字照抄。')
NO_MEMORY_HIT = '没有选文件，记忆里也没有相关内容：请先在「找文件」里加入要用的文件，或选一个 skill'
SKILL_INTRO = ('使用者选用了 skill「%s」，下面是它的说明，按说明完成使用者的要求。'
               '说明里提到的 skill 目录文件用 read_skill_file 读取，脚本用 run_script 运行，要交给脚本的长内容先用 '
               + WRITE_TOOL + ' 写进工作目录（只能用这几个工具，'
               '没有其他工具；说明里要求联网、保存到别处或询问使用者的步骤，改为在回答里直接给出结果）。')
SKILL_CITE_NOTE = '依据脚本输出或 skill 参考文件得出的内容，在后面写「' + SCRIPT_NOTE + '」，不必附原文摘句。'
TOOL_LIMIT_NOTE = '工具调用次数已到上限（%d 次），不能再调用工具，请根据已有结果直接回答。' % MAX_TOOL_CALLS
WRITE_LIMIT_NOTE = '写入次数已到上限（%d 次），没有写入；请用已写入的文件运行脚本，或直接回答。' % MAX_WRITE_CALLS


def skill_prompt(skill):
    return SKILL_INTRO % skill['name'] + '\n\n===== skill 说明 =====\n' + skill['body']


def memory_text(block):
    """记忆里一个文件的原文节选：摘句按它核对。"""
    return '\n\n'.join(block['paras'])


def memory_part(n, block):
    lines = ['===== 文件 %d：%s（来自记忆，节选）=====' % (n, block['path'])]
    if block['facts']:
        lines.append('以前的结论（只作线索）：')
        lines += ['- %s（原文：「%s」）' % (f['conclusion'], f['quote']) for f in block['facts']]
        lines.append('')
    lines.append('原文节选：')
    return '\n'.join(lines) + '\n' + memory_text(block)


def build_messages(files, history, skill=None, memory=()):
    """files: [(路径, 文字)]；history: [{role, content}]，最后一条是使用者的新问题；skill 为所选 skill；
    memory: 从记忆里找出的 [{'path', 'facts', 'paras'}]，编号接在已选文件之后。"""
    parts = [skill_prompt(skill)] if skill else []
    if files or memory:
        parts.append(CITE_RULE + (SKILL_CITE_NOTE if skill else ''))
    if files:
        parts.append('使用者选了下面 %d 个文件，以下是从中取出的文字。' % len(files))
    else:
        parts.append('使用者没有选文件。')
    if memory:
        parts.append(MEMORY_NOTE)
    for i, (path, text) in enumerate(files, 1):
        parts.append('===== 文件 %d：%s =====\n%s' % (i, path, text))
    for i, block in enumerate(memory, len(files) + 1):
        parts.append(memory_part(i, block))
    return [{'role': 'system', 'content': '\n\n'.join(parts)}] + history


def memory_public(memory, first):
    """页面上「参考了记忆」的说明：用到的结论与段落及其出处。"""
    return {'facts': sum(len(b['facts']) for b in memory), 'paras': sum(len(b['paras']) for b in memory),
            'files': [{'n': i, 'path': b['path'], 'paras': len(b['paras']),
                       'facts': [{'conclusion': f['conclusion'], 'quote': f['quote']} for f in b['facts']]}
                      for i, b in enumerate(memory, first)]}


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


def http_error_text(code, detail, tools=False, local=False):
    """把接口的 HTTP 错误写成「原因：下一步怎么做」；local 为本机地址的接口。"""
    if tools and code in (400, 404, 422, 500, 501) and re.search(r'tool|function', detail, re.I):
        return ('此模型不支持 skill 工具（函数调用，HTTP %d）：请改用「不用 skill」或不带脚本和参考文件的 skill，'
                '或换一个支持函数调用的模型' % code) + ('；本机 llama-server 须加 --jinja 启动才支持函数调用'
                                                     if local else '')
    if code in (401, 403):
        return '接口拒绝了请求（HTTP %d），多半是 AK 无效或过期：请检查 OpenCode 配置或「模型设置」里的 AK' % code
    if code == 404:
        return '接口地址或模型名不对（HTTP 404）：请检查接口地址和模型名'
    if code in (400, 413) and re.search(r'context|length|too long|token|长度|过长', detail, re.I):
        return '内容超过模型能接受的长度（HTTP %d）：请移除几个文件、调低字数上限或开始新对话' % code
    if code == 429:
        return '请求太频繁或额度已用完（HTTP 429）：请稍后再试'
    return '接口返回 HTTP %d%s' % (code, '：' + detail if detail else '')


def model_timeout(tg):
    return LOCAL_TIMEOUT if is_loopback(tg['base']) else TIMEOUT


def stream_chat(tg, msgs, timeout=None, tools=None):
    """从 OpenAI 兼容接口逐段取回答（逐段产出 str）；接口不流式时整段返回。给了 tools 且模型要调用时，
    最后产出一个列表 [{'id', 'name', 'arguments'}]。出错抛 ModelError，写明原因。
    timeout 默认按接口地址取：本机 LOCAL_TIMEOUT，其他 TIMEOUT。没有 AK 时不发 Authorization。"""
    timeout = timeout or model_timeout(tg)
    req_body = {'model': tg['model'], 'messages': msgs, 'stream': True}
    if tools:
        req_body['tools'] = tools
    body = json.dumps(req_body).encode('utf-8')
    headers = {'Content-Type': 'application/json', 'Accept': 'text/event-stream'}
    if tg['key']:
        headers['Authorization'] = 'Bearer ' + tg['key']
    headers.update(tg['headers'])
    req = urllib.request.Request(tg['base'] + '/chat/completions', data=body, method='POST', headers=headers)
    try:
        r = open_url(req, timeout)
    except urllib.error.HTTPError as e:
        try:
            detail = ' '.join(e.read(300).decode('utf-8', 'replace').split())
        except OSError:
            detail = ''
        raise ModelError(http_error_text(e.code, detail, bool(tools), is_loopback(tg['base'])))
    except (socket.timeout, TimeoutError):
        raise ModelError('接口 %d 秒没有响应' % timeout)
    except urllib.error.URLError as e:
        if tg.get('discovered') and not isinstance(e.reason, (socket.timeout, TimeoutError)):
            raise ModelError(LOCAL_DOWN)
        raise ModelError('连不上接口：%s' % e.reason)
    except OSError as e:
        if tg.get('discovered'):
            raise ModelError(LOCAL_DOWN)
        raise ModelError('连不上接口：%s' % e)
    try:
        with r:
            calls = {}      # 序号 -> {'id', 'name', 'arguments'}，流式时分段拼接
            if 'event-stream' not in r.headers.get('Content-Type', ''):
                try:
                    msg = json.loads(r.read())['choices'][0]['message']
                    text = msg.get('content') or ''
                    for i, c in enumerate(msg.get('tool_calls') or []):
                        fn = c.get('function') or {}
                        calls[i] = {'id': c.get('id') or '', 'name': fn.get('name') or '',
                                    'arguments': fn.get('arguments') or ''}
                except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                    raise ModelError('接口返回的内容看不懂')
                if text:
                    yield text
            else:
                for raw in r:
                    line = raw.decode('utf-8', 'replace').strip()
                    if not line.startswith('data:'):
                        continue
                    data = line[5:].strip()
                    if data == '[DONE]':
                        break
                    try:
                        delta = json.loads(data)['choices'][0].get('delta') or {}
                    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                        continue
                    for c in delta.get('tool_calls') or []:
                        if not isinstance(c, dict):
                            continue
                        slot = calls.setdefault(c.get('index', len(calls)), {'id': '', 'name': '', 'arguments': ''})
                        fn = c.get('function') or {}
                        slot['id'] = c.get('id') or slot['id']
                        slot['name'] += fn.get('name') or ''
                        slot['arguments'] += fn.get('arguments') or ''
                    piece = delta.get('content')
                    if piece:
                        yield piece
            if tools and calls:
                yield [calls[k] for k in sorted(calls)]
    except (socket.timeout, TimeoutError):
        raise ModelError('接口 %d 秒没有新内容' % timeout)
    except OSError as e:
        raise ModelError('连接中断：%s' % e)




# ---------------------------------------------------------------- 目录选择框

# tkinter 在子进程里弹框：不在服务线程里跑 Tk；出错时退出码 2，取消时 1
TK_PICK = r"""
import sys
try:
    import tkinter
    from tkinter import filedialog
    root = tkinter.Tk()
    root.withdraw()
    path = filedialog.askdirectory(initialdir=sys.argv[1], title=sys.argv[2], mustexist=True)
    root.destroy()
except Exception as e:
    sys.stderr.write(str(e) or e.__class__.__name__)
    sys.exit(2)
if not path:
    sys.exit(1)
sys.stdout.write(path)
"""


def detect_picker(env=None):
    """可用的目录选择框：依次看 zenity、kdialog、tkinter，返回第一个可用的名称；没有图形环境或都不可用时返回空字符串。"""
    env = os.environ if env is None else env
    if not env.get('DISPLAY') and not env.get('WAYLAND_DISPLAY'):
        return ''
    for name in ('zenity', 'kdialog'):
        if shutil.which(name, path=env.get('PATH', os.defpath)):
            return name
    try:
        has_tk = importlib.util.find_spec('tkinter') is not None
    except (ImportError, ValueError):
        has_tk = False
    return 'tkinter' if has_tk and env.get('DISPLAY') else ''


class PickerBusy(Exception):
    pass


class FolderPicker:
    """由本机服务弹出系统的目录选择框（浏览器拿不到完整路径）；同一时刻只开一个。"""

    def __init__(self, kind):
        self.kind = kind
        self.lock = threading.Lock()
        self.proc = None
        self.page = None

    def busy(self):
        with self.lock:
            return self.proc is not None

    def command(self, start, title):
        if self.kind == 'zenity':
            return ['zenity', '--file-selection', '--directory', '--title=' + title,
                    '--filename=' + start.rstrip(os.sep) + os.sep]
        if self.kind == 'kdialog':
            return ['kdialog', '--title', title, '--getexistingdirectory', start]
        return [sys.executable, '-c', TK_PICK, start, title]

    def pick(self, start, title, page='', gone=lambda: False):
        """弹框选文件夹，返回路径，取消（或页面已断开）时返回 None。已有选择框时抛 PickerBusy，失败时抛 ValueError。"""
        if not self.kind:
            raise ValueError('本机没有可用的选择窗口')
        env = dict(os.environ, PYTHONIOENCODING='utf-8')
        with self.lock:
            if self.proc:
                raise PickerBusy()
            try:
                proc = subprocess.Popen(self.command(start, title), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, env=env, start_new_session=True)
            except OSError as e:
                raise ValueError(e.strerror or str(e))
            self.proc, self.page = proc, page
        try:
            while True:     # 选择框开着时不设超时，页面断开时结束它
                try:
                    out, err = proc.communicate(timeout=0.3)
                    break
                except subprocess.TimeoutExpired:
                    if gone():
                        self._kill(proc)
                        proc.communicate()
                        return None
        finally:
            with self.lock:
                self.proc = self.page = None
        rc = proc.returncode
        path = out.decode('utf-8', 'replace').rstrip('\r\n')
        log.info('folder picker %s finished: rc=%s', self.kind, rc)
        if rc == 0 and path:
            return path
        if rc == 1 or rc < 0:
            return None
        lines = [x.strip() for x in err.decode('utf-8', 'replace').splitlines() if x.strip()]
        raise ValueError((lines[-1][:200] if lines else '') or '%s 退出码 %s' % (self.kind, rc))

    @staticmethod
    def _kill(proc):
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            proc.kill()

    def close(self, page=None):
        """结束开着的选择框；给了 page 时只结束由这个页面打开的。"""
        with self.lock:
            proc = self.proc if self.proc and (page is None or self.page == page) else None
        if proc:
            log.info('closing folder picker')
            self._kill(proc)


# ---------------------------------------------------------------- 网页服务

def open_folder(folder):
    """用 OPENERS 里第一个找得到的命令打开文件夹，不等它结束；没有命令或启动失败时抛 OSError。"""
    for cmd in OPENERS:
        if shutil.which(cmd[0]):
            try:
                proc = subprocess.Popen(list(cmd) + [folder], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, start_new_session=True)
            except OSError as e:
                raise OSError('无法启动 %s（%s）' % (cmd[0], e.strerror or e))
            threading.Thread(target=proc.wait, daemon=True).start()     # 回收子进程
            return
    raise OSError('本机没有 xdg-open 或 gio，无法打开文件管理器')


def reveal_file(path):
    """打开文件所在的文件夹并尽量选中文件：先调 FileManager1.ShowItems（gdbus，没有时 dbus-send），
    再试 SELECT_OPENERS，都不行时用 open_folder 只打开文件夹。返回是否已选中；连文件夹也打不开时抛 OSError。"""
    uri = 'file://' + urllib.parse.quote(path)
    for cmd in SHOW_ITEMS:
        if not shutil.which(cmd[0]):
            continue
        try:
            r = subprocess.run([a.replace('{uri}', uri) for a in cmd], stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=REVEAL_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as e:
            log.info('%s failed: %s', cmd[0], e)
            break
        if r.returncode == 0:
            return True
        log.info('%s exited %d', cmd[0], r.returncode)
        break       # 两种 D-Bus 命令调的是同一个接口，一个失败另一个也不会成
    for cmd in SELECT_OPENERS:
        if shutil.which(cmd[0]):
            try:
                proc = subprocess.Popen(list(cmd) + [uri], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, start_new_session=True)
            except OSError as e:
                log.info('cannot start %s: %s', cmd[0], e)
                continue
            threading.Thread(target=proc.wait, daemon=True).start()     # 回收子进程
            return True
    open_folder(os.path.dirname(path))
    return False


def log_file_path():
    """日志放工具目录，不可写时放 ~/.cache/ask-local-files/。"""
    if os.access(HERE, os.W_OK):
        return os.path.join(HERE, LOG_NAME)
    d = os.path.join(XDG_CACHE, 'ask-local-files')
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, LOG_NAME)


class WebState:
    def __init__(self, port, token, config, index, memory=None):
        self.port = port
        self.token = token
        self.origin = 'http://127.0.0.1:%d' % port
        self.config = config
        self.index = index
        self.texts = TextCache()
        self.memory = memory or ask_memory.Memory(MEMORY_FILE)     # 用到时才打开库文件
        self.started = time.monotonic()
        self.lock = threading.Lock()
        self.pages = {}         # 页面编号 -> (最近一次心跳时间, 所在窗口编号)
        self.seen = False
        self.empty_since = None
        self.quit = threading.Event()
        self.chats = 0          # 正在生成的回答数
        self.out_root = None    # skill 脚本输出的临时目录，退出时删除
        self.outputs = {}       # 输出文件编号 -> (路径, 文件名)
        self.picker = FolderPicker(detect_picker())
        self.local_models = LocalModels()

    def ping(self, page, shell=None):
        """登记心跳，返回当前开着的窗口数：同一窗口里各标签的页面（shell 相同）算一个。"""
        with self.lock:
            self.pages[page] = (time.monotonic(), shell or page)
            self.seen = True
            self.empty_since = None
            return len({s for _, s in self.pages.values()})

    def bye(self, page):
        self.picker.close(page)
        with self.lock:
            self.pages.pop(page, None)
            if self.seen and not self.pages and self.empty_since is None:
                self.empty_since = time.monotonic()

    def new_output_dir(self, prefix='tmp'):
        with self.lock:
            if not self.out_root:
                self.out_root = tempfile.mkdtemp(prefix='ask-local-files-out-')
            root = self.out_root
        return tempfile.mkdtemp(prefix=prefix, dir=root)

    def new_work_dir(self):
        """每个问题一个工作目录，供 write_work_file 写入、脚本读取；回答结束时由调用方删除。"""
        return self.new_output_dir('work-')

    def register_outputs(self, out_dir):
        out = []
        for rel, size in list_outputs(out_dir):
            fid = secrets.token_urlsafe(12)
            with self.lock:
                self.outputs[fid] = (os.path.join(out_dir, rel), os.path.basename(rel))
            out.append({'id': fid, 'name': rel, 'size': size})
        return out

    def cleanup(self):
        self.picker.close()
        if self.out_root:
            shutil.rmtree(self.out_root, ignore_errors=True)

    def chat_begin(self):
        with self.lock:
            self.chats += 1

    def chat_end(self):
        with self.lock:
            self.chats -= 1

    def should_exit(self):
        """返回退出原因，不该退出时返回 None；有回答正在生成或选择框开着时一律不退出。"""
        if self.picker.busy():      # 选择框开着时也不按心跳把页面判为已关闭
            return None
        now = time.monotonic()
        with self.lock:
            for page, (t, _) in list(self.pages.items()):
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


def models_public(config, local=None):
    """页面用的模型清单：loopback 为本机地址（等待更久），auth 为是否带 AK，limit 为按上下文折算的字数上限。"""
    targets, default = model_targets(config, local)
    return {'models': [{'id': k, 'label': v['label'], 'host': v['host'], 'loopback': is_loopback(v['base']),
                        'auth': bool(v['key']), 'limit': int(v['ctx'] * CTX_SHARE) if v.get('ctx') else 0,
                        'limit_note': v.get('ctx_note') or ''} for k, v in targets.items()],
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
            st.local_models.refresh()
            out = st.config.public()
            out.update(models_public(st.config, st.local_models))
            out.update({'index': st.index.status(), 'version': VERSION, 'max_files': MAX_FILES,
                        'picker': bool(st.picker.kind),
                        'pdftotext': bool(shutil.which('pdftotext')), 'doc2kb': doc2kb_core is not None})
            out['memory_stats'] = self.memory_stats()
            self.send_json(200, out)
        elif url.path == '/api/dirs':
            self.send_json(200, {'dirs': list_dirs(arg('prefix')), 'places': common_places()})
        elif url.path == '/api/memory':
            try:
                out = st.memory.listing(arg('q').strip()[:200])
            except (sqlite3.Error, OSError) as e:
                log.warning('memory: listing failed: %s', e)
                self.send_json(500, {'error': '读不了记忆（%s）' % e})
                return
            out['stats'] = self.memory_stats()
            self.send_json(200, out)
        elif url.path == '/api/skills':
            skills, problems = load_skills()
            self.send_json(200, {'skills': skills_public(skills), 'problems': problems, 'user_dir': USER_SKILLS})
        elif url.path == '/api/output':
            with st.lock:
                hit = st.outputs.get(arg('id'))
            if not hit or not os.path.isfile(hit[0]):
                self.send_json(404, {'error': '文件已不存在（服务重启后输出文件会删除），请重新提问生成'})
                return
            with open(hit[0], 'rb') as f:
                body = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + urllib.parse.quote(hit[1]))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)
        elif url.path == '/api/search':
            try:
                self.send_json(200, st.index.search(arg('q'), arg('ext'), arg('re') == '1', sort_arg(arg('sort'))))
            except re.error as e:
                self.send_json(400, {'error': '正则表达式有误：%s' % e})
        elif url.path == '/api/browse':
            dedup = arg('dedup')
            self.send_json(200, st.index.browse(arg('dir'), dedup == '1' if dedup else st.config.dedup, sort_arg(arg('sort'))))
        elif url.path == '/api/text':
            path = st.index.resolve(arg('path'))
            if not path:
                self.send_json(400, {'error': MISSING_FILE})
                return
            try:
                text, note = st.texts.get(path)
            except Exception as e:      # 转换器对损坏文件可能抛出任何异常
                log.info('extract failed: %r (%s)', path, e)
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
            shell = data.get('shell')
            self.send_json(200, {'ok': True, 'pages': st.ping(str(data.get('page')), str(shell) if shell else None)})
        elif url.path == '/api/quit':
            busy = st.chats > 0
            log.info('quit requested by page%s', ', waiting for answers' if busy else '')
            self.send_json(200, {'ok': True, 'waiting': busy})
            st.picker.close()
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
        elif url.path == '/api/memory':
            self.post_memory(data)
        elif url.path == '/api/test-model':
            self.post_test_model(data)
        elif url.path == '/api/local-models':
            found = st.local_models.refresh()
            out = models_public(st.config, st.local_models)
            out['found'] = {k: sum(1 for i in found if i.startswith('local:%s/' % k)) for k, _, _ in LOCAL_SERVERS}
            out['servers'] = [{'kind': k, 'port': p, 'name': n} for k, p, n in LOCAL_SERVERS]
            self.send_json(200, out)
        elif url.path == '/api/pick-folder':
            self.post_pick_folder(data)
        elif url.path == '/api/open-folder':
            self.post_open_folder(data)
        elif url.path == '/api/docx':
            self.post_docx(data)
        elif url.path == '/api/export':
            self.post_export(data)
        else:
            self.send_json(404, {'error': '没有这个地址'})

    def memory_stats(self):
        try:
            return self.state.memory.stats()
        except (sqlite3.Error, OSError) as e:
            log.warning('memory: stats failed: %s', e)
            return {'files': 0, 'facts': 0, 'bytes': 0, 'max_bytes': ask_memory.MAX_BYTES, 'error': str(e)}

    def post_memory(self, data):
        """记忆的管理：「不对」（wrong）、删一条结论（fact）、忘掉一个文件（file）、清空（clear）。"""
        mem, action = self.state.memory, data.get('action')
        try:
            if action == 'wrong' and isinstance(data.get('answer_id'), str) and data['answer_id']:
                n = mem.wrong(data['answer_id'])
            elif action == 'fact' and isinstance(data.get('id'), int) and not isinstance(data['id'], bool):
                n = int(mem.forget_fact(data['id']))
            elif action == 'file' and isinstance(data.get('path'), str):
                n = int(mem.forget_file(data['path']))
            elif action == 'clear':
                mem.clear()
                n = 0
            else:
                self.send_json(400, {'error': '不认识的记忆操作'})
                return
        except (sqlite3.Error, OSError) as e:
            # 页面传来的取值只记已知的几种，异常文字去掉换行，免得伪造日志行
            act = {'wrong': 'wrong', 'fact': 'fact', 'file': 'file', 'clear': 'clear'}.get(action, '?')
            log.warning('memory: %s failed: %s', act, str(e).replace('\r', ' ').replace('\n', ' '))
            self.send_json(500, {'error': '记忆写不进去（%s）' % e})
            return
        self.send_json(200, {'ok': True, 'count': n, 'stats': self.memory_stats()})

    def post_docx(self, data):
        """「保存为 Word」：把页面发来的 Markdown 转成 docx 返回，不落盘。"""
        text, title = data.get('markdown'), data.get('title') or ''
        if not isinstance(text, str) or not text.strip() or not isinstance(title, str):
            self.send_json(400, {'error': '没有可保存的内容'})
            return
        try:
            body = markdown_docx(text, title[:200])
        except Exception as e:      # 转换脚本缺失或出错时都只回报原因
            log.error('docx failed: %s', e)
            self.send_json(500, {'error': '转换出错（%s）' % (str(e) or e.__class__.__name__)})
            return
        self.send_response(200)
        self.send_header('Content-Type', DOCX_TYPE)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def post_export(self, data):
        """把已选文件取出的文字拼成与发给模型相同的系统消息（原文佐证要求加全部文件，不带 skill），供页面复制、保存。
        取不出文字的文件跳过，写明个数；不受发送字数上限限制。"""
        st = self.state
        files = data.get('files')
        if not isinstance(files, list) or not files or len(files) > MAX_FILES:
            self.send_json(400, {'error': '请选 1 到 %d 个文件' % MAX_FILES})
            return
        got = []
        for asked in files:
            path = st.index.resolve(asked)
            if not path:
                self.send_json(400, {'error': '%s：%s' % (MISSING_FILE, asked)})
                return
            try:
                got.append((path, st.texts.get(path)[0]))
            except Exception as e:      # 转换器对损坏文件可能抛出任何异常
                log.info('extract failed: %r (%s)', path, e)
        if not got:
            self.send_json(400, {'error': '已选文件都取不出文字'})
            return
        text = build_messages(got, [])[0]['content']
        self.send_json(200, {'text': text, 'chars': len(text), 'files': len(got), 'skipped': len(files) - len(got)})

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
            if 'dedup' in data:
                cfg.dedup = bool(data['dedup'])
            if 'regex' in data:
                cfg.regex = bool(data['regex'])
            for k in ('memory', 'use_memory', 'memory_notice'):
                if k in data:
                    setattr(cfg, k, bool(data[k]))
            cfg.save()
        out = cfg.public()
        out.update(models_public(cfg, self.state.local_models))
        self.send_json(200, out)


    def post_open_folder(self, data):
        """用文件管理器打开已索引文件所在的文件夹并尽量选中它；打不开时带上文件夹路径，页面让使用者自己去打开。"""
        path = self.state.index.resolve(data.get('path'))
        if not path:
            self.send_json(400, {'error': MISSING_FILE})
            return
        folder = os.path.dirname(path)
        if not os.path.isdir(folder):
            self.send_json(400, {'error': MISSING_FILE})
            return
        try:
            selected = reveal_file(path)
        except OSError as e:
            log.info('cannot open folder %r: %s', folder, e)
            self.send_json(500, {'error': str(e), 'folder': folder})
            return
        self.send_json(200, {'ok': True, 'folder': folder, 'selected': selected})

    def post_pick_folder(self, data):
        """弹出目录选择框，只把选中的路径交给页面，不添加、不保存；取消时 path 为 null。"""
        title = ' '.join(str(data.get('title') or '选择文件夹').split())[:100]
        try:
            path = self.state.picker.pick(start_dir(data.get('start')), title, str(data.get('page') or ''),
                                          self.client_gone)
        except PickerBusy:
            self.send_json(409, {'error': '已有选择窗口打开：请先在那个窗口里选好或取消'})
            return
        except ValueError as e:
            self.send_json(200, {'ok': False, 'error': str(e)})
            return
        self.send_json(200, {'ok': True, 'path': path})

    def post_test_model(self, data):
        """向所选模型发一条极短的请求，拿到第一段回答即算连通。"""
        targets, default = model_targets(self.state.config, self.state.local_models)
        tg = targets.get(data.get('model') or default)
        if not tg:
            self.send_json(200, {'ok': False, 'error': NO_MODEL})
            return
        reply = ''
        try:
            # 本机模型第一次请求要先加载模型，测试连接也按普通接口的时限等
            for piece in stream_chat(tg, [{'role': 'user', 'content': '请只回复「好」。'}],
                                     timeout=TIMEOUT if is_loopback(tg['base']) else TEST_TIMEOUT):
                reply = piece
                break
        except ModelError as e:
            self.send_json(200, {'ok': False, 'error': str(e)})
            return
        self.send_json(200, {'ok': True, 'model': tg['label'], 'reply': reply[:50]})

    def post_chat(self, data):
        st = self.state
        files, history = data.get('files'), data.get('messages')
        skill = None
        if data.get('skill'):
            skill = load_skills()[0].get(str(data['skill']))
            if not skill:
                self.send_json(400, {'error': '找不到 skill「%s」，请点「重新读取」后重选' % data['skill']})
                return
            if skill['too_long']:
                self.send_json(400, {'error': 'skill「%s」的说明超过 %d 字，不能选用' % (skill['name'], SKILL_MAX_CHARS)})
                return
        use_mem = st.config.memory and data.get('memory') is True
        if not isinstance(files, list) or len(files) > MAX_FILES or (not files and not skill and not use_mem):
            self.send_json(400, {'error': '请选 1 到 %d 个文件' % MAX_FILES})
            return
        if not isinstance(history, list) or not history or not all(
                isinstance(m, dict) and m.get('role') in ('user', 'assistant') and isinstance(m.get('content'), str)
                for m in history) or history[-1]['role'] != 'user':
            self.send_json(400, {'error': '请先写下要问的内容'})
            return
        if not history[-1]['content'].strip():
            if not skill:
                self.send_json(400, {'error': '请先写下要问的内容'})
                return
            history[-1]['content'] = EMPTY_ASK
        targets, default = model_targets(st.config, st.local_models)
        tg = targets.get(data.get('model') or default)
        if not tg:
            self.send_json(400, {'error': NO_MODEL})
            return
        texts, paths = [], []
        for asked in files:
            path = st.index.resolve(asked)
            if not path:
                self.send_json(400, {'error': '%s：%s' % (MISSING_FILE, asked)})
                return
            paths.append(path)
            try:
                texts.append(st.texts.get(path)[0])
            except Exception as e:
                self.send_json(400, {'error': '取不出文字：%s（%s）' % (path, e)})
                return
        files = paths       # 之后只用索引里存的路径
        if st.config.memory:
            self.remember_files(paths, texts)
        # 字数上限管文件、skill 说明与对话的合计；截断只截文件，给 skill 和对话留出位置
        limit = model_limit(st.config, tg)
        files_chars, history_chars = sum(len(t) for t in texts), sum(len(m['content']) for m in history)
        skill_chars = len(skill['body']) if skill else 0
        fixed = history_chars + skill_chars
        total = files_chars + fixed
        if fixed >= limit:
            self.send_json(413, {'error': 'history_too_long', 'history': history_chars, 'skill': skill_chars,
                                 'limit': limit})
            return
        if total > limit and not data.get('truncate'):
            self.send_json(413, {'error': 'too_long', 'total': total, 'files': files_chars,
                                 'history': history_chars, 'skill': skill_chars, 'limit': limit})
            return
        # 记忆：没选文件时可用全部余量；选了文件时至多占余量的 1/MEMORY_SHARE，且只用文件用不完的部分，
        # 不因记忆而要求截断；已在截断时照样给记忆留出这一份
        room = limit - fixed
        if not files:
            budget = room
        elif data.get('truncate'):
            budget = room // MEMORY_SHARE
        else:
            budget = min(room // MEMORY_SHARE, room - files_chars)
        # 按最近两问检索：追问（如「那第二阶段呢」）与「继续」也能带上前一问的关键字
        asks = [m['content'] for m in history if m['role'] == 'user'][-2:]
        memory = self.recall('\n'.join(reversed(asks)), paths, budget) if use_mem else []
        mem_chars = sum(len(memory_part(i, b)) for i, b in enumerate(memory, 1))
        while memory and mem_chars > (room - files_chars if files and not data.get('truncate') else room):
            # 估算预算时没算每个文件的标题行：放不下时从最不相关的文件起去掉
            mem_chars -= len(memory_part(len(memory), memory.pop()))
        if not files and not skill and not memory:
            self.send_json(400, {'error': NO_MEMORY_HIT})
            return
        mem_texts = [memory_text(b) for b in memory]
        full = texts
        texts, cut = fit_texts(texts, room - mem_chars)
        msgs = build_messages(list(zip(files, texts)), [{'role': m['role'], 'content': m['content']}
                                                       for m in history], skill, memory)
        tools = skill_tools(skill) if skill else None
        log.info('chat: %d files, %d chars, %d from memory, model %s, host %s, skill %s, tools %s', len(files),
                 total, mem_chars, tg['model'], tg['host'], skill['name'] if skill else '-', bool(tools))
        self.send_response(200)
        self.send_header('Content-Type', 'application/x-ndjson; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        st.chat_begin()
        work = None
        emit = lambda obj: (self.wfile.write((json.dumps(obj, ensure_ascii=False) + '\n').encode('utf-8')),
                            self.wfile.flush())
        try:
            emit({'start': True, 'truncated': cut, 'model': tg['model'], 'host': tg['host'],
                  'skill': skill['name'] if skill else '', 'memory': memory_public(memory, len(files) + 1)})
            pieces, used, writes = [], 0, 0
            if skill and skill['scripts']:
                work = st.new_work_dir()
            while True:
                offered = tools if used < MAX_TOOL_CALLS else None
                if offered and writes >= MAX_WRITE_CALLS:
                    offered = [t for t in offered if t['function']['name'] != WRITE_TOOL]
                calls = None
                for piece in stream_chat(tg, msgs, tools=offered):
                    if isinstance(piece, list):
                        calls = piece
                    else:
                        pieces.append(piece)
                        emit({'t': piece})
                if not calls:
                    break
                msgs.append({'role': 'assistant', 'content': None, 'tool_calls': [
                    {'id': c['id'] or 'call_%d' % i, 'type': 'function',
                     'function': {'name': c['name'], 'arguments': c['arguments']}} for i, c in enumerate(calls)]})
                for i, c in enumerate(calls):
                    # 写入单独计数；超出写入次数的调用不执行，并按普通工具调用计数，保证总次数有上限
                    if c['name'] == WRITE_TOOL and writes < MAX_WRITE_CALLS:
                        writes += 1
                        result = self.run_tool(skill, c, files, emit, work)
                    elif c['name'] == WRITE_TOOL:
                        used += 1
                        result = WRITE_LIMIT_NOTE
                    else:
                        used += 1
                        result = self.run_tool(skill, c, files, emit, work) if used <= MAX_TOOL_CALLS \
                            else TOOL_LIMIT_NOTE
                    msgs.append({'role': 'tool', 'tool_call_id': c['id'] or 'call_%d' % i, 'content': result})
                if used >= MAX_TOOL_CALLS:
                    msgs.append({'role': 'user', 'content': TOOL_LIMIT_NOTE})
            answer = ''.join(pieces)
            if files or memory:
                count, missing = check_quotes(answer, full + mem_texts)
                uncited = count == 0 and NOT_FOUND not in answer and not (skill and SCRIPT_NOTE in answer)
            else:
                count, missing, uncited = 0, [], False
            answer_id, kept = '', 0
            if st.config.memory and count > len(missing):
                answer_id = secrets.token_urlsafe(9)
                sources = dict(enumerate(files + [b['path'] for b in memory], 1))
                kept = self.remember_answer(answer_id, history[-1]['content'], answer, sources, missing)
            log.info('answer: %d quotes, %d not found in files, %d remembered, %d tool calls, %d writes',
                     count, len(missing), kept, used, writes)
            emit({'done': True, 'quotes': count, 'missing': missing, 'uncited': uncited,
                  'answer_id': answer_id if kept else '', 'remembered': kept})
        except ModelError as e:
            log.warning('chat failed: %s', e)
            try:
                emit({'error': str(e)})
            except OSError:
                pass
        except OSError as e:
            log.info('page went away during answer: %s', e)
        finally:
            if work:
                shutil.rmtree(work, ignore_errors=True)
            st.chat_end()

    # 记忆出错（库文件损坏、磁盘满等）只记日志，不影响提问
    def remember_files(self, paths, texts):
        for path, text in zip(paths, texts):
            try:
                self.state.memory.remember_file(path, os.stat(path), text)
            except (sqlite3.Error, OSError) as e:
                log.warning('memory: cannot remember %s: %s', path, e)
                return

    def recall(self, question, exclude, budget):
        try:
            return self.state.memory.recall(question, exclude, budget, lambda p: self.state.texts.get(p)[0])
        except (sqlite3.Error, OSError) as e:
            log.warning('memory: recall failed: %s', e)
            return []

    def remember_answer(self, answer_id, question, answer, sources, missing):
        try:
            return self.state.memory.remember_answer(answer_id, question, answer, sources, missing)
        except (sqlite3.Error, OSError) as e:
            log.warning('memory: cannot remember answer: %s', e)
            return 0

    def client_gone(self):
        """页面断开（点了「停止」或关了页面）时返回 True。"""
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            return bool(readable) and self.connection.recv(1, socket.MSG_PEEK) == b''
        except (OSError, ValueError):
            return True

    def run_tool(self, skill, call, files, emit, work=None):
        """执行模型要求的一次工具调用，把过程发给页面，返回交给模型的结果文字。"""
        st = self.state
        try:
            args = json.loads(call['arguments'] or '{}')
            if not isinstance(args, dict):
                raise ValueError
        except ValueError:
            args = None
        name = call['name']
        kinds = {'read_skill_file': 'read', 'run_script': 'run', WRITE_TOOL: 'write'}
        info = {'kind': kinds.get(name, 'run'), 'name': '', 'args': []}
        if args is None:
            info.update(error='参数不是合法的 JSON')
        elif name == 'read_skill_file':
            info['name'] = str(args.get('path') or '')
        elif name == WRITE_TOOL and work:
            info.update(name=str(args.get('path') or ''), append=args.get('append') is True)
        elif name == 'run_script':
            info['name'] = str(args.get('script') or '')
            info['args'] = args.get('args') if isinstance(args.get('args'), list) else []
        else:
            info.update(name=name, error='没有这个工具：%s' % name)
        if 'error' in info:
            emit({'tool': info})
            return '调用失败：%s' % info['error']
        emit({'tool_start': info})
        try:
            if info['kind'] == 'read':
                text = read_skill_file(skill, info['name'])
                info['output'] = text
                emit({'tool': info})
                return text
            if info['kind'] == 'write':
                rel, n, total = write_work_file(work, info['name'], args.get('content'), info['append'])
                info.update(name=rel, chars=n, total=total, output=args.get('content'))
                emit({'tool': info})
                log.info('skill %s wrote %s: %d chars (total %d)', skill['name'], rel, n, total)
                return '已%s %s：本次 %d 字，文件共 %d 字。运行脚本时参数直接写 %s（脚本在 ASK_WORK 里找）。' % (
                    '追加到' if info['append'] else '写入', rel, n, total, rel)
            out_dir = st.new_output_dir()
            env = {'ASK_FILES': '\n'.join(files), 'ASK_ROOTS': '\n'.join(st.config.folders), 'ASK_TOOL_DIR': HERE}
            if work:
                env['ASK_WORK'] = work
            r = run_skill_script(skill, info['name'], args.get('args'), env, out_dir, gone=self.client_gone)
        except (ValueError, OSError) as e:
            info['error'] = str(e)
            emit({'tool': info})
            return '调用失败：%s' % e
        if r['stopped']:
            raise ConnectionAbortedError('page stopped the answer while a script was running')
        info.update(exit=r['exit'], output=r['output'], timed_out=r['timed_out'],
                    files=st.register_outputs(out_dir))
        emit({'tool': info})
        parts = ['退出码 %s' % r['exit']]
        if r['timed_out']:
            parts.append('超过 %d 秒未结束，已终止' % SCRIPT_TIMEOUT)
        parts.append('输出：\n' + (r['output'] or '（无）'))
        if info['files']:
            parts.append('生成的文件（使用者可在页面上保存）：' + '、'.join(f['name'] for f in info['files']))
        return clip('\n'.join(parts))


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


def desktop_dirs():
    """XDG 应用目录：XDG_DATA_HOME 与 XDG_DATA_DIRS 下的 applications。"""
    home = os.environ.get('XDG_DATA_HOME') or os.path.join(HOME, '.local', 'share')
    dirs = os.environ.get('XDG_DATA_DIRS') or '/usr/local/share:/usr/share'
    return [os.path.join(d, 'applications') for d in [home] + dirs.split(':') if d]


def desktop_exec(path):
    """桌面文件 [Desktop Entry] 里 Exec 的程序：绝对路径或 PATH 里的命令，存在且可执行时返回完整路径。"""
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    entry = False
    for line in lines:
        line = line.strip()
        if line.startswith('['):
            entry = line == '[Desktop Entry]'
        elif entry and line.startswith('Exec='):
            try:
                words = shlex.split(line[5:])
            except ValueError:
                return None
            while words and (words[0] == 'env' or '=' in words[0]):     # env 变量=值 程序 ……
                words = words[1:]
            if not words:
                return None
            exe = words[0] if os.path.isabs(words[0]) else shutil.which(words[0])
            return exe if exe and os.path.isfile(exe) and os.access(exe, os.X_OK) else None
    return None


def find_app_browser():
    """返回第一个找得到的 Chromium 系浏览器路径：先按 APP_BROWSERS 在 PATH 里找，再按 APP_DESKTOP_FILES 的 Exec 找；
    都没有时返回 None。"""
    for name in APP_BROWSERS:
        path = shutil.which(name)
        if path:
            return path
    for d in desktop_dirs():
        for name in APP_DESKTOP_FILES:
            exe = desktop_exec(os.path.join(d, name))
            if exe:
                log.info('browser from %s', os.path.join(d, name))
                return exe
    return None


def open_page(url, app=True):
    """打开页面：优先用 Chromium 系浏览器的应用窗口模式（不显示地址栏），找不到或启动失败时用默认浏览器。"""
    exe = find_app_browser() if app else None
    if exe:
        try:
            subprocess.Popen([exe, '--app=' + url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            log.info('opened app window with %s', exe)
            return True
        except OSError as e:
            log.warning('app window failed with %s: %s', exe, e)
    elif app:
        log.info('no Chromium-family browser found, using the default browser')
    return webbrowser.open(url)


def serve(open_browser=True, app=True):
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
    if open_browser and not open_page(url, app):
        tell_user('未能自动打开浏览器，请在浏览器中打开：' + url)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        st.cleanup()
    log.info('server stopped')
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description='找文件问 AI：快速找出文件，交给 AI 提问、改写、总结（双击运行即此程序）。')
    ap.add_argument('--no-browser', action='store_true', help='不自动打开浏览器，只打印网址')
    ap.add_argument('--no-app', action='store_true',
                    help='用默认浏览器的普通窗口打开，不用 Chromium 系浏览器的应用窗口模式')
    ap.add_argument('--log-level', default=os.environ.get('LOG_LEVEL', 'WARNING').upper(),
                    choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
                    help='日志级别，默认 WARNING，可由环境变量 LOG_LEVEL 覆盖')
    args = ap.parse_args(argv)
    logging.basicConfig(level=args.log_level, format=LOG_FORMAT, stream=sys.stderr)
    return serve(open_browser=not args.no_browser, app=not args.no_app)


if __name__ == '__main__':
    sys.exit(main())
