#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""预装 skill 的评测场景（tests/skill_evals/<skill>.json）：检查格式，并把场景里确定性的一步（运行脚本）实际跑一遍。
经模型的部分由 tests/skill_eval_run.py 用配置的模型跑，不在这里。"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

TOOL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS = os.path.join(TOOL, 'skills')
EVALS = os.path.join(TOOL, 'tests', 'skill_evals')
TOOLS = {'read_skill_file', 'run_script', 'write_work_file'}


def load(name):
    with open(os.path.join(EVALS, name), encoding='utf-8') as f:
        return json.load(f)


class SkillEvalFormatTest(unittest.TestCase):
    def test_every_bundled_skill_has_three_scenarios(self):
        skills = sorted(n for n in os.listdir(SKILLS) if os.path.isfile(os.path.join(SKILLS, n, 'SKILL.md')))
        evals = sorted(n[:-5] for n in os.listdir(EVALS) if n.endswith('.json'))
        self.assertEqual(evals, skills)
        for name in skills:
            data = load(name + '.json')
            self.assertEqual(data['skill'], name)
            self.assertGreaterEqual(len(data['scenarios']), 3, name)
            for sc in data['scenarios']:
                self.assertTrue(sc['name'] and sc['question'], name)
                self.assertIsInstance(sc['files'], dict)
                for rel in sc['files']:
                    self.assertFalse(os.path.isabs(rel) or '..' in rel.split('/'), rel)
                exp = sc['expect']
                self.assertLessEqual(set(exp['tools']), TOOLS, name)
                self.assertIsInstance(exp['contains'], list)
                self.assertIsInstance(exp['not_contains'], list)
                self.assertTrue(exp['check'])
                if 'script' in sc:
                    self.assertTrue(os.path.isfile(os.path.join(SKILLS, name, sc['script']['run'])), sc['name'])

    def test_fictional_content_only(self):
        # 场景内容只用虚构的占位：不出现路径、主机、邮箱一类真实形态
        for n in os.listdir(EVALS):
            with open(os.path.join(EVALS, n), encoding='utf-8') as f:
                text = f.read()
            self.assertNotRegex(text, r'/home/(?!user/)|[A-Za-z]:\\\\|@[a-z0-9-]+\.[a-z]|\b\d{1,3}(\.\d{1,3}){3}\b', n)


class SkillEvalScriptTest(unittest.TestCase):
    """场景里的 script：在临时目录放好已选文件与工作目录文件，按页面运行时的环境变量运行，检查输出与生成的文件。"""

    def run_scenario(self, skill, sc):
        root = tempfile.mkdtemp(prefix='skill-eval-')
        self.addCleanup(shutil.rmtree, root, True)
        docs, work, out = (os.path.join(root, d) for d in ('docs', 'work', 'out'))
        for d in (docs, work, out):
            os.makedirs(d)
        files = []
        for rel, text in sc['files'].items():
            path = os.path.join(docs, rel)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(text)
            files.append(path)
        for rel, text in sc['script'].get('work', {}).items():
            path = os.path.join(work, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(text)
        d = os.path.join(SKILLS, skill)
        env = dict(os.environ, ASK_FILES='\n'.join(files), ASK_ROOTS=docs, ASK_OUT=out, ASK_WORK=work,
                   ASK_TOOL_DIR=TOOL, PYTHONIOENCODING='utf-8')
        r = subprocess.run([sys.executable, os.path.join(d, sc['script']['run'])] + sc['script']['args'], cwd=d,
                           env=env, capture_output=True, text=True, encoding='utf-8', timeout=60)
        return r.stdout, sorted(os.listdir(out))

    def test_scripts(self):
        n = 0
        for name in sorted(os.listdir(EVALS)):
            data = load(name)
            for sc in data['scenarios']:
                if 'script' not in sc:
                    continue
                with self.subTest(skill=data['skill'], scenario=sc['name']):
                    output, made = self.run_scenario(data['skill'], sc)
                    for want in sc['script']['output']:
                        self.assertIn(want, output)
                    if 'files' in sc['script']:
                        self.assertEqual(made, sorted(sc['script']['files']))
                    n += 1
        self.assertGreaterEqual(n, 7)

    def test_errors_say_how_to_fix(self):
        # 脚本报错写明原因和怎么改
        data = load('doc-compare.json')
        sc = next(s for s in data['scenarios'] if s['name'] == '只选了一个文件')
        output, _ = self.run_scenario('doc-compare', sc)
        self.assertRegex(output, r'出错：.+。请.+')


class SkillEvalRunnerTest(unittest.TestCase):
    """评测脚本 skill_eval_run.py：用假模型走通一遍（只验证流程，不评回答好坏）。"""

    def test_runner_with_fake_model(self):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import fake_model
        import skill_eval_run
        server, base = fake_model.start()
        self.addCleanup(server.shutdown)
        root = tempfile.mkdtemp(prefix='skill-eval-run-')
        self.addCleanup(shutil.rmtree, root, True)
        out = os.path.join(root, 'report.md')
        rc = skill_eval_run.main(['--base', base, '--model', 'ok', '--skill', 'prompt-optimize', '--out', out,
                                  '--log-level', 'ERROR'])
        self.assertEqual(rc, 0)
        with open(out, encoding='utf-8') as f:
            report = f.read()
        self.assertIn('## prompt-optimize', report)
        self.assertEqual(report.count('### '), 3)
        self.assertIn('你好，世界', report)
        self.assertIn('✓ 没有出错', report)
        self.assertEqual(skill_eval_run.main(['--base', '', '--model', '', '--out', out]), 2)


if __name__ == '__main__':
    unittest.main()
