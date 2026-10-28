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
    topics、points  主题页：由模型把结论按主题整理而成，每条要点带出处文件与原句
    queue   更新队列：新记入或改过的结论（fact）、内容变了的文件（file），等模型更新时处理
    updates 最近 MAX_UPDATES 次更新的记录：方式、各类改动数、改动说明与撤销所需的改动前内容

检索：提问切成关键字（标点、空格和常见虚词分开；中日韩文字连续 SHORT_RUN 字以内为一个关键字，
更长的取相邻两字），SQLite 有 FTS5 时用全文索引找候选，没有时用 LIKE；候选按命中的关键字数排序，
结论另按被重复得出的次数和新近程度加分，少于最好一条一半关键字的不要。
取用前检查候选文件：不在了就忘掉它的段落、结论和要点；大小或修改时间变了就重新取文字，原句已不在新文字里的
结论和要点标为待复核（不拿去参考）并把文件排进更新队列。主题页要点排在最前，其次是结论，都带上原句所在的段落，
其余为相关段落，按文件归组，合计不超过给定字数。

更新（参考 LLM Wiki 的做法，由调用方把 update_prompt 发给模型、把回答交给 apply_update）：
把队列里的内容分批交给模型，按主题整理成主题页，合并重复、以新代旧、删除不再成立的说法，
文件改过的按新文字改写结论和要点；体检把全部结论分批再过一遍。模型给出的结论与要点都须原句核对通过、
不是「不对」过的，才写入；文件改过而模型没能改写成功的结论和要点删除。每次更新记下改动前的内容，可从最近一次往回撤销。

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

SCHEMA = 2
FACT_COLS = ('id', 'path', 'quote', 'qkey', 'conclusion', 'question', 'count', 'first', 'last', 'answers', 'toks', 'stale')
# 检索用的文字与另取的列：结论为「结论 原句」，要点为「主题：要点」；最后一列为是否待复核
MATCH_COLS = {'paras': ('text', ''),
              'facts': ("conclusion || ' ' || quote", ', count, last, quote, stale'),
              'points': ("(SELECT title FROM topics WHERE id = topic) || '：' || text", ', quote, stale')}
POINT_COLS = ('id', 'topic', 'path', 'text', 'quote', 'qkey', 'toks', 'stale')
MAX_UPDATES = 20            # 保留几次更新记录（可撤销）
BATCH_CHARS = 12000         # 每批交给模型的内容至多这么多字（另受发送字数上限限制）
BATCH_FACTS = 40           # 每批至多交给模型多少条结论
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


UPDATE_PROMPT = '''你在维护使用者本机的一份「记忆」：从以前的问答里记下的结论（conclusions），以及按主题整理成的主题页（topics）。
使用者会给你一个 JSON：files 为文件号与路径；conclusions 每条有 id（F 开头）、所在文件号 file、结论 conclusion、
原文摘句 quote、被得出的次数 count，changed 为 true 表示文件改过、原句已不在新文字里；topics 为相关的已有主题页，
每页有 id（T 开头）、标题 title 和要点 points（每条有 file、text、quote，changed 同上）；texts 为改过的文件的新文字（可能只是开头一部分）。

请做三件事：
1. 把结论按主题整理成主题页：同一件事、同一个对象的结论归到一页，标题简短（不超过 20 字），要点一句一条。
   已有的页给出原 id，整页替换为你给出的要点；新页的 id 写 "new"。没必要成页的可以不放。
2. 体检：两条结论说的是同一件事，用 merge 把一条并进另一条；说法冲突、一条被另一条更新的说法取代时，用 replace 以新代旧；
   不再成立或没有价值的用 delete。
3. changed 为 true 的结论：对照 texts 里该文件的新文字，用 rewrite 改写结论并给出新的原文摘句；新文字里找不到依据的用 delete。

规则：每条要点和改写后的结论都必须附文件号 file 和从该文件原文逐字照抄的摘句 quote（可直接用给出的 quote，或从 texts 里照抄），
摘句核对不过的不会写入。只依据给出的内容，不编造，不加入给出内容以外的事实。没提到的结论保持不变。

只输出一个 JSON 对象，不要其他文字，格式：
{"conclusions": [{"id": "F3", "action": "rewrite", "conclusion": "改写后的结论", "file": 1, "quote": "原文摘句"},
                 {"id": "F4", "action": "merge", "into": "F2"}, {"id": "F5", "action": "replace", "into": "F6"},
                 {"id": "F7", "action": "delete"}],
 "topics": [{"id": "T2", "title": "主题", "points": [{"text": "要点", "file": 1, "quote": "原文摘句"}]}],
 "delete_topics": ["T9"]}'''
