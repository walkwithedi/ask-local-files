#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""预装 skill 里的脚本：用临时文件按页面运行时的环境变量调用，只用标准库 unittest。"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
import xml.dom.minidom

TOOL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS = os.path.join(TOOL, 'skills')

CT = ('<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
      '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
      '<Default Extension="xml" ContentType="application/xml"/>%s</Types>')
RELS = ('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/%s" Target="%s"/>'
        '</Relationships>')


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)


def make_xlsx(path):
    """最小 xlsx：一个工作表「明细」，表头 部门/金额，含共享字符串与行内字符串。"""
    ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    sheet = ('<worksheet xmlns="%s"><sheetData>'
             '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
             '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>100</v></c></row>'
             '<row r="3"><c r="A3" t="inlineStr"><is><t>财务</t></is></c><c r="B3"><v>50.5</v></c></row>'
             '<row r="4"><c r="A4" t="s"><v>3</v></c><c r="B4"><v>30</v></c></row>'
             '</sheetData></worksheet>') % ns
    shared = '<sst xmlns="%s">%s</sst>' % (ns, ''.join('<si><t>%s</t></si>' % t for t in ('部门', '金额', '财务', '销售')))
    wb = ('<workbook xmlns="%s" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
          '<sheets><sheet name="明细" sheetId="1" r:id="rId1"/></sheets></workbook>') % ns
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('[Content_Types].xml', CT % '')
        z.writestr('_rels/.rels', RELS % ('officeDocument', 'xl/workbook.xml'))
        z.writestr('xl/workbook.xml', wb)
        z.writestr('xl/_rels/workbook.xml.rels', RELS % ('worksheet', 'worksheets/sheet1.xml'))
        z.writestr('xl/worksheets/sheet1.xml', sheet)
        z.writestr('xl/sharedStrings.xml', shared)


class ScriptCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='skill-scripts-')
        self.out = os.path.join(self.root, 'out')
        os.makedirs(self.out)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def run_script(self, skill, script, args, files, roots=()):
        env = dict(os.environ, ASK_FILES='\n'.join(files), ASK_ROOTS='\n'.join(roots), ASK_OUT=self.out,
                   ASK_WORK=os.path.join(self.root, 'work'), ASK_TOOL_DIR=TOOL, PYTHONIOENCODING='utf-8')
        d = os.path.join(SKILLS, skill)
        r = subprocess.run([sys.executable, os.path.join(d, script)] + args, cwd=d, env=env,
                           capture_output=True, text=True, encoding='utf-8', timeout=60)
        return r.returncode, r.stdout


class DocCompareTest(ScriptCase):
    def test_lists_changes_and_html(self):
        a, b = os.path.join(self.root, '旧.txt'), os.path.join(self.root, '新.txt')
        write(a, '付款期限 30 天。\n甲方负责验收。\n保密条款。\n')
        write(b, '付款期限 45 天。\n甲方负责验收。\n')
        rc, out = self.run_script('doc-compare', 'scripts/compare.py', ['--html', '报告'], [a, b])
        self.assertEqual(rc, 0)
        self.assertIn('修改 1、新增 0、删除 1', out)
        self.assertIn('【-30-】【+45+】', out)
        self.assertTrue(os.path.isfile(os.path.join(self.out, '报告.html')))

    def test_xlsx_through_doc2kb(self):
        a, b = os.path.join(self.root, '旧', '费用.xlsx'), os.path.join(self.root, '新', '费用.xlsx')
        os.makedirs(os.path.dirname(a))
        os.makedirs(os.path.dirname(b))
        make_xlsx(a)
        make_xlsx(b)
        rc, out = self.run_script('doc-compare', 'scripts/compare.py', [], [a, b])
        self.assertEqual(rc, 0, out)
        self.assertIn('完全相同', out)

    def test_needs_two_files(self):
        rc, out = self.run_script('doc-compare', 'scripts/compare.py', ['1'], [])
        self.assertEqual(rc, 2)


