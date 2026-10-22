#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""doc2kb_core.py - turn Office, PDF and mail files into Markdown for a knowledge base.

Design constraints:
  - Python 3 standard library only (zipfile, xml.etree, email, ...); Python 3.7 or newer.
  - No pandoc, LibreOffice, python-docx, openpyxl or python-pptx.
  - PDF needs the poppler-utils commands (pdftotext, pdfimages, pdfinfo); without them a
    PDF is reported as failed and everything else still works.

Output (one file mode):
  <outdir>/
      <document>.md          the converted document, embedded workbooks inlined
      images/                exported pictures
      _image-list.md         every picture with its section and whether text describes it
      _batch-report.txt      processing log and warnings

Labels written into the output follow --lang (en or zh); the zh labels and file names stay
fixed between versions, so existing output folders keep working.

docx, xlsx and pptx are zip packages of OOXML parts; this module reads the XML directly.
Heading levels come from the styles.xml mapping plus heuristics, so documents with
irregular styles lose some structure but never their text.

Used by doc2kb.sh (desktop and cron use); other tools can import it as a library
(process_one(), convert()).
"""

import argparse
import datetime
import hashlib
import io
import json
import logging
import os
import posixpath
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from xml.etree import ElementTree as ET


log = logging.getLogger('doc2kb')
LOG_FORMAT = '%(asctime)s [%(levelname)s] %(name)s.%(funcName)s:%(lineno)d - %(message)s'

# ---------------------------------------------------------------- labels

LANGS = ('en', 'zh')
LANG = 'en'

# key: (English, Chinese). Keep the Chinese text unchanged: fingerprints and the file
# parsers (doc2kb_dedup.py, older output folders) depend on it.
LABELS = {
    # report
    'warning_prefix': ('[warning] ', '[警告] '),
    # xlsx
    'xlsx_styles_failed': ('styles.xml could not be parsed; dates are shown as raw numbers (%s): %s',
                           'styles.xml 解析失败，日期将按原始数值输出 (%s): %s'),
    'xlsx_open_failed': ('cannot open xlsx (%s): %s', '无法打开 xlsx (%s): %s'),
    'xlsx_shared_failed': ('sharedStrings could not be parsed (%s): %s', 'sharedStrings 解析失败 (%s): %s'),
    'xlsx_workbook_failed': ('workbook could not be parsed (%s): %s', 'workbook 解析失败 (%s): %s'),
    'xlsx_sheet_missing': ('sheet "%s" is not in the package (%s); skipped', 'sheet「%s」在包内找不到（%s），已跳过'),
    'no_path': ('no path', '无路径'),
    'xlsx_sheet_failed': ('sheet %s could not be parsed: %s', 'sheet %s 解析失败: %s'),
    'xlsx_blank_rows': ('  sheet "%s": skipped %d empty rows', '  sheet「%s」跳过了 %d 个全空行'),
    'xlsx_truncated': ('%s / sheet "%s" has more than %d rows; truncated', '%s / sheet「%s」超过 %d 行，已截断'),
    'xlsx_truncated_row': ('... (further rows left out; the sheet has more than %d rows)',
                           '...（后续行已省略，原表超过 %d 行）'),
    'empty_table': ('(empty table)\n', '（空表）\n'),
    'column_n': ('Column %d', '列%d'),
    # docx
    'docx_styles_failed': ('styles.xml could not be parsed: %s', 'styles.xml 解析失败: %s'),
    'emb_docx_open_failed': ('embedded docx cannot be opened (%s): %s', '嵌入 docx 无法打开 (%s): %s'),
    'emb_no_document': ('embedded object %s has no document.xml', '嵌入对象 %s 不含 document.xml'),
    'emb_docx_failed': ('embedded docx could not be parsed (%s): %s', '嵌入 docx 解析失败 (%s): %s'),
    'emb_docx_images': ('\n> (This embedded document holds %d images; they were not extracted. '
                        'Open the attachment separately if you need them.)\n',
                        '\n> （此嵌入文档含 %d 张图片，未提取。如有需要请单独打开该附件。）\n'),
    'docx_invalid': ('not a valid docx (no document.xml)', '不是有效的 docx（缺 document.xml）'),
    'docx_style_map': ('heading style mapping: %d entries', '标题样式映射: %d 条'),
    'docx_no_styles': ('no heading style recognised; the output has no heading levels',
                       '未能识别任何标题样式，输出将缺少标题层级'),
    'docx_no_body': ('document.xml has no body', 'document.xml 缺 body'),
    'table_marker': ('[table]', '[表格]'),
    'doc_start': ('(start of document)', '（文档开头）'),
    'orphan_tag': ('  ← orphan image (no text describes it)', '  ← 孤儿图（周围无文字描述）'),
    'image_at': ('see %s/%s', '见 %s/%s'),
    'image_not_saved': ('image file not exported in this run', '本次未导出图片文件'),
    'docx_anchor': ('\n[Image %03d: %s, %s]%s\n', '\n[图 %03d：%s，%s]%s\n'),
    'emb_found': ('found %d embedded objects', '发现 %d 个嵌入对象'),
    'emb_missing': ('embedded object %s is not in the package (%s)', '嵌入对象 %s 在包内找不到 (%s)'),
    'emb_same': ('\n> ℹ The attachment embedded here (%s) has exactly the same content as "%s" above; '
                 'it is not expanded again, see above.\n',
                 '\n> ℹ 此处嵌入的附件（%s）与前文「%s」内容完全相同，不再重复展开，请参见上文。\n'),
    'emb_same_word': ('\n> ℹ The attachment embedded here (%s) has exactly the same content as "%s" above; '
                      'it is not expanded again.\n',
                      '\n> ℹ 此处嵌入的附件（%s）与前文「%s」内容完全相同，不再重复展开。\n'),
    'emb_dup_bytes': ('skipped duplicate attachment %s (same as %s)', '跳过重复附件 %s（与 %s 相同）'),
    'emb_dup_text': ('skipped duplicate attachment %s (same content as %s)', '跳过重复附件 %s（与 %s 内容相同）'),
    'legacy_kind': ('legacy Office format or OLE container', '老版 Office 格式或 OLE 容器'),
    'emb_legacy_warn': ('embedded object %s is a %s and cannot be opened with the standard library. '
                        'Open it in an office application, save it as xlsx/docx and process that file separately.',
                        '嵌入对象 %s 是%s，纯标准库无法解开。请在 WPS 里双击该对象，另存为 xlsx/docx 后单独处理。'),
    'emb_gap': ('\n> ⚠ An attachment is embedded here (%s, %s); it could not be opened automatically, '
                'get its content separately.\n',
                '\n> ⚠ 此处嵌入了一个附件（%s，%s），未能自动解开，需单独获取内容。\n'),
    'emb_unknown': ('embedded object %s has an unknown type; skipped', '嵌入对象 %s 类型无法识别，已跳过'),
    'inline_word': ('\n---\n\n**Inline attachment (Word): %s**\n', '\n---\n\n**内联附件（Word）：%s**\n'),
    'inlined_word': ('inlined Word attachment %s (%d characters)', '已内联 Word 附件 %s（%d 字符）'),
    'sheet_caption': ('\n*Sheet: %s* (%d rows)\n', '\n*工作表：%s*（%d 行）\n'),
    'inline_book': ('\n---\n\n**Inline attachment: %s**\n', '\n---\n\n**内联附件：%s**\n'),
    'inlined_book': ('inlined %s (%d sheets, %d characters)', '已内联 %s（%d 个 sheet，%d 字符）'),
    # pptx
    'pptx_no_slides': ('no slides found', '未找到任何幻灯片'),
    'pptx_count': ('%d slides', '共 %d 页幻灯片'),
    'slide_heading': ('\n## Slide %d\n', '\n## 第 %d 页\n'),
    'slide_no_text': ('*(no text on this slide)*', '*（本页无文字）*'),
    'slide_anchor': ('\n[Image: slide %d, %s]\n', '\n[图：第 %d 页，%s]\n'),
    'slide_section': ('Slide %d', '第 %d 页'),
    'list_sep': (', ', '、'),
    'pptx_shapes': ('these slides may hold charts drawn with shapes (vector, cannot be exported as images): '
                    'slide %s. Take screenshots of them by hand.',
                    '以下页面可能用 PPT 形状绘制了图表（矢量，无法提取为图片）：第 %s 页。这些页需要手工截图。'),
    # xlsx file
    'sheet_heading': ('\n## Sheet: %s\n', '\n## 工作表：%s\n'),
    'sheet_rows': ('%d rows\n', '共 %d 行\n'),
    'xlsx_count': ('xlsx: %d sheets', 'xlsx: %d 个工作表'),
    # pdf
    'pdf_no_tool': ('pdftotext is missing; PDF cannot be processed', '缺少 pdftotext，无法处理 PDF'),
    'pdf_no_text': ('the PDF has almost no text layer; it may be a scan. This tool does no OCR.',
                    'PDF 几乎没有文本层，可能是扫描件。本工具不做 OCR。'),
    'pdf_section': ('(PDF has no section information)', '（PDF 无章节信息）'),
    'pdf_images': ('extracted %d embedded images from the PDF (fragments under 8 KB dropped)',
                   'PDF 抽出 %d 张嵌入图片（已过滤小于 8KB 的碎片）'),
    'pdf_images_warn': ('a PDF does not tell where images sit in the text; every image is marked for review. '
                        'Use the original docx/pptx if you have it.',
                        'PDF 路径下无法判断图与正文的位置关系，所有图均标为待人工确认。'
                        '如有原始 docx/pptx，强烈建议改用原始文件。'),
    'pdf_lossy': ('PDF is a lossy intermediate format: embedded workbooks are lost, vector graphics cannot '
                  'be extracted and heading levels are gone. Use the original docx/pptx if you can.',
                  'PDF 是有损中间格式：嵌入的 Excel 附件会丢失，矢量图无法提取，标题层级也已丢失。'
                  '如能拿到原始 docx/pptx，请优先使用原始文件。'),
    # contact sheets
    'montage_missing': ('montage (ImageMagick) not found; contact sheets skipped. Install: sudo apt install imagemagick',
                        '未找到 montage 命令（ImageMagick），跳过拼图。安装: sudo apt install imagemagick'),
    'sheet_dir': ('contact-sheets', '合并图'),
    'sheet_file': ('%scontact-sheet_%02d.png', '%s合并图_%02d.png'),
    'sheet_label': ('Image %03d', '图 %03d'),
    'sheet_failed': ('contact sheet %d failed: %s', '拼图第 %d 张失败: %s'),
    'sheet_done': ('made %d contact sheets (%d tiles each, numbered as in the image list)',
                   '已生成 %d 张合并图（每张 %d 格，编号与图片清单一致）→ 合并图/'),
    'sheet_hint': ('  note: contact sheets are for browsing; give an AI the single images from images/ to read them',
                   '  提示: 合并图仅作索引浏览，AI 细读单图请从 images/ 单发原图'),
    # eml
    'eml_failed': ('cannot parse mail %s: %s', '无法解析邮件 %s: %s'),
    'no_subject': ('(no subject)', '(无主题)'),
    'mail_info': ('## Mail\n', '## 邮件信息\n'),
    'mail_table_head': ('| Field | Value |', '| 字段 | 内容 |'),
    'mail_from': ('From', '发件人'),
    'mail_to': ('To', '收件人'),
    'mail_cc': ('Cc', '抄送'),
    'mail_date': ('Date', '日期'),
    'mail_body': ('## Body\n', '## 正文\n'),
    'mail_empty': ('*(empty body)*', '*（正文为空）*'),
    'mail_img_failed': ('cannot write mail image %s: %s', '写入邮件图片失败 %s: %s'),
    'mail_anchor': ('\n[Image %03d: picture in the mail (%s), %s]  ← orphan image (mail pictures rarely have a description)\n',
                    '\n[图 %03d：邮件内图片（%s），%s]  ← 孤儿图（邮件图片通常无文字描述）\n'),
    'mail_img_section': ('Mail pictures', '邮件图片'),
    'mail_img_heading': ('## Mail pictures\n', '## 邮件图片\n'),
    'mail_att_heading': ('## Attachments\n', '## 附件\n'),
    'mail_att_note1': ('> This tool does not read mail attachments; they are only listed here.',
                       '> 本工具不解析邮件附件，以下仅登记附件清单作为占位。'),
    'mail_att_note2': ('> To get an attachment\'s content, save it from the mail and convert that file separately.\n',
                       '> 如需附件内容，请从邮件中另存附件后，用本工具单独处理该文件。\n'),
    'mail_att_head': ('| No. | Attachment | Size | Type |', '| 序号 | 附件名 | 大小 | 类型 |'),
    'att_supported': ('can be converted separately', '本工具可单独处理'),
    'att_legacy': ('legacy binary format, save in a new format first', '旧版二进制格式，需先另存为新格式'),
    'att_unsupported': ('unsupported type', '不支持的类型'),
    'mail_log': ('mail: subject "%s", %d attachments, %d images', '邮件: 主题「%s」，附件 %d 个，图片 %d 张'),
    # duplicates and moved documents
    'moved_heading': ('## Documents moved, content unchanged', '## 内容未变、仅位置变动的文档'),
    'moved_note': ('> These %d documents were scanned again in this run (usually a folder was moved, renamed or '
                   'copied, which refreshes file times). Their text is identical to what is already in the '
                   'knowledge base, so it is left out here; read it in "File holding the text".',
                   '> 以下 %d 份文档本轮被重新扫描到（通常是目录移动、改名或拷贝刷新了文件时间），'
                   '提取出的正文与已入库版本完全相同，正文从略，请到「正文所在文件」中查阅。'),
    'moved_table_head': ('| Current path | Old path | File holding the text |', '| 现路径 | 原路径 | 正文所在文件 |'),
    'dup_note': ('\n> Note: %d more copies with the same content in this folder were left out: %s\n',
                 '\n> 注：本目录下另有 %d 份内容相同的副本，已略去：%s\n'),
    'dup_log': ('duplicates: kept %s, left out %d copies with the same content', '去重：保留 %s，略去 %d 份内容相同的副本'),
    # times
    'src_doc': ('document properties', '文档属性'),
    'src_mail': ('mail date', '邮件日期'),
    'src_fs': ('file system time', '文件系统时间'),
    'src_newest': ('latest change time', '最新变动时间'),
    'unknown': ('unknown', '未知'),
    # process_one
    'status_ok': ('done', '成功'),
    'status_failed': ('failed', '失败'),
    'status_skipped': ('kept', '跳过'),
    'unsupported': ('unsupported format %s', '不支持的格式 %s'),
    'corrupt_note': ('file damaged or encrypted, cannot open', '文件损坏或加密，无法打开'),
    'corrupt_warn': ('%s: file damaged or encrypted', '%s: 文件损坏或加密'),
    'parse_error_note': ('parse error: %s', '解析异常: %s'),
    'parse_error_warn': ('%s: parse error %s', '%s: 解析异常 %s'),
    'nothing_extracted': ('nothing was extracted', '未提取到任何内容'),
    'header': ('> Source file: %s\n'
               '> Document last modified: %s (from: %s%s)\n'
               '> Extracted: %s\n'
               '> Note: "Document last modified" is when the document itself last changed; "Extracted" is when '
               'this knowledge base was made. They mean different things. For the authoritative version, '
               'go by the revision history in the text.\n\n',
               '> 来源文件：%s\n'
               '> 文档最后修改：%s（来源：%s%s）\n'
               '> 知识库提取时间：%s\n'
               '> 注：「文档最后修改」是文档自身被改动的时间，「提取时间」是本知识库生成的时间，两者含义不同。'
               '业务意义上的权威版本请以正文修订记录为准。\n\n'),
    'unreliable': ('; the document does not record it, so copying may have changed this time',
                   '，文档未记录该属性，此时间可能因拷贝而失真'),
    'log_chars': ('characters: %d', '正文字符数: %d'),
    'log_images': ('images: %d', '图片数: %d'),
    'log_orphans': ('likely orphan images: %d', '疑似孤儿图: %d'),
    'log_source': ('source: %s', '源文件: %s'),
    'log_output': ('output: %s', '输出: %s'),
    # index
    'index_title': ('Knowledge base index', '知识库索引'),
    'src_root': ('> Source folder: %s', '> 源目录：%s'),
    'generated': ('> Generated: %s', '> 生成时间：%s'),
    'doc_count': ('> %d documents. ', '> 共 %d 份文档。'),
    'doc_count_merged': ('This part is the index; the text follows in one section per document.',
                         '本节为索引，正文见下方各章。'),
    'doc_count_index': ('Read this file first to decide which document answers a question.',
                        '本文件供先行阅读，用于判断某个问题应查哪份文档。'),
    'index_mechanical': ('> Summaries and headings are extracted mechanically, not summarised.',
                         '> 摘要与标题为机械提取，非内容概括。'),
    'index_order': ('> Sorted by "Document last modified", newest first.',
                    '> 下表按「文档最后修改」倒序，越靠前的资料越新。'),
    'index_fs_mark': ('> Times marked ※ come from the file system (not recorded in the document) and may '
                      'have changed when the file was copied.',
                      '> 带 ※ 的时间取自文件系统（文档未记录），可能因拷贝而失真。'),
    'index_list': ('## Documents\n', '## 文档一览\n'),
    'index_table_head': ('| Document | Last modified | Original path | Characters | Images |',
                         '| 文档 | 文档最后修改 | 原始路径 | 字符数 | 图片 |'),
    'item_path': ('- Original path: %s', '- 原始路径：%s'),
    'item_modified': ('- Document last modified: %s (from: %s)', '- 文档最后修改：%s（来源：%s）'),
    'item_counts': ('- Characters: %d  Images: %d (orphan images %d)', '- 字符数：%d　图片：%d 张（孤儿图 %d 张）'),
    'item_summary': ('\n**Opening text (first part of the original)**\n', '\n**开头摘要（原文前段）**\n'),
    'item_outline': ('\n**Sections**\n', '\n**章节结构**\n'),
    'item_truncated': ('  - … (too many headings; first %d shown)', '  - …（标题过多，仅列前 %d 条）'),
    'body_mark': ('<!-- Text below: one section per document, newest "Document last modified" first -->\n',
                  '<!-- 以下为正文，按「文档最后修改」倒序，每份文档一节 -->\n'),
    'section_path': ('> Original path: %s\n', '> 原始路径：%s\n'),
    'section_modified': ('> Document last modified: %s (from: %s)\n', '> 文档最后修改：%s（来源：%s）\n'),
    'section_extracted': ('> Extracted: %s\n\n', '> 知识库提取时间：%s\n\n'),
    # file and folder names
    'index_file': ('_kb-index.md', '_知识库索引.md'),
    'image_list_file': ('_image-list.md', '_图片清单.md'),
    'report_file': ('_batch-report.txt', '_批量处理报告.txt'),
    'out_dir_suffix': ('_doc2kb', '_知识库'),
    # image list
    'imglist_title': ('# Image list (all documents)\n\n', '# 图片清单（全部文档）\n\n'),
    'imglist_count': ('%d documents, %d images, %d likely orphan images.\n\n',
                      '共 %d 份文档、%d 张图，其中疑似「孤儿图」%d 张。\n\n'),
    'imglist_note': ('> Orphan image = no text around it describes it; its information exists only in the image.\n'
                     '> This is a keyword heuristic and can be wrong; please check by hand.\n',
                     '> 孤儿图 = 周围文字没有描述它的图，信息只存在于图像中。\n'
                     '> 判断依据是关键词启发式，会有误判，请人工复核。\n'),
    'imglist_not_saved': ('> Note: this run skipped images, so no image files were exported.\n'
                          '> The tables only record where images are; run again without that option to get them.\n',
                          '> 注意：本次运行选择了「跳过图片」，图片文件并未导出。\n'
                          '> 下表仅记录文档中图的位置，需要图时请取消该选项重新运行。\n'),
    'imglist_per_doc': ('## Orphan images per document\n\n', '## 各文档孤儿图统计\n\n'),
    'imglist_doc_head': ('| Document | Images | Orphan images |\n| --- | --- | --- |\n', '| 文档 | 图片 | 孤儿图 |\n| --- | --- | --- |\n'),
    'imglist_dir': ('- Image folder: %s\n\n', '- 图片目录：%s\n\n'),
    'imglist_review': ('**Review first (likely orphan images)**\n\n', '**建议优先人工查看（疑似孤儿图）**\n\n'),
    'imglist_at': ('- %s — in "%s"\n', '- %s — 位于「%s」\n'),
    'imglist_ctx': ('  - context: …%s ▮ %s…\n', '  - 上下文：…%s ▮ %s…\n'),
    'imglist_head': ('| No. | File | Section | Orphan |\n| --- | --- | --- | --- |\n',
                     '| 编号 | 文件 | 所在章节 | 孤儿图 |\n| --- | --- | --- | --- |\n'),
    'yes': ('yes', '是'),
    'no': ('no', '否'),
}

# Report section titles and lines (batch report); kept apart for readability.
LABELS.update({
    'rep_title': ('Batch report\n', '批量处理报告\n'),
    'rep_src': ('Source folder: %s\n', '源目录：%s\n'),
    'rep_out': ('Output folder: %s\n', '输出目录：%s\n'),
    'rep_done': ('Finished: %s\n\n', '完成时间：%s\n\n'),
    'rep_since': ('Date filter: only documents changed on or after %s\n', '日期过滤：只处理 %s（含当天）之后修改的文档\n'),
    'rep_counts': ('New this run %d  kept from earlier runs %d  failed %d  need a new format first %d',
                   '本轮新处理 %d 份　沿用已有 %d 份　失败 %d 份　需先转格式 %d 份'),
    'rep_filtered_n': ('  excluded by date %d', '　因日期排除 %d 份'),
    'rep_dup_n': ('  duplicates left out %d', '　重复略去 %d 份'),
    'rep_moved_n': ('  moved, left out %d', '　位置变动略去 %d 份'),
    'rep_total': ('Documents in the knowledge base: %d\n\n', '知识库现有文档合计 %d 份\n\n'),
    'rep_list': ('Documents in the knowledge base', '知识库文档一览'),
    'rep_list_head': ('Document | Last modified | Characters | Tables | Images | Orphan images | This run\n',
                      '文档 | 文档最后修改 | 字符数 | 表格 | 图片 | 孤儿图 | 本轮\n'),
    'rep_new': ('new', '新处理'),
    'rep_kept': ('kept', '沿用'),
    'rep_sum': ('\nTotal: %d characters, %d images (orphan images %d)\n',
                '\n合计：%d 字符，%d 张图（孤儿图 %d 张）\n'),
    'rep_sum_note': ('(sorted by "Document last modified", newest first; times marked ※ come from the file '
                     'system, not the document, and may have changed when the file was copied)\n\n',
                     '（按文档最后修改倒序排列；带 ※ 的时间取自文件系统，文档本身未记录，可能因拷贝而失真）\n\n'),
    'rep_failed': ('Failed (skipped, other files not affected)', '失败（已跳过，不影响其他文件）'),
    'rep_reason': ('  • %s\n      reason: %s\n', '  • %s\n      原因：%s\n'),
    'rep_skipped': ('Skipped: output already exists (incremental)', '跳过：输出已存在（增量模式）'),
    'rep_skipped_hint': ('\nTo process them again, add --force or delete the md files.\n\n',
                         '\n如需重新处理，请加 --force 或删除对应 md 文件。\n\n'),
    'rep_dups': ('Duplicate files (same text, one kept)', '重复文件（正文内容相同，只保留一份）'),
    'rep_dups_note': ('%d groups, %d left out. Files with identical bytes are parsed once; files whose\n'
                      'extracted text is identical are kept once, the one with the newest document time.\n'
                      'Source files and archives are not changed. To keep every copy, run with -D.\n\n',
                      '共 %d 组，略去 %d 份。字节完全相同的文件只解析一份；「提取出的正文相同」的\n'
                      '只保留文档时间最新的一份。源文件与压缩包未做任何改动。\n'
                      '如需保留全部副本，运行时加 -D 参数。\n\n'),
    'rep_group': ('  group %d:\n', '  第 %d 组：\n'),
    'rep_keep': ('    kept     → %s (%s)\n', '    保留 → %s（%s）\n'),
    'rep_drop': ('    left out   %s (%s)\n', '    略去   %s（%s）\n'),
    'time_unknown': ('time unknown', '时间未知'),
    'rep_moved': ('Moved, content unchanged (text already in the knowledge base)', '内容未变、仅位置变动（正文已在知识库中）'),
    'rep_moved_note': ('%d documents. They were scanned again in this run (usually a folder was moved or renamed),\n'
                       'but their text is identical to what is already in the knowledge base. The text is not\n'
                       'written again; the output index lists old and new paths. Source files are not changed.\n\n',
                       '共 %d 份。这些文档本轮被重新扫描到（通常是目录移动、改名），\n'
                       '但正文与已入库的版本完全相同，正文不再写入，只在产物索引区\n'
                       '列出路径对照。源文件未做任何改动。\n\n'),
    'rep_moved_item': ('  • %s\n      old path: %s (text in %s)\n', '  • %s\n      原路径：%s（正文见 %s）\n'),
    'rep_filtered': ('Excluded by date filter (before %s)\n', '因日期过滤排除（早于 %s）\n'),
    'rep_filtered_note': ('%d documents. Times marked ※ come from the file system (not recorded in the document);\n'
                          'copying refreshes them, so check the original if something was excluded by mistake.\n\n',
                          '共 %d 份。带 ※ 的时间取自文件系统（文档未记录该属性），\n'
                          '拷贝会刷新此时间，如有误伤请核对原文件。\n\n'),
    'rep_filtered_item': ('  • %s\n      modified: %s%s\n', '  • %s\n      修改时间：%s%s\n'),
    'rep_legacy': ('Need a new format first (legacy binary .doc/.xls/.ppt)', '需先转格式（旧版二进制 .doc/.xls/.ppt）'),
    'rep_legacy_hint': ('\nOpen them in an office application, save as .docx / .xlsx / .pptx and run again.\n\n',
                        '\n请用 WPS 打开后另存为 .docx / .xlsx / .pptx，再重新运行。\n\n'),
    'rep_logs': ('Log per document', '各文档处理日志'),
    'rep_warnings': ('\n%d warnings:\n', '\n警告 %d 条：\n'),
    'kept_note': ('kept from an earlier run', '本轮跳过，沿用已有结果'),
    'kept_log': ('skipped in this run (output exists); figures below come from the existing file',
                 '本轮跳过（输出已存在），以下统计来自已有文件'),
    # run_batch / run_files console output
    'no_folder': ('folder not found: %s', '目录不存在: %s'),
    'bad_date': ('date must be YYYY-MM-DD or "YYYY-MM-DD HH:MM": %s', '日期格式应为 YYYY-MM-DD 或 "YYYY-MM-DD HH:MM": %s'),
    'none_by_date': ('no file matches the date filter (%d excluded as older than %s).',
                     '未找到符合日期条件的文件（%d 个因早于 %s 被排除）。'),
    'none_found': ('no file to process.', '未找到可处理的文件。'),
    'skip_exists': ('[%d/%d] skipped (exists): %s', '[%d/%d] 跳过（已存在）: %s'),
    'written_n': ('  wrote %d documents', '  实际写入 %d 份文档'),
    'fpdb_failed': ('cannot write the text fingerprint file: %s', '写入正文指纹库失败: %s'),
    'batch_done': ('Batch finished. Output folder: %s', '批量完成。输出目录: %s'),
    'batch_counts': ('  new this run %d, kept %d, failed %d, need a new format %d',
                     '  本轮新处理 %d 份，沿用已有 %d 份，失败 %d 份，需转格式 %d 份'),
    'excluded_n': ('  excluded as older than %s: %d', '  因早于 %s 被排除 %d 份'),
    'dups_n': ('  duplicates left out %d (%d groups)', '  重复略去 %d 份（%d 组）'),
    'moved_n': ('  moved, left out %d (text already in the knowledge base)', '  位置变动略去 %d 份（正文已在知识库中）'),
    'kb_total': ('  documents in the knowledge base: %d', '  知识库现有 %d 份文档'),
    'totals': ('  %d characters, %d images in total', '  合计 %d 字符，%d 张图片'),
    'report_at': ('  report: %s', '  汇总报告: %s'),
    'merged_at': ('  merged knowledge base: %s', '  合并知识库: %s'),
    'index_at': ('  knowledge base index: %s', '  知识库索引: %s'),
    'file_missing': ('file not found: %s', '文件不存在: %s'),
    'skip_legacy': ('skipped legacy format (save it in a new format first): %s', '跳过旧版格式（请先另存为新格式）: %s'),
    'skip_unsupported': ('skipped unsupported format: %s', '跳过不支持的格式: %s'),
    'none_by_date_files': ('no file matches the date filter (%d older than %s)', '没有符合日期条件的文件（%d 个早于 %s）'),
    'none_files': ('no file to process', '没有可处理的文件'),
    'files_done': ('Done. Output folder: %s', '完成。输出目录: %s'),
    'files_counts': ('  done %d, failed %d', '  成功 %d 份，失败 %d 份'),
    'skip_dup': ('[%d/%d] same file as %s, not parsed again: %s', '[%d/%d] 与 %s 完全相同，不再解析: %s'),
    # archives
    'arc_bad': ('archive damaged or not readable: %s', '压缩包损坏或无法读取：%s'),
    'arc_unsupported': ('%s archives need a tool outside the standard library; extract it and run again',
                        '%s 压缩包需另装工具才能解开，请先解压后再运行'),
    'arc_encrypted': ('encrypted inside the archive, cannot open', '压缩包内文件已加密，无法打开'),
    'arc_too_big': ('larger than %s unpacked, not opened', '解压后超过 %s，未打开'),
    'arc_total': ('the archive holds more than %s of documents; this and later ones not opened',
                  '压缩包内文档合计超过 %s，本文件及之后的未打开'),
    'arc_deep': ('archives nested more than %d deep are not opened', '压缩包嵌套超过 %d 层，未再展开'),
    'arc_missing': ('member not found in the archive', '压缩包内找不到该文件'),
})

# Marks that later steps look for in converted text; both languages are recognised.
ORPHAN_MARKS = ('← 孤儿图', '← orphan image')
GAP_MARKS = ('未能自动解开', 'could not be opened automatically')
SHEET_PREFIXES = ('合并图', 'contact-sheet')
OUT_DIR_SUFFIXES = ('_知识库', '_doc2kb')
IMAGE_REF_RE = re.compile(r'(?:见|see) images/\S+')


def set_lang(lang):
    global LANG
    if lang not in LANGS:
        raise ValueError('language must be one of %s' % ', '.join(LANGS))
    LANG = lang


def T(key, *args):
    """Label in the current language, formatted with args when given."""
    text = LABELS[key][LANGS.index(LANG)]
    return text % args if args else text


def both(key):
    """The label in every language, for parsers that must read either."""
    return LABELS[key]


# Where a "document last modified" time came from. Stored as a code; older sidecar files
# hold the Chinese label, which src_code() maps back.
SRC_CODES = ('doc', 'mail', 'fs', 'newest', 'unknown')
RELIABLE_SRC = ('doc', 'mail')


def src_code(s):
    if s in SRC_CODES:
        return s
    for code in SRC_CODES:
        if s in both('src_' + code if code != 'unknown' else 'unknown'):
            return code
    return 'unknown' if not s else s


def src_label(s):
    code = src_code(s)
    if code == 'unknown':
        return T('unknown')
    if code in SRC_CODES:
        return T('src_' + code)
    return s


# Processing status, stored as a code and shown through T().
OK, FAILED, SKIPPED = 'ok', 'failed', 'skipped'

# ---------------------------------------------------------------- namespaces

NS = {
    'w':   'http://schemas.openxmlformats.org/wordprocessingml/2006/main',
    'r':   'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
    'a':   'http://schemas.openxmlformats.org/drawingml/2006/main',
    'p':   'http://schemas.openxmlformats.org/presentationml/2006/main',
    'pr':  'http://schemas.openxmlformats.org/package/2006/relationships',
    's':   'http://schemas.openxmlformats.org/spreadsheetml/2006/main',
    'wp':  'http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing',
    'v':   'urn:schemas-microsoft-com:vml',
    'o':   'urn:schemas-microsoft-com:office:office',
}


def q(ns, tag):
    """Tag name with its namespace."""
    return '{%s}%s' % (NS[ns], tag)


# ---------------------------------------------------------------- helpers

MC_FALLBACK = '{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback'


def iter_skip_fallback(el):
    """Walk descendants in document order, skipping mc:Fallback subtrees.

    Text boxes and shapes keep two copies inside mc:AlternateContent, the new format
    (Choice) and a compatible one (Fallback); walking both doubles text and images.
    """
    stack = [el]
    while stack:
        node = stack.pop()
        if node.tag == MC_FALLBACK:
            continue
        yield node
        stack.extend(reversed(list(node)))


def resolve_part(base_dir, target):
    """Resolve a relationship Target to a path inside the package.

    Both forms occur: absolute (/xl/worksheets/sheet1.xml, common in files written by
    libraries) and relative (worksheets/sheet1.xml, ../media/image1.png).
    """
    target = (target or '').replace('\\', '/')
    if target.startswith('/'):
        return target.lstrip('/')
    return posixpath.normpath(posixpath.join(base_dir, target))


def block_children(el):
    """Block-level children of a Word element, unwrapping content controls (w:sdt) and w:customXml.

    Cover pages, tables of contents and form templates wrap paragraphs and tables in
    content controls; without unwrapping, whole blocks are lost.
    """
    out = []
    for ch in el:
        if ch.tag == q('w', 'sdt'):
            content = ch.find(q('w', 'sdtContent'))
            if content is not None:
                out.extend(block_children(content))
        elif ch.tag == q('w', 'customXml'):
            out.extend(block_children(ch))
        else:
            out.append(ch)
    return out


class Report:
    """Collects log lines and warnings while a document is processed."""
    def __init__(self):
        self.lines = []
        self.warnings = []

    def log(self, msg):
        self.lines.append(msg)
        log.debug('%s', msg)

    def warn(self, msg):
        self.warnings.append(msg)
        self.lines.append(T('warning_prefix') + msg)
        log.debug('warning: %s', msg)


def fs_text(s):
    """Make a string from the file system safe to write out.

    Chinese file names copied from Windows are often GBK; on Linux, Python represents
    those non-UTF-8 bytes as surrogates, and writing such a string to a UTF-8 file raises
    "surrogates not allowed".

    Order: return the string if it encodes; otherwise take back the raw bytes and try the
    common Chinese encodings; fall back to replacement characters.
    Only for display and output: open files with the original string.
    """
    if not isinstance(s, str):
        return s
    try:
        s.encode('utf-8')
        return s
    except UnicodeEncodeError:
        pass  # not valid UTF-8: repaired below

    # A path may mix a UTF-8 folder name with a GBK file name; decoding the whole string
    # with one encoding garbles it, so each component is repaired on its own.
    if '/' in s or os.sep in s:
        sep = '/' if '/' in s else os.sep
        return sep.join(fs_text(part) for part in s.split(sep))

    raw = s.encode('utf-8', 'surrogateescape')
    for enc in ('gbk', 'gb18030', 'big5', 'utf-8'):
        try:
            out = raw.decode(enc)
            out.encode('utf-8')
            return out
        except (UnicodeDecodeError, UnicodeEncodeError):
            continue
    return s.encode('utf-8', 'replace').decode('utf-8', 'replace')


def safe_name(s, maxlen=60):
    """Any string as a safe file name."""
    s = fs_text(s)
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', s)
    s = s.strip('. ')
    return s[:maxlen] or 'untitled'


def safe_component(s):
    """A path component with spaces and brackets turned into underscores, safe in scripts and links."""
    s = safe_name(s)
    s = re.sub(r'[\s()（）\[\]{}]+', '_', s)
    return s.strip('_') or 'x'


def col_to_index(col):
    """Excel column 'AB' -> 28 (1-based)."""
    n = 0
    for ch in col:
        n = n * 26 + (ord(ch) - ord('A') + 1)
    return n


def split_cell_ref(ref):
    """'AB12' -> ('AB', 12)"""
    m = re.match(r'([A-Z]+)(\d+)', ref)
    if not m:
        return None, None
    return m.group(1), int(m.group(2))


# ================================================================ XLSX

# Built-in number formats that show dates or times (including the CJK ones 27-36, 50-58)
XLSX_BUILTIN_DATE_KIND = {}
for _i in list(range(14, 18)) + list(range(27, 32)) + [36] + list(range(50, 55)) + [57, 58]:
    XLSX_BUILTIN_DATE_KIND[_i] = 'date'
for _i in [18, 19, 20, 21, 32, 33, 34, 35, 45, 47, 55, 56]:
    XLSX_BUILTIN_DATE_KIND[_i] = 'time'
XLSX_BUILTIN_DATE_KIND[22] = 'datetime'


def classify_numfmt(code):
    """Is a custom number format a date or time? Returns 'date' / 'time' / 'datetime' / None.

    Quoted text, escaped characters, brackets (locale, colour, elapsed time such as [h])
    and placeholders are removed first; then year, month, day, hour or second codes decide.
    Elapsed durations are not dates.
    """
    if re.search(r'\[(h+|m+|s+)\]', (code or '').lower()):
        return None     # [h]:mm and the like can exceed 24 hours; keep the raw number
    s = re.sub(r'"[^"]*"|\\.|\[[^\]]*\]|_.|\*.', '', code or '')
    s = s.split(';')[0].lower()
    has_date = 'y' in s or 'd' in s
    has_time = 'h' in s or 's' in s
    if has_date and has_time:
        return 'datetime'
    if has_date:
        return 'date'
    if has_time:
        return 'time'
    return None


def load_date_styles(zf, names, report, source_name=''):
    """Read xl/styles.xml; returns {cell style index (string): date kind}, date and time formats only."""
    if 'xl/styles.xml' not in names:
        return {}
    try:
        root = ET.fromstring(zf.read('xl/styles.xml'))
    except Exception as e:
        report.warn(T('xlsx_styles_failed', source_name, e))
        return {}
    custom = {}
    nfs = root.find(q('s', 'numFmts'))
    if nfs is not None:
        for nf in nfs.findall(q('s', 'numFmt')):
            try:
                custom[int(nf.get('numFmtId'))] = nf.get('formatCode') or ''
            except (TypeError, ValueError):
                pass  # a malformed number format id; the cell keeps its raw value
    out = {}
    xfs = root.find(q('s', 'cellXfs'))
    if xfs is None:
        return out
    for i, xf in enumerate(xfs.findall(q('s', 'xf'))):
        try:
            fid = int(xf.get('numFmtId') or 0)
        except ValueError:
            continue
        kind = classify_numfmt(custom[fid]) if fid in custom else XLSX_BUILTIN_DATE_KIND.get(fid)
        if kind:
            out[str(i)] = kind
    return out


def excel_serial_to_text(val, kind, date1904=False):
    """Turn the serial number in a date-formatted cell into readable text; unchanged if it cannot."""
    try:
        x = float(val)
    except (TypeError, ValueError):
        return val
    if x < 0:
        return val
    if date1904:
        base = datetime.datetime(1904, 1, 1)
    else:
        base = datetime.datetime(1899, 12, 30)
        if x < 60:
            x += 1      # the 1900 system treats 1900 as a leap year; serials before 60 need a day added
    try:
        dt = base + datetime.timedelta(days=x)
    except OverflowError:
        return val
    dt = (dt + datetime.timedelta(microseconds=500000)).replace(microsecond=0)
    hm = '%H:%M:%S' if dt.second else '%H:%M'
    if kind == 'date':
        return dt.strftime('%Y-%m-%d')
    if kind == 'time':
        return dt.strftime(hm)
    return dt.strftime('%Y-%m-%d ' + hm)


def parse_xlsx(data, report, source_name='', max_rows=2000):
    """Parse an xlsx (bytes or a path); returns {sheet name: [[cell, ...], ...]}."""
    result = {}
    try:
        if isinstance(data, bytes):
            zf = zipfile.ZipFile(io.BytesIO(data))
        else:
            zf = zipfile.ZipFile(data)
    except Exception as e:
        report.warn(T('xlsx_open_failed', source_name, e))
        return result

    with zf:
        names = zf.namelist()

        # --- sharedStrings ---
        shared = []
        if 'xl/sharedStrings.xml' in names:
            try:
                root = ET.fromstring(zf.read('xl/sharedStrings.xml'))
                for si in root.findall(q('s', 'si')):
                    # one si may be split into several r/t runs
                    parts = [t.text or '' for t in si.iter(q('s', 't'))]
                    shared.append(''.join(parts))
            except Exception as e:
                report.warn(T('xlsx_shared_failed', source_name, e))

        # --- workbook: sheet name -> part ---
        sheet_map = []   # [(name, target path)]
        date1904 = False
        try:
            wb = ET.fromstring(zf.read('xl/workbook.xml'))
            wbpr = wb.find(q('s', 'workbookPr'))
            if wbpr is not None and (wbpr.get('date1904') or '').lower() in ('1', 'true'):
                date1904 = True
            rels_root = ET.fromstring(zf.read('xl/_rels/workbook.xml.rels'))
            rid2target = {}
            for rel in rels_root:
                rid2target[rel.get('Id')] = rel.get('Target')

            for sh in wb.iter(q('s', 'sheet')):
                name = sh.get('name')
                rid = sh.get('{%s}id' % NS['r'])
                target = rid2target.get(rid, '')
                if target:
                    target = resolve_part('xl', target)
                sheet_map.append((name, target))
        except Exception as e:
            report.warn(T('xlsx_workbook_failed', source_name, e))
            # fall back to the worksheets folder
            for n in names:
                if n.startswith('xl/worksheets/sheet') and n.endswith('.xml'):
                    sheet_map.append((os.path.basename(n)[:-4], n))

        date_styles = load_date_styles(zf, names, report, source_name)

        # --- cells, sheet by sheet ---
        for sname, spath in sheet_map:
            if spath not in names:
                report.warn(T('xlsx_sheet_missing', sname, spath or T('no_path')))
                continue
            try:
                sroot = ET.fromstring(zf.read(spath))
            except Exception as e:
                report.warn(T('xlsx_sheet_failed', sname, e))
                continue

            rows = []
            truncated = False
            n_empty = 0
            for row in sroot.iter(q('s', 'row')):
                if len(rows) >= max_rows:
                    truncated = True
                    break
                cells = {}
                for c in row.findall(q('s', 'c')):
                    ref = c.get('r') or ''
                    col, _ = split_cell_ref(ref)
                    if col is None:
                        continue
                    ctype = c.get('t')
                    v = c.find(q('s', 'v'))
                    is_node = c.find(q('s', 'is'))

                    if ctype == 's' and v is not None:
                        try:
                            text = shared[int(v.text)]
                        except (ValueError, IndexError):
                            text = ''
                    elif ctype == 'inlineStr' and is_node is not None:
                        text = ''.join(t.text or '' for t in is_node.iter(q('s', 't')))
                    elif v is not None:
                        text = v.text or ''
                        kind = date_styles.get(c.get('s') or '0')
                        if kind and ctype in (None, 'n'):
                            text = excel_serial_to_text(text, kind, date1904)
                    else:
                        text = ''

                    cells[col_to_index(col)] = text.strip()

                if cells and any(v.strip() for v in cells.values()):
                    width = max(cells)
                    rows.append([cells.get(i + 1, '') for i in range(width)])
                else:
                    n_empty += 1

            if n_empty > 50:
                report.log(T('xlsx_blank_rows', sname, n_empty))

            # drop trailing columns that are empty in every row
            if rows:
                used = max(
                    (max((i for i, x in enumerate(r) if x.strip()), default=-1)
                     for r in rows)
                ) + 1
                if used > 0:
                    rows = [r[:used] for r in rows]

            if truncated:
                report.warn(T('xlsx_truncated', source_name, sname, max_rows))
                rows.append([T('xlsx_truncated_row', max_rows)])

            if rows:
                result[sname] = rows

    return result


def rows_to_markdown(rows):
    """Two-dimensional list as a Markdown table; the first row is the header."""
    if not rows:
        return T('empty_table')

    width = max(len(r) for r in rows)
    rows = [r + [''] * (width - len(r)) for r in rows]

    def esc(c):
        return c.replace('|', '\\|').replace('\n', ' ').strip()

    out = []
    header = rows[0]
    # an all-empty header gets generated column names
    if not any(h.strip() for h in header):
        header = [T('column_n', i + 1) for i in range(width)]
        body = rows
    else:
        body = rows[1:]

    out.append('| ' + ' | '.join(esc(c) or ' ' for c in header) + ' |')
    out.append('| ' + ' | '.join(['---'] * width) + ' |')
    for r in body:
        out.append('| ' + ' | '.join(esc(c) or ' ' for c in r) + ' |')
    return '\n'.join(out) + '\n'


# ================================================================ DOCX

def load_style_map(zf, report):
    """styleId -> heading level (1-6) from styles.xml; styles that are not headings are left out."""
    smap = {}
    if 'word/styles.xml' not in zf.namelist():
        return smap
    try:
        root = ET.fromstring(zf.read('word/styles.xml'))
    except Exception as e:
        report.warn(T('docx_styles_failed', e))
        return smap

    for st in root.findall(q('w', 'style')):
        sid = st.get(q('w', 'styleId')) or ''
        name_el = st.find(q('w', 'name'))
        name = name_el.get(q('w', 'val')) if name_el is not None else ''

        candidates = [sid, name]
        level = None
        for c in candidates:
            if not c:
                continue
            c_low = c.lower().replace(' ', '')
            # Heading1 / heading1 / 标题1 (Chinese Word) / 1
            m = re.match(r'^(heading|标题)(\d)$', c_low)
            if m:
                level = int(m.group(2))
                break
            if re.match(r'^\d$', c_low) and sid == c_low:
                # Chinese Word often uses styleId "1", "2"
                level = int(c_low)
                break
        if level and 1 <= level <= 6:
            smap[sid] = level

    return smap


def para_text(p):
    """Plain text of a w:p (one copy of text boxes and other duplicated content)."""
    parts = []
    for node in iter_skip_fallback(p):
        if node.tag == q('w', 't'):
            parts.append(node.text or '')
        elif node.tag == q('w', 'tab'):
            parts.append('\t')
        elif node.tag == q('w', 'br'):
            parts.append('\n')
    return ''.join(parts)


def para_heading_level(p, style_map):
    """Heading level of a paragraph, or None."""
    ppr = p.find(q('w', 'pPr'))
    if ppr is None:
        return None
    pstyle = ppr.find(q('w', 'pStyle'))
    if pstyle is None:
        return None
    sid = pstyle.get(q('w', 'val')) or ''
    if sid in style_map:
        return style_map[sid]
    # fallback: a styleId named Heading2 and the like
    m = re.match(r'^(?:heading|标题)(\d)$', sid.lower().replace(' ', ''))
    if m:
        lv = int(m.group(1))
        return lv if 1 <= lv <= 6 else None
    return None


def para_images(p):
    """rIds of the pictures in a paragraph, both DrawingML and VML."""
    rids = []
    blip_tag = q('a', 'blip')                    # DrawingML (current)
    vml_tag = '{%s}imagedata' % NS['v']          # VML (older, common in WPS files)
    for node in iter_skip_fallback(p):
        if node.tag == blip_tag:
            rid = node.get('{%s}embed' % NS['r'])
        elif node.tag == vml_tag:
            rid = node.get('{%s}id' % NS['r'])
        else:
            continue
        if rid:
            rids.append(rid)
    return rids


def para_ole_objects(p):
    """rIds of the OLE objects (embedded workbooks and the like) in a paragraph."""
    rids = []
    ole_tag = '{%s}OLEObject' % NS['o']
    for node in iter_skip_fallback(p):
        if node.tag == ole_tag:
            rid = node.get('{%s}id' % NS['r'])
            if rid:
                rids.append(rid)
    return rids


def _w_int(el, tag, default=0):
    """Integer w:val of the w:tag child of el."""
    if el is None:
        return default
    node = el.find(q('w', tag))
    if node is None:
        return default
    try:
        return int(node.get(q('w', 'val')))
    except (TypeError, ValueError):
        return default


def cell_text(tc):
    """Text of a table cell. A nested table is flattened: cells joined with " / ", rows with "；"."""
    texts = []
    for el in block_children(tc):
        if el.tag == q('w', 'p'):
            t = para_text(el).strip()
            if t:
                texts.append(t)
        elif el.tag == q('w', 'tbl'):
            lines = [' / '.join(c for c in r if c) for r in table_to_rows(el)]
            nested = '；'.join(x for x in lines if x)
            if nested:
                texts.append(nested)
    return ' '.join(texts)


def table_to_rows(tbl):
    """w:tbl -> two-dimensional list.

    A horizontally merged cell (gridSpan) is followed by empty cells and leading gaps
    (gridBefore) become empty cells, so later cells stay in their columns; rows and cells
    wrapped in content controls are unwrapped.
    """
    rows = []
    for tr in block_children(tbl):
        if tr.tag != q('w', 'tr'):
            continue
        cells = [''] * _w_int(tr.find(q('w', 'trPr')), 'gridBefore')
        for tc in block_children(tr):
            if tc.tag != q('w', 'tc'):
                continue
            cells.append(cell_text(tc))
            span = _w_int(tc.find(q('w', 'tcPr')), 'gridSpan', 1)
            cells.extend([''] * max(0, span - 1))
        if cells:
            rows.append(cells)
    return rows


# Words near a picture that show the text refers to it. The document's language decides,
# not the output language, so both lists always apply.
IMG_HINT_WORDS = [
    '如图', '下图', '上图', '如下图', '见图', '界面示意', '页面示例',
    '示意图', '图中', '如下所示', '界面如下', '页面如下', '流程如下',
    '菜单及流程页面', '具体功能界面', '页面示例如下', '入口如下图',
    '展示如下', '如下面', '图片中',
    'shown below', 'shown above', 'as shown', 'see figure', 'see the figure', 'screenshot',
    'the figure below', 'the image below', 'the diagram below', 'as follows:',
]


def looks_described(before_text, after_text):
    """Heuristic: does the text around a picture describe it?
    before/after are a few hundred characters of context."""
    ctx = (before_text[-200:] + ' ' + after_text[:300])
    low = ctx.lower()
    for w in IMG_HINT_WORDS:
        if w in ctx or w in low:
            return True
    return False


def parse_docx_embedded(raw, report, source_name=''):
    """Parse a docx embedded in the main document (a field description, say).
    Text and tables only; no pictures and no nested objects. Returns Markdown, '' on failure."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except Exception as e:
        report.warn(T('emb_docx_open_failed', source_name, e))
        return ''

    md = []
    with zf:
        if 'word/document.xml' not in zf.namelist():
            report.warn(T('emb_no_document', source_name))
            return ''
        style_map = load_style_map(zf, report)
        try:
            doc = ET.fromstring(zf.read('word/document.xml'))
        except Exception as e:
            report.warn(T('emb_docx_failed', source_name, e))
            return ''
        body = doc.find(q('w', 'body'))
        if body is None:
            return ''

        n_imgs = 0
        for el in block_children(body):
            if el.tag == q('w', 'p'):
                text = para_text(el).strip()
                lv = para_heading_level(el, style_map)
                n_imgs += len(para_images(el))
                if lv and text:
                    # headings of an embedded document become bold text, keeping the main outline clean
                    md.append('\n**%s**\n' % text)
                elif text:
                    md.append(text + '\n')
            elif el.tag == q('w', 'tbl'):
                rows = table_to_rows(el)
                if rows:
                    md.append('\n' + rows_to_markdown(rows))

        if n_imgs:
            md.append(T('emb_docx_images', n_imgs))

    return '\n'.join(md)


