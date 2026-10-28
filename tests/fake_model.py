#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试用的 OpenAI 兼容模型接口，只用标准库。

按请求里的模型名决定行为：
    ok        流式返回「你好，」「世界」
    plain     不流式，整段返回「整段回答」
    slow      先返回「开始」，再等 30 秒才结束（测停止与等待）
    long      流式返回 200 行（测对话区滚动）
    cite      回答带两条摘句：一条在「季度总结.txt」原文里，一条不在
    notfound  回答「所选文件中未找到」
    supp      回答带一条核对得上的摘句，末尾「文件外补充（未核对）：」一段里另有一条编造的摘句
    memo      回答带一条摘句「第一阶段在三月底完成验收」，引自文件 1（测记忆）；收到记忆更新时，
              把给出的结论（文件没改过的）整理成一页「验收安排」
    err401 / err404 / err429 / err500   返回对应状态码
    toolong   返回 400，内容含 context length exceeded
    call:<工具名>:<JSON 参数>   请求带 tools 且还没有工具结果时流式返回一次工具调用（参数分两段发），
              拿到工具结果后回答「结果：<工具结果>」
    loop:<工具名>:<JSON 参数>   只要请求带 tools 就再调用一次；不带 tools 时回答「停」
    seq:<JSON 列表>   列表每项为 [工具名, 参数对象]，按已有工具结果数依次调用下一个；
              用完或请求不带 tools 时回答「结果：」加全部工具结果（以「|」分隔）
    notools   请求带 tools 时返回 400（tools not supported）
GET /last 返回最近一次请求的 JSON 正文（不算概括对话标题的请求，那个记在 last_title；其他 GET 路径同样返回，除非设了 serving）。
FakeModel.auth 为最近一次请求的 Authorization 头（没有时为 None）。
FakeModel.serving 设为 {'models': [...], 'props': {...}, 'ps': {...}} 时模拟本机模型服务：
    GET /v1/models 列出 models，/props（llama-server）与 /api/ps（Ollama）返回对应内容，没给的返回 404。

