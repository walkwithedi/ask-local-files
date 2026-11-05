#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用配置的模型跑预装 skill 的评测场景（tests/skill_evals/<skill>.json），写成 Markdown 报告，供修改 skill 前后对比。

每个场景起一份独立的资料管家服务（临时家目录，只含场景里的文件），按页面的做法选文件、选 skill、提问，
记下回答、工具调用与自动检查（要调用的工具、回答应含与不应含的词）；「人工核对要点」留给人看。
只用 Python 3 标准库。用法：
    skill_eval_run.py --base 接口地址 --model 模型名 [--key AK] [--skill 名称 ...] [--out 报告.md]
接口地址等也可由环境变量 ASK_EVAL_BASE、ASK_EVAL_MODEL、ASK_EVAL_KEY 给出。退出码 0 为跑完（不论检查是否通过），2 为启动失败。
"""

import argparse
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

LOG_FORMAT = '%(asctime)s [%(levelname)s] %(name)s.%(funcName)s:%(lineno)d - %(message)s'
log = logging.getLogger('skill_eval_run')
TOOL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVALS = os.path.join(TOOL, 'tests', 'skill_evals')
NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def call(base, token, path, body=None, timeout=600):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode('utf-8') if body is not None else None,
                                 headers={'X-Token': token, 'Content-Type': 'application/json'},
                                 method='POST' if body is not None else 'GET')
    try:
        with NO_PROXY.open(req, timeout=timeout) as r:
            return r.status, r.read().decode('utf-8')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8')


def run_scenario(skill, sc, model):
    """起服务、提问，返回 {'answer', 'tools', 'error'}。"""
    home = tempfile.mkdtemp(prefix='skill-eval-')
    proc = None
    try:
        docs = os.path.join(home, '资料')
        os.makedirs(docs)
        paths = []
        for rel, text in sc['files'].items():
            p = os.path.join(docs, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, 'w', encoding='utf-8') as f:
                f.write(text)
            paths.append(p)
        conf = os.path.join(home, '.config', 'ask-local-files')
        os.makedirs(conf)
        with open(os.path.join(conf, 'config.json'), 'w', encoding='utf-8') as f:
            json.dump({'folders': [docs], 'custom': model, 'model': 'custom', 'memory': False}, f, ensure_ascii=False)
        env = dict(os.environ, HOME=home, XDG_CONFIG_HOME=os.path.join(home, '.config'),
                   XDG_CACHE_HOME=os.path.join(home, '.cache'), LOG_LEVEL='ERROR', DISPLAY='', WAYLAND_DISPLAY='',
                   ASK_LOCAL_FILES_DATA=os.path.join(home, 'data'))  # 不在工具目录里建「数据」、写 data-dir.txt
        port = free_port()
        proc = subprocess.Popen([sys.executable, os.path.join(TOOL, 'ask_local_files.py'), '--no-browser', '--port',
                                 str(port)], env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        token = None
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and token is None:
            line = proc.stdout.readline()
            if '?t=' in line:
                token = line.strip().split('?t=', 1)[1]
        if not token:
            return {'answer': '', 'tools': [], 'error': '服务没有启动'}
        base = 'http://127.0.0.1:%d' % port
        body = {'files': paths, 'messages': [{'role': 'user', 'content': sc['question']}], 'model': 'custom',
                'skill': skill, 'memory': False}
        for _ in range(40):         # 等索引扫描完，文件才认得出
            code, raw = call(base, token, '/api/chat', body)
            if code != 400 or '不在' not in raw and '找不到' not in raw:
                break
            time.sleep(0.25)
        if code != 200:
            return {'answer': '', 'tools': [], 'error': 'HTTP %d：%s' % (code, raw[:300])}
        answer, tools = [], []
        for line in raw.splitlines():
            if not line.strip():
                continue
            m = json.loads(line)
            answer.append(m.get('t', ''))
            if 'tool' in m:
                tools.append(m['tool'])
            if m.get('error'):
                return {'answer': ''.join(answer), 'tools': tools, 'error': m['error']}
        return {'answer': ''.join(answer), 'tools': tools, 'error': ''}
    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(home, ignore_errors=True)


def checks(sc, got):
    """自动检查：[(说明, 是否通过)]。"""
    exp, called = sc['expect'], {t.get('kind') for t in got['tools']}
    kinds = {'read_skill_file': 'read', 'run_script': 'run', 'write_work_file': 'write'}
    out = [('没有出错', not got['error'])]
    out += [('调用了 %s' % t, kinds[t] in called) for t in exp['tools']]
    out += [('回答含「%s」' % w, w in got['answer']) for w in exp['contains']]
    out += [('回答不含「%s」' % w, w not in got['answer']) for w in exp['not_contains']]
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description='用配置的模型跑预装 skill 的评测场景，写成 Markdown 报告。')
    ap.add_argument('--base', default=os.environ.get('ASK_EVAL_BASE', ''), help='OpenAI 兼容接口地址，如 http://127.0.0.1:8080/v1')
    ap.add_argument('--model', default=os.environ.get('ASK_EVAL_MODEL', ''), help='模型名')
    ap.add_argument('--key', default=os.environ.get('ASK_EVAL_KEY', ''), help='AK，本机模型可不填')
    ap.add_argument('--skill', action='append', help='只跑这几个 skill，可重复；默认全部')
    ap.add_argument('--out', default='skill-eval-report.md', help='报告文件，默认 skill-eval-report.md')
    ap.add_argument('--log-level', default=os.environ.get('LOG_LEVEL', 'INFO').upper(),
                    choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'], help='日志级别，默认 INFO，可由环境变量 LOG_LEVEL 覆盖')
    args = ap.parse_args(argv)
    logging.basicConfig(level=args.log_level, format=LOG_FORMAT, stream=sys.stderr)
    if not args.base or not args.model:
        log.error('missing --base or --model')
        print('启动失败：没有给出模型接口。请用 --base 与 --model（或环境变量 ASK_EVAL_BASE、ASK_EVAL_MODEL）给出。')
        return 2
    model = {'base': args.base, 'model': args.model, 'key': args.key or 'none'}
    names = sorted(n[:-5] for n in os.listdir(EVALS) if n.endswith('.json'))
    lines = ['# skill 评测报告', '', '模型：%s（%s）' % (args.model, args.base), '']
    passed = total = 0
    for name in names:
        if args.skill and name not in args.skill:
            continue
        with open(os.path.join(EVALS, name + '.json'), encoding='utf-8') as f:
            data = json.load(f)
        lines += ['## ' + name, '']
        for sc in data['scenarios']:
            log.info('running %s / %s', name, sc['name'])
            got = run_scenario(name, sc, model)
            res = checks(sc, got)
            ok = all(r for _, r in res)
            passed += ok
            total += 1
            lines += ['### %s %s' % ('通过' if ok else '未通过', sc['name']), '', '提问：' + sc['question'], '',
                      '自动检查：' + '；'.join('%s %s' % ('✓' if r else '✗', t) for t, r in res), '',
                      '人工核对要点：' + sc['expect']['check'], '',
                      '工具调用：' + ('、'.join('%s %s' % (t.get('kind'), t.get('name')) for t in got['tools']) or '无'), '',
                      '回答：', '', '```', got['answer'] or '（空）' + ('，' + got['error'] if got['error'] else ''), '```', '']
    lines.insert(3, '自动检查通过：%d / %d 个场景' % (passed, total))
    with open(args.out, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    print('已写入 %s：自动检查通过 %d / %d 个场景。' % (args.out, passed, total))
    return 0


if __name__ == '__main__':
    sys.exit(main())
