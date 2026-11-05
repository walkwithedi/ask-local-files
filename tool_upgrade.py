#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tool_upgrade  --  工具目录原地升级与退回上一版（升级模块母版）

只用 Python 3 标准库。各工具目录放一份原样副本，由工具的命令行（--upgrade、--rollback）、
本机网页服务或桌面对话框调用；副本不修改，要改先改母版并升 MODULE_VERSION。

升级在原位置替换工具目录的内容，路径不变，桌面链接、菜单项与目录里保留的设置不受影响：
  1. 解压 zip（或复制解压好的目录）到工具目录旁的临时目录「.<目录名>.升级中」；
  2. 按保留清单 upgrade-keep.txt（一行一个相对路径，可为空，# 开头为注释）把文件从旧目录复制进来；
     写成「move: 相对路径」的条目改为搬过来（同一文件系统内改名，不复制，旧目录里随之没有），用于很大的
     数据目录；退回上一版时同样把它搬回换上的版本。
  3. 保留可执行权限（去掉 setuid 等特殊位），根目录的 .desktop 设为可执行；
  4. 旧目录改名为「.<目录名>.上一版」（原有的上一版在成功后删除，只留一份），新目录改名为原目录名。
任一步失败都恢复原样并删除临时目录。退回上一版把「上一版」与当前目录对调。

检查（inspect）拒绝：顶层目录名与工具目录名不一致；读不出版本；缺主程序或双击入口；条目带 ..、
绝对路径、符号链接、设备文件；超过 MAX_ENTRIES 个条目或解压后超过 MAX_TOTAL 字节；工具目录的
上一级文件夹不可写。新版本号低于或等于当前版本时标记为降级或重装，由调用方先提醒再确认。

版本格式（两种都支持）：
  - 目录里有 VERSION（X.Y.Z）：版本取 VERSION，更新说明取 CHANGELOG.md 的「## X.Y.Z」各节；
  - 否则读 README.md「## 版本说明」中的「- vX.Y（YYYY-MM-DD）：……」条目，取最高的一条。

命令行（作用于本文件所在目录，或 --tool-dir 指定的目录）：
  python3 tool_upgrade.py --upgrade <zip或目录> [--yes] [--main 主程序] [--keep 相对路径]
  python3 tool_upgrade.py --rollback [--yes]
  python3 tool_upgrade.py --inspect <zip或目录> [--json]