单独运行时监听 127.0.0.1 的随机端口，把端口号打印到标准输出第一行。
"""

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeModel(BaseHTTPRequestHandler):
    last = {}
    last_title = {}
    auth = None
    serving = None

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        local = FakeModel.serving
        if local is not None:
            path = self.path.split('?')[0]
            if path == '/v1/models':
                self.error(200, json.dumps({'data': [{'id': m} for m in local.get('models') or []]}))
            elif path in ('/props', '/api/ps') and local.get(path.strip('/').split('/')[-1]) is not None:
                self.error(200, json.dumps(local[path.strip('/').split('/')[-1]]))
            else:
                self.error(404, '{"error":"not found"}')
            return
        body = json.dumps(FakeModel.last, ensure_ascii=False).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def error(self, code, text):
        body = text.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0)
        data = json.loads(self.rfile.read(n).decode('utf-8'))
        if data['messages'][0]['content'].startswith('把下面这次问答概括成'):
            FakeModel.last_title = data
        else:
            FakeModel.last = data
        FakeModel.auth = self.headers.get('Authorization')
        model = data.get('model', '')
        if model.startswith('err'):
            self.error(int(model[3:]), '{"error":{"message":"fake error"}}')
            return
        if model == 'toolong':
            self.error(400, '{"error":{"message":"context length exceeded"}}')
            return
        if model == 'notools' and data.get('tools'):
            self.error(400, '{"error":{"message":"tools not supported"}}')
            return
        if model.startswith(('call:', 'loop:', 'seq:')):
            self.tool_model(model, data)
            return
        if model == 'plain':
            body = json.dumps({'choices': [{'message': {'content': '整段回答'}}]}, ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        pieces = ['开始'] if model == 'slow' else ['你好，', '世界']
        if model == 'long':
            pieces = ['第 %d 行\n' % i for i in range(1, 201)]
        if model == 'cite':
            pieces = ['要点一（文件 1：「甲甲', '甲」）。', '要点二（文件 1：「编造的话」）。']
        if model == 'supp':
            pieces = ['要点（文件 1：「甲甲', '甲」）。\n\n文件外补充（未核对）：', '另一说法（文件 1：「编造的话」）。']
        if model == 'notfound':
            pieces = ['所选文件中未找到。']
        if data['messages'][0]['content'].startswith('把下面这次问答概括成'):
            # 概括对话标题：titlefail 时不给内容，其余给一个带前缀和引号的标题
            pieces = [] if model == 'titlefail' else ['标题：「季度', '要点」']
        if model == 'memo':
            pieces = ['第一阶段三月底验收（文件 1：「第一阶段', '在三月底完成验收」）。']
            if '你在维护使用者本机的一份「记忆」' in data['messages'][0]['content']:
                # 记忆更新：把给出的结论整理成一页「验收安排」
                work = json.loads(data['messages'][1]['content'])
                points = [{'text': c['conclusion'], 'file': c['file'], 'quote': c['quote']}
                          for c in work['conclusions'] if not c.get('changed')]
                text = json.dumps({'topics': [{'id': 'new', 'title': '验收安排', 'points': points}]}, ensure_ascii=False)
                pieces = [text[i:i + 20] for i in range(0, len(text), 20)]
        if model == 'md':
            text = ('# 标题\n\n**加粗** 与 *斜体* 和 `代码`，见 [说明](https://example.com/a)\n\n'
                    '- 甲\n  - 甲一\n- 乙\n\n1. 一\n2. 二\n\n> 引用\n\n---\n\n'
                    '| 列一 | 列二 |\n| --- | ---: |\n| a | b |\n\n```\nx < y\n```\n\n<img src=x onerror=alert(1)>')
            pieces = [text[i:i + 7] for i in range(0, len(text), 7)]
        for p in pieces:
            chunk = {'choices': [{'delta': {'content': p}}]}
            self.wfile.write(('data: %s\n\n' % json.dumps(chunk, ensure_ascii=False)).encode('utf-8'))
            self.wfile.flush()
        if model == 'slow':
            time.sleep(30)
        self.wfile.write(b'data: [DONE]\n\n')
        self.wfile.flush()


    def sse(self, chunks):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        for delta in chunks:
            chunk = {'choices': [{'delta': delta}]}
            self.wfile.write(('data: %s\n\n' % json.dumps(chunk, ensure_ascii=False)).encode('utf-8'))
            self.wfile.flush()
        self.wfile.write(b'data: [DONE]\n\n')
        self.wfile.flush()

    def tool_model(self, model, data):
        results = [m['content'] for m in data['messages'] if m.get('role') == 'tool']
        if model.startswith('seq:'):
            steps = json.loads(model[4:])
            if data.get('tools') and len(results) < len(steps):
                name, args = steps[len(results)]
                self.sse([{'tool_calls': [{'index': 0, 'id': 'c%d' % len(results), 'type': 'function',
                                           'function': {'name': name,
                                                        'arguments': json.dumps(args, ensure_ascii=False)}}]}])
            else:
                self.sse([{'content': '结果：' + '|'.join(results)}])
            return
        mode, name, args = model.split(':', 2)
        if data.get('tools') and (mode == 'loop' or not results):
            half = len(args) // 2
            self.sse([{'content': '先看一下。'},
                      {'tool_calls': [{'index': 0, 'id': 'c%d' % len(results), 'type': 'function',
                                       'function': {'name': name, 'arguments': args[:half]}}]},
                      {'tool_calls': [{'index': 0, 'function': {'arguments': args[half:]}}]}])
            return
        self.sse([{'content': '结果：' + results[-1] if results and mode == 'call' else '停'}])


def start():
    """在后台线程启动，返回 (server, 接口地址)。"""
    server = ThreadingHTTPServer(('127.0.0.1', 0), FakeModel)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, 'http://127.0.0.1:%d/v1' % server.server_address[1]


if __name__ == '__main__':
    srv = ThreadingHTTPServer(('127.0.0.1', 0), FakeModel)
    srv.daemon_threads = True
    print(srv.server_address[1], flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    sys.exit(0)
