#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 Word：把 Markdown 子集转成 docx，只用 Python 3 标准库。

支持：# 至 ###### 标题、段落（行内换行保留）、**加粗**、*斜体*、`代码`、[文字](地址)、
- / * / + 无序列表与 1. 有序列表（缩进表示层级）、| 表格 |、> 引用、``` 代码块、--- 分隔线。
其余写法按普通文字原样写入。版式：A4，标题用 Word 内置「标题 1」至「标题 6」样式，
正文宋体、标题黑体，表格细边框、表头加粗并在跨页时重复。

用法：
    make_docx.py 输入.md [--out 名称.docx] [--title 文档标题] [--log-level LEVEL]
输入为相对路径时先在 ASK_WORK（write_work_file 写入的工作目录）里找，再按当前目录找；写 - 从标准输入读。
结果写到 ASK_OUT（未设置时写到当前目录），重名时加序号，不覆盖已有文件。
"""

import argparse
import datetime
import logging
import os
import re
import sys
import zipfile
from io import BytesIO
from xml.sax.saxutils import escape

LOG_FORMAT = '%(asctime)s [%(levelname)s] %(name)s.%(funcName)s:%(lineno)d - %(message)s'
log = logging.getLogger('make_docx')

PAGE_W, PAGE_H, MARGIN_X, MARGIN_Y = 11906, 16838, 1800, 1440     # A4，单位 1/20 磅
TEXT_W = PAGE_W - 2 * MARGIN_X
MAX_INPUT_CHARS = 2000000

FENCE = re.compile(r'^ {0,3}(```|~~~)')
HEAD = re.compile(r'^ {0,3}(#{1,6})\s+(.*?)(?:\s+#+)?\s*$')
HR = re.compile(r'^ {0,3}([-*_])(?:\s*\1){2,}\s*$')
ITEM = re.compile(r'^(\s*)([-*+]|(\d{1,9})[.)])\s+(.*)$')
QUOTE = re.compile(r'^ {0,3}> ?')
TABLE_SEP = re.compile(r'^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$')
INLINE = re.compile(r'`([^`\n]+)`|\*\*([^*\n]+?)\*\*|\*([^*\s](?:[^*\n]*[^*\s])?)\*|\[([^\]\n]+)\]\(([^()\s]+)\)')
BAD_XML = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f￾￿\ud800-\udfff]')
BAD_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


class Fail(Exception):
    pass


# ---------------------------------------------------------------- Markdown 解析

def cells(row):
    s = row.strip()
    if s.startswith('|'):
        s = s[1:]
    if s.endswith('|'):
        s = s[:-1]
    return [c.strip() for c in s.split('|')]


def parse(text):
    """返回块列表：('h', 级别, 文字)、('p', [行])、('code', 文字)、('hr',)、('quote', 块列表)、
    ('table', 表头, 对齐, 行)、('li', 层级, 列表编号, 是否有序, 起始号, [行])、('lip', 层级, [行])。"""
    lines = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    blocks, lists, para, blank = [], [], None, False
    counter = [0]

    def block(b):
        nonlocal para, lists
        para, lists = None, []
        blocks.append(b)

    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            para, blank = None, True
            i += 1
            continue
        was_blank, blank = blank, False
        m = FENCE.match(line)
        if m:
            end = re.compile('^ {0,3}' + re.escape(m.group(1)[0]) + '{3,}\\s*$')
            code = []
            i += 1
            while i < len(lines) and not end.match(lines[i]):
                code.append(lines[i])
                i += 1
            block(('code', '\n'.join(code)))
            i += 1
            continue
        m = HEAD.match(line)
        if m:
            block(('h', len(m.group(1)), m.group(2)))
            i += 1
            continue
        if HR.match(line):
            block(('hr',))
            i += 1
            continue
        if QUOTE.match(line):
            inner = [QUOTE.sub('', line, count=1)]
            while i + 1 < len(lines) and QUOTE.match(lines[i + 1]):
                i += 1
                inner.append(QUOTE.sub('', lines[i], count=1))
            block(('quote', parse('\n'.join(inner))))
            i += 1
            continue
        if '|' in line and i + 1 < len(lines) and '|' in lines[i + 1] and TABLE_SEP.match(lines[i + 1]):
            head = cells(line)
            align = ['center' if re.match(r'^:-+:$', c) else 'right' if c.endswith(':') else ''
                     for c in cells(lines[i + 1])]
            rows = []
            i += 1
            while i + 1 < len(lines) and '|' in lines[i + 1] and lines[i + 1].strip():
                i += 1
                rows.append(cells(lines[i])[:len(head)])
            block(('table', head, align, rows))
            i += 1
            continue
        m = ITEM.match(line)
        if m:
            indent, ordered = len(m.group(1).replace('\t', '    ')), m.group(3) is not None
            while lists and lists[-1]['indent'] > indent:
                lists.pop()
            top = lists[-1] if lists else None
            if top and top['indent'] == indent and top['ordered'] != ordered:
                lists.pop()
                top = lists[-1] if lists else None
            if not top or indent > top['indent']:
                counter[0] += 1
                top = {'indent': indent, 'ordered': ordered, 'id': counter[0],
                       'start': int(m.group(3)) if ordered else 1}
                lists.append(top)
            blocks.append(('li', len(lists) - 1, top['id'], ordered, top['start'], [m.group(4)]))
            para = None
            i += 1
            continue
        if lists and (line[:1].isspace() or not was_blank):
            last = blocks[-1]
            if was_blank:
                para = ('lip', len(lists) - 1, [line.strip()])
                blocks.append(para)
            elif para is not None and blocks[-1] is para:
                para[2].append(line.strip())
            else:
                last[5].append(line.strip())
            i += 1
            continue
        lists = []
        if para is not None and para[0] == 'p':
            para[1].append(line.strip())
        else:
            para = ('p', [line.strip()])
            blocks.append(para)
        i += 1
    return blocks