COUNT_NAMES = (('topics_new', '新建主题页', '页'), ('topics_updated', '更新主题页', '页'),
               ('topics_deleted', '删除主题页', '页'), ('merged', '合并', '条'), ('replaced', '以新代旧', '条'),
               ('rewritten', '改写', '条'), ('deleted', '删除', '条'))


def update_messages(payload):
    """一批更新要发给模型的消息。"""
    return [{'role': 'system', 'content': UPDATE_PROMPT}, {'role': 'user', 'content': payload}]


def parse_plan(reply):
    """模型回答里的 JSON 对象；可带 ```json 围栏或前后说明文字。看不懂时抛 ValueError。"""
    start, end = reply.find('{'), reply.rfind('}')
    try:
        plan = json.loads(reply[start:end + 1]) if 0 <= start < end else None
    except ValueError:
        plan = None
    if not isinstance(plan, dict):
        raise ValueError('模型的回答不是约定的 JSON')
    return plan


def summary(counts):
    """各类改动数写成一句话，如「新建主题页 1 页、合并 2 条」；没有改动时为「没有改动」。"""
    out = ['%s %d %s' % (name, counts[k], unit) for k, name, unit in COUNT_NAMES if counts.get(k)]
    return '、'.join(out) or '没有改动'


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
                if version not in (0, 1, SCHEMA):
                    raise sqlite3.DatabaseError('schema %d' % version)
                db.execute('PRAGMA foreign_keys = ON')
                if version == 0:
                    self._create(db)
                if version < 2:
                    self._upgrade(db)
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
        db.execute('PRAGMA user_version = 1')
        db.commit()

    @staticmethod
    def _upgrade(db):
        """版本 1 升到 2：就地加上主题页、更新队列与更新记录，已有的文件、段落、结论和「不对」记录保留。"""
        db.executescript('''
            ALTER TABLE facts ADD COLUMN stale INTEGER NOT NULL DEFAULT 0;
            CREATE TABLE topics (id INTEGER PRIMARY KEY, title TEXT, updated REAL);
            CREATE TABLE points (id INTEGER PRIMARY KEY, topic INTEGER REFERENCES topics(id) ON DELETE CASCADE,
                                 path TEXT REFERENCES files(path) ON DELETE CASCADE, text TEXT, quote TEXT,
                                 qkey TEXT, toks TEXT, stale INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX points_topic ON points(topic);
            CREATE INDEX points_path ON points(path);
            CREATE TABLE queue (kind TEXT, ref TEXT, added REAL, PRIMARY KEY (kind, ref));
            CREATE TABLE updates (id INTEGER PRIMARY KEY, time REAL, mode TEXT, counts TEXT, changes TEXT, undo TEXT);
        ''')
        if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'paras_fts'").fetchone():
            db.execute("CREATE VIRTUAL TABLE points_fts USING fts5(toks, content='points', content_rowid='id')")
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
        db.execute("DELETE FROM queue WHERE kind = 'fact' AND ref = ?", (str(fid),))

    def _put_fact(self, db, row):
        """按完整的一行（FACT_COLS 的顺序）写入结论，id 相同的先删；返回 id。"""
        if row[0] is not None:
            self._drop_fact(db, row[0])
        cur = db.execute('INSERT INTO facts (%s) VALUES (%s)' % (', '.join(FACT_COLS), ', '.join('?' * len(FACT_COLS))),
                         row)
        if self.fts:
            db.execute('INSERT INTO facts_fts (rowid, toks) VALUES (?, ?)', (cur.lastrowid, row[FACT_COLS.index('toks')]))
        return cur.lastrowid

    def _drop_point(self, db, pid):
        if self.fts:
            row = db.execute('SELECT toks FROM points WHERE id = ?', (pid,)).fetchone()
            if row:
                db.execute("INSERT INTO points_fts (points_fts, rowid, toks) VALUES ('delete', ?, ?)", (pid, row[0]))
        db.execute('DELETE FROM points WHERE id = ?', (pid,))

    def _put_point(self, db, row):
        """按完整的一行（POINT_COLS 的顺序）写入要点，id 为 None 时新建；返回 id。"""
        if row[0] is not None:
            self._drop_point(db, row[0])
        cur = db.execute('INSERT INTO points (%s) VALUES (%s)' % (', '.join(POINT_COLS),
                                                                  ', '.join('?' * len(POINT_COLS))), row)
        if self.fts:
            db.execute('INSERT INTO points_fts (rowid, toks) VALUES (?, ?)',
                       (cur.lastrowid, row[POINT_COLS.index('toks')]))
        return cur.lastrowid

    def _drop_topic(self, db, tid):
        for (pid,) in db.execute('SELECT id FROM points WHERE topic = ?', (tid,)).fetchall():
            self._drop_point(db, pid)
        db.execute('DELETE FROM topics WHERE id = ?', (tid,))

    def _drop_empty_topics(self, db):
        db.execute('DELETE FROM topics WHERE id NOT IN (SELECT DISTINCT topic FROM points WHERE topic IS NOT NULL)')

    def _drop_file(self, db, path):
        self._drop_paras(db, path)
        for (fid,) in db.execute('SELECT id FROM facts WHERE path = ?', (path,)).fetchall():
            self._drop_fact(db, fid)
        for (pid,) in db.execute('SELECT id FROM points WHERE path = ?', (path,)).fetchall():
            self._drop_point(db, pid)
        self._drop_empty_topics(db)
        db.execute("DELETE FROM queue WHERE kind = 'file' AND ref = ?", (path,))
        db.execute('DELETE FROM files WHERE path = ?', (path,))

    @staticmethod
    def _enqueue(db, kind, ref):
        db.execute('INSERT OR IGNORE INTO queue (kind, ref, added) VALUES (?, ?, ?)', (kind, str(ref), time.time()))

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
            if row:     # 文件改过：原句已不在新文字里的结论和要点待复核，不拿去参考，等更新时由模型改写
                flat, stale = norm(text), False
                for table in ('facts', 'points'):
                    for rid, quote in db.execute('SELECT id, quote FROM %s WHERE path = ?' % table, (path,)).fetchall():
                        bad = not quote_in(quote, flat)
                        db.execute('UPDATE %s SET stale = ? WHERE id = ?' % table, (int(bad), rid))
                        stale = stale or bad
                if stale:
                    self._enqueue(db, 'file', path)
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
                               'toks = ?, quote = ?, stale = 0 WHERE id = ?',
                               (concl, question, count + (answer_id not in json.loads(answers or '[]')), now,
                                json.dumps(ids[-MAX_ANSWER_IDS:]), toks, quote, fid))
                    if old != toks:
                        self._enqueue(db, 'fact', fid)
                else:
                    cur = db.execute('INSERT INTO facts (path, quote, qkey, conclusion, question, count, first, last, '
                                     'answers, toks) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)',
                                     (path, quote, qkey, concl, question, now, now, json.dumps([answer_id]), toks))
                    fid = cur.lastrowid
                    self._enqueue(db, 'fact', fid)
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
                    # 由这条结论整理出的同文件、同原句的要点一并删掉
                    for (pid,) in db.execute('SELECT id FROM points WHERE path = ? AND qkey = ?', (path, qkey)).fetchall():
                        self._drop_point(db, pid)
                    n += 1
            self._drop_empty_topics(db)
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
            for t in ('points', 'topics', 'facts', 'paras', 'files', 'rejected', 'queue', 'updates'):
                db.execute('DELETE FROM %s' % t)
            if self.fts:
                for t in ('paras', 'facts', 'points'):
                    db.execute("INSERT INTO %s_fts (%s_fts) VALUES ('delete-all')" % (t, t))
            db.commit()
        with self.lock:     # 交还磁盘空间
            self._conn().execute('VACUUM')

    # ------------------------------------------------------------ 查看

    def stats(self):
        with self.lock:
            db = self._conn()
            files, size = db.execute('SELECT COUNT(*), COALESCE(SUM(bytes), 0) FROM files').fetchone()
            facts = db.execute('SELECT COUNT(*) FROM facts').fetchone()[0]
            topics = db.execute('SELECT COUNT(*) FROM topics').fetchone()[0]
            queued = db.execute('SELECT COUNT(*) FROM queue').fetchone()[0]
        return {'files': files, 'facts': facts, 'topics': topics, 'queued': queued, 'bytes': size,
                'max_bytes': self.max_bytes}

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
            esc = "ESCAPE '\\'"
            topics = db.execute('SELECT t.id, t.title, t.updated FROM topics t WHERE t.title LIKE ?1 {0} '
                                'OR EXISTS (SELECT 1 FROM points p WHERE p.topic = t.id AND (p.text LIKE ?1 {0} '
                                'OR p.quote LIKE ?1 {0} OR p.path LIKE ?1 {0})) '
                                'ORDER BY t.updated DESC LIMIT ?2'.format(esc), (like, limit)).fetchall()
            points = {t[0]: [{'path': r[0], 'text': r[1], 'quote': r[2], 'stale': bool(r[3])} for r in db.execute(
                'SELECT path, text, quote, stale FROM points WHERE topic = ? ORDER BY id', (t[0],)).fetchall()]
                for t in topics}
        return {'facts': [{'id': r[0], 'path': r[1], 'conclusion': r[2], 'quote': r[3], 'count': r[4],
                           'last': r[5]} for r in facts],
                'files': [{'path': r[0], 'bytes': r[1], 'used': r[2], 'facts': r[3]} for r in files],
                'topics': [{'id': t[0], 'title': t[1], 'updated': t[2], 'points': points.get(t[0], [])}
                           for t in topics]}

    def forget_topic(self, tid):
        with self.lock:
            db = self._conn()
            hit = db.execute('SELECT 1 FROM topics WHERE id = ?', (tid,)).fetchone()
            self._drop_topic(db, tid)
            db.commit()
            return bool(hit)

    # ------------------------------------------------------------ 更新：主题页与体检

    def pending(self):
        """更新队列里还有几项。"""
        with self.lock:
            return self._conn().execute('SELECT COUNT(*) FROM queue').fetchone()[0]

    def plan_update(self, full=False):
        """要做的几批：改过的文件每个一批 ('file', 路径)，队里的结论每 BATCH_FACTS 条一批 ('facts', [id])；
        full 时另把全部结论每 BATCH_FACTS 条一批做体检 ('lint', [id])。每批的内容到做时再取。"""
        with self.lock:
            db = self._conn()
            files = [r[0] for r in db.execute("SELECT ref FROM queue WHERE kind = 'file' ORDER BY added, ref")]
            # 待复核的结论由文件那一批处理
            queued = [int(r[0]) for r in db.execute(
                "SELECT q.ref FROM queue q LEFT JOIN facts f ON f.id = CAST(q.ref AS INTEGER) "
                "WHERE q.kind = 'fact' AND COALESCE(f.stale, 0) = 0 ORDER BY q.added, q.ref")]
            every = [r[0] for r in db.execute('SELECT id FROM facts WHERE stale = 0 ORDER BY path, id')] if full else []
        chunks = lambda ids: [ids[i:i + BATCH_FACTS] for i in range(0, len(ids), BATCH_FACTS)]
        return ([('file', p) for p in files] + [('facts', c) for c in chunks(queued)] +
                [('lint', c) for c in chunks(every)])

    def _flat(self, db, path):
        return ''.join(norm(r[0]) for r in db.execute('SELECT text FROM paras WHERE path = ? ORDER BY n', (path,)))

    def update_batch(self, unit, budget):
        """取一批要交给模型的内容，返回 (要发的 JSON 文字, 上下文)；这一批已没什么可做时返回 (None, 上下文)，
        上下文仍交给 apply_update 以便移出队列。合计不超过 budget 字。"""
        kind, ref = unit
        budget = min(budget, BATCH_CHARS)
        ctx = {'kind': kind, 'files': {}, 'facts': {}, 'points': {}, 'topics': {}, 'consumed': []}
        payload = {'files': {}, 'conclusions': [], 'topics': []}

        def fno(path):
            for n, p in ctx['files'].items():
                if p == path:
                    return n
            n = len(ctx['files']) + 1
            ctx['files'][n] = path
            payload['files'][str(n)] = path
            return n

        def size():
            return len(json.dumps(payload, ensure_ascii=False))

        def add_fact(row):
            f = dict(zip(FACT_COLS, row))
            item = {'id': 'F%d' % f['id'], 'file': fno(f['path']), 'conclusion': f['conclusion'], 'quote': f['quote'],
                    'count': f['count']}
            if f['stale']:
                item['changed'] = True
            payload['conclusions'].append(item)
            if size() > budget:
                payload['conclusions'].pop()
                return False
            ctx['facts'][f['id']] = f
            return True

        def add_topic(db, tid):
            if tid in ctx['topics']:
                return True
            title = db.execute('SELECT title FROM topics WHERE id = ?', (tid,)).fetchone()
            if not title:
                return True
            rows = db.execute('SELECT %s FROM points WHERE topic = ? ORDER BY id' % ', '.join(POINT_COLS),
                              (tid,)).fetchall()
            pts = []
            for r in rows:
                pt = dict(zip(POINT_COLS, r))
                item = {'id': 'P%d' % pt['id'], 'file': fno(pt['path']), 'text': pt['text'], 'quote': pt['quote']}
                if pt['stale']:
                    item['changed'] = True
                pts.append(item)
            payload['topics'].append({'id': 'T%d' % tid, 'title': title[0], 'points': pts})
            if size() > budget:
                payload['topics'].pop()
                return False
            ctx['topics'][tid] = title[0]
            for r in rows:
                ctx['points'][r[0]] = dict(zip(POINT_COLS, r))
            return True

        with self.lock:
            db = self._conn()
            cols = ', '.join(FACT_COLS)
            if kind == 'file':
                ctx['consumed'].append(('file', ref))
                stale = db.execute('SELECT %s FROM facts WHERE path = ? AND stale = 1 ORDER BY id' % cols,
                                   (ref,)).fetchall()
                tids = [r[0] for r in db.execute('SELECT DISTINCT topic FROM points WHERE path = ? AND stale = 1',
                                                 (ref,))]
                if not stale and not tids:
                    return None, ctx
                fno(ref)
                payload['texts'] = {'1': ''}
                for row in stale:
                    add_fact(row)
                for tid in tids:
                    add_topic(db, tid)
                # 文件的新文字放在最后，用剩下的字数，按段落取
                left = budget - size()
                text = []
                for (para,) in db.execute('SELECT text FROM paras WHERE path = ? ORDER BY n', (ref,)):
                    if len(para) + 2 > left:
                        break
                    text.append(para)
                    left -= len(para) + 2
                payload['texts']['1'] = '\n\n'.join(text)
            else:
                ids = list(ref)
                rows = [db.execute('SELECT %s FROM facts WHERE id = ? AND stale = 0' % cols, (i,)).fetchone()
                        for i in ids]
                rows = [r for r in rows if r]
                done = []
                for r in rows:
                    if not add_fact(r):
                        break
                    done.append(r[0])
                if kind == 'facts':     # 已不在或待复核的移出队列；放不下的留在队里，下一批再做
                    there = {r[0] for r in rows}
                    ctx['consumed'] = [('fact', str(i)) for i in ids if i in done or i not in there]
                if not done:
                    return None, ctx
                # 相关的主题页与其他结论：按这批结论的关键字找，至多用掉剩下字数的一半
                words = keywords(' '.join(ctx['facts'][i]['conclusion'] for i in done))
                cap = size() + (budget - size()) // 2
                for pid, path, *_ in self._match(db, 'points', words, ()):
                    tid = db.execute('SELECT topic FROM points WHERE id = ?', (pid,)).fetchone()
                    if size() >= cap or not tid or not add_topic(db, tid[0]):
                        break
                for fid, *_ in self._match(db, 'facts', words, ()):
                    if fid in ctx['facts']:
                        continue
                    row = db.execute('SELECT %s FROM facts WHERE id = ?' % cols, (fid,)).fetchone()
                    if size() >= budget or not row or not add_fact(row):
                        break
        return json.dumps(payload, ensure_ascii=False), ctx

    def apply_update(self, ctx, reply, mode, run=None):
        """按模型的回答（UPDATE_PROMPT 约定的 JSON）改写记忆，返回 (更新记录 id, 各类改动数, 改动说明)。
        结论与要点都须原句核对通过、不是「不对」过的才写入；这一批里文件改过而没改写成功的删除。
        有改动时记进更新记录：run 为同一次更新的记录 id 时并入该记录，否则新建一条。回答看不懂时抛 ValueError。"""
        plan = {}
        if reply is not None:
            plan = parse_plan(reply)
        counts, changes, undo, seen = {}, [], [], set()
        bump = lambda k: counts.__setitem__(k, counts.get(k, 0) + 1)
        now = time.time()
        with self.lock:
            db = self._conn()
            flats = {}

            def flat(path):
                if path not in flats:
                    flats[path] = self._flat(db, path)
                return flats[path]

            def snap(t, rid):
                if (t, rid) in seen:
                    return
                seen.add((t, rid))
                cols = {'fact': FACT_COLS, 'point': POINT_COLS, 'topic': ('id', 'title', 'updated')}[t]
                row = db.execute('SELECT %s FROM %ss WHERE id = ?' % (', '.join(cols), t), (rid,)).fetchone()
                undo.append({'t': t, 'id': rid, 'row': list(row) if row else None})

            def ok_quote(path, quote, text):
                return (path and quote and text and quote_in(quote, flat(path)) and not db.execute(
                    'SELECT 1 FROM rejected WHERE path = ? AND qkey = ? AND ckey = ?',
                    (path, norm(quote), norm(text))).fetchone())

            def path_of(n, default=None):
                try:
                    return ctx['files'].get(int(n), default)
                except (TypeError, ValueError):
                    return default

            def fact_row(fid):
                row = db.execute('SELECT %s FROM facts WHERE id = ?' % ', '.join(FACT_COLS), (fid,)).fetchone()
                return dict(zip(FACT_COLS, row)) if row else None

            def ref_id(text, prefix, known):
                text = str(text or '')
                if text[:1].upper() == prefix and text[1:].isdigit() and int(text[1:]) in known:
                    return int(text[1:])
                return None

            # 结论：改写、合并、以新代旧、删除
            for op in plan.get('conclusions') or []:
                if not isinstance(op, dict):
                    continue
                fid = ref_id(op.get('id'), 'F', ctx['facts'])
                f = fact_row(fid) if fid else None
                action = str(op.get('action') or 'keep')
                if not f or action == 'keep':
                    continue
                if action == 'rewrite':     # 只改写文件改过、原句不在了的结论
                    if not f['stale']:
                        continue
                    concl = _clean(str(op.get('conclusion') or ''))
                    quote = str(op.get('quote') or '').strip()
                    path = path_of(op.get('file'), f['path'])
                    if path != f['path'] or not ok_quote(path, quote, concl):
                        continue
                    other = db.execute('SELECT id FROM facts WHERE path = ? AND qkey = ? AND id != ?',
                                       (path, norm(quote), fid)).fetchone()
                    if other:
                        action, op = 'merge', {'into': 'F%d' % other[0]}
                        ctx['facts'].setdefault(other[0], fact_row(other[0]))
                    else:
                        snap('fact', fid)
                        f.update(conclusion=concl, quote=quote, qkey=norm(quote), stale=0,
                                 toks=' '.join(tokens(concl + ' ' + quote)))
                        self._put_fact(db, [f[c] for c in FACT_COLS])
                        self._enqueue(db, 'fact', fid)      # 改写后的结论之后再整理进主题页
                        bump('rewritten')
                        changes.append('改写：%s → %s' % (ctx['facts'][fid]['conclusion'], concl))
                        continue
                if action in ('merge', 'replace'):
                    into = ref_id(op.get('into'), 'F', ctx['facts'])
                    g = fact_row(into) if into and into != fid else None
                    if not g or g['stale']:
                        continue
                    snap('fact', fid)
                    snap('fact', into)
                    if action == 'merge':
                        ids = json.loads(g['answers'] or '[]')
                        ids += [a for a in json.loads(f['answers'] or '[]') if a not in ids]
                        g.update(count=g['count'] + f['count'], answers=json.dumps(ids[-MAX_ANSWER_IDS:]),
                                 first=min(g['first'], f['first']), last=max(g['last'], f['last']))
                        self._put_fact(db, [g[c] for c in FACT_COLS])
                        bump('merged')
                        changes.append('合并：%s → %s' % (f['conclusion'], g['conclusion']))
                    else:
                        bump('replaced')
                        changes.append('以新代旧：%s → %s' % (f['conclusion'], g['conclusion']))
                    self._drop_fact(db, fid)
                elif action == 'delete':
                    snap('fact', fid)
                    self._drop_fact(db, fid)
                    bump('deleted')
                    changes.append('删除：' + f['conclusion'])

            # 主题页：给了 id 的整页替换，new 的新建（同名的并入已有的那页）
            for page in plan.get('topics') or []:
                if not isinstance(page, dict):
                    continue
                title = _clean(str(page.get('title') or ''))[:60]
                pts, keys = [], set()
                for pt in page.get('points') or []:
                    if not isinstance(pt, dict):
                        continue
                    text, quote = _clean(str(pt.get('text') or '')), str(pt.get('quote') or '').strip()
                    path = path_of(pt.get('file'))
                    key = (path, norm(quote), norm(text))
                    if key in keys or not ok_quote(path, quote, text):
                        continue
                    keys.add(key)
                    pts.append((path, text, quote))
                if not title or not pts:
                    continue
                tid = ref_id(page.get('id'), 'T', ctx['topics'])
                if tid and not db.execute('SELECT 1 FROM topics WHERE id = ?', (tid,)).fetchone():
                    tid = None
                keep = []
                if not tid:
                    same = db.execute('SELECT id FROM topics WHERE title = ?', (title,)).fetchone()
                    if same:
                        tid = same[0]
                        if tid not in ctx['topics']:    # 不在这一批里的同名页：保留原有要点，只添新的
                            keep = [(r[0], r[1], r[2]) for r in db.execute(
                                'SELECT path, text, quote FROM points WHERE topic = ?', (tid,))]
                if tid:
                    now_pts = {(r[0], r[1], norm(r[2])) for r in db.execute(
                        'SELECT path, qkey, text FROM points WHERE topic = ?', (tid,))}
                    want = {(p, norm(q), norm(t)) for p, t, q in keep + pts}
                    cur = db.execute('SELECT title FROM topics WHERE id = ?', (tid,)).fetchone()
                    if want == now_pts and cur and cur[0] == title:     # 没有变化
                        continue
                    snap('topic', tid)
                    for (pid,) in db.execute('SELECT id FROM points WHERE topic = ?', (tid,)).fetchall():
                        snap('point', pid)
                        self._drop_point(db, pid)
                    db.execute('UPDATE topics SET title = ?, updated = ? WHERE id = ?', (title, now, tid))
                    bump('topics_updated')
                    changes.append('更新主题页：' + title)
                else:
                    tid = db.execute('INSERT INTO topics (title, updated) VALUES (?, ?)', (title, now)).lastrowid
                    seen.add(('topic', tid))
                    undo.append({'t': 'topic', 'id': tid, 'row': None})
                    bump('topics_new')
                    changes.append('新建主题页：' + title)
                have = {(p, norm(q), norm(t)) for p, t, q in keep}
                for path, text, quote in keep + [p for p in pts if (p[0], norm(p[2]), norm(p[1])) not in have]:
                    pid = self._put_point(db, [None, tid, path, text, quote, norm(quote),
                                               ' '.join(tokens(title + ' ' + text + ' ' + quote)), 0])
                    seen.add(('point', pid))
                    undo.append({'t': 'point', 'id': pid, 'row': None})
            for ref in plan.get('delete_topics') or []:
                tid = ref_id(ref, 'T', ctx['topics'])
                row = db.execute('SELECT title FROM topics WHERE id = ?', (tid,)).fetchone() if tid else None
                if not row:
                    continue
                snap('topic', tid)
                for (pid,) in db.execute('SELECT id FROM points WHERE topic = ?', (tid,)).fetchall():
                    snap('point', pid)
                self._drop_topic(db, tid)
                bump('topics_deleted')
                changes.append('删除主题页：' + row[0])

            # 文件改过而没改写成功的结论和要点删除；删光要点的主题页一并删除
            if ctx['kind'] == 'file':
                path = ctx['consumed'][0][1]
                for fid, concl in db.execute('SELECT id, conclusion FROM facts WHERE path = ? AND stale = 1',
                                             (path,)).fetchall():
                    snap('fact', fid)
                    self._drop_fact(db, fid)
                    bump('deleted')
                    changes.append('删除（文件已改，原句不在了）：' + concl)
                for (pid,) in db.execute('SELECT id FROM points WHERE path = ? AND stale = 1', (path,)).fetchall():
                    snap('point', pid)
                    self._drop_point(db, pid)
            for tid, title in db.execute('SELECT id, title FROM topics WHERE id NOT IN '
                                         '(SELECT DISTINCT topic FROM points WHERE topic IS NOT NULL)').fetchall():
                snap('topic', tid)
                db.execute('DELETE FROM topics WHERE id = ?', (tid,))
                bump('topics_deleted')
                changes.append('删除主题页（要点都已删除）：' + title)
            for kind, ref in ctx['consumed']:
                db.execute('DELETE FROM queue WHERE kind = ? AND ref = ?', (kind, str(ref)))
            if undo:
                run = self._record(db, run, mode, counts, changes, undo, now)
            db.commit()
        return run, counts, changes

    @staticmethod
    def _record(db, run, mode, counts, changes, undo, now):
        row = db.execute('SELECT counts, changes, undo FROM updates WHERE id = ?', (run,)).fetchone() if run else None
        if row:
            old = json.loads(row[0])
            for k, v in counts.items():
                old[k] = old.get(k, 0) + v
            db.execute('UPDATE updates SET time = ?, counts = ?, changes = ?, undo = ? WHERE id = ?',
                       (now, json.dumps(old), json.dumps(json.loads(row[1]) + changes, ensure_ascii=False),
                        json.dumps(json.loads(row[2]) + [undo], ensure_ascii=False), run))
        else:
            run = db.execute('INSERT INTO updates (time, mode, counts, changes, undo) VALUES (?, ?, ?, ?, ?)',
                             (now, mode, json.dumps(counts), json.dumps(changes, ensure_ascii=False),
                              json.dumps([undo], ensure_ascii=False))).lastrowid
        db.execute('DELETE FROM updates WHERE id NOT IN (SELECT id FROM updates ORDER BY id DESC LIMIT %d)'
                   % MAX_UPDATES)
        return run

    def history(self):
        """更新记录，新的在前：[{'id', 'time', 'mode', 'counts', 'changes'}]。"""
        with self.lock:
            rows = self._conn().execute('SELECT id, time, mode, counts, changes FROM updates ORDER BY id DESC').fetchall()
        return [{'id': r[0], 'time': r[1], 'mode': r[2], 'counts': json.loads(r[3]), 'changes': json.loads(r[4])}
                for r in rows]

    def undo_update(self, run):
        """撤销最近一次更新（run 须为最近一次的 id）：把这次改过的结论、要点和主题页恢复到更新之前。
        已不在记忆里的文件、「不对」过的结论不恢复。返回是否撤销了。"""
        with self.lock:
            db = self._conn()
            last = db.execute('SELECT id, undo FROM updates ORDER BY id DESC LIMIT 1').fetchone()
            if not last or last[0] != run:
                return False
            for undo in reversed(json.loads(last[1])):
                for u in undo:      # 先删掉现在的，再按 主题页、结论、要点 的顺序放回原样
                    {'fact': self._drop_fact, 'point': self._drop_point,
                     'topic': lambda d, i: d.execute('DELETE FROM topics WHERE id = ?', (i,))}[u['t']](db, u['id'])
                for t in ('topic', 'fact', 'point'):
                    for u in undo:
                        row = u['row']
                        if u['t'] != t or not row:
                            continue
                        try:
                            if t == 'topic':
                                db.execute('INSERT INTO topics (id, title, updated) VALUES (?, ?, ?)', row)
                            elif t == 'fact':
                                f = dict(zip(FACT_COLS, row))
                                if not db.execute('SELECT 1 FROM rejected WHERE path = ? AND qkey = ? AND ckey = ?',
                                                  (f['path'], f['qkey'], norm(f['conclusion']))).fetchone():
                                    self._put_fact(db, row)
                            else:
                                self._put_point(db, row)
                        except sqlite3.IntegrityError:      # 所在文件或主题页已不在了
                            continue
            self._drop_empty_topics(db)
            db.execute('DELETE FROM updates WHERE id = ?', (run,))
            db.commit()
            return True

    # ------------------------------------------------------------ 检索

    def _match(self, db, table, words, exclude):
        """含任一关键字的 [(id, path, 文字, …)]，最多 CANDIDATES 条；结论与要点不含待复核的。"""
        col, extra = MATCH_COLS[table]
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
        return [r for r in rows if r[1] not in exclude and not (table != 'paras' and r[-1])]

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
        主题页要点（结论写作「主题：要点」，id 为 p 加编号）在前，其次是结论，合计不超过 budget 字
        （结论与段落都算）；同一原句只用一次。extract(path) 返回文件当前的文字。"""
        words = keywords(question)
        if not words or budget <= 0:
            return []
        exclude = set(exclude)
        with self.lock:
            db = self._conn()
            for attempt in (1, 2):
                points = self._match(db, 'points', words, exclude)
                facts = self._match(db, 'facts', words, exclude)
                paras = self._match(db, 'paras', words, exclude)
                paths = {r[1] for r in points} | {r[1] for r in facts} | {r[1] for r in paras}
                if attempt == 1 and extract and self._fresh(db, sorted(paths), extract):
                    continue
                break
            if not points and not facts and not paras:
                return []
            now = time.time()
            hits = lambda text: sum(1 for w in words if w in text.lower())
            scored = []
            for pid, path, text, quote, _ in points:
                h = hits(text)
                scored.append((h, h, 'point', pid, path))
            for fid, path, text, count, last, quote, _ in facts:
                h = hits(text)
                bonus = math.log(1 + count) + 1.0 / (1 + (now - last) / 86400 / 30)
                scored.append((h, h + bonus, 'fact', fid, path))
            for pid, path, text in paras:
                h = hits(text)
                scored.append((h, h, 'para', pid, path))
            best = max(s[0] for s in scored)
            scored = [s for s in scored if s[0] * 2 >= best and s[0] > 0]
            order = {'point': 0, 'fact': 1, 'para': 2}
            scored.sort(key=lambda s: (order[s[2]], -s[1]))
            out, used, quotes, left = {}, set(), set(), budget
            for _, _, kind, rid, path in scored:
                if kind in ('point', 'fact'):
                    sql = ("SELECT 'p' || p.id, t.title || '：' || p.text, p.quote, 1 FROM points p "
                           'JOIN topics t ON t.id = p.topic WHERE p.id = ?' if kind == 'point' else
                           'SELECT id, conclusion, quote, count FROM facts WHERE id = ?')
                    f = db.execute(sql, (rid,)).fetchone()
                    if not f or (path, norm(f[2])) in quotes:
                        continue
                    around = self._quote_paras(db, path, f[2])
                    need = [p for p in around if p[0] not in used]
                    cost = len(f[1]) + len(f[2]) + sum(len(p[2]) for p in need)
                    if not around or cost > left:
                        continue
                    block = out.setdefault(path, {'path': path, 'facts': [], 'paras': {}})
                    block['facts'].append({'id': f[0], 'conclusion': f[1], 'quote': f[2], 'count': f[3]})
                    quotes.add((path, norm(f[2])))
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