def image_location(img_ref, fname, save_images):
    return T('image_at', img_ref, fname) if save_images else T('image_not_saved')


def parse_docx(path, outdir, report, inline_excel=True,
               imgdir=None, img_ref='images', save_images=True):
    """Parse a docx; returns (Markdown, image manifest).

    imgdir  : where pictures are written, default outdir/images
    img_ref : path prefix used in the anchors in the text
    """
    md = []
    img_manifest = []

    if imgdir is None:
        imgdir = os.path.join(outdir, 'images')
    if save_images:
        os.makedirs(imgdir, exist_ok=True)

    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()

        if 'word/document.xml' not in names:
            report.warn(T('docx_invalid'))
            return '', []

        style_map = load_style_map(zf, report)
        report.log(T('docx_style_map', len(style_map)))
        if not style_map:
            report.warn(T('docx_no_styles'))

        # --- rels: rId -> target ---
        rid2target = {}
        if 'word/_rels/document.xml.rels' in names:
            rroot = ET.fromstring(zf.read('word/_rels/document.xml.rels'))
            for rel in rroot:
                rid2target[rel.get('Id')] = rel.get('Target')

        doc = ET.fromstring(zf.read('word/document.xml'))
        body = doc.find(q('w', 'body'))
        if body is None:
            report.warn(T('docx_no_body'))
            return '', []

        children = block_children(body)
        # text of every block first, for the context checks around pictures
        flat_text = []
        for el in children:
            if el.tag == q('w', 'p'):
                flat_text.append(para_text(el))
            elif el.tag == q('w', 'tbl'):
                flat_text.append(T('table_marker'))
            else:
                flat_text.append('')

        img_counter = 0
        cur_section = T('doc_start')
        embedded = []   # (rId, target, block index)

        for idx, el in enumerate(children):

            # ---------- paragraph ----------
            if el.tag == q('w', 'p'):
                text = para_text(el).strip()
                lv = para_heading_level(el, style_map)
                imgs = para_images(el)
                oles = para_ole_objects(el)

                if lv and text:
                    md.append('\n' + '#' * lv + ' ' + text + '\n')
                    cur_section = text
                    continue

                # OLE objects (workbooks and the like)
                for rid in oles:
                    target = rid2target.get(rid, '')
                    if target:
                        embedded.append((rid, target, idx))

                # pictures
                for rid in imgs:
                    target = rid2target.get(rid, '')
                    if not target:
                        continue
                    # relative to word/ as the standard says; a few packages write the word/ prefix
                    src = next((c for c in (resolve_part('word', target), target)
                                if c in names), None)
                    if not src:
                        continue

                    img_counter += 1
                    ext = os.path.splitext(src)[1] or '.png'
                    fname = 'img_%03d%s' % (img_counter, ext)
                    if save_images:
                        with open(os.path.join(imgdir, fname), 'wb') as f:
                            f.write(zf.read(src))

                    before = ''.join(flat_text[max(0, idx - 3):idx])
                    after = ''.join(flat_text[idx + 1:idx + 4])
                    described = looks_described(before, after)

                    tag = '' if described else T('orphan_tag')
                    loc = image_location(img_ref, fname, save_images)
                    md.append(T('docx_anchor', img_counter, cur_section, loc, tag))

                    img_manifest.append({
                        'no': img_counter,
                        'file': fname,
                        'section': cur_section,
                        'orphan': not described,
                        'ctx_before': before[-80:].replace('\n', ' '),
                        'ctx_after': after[:80].replace('\n', ' '),
                    })

                if text:
                    md.append(text + '\n')

            # ---------- table ----------
            elif el.tag == q('w', 'tbl'):
                rows = table_to_rows(el)
                if rows:
                    md.append('\n' + rows_to_markdown(rows))

        # --- inline embedded objects (workbooks, Word attachments) ---
        if inline_excel and embedded:
            report.log(T('emb_found', len(embedded)))
            seen_hash = {}   # content hash -> name of the first attachment with it
            for rid, target, idx in embedded:
                src = resolve_part('word', target)
                if src not in names:
                    report.warn(T('emb_missing', rid, target))
                    continue

                raw = zf.read(src)
                base_t = os.path.basename(target)
                low = base_t.lower()

                # the same attachment referenced in several places is expanded once
                h = hashlib.md5(raw).hexdigest()
                if h in seen_hash:
                    md.append(T('emb_same', base_t, seen_hash[h]))
                    report.log(T('emb_dup_bytes', base_t, seen_hash[h]))
                    continue
                seen_hash[h] = base_t

                # real type: magic number first, then the extension
                is_zip = raw[:2] == b'PK'
                is_cfb = raw[:8] == b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'

                if is_cfb or (not is_zip and (low.endswith('.bin')
                                              or low.endswith('.xls')
                                              or low.endswith('.doc'))):
                    kind = T('legacy_kind')
                    report.warn(T('emb_legacy_warn', base_t, kind))
                    md.append(T('emb_gap', base_t, kind))
                    continue

                if not is_zip:
                    report.warn(T('emb_unknown', base_t))
                    continue

                # --- a zip package: a workbook or a Word document ---
                try:
                    inner_names = zipfile.ZipFile(io.BytesIO(raw)).namelist()
                except Exception:
                    inner_names = []

                if any(n.startswith('word/') for n in inner_names):
                    sub_md = parse_docx_embedded(raw, report, source_name=base_t)
                    if sub_md.strip():
                        h2 = hashlib.md5(sub_md.encode('utf-8')).hexdigest()
                        if h2 in seen_hash:
                            md.append(T('emb_same_word', base_t, seen_hash[h2]))
                            report.log(T('emb_dup_text', base_t, seen_hash[h2]))
                            continue
                        seen_hash[h2] = base_t
                        md.append(T('inline_word', base_t))
                        md.append(sub_md)
                        md.append('\n---\n')
                        report.log(T('inlined_word', base_t, len(sub_md)))
                    continue

                # otherwise a workbook
                sheets = parse_xlsx(raw, report, source_name=base_t)
                if not sheets:
                    continue

                block = []
                for sname, rows in sheets.items():
                    block.append(T('sheet_caption', sname, len(rows)))
                    block.append(rows_to_markdown(rows))
                content_text = '\n'.join(block)

                h2 = hashlib.md5(content_text.encode('utf-8')).hexdigest()
                if h2 in seen_hash:
                    md.append(T('emb_same', base_t, seen_hash[h2]))
                    report.log(T('emb_dup_text', base_t, seen_hash[h2]))
                    continue
                seen_hash[h2] = base_t

                md.append(T('inline_book', base_t))
                md.append(content_text)
                md.append('\n---\n')
                report.log(T('inlined_book', base_t, len(sheets), len(content_text)))

    return '\n'.join(md), img_manifest


