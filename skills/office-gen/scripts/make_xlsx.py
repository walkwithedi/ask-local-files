#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 Excel：把 JSON 描述的表格转成 xlsx，只用 Python 3 标准库。

输入格式：
    {"sheets": [{"name": "明细", "rows": [["部门", "金额", "日期"], ["财务", 100, "2026-01-31"]]}]}
每个工作表的第一行为表头（加粗、底色、边框，冻结并加筛选）；"header": false 时不把第一行当表头。
单元格：数字写成数值；true/false 写成逻辑值；YYYY-MM-DD 写成日期，YYYY-MM-DD HH:MM[:SS] 写成日期时间；
其余一律按文字写入（如 001 保持文字，以 = 开头的也只是文字，不当公式）；null 为空单元格。
超过 15 位的整数按文字写入，避免 Excel 丢失末几位。列宽按内容估算。

用法：
    make_xlsx.py 输入.json [--out 名称.xlsx] [--log-level LEVEL]
输入为相对路径时先在 ASK_WORK（write_work_file 写入的工作目录）里找，再按当前目录找；写 - 从标准输入读。
结果写到 ASK_OUT（未设置时写到当前目录），重名时加序号，不覆盖已有文件。
"""

import argparse
import datetime
import json
import logging
import math
import os
import re
import sys
import unicodedata
import zipfile
from io import BytesIO
from xml.sax.saxutils import escape

LOG_FORMAT = '%(asctime)s [%(levelname)s] %(name)s.%(funcName)s:%(lineno)d - %(message)s'
log = logging.getLogger('make_xlsx')

MAX_INPUT_CHARS = 2000000
MAX_ROWS, MAX_COLS, MAX_CELL_CHARS = 1048576, 16384, 32767
DATE_RE = re.compile(r'^(\d{4})-(\d{2})-(\d{2})$')
DATETIME_RE = re.compile(r'^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?$')
EPOCH = datetime.datetime(1899, 12, 30)
BAD_XML = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f￾￿\ud800-\udfff]')
BAD_SHEET = re.compile(r'[\[\]:*?/\\]')
BAD_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
# 单元格样式编号，对应 styles() 里 cellXfs 的顺序
ST_HEAD, ST_DATE, ST_DATETIME = 1, 2, 3


class Fail(Exception):
    pass


def x(text):
    return escape(BAD_XML.sub('', text), {'"': '&quot;'})


def col_name(n):
    s = ''
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def text_width(s):
    return sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in s)


def as_date(s):
    """YYYY-MM-DD 或带时分的写法转成 (Excel 序列值, 样式, 显示宽度)，不是合法日期时返回 None。"""
    m = DATETIME_RE.match(s) or DATE_RE.match(s)
    if not m:
        return None
    parts = [int(p) for p in m.groups() if p is not None]
    try:
        dt = datetime.datetime(*parts)
    except ValueError:
        return None
    if dt.year < 1900:
        return None
    delta = dt - EPOCH
    serial = delta.days + delta.seconds / 86400.0
    if len(parts) == 3:
        return int(serial), ST_DATE, 11
    return serial, ST_DATETIME, 17


def fmt_number(v):
    if isinstance(v, int):
        return str(v)
    return repr(float(v))


class Book:
    def __init__(self):
        self.strings, self.index = [], {}
        self.notes = []

    def sst(self, s):
        if s not in self.index:
            self.index[s] = len(self.strings)
            self.strings.append(s)
        return self.index[s]

    def cell(self, ref, value, head, where):
        """返回 (单元格 XML, 显示宽度)；空单元格返回 ('', 0)。"""
        style = ST_HEAD if head else 0
        if value is None:
            return ('<c r="%s" s="%d"/>' % (ref, style), 0) if head else ('', 0)
        if isinstance(value, bool):
            return '<c r="%s" s="%d" t="b"><v>%d</v></c>' % (ref, style, int(value)), 5
        if isinstance(value, int) and abs(value) >= 10 ** 15:
            value = str(value)
        if isinstance(value, (int, float)):
            if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
                raise Fail('%s：数值不能是 NaN 或无穷大' % where)
            text = fmt_number(value)
            return '<c r="%s" s="%d"><v>%s</v></c>' % (ref, style, text), len(text)
        if isinstance(value, str):
            d = as_date(value)
            if d and not head:
                serial, st, width = d
                return '<c r="%s" s="%d"><v>%s</v></c>' % (ref, st, fmt_number(serial)), width
            if len(value) > MAX_CELL_CHARS:
                self.notes.append('%s 超过 %d 字，已截断' % (where, MAX_CELL_CHARS))
                value = value[:MAX_CELL_CHARS]
            width = max((text_width(ln) for ln in value.split('\n')), default=0)
            return '<c r="%s" s="%d" t="s"><v>%d</v></c>' % (ref, style, self.sst(value)), width
        raise Fail('%s：不支持的值类型 %s，只能是文字、数字、true/false 或 null' % (where, type(value).__name__))

    def sheet(self, sheet, n):
        rows = sheet.get('rows')
        if not isinstance(rows, list):
            raise Fail('第 %d 个工作表缺少 rows（行的列表）' % n)
        if len(rows) > MAX_ROWS:
            raise Fail('第 %d 个工作表超过 %d 行' % (n, MAX_ROWS))
        header = sheet.get('header', True) is not False
        widths, out, ncols = {}, [], 0
        for r, row in enumerate(rows):
            if not isinstance(row, list):
                raise Fail('第 %d 个工作表第 %d 行不是列表' % (n, r + 1))
            if len(row) > MAX_COLS:
                raise Fail('第 %d 个工作表第 %d 行超过 %d 列' % (n, r + 1, MAX_COLS))
            ncols = max(ncols, len(row))
            head = header and r == 0
            cells = []
            for c, value in enumerate(row):
                where = '第 %d 个工作表第 %d 行第 %d 列' % (n, r + 1, c + 1)
                xml, w = self.cell('%s%d' % (col_name(c), r + 1), value, head, where)
                cells.append(xml)
                widths[c] = max(widths.get(c, 0), w)
            out.append('<row r="%d">%s</row>' % (r + 1, ''.join(cells)))
        view = '<sheetView workbookViewId="0"%s>%s</sheetView>' % (
            ' tabSelected="1"' if n == 1 else '',
            '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
            '<selection pane="bottomLeft" activeCell="A2" sqref="A2"/>' if header and rows else '')
        cols = ''.join('<col min="%d" max="%d" width="%.1f" customWidth="1"/>'
                       % (c + 1, c + 1, min(max(w, 4) + 2, 60)) for c, w in sorted(widths.items()))
        dim = 'A1:%s%d' % (col_name(max(ncols, 1) - 1), max(len(rows), 1))
        filt = '<autoFilter ref="A1:%s%d"/>' % (col_name(ncols - 1), len(rows)) if header and rows and ncols else ''
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<dimension ref="%s"/><sheetViews>%s</sheetViews><sheetFormatPr defaultRowHeight="15"/>%s'
                '<sheetData>%s</sheetData>%s'
                '<pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" header="0.3" footer="0.3"/>'
                '</worksheet>') % (dim, view, '<cols>%s</cols>' % cols if cols else '', ''.join(out), filt)


def sheet_names(sheets, notes):
    names, seen = [], set()
    for n, s in enumerate(sheets, 1):
        raw = s.get('name')
        name = BAD_SHEET.sub('_', str(raw if raw is not None else '')).strip().strip("'")[:31] or 'Sheet%d' % n
        base, k = name, 2
        while name.lower() in seen:
            suffix = '(%d)' % k
            name = base[:31 - len(suffix)] + suffix
            k += 1
        if raw is not None and name != str(raw):
            notes.append('工作表名「%s」改为「%s」（不能含 []:*?/\\、不超过 31 字、不能重名）' % (raw, name))
        seen.add(name.lower())
        names.append(name)
    return names


STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<numFmts count="2"><numFmt numFmtId="164" formatCode="yyyy-mm-dd"/>'
    '<numFmt numFmtId="165" formatCode="yyyy-mm-dd hh:mm"/></numFmts>'
    '<fonts count="2"><font><sz val="11"/><name val="宋体"/><family val="3"/><charset val="134"/></font>'
    '<font><b/><sz val="11"/><name val="宋体"/><family val="3"/><charset val="134"/></font></fonts>'
    '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="FFD9D9D9"/><bgColor indexed="64"/></patternFill></fill>'
    '</fills>'
    '<borders count="2"><border><left/><right/><top/><bottom/><diagonal/></border>'
    '<border><left style="thin"><color rgb="FF808080"/></left><right style="thin"><color rgb="FF808080"/></right>'
    '<top style="thin"><color rgb="FF808080"/></top><bottom style="thin"><color rgb="FF808080"/></bottom>'
    '<diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="4">'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1">'
    '<alignment vertical="center"/></xf>'
    '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '<xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '</cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    '</styleSheet>')


def build_xlsx(data):
    """把 {"sheets": [...]} 转成 xlsx，返回 (字节, 提示列表, 统计)。格式不对时抛 Fail。"""
    if isinstance(data, dict) and 'sheets' not in data and 'rows' in data:
        data = {'sheets': [data]}
    if not isinstance(data, dict) or not isinstance(data.get('sheets'), list) or not data['sheets']:
        raise Fail('JSON 须为 {"sheets": [{"name": "工作表名", "rows": [[...], ...]}]}，且至少一个工作表')
    sheets = data['sheets']
    for n, s in enumerate(sheets, 1):
        if not isinstance(s, dict):
            raise Fail('第 %d 个工作表须为对象 {"name": ..., "rows": [...]}' % n)
    book = Book()
    names = sheet_names(sheets, book.notes)
    parts = [book.sheet(s, n) for n, s in enumerate(sheets, 1)]
    ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    rel = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    workbook = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<workbook xmlns="%s" xmlns:r="%s"><bookViews><workbookView activeTab="0"/></bookViews>'
                '<sheets>%s</sheets></workbook>') % (
        ns, rel, ''.join('<sheet name="%s" sheetId="%d" r:id="rId%d"/>' % (x(nm), i, i)
                         for i, nm in enumerate(names, 1)))
    n = len(parts)
    wb_rels = ''.join('<Relationship Id="rId%d" Type="%s/worksheet" Target="worksheets/sheet%d.xml"/>' % (i, rel, i)
                      for i in range(1, n + 1))
    wb_rels += ('<Relationship Id="rId%d" Type="%s/styles" Target="styles.xml"/>'
                '<Relationship Id="rId%d" Type="%s/sharedStrings" Target="sharedStrings.xml"/>') % (n + 1, rel, n + 2, rel)
    shared = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><sst xmlns="%s" count="%d" uniqueCount="%d">%s'
              '</sst>') % (ns, len(book.strings), len(book.strings),
                           ''.join('<si><t xml:space="preserve">%s</t></si>' % x(s) for s in book.strings))
    types = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
             '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
             '<Default Extension="xml" ContentType="application/xml"/>'
             '<Override PartName="/xl/workbook.xml" '
             'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>%s'
             '<Override PartName="/xl/styles.xml" '
             'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
             '<Override PartName="/xl/sharedStrings.xml" '
             'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
             '<Override PartName="/docProps/core.xml" '
             'ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
             '<Override PartName="/docProps/app.xml" '
             'ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
             '</Types>') % ''.join(
        '<Override PartName="/xl/worksheets/sheet%d.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' % i
        for i in range(1, n + 1))
    root_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                 '<Relationship Id="rId1" Type="%s/officeDocument" Target="xl/workbook.xml"/>'
                 '<Relationship Id="rId2" '
                 'Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" '
                 'Target="docProps/core.xml"/>'
                 '<Relationship Id="rId3" Type="%s/extended-properties" Target="docProps/app.xml"/>'
                 '</Relationships>') % (rel, rel)
    now = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    core = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            '<dcterms:created xsi:type="dcterms:W3CDTF">%s</dcterms:created>'
            '<dcterms:modified xsi:type="dcterms:W3CDTF">%s</dcterms:modified></cp:coreProperties>') % (now, now)
    app = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
           '<Application>make_xlsx.py</Application></Properties>')
    buf = BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', types)
        z.writestr('_rels/.rels', root_rels)
        z.writestr('xl/workbook.xml', workbook)
        z.writestr('xl/_rels/workbook.xml.rels',
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">%s'
                   '</Relationships>' % wb_rels)
        for i, p in enumerate(parts, 1):
            z.writestr('xl/worksheets/sheet%d.xml' % i, p)
        z.writestr('xl/styles.xml', STYLES)
        z.writestr('xl/sharedStrings.xml', shared)
        z.writestr('docProps/core.xml', core)
        z.writestr('docProps/app.xml', app)
    stats = [(nm, len(s['rows'])) for nm, s in zip(names, sheets)]
    return buf.getvalue(), book.notes, stats


# ---------------------------------------------------------------- 命令行

def safe_name(name, ext, fallback):
    base = BAD_NAME.sub('', os.path.basename(name or '').strip()).strip('. ')
    if base.lower().endswith(ext):
        base = base[:-len(ext)]
    base = base[:80].strip() or fallback
    return base + ext


def unique_path(folder, name):
    stem, ext = os.path.splitext(name)
    path, n = os.path.join(folder, name), 2
    while os.path.exists(path):
        path = os.path.join(folder, '%s(%d)%s' % (stem, n, ext))
        n += 1
    return path


def read_input(arg):
    if arg == '-':
        return sys.stdin.read(), '表格'
    path = arg
    work = os.environ.get('ASK_WORK', '')
    if not os.path.isabs(arg) and work and os.path.isfile(os.path.join(work, arg)):
        path = os.path.join(work, arg)
    if not os.path.isfile(path):
        raise Fail('找不到输入文件 %s：请先用 write_work_file 写入工作目录，再把同样的文件名传给本脚本' % arg)
    with open(path, 'rb') as f:
        raw = f.read(MAX_INPUT_CHARS * 4 + 1)
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise Fail('%s 不是 UTF-8 文本' % arg)
    if len(text) > MAX_INPUT_CHARS:
        raise Fail('%s 超过 %d 字' % (arg, MAX_INPUT_CHARS))
    return text, os.path.splitext(os.path.basename(path))[0]


def main(argv=None):
    ap = argparse.ArgumentParser(description='把 JSON 描述的表格转成 Excel（xlsx），写到 ASK_OUT。')
    ap.add_argument('input', help='JSON 文件：相对路径先在 ASK_WORK 里找；- 表示标准输入')
    ap.add_argument('--out', help='生成的文件名，默认与输入同名，扩展名自动补 .xlsx')
    ap.add_argument('--log-level', default=os.environ.get('LOG_LEVEL', 'WARNING'),
                    choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'], help='日志级别（默认取环境变量 LOG_LEVEL）')
    a = ap.parse_args(argv)
    logging.basicConfig(level=a.log_level, format=LOG_FORMAT, stream=sys.stderr)
    try:
        text, stem = read_input(a.input)
        try:
            data = json.loads(text)
        except ValueError as e:
            raise Fail('JSON 格式有误：%s' % e)
        content, notes, stats = build_xlsx(data)
        folder = os.environ.get('ASK_OUT') or os.getcwd()
        path = unique_path(folder, safe_name(a.out or stem, '.xlsx', '表格'))
        with open(path, 'wb') as f:
            f.write(content)
    except (Fail, OSError) as e:
        print('生成失败：%s。' % e)
        return 2
    log.info('wrote %s (%d bytes)', path, len(content))
    print('已生成 %s：%s。' % (os.path.basename(path), '、'.join('「%s」%d 行' % s for s in stats)))
    for note in notes:
        print('提示：%s。' % note)
    return 0


if __name__ == '__main__':
    sys.exit(main())
