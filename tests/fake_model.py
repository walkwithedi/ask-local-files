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
    err401 / err404 / err429 / err500   返回对应状态码
    toolong   返回 400，内容含 context length exceeded
GET /last 返回最近一次请求的 JSON 正文。

单独运行时监听 127.0.0.1 的随机端口，把端口号打印到标准输出第一行。
"""

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeModel(BaseHTTPRequestHandler):
    last = {}

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
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
        FakeModel.last = data
        model = data.get('model', '')
        if model.startswith('err'):
            self.error(int(model[3:]), '{"error":{"message":"fake error"}}')
            return
        if model == 'toolong':
            self.error(400, '{"error":{"message":"context length exceeded"}}')
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
        if model == 'notfound':
            pieces = ['所选文件中未找到。']
        for p in pieces:
            chunk = {'choices': [{'delta': {'content': p}}]}
            self.wfile.write(('data: %s\n\n' % json.dumps(chunk, ensure_ascii=False)).encode('utf-8'))
            self.wfile.flush()
        if model == 'slow':
            time.sleep(30)
        self.wfile.write(b'data: [DONE]\n\n')
        self.wfile.flush()


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