# ================================================================ PPTX

def parse_pptx(path, outdir, report, imgdir=None, img_ref='images',
               save_images=True):
    """Parse a pptx: the text and the pictures of every slide."""
    md = []
    img_manifest = []
    if imgdir is None:
        imgdir = os.path.join(outdir, 'images')
    if save_images:
        os.makedirs(imgdir, exist_ok=True)

    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        slides = sorted(
            [n for n in names if re.match(r'ppt/slides/slide\d+\.xml$', n)],
            key=lambda x: int(re.search(r'(\d+)', os.path.basename(x)).group(1))
        )
        if not slides:
            report.warn(T('pptx_no_slides'))
            return '', []

        report.log(T('pptx_count', len(slides)))
        img_counter = 0
        shape_only_pages = []

        for spath in slides:
            n = int(re.search(r'(\d+)', os.path.basename(spath)).group(1))
            root = ET.fromstring(zf.read(spath))

            relpath = 'ppt/slides/_rels/%s.rels' % os.path.basename(spath)
            rid2target = {}
            if relpath in names:
                rroot = ET.fromstring(zf.read(relpath))
                for rel in rroot:
                    rid2target[rel.get('Id')] = rel.get('Target')

            texts = []
            for para in root.iter(q('a', 'p')):
                line = ''.join(t.text or '' for t in para.iter(q('a', 't')))
                if line.strip():
                    texts.append(line.strip())

            md.append(T('slide_heading', n))
            if texts:
                for t in texts:
                    md.append('- ' + t)
            else:
                md.append(T('slide_no_text'))

            page_imgs = 0
            for blip in root.iter(q('a', 'blip')):
                rid = blip.get('{%s}embed' % NS['r'])
                target = rid2target.get(rid, '')
                if not target:
                    continue
                src = resolve_part('ppt/slides', target)
                if src not in names:
                    continue

                img_counter += 1
                page_imgs += 1
                ext = os.path.splitext(src)[1] or '.png'
                fname = 'slide%02d_img%02d%s' % (n, page_imgs, ext)
                if save_images:
                    with open(os.path.join(imgdir, fname), 'wb') as f:
                        f.write(zf.read(src))

                md.append(T('slide_anchor', n, image_location(img_ref, fname, save_images)))
                img_manifest.append({
                    'no': img_counter,
                    'file': fname,
                    'section': T('slide_section', n),
                    'orphan': len(''.join(texts)) < 30,
                    'ctx_before': '',
                    'ctx_after': ' '.join(texts)[:80],
                })

            # shapes but no bitmap on the slide: probably a chart drawn with shapes
            total_chars = len(''.join(texts))
            has_shapes = any(True for _ in root.iter(q('p', 'sp')))
            if page_imgs == 0 and has_shapes and total_chars < 200:
                shape_only_pages.append(n)

        if shape_only_pages:
            report.warn(T('pptx_shapes', T('list_sep').join(str(x) for x in shape_only_pages)))

    return '\n'.join(md), img_manifest