退出码：0 成功；1 被拒绝、失败或未确认；2 参数错误。
"""

import argparse
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile

MODULE_VERSION = '1.2'
MOVE_PREFIX = 'move:'
LOG_FORMAT = '%(asctime)s [%(levelname)s] %(name)s.%(funcName)s:%(lineno)d - %(message)s'
log = logging.getLogger('tool_upgrade')

KEEP_FILE = 'upgrade-keep.txt'
STAGING = '.%s.升级中'
PREVIOUS = '.%s.上一版'
PREVIOUS_OLD = '.%s.上一版.待删除'
SWAPPING = '.%s.退回中'
MAX_ENTRIES = 5000
MAX_TOTAL = 200 * 1024 * 1024        # 解压后合计最多多少字节
MAX_UPLOAD = 100 * 1024 * 1024       # 网页上传的 zip 最多多少字节
MAX_NOTE_CHARS = 4000
MAX_LISTED = 200                     # 保留与多出来的文件最多列出多少个
SKIP_EXTRA = {'__pycache__'}
RESUME_ENV = 'TOOL_UPGRADE_RESUME'
CHUNK = 1024 * 1024

README_ENTRY = re.compile(r'^- v(\d+(?:\.\d+)*)（(\d{4}-\d{2}-\d{2})）[：:]?\s*(.*)$')
CHANGELOG_HEAD = re.compile(r'^##\s+v?(\d+(?:\.\d+)*)\s*(?:\(.*\)|（.*）|-.*)?\s*$')
VERSION_TEXT = re.compile(r'^v?(\d+(?:\.\d+)*)$')

TEXT = {
    'zh': {
        'src_missing': '找不到所选的文件或目录：{path}',
        'src_kind': '所选的既不是 zip 文件也不是目录：{path}',
        'bad_zip': '不是有效的 zip 文件（{why}）',
        'tool_mismatch': '这不是本工具的发行包：顶层目录是「{got}」，本工具目录是「{want}」',
        'multi_top': '发行包顶层应只有一个目录「{want}」，实际有：{got}',
        'unsafe_path': '发行包里有不安全的路径「{name}」（含 ..、绝对路径或非法字符）',
        'symlink': '发行包里有符号链接「{name}」，为防止指向工具目录以外，不接受符号链接',
        'special': '发行包里有设备文件或其他特殊文件「{name}」',
        'encrypted': '发行包里有加密的条目「{name}」',
        'duplicate': '发行包里有重复的条目「{name}」',
        'too_many': '发行包的条目超过 {limit} 个（实际 {n} 个）',
        'too_big': '发行包解压后超过 {limit} MB',
        'no_version_new': '读不出新版的版本号（{why}）',
        'no_version_cur': '读不出当前版本号（{why}）',
        'no_version_file': '没有 VERSION，README.md 里也没有「## 版本说明」的「- vX.Y（日期）」条目',
        'bad_version_file': 'VERSION 的内容「{text}」不是 X.Y.Z 格式',
        'missing_main': '新版缺少主程序 {name}',
        'missing_launcher': '新版缺少双击入口（根目录的 .desktop 文件）',
        'parent_ro': '工具目录的上一级文件夹 {path} 不可写，无法原地替换',
        'same_dir': '所选的是当前工具目录或它的上一版，请选新版的 zip 或解压好的目录',
        'keep_bad': '保留清单 upgrade-keep.txt 里的路径「{name}」不安全（含 .. 或绝对路径）',
        'no_previous': '没有可退回的版本',
        'not_dir': '工具目录不存在：{path}',
        'upload_big': '上传的文件超过 {limit} MB',
        'upload_short': '上传没有完成，请重试',
        'unknown_check': '检查结果已过期，请重新选择新版',
        'busy': '已有升级或退回正在进行，请等它结束',
        'kind_upgrade': '升级',
        'kind_reinstall': '重装（版本相同）',
        'kind_downgrade': '降级（新选的版本比当前低）',
        'warn_reinstall': '所选版本与当前版本相同，继续会用它重新安装一遍。',
        'warn_downgrade': '所选版本 {new} 低于当前版本 {cur}，继续会降级；降级后可用「退回上一版」回到 {cur}。',
        'confirm': '升级到 {new}',
        'stage_check': '正在检查……',
        'stage_wait': '正在等待进行中的任务结束……',
        'stage_unpack': '正在解压新版……',
        'stage_keep': '正在复制保留的文件……',
        'stage_swap': '正在替换工具目录……',
        'stage_rollback': '正在退回上一版……',
        'done_upgrade': '已升级到 {new}，上一版留在 {prev}。',
        'done_rollback': '已退回 {new}，换下来的 {old} 留作上一版。',
        'restarting': '正在用新版重新启动……',
        'reopen': '请重新打开本工具。',
        'fail_upgrade': '升级失败：{why}。工具目录已恢复原样，请处理后重试',
        'fail_rollback': '退回失败：{why}。工具目录已恢复原样，请处理后重试',
        'refused': '不能升级：{why}。',
        'cli_head': '升级检查：{tool}',
        'cli_versions': '当前版本 {cur} → 新版本 {new}（{kind}）',
        'cli_notes': '两版之间的更新说明：',
        'cli_keep': '会保留的文件：',
        'cli_extras': '多出来的文件（不在保留清单、也不属于新版，不删除，随旧目录留在「上一版」里）：',
        'cli_none': '（无）',
        'cli_more': '……另有 {n} 个',
        'cli_ask': '继续{action}？(y/N) ',
        'cli_cancel': '未确认，没有改动。可加 --yes 跳过确认。',
        'cli_rb_head': '退回上一版：会把当前 {cur} 换成上一版 {prev}，当前版本留作上一版，可再次退回。',
        'cli_rb_action': '退回到 {prev}',
    },
    'en': {
        'src_missing': 'the chosen file or folder does not exist: {path}',
        'src_kind': 'the chosen item is neither a zip file nor a folder: {path}',
        'bad_zip': 'not a valid zip file ({why})',
        'tool_mismatch': 'this is not a release of this tool: its top folder is "{got}", this tool\'s folder is "{want}"',
        'multi_top': 'a release has one top folder "{want}"; this one has: {got}',
        'unsafe_path': 'the release holds an unsafe path "{name}" (.., an absolute path or a bad character)',
        'symlink': 'the release holds a symbolic link "{name}"; links are refused so nothing can point outside the tool folder',
        'special': 'the release holds a device or other special file "{name}"',
        'encrypted': 'the release holds an encrypted entry "{name}"',
        'duplicate': 'the release holds the entry "{name}" twice',
        'too_many': 'the release has more than {limit} entries ({n})',
        'too_big': 'the release unpacks to more than {limit} MB',
        'no_version_new': 'cannot read the new version ({why})',
        'no_version_cur': 'cannot read the current version ({why})',
        'no_version_file': 'there is no VERSION file and no "- vX.Y (date)" entry under "## 版本说明" in README.md',
        'bad_version_file': 'VERSION holds "{text}", not X.Y.Z',
        'missing_main': 'the new version lacks the main program {name}',
        'missing_launcher': 'the new version lacks the launcher (a .desktop file at the top)',
        'parent_ro': 'the folder above the tool folder, {path}, is not writable, so the tool cannot be replaced in place',
        'same_dir': 'the chosen folder is the tool folder itself or its previous version; choose the new release zip or its unpacked folder',
        'keep_bad': 'the keep list upgrade-keep.txt holds an unsafe path "{name}" (.. or an absolute path)',
        'no_previous': 'there is no previous version to roll back to',
        'not_dir': 'the tool folder does not exist: {path}',
        'upload_big': 'the uploaded file is larger than {limit} MB',
        'upload_short': 'the upload did not finish; try again',
        'unknown_check': 'the check has expired; choose the new version again',
        'busy': 'an upgrade or roll-back is already running; wait for it to finish',
        'kind_upgrade': 'upgrade',
        'kind_reinstall': 'reinstall (same version)',
        'kind_downgrade': 'downgrade (the chosen version is older)',
        'warn_reinstall': 'The chosen version is the current one; going on installs it again.',
        'warn_downgrade': 'The chosen version {new} is older than the current {cur}; going on downgrades. Roll back returns to {cur} afterwards.',
        'confirm': 'Upgrade to {new}',
        'stage_check': 'Checking…',
        'stage_wait': 'Waiting for running tasks to finish…',
        'stage_unpack': 'Unpacking the new version…',
        'stage_keep': 'Copying the kept files…',
        'stage_swap': 'Replacing the tool folder…',
        'stage_rollback': 'Rolling back…',
        'done_upgrade': 'Upgraded to {new}; the previous version is kept in {prev}.',
        'done_rollback': 'Rolled back to {new}; {old} is kept as the previous version.',
        'restarting': 'Restarting on the new version…',
        'reopen': 'Open the tool again.',
        'fail_upgrade': 'Upgrade failed: {why}. The tool folder is as it was; fix this and try again',
        'fail_rollback': 'Roll-back failed: {why}. The tool folder is as it was; fix this and try again',
        'refused': 'Cannot upgrade: {why}.',
        'cli_head': 'Upgrade check: {tool}',
        'cli_versions': 'Current version {cur} → new version {new} ({kind})',
        'cli_notes': 'Changes between the two versions:',
        'cli_keep': 'Files that will be kept:',
        'cli_extras': 'Extra files (neither on the keep list nor in the new version; not deleted, they stay with the previous version):',
        'cli_none': '(none)',
        'cli_more': '… and {n} more',
        'cli_ask': 'Go on and {action}? (y/N) ',
        'cli_cancel': 'Not confirmed; nothing changed. --yes skips the question.',
        'cli_rb_head': 'Roll back: the current {cur} is replaced by the previous {prev}; the current version becomes the previous one, so this can be undone.',
        'cli_rb_action': 'roll back to {prev}',
    },
}


def detect_lang(env=None):
    env = os.environ if env is None else env
    for k in ('LC_ALL', 'LC_MESSAGES', 'LANG'):
        v = env.get(k)
        if v:
            return 'zh' if v.lower().startswith('zh') else 'en'
    return 'en'


def text(lang, key, **kw):
    table = TEXT.get(lang) or TEXT['en']
    return table[key].format(**kw)


def joined(lang, *parts):
    """把几句话连起来：中文不加空格，英文以空格分隔。"""
    return ('' if lang == 'zh' else ' ').join(p for p in parts if p)


def sentence(msg, lang):
    """句末没有标点时补上句号。"""
    msg = msg.rstrip()
    if not msg or msg[-1] in '。.！!？?':
        return msg
    return msg + ('。' if lang == 'zh' else '.')


class UpgradeError(Exception):
    """带消息键的错误，message(lang) 给出对应语言的说明。"""

    def __init__(self, key, **kw):
        super().__init__(key)
        self.key = key
        self.kw = kw

    def message(self, lang):
        return text(lang, self.key, **self.kw)


# ---------------------------------------------------------------- 版本

def vtuple(s):
    return tuple(int(p) for p in s.split('.'))


def vcmp(a, b):
    n = max(len(a), len(b))
    a = a + (0,) * (n - len(a))
    b = b + (0,) * (n - len(b))
    return (a > b) - (a < b)


def plain(note):
    """更新说明给界面显示：去掉 Markdown 的行内代码标记，限制长度。"""
    return note.replace('`', '').strip()[:MAX_NOTE_CHARS]


def read_version(get):
    """get(相对路径) 返回文本或 None。返回 {'version': 元组, 'label': 'vX.Y', 'format', 'notes': [(元组, 'vX.Y', 说明)]}。"""
    vfile = get('VERSION')
    if vfile is not None:
        raw = vfile.strip()
        m = VERSION_TEXT.match(raw)
        if not m:
            raise UpgradeError('bad_version_file', text=raw[:40])
        notes = []
        cur, body = None, []
        for line in (get('CHANGELOG.md') or '').splitlines():
            if line.startswith('## '):
                if cur:
                    notes.append((cur, '\n'.join(body).strip()))
                h = CHANGELOG_HEAD.match(line)
                cur, body = (h.group(1) if h else None), []
            elif cur:
                body.append(line)
        if cur:
            notes.append((cur, '\n'.join(body).strip()))
        return {'version': vtuple(m.group(1)), 'label': 'v' + m.group(1), 'format': 'VERSION',
                'notes': [(vtuple(v), 'v' + v, plain(t)) for v, t in notes]}
    readme = get('README.md')
    notes, section = [], None
    for line in (readme or '').splitlines():
        if line.startswith('## '):
            section = line[3:].strip()
            continue
        if section != '版本说明':
            continue
        m = README_ENTRY.match(line)
        if m:
            notes.append((vtuple(m.group(1)), 'v' + m.group(1), plain(m.group(3))))
    if not notes:
        raise UpgradeError('no_version_file')
    best = max(notes, key=lambda n: n[0] + (0,) * (6 - len(n[0])))
    return {'version': best[0], 'label': best[1], 'format': 'README', 'notes': notes}


def notes_between(new, cur):
    """新版说明里版本高于当前、不高于新版的各条，新的在前。"""
    out = [n for n in new['notes'] if vcmp(n[0], cur['version']) > 0 and vcmp(n[0], new['version']) <= 0]
    out.sort(key=lambda n: n[0] + (0,) * (6 - len(n[0])), reverse=True)
    return [{'version': n[1], 'text': n[2]} for n in out]


def dir_get(root):
    def get(rel):
        p = os.path.join(root, rel)
        if os.path.islink(p) or not os.path.isfile(p):
            return None
        try:
            with open(p, encoding='utf-8', errors='replace') as f:
                return f.read(4 * 1024 * 1024)
        except OSError:
            return None
    return get


def current_version(tool_dir):
    return read_version(dir_get(tool_dir))


# ---------------------------------------------------------------- 发行包

def safe_parts(name):
    """把条目名拆成路径段；不安全时返回 None。"""
    if not name or '\\' in name or '\x00' in name or name.startswith('/') or re.match(r'^[A-Za-z]:', name):
        return None
    parts = [p for p in name.split('/') if p != '']
    if not parts or any(p in ('.', '..') for p in parts):
        return None
    return parts


def process_umask():
    """当前进程的 umask。读 /proc/self/status，不调用 os.umask()：后者要先改再改回，多线程的网页服务里
    会临时影响其他线程新建的文件。读不到时按 022。"""
    try:
        with open('/proc/self/status', encoding='ascii', errors='replace') as f:
            for line in f:
                if line.startswith('Umask:'):
                    return int(line.split()[1], 8) & 0o777
    except (OSError, ValueError, IndexError):
        log.debug('umask unreadable, using 022')     # 没有 /proc 或格式不同：按常见默认值处理
    return 0o022


def clean_mode(mode, head=b''):
    """解压后的权限：只看属主是否可执行，可执行的给 755、其余给 644，再按 umask 去掉相应的位
    （与 unzip、cp 相同），不保留 setuid、setgid 与粘滞位。"""
    if mode & 0o777:
        base = 0o755 if mode & 0o100 else 0o644
    else:
        base = 0o755 if head.startswith(b'#!') else 0o644
    return base & ~process_umask()


class ZipSource:
    kind = 'zip'

    def __init__(self, path):
        self.path = path
        try:
            self.zf = zipfile.ZipFile(path)
        except (zipfile.BadZipFile, OSError, ValueError) as e:
            raise UpgradeError('bad_zip', why=str(e) or e.__class__.__name__)
        infos = self.zf.infolist()
        if len(infos) > MAX_ENTRIES:
            raise UpgradeError('too_many', limit=MAX_ENTRIES, n=len(infos))
        self.files, self.dirs, self.tops = {}, set(), set()
        total = 0
        for zi in infos:
            parts = safe_parts(zi.filename)
            if parts is None:
                raise UpgradeError('unsafe_path', name=zi.filename)
            mode = (zi.external_attr >> 16) & 0xFFFF
            ftype = stat.S_IFMT(mode) if zi.create_system == 3 else 0
            if ftype and stat.S_ISLNK(mode):
                raise UpgradeError('symlink', name=zi.filename)
            if ftype and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise UpgradeError('special', name=zi.filename)
            if zi.flag_bits & 0x1:
                raise UpgradeError('encrypted', name=zi.filename)
            is_dir = zi.is_dir() or (ftype and stat.S_ISDIR(mode))
            self.tops.add(parts[0])
            rel = '/'.join(parts[1:])
            if not rel:
                if not is_dir:
                    self.tops.add(parts[0] + ' (file)')
                continue
            if is_dir:
                self.dirs.add(rel)
                continue
            if rel in self.files:
                raise UpgradeError('duplicate', name=zi.filename)
            total += zi.file_size
            if total > MAX_TOTAL:
                raise UpgradeError('too_big', limit=MAX_TOTAL // (1024 * 1024))
            self.files[rel] = (zi, mode & 0o7777 if zi.create_system == 3 else 0)

    def top(self):
        return self.tops

    def read(self, rel):
        item = self.files.get(rel)
        if item is None:
            return None
        if item[0].file_size > 4 * 1024 * 1024:
            return None
        try:
            return self.zf.read(item[0]).decode('utf-8', 'replace')
        except (zipfile.BadZipFile, OSError, RuntimeError, ValueError):
            return None

    def extract(self, dest):
        written = 0
        for rel in sorted(self.dirs):
            os.makedirs(os.path.join(dest, *rel.split('/')), mode=0o755, exist_ok=True)
        for rel, (zi, mode) in sorted(self.files.items()):
            target = os.path.join(dest, *rel.split('/'))
            if not inside(dest, target):
                raise UpgradeError('unsafe_path', name=zi.filename)
            os.makedirs(os.path.dirname(target), mode=0o755, exist_ok=True)
            head = b''
            with self.zf.open(zi) as src, open(target, 'xb') as out:
                while True:
                    chunk = src.read(CHUNK)
                    if not chunk:
                        break
                    if not head:
                        head = chunk[:2]
                    written += len(chunk)
                    if written > MAX_TOTAL:
                        raise UpgradeError('too_big', limit=MAX_TOTAL // (1024 * 1024))
                    out.write(chunk)
            os.chmod(target, clean_mode(mode, head))

    def close(self):
        self.zf.close()


class DirSource:
    kind = 'dir'

    def __init__(self, path):
        self.path = os.path.abspath(path)
        self.files, self.dirs = {}, set()
        total = count = 0
        for root, dirnames, names in os.walk(self.path, followlinks=False):
            for n in list(dirnames) + names:
                p = os.path.join(root, n)
                rel = os.path.relpath(p, self.path).replace(os.sep, '/')
                count += 1
                if count > MAX_ENTRIES:
                    raise UpgradeError('too_many', limit=MAX_ENTRIES, n='%d+' % MAX_ENTRIES)
                try:
                    st = os.lstat(p)
                except OSError as e:
                    raise UpgradeError('bad_zip', why=str(e))
                if stat.S_ISLNK(st.st_mode):
                    raise UpgradeError('symlink', name=rel)
                if stat.S_ISDIR(st.st_mode):
                    self.dirs.add(rel)
                elif stat.S_ISREG(st.st_mode):
                    total += st.st_size
                    if total > MAX_TOTAL:
                        raise UpgradeError('too_big', limit=MAX_TOTAL // (1024 * 1024))
                    self.files[rel] = (p, stat.S_IMODE(st.st_mode))
                else:
                    raise UpgradeError('special', name=rel)

    def top(self):
        return {os.path.basename(self.path)}

    def read(self, rel):
        item = self.files.get(rel)
        return dir_get(self.path)(rel) if item else None

    def extract(self, dest):
        written = 0
        for rel in sorted(self.dirs):
            os.makedirs(os.path.join(dest, *rel.split('/')), mode=0o755, exist_ok=True)
        for rel, (src, mode) in sorted(self.files.items()):
            target = os.path.join(dest, *rel.split('/'))
            os.makedirs(os.path.dirname(target), mode=0o755, exist_ok=True)
            # 检查之后文件可能被换成符号链接或变大：不跟随链接打开，按实际读出的字节再算一遍
            try:
                fd = os.open(src, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            except OSError as e:
                if os.path.islink(src):
                    raise UpgradeError('symlink', name=rel)
                raise e
            with os.fdopen(fd, 'rb') as fi, open(target, 'xb') as fo:
                if not stat.S_ISREG(os.fstat(fi.fileno()).st_mode):
                    raise UpgradeError('special', name=rel)
                while True:
                    chunk = fi.read(CHUNK)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_TOTAL:
                        raise UpgradeError('too_big', limit=MAX_TOTAL // (1024 * 1024))
                    fo.write(chunk)
            os.chmod(target, clean_mode(mode))

    def close(self):
        pass


def inside(base, path):
    base = os.path.realpath(base)
    path = os.path.realpath(path)
    return path == base or path.startswith(base.rstrip(os.sep) + os.sep)


def open_source(source, tool_dir):
    if not os.path.lexists(source):
        raise UpgradeError('src_missing', path=source)
    if os.path.isdir(source):
        real = os.path.realpath(source)
        name = os.path.basename(os.path.realpath(tool_dir))
        parent = os.path.dirname(os.path.realpath(tool_dir))
        for p in (tool_dir, os.path.join(parent, PREVIOUS % name), os.path.join(parent, STAGING % name)):
            if real == os.path.realpath(p):
                raise UpgradeError('same_dir')
        return DirSource(source)
    if os.path.isfile(source):
        return ZipSource(source)
    raise UpgradeError('src_kind', path=source)


# ---------------------------------------------------------------- 保留清单与多出来的文件

def read_keep(path):
    out = []
    try:
        with open(path, encoding='utf-8') as f:
            lines = f.read().splitlines()
    except OSError:
        return out
    for line in lines:
        s = line.strip()
        if s and not s.startswith('#'):
            out.append(s)
    return out


def split_move(s):
    """(相对路径, 是否搬走)：「move: 路径」为搬走的条目。"""
    s = s.strip()
    if s.lower().startswith(MOVE_PREFIX):
        return s[len(MOVE_PREFIX):].strip(), True
    return s, False


def moved_keep(items):
    """保留清单里搬走的条目（规范化后的相对路径集合）。"""
    return {norm_keep([s])[0] for s in items if split_move(s)[1]}


def norm_keep(items):
    out = []
    for s in items:
        parts = safe_parts(split_move(s)[0].replace(os.sep, '/'))
        if parts is None:
            raise UpgradeError('keep_bad', name=s)
        rel = '/'.join(parts)
        if rel not in out:
            out.append(rel)
    return out


def under_any(rel, roots):
    return any(rel == r or rel.startswith(r + '/') for r in roots)


def list_tree(root):
    """工具目录里的文件（含符号链接）的相对路径，跳过 __pycache__ 与 .pyc。"""
    out = []
    for d, dirnames, names in os.walk(root, followlinks=False):
        keep_dirs = []
        for n in dirnames:
            p = os.path.join(d, n)
            if n in SKIP_EXTRA:
                continue
            if os.path.islink(p):
                names.append(n)
            else:
                keep_dirs.append(n)
        dirnames[:] = keep_dirs
        for n in names:
            if n.endswith('.pyc'):
                continue
            out.append(os.path.relpath(os.path.join(d, n), root).replace(os.sep, '/'))
    return sorted(out)


def parent_writable(tool_dir):
    parent = os.path.dirname(os.path.realpath(tool_dir))
    return parent, os.access(parent, os.W_OK | os.X_OK)


# ---------------------------------------------------------------- 检查

def _kind(new, cur):
    c = vcmp(new['version'], cur['version'])
    return 'upgrade' if c > 0 else ('reinstall' if c == 0 else 'downgrade')


def inspect(tool_dir, source, main=(), lang=None, extra_keep=()):
    """检查所选的新版。返回可转成 JSON 的字典：ok、errors（说明文字）、tool、current、new、kind、
    kind_text、warning、confirm、notes（[{version, text}]）、keep、extras、extras_total。"""
    lang = lang or detect_lang()
    tool_dir = os.path.realpath(tool_dir)
    name = os.path.basename(tool_dir)
    rep = {'ok': False, 'errors': [], 'tool': name, 'current': '', 'new': '', 'kind': '', 'kind_text': '',
           'warning': '', 'confirm': '', 'notes': [], 'keep': [], 'extras': [], 'extras_total': 0,
           'source': os.path.abspath(source), 'module_version': MODULE_VERSION}
    src = None
    try:
        src, rep = _inspect(tool_dir, source, main, lang, extra_keep, rep)
    except UpgradeError as e:
        rep['errors'].append(e.message(lang))
    finally:
        if src is not None:
            src.close()
    rep.pop('_keep', None)
    rep.pop('_move', None)
    rep['ok'] = not rep['errors']
    return rep


def _inspect(tool_dir, source, main, lang, extra_keep, rep):
    name = rep['tool']
    if not os.path.isdir(tool_dir):
        raise UpgradeError('not_dir', path=tool_dir)
    try:
        cur = current_version(tool_dir)
    except UpgradeError as e:
        raise UpgradeError('no_version_cur', why=e.message(lang))
    rep['current'] = cur['label']
    src = open_source(source, tool_dir)
    try:
        tops = src.top()
        if len(tops) > 1:
            raise UpgradeError('multi_top', want=name, got='、'.join(sorted(tops)))
        if tops != {name}:
            raise UpgradeError('tool_mismatch', got=next(iter(tops)) if tops else '', want=name)
        try:
            new = read_version(src.read)
        except UpgradeError as e:
            raise UpgradeError('no_version_new', why=e.message(lang))
        rep['new'] = new['label']
        kind = _kind(new, cur)
        rep['kind'] = kind
        rep['kind_text'] = text(lang, 'kind_' + kind)
        if kind == 'reinstall':
            rep['warning'] = text(lang, 'warn_reinstall')
        elif kind == 'downgrade':
            rep['warning'] = text(lang, 'warn_downgrade', new=new['label'], cur=cur['label'])
        rep['confirm'] = text(lang, 'confirm', new=new['label'])
        rep['notes'] = notes_between(new, cur)
        for m in main:
            if m not in src.files:
                rep['errors'].append(text(lang, 'missing_main', name=m))
        had_launcher = any(n.endswith('.desktop') for n in os.listdir(tool_dir))
        if had_launcher and not any('/' not in r and r.endswith('.desktop') for r in src.files):
            rep['errors'].append(text(lang, 'missing_launcher'))
        keep = norm_keep(read_keep(os.path.join(tool_dir, KEEP_FILE)) + list(extra_keep))
        new_keep = src.read(KEEP_FILE)
        if new_keep is not None:
            keep = norm_keep(keep + [s.strip() for s in new_keep.splitlines()
                                     if s.strip() and not s.strip().startswith('#')])
        raw = read_keep(os.path.join(tool_dir, KEEP_FILE)) + list(extra_keep)
        if new_keep is not None:
            raw += [s.strip() for s in new_keep.splitlines() if s.strip() and not s.strip().startswith('#')]
        rep['_keep'] = keep
        rep['_move'] = moved_keep(raw)
        tree = list_tree(tool_dir)
        kept = [r for r in tree if under_any(r, keep)]
        rep['keep'] = kept[:MAX_LISTED]
        extras = [r for r in tree if not under_any(r, keep) and r not in src.files]
        rep['extras_total'] = len(extras)
        rep['extras'] = extras[:MAX_LISTED]
        parent, ok = parent_writable(tool_dir)
        if not ok:
            rep['errors'].append(text(lang, 'parent_ro', path=parent))
    except BaseException:
        src.close()
        raise
    return src, rep


# ---------------------------------------------------------------- 应用与退回

def _rmtree(path):
    if os.path.islink(path):
        os.unlink(path)
    elif os.path.isdir(path):
        shutil.rmtree(path)
    elif os.path.lexists(path):
        os.unlink(path)


def _move_keep(old, new, rels, done):
    """把 rels 从 old 改名搬到 new（同一文件系统，不复制）；搬好的记进 done 以便失败时搬回。"""
    for rel in rels:
        src = os.path.join(old, *rel.split('/'))
        if not os.path.lexists(src) or not inside(old, os.path.dirname(src)):
            continue
        dst = os.path.join(new, *rel.split('/'))
        if not inside(new, os.path.dirname(dst)):
            raise UpgradeError('keep_bad', name=rel)
        if os.path.lexists(dst):
            _rmtree(dst)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.rename(src, dst)
        done.append((dst, src))
        log.info('moved %s', rel)


def _move_back(done):
    for dst, src in reversed(done):
        try:
            os.makedirs(os.path.dirname(src), exist_ok=True)
            os.rename(dst, src)
        except OSError as e:
            log.error('cannot move %s back: %s', src, e)
    del done[:]


def _copy_keep(old, new, keep, move=()):
    for rel in keep:
        if rel in move:
            continue
        src = os.path.join(old, *rel.split('/'))
        if not os.path.lexists(src):
            continue
        dst = os.path.join(new, *rel.split('/'))
        if not inside(new, os.path.dirname(dst)):
            raise UpgradeError('keep_bad', name=rel)
        if not inside(old, os.path.dirname(src)):
            # 路径经过工具目录里指向别处的符号链接：不把目录外的文件带进来
            log.warning('keep %s skipped: outside the tool folder', rel)
            continue
        if os.path.lexists(dst):
            _rmtree(dst)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.islink(src):
            os.symlink(os.readlink(src), dst)
        elif os.path.isdir(src):
            shutil.copytree(src, dst, symlinks=True)
        else:
            shutil.copy2(src, dst)
        log.info('kept %s', rel)


def _gio_trust(old_dir, new_dir):
    """旧入口被桌面标记为可信时，给新入口同样的标记（没有 gio 时跳过）。"""
    gio = shutil.which('gio')
    if not gio:
        return
    for n in os.listdir(new_dir):
        if not n.endswith('.desktop'):
            continue
        old = os.path.join(old_dir, n)
        if not os.path.isfile(old):
            continue
        try:
            r = subprocess.run([gio, 'info', '-a', 'metadata::trusted', old], stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, universal_newlines=True, timeout=5)
            if 'metadata::trusted: true' in r.stdout:
                subprocess.run([gio, 'set', os.path.join(new_dir, n), 'metadata::trusted', 'true'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        except (OSError, subprocess.SubprocessError) as e:
            log.debug('gio trust %s: %s', n, e)


def _fail_rename(src, dst):
    os.rename(src, dst)


def apply(tool_dir, source, main=(), lang=None, extra_keep=(), progress=None):
    """检查并应用升级。成功返回检查结果字典（另有 message、previous）；被拒绝或失败抛 UpgradeError
    （键为 refused 或 fail_upgrade，message(lang) 为完整的一句话）。失败时工具目录恢复原样。"""
    lang = lang or detect_lang()
    stage = progress or (lambda key: None)
    tool_dir = os.path.realpath(tool_dir)
    name = os.path.basename(tool_dir)
    parent = os.path.dirname(tool_dir)
    staging = os.path.join(parent, STAGING % name)
    previous = os.path.join(parent, PREVIOUS % name)
    prev_old = os.path.join(parent, PREVIOUS_OLD % name)
    stage('stage_check')
    rep = {'errors': [], 'tool': name, 'current': '', 'new': '', 'module_version': MODULE_VERSION}
    try:
        src, rep = _inspect(tool_dir, source, main, lang, extra_keep, dict(rep, notes=[], keep=[], extras=[]))
    except UpgradeError as e:
        raise UpgradeError('refused', why=e.message(lang))
    if rep['errors']:
        src.close()
        raise UpgradeError('refused', why='；'.join(rep['errors']) if lang == 'zh' else '; '.join(rep['errors']))
    moved_old = moved_prev = False
    moved = []
    try:
        for p in (staging, prev_old):
            if os.path.lexists(p):
                _rmtree(p)
        os.mkdir(staging, 0o755)
        stage('stage_unpack')
        src.extract(staging)
        stage('stage_keep')
        _copy_keep(tool_dir, staging, rep['_keep'], rep['_move'])
        for n in os.listdir(staging):
            p = os.path.join(staging, n)
            if n.endswith('.desktop') and os.path.isfile(p) and not os.path.islink(p):
                os.chmod(p, stat.S_IMODE(os.stat(p).st_mode) | (0o755 & ~process_umask()) | 0o100)
        os.chmod(staging, stat.S_IMODE(os.stat(tool_dir).st_mode) | 0o700)
        stage('stage_swap')
        if os.path.lexists(previous):
            _fail_rename(previous, prev_old)
            moved_prev = True
        # 搬走的条目最后搬：之前任一步失败都不用搬回
        _move_keep(tool_dir, staging, sorted(rep['_move']), moved)
        _fail_rename(tool_dir, previous)
        moved_old = True
        _fail_rename(staging, tool_dir)
    except BaseException as e:
        log.error('upgrade failed: %s', e)
        try:
            if moved_old and not os.path.lexists(tool_dir):
                os.rename(previous, tool_dir)
            _move_back(moved)
            if moved_prev and not os.path.lexists(previous):
                os.rename(prev_old, previous)
        except OSError as e2:
            log.error('restore failed: %s', e2)
        if os.path.lexists(staging):
            shutil.rmtree(staging, ignore_errors=True)
        why = e.message(lang) if isinstance(e, UpgradeError) else (getattr(e, 'strerror', None) or str(e))
        raise UpgradeError('fail_upgrade', why=why)
    finally:
        src.close()
    if os.path.lexists(prev_old):
        shutil.rmtree(prev_old, ignore_errors=True)
    _gio_trust(previous, tool_dir)
    log.info('upgraded %s %s -> %s', name, rep['current'], rep['new'])
    rep.pop('_keep', None)
    rep.pop('_move', None)
    rep['previous'] = previous
    rep['message'] = text(lang, 'done_upgrade', new=rep['new'], prev=previous)
    return rep


def rollback_info(tool_dir):
    """{'available': 有无上一版, 'current': 'vX.Y', 'previous': 'vX.Y' 或 ''}。"""
    tool_dir = os.path.realpath(tool_dir)
    name = os.path.basename(tool_dir)
    previous = os.path.join(os.path.dirname(tool_dir), PREVIOUS % name)
    info = {'available': False, 'current': '', 'previous': '', 'path': previous}
    try:
        info['current'] = current_version(tool_dir)['label']
    except UpgradeError:
        pass        # 当前目录读不出版本时照样能退回，版本号留空
    if os.path.isdir(previous) and not os.path.islink(previous):
        info['available'] = True
        try:
            info['previous'] = current_version(previous)['label']
        except UpgradeError:
            info['previous'] = '?'
    return info


def rollback(tool_dir, lang=None, progress=None):
    """把上一版与当前目录对调。成功返回 rollback_info 的结果（另有 message）；失败抛 UpgradeError。"""
    lang = lang or detect_lang()
    (progress or (lambda key: None))('stage_rollback')
    tool_dir = os.path.realpath(tool_dir)
    name = os.path.basename(tool_dir)
    parent = os.path.dirname(tool_dir)
    previous = os.path.join(parent, PREVIOUS % name)
    swapping = os.path.join(parent, SWAPPING % name)
    info = rollback_info(tool_dir)
    if not info['available']:
        raise UpgradeError('fail_rollback', why=text(lang, 'no_previous'))
    if not parent_writable(tool_dir)[1]:
        raise UpgradeError('fail_rollback', why=text(lang, 'parent_ro', path=parent))
    if os.path.lexists(swapping):
        _rmtree(swapping)
    try:
        move = sorted(moved_keep(read_keep(os.path.join(tool_dir, KEEP_FILE))))
    except UpgradeError as e:
        raise UpgradeError('fail_rollback', why=e.message(lang))
    step = 0
    moved = []
    try:
        _move_keep(tool_dir, previous, move, moved)     # 搬走的数据跟着换上的版本走
        _fail_rename(tool_dir, swapping)
        step = 1
        _fail_rename(previous, tool_dir)
        step = 2
        _fail_rename(swapping, previous)
    except (OSError, UpgradeError) as e:
        log.error('rollback failed at step %d: %s', step, e)
        try:
            if step == 2:
                os.rename(tool_dir, previous)
                step = 1
            if step == 1:
                os.rename(swapping, tool_dir)
        except OSError as e2:
            log.error('restore failed: %s', e2)
        _move_back(moved)
        why = e.message(lang) if isinstance(e, UpgradeError) else (e.strerror or str(e))
        raise UpgradeError('fail_rollback', why=why)
    _gio_trust(previous, tool_dir)
    log.info('rolled back %s %s -> %s', name, info['current'], info['previous'])
    out = rollback_info(tool_dir)
    out['message'] = text(lang, 'done_rollback', new=info['previous'], old=info['current'])
    return out


# ---------------------------------------------------------------- 重新启动

def restart(script, args=(), resume=None):
    """用新版重新执行 script（同一进程号）；resume 为字典时经环境变量交给新进程（如端口与令牌）。"""
    env = dict(os.environ)
    if resume is not None:
        env[RESUME_ENV] = json.dumps(resume)
    logging.shutdown()
    os.chdir(os.path.dirname(os.path.abspath(script)))
    os.execve(sys.executable, [sys.executable, os.path.abspath(script)] + list(args), env)


def resumed():
    """取出并清掉重新启动时交来的字典；没有时返回 None。"""
    raw = os.environ.pop(RESUME_ENV, None)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------- 网页服务用

def save_upload(rfile, length, limit=None):
    """把请求体写进系统临时目录的文件，返回路径；超限或没收全时抛 UpgradeError（不留文件）。"""
    limit = MAX_UPLOAD if limit is None else limit
    if length > limit:
        raise UpgradeError('upload_big', limit=limit // (1024 * 1024))
    fd, path = tempfile.mkstemp(prefix='tool-upgrade-', suffix='.zip')
    try:
        left = length
        with os.fdopen(fd, 'wb') as f:
            while left > 0:
                chunk = rfile.read(min(CHUNK, left))
                if not chunk:
                    raise UpgradeError('upload_short')
                f.write(chunk)
                left -= len(chunk)
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass    # 临时文件已不在：没有要清理的
        raise
    return path


class WebJob:
    """本机网页服务里的升级任务：检查上传的新版、在后台升级或退回，完成后置 restart，由服务关闭并
    用 restart() 以新版重新启动。busy() 为真时（有任务进行中）先等待再动手。"""

    def __init__(self, tool_dir, main=(), lang=None, busy=None, extra_keep=()):
        self.tool_dir = tool_dir
        self.main = tuple(main)
        self.lang = lang or detect_lang()
        self.busy = busy or (lambda: False)
        self.extra_keep = tuple(extra_keep)
        self.lock = threading.Lock()
        self.pending = {}           # 检查编号 -> 临时 zip 路径（目录来源为 None）
        self.state = 'idle'         # idle / waiting / running / done / failed
        self.stage = ''
        self.message = ''
        self.restart = threading.Event()

    @property
    def running(self):
        return self.state in ('waiting', 'running')

    def _drop_pending(self):
        for p in self.pending.values():
            if isinstance(p, str):
                try:
                    os.unlink(p)
                except OSError:
                    pass    # 临时文件已不在：没有要清理的
        self.pending.clear()

    def check_upload(self, rfile, length):
        """检查上传的 zip，返回 (HTTP 状态码, 字典)。检查通过时字典带 id，供 start_upgrade 使用。"""
        with self.lock:
            if self.running:
                return 409, {'error': text(self.lang, 'busy')}
        try:
            path = save_upload(rfile, length)
        except UpgradeError as e:
            return (413 if e.key == 'upload_big' else 400), {'error': sentence(e.message(self.lang), self.lang)}
        rep = inspect(self.tool_dir, path, self.main, self.lang, self.extra_keep)
        rep.pop('source', None)
        rep.pop('_keep', None)
        with self.lock:
            self._drop_pending()
            if rep['ok']:
                rep['id'] = uuid.uuid4().hex
                self.pending[rep['id']] = path
            else:
                os.unlink(path)
        return 200, rep

    def check_path(self, path):
        """检查本机路径（目录或 zip），返回 (状态码, 字典)。"""
        rep = inspect(self.tool_dir, path, self.main, self.lang, self.extra_keep)
        rep.pop('_keep', None)
        with self.lock:
            self._drop_pending()
            if rep['ok']:
                rep['id'] = uuid.uuid4().hex
                self.pending[rep['id']] = ('path', path)
        return 200, rep

    def start_upgrade(self, check_id):
        with self.lock:
            if self.running:
                return 409, {'error': text(self.lang, 'busy')}
            item = self.pending.pop(str(check_id), None) if check_id else None
            if item is None:
                return 400, {'error': text(self.lang, 'unknown_check')}
            self.state, self.stage, self.message = 'waiting', text(self.lang, 'stage_check'), ''
        threading.Thread(target=self._run, args=('upgrade', item), daemon=True).start()
        return 200, {'ok': True}

    def start_rollback(self):
        with self.lock:
            if self.running:
                return 409, {'error': text(self.lang, 'busy')}
            info = rollback_info(self.tool_dir)
            if not info['available']:
                return 400, {'error': sentence(text(self.lang, 'fail_rollback', why=text(self.lang, 'no_previous')),
                                               self.lang)}
            self._drop_pending()
            self.state, self.stage, self.message = 'waiting', text(self.lang, 'stage_rollback'), ''
        threading.Thread(target=self._run, args=('rollback', None), daemon=True).start()
        return 200, {'ok': True}

    def _set_stage(self, key):
        with self.lock:
            self.stage = text(self.lang, key)

    def _run(self, what, item):
        tmp = item if isinstance(item, str) else None
        source = item[1] if isinstance(item, tuple) else item
        try:
            if self.busy():
                self._set_stage('stage_wait')
                while self.busy():
                    time.sleep(0.5)
            with self.lock:
                self.state = 'running'
            if what == 'upgrade':
                res = apply(self.tool_dir, source, self.main, self.lang, self.extra_keep, self._set_stage)
            else:
                res = rollback(self.tool_dir, self.lang, self._set_stage)
            with self.lock:
                self.state = 'done'
                self.message = joined(self.lang, res['message'], text(self.lang, 'restarting'))
                self.stage = text(self.lang, 'restarting')
            self.restart.set()
        except UpgradeError as e:
            with self.lock:
                self.state, self.stage, self.message = 'failed', '', sentence(e.message(self.lang), self.lang)
        except Exception as e:      # 意外错误也要让页面知道，不让任务卡在进行中
            log.exception('upgrade job crashed')
            with self.lock:
                self.state, self.stage = 'failed', ''
                self.message = sentence(text(self.lang, 'fail_upgrade' if what == 'upgrade' else 'fail_rollback',
                                             why=str(e)), self.lang)
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass    # 临时文件已不在：没有要清理的

    def status(self):
        with self.lock:
            out = {'state': self.state, 'stage': self.stage, 'message': self.message,
                   'module_version': MODULE_VERSION}
        out['rollback'] = rollback_info(self.tool_dir)
        out['rollback'].pop('path', None)
        out['current'] = out['rollback']['current']
        return out

    def cleanup(self):
        with self.lock:
            self._drop_pending()


# ---------------------------------------------------------------- 命令行

def _print_list(out, lang, items, total=None):
    if not items:
        print('  ' + text(lang, 'cli_none'), file=out)
        return
    for r in items:
        print('  ' + r, file=out)
    total = len(items) if total is None else total
    if total > len(items):
        print('  ' + text(lang, 'cli_more', n=total - len(items)), file=out)


def print_report(rep, lang, out=sys.stdout):
    print(text(lang, 'cli_head', tool=rep['tool']), file=out)
    if rep.get('current') and rep.get('new'):
        print(text(lang, 'cli_versions', cur=rep['current'], new=rep['new'], kind=rep['kind_text']), file=out)
    if rep.get('warning'):
        print(rep['warning'], file=out)
    if rep.get('notes'):
        print(text(lang, 'cli_notes'), file=out)
        for n in rep['notes']:
            print('  %s：%s' % (n['version'], n['text']) if lang == 'zh' else '  %s: %s' % (n['version'], n['text']),
                  file=out)
    if rep.get('new'):
        print(text(lang, 'cli_keep'), file=out)
        _print_list(out, lang, rep.get('keep') or [])
        print(text(lang, 'cli_extras'), file=out)
        _print_list(out, lang, rep.get('extras') or [], rep.get('extras_total'))


def _ask(prompt, inp, out):
    try:
        print(prompt, end='', file=out, flush=True)
        ans = inp()
    except (EOFError, KeyboardInterrupt):
        print(file=out)
        return False
    return ans.strip().lower() in ('y', 'yes', '是')


def cli(tool_dir, main=(), upgrade=None, do_rollback=False, yes=False, lang=None, extra_keep=(),
        out=None, err=None, inp=input):
    """工具命令行 --upgrade / --rollback 的实现。返回退出码：0 成功；1 被拒绝、失败或未确认。"""
    lang = lang or detect_lang()
    out = out or sys.stdout
    err = err or sys.stderr
    if do_rollback:
        info = rollback_info(tool_dir)
        if not info['available']:
            print(sentence(text(lang, 'fail_rollback', why=text(lang, 'no_previous')), lang), file=err)
            return 1
        print(text(lang, 'cli_rb_head', cur=info['current'], prev=info['previous']), file=out)
        if not yes and not _ask(text(lang, 'cli_ask', action=text(lang, 'cli_rb_action', prev=info['previous'])),
                                inp, out):
            print(text(lang, 'cli_cancel'), file=err)
            return 1
        try:
            res = rollback(tool_dir, lang)
        except UpgradeError as e:
            print(sentence(e.message(lang), lang), file=err)
            return 1
        print(joined(lang, res['message'], text(lang, 'reopen')), file=out)
        return 0
    rep = inspect(tool_dir, upgrade, main, lang, extra_keep)
    print_report(rep, lang, out)
    if not rep['ok']:
        for e in rep['errors']:
            print(text(lang, 'refused', why=e), file=err)
        return 1
    if not yes and not _ask(text(lang, 'cli_ask', action=rep['confirm']), inp, out):
        print(text(lang, 'cli_cancel'), file=err)
        return 1
    try:
        res = apply(tool_dir, upgrade, main, lang, extra_keep,
                    progress=lambda key: print(text(lang, key), file=err))
    except UpgradeError as e:
        print(sentence(e.message(lang), lang), file=err)
        return 1
    print(joined(lang, res['message'], text(lang, 'reopen')), file=out)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description='工具目录原地升级与退回上一版 / in-place upgrade and roll-back of a tool folder')
    act = ap.add_mutually_exclusive_group(required=True)
    act.add_argument('--upgrade', metavar='SRC', help='新版的 zip 或解压好的目录 / the new release zip or unpacked folder')
    act.add_argument('--rollback', action='store_true', help='退回上一版 / roll back to the previous version')
    act.add_argument('--inspect', metavar='SRC', help='只检查，不升级 / check only')
    act.add_argument('--module-version', action='store_true', help='打印本模块版本 / print this module\'s version')
    ap.add_argument('--tool-dir', default=os.path.dirname(os.path.abspath(__file__)),
                    help='工具目录，默认为本文件所在目录 / the tool folder (default: this file\'s folder)')
    ap.add_argument('--main', action='append', default=[], help='新版必须带的主程序（可多次）/ a main program the new version must have')
    ap.add_argument('--keep', action='append', default=[], help='另外保留的相对路径（可多次）/ another relative path to keep')
    ap.add_argument('--yes', action='store_true', help='不询问，直接执行 / do not ask')
    ap.add_argument('--json', action='store_true', help='--inspect 以 JSON 输出 / JSON output for --inspect')
    ap.add_argument('--lang', choices=['zh', 'en'], help='提示语言，默认按 LANG / message language (default from LANG)')
    ap.add_argument('--log-level', default=os.environ.get('LOG_LEVEL', 'WARNING').upper(),
                    choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
                    help='日志级别，默认 WARNING，可由环境变量 LOG_LEVEL 覆盖 / log level')
    a = ap.parse_args(argv)
    logging.basicConfig(level=a.log_level, format=LOG_FORMAT, stream=sys.stderr)
    lang = a.lang or detect_lang()
    if a.module_version:
        print(MODULE_VERSION)
        return 0
    if a.inspect:
        rep = inspect(a.tool_dir, a.inspect, a.main, lang, a.keep)
        rep.pop('_keep', None)
        if a.json:
            json.dump(rep, sys.stdout, ensure_ascii=False, indent=2)
            print()
        else:
            print_report(rep, lang)
            for e in rep['errors']:
                print(text(lang, 'refused', why=e), file=sys.stderr)
        return 0 if rep['ok'] else 1
    return cli(a.tool_dir, a.main, a.upgrade, a.rollback, a.yes, lang, a.keep)


if __name__ == '__main__':
    sys.exit(main())
