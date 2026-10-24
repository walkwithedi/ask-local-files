#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文档对比：取出两份文件的文字后逐行比对，列出新增、删除、修改，可另存左右对照的 HTML 报告。
只用 Python 3 标准库；docx、xlsx、pptx、eml、pdf 借用工具目录（ASK_TOOL_DIR）的 doc2kb_core.py 取文字。

用法：
    compare.py [旧 新] [--context N] [--only 修改|新增|删除] [--html 文件名]
旧、新为已选文件的序号（从 1 起，取自环境变量 ASK_FILES）或路径，默认 1 和 2；--html 写到 ASK_OUT。
"""

import argparse
import difflib
import html
import logging
import os
import re
import sys
import tempfile

LOG_FORMAT = '%(asctime)s [%(levelname)s] %(name)s.%(funcName)s:%(lineno)d - %(message)s'
log = logging.getLogger('doc_compare')
DOC_EXT = {'.docx', '.pptx', '.xlsx', '.xlsm', '.eml', '.pdf'}
MAX_SHOWN = 300     # 最多列出多少处改动


class Fail(Exception):
    pass


def resolve(arg, files):
    if arg.isdigit():
        n = int(arg)
        if not 1 <= n <= len(files):
            raise Fail('序号 %s 超出范围：已选 %d 个文件' % (arg, len(files)))
        return files[n - 1]
    return arg


def read_plain(path):
    with open(path, 'rb') as f:
        raw = f.read()
    if b'\0' in raw[:8192]:
        raise Fail('%s 看起来不是文本文件' % path)
    for enc in ('utf-8-sig', 'gb18030'):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode('utf-8', 'replace')


def extract(path):
    ext = os.path.splitext(path)[1].lower()
    if ext not in DOC_EXT:
        return read_plain(path)
    tool = os.environ.get('ASK_TOOL_DIR', '')
    if tool and tool not in sys.path:
        sys.path.insert(0, tool)
    try:
        import doc2kb_core
    except ImportError:
        raise Fail('找不到 doc2kb_core.py，读不了 %s；请在找文件问 AI 里运行本 skill' % ext)
    doc2kb_core.set_lang('zh')
    report = doc2kb_core.Report()
    with tempfile.TemporaryDirectory(prefix='doc-compare-') as tmp:
        md, _ = doc2kb_core.convert(path, ext, tmp, report, save_images=False)
    if not md.strip():
        raise Fail('%s 没有取出文字（可能是扫描件）' % path)
    return md


def lines_of(text):
    """按行切分，合并行内多余空白，去掉空行。"""
    return [re.sub(r'\s+', ' ', ln).strip() for ln in text.splitlines() if ln.strip()]


def changes(a, b):
    """[(类型, 旧起始行, 旧行列表, 新起始行, 新行列表)]，行号从 1 起。"""
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == 'equal':
            continue
        kind = {'replace': '修改', 'delete': '删除', 'insert': '新增'}[tag]
        out.append((kind, i1 + 1, a[i1:i2], j1 + 1, b[j1:j2]))
    return out


def inline_diff(old, new):
    """一行内的改动，用【-旧-】【+新+】标出。"""
    parts = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag == 'equal':
            parts.append(old[i1:i2])
        else:
            if i2 > i1:
                parts.append('【-%s-】' % old[i1:i2])
            if j2 > j1:
                parts.append('【+%s+】' % new[j1:j2])
    return ''.join(parts)


def report(old_path, new_path, a, b, diffs, context, only):
    counts = {k: sum(1 for d in diffs if d[0] == k) for k in ('修改', '新增', '删除')}
    out = ['旧：%s（%d 行）' % (old_path, len(a)), '新：%s（%d 行）' % (new_path, len(b))]
    if not diffs:
        out.append('两份文件取出的文字完全相同（忽略空行和多余空白）。')
        return '\n'.join(out)
    ratio = difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()
    out.append('共 %d 处改动：修改 %d、新增 %d、删除 %d；按行计相似度 %.0f%%' % (
        len(diffs), counts['修改'], counts['新增'], counts['删除'], ratio * 100))
    shown = [d for d in diffs if not only or d[0] == only]
    for kind, i, old, j, new in shown[:MAX_SHOWN]:
        out.append('')
        out.append('[%s] 旧第 %d 行 → 新第 %d 行' % (kind, i, j))
        if context and i > 1:
            out.extend('    ' + x for x in a[max(0, i - 1 - context):i - 1])
        if kind == '修改' and len(old) == len(new) == 1:
            out.append('  ~ ' + inline_diff(old[0], new[0]))
        else:
            out.extend('  - ' + x for x in old)
            out.extend('  + ' + x for x in new)
    if len(shown) > MAX_SHOWN:
        out.append('')
        out.append('……还有 %d 处改动未列出，可加 --only 分类查看' % (len(shown) - MAX_SHOWN))
    return '\n'.join(out)


def write_html(name, old_path, new_path, a, b):
    out_dir = os.environ.get('ASK_OUT')
    if not out_dir:
        raise Fail('没有输出目录（ASK_OUT），不能生成报告')
    name = os.path.basename(name) or '对比报告.html'
    if not name.lower().endswith('.html'):
        name += '.html'
    table = difflib.HtmlDiff(wrapcolumn=60).make_table(a, b, html.escape(os.path.basename(old_path)),
                                                       html.escape(os.path.basename(new_path)), context=True)
    page = ('<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><title>文档对比</title><style>'
            'body{font:14px/1.5 "Noto Sans CJK SC","WenQuanYi Micro Hei","Microsoft YaHei",sans-serif;margin:16px}'
            'table.diff{border-collapse:collapse;width:100%}td{vertical-align:top;padding:1px 4px}'
            '.diff_add{background:#d4f8d4}.diff_chg{background:#fff3b0}.diff_sub{background:#ffd7d5}'
            '.diff_header{color:#888}</style></head><body><h1>文档对比</h1>'
            '<p>旧：' + html.escape(old_path) + '<br>新：' + html.escape(new_path) + '</p>' + table + '</body></html>')
    with open(os.path.join(out_dir, name), 'w', encoding='utf-8') as f:
        f.write(page)
    return '已生成对比报告 %s' % name


def main(argv=None):
    ap = argparse.ArgumentParser(description='文档对比：逐行列出两份文件的新增、删除、修改（文件取自 ASK_FILES）。')
    ap.add_argument('files', nargs='*', help='旧、新文件的序号或路径，默认 1 2')
    ap.add_argument('--context', type=int, default=1, help='每处改动前显示几行原文，默认 1')
    ap.add_argument('--only', choices=['修改', '新增', '删除'], help='只列出一类改动')
    ap.add_argument('--html', help='另存左右对照的 HTML 报告的文件名')
    ap.add_argument('--log-level', default=os.environ.get('LOG_LEVEL', 'WARNING').upper(),
                    choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'], help='日志级别，默认 WARNING，可由环境变量 LOG_LEVEL 覆盖')
    args = ap.parse_args(argv)
    logging.basicConfig(level=args.log_level, format=LOG_FORMAT, stream=sys.stderr)
    files = [p for p in os.environ.get('ASK_FILES', '').split('\n') if p.strip()]
    try:
        pair = args.files or ['1', '2']
        if len(pair) != 2:
            raise Fail('要给两个文件：旧的在前，新的在后')
        old_path, new_path = (resolve(x, files) for x in pair)
        a, b = lines_of(extract(old_path)), lines_of(extract(new_path))
        diffs = changes(a, b)
        print(report(old_path, new_path, a, b, diffs, max(0, args.context), args.only))
        if args.html:
            print(write_html(args.html, old_path, new_path, a, b))
    except Fail as e:
        print('出错：%s' % e)
        return 2
    except OSError as e:
        print('出错：读写文件失败（%s）' % e)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