# ================================================================ XLSX file

def parse_xlsx_file(path, outdir, report, imgdir=None, img_ref='images',
                    save_images=True, display_name=None):
    sheets = parse_xlsx(path, report, source_name=os.path.basename(path))
    if not sheets:
        return '', []
    md = ['# ' + (display_name or
                  os.path.splitext(os.path.basename(path))[0]) + '\n']
    for sname, rows in sheets.items():
        md.append(T('sheet_heading', sname))
        md.append(T('sheet_rows', len(rows)))
        md.append(rows_to_markdown(rows))
    report.log(T('xlsx_count', len(sheets)))
    return '\n'.join(md), []


# ================================================================ PDF

def have(cmd):
    return shutil.which(cmd) is not None


def pdf_pages(path):
    """Number of pages from pdfinfo, or 0 when unknown."""
    if not have('pdfinfo'):
        return 0
    try:
        out = subprocess.run(['pdfinfo', path], capture_output=True,
                             timeout=30).stdout.decode('utf-8', 'replace')
    except (OSError, subprocess.SubprocessError):
        return 0
    m = re.search(r'^Pages:\s+(\d+)', out, re.M)
    return int(m.group(1)) if m else 0


def parse_pdf(path, outdir, report, imgdir=None, img_ref='images',
              save_images=True, display_name=None):
    if not have('pdftotext'):
        report.warn(T('pdf_no_tool'))
        return '', []

    if imgdir is None:
        imgdir = os.path.join(outdir, 'images')
    if save_images:
        os.makedirs(imgdir, exist_ok=True)

    txt = subprocess.run(
        ['pdftotext', '-layout', path, '-'],
        capture_output=True
    ).stdout.decode('utf-8', 'replace')

    if len(txt.strip()) < 50:
        report.warn(T('pdf_no_text'))

    img_manifest = []
    if save_images and have('pdfimages'):
        prefix = os.path.join(imgdir, 'pdfimg')
        subprocess.run(['pdfimages', '-png', '-p', path, prefix],
                       capture_output=True)
        n = 0
        for f in sorted(os.listdir(imgdir)):
            if not f.startswith('pdfimg'):
                continue
            fp = os.path.join(imgdir, f)
            if os.path.getsize(fp) < 8000:
                os.remove(fp)
                continue
            n += 1
            img_manifest.append({
                'no': n, 'file': f, 'section': T('pdf_section'),
                'orphan': True, 'ctx_before': '', 'ctx_after': '',
            })
        report.log(T('pdf_images', n))
        report.warn(T('pdf_images_warn'))

    md = '# ' + (display_name or
                 os.path.splitext(os.path.basename(path))[0]) + '\n\n' + txt
    return md, img_manifest


# ================================================================ contact sheets

def make_contact_sheets(manifest, outdir, report, tile=4, target_long=4000,
                        imgdir=None, sheetdir=None, prefix=''):
    """Tile pictures tile x tile into a few sheets, each tile labelled with its number
    (the same numbers as the image list). Needs ImageMagick's montage; skipped without it."""
    if not manifest:
        return []
    if not have('montage'):
        report.warn(T('montage_missing'))
        return []

    if imgdir is None:
        imgdir = os.path.join(outdir, 'images')
    if sheetdir is None:
        sheetdir = os.path.join(outdir, T('sheet_dir'))
    os.makedirs(sheetdir, exist_ok=True)

    per_sheet = tile * tile
    cell = target_long // tile          # tile edge length
    items = sorted(manifest, key=lambda m: m['no'])
    n_sheets = (len(items) + per_sheet - 1) // per_sheet

    made = []
    for si in range(n_sheets):
        batch = items[si * per_sheet:(si + 1) * per_sheet]
        out_png = os.path.join(sheetdir, T('sheet_file', prefix, si + 1))

        cmd = ['montage']
        for m in batch:
            fp = os.path.join(imgdir, m['file'])
            if not os.path.isfile(fp):
                continue
            cmd += ['-label', T('sheet_label', m['no']), fp]
        cmd += [
            '-tile', '%dx%d' % (tile, tile),
            '-geometry', '%dx%d>+6+6' % (cell, cell),
            '-background', 'white',
            '-fill', 'black',
            '-pointsize', '28',
            out_png,
        ]
        r = subprocess.run(cmd, capture_output=True)
        if r.returncode == 0 and os.path.isfile(out_png):
            made.append(out_png)
        else:
            report.warn(T('sheet_failed', si + 1, r.stderr.decode('utf-8', 'replace')[:200]))

    if made:
        report.log(T('sheet_done', len(made), per_sheet))
        report.log(T('sheet_hint'))
    return made


# ================================================================ EML

def _decode_mime_header(raw):
    """Decode MIME encoded words (=?utf-8?B?...?= and the like)."""
    if not raw:
        return ''
    try:
        from email.header import decode_header, make_header
        return str(make_header(decode_header(raw)))
    except Exception:
        return raw


def _html_to_text(html):
    """HTML body as plain text: drop scripts and styles, block tags become line breaks, decode entities."""
    if not html:
        return ''
    t = re.sub(r'(?is)<(script|style)[^>]*>.*?</\1>', '', html)
    t = re.sub(r'(?i)<br\s*/?>', '\n', t)
    t = re.sub(r'(?i)</(p|div|tr|li|h[1-6]|table)\s*>', '\n', t)
    t = re.sub(r'(?i)</t[dh]\s*>', '\t', t)
    t = re.sub(r'(?s)<[^>]+>', '', t)
    try:
        import html as _h
        t = _h.unescape(t)
    except Exception:
        pass  # entities stay as they are
    t = re.sub(r'[ \t]+\n', '\n', t)
    t = re.sub(r'\n{3,}', '\n\n', t)
    return t.strip()


def _part_text(part):
    """Decoded text of a MIME part, tolerant of a wrong charset declaration."""
    try:
        payload = part.get_payload(decode=True)
    except Exception:
        return ''
    if payload is None:
        return ''
    charset = part.get_content_charset()
    for cs in [charset, 'utf-8', 'gb18030', 'gbk', 'big5', 'latin-1']:
        if not cs:
            continue
        try:
            return payload.decode(cs)
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode('utf-8', 'replace')


def _image_size(data):
    """Width and height from PNG, JPEG or GIF bytes; (0, 0) when unknown."""
    try:
        if data[:8] == b'\x89PNG\r\n\x1a\n' and len(data) > 24:
            w, h = struct.unpack('>II', data[16:24])
            return w, h
        if data[:3] == b'\xff\xd8\xff':
            i = 2
            while i < len(data) - 9:
                if data[i] != 0xFF:
                    i += 1
                    continue
                mk = data[i + 1]
                if mk in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack('>HH', data[i + 5:i + 9])
                    return w, h
                seg = struct.unpack('>H', data[i + 2:i + 4])[0]
                i += 2 + seg
        if data[:6] in (b'GIF87a', b'GIF89a') and len(data) > 10:
            w, h = struct.unpack('<HH', data[6:10])
            return w, h
    except Exception:
        pass  # truncated or odd header: size unknown
    return 0, 0


def parse_eml(path, outdir, report, inline_excel=True,
              imgdir=None, img_ref='images', save_images=True):
    """Parse an .eml mail:
      - header fields (from, to, cc, date, subject)
      - body (plain text preferred; an HTML-only body becomes plain text)
      - attachments: listed, not read
      - inline pictures and picture attachments exported like any picture, with anchors
    inline_excel has no effect here (attachments are not read); it keeps the interface uniform.
    """
    import email

    md = []
    img_manifest = []

    if imgdir is None:
        imgdir = os.path.join(outdir, 'images')
    if save_images:
        os.makedirs(imgdir, exist_ok=True)

    try:
        with open(path, 'rb') as f:
            msg = email.message_from_binary_file(f)
    except Exception as e:
        report.warn(T('eml_failed', fs_text(os.path.basename(path)), e))
        return '', []

    # ---------- header ----------
    subject = _decode_mime_header(msg.get('Subject', '')) or T('no_subject')
    md.append('# %s\n' % subject)
    md.append(T('mail_info'))
    md.append(T('mail_table_head'))
    md.append('| --- | --- |')
    for label, key in ((T('mail_from'), 'From'), (T('mail_to'), 'To'),
                       (T('mail_cc'), 'Cc'), (T('mail_date'), 'Date')):
        v = _decode_mime_header(msg.get(key, ''))
        if v:
            v = v.replace('|', '\\|').replace('\n', ' ')
            md.append('| %s | %s |' % (label, v))
    md.append('')

    # ---------- parts ----------
    text_parts, html_parts = [], []
    attachments = []          # (file name, bytes)
    images = []               # (file name, bytes)

    for part in msg.walk():
        if part.get_content_maintype() == 'multipart':
            continue
        ctype = part.get_content_type()
        disp = (part.get('Content-Disposition') or '').lower()
        fname = _decode_mime_header(part.get_filename() or '')

        if ctype == 'text/plain' and 'attachment' not in disp:
            text_parts.append(_part_text(part))
            continue
        if ctype == 'text/html' and 'attachment' not in disp:
            html_parts.append(_part_text(part))
            continue

        try:
            data = part.get_payload(decode=True)
        except Exception:
            data = None
        if not data:
            continue

        if part.get_content_maintype() == 'image':
            ext = os.path.splitext(fname)[1].lower() or '.png'
            images.append((fname or ('inline%d%s' % (len(images) + 1, ext)),
                           data))
        elif fname:
            attachments.append((fname, data))

    # ---------- body ----------
    body = '\n'.join(t for t in text_parts if t.strip()).strip()
    if not body:
        body = '\n'.join(_html_to_text(h) for h in html_parts).strip()
    md.append(T('mail_body'))
    md.append(body if body else T('mail_empty'))
    md.append('')

    # ---------- pictures ----------
    if images:
        img_block = []
        for i, (fname, data) in enumerate(images, 1):
            # signature icons and dividers are dropped by pixel size; by byte size only
            # when the size cannot be read
            iw, ih = _image_size(data)
            if iw and ih:
                if iw < 150 or ih < 150:
                    continue
            elif len(data) < 8000:
                continue
            ext = os.path.splitext(fname)[1].lower() or '.png'
            out_name = 'mailimg_%03d%s' % (len(img_manifest) + 1, ext)
            if save_images:
                try:
                    with open(os.path.join(imgdir, out_name), 'wb') as g:
                        g.write(data)
                except OSError as e:
                    report.warn(T('mail_img_failed', out_name, e))
                    continue
            loc = image_location(img_ref, out_name, save_images)
            img_block.append(T('mail_anchor', len(img_manifest) + 1, fname or out_name, loc))
            img_manifest.append({
                'no': len(img_manifest) + 1, 'file': out_name,
                'section': T('mail_img_section'), 'orphan': True,
                'ctx_before': '', 'ctx_after': subject[:60],
            })
        if img_block:
            md.append(T('mail_img_heading'))
            md.extend(img_block)
            md.append('')

    # ---------- attachments (listed, not read) ----------
    if attachments:
        md.append(T('mail_att_heading'))
        md.append(T('mail_att_note1'))
        md.append(T('mail_att_note2'))
        md.append(T('mail_att_head'))
        md.append('| --- | --- | --- | --- |')
        for i, (fname, data) in enumerate(attachments, 1):
            ext = os.path.splitext(fname)[1].lower()
            if ext in SUPPORTED_EXT:
                kind = T('att_supported')
            elif ext in LEGACY_EXT:
                kind = T('att_legacy')
            else:
                kind = T('att_unsupported')
            safe_fn = fname.replace('|', '\\|').replace('\n', ' ')
            md.append('| %d | %s | %s | %s |'
                      % (i, safe_fn, human_size(len(data)), kind))
        md.append('')

    report.log(T('mail_log', subject[:40], len(attachments), len(img_manifest)))
    return '\n'.join(md), img_manifest