# ---------------------------------------------------------------- docx 写出

def x(text):
    return escape(BAD_XML.sub('', text), {'"': '&quot;'})


def run(text, bold=False, italic=False, code=False):
    props = ''
    if code:
        props += '<w:rStyle w:val="CodeChar"/>'
    if bold:
        props += '<w:b/><w:bCs/>'
    if italic:
        props += '<w:i/><w:iCs/>'
    rpr = '<w:rPr>%s</w:rPr>' % props if props else ''
    parts = text.split('\n')
    out = []
    for k, part in enumerate(parts):
        if k:
            out.append('<w:r>%s<w:br/></w:r>' % rpr)
        if part:
            out.append('<w:r>%s<w:t xml:space="preserve">%s</w:t></w:r>' % (rpr, x(part)))
    return ''.join(out)


def inline(text, bold=False, italic=False):
    out, last = [], 0
    for m in INLINE.finditer(text):
        if m.start() > last:
            out.append(run(text[last:m.start()], bold, italic))
        if m.group(1) is not None:
            out.append(run(m.group(1), bold, italic, code=True))
        elif m.group(2) is not None:
            out.append(inline(m.group(2), True, italic))
        elif m.group(3) is not None:
            out.append(inline(m.group(3), bold, True))
        else:
            out.append(inline(m.group(4), bold, italic))
            if m.group(5) != m.group(4):
                out.append(run('（%s）' % m.group(5), bold, italic))
        last = m.end()
    if last < len(text):
        out.append(run(text[last:], bold, italic))
    return ''.join(out)


def lines_inline(lines):
    return inline('\n'.join(lines))


def para(content, style=None, extra=''):
    ppr = ('<w:pStyle w:val="%s"/>' % style if style else '') + extra
    return '<w:p>%s%s</w:p>' % ('<w:pPr>%s</w:pPr>' % ppr if ppr else '', content)