def read_back(path):
    """用工具目录的 doc2kb_core 取出生成文件的文字，与界面上「问 AI」读文件的方式相同。"""
    if TOOL not in sys.path:
        sys.path.insert(0, TOOL)
    import doc2kb_core
    doc2kb_core.set_lang('zh')
    with tempfile.TemporaryDirectory() as tmp:
        md, _ = doc2kb_core.convert(path, os.path.splitext(path)[1], tmp, doc2kb_core.Report(), save_images=False)
    return md


class OfficeGenTest(ScriptCase):
    def setUp(self):
        super().setUp()
        self.work = os.path.join(self.root, 'work')

    def assert_valid_zip(self, path):
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                xml.dom.minidom.parseString(z.read(name))

    def test_docx_round_trip(self):
        write(os.path.join(self.work, '报告.md'),
              '# 季度报告\n\n本季度**收入**增长，见[附表](http://example.com)。\n第二行 `a<b&c`\n\n'
              '- 第一项\n  - 子项\n- 第二项\n\n1. 步骤一\n2. 步骤二\n\n'
              '| 部门 | 金额 |\n|---|--:|\n| 财务 | 100 |\n\n> 引用\n\n```\n代码 <x>\n```\n\n---\n结尾\x01')
        rc, out = self.run_script('office-gen', 'scripts/make_docx.py', ['报告.md', '--out', '季度/汇总',
                                                                         '--title', '标题 & 说明'], [])
        self.assertEqual(rc, 0, out)
        self.assertIn('已生成 汇总.docx', out)
        path = os.path.join(self.out, '汇总.docx')
        self.assert_valid_zip(path)
        with zipfile.ZipFile(path) as z:
            doc = z.read('word/document.xml').decode('utf-8')
            self.assertIn('w:val="Heading1"', doc)
            self.assertIn('w:val="Title"', doc)
            self.assertIn('w:w="11906" w:h="16838"', doc)
            self.assertIn('<w:tblHeader/>', doc)
            self.assertIn('w:eastAsia="黑体"', z.read('word/styles.xml').decode('utf-8'))
        md = read_back(path)
        for want in ('标题 & 说明', '# 季度报告', '本季度收入增长，见附表（http://example.com）', 'a<b&c',
                     '第一项', '子项', '步骤二', '| 财务 | 100 |', '引用', '代码 <x>', '结尾'):
            self.assertIn(want, md)

    def test_docx_no_overwrite_and_errors(self):
        write(os.path.join(self.work, 'a.md'), '正文')
        for _ in range(2):
            rc, out = self.run_script('office-gen', 'scripts/make_docx.py', ['a.md'], [])
            self.assertEqual(rc, 0, out)
        self.assertEqual(sorted(os.listdir(self.out)), ['a(2).docx', 'a.docx'])
        rc, out = self.run_script('office-gen', 'scripts/make_docx.py', ['没有.md'], [])
        self.assertEqual(rc, 2)
        self.assertIn('write_work_file', out)

    def test_xlsx_round_trip(self):
        data = {'sheets': [
            {'name': '明细', 'rows': [['部门', '金额', '日期', '编号', '时间'],
                                      ['财务', 100, '2026-01-31', '001', '2026-02-01 09:30'],
                                      ['销售', 50.5, '2026-02-30', 12345678901234567, None], [True]]},
            {'name': '明细', 'rows': [['x']], 'header': False}]}
        write(os.path.join(self.work, '数据.json'), json.dumps(data, ensure_ascii=False))
        rc, out = self.run_script('office-gen', 'scripts/make_xlsx.py', ['数据.json'], [])
        self.assertEqual(rc, 0, out)
        self.assertIn('「明细(2)」1 行', out)
        self.assertIn('改为「明细(2)」', out)
        path = os.path.join(self.out, '数据.xlsx')
        self.assert_valid_zip(path)
        with zipfile.ZipFile(path) as z:
            s1 = z.read('xl/worksheets/sheet1.xml').decode('utf-8')
            s2 = z.read('xl/worksheets/sheet2.xml').decode('utf-8')
        self.assertIn('state="frozen"', s1)
        self.assertIn('<autoFilter ref="A1:E4"/>', s1)
        self.assertIn('<c r="B2" s="0"><v>100</v></c>', s1)
        self.assertIn('<c r="C2" s="2"><v>46053</v></c>', s1)     # 2026-01-31 的 Excel 序列值
        self.assertNotIn('frozen', s2)
        md = read_back(path)
        for want in ('| 财务 | 100 | 2026-01-31 | 001 | 2026-02-01 09:30 |', '2026-02-30', '12345678901234567'):
            self.assertIn(want, md)

    def test_xlsx_bad_input(self):
        for bad, msg in (('{"sheets": []}', '至少一个工作表'), ('[1', 'JSON 格式有误'),
                         ('{"sheets": [{"rows": [[{"a": 1}]]}]}', '第 1 个工作表第 1 行第 1 列'),
                         ('{"sheets": [{"rows": "x"}]}', '缺少 rows')):
            write(os.path.join(self.work, 'bad.json'), bad)
            rc, out = self.run_script('office-gen', 'scripts/make_xlsx.py', ['bad.json'], [])
            self.assertEqual(rc, 2, bad)
            self.assertIn(msg, out)
        self.assertEqual(os.listdir(self.out), [])

    def test_help(self):
        for script in ('make_docx.py', 'make_xlsx.py'):
            rc, out = self.run_script('office-gen', 'scripts/' + script, ['--help'], [])
            self.assertEqual(rc, 0)
            self.assertIn('--log-level', out)


