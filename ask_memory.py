#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ask_memory  --  找文件问 AI 的记忆

纯 Python 3 标准库实现，由 ask_local_files.py 导入使用。记忆是本机的一个 SQLite 文件
（默认 ~/.local/share/ask-local-files/memory.sqlite，XDG_DATA_HOME 生效，权限 600），只当前用户可读：

    files   提问时选过的文件：路径、大小、修改时间、字节数、最近使用时间
    paras   这些文件取出的文字，按段落存放（空行分段，超过 PARA_CHARS 字的再按行、按句切开）
    facts   核对通过的结论：回答里带（文件 N：「摘句」）的句子去掉出处后的结论、原句、所在文件、
            提问、被重复得出的次数、首次与最近时间、来自哪几条回答
    rejected  使用者点了「不对」的结论（文件、原句、结论），之后不再记入

检索：提问切成关键字（标点、空格和常见虚词分开；中日韩文字连续 SHORT_RUN 字以内为一个关键字，
更长的取相邻两字），SQLite 有 FTS5 时用全文索引找候选，没有时用 LIKE；候选按命中的关键字数排序，
结论另按被重复得出的次数和新近程度加分，少于最好一条一半关键字的不要。
取用前检查候选文件：不在了就忘掉它的段落和结论；大小或修改时间变了就重新取文字，原句已不在新文字里的结论删除。
结论排在前面并带上原句所在的段落，其余为相关段落，按文件归组，合计不超过给定字数。