# ================================================================ duplicates

def content_fingerprint(md, manifest):
    """Content fingerprint, to find files with the same text but different bytes.
    - the leading metadata block (source name, extraction time) differs per file; dropped
    - whitespace normalised
    - the number of pictures is part of the key, so the same text with other pictures differs
    """
    body = re.sub(r'\A(?:>[^\n]*\n)+\s*', '', md or '')
    # picture anchors hold each copy's mirrored path: location, not content
    body = IMAGE_REF_RE.sub('见 <图>', body)
    # in one-file-per-document mode the top heading comes from the file name: location too
    body = re.sub(r'\A#\s+[^\n]*\n', '', body)
    body = re.sub(r'\s+', ' ', body).strip()
    key = '%s|imgs=%d' % (body, len(manifest or []))
    return hashlib.md5(key.encode('utf-8')).hexdigest()


def text_fingerprint(md):
    """Text-only fingerprint for comparisons across runs.
    A single document's md and its section in a merged file give the same value (merging
    replaces the header block and demotes every heading by one level; both are normalised
    here), so both this run's results and older incremental files can be compared.
    """
    body = (md or '') + '\n'
    # the leading metadata block and the "more copies" note differ per output
    body = re.sub(r'\A(?:(?:>[^\n]*)?[ \t]*\n)+', '', body)
    # heading levels change when merged; keep only the heading text
    body = re.sub(r'^#{1,6}[ \t]+', '', body, flags=re.M)
    # picture anchors hold each copy's mirrored path
    body = IMAGE_REF_RE.sub('见 <图>', body)
    body = re.sub(r'\s+', ' ', body).strip()
    if not body:
        return ''
    return hashlib.md5(body.encode('utf-8')).hexdigest()


def load_fp_db(path):
    """Read the text fingerprint file: one "fingerprint<TAB>original path<TAB>md holding the text"
    per line. For a fingerprint seen more than once, the path comes from the last line (latest
    location) and the md from the first."""
    db = {}
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            for ln in f:
                parts = ln.rstrip('\n').split('\t')
                if len(parts) < 3 or not parts[0]:
                    continue
                if parts[0] in db:
                    db[parts[0]] = (parts[1], db[parts[0]][1])
                else:
                    db[parts[0]] = (parts[1], parts[2])
    except OSError:
        pass  # no fingerprint file yet: nothing is known
    return db


def append_fp_db(path, rows):
    """Append (fingerprint, original path, md holding the text) rows to the fingerprint file."""
    if not rows:
        return
    with open(path, 'a', encoding='utf-8', errors='replace') as f:
        for fp, rel, md in rows:
            f.write('%s\t%s\t%s\n' % (fp, rel, md))


def moved_section_text(moved):
    """The "moved, content unchanged" part of a merged file's index.
    moved: [{'rel': current path, 'old_rel': old path, 'kb_file': file holding the text}]"""
    buf = ['', T('moved_heading'), '',
           T('moved_note', len(moved)), '',
           T('moved_table_head'), '| --- | --- | --- |']
    buf.extend(moved_rows(moved))
    buf.append('')
    return '\n'.join(buf)


def moved_rows(moved):
    return ['| %s | %s | %s |' % (m['rel'], m['old_rel'], m['kb_file'])
            for m in moved]


def _remove_entry_outputs(e, md_dir, img_root):
    """Delete an entry's md (written already in one-file mode) and its picture folder."""
    try:
        p = os.path.join(md_dir, e['stat']['md'])
        if os.path.isfile(p):
            os.remove(p)
    except OSError:
        pass  # already gone or locked; the output stays, which is harmless
    try:
        ref = (e.get('img_ref') or '').replace('images/', '', 1)
        if ref:
            d = os.path.join(img_root, ref.replace('/', os.sep))
            if os.path.isdir(d):
                for fn in os.listdir(d):
                    try:
                        os.remove(os.path.join(d, fn))
                    except OSError:
                        pass  # a file that cannot be removed stays
                while os.path.isdir(d) and not os.listdir(d) and \
                        os.path.abspath(d) != os.path.abspath(img_root):
                    os.rmdir(d)
                    d = os.path.dirname(d)
    except OSError:
        pass  # a folder that cannot be removed stays


def drop_known_entries(entries, known, md_dir, img_root):
    """Across runs: documents processed now whose text fingerprint is already known (scanned
    again after a folder move, rename or copy) are not written; only their paths are listed.
    Returns (kept entries, moved list)."""
    kept, moved = [], []
    for e in entries:
        fp = e.get('text_fp')
        if e['stat']['status'] == OK and fp and fp in known:
            old_rel, kb_file = known[fp]
            moved.append({'rel': e['rel'], 'old_rel': old_rel,
                          'kb_file': kb_file, 'fp': fp})
            _remove_entry_outputs(e, md_dir, img_root)
            continue
        kept.append(e)
    return kept, moved


def dedup_entries(entries, md_dir, img_root, report, merge=False):
    """Within one run: entries with the same content fingerprint keep one copy, the one with
    the newest document time (ties: first relative path, so results are reproducible).
    Copies with identical bytes were left out before parsing (byte_dups on an entry, see
    add_byte_duplicates); they join the group of the entry that stands for them.
    Only the output changes, never the source files. Returns (kept entries, duplicate groups)."""
    groups = {}
    for e in entries:
        if e['stat']['status'] not in (OK, SKIPPED):
            continue
        fp = e.get('fingerprint')
        if not fp and e.get('byte_dups'):
            fp = 'rel:' + e['rel']      # output kept from an earlier run: no fingerprint
        if not fp:
            continue
        groups.setdefault(fp, []).append(e)

    dup_groups = []
    dropped = set()

    for fp, members in groups.items():
        if len(members) < 2 and not members[0].get('byte_dups'):
            continue
        members.sort(key=lambda e: (e['stat'].get('doc_mtime') or '', ),
                     reverse=True)
        top_time = members[0]['stat'].get('doc_mtime') or ''
        same_time = [m for m in members
                     if (m['stat'].get('doc_mtime') or '') == top_time]
        same_time.sort(key=lambda e: e['rel'])
        keeper = same_time[0]
        others = [m for m in members if m is not keeper]
        for o in others:
            dropped.add(id(o))
            _remove_entry_outputs(o, md_dir, img_root)
        # copies never parsed: report lines only
        others += [{'rel': r, 'stat': {'doc_mtime': m['stat'].get('doc_mtime', '')}}
                   for m in members for r in m.get('byte_dups', [])]
        others.sort(key=lambda o: o['rel'])

        dup_groups.append({'keeper': keeper, 'others': others})
        report.log(T('dup_log', keeper['rel'], len(others)))

        # the kept copy says where the others were; output from an earlier run is left alone
        if keeper['stat']['status'] != OK:
            continue
        note = T('dup_note', len(others), T('list_sep').join(o['rel'] for o in others))
        if merge:
            keeper['body'] = _insert_after_header(keeper.get('body', ''), note)
        else:
            try:
                p = os.path.join(md_dir, keeper['stat']['md'])
                with open(p, encoding='utf-8') as f:
                    txt = f.read()
                with open(p, 'w', encoding='utf-8', errors='replace') as f:
                    f.write(_insert_after_header(txt, note))
            except OSError:
                pass  # the note is only a hint; the copy stays without it

    kept = [e for e in entries if id(e) not in dropped]
    return kept, dup_groups


def _insert_after_header(md, note):
    """Insert a note after the leading metadata block."""
    m = re.match(r'\A(?:>[^\n]*\n)+', md or '')
    if m:
        return md[:m.end()] + note + md[m.end():]
    return note + (md or '')


# ================================================================ document times

def _parse_iso_utc(s):
    """An ISO 8601 time from docProps, as local time. Without a zone it is UTC (as OOXML says)."""
    if not s:
        return ''
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?'
                 r'\s*(Z|[+-]\d{2}:?\d{2})?', s.strip())
    if not m:
        return ''
    y, mo, d, h, mi = (int(m.group(i)) for i in range(1, 6))
    sec = int(m.group(6) or 0)
    tzs = m.group(7) or 'Z'
    try:
        if tzs == 'Z':
            tz = datetime.timezone.utc
        else:
            t = tzs.replace(':', '')
            sign = 1 if t[0] == '+' else -1
            tz = datetime.timezone(datetime.timedelta(
                seconds=sign * (int(t[1:3]) * 3600 + int(t[3:5]) * 60)))
        dt = datetime.datetime(y, mo, d, h, mi, sec, tzinfo=tz)
    except ValueError:
        return ''
    return dt.astimezone().strftime('%Y-%m-%d %H:%M')


def _parse_pdf_date(s):
    """A PDF date, in either form, as local time:
      1) raw     D:20260615093000+08'00'  /  D:20260101120000Z
      2) pdfinfo "Mon Jun 15 01:30:00 2026 UTC"
    """
    if not s:
        return ''
    s = s.strip()

    # --- form 1: D:YYYYMMDDHHmmSS[±HH'mm'|Z] ---
    m = re.match(r"D?:?\s*(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})?"
                 r"\s*(Z|[+-]\d{2}'?\d{2}'?)?", s)
    if m and m.group(1):
        y, mo, d, h, mi = (int(m.group(i)) for i in range(1, 6))
        sec = int(m.group(6) or 0)
        tzs = m.group(7)
        try:
            if not tzs or tzs == 'Z':
                tz = datetime.timezone.utc
            else:
                t = tzs.replace("'", "")
                sign = 1 if t[0] == '+' else -1
                tz = datetime.timezone(datetime.timedelta(
                    seconds=sign * (int(t[1:3]) * 3600 + int(t[3:5]) * 60)))
            return datetime.datetime(y, mo, d, h, mi, sec, tzinfo=tz
                                     ).astimezone().strftime('%Y-%m-%d %H:%M')
        except ValueError:
            pass  # not form 1; try form 2 below

    # --- form 2: pdfinfo's readable form, maybe with a zone name at the end ---
    tail_utc = bool(re.search(r'\b(UTC|GMT)\b', s))
    cleaned = re.sub(r'\s*\b(UTC|GMT|CST|CET|[A-Z]{3,4})\b\s*$', '', s).strip()
    for fmt in ('%a %b %d %H:%M:%S %Y', '%b %d %H:%M:%S %Y',
                '%Y-%m-%d %H:%M:%S', '%a %b %d %Y %H:%M:%S'):
        try:
            dt = datetime.datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
        if tail_utc:
            dt = dt.replace(tzinfo=datetime.timezone.utc).astimezone()
        return dt.strftime('%Y-%m-%d %H:%M')
    return ''


def read_doc_modified(path):
    """The document's "last modified" time.
    The document's own metadata first (copying and downloading do not change it); otherwise
    the file system time, marked as unreliable. Returns (time string, source code)."""
    ext = os.path.splitext(path)[1].lower()
    val = ''

    if ext in ('.docx', '.xlsx', '.xlsm', '.pptx'):
        try:
            with zipfile.ZipFile(path) as zf:
                if 'docProps/core.xml' in zf.namelist():
                    root = ET.fromstring(zf.read('docProps/core.xml'))
                    for tag in ('{http://purl.org/dc/terms/}modified',
                                '{http://purl.org/dc/terms/}created'):
                        el = root.find(tag)
                        if el is not None and (el.text or '').strip():
                            val = _parse_iso_utc(el.text)
                            if val:
                                break
        except (zipfile.BadZipFile, OSError, ET.ParseError):
            val = ''
    elif ext == '.eml':
        try:
            import email
            from email.utils import parsedate_to_datetime
            with open(path, 'rb') as f:
                m = email.message_from_binary_file(f)
            d = m.get('Date')
            if d:
                dt = parsedate_to_datetime(d)
                if dt.tzinfo is None:
                    # -0000 in the header means "UTC, sender's zone unknown"
                    dt = dt.replace(tzinfo=datetime.timezone.utc)
                dt = dt.astimezone()
                val = dt.strftime('%Y-%m-%d %H:%M')
        except Exception:
            val = ''
        if val:
            return val, 'mail'
    elif ext == '.pdf' and have('pdfinfo'):
        try:
            out = subprocess.run(['pdfinfo', path], capture_output=True,
                                 timeout=30).stdout.decode('utf-8', 'replace')
            for key in ('ModDate:', 'CreationDate:'):
                for ln in out.split('\n'):
                    if ln.startswith(key):
                        val = _parse_pdf_date(ln.split(':', 1)[1])
                        break
                if val:
                    break
        except (OSError, subprocess.SubprocessError):
            val = ''

    if val:
        return val, 'doc'

    try:
        mt = os.path.getmtime(path)
        return (datetime.datetime.fromtimestamp(mt).strftime('%Y-%m-%d %H:%M'), 'fs')
    except OSError:
        return '', 'unknown'


def detect_modified(path):
    """Time used to detect changes in incremental runs: the latest of
      - the file system modification time (content rewritten)
      - the file system change time (file copied in, moved, permissions changed)
      - the document's own time (original edit)
    so both new files in the folder and changed files are found.
    Only used for filtering; the md header still shows read_doc_modified."""
    cands = []
    try:
        st = os.stat(path)
        cands.append(datetime.datetime.fromtimestamp(st.st_mtime))
        cands.append(datetime.datetime.fromtimestamp(st.st_ctime))
    except OSError:
        pass  # no file system time; the document time alone decides
    doc_t, _ = read_doc_modified(path)
    if doc_t:
        try:
            cands.append(datetime.datetime.strptime(doc_t, '%Y-%m-%d %H:%M'))
        except ValueError:
            pass  # unexpected format; the file system times decide
    if not cands:
        return '', 'unknown'
    return max(cands).strftime('%Y-%m-%d %H:%M'), 'newest'


# ================================================================ scanning

SUPPORTED_EXT = ('.docx', '.pptx', '.xlsx', '.xlsm', '.pdf', '.eml')
LEGACY_EXT = ('.doc', '.xls', '.ppt', '.msg')


def _date_filter(items, since, time_basis, cache):
    """Split items into (kept, excluded by date); sets mtime / mtime_src on each."""
    kept, filtered = [], []
    for it in items:
        try:
            mt, msrc = item_modified(it, cache, time_basis, since)
        except ArchiveError as e:
            it.update(_error_item(it['rel'], it['path'], it['ext'], T('arc_bad', e)))
            it.pop('arc', None)
            mt, msrc = item_modified(it, cache, time_basis, since)
        it['mtime'] = mt
        it['mtime_src'] = msrc
        # mt is 'YYYY-MM-DD HH:MM'; since compares by day or by minute
        if not mt or mt[:len(since)] < since:
            filtered.append(it)
        else:
            kept.append(it)
    return kept, filtered


def scan_dir(root, recursive=True, since=None, time_basis='doc', cache=None):
    """Walk a folder; returns (supported files, legacy files, files excluded by date).
    Each item is a dict: path / rel / size / ext. Documents inside zip and tar archives
    are listed as rel = "archive/path inside" with arc set (see expand_archive); an archive
    or member that cannot be read is an item with error set.
    since: 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM'; only documents changed then or later are kept.
    Hidden folders, lock files and this tool's own output folders are skipped.
    cache: the ArchiveCache that unpacks members; without one a private one is used and
    cleaned up before returning."""
    own_cache = cache is None
    if own_cache:
        cache = ArchiveCache()
    try:
        return _scan_dir(root, recursive, since, time_basis, cache)
    finally:
        if own_cache:
            cache.cleanup()


def _scan_dir(root, recursive, since, time_basis, cache):
    ok, legacy, filtered = [], [], []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if not d.startswith('.')
                       and not d.endswith(OUT_DIR_SUFFIXES)
                       and d != 'images']
        for fn in sorted(filenames):
            if fn.startswith('.') or fn.startswith('~$'):
                continue
            ext = os.path.splitext(fn)[1].lower()
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            item = {'path': full, 'rel': fs_text(rel),
                    'size': size, 'ext': ext}
            kind = archive_kind(fn)
            if kind in ('zip', 'tar'):
                found, old = expand_archive(full, fs_text(rel), cache)
                legacy.extend(old)
            elif kind == 'other':
                found = [_error_item(fs_text(rel), full, ext, T('arc_unsupported', ext))]
            elif ext in SUPPORTED_EXT:
                found = [item]
            elif ext in LEGACY_EXT:
                legacy.append(item)
                continue
            else:
                continue
            if since:
                found, out = _date_filter(found, since, time_basis, cache)
                filtered.extend(out)
            ok.extend(found)
        if not recursive:
            break
    ok.sort(key=lambda x: x['rel'])
    legacy.sort(key=lambda x: x['rel'])
    filtered.sort(key=lambda x: x['rel'])
    return ok, legacy, filtered