class SkillCreateTest(ScriptCase):
    MD = '---\nname: %s\ndescription: %s\n---\n\n%s\n'

    def setUp(self):
        super().setUp()
        self.skill = os.path.join(self.root, 'work', 'skill')

    def pack(self):
        return self.run_script('skill-create', 'scripts/pack_skill.py', ['skill'], [])

    def test_pack_and_installable_layout(self):
        write(os.path.join(self.skill, 'SKILL.md'), self.MD % ('weekly-report', '写周报', '按周汇总要点。'))
        write(os.path.join(self.skill, 'references', '术语.md'), '术语表')
        rc, out = self.pack()
        self.assertEqual(rc, 0, out)
        self.assertIn('已打包 weekly-report.zip：2 个文件', out)
        with zipfile.ZipFile(os.path.join(self.out, 'weekly-report.zip')) as z:
            self.assertEqual(sorted(z.namelist()), ['weekly-report/SKILL.md', 'weekly-report/references/术语.md'])
        # 重名不覆盖
        rc, out = self.pack()
        self.assertIn('weekly-report-2.zip', out)

    def test_rejects(self):
        cases = [
            ({'SKILL.md': self.MD % ('ok', '用途', '正文'), 'scripts/run.py': 'print(1)'}, '不打包脚本'),
            ({'SKILL.md': self.MD % ('Bad_Name', '用途', '正文')}, '小写字母'),
            ({'SKILL.md': '---\nname: ok\n---\n\n正文'}, '缺少 name 或 description'),
            ({'SKILL.md': self.MD % ('ok', '用' * 1025, '正文')}, 'description 超过'),
            ({'SKILL.md': self.MD % ('ok', '用途', '')}, '没有说明正文'),
            ({'readme.md': 'x'}, '没有 SKILL.md'),
        ]
        for files, msg in cases:
            shutil.rmtree(self.skill, ignore_errors=True)
            for rel, text in files.items():
                write(os.path.join(self.skill, rel), text)
            rc, out = self.pack()
            self.assertEqual(rc, 2, out)
            self.assertIn('打包失败', out)
            self.assertIn(msg, out)
        self.assertEqual(os.listdir(self.out), [])

    def test_rejects_link_and_missing_folder(self):
        write(os.path.join(self.skill, 'SKILL.md'), self.MD % ('ok', '用途', '正文'))
        os.symlink('/etc/hostname', os.path.join(self.skill, 'x.md'))
        rc, out = self.pack()
        self.assertEqual(rc, 2)
        self.assertIn('链接', out)
        rc, out = self.run_script('skill-create', 'scripts/pack_skill.py', ['没有'], [])
        self.assertEqual(rc, 2)
        self.assertIn('找不到文件夹', out)

    def test_help(self):
        rc, out = self.run_script('skill-create', 'scripts/pack_skill.py', ['--help'], [])
        self.assertEqual(rc, 0)
        self.assertIn('--log-level', out)


if __name__ == '__main__':
    unittest.main()