class Doc:
    def __init__(self):
        self.nums = []          # [(numId, 有序, 层级, 起始号)]
        self.list_num = {}      # 解析得到的列表编号 -> numId
        self.stats = {'标题': 0, '段落': 0, '表格': 0, '列表项': 0}

    def num_for(self, list_id, ordered, level, start):
        if list_id not in self.list_num:
            num_id = len(self.nums) + 1
            self.nums.append((num_id, ordered, level, start))
            self.list_num[list_id] = num_id
        return self.list_num[list_id]

    def table(self, head, align, rows):
        n = max(1, len(head))
        col = TEXT_W // n
        out = ['<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/><w:tblW w:w="5000" w:type="pct"/>'
               '<w:tblLook w:val="04A0" w:firstRow="1" w:lastRow="0" w:firstColumn="0" w:lastColumn="0" '
               'w:noHBand="0" w:noVBand="1"/></w:tblPr><w:tblGrid>']
        out.append(''.join('<w:gridCol w:w="%d"/>' % col for _ in range(n)))
        out.append('</w:tblGrid>')
        for r, row in enumerate([head] + rows):
            trpr = '<w:trPr><w:tblHeader/></w:trPr>' if r == 0 else ''
            out.append('<w:tr>%s' % trpr)
            for k in range(n):
                text = row[k] if k < len(row) else ''
                jc = '<w:jc w:val="%s"/>' % align[k] if k < len(align) and align[k] else ''
                body = inline(text.replace('<br>', '\n').replace('<br/>', '\n'), bold=(r == 0))
                out.append('<w:tc><w:tcPr><w:tcW w:w="%d" w:type="dxa"/></w:tcPr>%s</w:tc>'
                           % (col, para(body, 'TableText', jc)))
            out.append('</w:tr>')
        out.append('</w:tbl>')
        # 两张表或表与后文之间留一个空段，避免 Word 把相邻的表并成一张
        return ''.join(out) + para('', 'TableGap')

    def render(self, blocks, quote=False):
        out = []
        for b in blocks:
            kind = b[0]
            if kind == 'h':
                self.stats['标题'] += 1
                out.append(para(inline(b[2]), 'Heading%d' % b[1]))
            elif kind == 'p':
                self.stats['段落'] += 1
                out.append(para(lines_inline(b[1]), 'Quote' if quote else None))
            elif kind == 'code':
                self.stats['段落'] += 1
                out.append(para(run(b[1]), 'CodeBlock'))
            elif kind == 'hr':
                out.append(para('', 'Rule'))
            elif kind == 'quote':
                out.append(self.render(b[1], quote=True))
            elif kind == 'table':
                self.stats['表格'] += 1
                out.append(self.table(b[1], b[2], b[3]))
            elif kind == 'li':
                self.stats['列表项'] += 1
                _, level, list_id, ordered, start, lines = b
                level = min(level, 8)
                num_id = self.num_for(list_id, ordered, level, start)
                out.append(para(lines_inline(lines), 'ListParagraph',
                                '<w:numPr><w:ilvl w:val="%d"/><w:numId w:val="%d"/></w:numPr>' % (level, num_id)))
            elif kind == 'lip':
                level = min(b[1], 8)
                out.append(para(lines_inline(b[2]), 'ListParagraph',
                                '<w:ind w:left="%d"/>' % (420 * (level + 1))))
        return ''.join(out)


W_NS = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"')

CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '<Override PartName="/word/styles.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
    '<Override PartName="/word/numbering.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>'
    '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
    '<Override PartName="/docProps/app.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
    '</Types>')

ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="word/document.xml"/>'
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" '
    'Target="docProps/core.xml"/>'
    '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" '
    'Target="docProps/app.xml"/>'
    '</Relationships>')

DOC_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
    'Target="styles.xml"/>'
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" '
    'Target="numbering.xml"/>'
    '</Relationships>')

APP = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
       '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
       '<Application>make_docx.py</Application></Properties>')

BODY_FONT = '<w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" w:eastAsia="宋体" w:cs="Times New Roman"/>'
HEAD_FONT = '<w:rFonts w:ascii="Arial" w:hAnsi="Arial" w:eastAsia="黑体" w:cs="Arial"/>'
MONO_FONT = '<w:rFonts w:ascii="Courier New" w:hAnsi="Courier New" w:eastAsia="宋体" w:cs="Courier New"/>'
HEAD_SIZES = (32, 30, 28, 24, 21, 21)      # 半磅：三号、小三、四号、小四、五号、五号