def human_size(n):
    for unit in ('B', 'KB', 'MB'):
        if n < 1024:
            return '%.0f %s' % (n, unit) if unit == 'B' else '%.1f %s' % (n, unit)
        n /= 1024.0
    return '%.1f GB' % n


def img_sub(rel_dir, base):
    """Mirrored picture sub-path (the same rule as process_one)."""
    parts = [safe_component(p) for p in rel_dir.split(os.sep) if p] \
        if rel_dir else []
    parts.append(safe_component(base))
    return os.path.join(*parts)


def count_gaps(md):
    return sum(md.count(m) for m in GAP_MARKS)


def count_orphans(md):
    return sum(md.count(m) for m in ORPHAN_MARKS)


# ================================================================ archives

ZIP_EXT = ('.zip',)
TAR_EXT = ('.tar', '.tar.gz', '.tgz', '.tar.bz2', '.tbz2', '.tbz', '.tar.xz', '.txz')
OTHER_ARCHIVE_EXT = ('.7z', '.rar')
ARC_MAX_DEPTH = 3                    # an archive inside an archive inside an archive
ARC_MAX_MEMBER = 512 * 1024 * 1024   # one document or nested archive, unpacked
ARC_MAX_TOTAL = 2 * 1024 * 1024 * 1024   # everything taken from one archive, unpacked


class ArchiveError(Exception):
    pass


def archive_kind(name):
    """'zip', 'tar', 'other' (a format the standard library cannot open) or None."""
    n = (name or '').lower()
    if n.endswith(ZIP_EXT):
        return 'zip'
    if n.endswith(TAR_EXT):
        return 'tar'
    if n.endswith(OTHER_ARCHIVE_EXT):
        return 'other'
    return None


def _zip_member_name(info):
    """A zip member name as text. Without the UTF-8 flag the name was decoded as cp437;
    archives made on Chinese Windows store GBK there, so the raw bytes are decoded again."""
    name = info.filename
    if info.flag_bits & 0x800:
        return name
    try:
        raw = name.encode('cp437')
    except UnicodeEncodeError:
        return name
    for enc in ('utf-8', 'gbk', 'gb18030'):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return name


def clean_member_name(name):
    """A member path that cannot leave its archive: no absolute paths, drive letters, "." or ".."."""
    parts = [p for p in fs_text(name).replace('\\', '/').split('/') if p not in ('', '.', '..')]
    if parts and re.match(r'^[A-Za-z]:$', parts[0]):
        parts = parts[1:]
    return '/'.join(parts)


def _member_hidden(name):
    parts = name.split('/')
    return any(p.startswith('.') or p == '__MACOSX' for p in parts) or \
        parts[-1].startswith('~$')


class ArchiveCache:
    """Unpacks archive members into one private temporary folder, on demand and once each.
    Nothing is written next to the archives; cleanup() removes the folder.
    A member is addressed as (path of the outermost archive, (member key, ...)): one key per
    level, the last one the document itself."""

    def __init__(self):
        self._tmp = None
        self._n = 0
        self._local = {}      # (outer, keys) -> unpacked file
        self._lists = {}      # unpacked or outer archive path -> member list

    def cleanup(self):
        if self._tmp:
            shutil.rmtree(self._tmp, ignore_errors=True)
            self._tmp = None
        self._local.clear()
        self._lists.clear()

    def _new_dir(self):
        if not self._tmp:
            self._tmp = tempfile.mkdtemp(prefix='doc2kb-arc-')
        self._n += 1
        d = os.path.join(self._tmp, str(self._n))
        os.mkdir(d)
        return d

    def members(self, path, kind):
        """The regular files in an archive: dicts with key, name (cleaned), size, mtime,
        encrypted. Folders, links and devices are left out. Raises ArchiveError."""
        if path in self._lists:
            return self._lists[path]
        out = []
        try:
            if kind == 'zip':
                with zipfile.ZipFile(path) as zf:
                    for info in zf.infolist():
                        if info.is_dir():
                            continue
                        mode = (info.external_attr >> 16) & 0o170000
                        if mode and mode != 0o100000:
                            continue      # symbolic link or other special file
                        try:
                            mt = datetime.datetime(*info.date_time).timestamp()
                        except (ValueError, OverflowError):
                            mt = None
                        out.append({'key': info.filename,
                                    'name': clean_member_name(_zip_member_name(info)),
                                    'size': info.file_size, 'mtime': mt,
                                    'encrypted': bool(info.flag_bits & 0x1)})
            else:
                with tarfile.open(path, 'r:*') as tf:
                    for m in tf.getmembers():
                        if not m.isfile():
                            continue
                        out.append({'key': m.name, 'name': clean_member_name(m.name),
                                    'size': m.size, 'mtime': m.mtime, 'encrypted': False})
        except (zipfile.BadZipFile, tarfile.TarError, OSError, EOFError,
                ValueError, NotImplementedError) as e:
            raise ArchiveError(str(e) or e.__class__.__name__)
        self._lists[path] = out
        return out

    def _write(self, src, dest, limit):
        n = 0
        with open(dest, 'wb') as f:
            while True:
                buf = src.read(1024 * 1024)
                if not buf:
                    break
                n += len(buf)
                if n > limit:
                    raise ArchiveError(T('arc_too_big', human_size(limit)))
                f.write(buf)

    def _dest(self, m):
        base = m['name'].rsplit('/', 1)[-1] or 'member'
        if len(base.encode('utf-8', 'replace')) > 200:
            stem, ext = os.path.splitext(base)
            base = safe_name(stem) + ext
        return os.path.join(self._new_dir(), base)

    def _unpack(self, path, kind, wanted):
        """Unpack the members of one archive whose keys are in wanted ({key: cache key}).
        A tar archive is read once from start to end for all of them."""
        lst = {m['key']: m for m in self.members(path, kind)}
        try:
            if kind == 'zip':
                with zipfile.ZipFile(path) as zf:
                    for key, ck in wanted.items():
                        m = lst[key]
                        dest = self._dest(m)
                        with zf.open(key) as src:
                            self._write(src, dest, ARC_MAX_MEMBER)
                        self._done(ck, dest, m)
            else:
                with tarfile.open(path, 'r:*') as tf:
                    for ti in tf:
                        ck = wanted.get(ti.name)
                        if ck is None or not ti.isfile() or ck in self._local:
                            continue
                        dest = self._dest(lst[ti.name])
                        src = tf.extractfile(ti)
                        self._write(src, dest, ARC_MAX_MEMBER)
                        self._done(ck, dest, lst[ti.name])
        except ArchiveError:
            raise
        except (zipfile.BadZipFile, tarfile.TarError, OSError, EOFError, KeyError,
                ValueError, NotImplementedError, RuntimeError) as e:
            # RuntimeError: an encrypted zip member without a password
            raise ArchiveError(str(e) or e.__class__.__name__)

    def _done(self, ck, dest, m):
        if m.get('mtime'):
            try:
                os.utime(dest, (m['mtime'], m['mtime']))
            except (OSError, OverflowError, ValueError):
                pass  # keeps the unpack time; only the fallback document time is affected
        self._local[ck] = dest

    def local(self, outer, keys):
        """The unpacked file of a member (nested archives on the way are unpacked too)."""
        ck = (outer, tuple(keys))
        if ck in self._local:
            return self._local[ck]
        parent = outer if len(keys) == 1 else self.local(outer, keys[:-1])
        kind = archive_kind(keys[-2] if len(keys) > 1 else outer)
        wanted = {keys[-1]: ck}
        if kind == 'tar':
            # one pass for every document and nested archive of this tar
            total = 0
            for m in self.members(parent, kind):
                if m['size'] > ARC_MAX_MEMBER or not _wanted_member(m['name']):
                    continue
                total += m['size']
                if total > ARC_MAX_TOTAL:
                    break
                wanted.setdefault(m['key'], (outer, tuple(keys[:-1]) + (m['key'],)))
        self._unpack(parent, kind, wanted)
        if ck not in self._local:
            raise ArchiveError(T('arc_missing'))
        return self._local[ck]


def _wanted_member(name):
    ext = os.path.splitext(name)[1].lower()
    return not _member_hidden(name) and (
        ext in SUPPORTED_EXT or archive_kind(name) in ('zip', 'tar'))


def _error_item(rel, path, ext, note):
    """An archive or member that cannot be read; processed as a failure with this note."""
    return {'path': path, 'rel': rel, 'size': 0, 'ext': ext, 'error': note}


def expand_archive(outer, rel, cache, keys=(), depth=1):
    """The documents inside an archive as scan items (path / rel / size / ext / arc).
    rel is the archive's relative path; members get rel + '/' + their path in the archive.
    Returns (supported items, legacy items); unreadable parts come back as error items."""
    ok, legacy = [], []
    kind = archive_kind(keys[-1] if keys else outer)
    try:
        local = cache.local(outer, keys) if keys else outer
        members = cache.members(local, kind)
    except ArchiveError as e:
        return [_error_item(rel, outer, '.' + kind, T('arc_bad', e))], []
    total = 0
    for m in sorted(members, key=lambda x: x['name']):
        name = m['name']
        if not name or _member_hidden(name):
            continue
        mrel = rel + '/' + name
        ext = os.path.splitext(name)[1].lower()
        sub = archive_kind(name)
        if ext in LEGACY_EXT:
            legacy.append({'path': outer, 'rel': mrel, 'size': m['size'], 'ext': ext})
            continue
        if sub == 'other':
            ok.append(_error_item(mrel, outer, ext, T('arc_unsupported', ext)))
            continue
        if ext not in SUPPORTED_EXT and sub is None:
            continue
        if m['encrypted']:
            ok.append(_error_item(mrel, outer, ext, T('arc_encrypted')))
            continue
        if m['size'] > ARC_MAX_MEMBER:
            ok.append(_error_item(mrel, outer, ext, T('arc_too_big', human_size(ARC_MAX_MEMBER))))
            continue
        total += m['size']
        if total > ARC_MAX_TOTAL:
            ok.append(_error_item(mrel, outer, ext, T('arc_total', human_size(ARC_MAX_TOTAL))))
            continue
        if sub:
            if depth >= ARC_MAX_DEPTH:
                ok.append(_error_item(mrel, outer, ext, T('arc_deep', ARC_MAX_DEPTH)))
                continue
            o, l = expand_archive(outer, mrel, cache, keys + (m['key'],), depth + 1)
            ok.extend(o)
            legacy.extend(l)
            continue
        ok.append({'path': outer, 'rel': mrel, 'size': m['size'], 'ext': ext,
                   'arc': (outer, keys + (m['key'],))})
    return ok, legacy


def item_source(it, cache):
    """The file to parse for a scan item: the file itself, or the unpacked archive member."""
    if it.get('arc'):
        return cache.local(*it['arc'])
    return it['path']


def item_modified(it, cache, time_basis='doc', since=None):
    """(time, source) of a scan item for the date filter. For an archive member the 'newest'
    basis also counts the archive's own file times; when those are already older than since
    the member is not unpacked (a member cannot be newer than the archive that holds it)."""
    if not it.get('arc') or it.get('error'):
        if time_basis == 'newest':
            return detect_modified(it['path'])
        return read_doc_modified(it['path'])
    outer_t = ''
    if time_basis == 'newest':
        outer_t, _ = detect_modified(it['arc'][0])
        if since and outer_t and outer_t[:len(since)] < since:
            return outer_t, 'newest'
    mt, msrc = read_doc_modified(cache.local(*it['arc']))
    if time_basis == 'newest':
        return max(mt, outer_t), 'newest'
    return mt, msrc


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            buf = f.read(1024 * 1024)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def byte_duplicates(items, cache, keep_first=None):
    """Files with identical bytes, found before parsing: only one of each group is parsed.
    The copy kept: one keep_first(item) marks (its output exists already), then one outside
    any archive, then the first relative path. Returns {kept rel: [left-out items]}."""
    groups = {}
    for it in items:
        if it.get('error'):
            continue
        try:
            digest = sha256_file(item_source(it, cache))
        except (OSError, ArchiveError):
            continue    # read again when processed; its failure is reported there
        groups.setdefault(digest, []).append(it)
    dups = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda x: (0 if keep_first and keep_first(x) else 1,
                                    1 if x.get('arc') else 0, x['rel']))
        dups[members[0]['rel']] = members[1:]
    return dups


def failed_entry(rel, src, base, note):
    """A report entry for a file that failed without being parsed."""
    return {
        'rel': rel,
        'stat': {'src': src, 'md': base + '.md', 'chars': 0, 'tables': 0, 'imgs': 0,
                 'orphans': 0, 'gaps': 0, 'status': FAILED, 'note': note,
                 'doc_mtime': '', 'mtime_src': '', 'img_dir': ''},
        'summary': '', 'headings': [], 'truncated': False, 'manifest': [],
        'img_ref': '', 'body': '', 'fingerprint': '', 'text_fp': '',
        'log': [T('log_source', rel), note], 'warnings': [],
    }


def add_byte_duplicates(entries, dups):
    """Attach the copies left out before parsing to the entry of the copy kept. When the kept
    copy failed, the copies get the same failure (their bytes are the same)."""
    extra = []
    for e in entries:
        others = dups.get(e['rel'])
        if not others:
            continue
        if e['stat']['status'] == FAILED:
            for o in others:
                extra.append(failed_entry(o['rel'], o['path'], '', e['stat']['note']))
        else:
            e['byte_dups'] = [o['rel'] for o in others]
    return entries + extra


def base_names(items):
    """md file stems for scan items, numbered when names repeat. Files outside archives are
    numbered first (in item order), so a new archive does not rename existing output."""
    used, out = {}, [None] * len(items)
    order = sorted(range(len(items)),
                   key=lambda i: (1 if items[i].get('arc') or items[i].get('error') else 0, i))
    for i in order:
        it = items[i]
        name = it['rel'].replace('\\', '/').rsplit('/', 1)[-1] \
            if it.get('arc') or it.get('error') else os.path.basename(it['path'])
        stem = safe_name(os.path.splitext(name)[0])
        if stem in used:
            used[stem] += 1
            out[i] = '%s_%d' % (stem, used[stem])
        else:
            used[stem] = 1
            out[i] = stem
    return out


# ================================================================ one document

def convert(src, ext, md_dir, report, inline_excel=True, imgdir=None, img_ref='images',
            save_images=True):
    """Parse one file by its extension; returns (Markdown, image manifest).
    Raises zipfile.BadZipFile or other errors from the parsers; process_one() handles them."""
    if ext == '.docx':
        return parse_docx(src, md_dir, report, inline_excel=inline_excel,
                          imgdir=imgdir, img_ref=img_ref, save_images=save_images)
    if ext == '.pptx':
        return parse_pptx(src, md_dir, report, imgdir=imgdir, img_ref=img_ref,
                          save_images=save_images)
    if ext in ('.xlsx', '.xlsm'):
        return parse_xlsx_file(src, md_dir, report, imgdir=imgdir, img_ref=img_ref,
                               save_images=save_images)
    if ext == '.eml':
        return parse_eml(src, md_dir, report, inline_excel=inline_excel,
                         imgdir=imgdir, img_ref=img_ref, save_images=save_images)
    if ext == '.pdf':
        md, manifest = parse_pdf(src, md_dir, report, imgdir=imgdir, img_ref=img_ref,
                                 save_images=save_images)
        report.warn(T('pdf_lossy'))
        return md, manifest
    raise ValueError(ext)


def process_one(src, md_dir, report, args, img_root=None, rel_dir='',
                md_basename=None):
    """Process one document; returns (stat dict, Markdown with header, image manifest).
    md_dir     : Markdown output folder (in batch mode every md sits at this level)
    img_root   : picture root; in batch mode md_dir/images, mirrored by rel_dir below it
    rel_dir    : the source file's folder relative to the source root
    md_basename: md file name stem (to tell apart files with the same name)
    args needs no_inline, sheets and may carry no_images and merge."""
    ext = os.path.splitext(src)[1].lower()
    base = md_basename or safe_name(os.path.splitext(os.path.basename(src))[0])
    save_imgs = not getattr(args, 'no_images', False)

    if img_root is None:
        imgdir = os.path.join(md_dir, 'images')
        img_ref = 'images'
    else:
        parts = [safe_component(p) for p in rel_dir.split(os.sep) if p] \
            if rel_dir else []
        parts.append(safe_component(base))
        imgdir = os.path.join(img_root, os.path.join(*parts))
        img_ref = 'images/' + '/'.join(parts)

    stat = {
        'src': src, 'md': base + '.md', 'chars': 0, 'tables': 0, 'imgs': 0,
        'orphans': 0, 'gaps': 0, 'status': OK, 'note': '',
        'doc_mtime': '', 'mtime_src': '', 'img_dir': imgdir,
    }

    if ext not in SUPPORTED_EXT:
        stat['status'] = FAILED
        stat['note'] = T('unsupported', ext)
        return stat, '', []
    try:
        md, manifest = convert(src, ext, md_dir, report,
                               inline_excel=not getattr(args, 'no_inline', False),
                               imgdir=imgdir, img_ref=img_ref, save_images=save_imgs)
    except zipfile.BadZipFile:
        stat['status'] = FAILED
        stat['note'] = T('corrupt_note')
        report.warn(T('corrupt_warn', fs_text(os.path.basename(src))))
        return stat, '', []
    except Exception as e:
        stat['status'] = FAILED
        stat['note'] = T('parse_error_note', str(e)[:120])
        report.warn(T('parse_error_warn', fs_text(os.path.basename(src)), e))
        log.debug('parse error in %s', fs_text(src), exc_info=True)
        return stat, '', []

    if not md.strip():
        report.warn(T('nothing_extracted'))
        stat['status'] = FAILED
        stat['note'] = T('nothing_extracted')
        return stat, '', manifest

    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
    doc_mtime, mtime_src = read_doc_modified(src)
    stat['doc_mtime'] = doc_mtime
    stat['mtime_src'] = mtime_src
    unreliable = mtime_src not in RELIABLE_SRC
    header = T('header', fs_text(os.path.basename(src)), doc_mtime or T('unknown'),
               src_label(mtime_src), T('unreliable') if unreliable else '', ts)
    md = header + md

    if not getattr(args, 'merge', False):
        main_md = os.path.join(md_dir, base + '.md')
        with open(main_md, 'w', encoding='utf-8', errors='replace') as f:
            f.write(md)

    stat['chars'] = len(md)
    stat['imgs'] = len(manifest)
    stat['orphans'] = sum(1 for m in manifest if m['orphan'])
    stat['tables'] = len(re.findall(r'^\|[\s\-|]+\|$', md, re.M))
    stat['gaps'] = count_gaps(md)

    report.log('')
    report.log(T('log_chars', stat['chars']))
    report.log(T('log_images', stat['imgs']))
    if manifest:
        report.log(T('log_orphans', stat['orphans']))

    if getattr(args, 'sheets', False) and manifest and save_imgs:
        make_contact_sheets(manifest, md_dir, report,
                            imgdir=imgdir, sheetdir=imgdir)

    return stat, md, manifest


