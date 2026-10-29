#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把工作目录里写好的 skill 文件夹打成 .zip 包，只用 Python 3 标准库。

检查：根上有 SKILL.md，开头 --- 之间有 name 与 description；name 只用小写字母、数字和连字符，
不超过 64 个字符；description 不超过 1024 字；正文不超过 5 万字；只含 .md、.txt 文件（不打包脚本），
没有链接，文件不超过 200 个、合计不超过 20 MB。
包里的文件放在以 name 命名的文件夹下，可用「找文件问 AI」的「安装」装进自建 skill 目录。

用法：
    pack_skill.py 文件夹 [--log-level LEVEL]
文件夹为相对路径时先在 ASK_WORK（write_work_file 写入的工作目录）里找，再按当前目录找。
结果写到 ASK_OUT（未设置时写到当前目录），重名时加序号，不覆盖已有文件。
退出码 0 为已打包，2 为检查不通过或写不进。
"""

import argparse
import logging
import os
import re
import sys
import zipfile

LOG_FORMAT = '%(asctime)s [%(levelname)s] %(name)s.%(funcName)s:%(lineno)d - %(message)s'
log = logging.getLogger('pack_skill')

NAME_RE = re.compile(r'^[a-z0-9]+(-[a-z0-9]+)*$')
ALLOWED_EXT = {'.md', '.txt'}
MAX_DESC, MAX_BODY = 1024, 50000
MAX_FILES, MAX_BYTES = 200, 20 * 1024 * 1024


class Fail(Exception):
    pass


def front_matter(text):
    """SKILL.md 开头 --- 之间的 key: value（带引号的值去掉引号，> 与 | 多行值合成一行），返回 (字典, 正文)。"""
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


def find_dir(arg):
    work = os.environ.get('ASK_WORK')
    if not os.path.isabs(arg) and work and os.path.isdir(os.path.join(work, arg)):
        return os.path.join(work, arg)
    if os.path.isdir(arg):
        return arg
    raise Fail('找不到文件夹 %s（先用 write_work_file 把 SKILL.md 写到 %s/SKILL.md）' % (arg, arg))


def collect(src):
    """检查并列出要打包的文件 [(绝对路径, 相对路径)]。"""
    files, total, bad = [], 0, []
    for root, dirs, names in os.walk(src):
        dirs.sort()
        for n in dirs + sorted(names):
            if os.path.islink(os.path.join(root, n)):
                raise Fail('不能打包链接：%s' % os.path.relpath(os.path.join(root, n), src))
        for n in sorted(names):
            path = os.path.join(root, n)
            rel = os.path.relpath(path, src).replace(os.sep, '/')
            if os.path.splitext(n)[1].lower() not in ALLOWED_EXT:
                bad.append(rel)
                continue
            files.append((path, rel))
            total += os.path.getsize(path)
    if bad:
        raise Fail('只能打包说明和参考文本（.md、.txt），不打包脚本或其他文件：%s' % '、'.join(bad[:10]))
    if len(files) > MAX_FILES:
        raise Fail('文件超过 %d 个' % MAX_FILES)
    if total > MAX_BYTES:
        raise Fail('合计超过 %d MB' % (MAX_BYTES // 1024 // 1024))
    return files


def check_skill_md(src):
    path = os.path.join(src, 'SKILL.md')
    if not os.path.isfile(path):
        raise Fail('文件夹根上没有 SKILL.md')
    try:
        with open(path, encoding='utf-8-sig') as f:
            text = f.read()
    except UnicodeDecodeError:
        raise Fail('SKILL.md 不是 UTF-8 文字')
    meta, body = front_matter(text)
    name, desc = meta.get('name', '').strip(), meta.get('description', '').strip()
    if not name or not desc:
        raise Fail('SKILL.md 开头两行 --- 之间缺少 name 或 description')
    if len(name) > 64 or not NAME_RE.match(name):
        raise Fail('name「%s」只能用小写字母、数字和连字符，不超过 64 个字符' % name[:80])
    if len(desc) > MAX_DESC:
        raise Fail('description 超过 %d 字' % MAX_DESC)
    if not body:
        raise Fail('SKILL.md 没有说明正文')
    if len(body) > MAX_BODY:
        raise Fail('说明正文超过 %d 字' % MAX_BODY)
    return name


def unique_path(folder, name):
    stem, ext = os.path.splitext(name)
    path, i = os.path.join(folder, name), 2
    while os.path.exists(path):
        path = os.path.join(folder, '%s-%d%s' % (stem, i, ext))
        i += 1
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(description='把写好的 skill 文件夹打成 .zip 包，写到 ASK_OUT。')
    ap.add_argument('folder', help='skill 文件夹：相对路径先在 ASK_WORK 里找')
    ap.add_argument('--log-level', default=os.environ.get('LOG_LEVEL', 'WARNING'),
                    choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'], help='日志级别（默认取环境变量 LOG_LEVEL）')
    a = ap.parse_args(argv)
    logging.basicConfig(level=a.log_level, format=LOG_FORMAT, stream=sys.stderr)
    try:
        src = find_dir(a.folder)
        name = check_skill_md(src)
        files = collect(src)
        out = unique_path(os.environ.get('ASK_OUT') or os.getcwd(), name + '.zip')
        with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as zf:
            for path, rel in files:
                zf.write(path, name + '/' + rel)
    except (Fail, OSError) as e:
        print('打包失败：%s。' % e)
        return 2
    log.info('wrote %s (%d files)', out, len(files))
    print('已打包 %s：%d 个文件（%s）。请使用者点「安装 %s」装进自建 skill 目录。' % (
        os.path.basename(out), len(files), '、'.join(rel for _, rel in files[:20]), os.path.basename(out)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