def styles():
    s = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:styles %s>' % W_NS,
         '<w:docDefaults><w:rPrDefault><w:rPr>%s<w:sz w:val="21"/><w:szCs w:val="21"/>'
         '<w:lang w:val="en-US" w:eastAsia="zh-CN"/></w:rPr></w:rPrDefault>'
         '<w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="312" w:lineRule="auto"/>'
         '</w:pPr></w:pPrDefault></w:docDefaults>' % BODY_FONT,
         '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/></w:style>',
         '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/>'
         '<w:next w:val="Normal"/><w:qFormat/><w:pPr><w:keepNext/><w:spacing w:before="240" w:after="360"/>'
         '<w:jc w:val="center"/><w:outlineLvl w:val="0"/></w:pPr>'
         '<w:rPr>%s<w:b/><w:bCs/><w:sz w:val="36"/><w:szCs w:val="36"/></w:rPr></w:style>' % HEAD_FONT]
    for n, size in enumerate(HEAD_SIZES, 1):
        s.append('<w:style w:type="paragraph" w:styleId="Heading%d"><w:name w:val="heading %d"/>'
                 '<w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:uiPriority w:val="9"/><w:qFormat/>'
                 '<w:pPr><w:keepNext/><w:keepLines/><w:spacing w:before="%d" w:after="120"/>'
                 '<w:outlineLvl w:val="%d"/></w:pPr>'
                 '<w:rPr>%s<w:b/><w:bCs/><w:sz w:val="%d"/><w:szCs w:val="%d"/></w:rPr></w:style>'
                 % (n, n, 360 if n <= 2 else 240, n - 1, HEAD_FONT, size, size))
    s.append('<w:style w:type="paragraph" w:styleId="ListParagraph"><w:name w:val="List Paragraph"/>'
             '<w:basedOn w:val="Normal"/><w:qFormat/><w:pPr><w:spacing w:after="60"/></w:pPr>'
             '</w:style>')
    s.append('<w:style w:type="paragraph" w:styleId="Quote"><w:name w:val="Quote"/><w:basedOn w:val="Normal"/>'
             '<w:qFormat/><w:pPr><w:pBdr><w:left w:val="single" w:sz="18" w:space="8" w:color="BFBFBF"/></w:pBdr>'
             '<w:ind w:left="420"/></w:pPr><w:rPr><w:color w:val="595959"/></w:rPr></w:style>')
    s.append('<w:style w:type="paragraph" w:styleId="CodeBlock"><w:name w:val="Code Block"/>'
             '<w:basedOn w:val="Normal"/><w:pPr><w:shd w:val="clear" w:color="auto" w:fill="F2F2F2"/>'
             '<w:spacing w:after="120" w:line="240" w:lineRule="auto"/></w:pPr>'
             '<w:rPr>%s<w:sz w:val="18"/><w:szCs w:val="18"/></w:rPr></w:style>' % MONO_FONT)
    s.append('<w:style w:type="paragraph" w:styleId="Rule"><w:name w:val="Horizontal Rule"/>'
             '<w:basedOn w:val="Normal"/><w:pPr><w:pBdr><w:bottom w:val="single" w:sz="6" w:space="1" '
             'w:color="A6A6A6"/></w:pBdr></w:pPr></w:style>')
    s.append('<w:style w:type="paragraph" w:styleId="TableText"><w:name w:val="Table Text"/>'
             '<w:basedOn w:val="Normal"/><w:pPr><w:spacing w:before="40" w:after="40" w:line="264" '
             'w:lineRule="auto"/></w:pPr></w:style>')
    s.append('<w:style w:type="paragraph" w:styleId="TableGap"><w:name w:val="Table Gap"/>'
             '<w:basedOn w:val="Normal"/><w:pPr><w:spacing w:after="0" w:line="120" w:lineRule="auto"/></w:pPr>'
             '<w:rPr><w:sz w:val="4"/><w:szCs w:val="4"/></w:rPr></w:style>')
    s.append('<w:style w:type="character" w:styleId="CodeChar"><w:name w:val="Code Char"/>'
             '<w:rPr>%s<w:shd w:val="clear" w:color="auto" w:fill="F2F2F2"/></w:rPr></w:style>' % MONO_FONT)
    border = ''.join('<w:%s w:val="single" w:sz="4" w:space="0" w:color="808080"/>' % side
                     for side in ('top', 'left', 'bottom', 'right', 'insideH', 'insideV'))
    s.append('<w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/>'
             '<w:tblPr><w:tblBorders>%s</w:tblBorders><w:tblCellMar><w:left w:w="108" w:type="dxa"/>'
             '<w:right w:w="108" w:type="dxa"/></w:tblCellMar></w:tblPr></w:style>' % border)
    s.append('</w:styles>')
    return ''.join(s)


BULLETS = ('•', '◦', '▪')


def numbering(nums):
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:numbering %s>' % W_NS]
    for abstract, ordered in ((1, False), (2, True)):
        out.append('<w:abstractNum w:abstractNumId="%d"><w:multiLevelType w:val="hybridMultilevel"/>' % abstract)
        for lvl in range(9):
            fmt, text = ('decimal', '%%%d.' % (lvl + 1)) if ordered else ('bullet', BULLETS[lvl % 3])
            out.append('<w:lvl w:ilvl="%d"><w:start w:val="1"/><w:numFmt w:val="%s"/><w:lvlText w:val="%s"/>'
                       '<w:lvlJc w:val="left"/><w:pPr><w:ind w:left="%d" w:hanging="420"/></w:pPr></w:lvl>'
                       % (lvl, fmt, text, 420 * (lvl + 1)))
        out.append('</w:abstractNum>')
    for num_id, ordered, level, start in nums:
        out.append('<w:num w:numId="%d"><w:abstractNumId w:val="%d"/>'
                   '<w:lvlOverride w:ilvl="%d"><w:startOverride w:val="%d"/></w:lvlOverride></w:num>'
                   % (num_id, 2 if ordered else 1, level, start))
    out.append('</w:numbering>')
    return ''.join(out)