# ================================================================ index

def extract_outline(md, max_headings=40, summary_chars=200):
    """Opening text and the level 1 and 2 headings of a Markdown text."""
    lines = md.split('\n')
    # opening text: skip the quoted header and headings, take the first body paragraphs
    summary_parts = []
    for ln in lines:
        t = ln.strip()
        if not t or t[0] in '>#|*' or t.startswith('---'):
            continue
        if t.startswith('[图') or t.startswith('[Image') or t.startswith('（AI'):
            continue
        t = re.sub(r'^[-+]\s*', '', t)          # list markers
        if not t:
            continue
        summary_parts.append(t)
        if sum(len(x) for x in summary_parts) >= summary_chars:
            break
    joined = ' '.join(summary_parts)
    summary = joined[:summary_chars]
    if len(joined) > summary_chars:
        summary += '…'

    headings = []
    for ln in lines:
        m = re.match(r'^(#{1,2})\s+(.+)$', ln.strip())
        if m:
            headings.append((len(m.group(1)), m.group(2).strip()))
    truncated = len(headings) > max_headings
    return summary, headings[:max_headings], truncated


def sorted_ok(entries):
    """Done and kept entries, newest document time first."""
    ok = [e for e in entries if e['stat']['status'] in (OK, SKIPPED)]
    ok.sort(key=lambda e: e['stat'].get('doc_mtime') or '', reverse=True)
    return ok


def build_index_text(entries, ts, src_root, title=None, merged=False):
    """Index text (a file of its own, or the start of a merged file)."""
    ok = sorted_ok(entries)
    buf = []
    w = buf.append

    w('# %s\n' % (title or T('index_title')))
    w(T('src_root', src_root))
    w(T('generated', ts))
    w(T('doc_count', len(ok)) + (T('doc_count_merged') if merged else T('doc_count_index')))
    w(T('index_mechanical'))
    w(T('index_order'))
    w(T('index_fs_mark') + '\n')

    w(T('index_list'))
    w(T('index_table_head'))
    w('| --- | --- | --- | --- | --- |')
    for e in ok:
        st = e['stat']
        w('| %s | %s | %s | %d | %d |' % (
            st['md'], fmt_mtime(st), e['rel'], st['chars'], st['imgs']))
    w('')

    for e in ok:
        st = e['stat']
        w('\n### %s\n' % st['md'])
        _index_item(w, e)
        w('')
    return '\n'.join(buf)


def _index_item(w, e):
    st = e['stat']
    w(T('item_path', e['rel']))
    w(T('item_modified', st.get('doc_mtime') or T('unknown'), src_label(st.get('mtime_src') or 'unknown')))
    w(T('item_counts', st['chars'], st['imgs'], st['orphans']))
    if e['summary']:
        w(T('item_summary'))
        w(e['summary'])
    if e['headings']:
        w(T('item_outline'))
        for lv, h in e['headings']:
            w('%s- %s' % ('  ' * (lv - 1), h))
        if e['truncated']:
            w(T('item_truncated', len(e['headings'])))


def write_merged(entries, md_dir, ts, src_root, out_name, moved=None):
    """Merged mode: the index and every document's text in one md."""
    ok = sorted_ok(entries)
    path = os.path.join(md_dir, out_name + '.md')
    idx = build_index_text(entries, ts, src_root, title=out_name, merged=True)
    with open(path, 'w', encoding='utf-8', errors='replace') as f:
        f.write(idx)
        if moved:
            f.write(moved_section_text(moved))
        f.write('\n\n---\n\n')
        f.write(T('body_mark'))
        for e in ok:
            st = e['stat']
            f.write('\n\n---\n\n')
            f.write('# %s\n\n' % os.path.splitext(st['md'])[0])
            f.write(T('section_path', e['rel']))
            f.write(T('section_modified', st.get('doc_mtime') or T('unknown'),
                      src_label(st.get('mtime_src') or 'unknown')))
            f.write(T('section_extracted', ts))
            body = e.get('body', '')
            # drop the single-file header (the quoted lines above say the same)
            body = re.sub(r'^(> [^\n]*\n)+\n', '', body)
            # demote every heading by one level so they sit under the section heading
            body = re.sub(r'^(#{1,5}) ', r'#\1 ', body, flags=re.M)
            f.write(body.strip() + '\n')
    return path


def write_index(entries, md_dir, ts, src_root):
    """The index an AI reads first to decide which document to look in."""
    path = os.path.join(md_dir, T('index_file'))
    ok = sorted_ok(entries)
    buf = []
    w = buf.append
    w('# %s\n' % T('index_title'))
    w(T('src_root', src_root))
    w(T('generated', ts))
    w(T('doc_count', len(ok)) + T('doc_count_index'))
    w(T('index_mechanical'))
    w(T('index_order'))
    w(T('index_fs_mark') + '\n')
    w(T('index_list'))
    w(T('index_table_head'))
    w('| --- | --- | --- | --- | --- |')
    for e in ok:
        st = e['stat']
        w('| %s | %s | %s | %d | %d |' % (
            st['md'], fmt_mtime(st), e['rel'], st['chars'], st['imgs']))
    w('')
    text = '\n'.join(buf) + '\n'
    for e in ok:
        item = []
        _index_item(item.append, e)
        text += '\n---\n\n## %s\n\n%s\n\n' % (e['stat']['md'], '\n'.join(item))
    with open(path, 'w', encoding='utf-8', errors='replace') as f:
        f.write(text)
    return path


def fmt_mtime(st):
    """Document time; a file system time gets a ※ mark."""
    t = st.get('doc_mtime') or ''
    if not t:
        return T('unknown')
    return t + ('' if src_code(st.get('mtime_src') or '') in RELIABLE_SRC else ' ※')


def write_manifest_all(entries, md_dir, images_saved=True):
    """Batch mode: one image list for all documents."""
    docs = [e for e in entries if e.get('manifest')]
    if not docs:
        return
    path = os.path.join(md_dir, T('image_list_file'))
    total = sum(len(e['manifest']) for e in docs)
    total_orphan = sum(1 for e in docs for m in e['manifest'] if m['orphan'])
    with open(path, 'w', encoding='utf-8', errors='replace') as f:
        f.write(T('imglist_title'))
        f.write(T('imglist_count', len(docs), total, total_orphan))
        f.write(T('imglist_note'))
        if not images_saved:
            f.write(T('imglist_not_saved'))
        f.write('\n')

        f.write(T('imglist_per_doc'))
        f.write(T('imglist_doc_head'))
        for e in docs:
            n_o = sum(1 for m in e['manifest'] if m['orphan'])
            f.write('| %s | %d | %d |\n'
                    % (e['stat']['md'], len(e['manifest']), n_o))
        f.write('\n')

        for e in docs:
            f.write('\n---\n\n## %s\n\n' % e['stat']['md'])
            f.write(T('item_path', e['rel']) + '\n')
            f.write(T('imglist_dir', e.get('img_ref', '')))
            orphans = [m for m in e['manifest'] if m['orphan']]
            if orphans:
                f.write(T('imglist_review'))
                for m in orphans:
                    f.write(T('imglist_at', m['file'], m['section']))
                    if m.get('ctx_before') or m.get('ctx_after'):
                        f.write(T('imglist_ctx', m.get('ctx_before', ''), m.get('ctx_after', '')))
                f.write('\n')
            f.write(T('imglist_head'))
            for m in e['manifest']:
                f.write('| %d | %s | %s | %s |\n'
                        % (m['no'], m['file'], m['section'],
                           T('yes') if m['orphan'] else T('no')))
    return path


def write_batch_report(entries, md_dir, ts, src_root, skipped, legacy,
                       filtered=None, since=None, dup_groups=None,
                       moved=None):
    """The batch report."""
    path = os.path.join(md_dir, T('report_file'))
    ok = [e for e in entries if e['stat']['status'] in (OK, SKIPPED)]
    fresh = [e for e in entries if e['stat']['status'] == OK]
    fail = [e for e in entries if e['stat']['status'] == FAILED]
    rule = '-' * 70

    with open(path, 'w', encoding='utf-8', errors='replace') as f:
        f.write(T('rep_title'))
        f.write('=' * 70 + '\n\n')
        f.write(T('rep_src', src_root))
        f.write(T('rep_out', md_dir))
        f.write(T('rep_done', ts))
        if since:
            f.write(T('rep_since', since))
        f.write(T('rep_counts', len(fresh), len(skipped), len(fail), len(legacy)))
        f.write(T('rep_filtered_n', len(filtered or [])))
        n_dup = sum(len(g['others']) for g in (dup_groups or []))
        f.write(T('rep_dup_n', n_dup))
        if moved:
            f.write(T('rep_moved_n', len(moved)))
        f.write('\n')
        f.write(T('rep_total', len(ok)))

        if ok:
            f.write(rule + '\n' + T('rep_list') + '\n' + rule + '\n')
            f.write(T('rep_list_head'))
            ok_sorted = sorted(ok, key=lambda x: x['stat'].get('doc_mtime') or '',
                               reverse=True)
            for e in ok_sorted:
                st = e['stat']
                f.write('%s | %s | %d | %d | %d | %d | %s\n'
                        % (st['md'], fmt_mtime(st), st['chars'], st['tables'],
                           st['imgs'], st['orphans'],
                           T('rep_new') if st['status'] == OK else T('rep_kept')))
            tot_c = sum(e['stat']['chars'] for e in ok)
            tot_i = sum(e['stat']['imgs'] for e in ok)
            tot_o = sum(e['stat']['orphans'] for e in ok)
            f.write(T('rep_sum', tot_c, tot_i, tot_o))
            f.write(T('rep_sum_note'))

        if fail:
            f.write(rule + '\n' + T('rep_failed') + '\n' + rule + '\n')
            for e in fail:
                f.write(T('rep_reason', e['rel'], e['stat']['note']))
            f.write('\n')

        if skipped:
            f.write(rule + '\n' + T('rep_skipped') + '\n' + rule + '\n')
            for r in skipped:
                f.write('  • %s\n' % r)
            f.write(T('rep_skipped_hint'))

        if dup_groups:
            n_dup = sum(len(g['others']) for g in dup_groups)
            f.write(rule + '\n' + T('rep_dups') + '\n' + rule + '\n')
            f.write(T('rep_dups_note', len(dup_groups), n_dup))
            for i, g in enumerate(dup_groups, 1):
                k = g['keeper']
                f.write(T('rep_group', i))
                f.write(T('rep_keep', k['rel'], k['stat'].get('doc_mtime') or T('time_unknown')))
                for o in g['others']:
                    f.write(T('rep_drop', o['rel'], o['stat'].get('doc_mtime') or T('time_unknown')))
            f.write('\n')

        if moved:
            f.write(rule + '\n' + T('rep_moved') + '\n' + rule + '\n')
            f.write(T('rep_moved_note', len(moved)))
            for m in moved:
                f.write(T('rep_moved_item', m['rel'], m['old_rel'], m['kb_file']))
            f.write('\n')

        if filtered:
            f.write(rule + '\n' + T('rep_filtered', since) + rule + '\n')
            f.write(T('rep_filtered_note', len(filtered)))
            for it in filtered:
                mark = '' if src_code(it.get('mtime_src') or '') in RELIABLE_SRC else ' ※'
                f.write(T('rep_filtered_item', it['rel'], it.get('mtime') or T('unknown'), mark))
            f.write('\n')

        if legacy:
            f.write(rule + '\n' + T('rep_legacy') + '\n' + rule + '\n')
            for it in legacy:
                f.write('  • %s\n' % it['rel'])
            f.write(T('rep_legacy_hint'))

        f.write('=' * 70 + '\n' + T('rep_logs') + '\n' + '=' * 70 + '\n')
        for e in entries:
            f.write('\n' + rule + '\n%s\n' % e['rel'])
            f.write(rule + '\n')
            f.write('\n'.join(e['log']) + '\n')
            if e['warnings']:
                f.write(T('rep_warnings', len(e['warnings'])))
                for w in e['warnings']:
                    f.write('  • %s\n' % w)
    return path


def print_stats(**counts):
    """Machine-readable results for doc2kb.sh: one "STAT<TAB>key<TAB>value" line each."""
    for k in sorted(counts):
        print('STAT\t%s\t%s' % (k, counts[k]))


# ================================================================ batch mode

DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}( \d{2}:\d{2})?$')
NO_MATCH = 10    # exit code: no matching file (cron runs exit quietly on it)


def _kept_entry(src, rel, rel_dir, base, md_path, img_root):
    """Rebuild the index and report entry of a document skipped because its output exists,
    so the summary files stay complete in incremental runs."""
    with open(md_path, encoding='utf-8') as f:
        old_md = f.read()
    summary, headings, truncated = extract_outline(old_md)
    sub = img_sub(rel_dir, base)
    d = os.path.join(img_root, sub)
    cached = []
    c_mtime = c_msrc = ''
    mf = os.path.join(d, '.manifest.json')
    if os.path.isfile(mf):
        try:
            with open(mf, encoding='utf-8') as g:
                raw_c = json.load(g)
            if isinstance(raw_c, dict):
                cached = raw_c.get('manifest', [])
                c_mtime = raw_c.get('doc_mtime', '')
                c_msrc = src_code(raw_c.get('mtime_src', ''))
            else:
                cached = raw_c        # older sidecar format
        except (OSError, ValueError):
            cached = []
    if not c_mtime:
        c_mtime, c_msrc = read_doc_modified(src)
    n_img = len(cached) if cached else (
        len([x for x in os.listdir(d)
             if not x.startswith(SHEET_PREFIXES) and not x.startswith('.')])
        if os.path.isdir(d) else 0)
    return {
        'rel': rel,
        'stat': {'src': src, 'md': base + '.md',
                 'chars': len(old_md),
                 'tables': len(re.findall(r'^\|[\s\-|]+\|$', old_md, re.M)),
                 'imgs': n_img,
                 'orphans': sum(1 for m in cached if m['orphan'])
                 if cached else count_orphans(old_md),
                 'gaps': count_gaps(old_md),
                 'doc_mtime': c_mtime, 'mtime_src': c_msrc,
                 'status': SKIPPED, 'note': T('kept_note')},
        'summary': summary, 'headings': headings,
        'truncated': truncated, 'manifest': cached,
        'img_ref': 'images/' + sub.replace(os.sep, '/'),
        'log': [T('kept_log')],
        'warnings': [],
    }


def run_batch(args, writer=None):
    """Convert every document in the folder args.batch. writer, when given, replaces the merged
    or per-document output: see call_writer()."""
    cache = ArchiveCache()
    try:
        return _run_batch(args, cache, writer)
    finally:
        cache.cleanup()


def call_writer(writer, entries, md_dir, ts, src_root, out_name):
    """Library use: writer(entries, md_dir, ts, src_root, out_name) writes the output files instead
    of write_merged() / write_index(). entries are the converted documents after the date filter,
    duplicate removal and the fingerprint file: dicts with 'rel' (path below src_root), 'body'
    (the Markdown text) and 'stat' (status, md, chars, imgs, doc_mtime, mtime_src). It returns
    (path, file_of, finish): path of the main output file or None; file_of maps rel to the output
    file name holding that document (used for fingerprint lines); finish(report_path), or None,
    prints the writer's own summary lines and may add to the report."""
    path, file_of, finish = writer(entries, md_dir, ts, src_root, out_name)
    return path, file_of or {}, finish