容量：文字超过 max_bytes（默认 200 MB）时，先忘掉最久没用的文件及其结论。
数据库打不开或版本不符时改名留存，另建新库。
"""

import json
import logging
import math
import os
import re
import sqlite3
import threading
import time

log = logging.getLogger('ask_memory')

SCHEMA = 1
PARA_CHARS = 800            # 段落超过这么多字时再切开
MAX_BYTES = 200 * 1024 * 1024
CANDIDATES = 200            # 每类最多取多少条候选再排序
MAX_KEYWORDS = 16
SHORT_RUN = 4               # 中日韩文字连续这么多字以内为一个关键字，更长的取相邻两字
MAX_ANSWER_IDS = 20         # 一条结论最多记住来自哪几条回答
FACT_SCAN = 4000            # 不用全文索引时，最多扫描多少条结论
CJK = '぀-ヿ㐀-䶿一-鿿豈-﫿가-힯'
STOP_CJK = sorted('''什么时候 有没有 是不是 为什么 什么样 怎么样 是什么 关于 什么 怎么 怎样 如何 哪些 哪个 哪里 哪儿 多少
    是否 请问 帮我 一下 这个 那个 这些 那些 我们 你们 他们 现在 目前 一些 情况 相关 有关 内容 资料 文件 记录
    介绍 讲讲 解释 说说 了解 总结 归纳 概括 列出 告诉 的 了 吗 呢 吧 啊 呀 么 是 和 与 及 或 在 有 请 我 你'''.split(),
                  key=len, reverse=True)
STOP_EN = set('''a an the is are was were be of to in on at for and or what who whom when where which why how does
    do did about tell me please find look up explain any there'''.split())
STOP_RE = re.compile('|'.join(map(re.escape, STOP_CJK)))
RUN_RE = re.compile('([%s]+)|((?:(?![%s])[^\\W_])+)' % (CJK, CJK))
CJK_RE = re.compile('[%s]' % CJK)
SENT_END = '。！？；!?;'
CITE_RE = re.compile(r'[（(]?\s*文件\s*(\d+)\s*[:：]\s*「([^「」]+)」\s*[）)]?')
LEAD_RE = re.compile(r'^(?:[-*+>#|]+|\d+[.、)）]|[（(]\d+[)）]|[，,、：:；;。\s*_|])+')
TAIL_RE = re.compile(r'[\s（(，,、：:；;*_|]+$')


def norm(text):
    """比较原句用：去掉全部空白。"""
    return re.sub(r'\s+', '', text)


def quote_parts(quote):
    """摘句按省略号分开，各部分分别核对（与 ask_local_files.check_quotes 一致）。"""
    return [p for p in re.split(r'…+|\.{3,}', norm(quote)) if p]


def quote_in(quote, flat):
    parts = quote_parts(quote)
    return bool(parts) and all(p in flat for p in parts)


def _grams(run):
    return [run] if len(run) < 2 else [run[i:i + 2] for i in range(len(run) - 1)]


def tokens(text):
    """建索引用的词：中日韩文字取相邻两字，其他文字取整词，小写。"""
    out = []
    for cjk, word in RUN_RE.findall(text.lower()):
        out += _grams(cjk) if cjk else [word]
    return out


def keywords(question):
    """提问里的关键字（小写），最多 MAX_KEYWORDS 个；只剩单字时保留单字。"""
    words, singles = [], []
    for piece in STOP_RE.split(question.lower()):
        for cjk, word in RUN_RE.findall(piece):
            if cjk:
                found = [cjk] if len(cjk) <= SHORT_RUN else _grams(cjk)
            elif word not in STOP_EN:
                found = [word]
            else:
                found = []
            for w in found:
                (singles if len(w) < 2 else words).append(w)
    out = []
    for w in words or singles:
        if w not in out:
            out.append(w)
    return out[:MAX_KEYWORDS]


def split_paras(text):
    """按空行分段；超过 PARA_CHARS 字的段按行合并切开，单行过长的再按句末标点切开。"""
    out = []
    for block in re.split(r'\n[ \t]*\n', text):
        block = block.strip('\n')
        if not block.strip():
            continue
        if len(block) <= PARA_CHARS:
            out.append(block)
            continue
        buf = ''
        for line in block.split('\n'):
            for piece in _split_line(line):
                if buf and len(buf) + 1 + len(piece) > PARA_CHARS:
                    out.append(buf)
                    buf = ''
                buf = buf + '\n' + piece if buf else piece
        if buf.strip():
            out.append(buf)
    return out


def _split_line(line):
    if len(line) <= PARA_CHARS:
        return [line]
    out, buf = [], ''
    for s in re.findall(r'[^%s]*[%s]+|[^%s]+$' % (SENT_END, SENT_END, SENT_END), line):
        while len(s) > PARA_CHARS:
            if buf:
                out.append(buf)
                buf = ''
            out.append(s[:PARA_CHARS])
            s = s[PARA_CHARS:]
        if buf and len(buf) + len(s) > PARA_CHARS:
            out.append(buf)
            buf = ''
        buf += s
    if buf:
        out.append(buf)
    return out


def conclusions(answer):
    """回答里的 [(摘句序号 i, 文件号, 摘句, 结论)]：结论为摘句所在句子去掉出处后的文字；
    一句只有出处时用这一行去掉全部出处后的文字。"""
    out, i = [], 0
    for line in answer.split('\n'):
        prev, last = 0, ''
        for m in CITE_RE.finditer(line):
            left = line[prev:m.start()]
            cut = max((left.rfind(c, 0, len(left.rstrip(SENT_END + ' '))) for c in SENT_END), default=-1)
            text = _clean(left[cut + 1:]) or last or _clean(CITE_RE.sub('', line))
            last = text
            out.append((i, int(m.group(1)), m.group(2), text))
            i += 1
            prev = m.end()
    return out


def _clean(text):
    return TAIL_RE.sub('', LEAD_RE.sub('', text.strip())).strip()


class Memory:
    def __init__(self, path, max_bytes=MAX_BYTES):
        self.path = path
        self.max_bytes = max_bytes
        self.lock = threading.RLock()
        self.db = None
        self.fts = False

    # ------------------------------------------------------------ 打开与建库

    def _conn(self):
        if self.db is None:
            self.db = self._open()
        return self.db

    def _open(self):
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        for attempt in (1, 2):
            db = None
            try:
                fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
                os.close(fd)
                db = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
                version = db.execute('PRAGMA user_version').fetchone()[0]
                if version not in (0, SCHEMA):
                    raise sqlite3.DatabaseError('schema %d' % version)
                db.execute('PRAGMA foreign_keys = ON')
                if version == 0:
                    self._create(db)
                self.fts = bool(db.execute("SELECT 1 FROM sqlite_master WHERE name = 'paras_fts'").fetchone())
                return db
            except sqlite3.DatabaseError as e:
                if attempt == 2:
                    raise
                keep = '%s.broken-%s' % (self.path, time.strftime('%Y%m%d%H%M%S'))
                log.warning('memory database unusable (%s), kept as %s', e, keep)
                if db is not None:
                    db.close()
                os.replace(self.path, keep)
        return None

    @staticmethod
    def _create(db):
        db.executescript('''
            CREATE TABLE files (path TEXT PRIMARY KEY, size INTEGER, mtime INTEGER, bytes INTEGER, used REAL);
            CREATE TABLE paras (id INTEGER PRIMARY KEY, path TEXT REFERENCES files(path) ON DELETE CASCADE,
                                n INTEGER, text TEXT, toks TEXT);
            CREATE INDEX paras_path ON paras(path);
            CREATE TABLE facts (id INTEGER PRIMARY KEY, path TEXT REFERENCES files(path) ON DELETE CASCADE,
                                quote TEXT, qkey TEXT, conclusion TEXT, question TEXT, count INTEGER,
                                first REAL, last REAL, answers TEXT, toks TEXT);
            CREATE UNIQUE INDEX facts_key ON facts(path, qkey);
            CREATE TABLE rejected (path TEXT, qkey TEXT, ckey TEXT, PRIMARY KEY (path, qkey, ckey));
        ''')
        try:
            db.execute("CREATE VIRTUAL TABLE paras_fts USING fts5(toks, content='paras', content_rowid='id')")
            db.execute("CREATE VIRTUAL TABLE facts_fts USING fts5(toks, content='facts', content_rowid='id')")
        except sqlite3.OperationalError as e:
            log.info('no FTS5 in this SQLite, using LIKE: %s', e)
        db.execute('PRAGMA user_version = %d' % SCHEMA)
        db.commit()

    def close(self):
        with self.lock:
            if self.db is not None:
                self.db.close()
                self.db = None

    # ------------------------------------------------------------ 写入

    def _add_para(self, db, path, n, text):
        toks = ' '.join(tokens(text))
        cur = db.execute('INSERT INTO paras (path, n, text, toks) VALUES (?, ?, ?, ?)', (path, n, text, toks))
        if self.fts:
            db.execute('INSERT INTO paras_fts (rowid, toks) VALUES (?, ?)', (cur.lastrowid, toks))

    def _drop_paras(self, db, path):
        if self.fts:
            for pid, toks in db.execute('SELECT id, toks FROM paras WHERE path = ?', (path,)).fetchall():
                db.execute("INSERT INTO paras_fts (paras_fts, rowid, toks) VALUES ('delete', ?, ?)", (pid, toks))
        db.execute('DELETE FROM paras WHERE path = ?', (path,))

    def _drop_fact(self, db, fid):
        if self.fts:
            row = db.execute('SELECT toks FROM facts WHERE id = ?', (fid,)).fetchone()
            if row:
                db.execute("INSERT INTO facts_fts (facts_fts, rowid, toks) VALUES ('delete', ?, ?)", (fid, row[0]))
        db.execute('DELETE FROM facts WHERE id = ?', (fid,))

    def _drop_file(self, db, path):
        self._drop_paras(db, path)
        for (fid,) in db.execute('SELECT id FROM facts WHERE path = ?', (path,)).fetchall():
            self._drop_fact(db, fid)
        db.execute('DELETE FROM files WHERE path = ?', (path,))

    def remember_file(self, path, st, text):
        """记住（或更新）一个文件的文字；大小和修改时间没变时只更新最近使用时间。返回是否重写了段落。"""
        with self.lock:
            db = self._conn()
            row = db.execute('SELECT size, mtime FROM files WHERE path = ?', (path,)).fetchone()
            now = time.time()
            if row and row == (st.st_size, st.st_mtime_ns):
                db.execute('UPDATE files SET used = ? WHERE path = ?', (now, path))
                db.commit()
                return False
            self._drop_paras(db, path)
            # 不用 INSERT OR REPLACE：替换会先删行，连带删掉这个文件的结论
            sql = ('UPDATE files SET size = ?, mtime = ?, bytes = ?, used = ? WHERE path = ?' if row else
                   'INSERT INTO files (size, mtime, bytes, used, path) VALUES (?, ?, ?, ?, ?)')
            db.execute(sql, (st.st_size, st.st_mtime_ns, len(text.encode('utf-8')), now, path))
            for n, p in enumerate(split_paras(text)):
                self._add_para(db, path, n, p)
            if row:     # 文件改过：原句已不在新文字里的结论删除
                flat = norm(text)
                for fid, quote in db.execute('SELECT id, quote FROM facts WHERE path = ?', (path,)).fetchall():
                    if not quote_in(quote, flat):
                        self._drop_fact(db, fid)
            self._cap(db)
            db.commit()
            return True

    def _cap(self, db):
        total = db.execute('SELECT COALESCE(SUM(bytes), 0) FROM files').fetchone()[0]
        if total <= self.max_bytes:
            return
        for path, size in db.execute('SELECT path, bytes FROM files ORDER BY used').fetchall():
            if total <= self.max_bytes:
                break
            self._drop_file(db, path)
            total -= size
            log.info('memory over %d bytes, forgot %s', self.max_bytes, path)

    def remember_answer(self, answer_id, question, answer, sources, missing=()):
        """把回答里核对通过的结论记下。sources: {文件号: 路径}，只记这些文件的摘句；
        missing: 核对没通过的 [{'file', 'quote'}]。返回记住的条数。"""
        bad = {(m.get('file'), m.get('quote')) for m in missing}
        found = [(n, q, c) for _, n, q, c in conclusions(answer) if n in sources and (n, q) not in bad and c]
        if not found:
            return 0
        now, kept = time.time(), 0
        with self.lock:
            db = self._conn()
            for n, quote, concl in found:
                path, qkey, ckey = sources[n], norm(quote), norm(concl)
                if not db.execute('SELECT 1 FROM files WHERE path = ?', (path,)).fetchone():
                    continue
                if db.execute('SELECT 1 FROM rejected WHERE path = ? AND qkey = ? AND ckey = ?',
                              (path, qkey, ckey)).fetchone():
                    continue
                row = db.execute('SELECT id, count, answers, toks FROM facts WHERE path = ? AND qkey = ?',
                                 (path, qkey)).fetchone()
                toks = ' '.join(tokens(concl + ' ' + quote))
                if row:
                    fid, count, answers, old = row
                    ids = [a for a in json.loads(answers or '[]') if a != answer_id] + [answer_id]
                    if self.fts:
                        db.execute("INSERT INTO facts_fts (facts_fts, rowid, toks) VALUES ('delete', ?, ?)", (fid, old))
                    db.execute('UPDATE facts SET conclusion = ?, question = ?, count = ?, last = ?, answers = ?, '
                               'toks = ?, quote = ? WHERE id = ?',
                               (concl, question, count + (answer_id not in json.loads(answers or '[]')), now,
                                json.dumps(ids[-MAX_ANSWER_IDS:]), toks, quote, fid))
                else:
                    cur = db.execute('INSERT INTO facts (path, quote, qkey, conclusion, question, count, first, last, '
                                     'answers, toks) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)',
                                     (path, quote, qkey, concl, question, now, now, json.dumps([answer_id]), toks))
                    fid = cur.lastrowid
                if self.fts:
                    db.execute('INSERT INTO facts_fts (rowid, toks) VALUES (?, ?)', (fid, toks))
                kept += 1
            db.commit()
        return kept

    # ------------------------------------------------------------ 忘掉

    def wrong(self, answer_id):
        """「不对」：忘掉这条回答记住的结论，并且之后不再记入同样的结论。返回忘掉的条数。"""
        with self.lock:
            db = self._conn()
            rows = db.execute('SELECT id, path, qkey, conclusion, answers FROM facts').fetchall()
            n = 0
            for fid, path, qkey, concl, answers in rows:
                if answer_id in json.loads(answers or '[]'):
                    db.execute('INSERT OR IGNORE INTO rejected (path, qkey, ckey) VALUES (?, ?, ?)',
                               (path, qkey, norm(concl)))
                    self._drop_fact(db, fid)
                    n += 1
            db.commit()
            return n

    def forget_fact(self, fid):
        with self.lock:
            db = self._conn()
            hit = db.execute('SELECT 1 FROM facts WHERE id = ?', (fid,)).fetchone()
            self._drop_fact(db, fid)
            db.commit()
            return bool(hit)

    def forget_file(self, path):
        with self.lock:
            db = self._conn()
            hit = db.execute('SELECT 1 FROM files WHERE path = ?', (path,)).fetchone()
            self._drop_file(db, path)
            db.commit()
            return bool(hit)

    def clear(self):
        with self.lock:
            db = self._conn()
            for t in ('facts', 'paras', 'files', 'rejected'):
                db.execute('DELETE FROM %s' % t)
            if self.fts:
                db.execute("INSERT INTO paras_fts (paras_fts) VALUES ('delete-all')")
                db.execute("INSERT INTO facts_fts (facts_fts) VALUES ('delete-all')")
            db.commit()
        with self.lock:     # 交还磁盘空间
            self._conn().execute('VACUUM')

    # ------------------------------------------------------------ 查看

    def stats(self):
        with self.lock:
            db = self._conn()
            files, size = db.execute('SELECT COUNT(*), COALESCE(SUM(bytes), 0) FROM files').fetchone()
            facts = db.execute('SELECT COUNT(*) FROM facts').fetchone()[0]
        return {'files': files, 'facts': facts, 'bytes': size, 'max_bytes': self.max_bytes}

    def listing(self, q='', limit=200):
        """记忆列表：结论（新的在前）与文件（最近用的在前），q 不为空时只列含 q 的。"""
        like = '%' + q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        with self.lock:
            db = self._conn()
            facts = db.execute('SELECT id, path, conclusion, quote, count, last FROM facts '
                               "WHERE conclusion LIKE ?1 ESCAPE '\\' OR quote LIKE ?1 ESCAPE '\\' "
                               "OR path LIKE ?1 ESCAPE '\\' ORDER BY last DESC LIMIT ?2", (like, limit)).fetchall()
            files = db.execute('SELECT f.path, f.bytes, f.used, (SELECT COUNT(*) FROM facts WHERE path = f.path) '
                               "FROM files f WHERE f.path LIKE ?1 ESCAPE '\\' ORDER BY f.used DESC LIMIT ?2",
                               (like, limit)).fetchall()
        return {'facts': [{'id': r[0], 'path': r[1], 'conclusion': r[2], 'quote': r[3], 'count': r[4],
                           'last': r[5]} for r in facts],
                'files': [{'path': r[0], 'bytes': r[1], 'used': r[2], 'facts': r[3]} for r in files]}

    # ------------------------------------------------------------ 检索

    def _match(self, db, table, words, exclude):
        """含任一关键字的 [(id, path, 文字)]，最多 CANDIDATES 条。"""
        col = 'text' if table == 'paras' else "conclusion || ' ' || quote"
        extra = ', count, last, quote' if table == 'facts' else ''
        long_words = [w for w in words if len(w) > 1 or not CJK_RE.match(w)]
        if self.fts and long_words and len(long_words) == len(words):
            query = ' OR '.join('"%s"' % ' '.join(tokens(w)).replace('"', '') for w in long_words if tokens(w))
            if not query:
                return []
            sql = ('SELECT t.id, t.path, %s%s FROM %s_fts JOIN %s t ON t.id = %s_fts.rowid WHERE %s_fts MATCH ? '
                   'ORDER BY bm25(%s_fts) LIMIT %d' % (col, extra, table, table, table, table, table, CANDIDATES * 4))
            rows = db.execute(sql, (query,)).fetchall()
        else:
            cond = ' OR '.join(['lower(%s) LIKE ?' % col] * len(words))
            sql = 'SELECT id, path, %s%s FROM %s WHERE %s LIMIT %d' % (col, extra, table, cond, FACT_SCAN)
            rows = db.execute(sql, ['%' + w + '%' for w in words]).fetchall()
        return [r for r in rows if r[1] not in exclude]

    def _fresh(self, db, paths, extract):
        """检查候选文件：不在了就忘掉；改过就重新取文字。返回是否有改动。"""
        changed = False
        for path in paths:
            row = db.execute('SELECT size, mtime FROM files WHERE path = ?', (path,)).fetchone()
            if not row:
                continue
            try:
                st = os.stat(path)
            except OSError:
                self._drop_file(db, path)
                changed = True
                log.info('memory: %s is gone, forgotten', path)
                continue
            if (st.st_size, st.st_mtime_ns) == row:
                continue
            try:
                text = extract(path)
            except Exception as e:      # 转换器对损坏文件可能抛出任何异常
                log.info('memory: cannot read changed %s (%s), forgotten', path, e)
                self._drop_file(db, path)
                changed = True
                continue
            db.commit()
            self.remember_file(path, st, text)
            changed = True
        if changed:
            db.commit()
        return changed

    def recall(self, question, exclude=(), budget=0, extract=None):
        """提问用的记忆：[{'path', 'facts': [{'id', 'conclusion', 'quote', 'count'}], 'paras': [文字]}]，
        结论在前，合计不超过 budget 字（结论与段落都算）。extract(path) 返回文件当前的文字。"""
        words = keywords(question)
        if not words or budget <= 0:
            return []
        exclude = set(exclude)
        with self.lock:
            db = self._conn()
            for attempt in (1, 2):
                facts = self._match(db, 'facts', words, exclude)
                paras = self._match(db, 'paras', words, exclude)
                paths = {r[1] for r in facts} | {r[1] for r in paras}
                if attempt == 1 and extract and self._fresh(db, sorted(paths), extract):
                    continue
                break
            if not facts and not paras:
                return []
            now = time.time()
            hits = lambda text: sum(1 for w in words if w in text.lower())
            scored = []
            for fid, path, text, count, last, quote in facts:
                h = hits(text)
                bonus = math.log(1 + count) + 1.0 / (1 + (now - last) / 86400 / 30)
                scored.append((h, h + bonus, 'fact', fid, path))
            for pid, path, text in paras:
                h = hits(text)
                scored.append((h, h, 'para', pid, path))
            best = max(s[0] for s in scored)
            scored = [s for s in scored if s[0] * 2 >= best and s[0] > 0]
            scored.sort(key=lambda s: (s[2] != 'fact', -s[1]))
            out, used, left = {}, set(), budget
            for _, _, kind, rid, path in scored:
                if kind == 'fact':
                    f = db.execute('SELECT id, conclusion, quote, count FROM facts WHERE id = ?', (rid,)).fetchone()
                    if not f:
                        continue
                    around = self._quote_paras(db, path, f[2])
                    need = [p for p in around if p[0] not in used]
                    cost = len(f[1]) + len(f[2]) + sum(len(p[2]) for p in need)
                    if not around or cost > left:
                        continue
                    block = out.setdefault(path, {'path': path, 'facts': [], 'paras': {}})
                    block['facts'].append({'id': f[0], 'conclusion': f[1], 'quote': f[2], 'count': f[3]})
                    for pid, n, text in need:
                        used.add(pid)
                        block['paras'][n] = text
                    left -= cost
                elif rid not in used:
                    p = db.execute('SELECT n, text FROM paras WHERE id = ?', (rid,)).fetchone()
                    if not p or len(p[1]) > left:
                        continue
                    used.add(rid)
                    out.setdefault(path, {'path': path, 'facts': [], 'paras': {}})['paras'][p[0]] = p[1]
                    left -= len(p[1])
            for path in out:
                db.execute('UPDATE files SET used = ? WHERE path = ?', (now, path))
            db.commit()
        return [{'path': b['path'], 'facts': b['facts'], 'paras': [b['paras'][n] for n in sorted(b['paras'])]}
                for b in out.values()]

    def _quote_paras(self, db, path, quote):
        """原句所在的段落 [(id, n, 文字)]；跨段的原句给出所跨的各段；找不到时返回空列表。"""
        rows = db.execute('SELECT id, n, text FROM paras WHERE path = ? ORDER BY n', (path,)).fetchall()
        parts = quote_parts(quote)
        if not parts:
            return []
        flat, spans, pos = '', [], 0
        for r in rows:
            t = norm(r[2])
            spans.append((pos, pos + len(t), r))
            flat += t
            pos += len(t)
        need, start = [], 0
        for part in parts:
            i = flat.find(part, start)
            if i < 0:
                return []
            j = i + len(part)
            need += [s[2] for s in spans if s[0] < j and s[1] > i and s[2] not in need]
            start = j
        return need