def core(title):
    now = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            '<dc:title>%s</dc:title>'
            '<dcterms:created xsi:type="dcterms:W3CDTF">%s</dcterms:created>'
            '<dcterms:modified xsi:type="dcterms:W3CDTF">%s</dcterms:modified>'
            '</cp:coreProperties>') % (x(title), now, now)


def build_docx(markdown, title=''):
    """把 Markdown 转成 docx，返回 (字节, 统计)。title 非空时作为首行标题与文档属性里的标题。"""
    doc = Doc()
    body = doc.render(parse(markdown))
    if title.strip():
        body = para(inline(title.strip()), 'Title') + body
    sect = ('<w:sectPr><w:pgSz w:w="%d" w:h="%d"/><w:pgMar w:top="%d" w:right="%d" w:bottom="%d" w:left="%d" '
            'w:header="851" w:footer="992" w:gutter="0"/></w:sectPr>'
            % (PAGE_W, PAGE_H, MARGIN_Y, MARGIN_X, MARGIN_Y, MARGIN_X))
    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document %s><w:body>%s%s</w:body>'
                '</w:document>') % (W_NS, body, sect)
    buf = BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', CONTENT_TYPES)
        z.writestr('_rels/.rels', ROOT_RELS)
        z.writestr('word/document.xml', document)
        z.writestr('word/_rels/document.xml.rels', DOC_RELS)
        z.writestr('word/styles.xml', styles())
        z.writestr('word/numbering.xml', numbering(doc.nums))
        z.writestr('docProps/core.xml', core(title.strip()))
        z.writestr('docProps/app.xml', APP)
    return buf.getvalue(), doc.stats


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
        return sys.stdin.read(), '文档'
    path = arg
    work = os.environ.get('ASK_WORK', '')
    if not os.path.isabs(arg) and work and os.path.isfile(os.path.join(work, arg)):
        path = os.path.join(work, arg)
    if not os.path.isfile(path):
        raise Fail('找不到输入文件 %s：请先用 write_work_file 写入工作目录，再把同样的文件名传给本脚本' % arg)
    with open(path, 'rb') as f:
        raw = f.read(MAX_INPUT_CHARS * 4 + 1)
    for enc in ('utf-8-sig', 'gb18030'):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise Fail('%s 不是 UTF-8 文本' % arg)
    if len(text) > MAX_INPUT_CHARS:
        raise Fail('%s 超过 %d 字' % (arg, MAX_INPUT_CHARS))
    return text, os.path.splitext(os.path.basename(path))[0]


def main(argv=None):
    ap = argparse.ArgumentParser(description='把 Markdown 子集转成 Word（docx），写到 ASK_OUT。')
    ap.add_argument('input', help='Markdown 文件：相对路径先在 ASK_WORK 里找；- 表示标准输入')
    ap.add_argument('--out', help='生成的文件名，默认与输入同名，扩展名自动补 .docx')
    ap.add_argument('--title', default='', help='文档标题，写在第一行并记入文档属性')
    ap.add_argument('--log-level', default=os.environ.get('LOG_LEVEL', 'WARNING'),
                    choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'], help='日志级别（默认取环境变量 LOG_LEVEL）')
    a = ap.parse_args(argv)
    logging.basicConfig(level=a.log_level, format=LOG_FORMAT, stream=sys.stderr)
    try:
        text, stem = read_input(a.input)
        if not text.strip():
            raise Fail('输入是空的，没有可写入的内容')
        data, stats = build_docx(text, a.title)
        folder = os.environ.get('ASK_OUT') or os.getcwd()
        path = unique_path(folder, safe_name(a.out or stem, '.docx', '文档'))
        with open(path, 'wb') as f:
            f.write(data)
    except Fail as e:
        print('生成失败：%s。' % e)
        return 2
    except OSError as e:
        print('生成失败：%s。' % e)
        return 2
    log.info('wrote %s (%d bytes)', path, len(data))
    print('已生成 %s：%s。' % (os.path.basename(path), '、'.join('%s %d 个' % kv for kv in stats.items())))
    return 0


if __name__ == '__main__':
    sys.exit(main())