def _run_batch(args, cache, writer=None):
    src_root = os.path.abspath(args.batch)
    if not os.path.isdir(src_root):
        log.error('%s', T('no_folder', src_root))
        return 2

    since = getattr(args, 'since', None)
    if since and not DATE_RE.match(since):
        log.error('%s', T('bad_date', since))
        return 2
    files, legacy, filtered = scan_dir(src_root,
                                       recursive=not args.no_recursive,
                                       since=since,
                                       time_basis=getattr(args, 'time_basis', 'doc'),
                                       cache=cache)

    # scan only: a tab-separated list for the desktop dialogs
    if args.scan_only:
        for it in files:
            print('OK\t%s\t%s\t%s' % (fs_text(it['rel']),
                                        human_size(it['size']),
                                        it.get('mtime', '')))
        for it in legacy:
            print('LEGACY\t%s\t%s' % (fs_text(it['rel']),
                                       human_size(it['size'])))
        for it in filtered:
            print('FILTERED\t%s\t%s' % (fs_text(it['rel']),
                                         it.get('mtime', '')))
        return 0

    # only these relative paths (files ticked in the dialog, or an incremental batch).
    # Files named here are not date-filtered; incremental runs rely on that to retry failures.
    if args.only:
        wanted = set()
        with open(args.only, encoding='utf-8') as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    wanted.add(ln)
        files = sorted((it for it in files + filtered if it['rel'] in wanted),
                       key=lambda x: x['rel'])

    if not files:
        if filtered:
            print(T('none_by_date', len(filtered), since))
        else:
            print(T('none_found'))
        return NO_MATCH

    md_dir = os.path.abspath(args.outdir or os.path.join(src_root, T('out_dir_suffix')))
    img_root = os.path.join(md_dir, 'images')
    os.makedirs(md_dir, exist_ok=True)
    merge = getattr(args, 'merge', False)

    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
    entries, skipped = [], []

    # same names get a number; counted over every file, so names stay stable between runs
    bases = base_names(files)
    by_rel = {it['rel']: b for it, b in zip(files, bases)}

    def output_exists(it):
        return os.path.exists(os.path.join(md_dir, by_rel[it['rel']] + '.md')) \
            and not args.force and not merge

    # identical bytes: only one copy is parsed
    dups = {}
    if not getattr(args, 'no_dedup', False):
        dups = byte_duplicates(files, cache, keep_first=output_exists)
    dup_of = {o['rel']: k for k, others in dups.items() for o in others}

    total = len(files)
    for i, (it, base) in enumerate(zip(files, bases), 1):
        rel = it['rel']
        rel_dir = os.path.dirname(rel)

        if rel in dup_of:
            print(T('skip_dup', i, total, dup_of[rel], fs_text(rel)), flush=True)
            continue

        if it.get('error'):
            print('[%d/%d] %s' % (i, total, fs_text(rel)), flush=True)
            entries.append(failed_entry(rel, it['path'], base, it['error']))
            continue
        try:
            src = item_source(it, cache)
        except ArchiveError as e:
            print('[%d/%d] %s' % (i, total, fs_text(rel)), flush=True)
            entries.append(failed_entry(rel, it['path'], base, T('arc_bad', e)))
            continue

        md_path = os.path.join(md_dir, base + '.md')

        # incremental: an existing output is kept (not in merged mode, which rewrites everything)
        if output_exists(it):
            skipped.append(rel)
            print(T('skip_exists', i, total, fs_text(rel)), flush=True)
            try:
                entries.append(_kept_entry(src, rel, rel_dir, base, md_path, img_root))
            except OSError:
                pass  # output vanished meanwhile: left out of the summary
            continue

        print('[%d/%d] %s' % (i, total, fs_text(rel)), flush=True)
        report = Report()
        report.log(T('log_source', fs_text(
            os.path.join(src_root, rel) if it.get('arc') else src)))
        report.log(T('log_output', base + '.md'))
        report.log('')

        stat, md, manifest = process_one(
            src, md_dir, report, args,
            img_root=img_root, rel_dir=rel_dir, md_basename=base)

        summary, headings, truncated = ('', [], False)
        sub0 = img_sub(rel_dir, base)
        if md:
            summary, headings, truncated = extract_outline(md)
            # sidecar file: later incremental runs restore the image list from it
            if not getattr(args, 'no_images', False):
                try:
                    d0 = os.path.join(img_root, sub0)
                    os.makedirs(d0, exist_ok=True)
                    with open(os.path.join(d0, '.manifest.json'),
                              'w', encoding='utf-8', errors='replace') as g:
                        json.dump({'manifest': manifest,
                                   'doc_mtime': stat.get('doc_mtime', ''),
                                   'mtime_src': stat.get('mtime_src', '')},
                                  g, ensure_ascii=False)
                except OSError:
                    pass  # no sidecar: the next run reads the times again

        entries.append({
            'rel': rel, 'stat': stat, 'summary': summary,
            'headings': headings, 'truncated': truncated,
            'manifest': manifest if md else [],
            'img_ref': 'images/' + sub0.replace(os.sep, '/'),
            'body': md if merge else '',
            'fingerprint': content_fingerprint(md, manifest) if md else '',
            'text_fp': text_fingerprint(md) if md else '',
            'log': report.lines, 'warnings': report.warnings,
        })

        # remove picture folders left empty (failed files, documents without pictures)
        try:
            d = os.path.join(img_root, sub0)
            while os.path.isdir(d) and not os.listdir(d) \
                    and os.path.abspath(d) != os.path.abspath(img_root):
                os.rmdir(d)
                d = os.path.dirname(d)
        except OSError:
            pass  # a folder that cannot be removed stays

        # write the report after every document, so a cancelled run keeps what it finished
        write_batch_report(entries, md_dir, ts, src_root, skipped, legacy,
                           filtered=filtered, since=since)

    dup_groups = []
    if not getattr(args, 'no_dedup', False):
        entries = add_byte_duplicates(entries, dups)
        entries, dup_groups = dedup_entries(entries, md_dir, img_root, Report(), merge=merge)

    # across runs: text already in the fingerprint file is not written again (incremental mode)
    moved = []
    fp_db = getattr(args, 'fp_db', None)
    if fp_db:
        entries, moved = drop_known_entries(entries, load_fp_db(fp_db), md_dir, img_root)

    ts_end = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
    rp = write_batch_report(entries, md_dir, ts_end, src_root, skipped, legacy,
                            filtered=filtered, since=since,
                            dup_groups=dup_groups, moved=moved)
    written = sum(1 for e in entries if e['stat']['status'] in (OK, SKIPPED))
    file_of, finish = {}, None
    if writer:
        out_name = args.merge_name or safe_name(os.path.basename(src_root))
        ip, file_of, finish = call_writer(writer, entries, md_dir, ts_end, src_root, out_name)
        print(T('written_n', written))
    elif merge:
        out_name = args.merge_name or safe_name(os.path.basename(src_root))
        ip = write_merged(entries, md_dir, ts_end, src_root, out_name, moved=moved)
        print(T('written_n', written))
    else:
        ip = write_index(entries, md_dir, ts_end, src_root)
    if fp_db:
        kb_name = os.path.basename(ip) if (merge or writer) and ip else None
        rows = [(e['text_fp'], e['rel'], file_of.get(e['rel']) or kb_name or e['stat']['md'])
                for e in entries
                if e['stat']['status'] == OK and e.get('text_fp')]
        # moved documents get a line too: the path points to the newest location
        rows += [(m['fp'], m['rel'], m['kb_file']) for m in moved]
        try:
            append_fp_db(fp_db, rows)
        except OSError as e:
            log.error('%s', T('fpdb_failed', e))
    write_manifest_all(entries, md_dir, images_saved=not getattr(args, 'no_images', False))

    ok_n = sum(1 for e in entries if e['stat']['status'] == OK)
    fail_n = sum(1 for e in entries if e['stat']['status'] == FAILED)
    tot_c = sum(e['stat']['chars'] for e in entries)
    tot_i = sum(e['stat']['imgs'] for e in entries)
    n_dup = sum(len(g['others']) for g in dup_groups)

    print('')
    print(T('batch_done', md_dir))
    print(T('batch_counts', ok_n, len(skipped), fail_n, len(legacy)))
    if filtered:
        print(T('excluded_n', since, len(filtered)))
    if dup_groups:
        print(T('dups_n', n_dup, len(dup_groups)))
    if moved:
        print(T('moved_n', len(moved)))
    print(T('kb_total', written))
    print(T('totals', tot_c, tot_i))
    print(T('report_at', os.path.basename(rp)))
    if writer:
        if finish:
            finish(rp)
    else:
        print(T('merged_at' if merge else 'index_at', os.path.basename(ip)))
    print_stats(new=ok_n, kept=len(skipped), failed=fail_n, legacy=len(legacy),
                written=written, dup=n_dup, moved=len(moved), chars=tot_c, images=tot_i)
    # failed files, tab-separated, so doc2kb.sh can schedule retries
    for e in entries:
        if e['stat']['status'] == FAILED:
            print('FAILED\t%s' % e['rel'])
    return 0


# ================================================================ named files

def run_files(args, writer=None):
    """Process one or more named files; a zip or tar archive stands for the documents in it.
    Merged mode: one md named after the folder of the first file.
    Otherwise one md per document. writer, when given, replaces both: see call_writer()."""
    cache = ArchiveCache()
    try:
        return _run_files(args, cache, writer)
    finally:
        cache.cleanup()


def _run_files(args, cache, writer=None):
    paths = []
    if args.files:
        with open(args.files, encoding='utf-8') as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    paths.append(ln)
    elif args.input:
        paths = [args.input]

    since = getattr(args, 'since', None)
    if since and not DATE_RE.match(since):
        log.error('%s', T('bad_date', since))
        return 2

    items = []
    for p in paths:
        if not os.path.isfile(p):
            log.error('%s', T('file_missing', p))
            continue
        ext = os.path.splitext(p)[1].lower()
        name = fs_text(os.path.basename(p))
        kind = archive_kind(p)
        if kind in ('zip', 'tar'):
            found, old = expand_archive(os.path.abspath(p), name, cache)
            for it in old:
                log.warning('%s', T('skip_legacy', it['rel']))
            items.extend(found)
            continue
        if kind == 'other':
            log.warning('%s', T('arc_unsupported', ext))
            continue
        if ext in LEGACY_EXT:
            log.warning('%s', T('skip_legacy', p))
            continue
        if ext not in SUPPORTED_EXT:
            log.warning('%s', T('skip_unsupported', p))
            continue
        items.append({'path': os.path.abspath(p), 'rel': name,
                      'size': os.path.getsize(p), 'ext': ext})

    filtered = []
    if since:
        items, filtered = _date_filter(items, since,
                                       getattr(args, 'time_basis', 'doc'), cache)
    valid = [it['path'] for it in items]

    if not valid:
        if filtered:
            log.error('%s', T('none_by_date_files', len(filtered), since))
        else:
            log.error('%s', T('none_files'))
        return 2

    merge = getattr(args, 'merge', False)
    src_root = os.path.dirname(valid[0])

    if args.outdir:
        md_dir = os.path.abspath(args.outdir)
    elif merge or len(valid) > 1:
        md_dir = os.path.join(src_root, T('out_dir_suffix'))
    else:
        base0 = safe_name(os.path.splitext(os.path.basename(valid[0]))[0])
        md_dir = os.path.join(src_root, base0 + T('out_dir_suffix'))
    os.makedirs(md_dir, exist_ok=True)
    img_root = os.path.join(md_dir, 'images')

    entries = []
    bases = base_names(items)
    dups = {}
    if not getattr(args, 'no_dedup', False):
        dups = byte_duplicates(items, cache)
    dup_of = {o['rel']: k for k, others in dups.items() for o in others}
    total = len(items)

    for i, (it, base) in enumerate(zip(items, bases), 1):
        rel = it['rel']
        if rel in dup_of:
            print(T('skip_dup', i, total, dup_of[rel], rel), flush=True)
            continue
        print('[%d/%d] %s' % (i, total, rel), flush=True)
        if it.get('error'):
            entries.append(failed_entry(rel, it['path'], base, it['error']))
            continue
        try:
            src = item_source(it, cache)
        except ArchiveError as e:
            entries.append(failed_entry(rel, it['path'], base, T('arc_bad', e)))
            continue

        report = Report()
        report.log(T('log_source', fs_text(
            os.path.join(os.path.dirname(it['path']), rel) if it.get('arc') else src)))
        report.log(T('log_output', base + '.md'))
        report.log('')

        stat, md, manifest = process_one(
            src, md_dir, report, args,
            img_root=img_root, rel_dir='', md_basename=base)

        summary, headings, truncated = ('', [], False)
        if md:
            summary, headings, truncated = extract_outline(md)

        entries.append({
            'rel': rel, 'stat': stat, 'summary': summary,
            'headings': headings, 'truncated': truncated,
            'manifest': manifest if md else [],
            'img_ref': 'images/' + safe_component(base),
            'body': md if merge else '',
            'fingerprint': content_fingerprint(md, manifest) if md else '',
            'log': report.lines, 'warnings': report.warnings,
        })

        # remove a picture folder left empty
        try:
            d = os.path.join(img_root, safe_component(base))
            if os.path.isdir(d) and not os.listdir(d):
                os.rmdir(d)
        except OSError:
            pass  # a folder that cannot be removed stays

    dup_groups = []
    if not getattr(args, 'no_dedup', False):
        entries = add_byte_duplicates(entries, dups)
        entries, dup_groups = dedup_entries(entries, md_dir, img_root, Report(), merge=merge)

    ts_end = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
    rp = write_batch_report(entries, md_dir, ts_end, src_root, [], [],
                            filtered=filtered, since=since,
                            dup_groups=dup_groups)
    finish = None
    if writer:
        out_name = args.merge_name or safe_name(os.path.basename(src_root))
        ip, _, finish = call_writer(writer, entries, md_dir, ts_end, src_root, out_name)
    elif merge:
        out_name = args.merge_name or safe_name(os.path.basename(src_root))
        ip = write_merged(entries, md_dir, ts_end, src_root, out_name)
    else:
        ip = write_index(entries, md_dir, ts_end, src_root) if len(entries) > 1 else None
    write_manifest_all(entries, md_dir, images_saved=not getattr(args, 'no_images', False))

    ok_n = sum(1 for e in entries if e['stat']['status'] == OK)
    fail_n = sum(1 for e in entries if e['stat']['status'] == FAILED)
    tot_c = sum(e['stat']['chars'] for e in entries)
    tot_i = sum(e['stat']['imgs'] for e in entries)
    n_dup = sum(len(g['others']) for g in dup_groups)

    print('')
    print(T('files_done', md_dir))
    print(T('files_counts', ok_n, fail_n))
    if filtered:
        print(T('excluded_n', since, len(filtered)))
    if dup_groups:
        print(T('dups_n', n_dup, len(dup_groups)))
    print(T('totals', tot_c, tot_i))
    print(T('report_at', os.path.basename(rp)))
    if writer:
        if finish:
            finish(rp)
    elif ip:
        print(T('merged_at' if merge else 'index_at', os.path.basename(ip)))
    print_stats(new=ok_n, failed=fail_n, dup=n_dup, chars=tot_c, images=tot_i)
    return 0 if ok_n else 4


# ================================================================ entry point

def default_lang():
    lang = os.environ.get('DOC2KB_LANG', '').lower()
    return lang if lang in LANGS else 'en'


def build_parser():
    """Every command-line option; other tools add theirs and pass the result to run_batch() or
    run_files()."""
    ap = argparse.ArgumentParser(description='Convert Office, PDF and mail files to Markdown for a knowledge base.')
    ap.add_argument('input', nargs='?',
                    help='input file (.docx/.xlsx/.pptx/.pdf/.eml, or a .zip/.tar[.gz|.bz2|.xz] '
                         'archive holding them)')
    ap.add_argument('-o', '--outdir', default=None, help='output folder')
    ap.add_argument('--lang', choices=LANGS, default=default_lang(),
                    help='language of labels in the output (default: environment variable DOC2KB_LANG, else en)')
    ap.add_argument('--no-inline', action='store_true',
                    help='do not expand embedded workbooks into the text')
    ap.add_argument('--files', metavar='FILE', default=None,
                    help='read the paths to process from a list file, one per line')
    ap.add_argument('--merge', action='store_true',
                    help='write the index and every text into one md')
    ap.add_argument('--merge-name', default=None,
                    help='output file name in merged mode (without extension)')
    ap.add_argument('--no-images', action='store_true',
                    help='do not export picture files; the anchors stay in the text')
    ap.add_argument('--sheets', action='store_true',
                    help='tile extracted pictures 4x4 into contact sheets (needs ImageMagick)')
    ap.add_argument('--batch', metavar='DIR', default=None,
                    help='batch mode: process every document in this folder')
    ap.add_argument('--no-recursive', action='store_true',
                    help='batch mode: this folder only, no subfolders')
    ap.add_argument('--scan-only', action='store_true',
                    help='batch mode: print the file list only, process nothing')
    ap.add_argument('--only', metavar='FILE', default=None,
                    help='batch mode: process only the relative paths listed in this file')
    ap.add_argument('-D', '--no-dedup', action='store_true',
                    help='keep every copy (by default files with the same bytes or the same text '
                         'are kept once)')
    ap.add_argument('--time-basis', choices=['doc', 'newest'], default='doc',
                    help='what --since compares: doc = the document\'s own time (default); '
                         'newest = the latest of file system and document times, '
                         'which also finds files newly copied in')
    ap.add_argument('--since', metavar='DATE', default=None,
                    help='only documents changed on or after DATE, '
                         'YYYY-MM-DD or "YYYY-MM-DD HH:MM"')
    ap.add_argument('--fp-db', metavar='FILE', default=None,
                    help='batch mode: text fingerprint file. Documents whose text is already in it '
                         'are not written again (only their paths are listed); new ones are '
                         'appended. Incremental runs use it so moved folders are not filed twice')
    ap.add_argument('--force', action='store_true',
                    help='batch mode: process everything again, ignoring existing results')
    ap.add_argument('--log-level', default=os.environ.get('LOG_LEVEL', 'WARNING').upper(),
                    type=str.upper, choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
                    help='default: environment variable LOG_LEVEL, else WARNING')
    return ap


def main(argv=None):
    # file names may hold bytes that are not UTF-8 (GBK names from Windows); printing them
    # would raise UnicodeEncodeError, so the streams replace what they cannot encode
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(errors='replace')
        except (AttributeError, ValueError):
            pass  # a stream that cannot be reconfigured keeps its encoding

    ap = build_parser()
    args = ap.parse_args(argv)
    logging.basicConfig(level=args.log_level, format=LOG_FORMAT, stream=sys.stderr)
    set_lang(args.lang)

    if args.batch:
        return run_batch(args)
    if not args.input and not args.files:
        ap.error('give an input file, a list with --files, or a folder with --batch')
    return run_files(args)


if __name__ == '__main__':
    sys.exit(main())
