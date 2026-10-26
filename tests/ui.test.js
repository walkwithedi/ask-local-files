// ask_local_files.html 的界面测试：node --test ask-local-files/tests/ui.test.js
// 需要 Playwright（云端会话已预装，NODE_PATH 指向全局模块）；离线工作站不需要运行。
'use strict';
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn, execSync, execFileSync} = require('node:child_process');
const {chromium} = require('playwright');

const TOOL = path.dirname(__dirname);
const PYTHON = execSync('command -v python3').toString().trim();
let browser, modelProc, modelBase;

function firstLine(proc, re) {
  return new Promise((resolve, reject) => {
    let buf = '';
    const timer = setTimeout(() => reject(new Error('启动超时：' + buf)), 15000);
    proc.stdout.on('data', d => {
      buf += d.toString();
      const m = buf.match(re);
      if (m) { clearTimeout(timer); resolve(m[1]); }
    });
    proc.on('exit', code => { clearTimeout(timer); reject(new Error('进程退出 ' + code + '：' + buf)); });
  });
}

test.before(async () => {
  browser = await chromium.launch();
  modelProc = spawn('python3', [path.join(__dirname, 'fake_model.py')], {stdio: ['ignore', 'pipe', 'inherit']});
  modelBase = 'http://127.0.0.1:' + await firstLine(modelProc, /^(\d+)\s/) + '/v1';
});
test.after(async () => {
  await browser.close();
  modelProc.kill();
});

// 起一个独立的 ask_local_files 服务并打开页面。opts.folders 为 false 时不预置文件夹，
// opts.model 为假模型的行为名（为 null 时不配置模型），opts.files 为 {相对路径: 内容}，
// opts.homeDirs 为家目录下另建的文件夹，opts.noPdf 时 PATH 置空、找不到 pdftotext，
// opts.clock 时接管页面计时器（用 page.clock.fastForward 快进），opts.beforeGoto(page) 在打开页面前调用，
// opts.skills 为自建 skill {名称: {相对路径: 内容}}，写到 OpenCode 的 skills 文件夹，opts.env 为另加的环境变量；
// 页面默认打开「浏览文件夹」，多数用例从搜索开始，加载完后切到「搜索」，opts.tab 为 'browse' 时保持默认
// opts.height 为视口高度
async function launch(t, opts) {
  opts = Object.assign({folders: true, model: 'ok', width: 1280, files: {}, homeDirs: []}, opts || {});
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'ask-local-files-ui-'));
  const docs = path.join(home, '资料');
  const files = Object.assign({'季度总结.txt': '甲'.repeat(100), '子目录/会议记录.md': '乙'.repeat(50), '资料包.zip': 'x'}, opts.files);
  for (const [rel, text] of Object.entries(files)) {
    fs.mkdirSync(path.dirname(path.join(docs, rel)), {recursive: true});
    fs.writeFileSync(path.join(docs, rel), text);
  }
  for (const d of opts.homeDirs) fs.mkdirSync(path.join(home, d), {recursive: true});
  const conf = {folders: opts.folders ? [docs] : [], char_limit: opts.limit || 100000};
  if (opts.model) Object.assign(conf, {custom: {base: modelBase, model: opts.model, key: 'k'}, model: 'custom'});
  fs.mkdirSync(path.join(home, '.config', 'ask-local-files'), {recursive: true});
  fs.writeFileSync(path.join(home, '.config', 'ask-local-files', 'config.json'), JSON.stringify(conf));
  for (const [name, entries] of Object.entries(opts.skills || {})) {
    for (const [rel, text] of Object.entries(entries)) {
      const p = path.join(home, '.config', 'opencode', 'skills', name, rel);
      fs.mkdirSync(path.dirname(p), {recursive: true});
      fs.writeFileSync(p, text);
    }
  }
  const env = Object.assign({}, process.env, {HOME: home, XDG_CONFIG_HOME: path.join(home, '.config'),
    XDG_CACHE_HOME: path.join(home, '.cache'), LOG_LEVEL: 'ERROR'});
  if (opts.noPdf) env.PATH = '';
  Object.assign(env, opts.env || {});
  const proc = spawn(PYTHON, [path.join(TOOL, 'ask_local_files.py'), '--no-browser'], {env, stdio: ['ignore', 'pipe', 'ignore']});
  const url = await firstLine(proc, /网页界面：(\S+)/);
  const context = await browser.newContext({viewport: {width: opts.width, height: opts.height || 800}});
  const page = await context.newPage();
  page.setDefaultTimeout(5000);
  page.on('dialog', d => (page.onDialog ? page.onDialog(d) : d.accept()));
  if (opts.clock) await page.clock.install();
  if (opts.beforeGoto) await opts.beforeGoto(page);
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  await page.goto(url);
  if (opts.tab !== 'browse') {
    await page.locator('#browseList li').first().waitFor({timeout: 3000}).then(() => page.click('#tabSearch'), () => {});
  }
  t.after(async () => {
    await context.close();
    proc.kill();
    fs.rmSync(home, {recursive: true, force: true});
  });
  return {page, home, docs, url};
}

// 取出的文字开头的文件信息（路径、文件名、修改时间各一行，空一行）有多少字；测试文件没有作者
function infoLen(docs, rel) {
  const p = path.join(docs, rel);
  return ('路径：' + p + '\n文件名：' + path.basename(p) + '\n修改时间：').length + 'YYYY-MM-DD HH:MM'.length + 2;
}

async function pickByName(page, name) {
  await page.fill('#q', name);
  await page.locator('#results li', {hasText: name}).getByRole('button', {name: /加入/}).click();
  await page.locator('#picked li', {hasText: name}).getByText(/\d 字/).waitFor();
}

async function noHorizontalScroll(page) {
  const [sw, cw] = await page.evaluate(() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]);
  assert.ok(sw <= cw, `横向溢出：scrollWidth ${sw} > ${cw}`);
}

test('主流程：搜索、加入、提问、出回答', async t => {
  const {page} = await launch(t);
  await pickByName(page, '季度总结');
  await page.fill('#ask', '总结要点');
  await page.click('#send');
  await page.locator('#chat').getByText('你好，世界').waitFor();
});

test('360px 宽度下没有横向滚动', async t => {
  const {page} = await launch(t, {width: 360});
  await pickByName(page, '季度总结');
  await noHorizontalScroll(page);
});


// ---------------- 第 2 批 布局

async function ask(page, text) {
  await page.fill('#ask', text || '总结要点');
  await page.click('#send');
  await page.waitForFunction(() => !document.querySelector('#send').disabled && document.querySelector('#chat .msg.assistant'));
}

async function box(page, sel) {
  const b = await page.locator(sel).first().boundingBox();
  assert.ok(b, sel + ' 不可见');
  return b;
}

test('宽屏铺满窗口：找文件与问 AI 约 3:7，问 AI 占满其余宽度与高度，页脚常驻', async t => {
  const {page} = await launch(t, {width: 1600});
  const left = await box(page, '#findCard'), right = await box(page, '#askCard');
  assert.ok(right.x >= left.x + left.width, '问 AI 应在找文件右侧');
  const ratio = left.width / (left.width + right.width);
  assert.ok(ratio > 0.27 && ratio < 0.33, `找文件应约占三成：${left.width} / ${right.width}`);
  assert.ok(right.x + right.width >= 1600 - 40, '问 AI 应伸到窗口右侧');
  // 下方只留页脚和折叠区
  assert.ok(right.y + right.height >= 800 - 130 && right.y + right.height <= 800, '问 AI 应占满到窗口底部附近');
  const f = await box(page, 'footer');
  assert.ok(f.y + f.height <= 800, '页脚应常驻在窗口内');
  assert.strictEqual(await page.evaluate(() => document.documentElement.scrollHeight <= innerHeight), true, '整页不应滚动');
});

test('宽屏下长回答只在对话区滚动，输入框与页脚不动', async t => {
  const {page} = await launch(t, {width: 1600, model: 'long'});
  await pickByName(page, '季度总结');
  await ask(page);
  const a = await box(page, '#ask'), f = await box(page, 'footer');
  assert.ok(a.y + a.height <= f.y, '输入框应在页脚上方、窗口之内');
  assert.ok(f.y + f.height <= 800, '页脚应常驻在窗口内');
  const c = await box(page, '#chat');
  assert.ok(c.height > 220, `对话区应足够高：${c.height}`);     // 输入框约 4 行高
  assert.strictEqual(await page.evaluate(() => document.documentElement.scrollHeight <= innerHeight), true, '整页不应滚动');
});

test('窄屏单列，问 AI 在已选下方', async t => {
  const {page} = await launch(t, {width: 800});
  const picked = await box(page, '#pickedCard'), right = await box(page, '#askCard');
  assert.ok(right.y >= picked.y + picked.height, '问 AI 应在已选下方');
  await noHorizontalScroll(page);
});

test('对话区限高并滚到最新，输入框留在视口里', async t => {
  const {page} = await launch(t, {width: 1280, model: 'long'});
  await pickByName(page, '季度总结');
  await ask(page);
  const s = await page.$eval('#chat', e => ({o: getComputedStyle(e).overflowY, top: e.scrollTop, h: e.clientHeight, sh: e.scrollHeight}));
  assert.strictEqual(s.o, 'auto');
  assert.ok(s.sh > s.h, '长回答应在对话区内滚动');
  assert.ok(s.top + s.h >= s.sh - 2, '应滚到最新内容');
  const a = await box(page, '#ask');
  assert.ok(a.y + a.height <= 800, '输入框应在视口内');
});

test('窄屏开始对话后收起找文件，可点改选文件展开', async t => {
  const {page} = await launch(t, {width: 800});
  await pickByName(page, '季度总结');
  await ask(page);
  assert.ok(await page.isHidden('#findBody'));
  assert.match(await page.textContent('#findCollapsed'), /已选 1 个文件/);
  await page.click('#reselect');
  assert.ok(await page.isVisible('#findBody'));
});

test('宽屏开始对话后找文件不收起', async t => {
  const {page} = await launch(t, {width: 1280});
  await pickByName(page, '季度总结');
  await ask(page);
  assert.ok(await page.isVisible('#findBody'));
});

test('模型选择在输入框上方，保存清空在问 AI 标题行', async t => {
  const {page} = await launch(t);
  const m = await box(page, '#model'), a = await box(page, '#ask');
  assert.ok(m.y + m.height <= a.y, '模型选择应在输入框上方');
  const h = await box(page, '#h-ask'), sv = await box(page, '#save'), c = await box(page, '#chatWrap');
  assert.ok(Math.abs(sv.y - h.y) < h.height, '保存对话应与标题同一行');
  assert.ok(sv.y < c.y);
  const sd = await box(page, '#saveDocx');
  assert.ok(sd.y < c.y, '保存为 Word 应在对话区上方');
});

test('停止只在生成中出现，并替换发送', async t => {
  const {page} = await launch(t, {model: 'slow'});
  assert.ok(await page.isHidden('#stop'));
  await pickByName(page, '季度总结');
  await page.fill('#ask', '总结');
  await page.click('#send');
  await page.locator('#chat').getByText('开始').waitFor();
  assert.ok(await page.isVisible('#stop'));
  assert.ok(await page.isHidden('#send'));
  await page.click('#stop');
  await page.waitForSelector('#send', {state: 'visible'});
  assert.ok(await page.isHidden('#stop'));
});

test('仍然发送跟在超限提示里', async t => {
  const {page} = await launch(t, {limit: 1000, files: {'大文件.txt': '丙'.repeat(1500)}});
  await pickByName(page, '大文件');
  assert.strictEqual(await page.$eval('#sendCut', e => !!e.closest('#askNotice')), true);
});

test('字数只在已选标题处显示', async t => {
  const {page, docs} = await launch(t);
  await pickByName(page, '季度总结');
  assert.strictEqual(await page.textContent('#pickCount'), '1 / 50，共 ' + (100 + infoLen(docs, '季度总结.txt')) + ' 字');
  assert.doesNotMatch(await page.textContent('#pickStatus'), /合计/);
});

test('按钮统一在右侧，结果第二行只写所在文件夹', async t => {
  const {page} = await launch(t);
  await page.fill('#q', '会议');
  const li = page.locator('#results li', {hasText: '会议记录'});
  const b = await li.getByRole('button', {name: /加入/}).boundingBox(), n = await li.locator('.name').boundingBox();
  assert.ok(b.x > n.x, '加入按钮应在文件名右侧');
  const meta = await li.locator('.meta').textContent();
  assert.match(meta, /^资料\/子目录/);
  await li.getByRole('button', {name: '浏览文件夹 资料/子目录'}).waitFor();
  assert.doesNotMatch(meta, /会议记录/);
  await pickByName(page, '会议记录');
  const pl = page.locator('#picked li', {hasText: '会议记录'});
  const pb = await pl.getByRole('button', {name: /移除/}).boundingBox(), pn = await pl.locator('.name').boundingBox();
  assert.ok(pb.x > pn.x);
});

test('设置按用处就近放', async t => {
  const {page} = await launch(t);
  assert.strictEqual(await page.$('#settings'), null);
  assert.ok(await page.isHidden('#folderPanel'));
  await page.click('#manageFolders');
  assert.ok(await page.isVisible('#folder'));
  assert.strictEqual(await page.getAttribute('#manageFolders', 'aria-expanded'), 'true');
  await page.click('#modelSettings');
  assert.ok(await page.isVisible('#limit'));
  assert.ok(await page.isVisible('#cBase'));
});

test('刷新在搜索状态行', async t => {
  const {page} = await launch(t);
  assert.strictEqual(await page.$eval('#refresh', e => !!e.closest('#findStatusRow')), true);
  assert.strictEqual(await page.$eval('#findStatus', e => !!e.closest('#findStatusRow')), true);
});

// ---------------- 第 3 批 首次启动与配置

const isFocused = (page, sel) => page.$eval(sel, e => e === document.activeElement);
const inViewport = (page, sel) => page.$eval(sel, e => {
  const r = e.getBoundingClientRect();
  return r.top >= 0 && r.bottom <= innerHeight;
});

// 改写某个接口的返回：fn 收到原返回的 JSON，返回改过的
async function patchApi(page, pathPart, fn) {
  await page.route(u => u.pathname === pathPart, async route => {
    const res = await route.fetch();
    await route.fulfill({response: res, json: fn(await res.json())});
  });
}

test('首次启动：展开文件夹管理并聚焦路径输入框', async t => {
  const {page} = await launch(t, {folders: false, width: 800, tab: 'browse'});
  await page.waitForFunction(() => document.activeElement && document.activeElement.id === 'folder');
  assert.ok(await page.isVisible('#folderPanel'));
  assert.ok(await inViewport(page, '#folder'));
});

test('缺少 pdftotext 的提醒常驻，发送后仍在', async t => {
  const {page} = await launch(t, {noPdf: true});
  await page.locator('#banner').getByText(/pdftotext/).waitFor();
  await pickByName(page, '季度总结');
  await ask(page);
  assert.ok(await page.isVisible('#banner'));
});

test('没有可用模型时「问 AI」收成一行，不自动展开模型设置，找文件占满宽度', async t => {
  const {page} = await launch(t, {model: null, width: 1600});
  await page.locator('#noAiNote').waitFor();
  assert.ok(await page.evaluate(() => document.body.classList.contains('no-ai')));
  assert.ok(await page.isHidden('#modelPanel'));
  for (const sel of ['#ask', '#send', '#chatWrap', '#skillRow', '#model', '#save']) assert.ok(await page.isHidden(sel), sel);
  assert.ok(await page.isVisible('#modelSettings'));
  const find = await box(page, '#findCard'), ask = await box(page, '#askCard');
  assert.ok(find.width > 1400, '找文件宽度 ' + find.width);
  assert.ok(ask.y >= find.y + find.height - 1, '问 AI 应在找文件下方');
  await page.click('#modelSettings');
  await page.waitForSelector('#modelPanel', {state: 'visible'});
  await page.fill('#cBase', modelBase);
  await page.fill('#cModel', 'ok');
  await page.fill('#cKey', 'k');
  await page.click('#saveCustom');
  await page.waitForFunction(() => !document.body.classList.contains('no-ai'));
  assert.ok(await page.isHidden('#noAiNote'));
  assert.ok(await page.isVisible('#ask'));
});

test('没有可用模型时 360px 宽度下没有横向滚动', async t => {
  const {page} = await launch(t, {model: null, width: 360});
  await pickByName(page, '季度总结');
  await noHorizontalScroll(page);
});

test('「复制全部文字」带提问要求与全部文件，没有模型也能用', async t => {
  const {page, docs} = await launch(t, {model: null});
  assert.ok(await page.isHidden('#copyTexts'));
  await pickByName(page, '季度总结');
  await pickByName(page, '会议记录');
  await page.click('#copyTexts');
  await page.locator('#pickStatus', {hasText: '已复制：2 个文件'}).waitFor();
  assert.match(await page.getAttribute('#pickStatus', 'class'), /\bok\b/);
  const text = await page.evaluate(() => navigator.clipboard.readText());
  assert.match(text, /^只依据下面这些文件/);
  assert.ok(text.includes('===== 文件 1：' + path.join(docs, '季度总结.txt') + ' ====='));
  assert.ok(text.includes('===== 文件 2：' + path.join(docs, '子目录', '会议记录.md') + ' ====='));
  assert.ok(text.includes('甲'.repeat(100)));
});

test('「保存全部文字」存成 .md', async t => {
  const {page} = await launch(t);
  await pickByName(page, '季度总结');
  const [dl] = await Promise.all([page.waitForEvent('download'), page.click('#saveTexts')]);
  const body = fs.readFileSync(await dl.path(), 'utf8');
  assert.ok(body.includes('===== 文件 1：') && body.includes('甲'.repeat(100)));
  await page.locator('#pickStatus').getByText(/^已保存为 找文件问AI-文字-\d{8}-\d{4}\.md（在浏览器的下载文件夹里）：1 个文件/).waitFor();
});

test('路径输入补全与常用位置', async t => {
  const {page} = await launch(t, {folders: false, homeDirs: ['文档']});
  await page.fill('#folder', '~/');
  await page.waitForFunction(() => [...document.querySelectorAll('#folderList option')].some(o => o.value === '~/资料'));
  await page.locator('#places').getByRole('button', {name: '文档'}).click();
  assert.strictEqual(await page.inputValue('#folder'), '~/文档');
});

test('添加上级文件夹时提示合并了哪些子文件夹', async t => {
  const {page} = await launch(t);
  await page.click('#manageFolders');
  await page.fill('#folder', '~');
  await page.click('#addFolder');
  await page.locator('#folderStatus').getByText(/合并/).waitFor();
  assert.match(await page.textContent('#folderStatus'), /资料/);
});

test('读不了的文件夹列出路径', async t => {
  const {page} = await launch(t);
  await patchApi(page, '/api/search', r => Object.assign(r, {status: Object.assign(r.status,
    {error: '有 1 个文件夹无法读取，已跳过', unreadable: ['/示例/无权限']})}));
  await page.fill('#q', '季度');
  await page.locator('#findStatus').getByText('/示例/无权限').waitFor();
});

test('移除文件夹时写明已选文件会一并移出', async t => {
  const {page} = await launch(t);
  await pickByName(page, '季度总结');
  await page.click('#manageFolders');
  let msg = '';
  page.onDialog = d => { msg = d.message(); d.accept(); };
  await page.locator('#folders li').getByRole('button', {name: /移除/}).click();
  await page.waitForFunction(() => !document.querySelector('#picked li .name'));
  assert.match(msg, /已选的 1 个文件/);
});

test('扫描期间禁用刷新与重建索引', async t => {
  const {page} = await launch(t);
  await patchApi(page, '/api/search', r => Object.assign(r, {status: Object.assign(r.status, {scanning: true})}));
  await page.fill('#q', '季度');
  await page.waitForFunction(() => document.querySelector('#refresh').disabled && document.querySelector('#rebuild').disabled);
  assert.match(await page.textContent('#findStatus'), /正在扫描/);
});

test('已在扫描时点刷新说明会自动更新', async t => {
  const {page} = await launch(t);
  await page.route(u => u.pathname === '/api/refresh', r => r.fulfill({json: {ok: true, started: false, index: {scanning: true, files: 2, error: '', unreadable: []}}}));
  await page.click('#refresh');
  await page.locator('#findStatus').getByText(/自动更新/).waitFor();
});

test('字数上限写明范围和默认值，回车即保存', async t => {
  const {page} = await launch(t);
  await page.click('#modelSettings');
  const hint = await page.textContent('#modelPanel');
  assert.match(hint, /1,?000 到 2,?000,?000/);
  assert.match(hint, /默认 100,?000/);
  await page.fill('#limit', '5000');
  await page.press('#limit', 'Enter');
  await page.locator('#settingsStatus').getByText(/已保存/).waitFor();
});

test('自定义接口没填齐时写明暂不可用，填齐后自动选中', async t => {
  const {page} = await launch(t, {model: null});
  await page.click('#modelSettings');
  await page.fill('#cBase', 'http://127.0.0.1:9/v1');
  await page.fill('#cModel', 'ok');
  await page.click('#saveCustom');
  await page.locator('#settingsStatus').getByText(/暂不可用/).waitFor();
  await page.fill('#cKey', 'k');
  await page.click('#saveCustom');
  await page.waitForFunction(() => document.querySelector('#model').value === 'custom');
});

test('测试连接给出结果', async t => {
  const {page} = await launch(t);
  await page.click('#modelSettings');
  await page.click('#testModel');
  await page.locator('#settingsStatus').getByText(/连接正常/).waitFor();
});

test('测试连接失败写明原因', async t => {
  const {page} = await launch(t, {model: 'err401'});
  await page.click('#modelSettings');
  await page.click('#testModel');
  await page.locator('#settingsStatus').getByText(/AK/).waitFor();
});

test('几个保存按钮的可访问名称不同', async t => {
  const {page} = await launch(t);
  assert.strictEqual(await page.getAttribute('#saveLimit', 'aria-label'), '保存字数上限');
  assert.strictEqual(await page.getAttribute('#saveCustom', 'aria-label'), '保存自定义接口');
});

// ---------------- 第 4 批 搜索、已选、预览

const activeInfo = page => page.evaluate(() => {
  const a = document.activeElement, li = a && a.closest('li');
  return {id: a && a.id, text: a && a.textContent, list: li && li.parentElement.id, row: li && li.querySelector('.name') && li.querySelector('.name').textContent};
});

function manyFiles(n, prefix) {
  const out = {};
  for (let i = 1; i <= n; i++) out[(prefix || '批量') + String(i).padStart(2, '0') + '.txt'] = '丁'.repeat(10);
  return out;
}

test('扫描轮询时结果不变就不重绘，保留滚动与焦点', async t => {
  const {page} = await launch(t, {files: manyFiles(60)});
  await page.fill('#q', '批量');
  await page.locator('#results li', {hasText: '批量30'}).waitFor();
  await patchApi(page, '/api/search', r => Object.assign(r, {status: Object.assign(r.status, {scanning: true})}));
  await page.fill('#q', '批量 ');      // 触发一次搜索，进入扫描轮询
  await page.waitForFunction(() => document.querySelector('#refresh').disabled);
  await page.$eval('#results', e => { e.scrollTop = 300; e.querySelector('li').dataset.mark = '1'; });
  await page.locator('#results li', {hasText: '批量30'}).locator('[data-act="toggle"]').focus();
  const top = await page.$eval('#results', e => e.scrollTop);     // 聚焦时可能把那一行滚进视野
  await page.waitForTimeout(2000);
  assert.strictEqual(await page.$eval('#results li', e => e.dataset.mark), '1', '结果没变时不应重绘');
  assert.ok(Math.abs(await page.$eval('#results', e => e.scrollTop) - top) < 2);
  assert.strictEqual((await activeInfo(page)).row, '批量30.txt');
});

test('点加入后焦点回到同一行', async t => {
  const {page} = await launch(t);
  await page.fill('#q', '会议');
  await page.locator('#results li', {hasText: '会议记录'}).locator('[data-act="toggle"]').focus();
  await page.keyboard.press('Enter');
  await page.waitForFunction(() => document.activeElement && document.activeElement.textContent === '移除');
  assert.strictEqual((await activeInfo(page)).row, '会议记录.md');
});

test('在已选里移除后焦点落到下一行', async t => {
  const {page} = await launch(t);
  await pickByName(page, '季度总结');
  await pickByName(page, '会议记录');
  await page.locator('#picked li', {hasText: '季度总结'}).getByRole('button', {name: /移除/}).click();
  await page.waitForFunction(() => document.querySelectorAll('#picked li .name').length === 1);
  const a = await activeInfo(page);
  assert.strictEqual(a.list, 'picked');
  assert.strictEqual(a.row, '会议记录.md');
});

test('回车加入第一个结果的规则写在界面上，已加入时给提示', async t => {
  const {page} = await launch(t);
  assert.match(await page.getAttribute('#q', 'placeholder'), /回车加入第一个/);
  await page.fill('#q', '季度');
  await page.waitForFunction(() => { const n = document.querySelector('#results li .name'); return n && n.textContent === '季度总结.txt'; });
  await page.press('#q', 'Enter');
  await page.locator('#picked li', {hasText: '季度总结'}).waitFor();
  await page.press('#q', 'Enter');
  await page.locator('#findStatus').getByText(/已在已选里/).waitFor();
});

test('方向键在搜索框与结果之间移动', async t => {
  const {page} = await launch(t);
  await page.fill('#q', '资料');
  await page.locator('#results li').nth(2).waitFor();
  await page.press('#q', 'ArrowDown');
  const first = (await activeInfo(page)).row;
  assert.ok(first);
  await page.keyboard.press('ArrowDown');
  const second = (await activeInfo(page)).row;
  assert.notStrictEqual(second, first);
  await page.keyboard.press('ArrowUp');
  await page.keyboard.press('ArrowUp');
  assert.strictEqual((await activeInfo(page)).id, 'q');
});

test('读不了的格式在结果里标出，「加入」换成「打开文件夹」', async t => {
  const {page} = await launch(t);
  await page.fill('#q', '资料包');
  const li = page.locator('#results li', {hasText: '资料包.zip'});
  await li.getByRole('button', {name: '打开 资料包.zip 所在的文件夹'}).waitFor();
  assert.strictEqual(await li.locator('[data-act="toggle"]').count(), 0);
  assert.strictEqual(await li.locator('[data-act="open-folder"]').textContent(), '打开文件夹');
  assert.match(await li.locator('.meta').textContent(), /压缩包请先解压，再加入解压出的文件/);
});

function fakeOpenerBin(t) {
  const bin = fs.mkdtempSync(path.join(os.tmpdir(), 'ask-local-files-bin-'));
  fs.symlinkSync(path.join(__dirname, 'fake_opener.sh'), path.join(bin, 'xdg-open'));
  t.after(() => fs.rmSync(bin, {recursive: true, force: true}));
  return {bin, args: path.join(bin, 'args.txt')};
}

test('「打开文件夹」选不中文件时用 xdg-open 打开文件所在的文件夹', async t => {
  const {bin, args} = fakeOpenerBin(t);
  const {page, docs} = await launch(t, {env: {PATH: bin, FAKE_OPEN_ARGS: args}});
  await page.fill('#q', '资料包');
  await page.getByRole('button', {name: '打开 资料包.zip 所在的文件夹'}).click();
  await page.locator('#openStatus', {hasText: '已打开文件夹，未能选中文件：' + docs}).waitFor();
  assert.match(await page.getAttribute('#openStatus', 'class'), /\bok\b/);
  assert.ok(await page.isHidden('#copyFolder'));
  for (let i = 0; i < 50 && !fs.existsSync(args); i++) await new Promise(r => setTimeout(r, 100));
  assert.strictEqual(fs.readFileSync(args, 'utf8'), docs + '\n');
});

test('每个可读文件都有定位按钮，用文件管理器打开所在文件夹并选中', async t => {
  const bin = fs.mkdtempSync(path.join(os.tmpdir(), 'ask-local-files-bin-'));
  t.after(() => fs.rmSync(bin, {recursive: true, force: true}));
  fs.symlinkSync(path.join(__dirname, 'fake_opener.sh'), path.join(bin, 'nautilus'));
  const args = path.join(bin, 'args.txt');
  const {page, docs} = await launch(t, {env: {PATH: bin, FAKE_OPEN_ARGS: args}});
  await page.fill('#q', '');
  const zip = page.locator('#results li', {hasText: '资料包.zip'});
  await zip.waitFor();
  assert.strictEqual(await zip.locator('[data-act="reveal"]').count(), 0);
  const file = path.join(docs, '季度总结.txt');
  await page.locator('#results li', {hasText: '季度总结'}).getByRole('button', {name: '在文件夹中显示 季度总结.txt'}).click();
  await page.locator('#openStatus', {hasText: '已打开文件夹并选中文件：' + file}).waitFor();
  for (let i = 0; i < 50 && !fs.existsSync(args); i++) await new Promise(r => setTimeout(r, 100));
  assert.strictEqual(fs.readFileSync(args, 'utf8'), '--select\nfile://' + encodeURI(file) + '\n');
  await pickByName(page, '季度总结');
  await page.locator('#picked li', {hasText: '季度总结'}).getByRole('button', {name: '在文件夹中显示 季度总结.txt'}).waitFor();
  await page.click('#tabBrowse');
  await page.locator('#browseList button.dir').first().click();
  await page.locator('#browseList li', {hasText: '季度总结'}).locator('[data-act="reveal"]').waitFor();
});

test('打不开文件夹时写出路径并可「复制路径」', async t => {
  const {page, docs} = await launch(t, {noPdf: true});
  await page.fill('#q', '资料包');
  await page.getByRole('button', {name: '打开 资料包.zip 所在的文件夹'}).click();
  await page.locator('#openStatus', {hasText: '打开文件夹失败：本机没有 xdg-open 或 gio，无法打开文件管理器。请在文件管理器里打开：' + docs}).waitFor();
  assert.match(await page.getAttribute('#openStatus', 'class'), /\bbad\b/);
  await page.click('#copyFolder');
  await page.locator('#openStatus', {hasText: '已复制路径：' + docs}).waitFor();
  assert.strictEqual(await page.evaluate(() => navigator.clipboard.readText()), docs);
});

test('按钮的可访问名称带文件名', async t => {
  const {page} = await launch(t);
  await page.fill('#q', '季度');
  assert.strictEqual(await page.locator('#results li', {hasText: '季度总结'}).locator('[data-act="toggle"]').getAttribute('aria-label'), '加入 季度总结.txt');
  await pickByName(page, '季度总结');
  const li = page.locator('#picked li', {hasText: '季度总结'});
  await li.getByRole('button', {name: '预览 季度总结.txt'}).waitFor();
  await li.getByRole('button', {name: '移除 季度总结.txt'}).waitFor();
});

test('已选列表显示编号', async t => {
  const {page} = await launch(t);
  await pickByName(page, '季度总结');
  await pickByName(page, '会议记录');
  assert.deepStrictEqual(await page.$$eval('#picked li .num', es => es.map(e => e.textContent)), ['1.', '2.']);
});

test('选满 50 个时禁用加入并说明', async t => {
  const {page} = await launch(t, {files: Object.assign(manyFiles(25, '甲组'), manyFiles(25, '乙组'), {'批量51.txt': '丁'})});
  for (const g of ['甲组', '乙组']) {
    await page.fill('#q', g);
    await page.getByRole('button', {name: '全部加入（25 个）'}).click();
  }
  await page.locator('#pickCount').getByText(/^50 \/ 50/).waitFor();
  await page.fill('#q', '批量51');
  const b = page.locator('#results li', {hasText: '批量51'}).locator('[data-act="toggle"]');
  await page.waitForFunction(() => { const x = [...document.querySelectorAll('#results li button[data-act="toggle"]')]; return x.length && x.every(y => y.disabled); });
  assert.ok(await b.isDisabled());
  assert.match(await page.textContent('#findStatus'), /已选满 50 个/);
});

test('重绘已选时保留已展开的预览', async t => {
  const {page} = await launch(t);
  await pickByName(page, '季度总结');
  await page.locator('#picked li', {hasText: '季度总结'}).getByRole('button', {name: /预览/}).click();
  await page.locator('#picked .preview').waitFor();
  await pickByName(page, '会议记录');
  assert.ok(await page.locator('#picked li', {hasText: '季度总结'}).locator('.preview').isVisible());
});

test('一键移除取不出文字的文件', async t => {
  const {page} = await launch(t, {files: {'数据.bin': '\u0000\u0001'}});
  await page.fill('#q', '数据');
  await page.locator('#results li', {hasText: '数据.bin'}).locator('[data-act="toggle"]').click();
  await page.locator('#removeBad').getByText(/1 个/).waitFor();
  await page.click('#removeBad');
  await page.waitForFunction(() => !document.querySelector('#picked li .name'));
  assert.ok(await page.isHidden('#removeBad'));
});

test('超限时写明办法并立即给仍然发送，移除后清掉', async t => {
  const {page} = await launch(t, {limit: 1000, files: {'大文件.txt': '丙'.repeat(1500)}});
  await pickByName(page, '大文件');
  const msg = await page.textContent('#askStatus');
  assert.match(msg, /超过上限/);
  assert.match(msg, /移除/);
  assert.match(msg, /调高上限/);
  assert.ok(await page.isVisible('#sendCut'));
  await page.locator('#picked li', {hasText: '大文件'}).getByRole('button', {name: /移除/}).click();
  await page.waitForSelector('#sendCut', {state: 'hidden'});
  assert.doesNotMatch(await page.textContent('#askStatus'), /超过上限/);
});

test('清空已选只清文件，对话保留且不弹确认', async t => {
  const {page} = await launch(t);
  await pickByName(page, '季度总结');
  await ask(page);
  let asked = false;
  page.onDialog = d => { asked = true; d.accept(); };
  await page.click('#clearPicked');
  await page.waitForFunction(() => !document.querySelector('#picked li .name'));
  assert.strictEqual(asked, false);
  assert.ok(await page.locator('#chat').getByText('你好，世界').isVisible());
  assert.match(await page.textContent('#pickStatus'), /对话保留/);
});

test('预览顶部写明显示范围', async t => {
  const {page} = await launch(t, {files: {'长文.txt': '戊'.repeat(25000)}});
  await pickByName(page, '长文');
  await page.locator('#picked li', {hasText: '长文'}).getByRole('button', {name: /预览/}).click();
  await page.locator('#picked .preview').getByText(/^显示前 20,000 字，共 25,\d{3} 字，发送时用全文/).waitFor();
});

// ---------------- 第 5 批 提问、复制、保存、退出、约定一致性

async function readDownload(page, click) {
  const [d] = await Promise.all([page.waitForEvent('download'), click()]);
  return fs.readFileSync(await d.path(), 'utf-8');
}

test('发送按钮旁写明 Enter 发送、Shift+Enter 换行', async t => {
  const {page} = await launch(t);
  assert.strictEqual(await page.textContent('#sendHint'), 'Enter 发送，Shift+Enter 换行');
});

test('Enter 与 Ctrl+Enter 发送，Shift+Enter 换行，输入法组字中的回车不发送', async t => {
  const {page} = await launch(t);
  await pickByName(page, '季度总结');
  await page.fill('#ask', '第一行');
  await page.focus('#ask');
  await page.keyboard.press('Shift+Enter');
  await page.keyboard.type('第二行');
  assert.strictEqual(await page.inputValue('#ask'), '第一行\n第二行');
  // 组字中的回车：keyCode 229 / isComposing
  await page.evaluate(() => {
    const ev = new KeyboardEvent('keydown', {key: 'Enter', isComposing: true, bubbles: true, cancelable: true});
    document.querySelector('#ask').dispatchEvent(ev);
  });
  assert.strictEqual(await page.locator('#chat .msg').count(), 0);
  await page.keyboard.press('Enter');
  await page.locator('#chat .msg.assistant').getByText('世界').waitFor();
  assert.strictEqual(await page.locator('#chat .msg.user .msg-body').textContent(), '第一行\n第二行');
  await page.fill('#ask', '再问');
  await page.keyboard.press('Control+Enter');
  await page.locator('#chat .msg.user', {hasText: '再问'}).waitFor();
});

test('等待时显示已等待秒数，超过 30 秒提示可停止', async t => {
  const {page} = await launch(t, {model: 'slow', clock: true});
  await pickByName(page, '季度总结');
  await page.fill('#ask', '总结');
  await page.click('#send');
  await page.locator('#chat').getByText('开始').waitFor();
  await page.clock.fastForward(31000);
  await page.locator('#askStatus').getByText(/已等待 3\d 秒/).waitFor();
  assert.match(await page.textContent('#askStatus'), /「停止」/);
});

test('发送去向写明文件与对话的字数', async t => {
  const {page, docs} = await launch(t);
  await pickByName(page, '季度总结');
  assert.match(await page.textContent('#dest'), new RegExp('文件 ' + (100 + infoLen(docs, '季度总结.txt')) + ' 字 \\+ 对话 0 字'));
  await ask(page, '总结要点');
  assert.match(await page.textContent('#dest'), /对话 9 字/);
});

test('对话本身超过上限时提示开始新对话', async t => {
  const {page} = await launch(t, {model: 'long', limit: 1000});
  await pickByName(page, '季度总结');
  await ask(page);
  assert.match(await page.textContent('#askStatus'), /开始新对话/);
  await page.fill('#ask', '再说说');
  await page.click('#send');
  await page.locator('#askStatus').getByText(/对话已有/).waitFor();
});

test('停止后的半截回答标出不完整，并写进保存的对话', async t => {
  const {page} = await launch(t, {model: 'slow'});
  await pickByName(page, '季度总结');
  await page.fill('#ask', '总结');
  await page.click('#send');
  await page.locator('#chat').getByText('开始').waitFor();
  await page.click('#stop');
  await page.locator('#chat').getByText('（回答不完整）').waitFor();
  const text = await readDownload(page, () => page.click('#save'));
  assert.match(text, /回答不完整/);
});

test('回答结束后焦点回到输入框', async t => {
  const {page} = await launch(t);
  await pickByName(page, '季度总结');
  await ask(page);
  await page.waitForFunction(() => document.activeElement && document.activeElement.id === 'ask');
});

test('复制结果写入状态区', async t => {
  const {page} = await launch(t);
  await pickByName(page, '季度总结');
  await ask(page);
  await page.locator('#chat').getByRole('button', {name: /复制/}).first().click();
  await page.locator('#chatStatus').getByText(/已复制/).waitFor();
});

test('生成中禁用保存对话', async t => {
  const {page} = await launch(t, {model: 'slow'});
  await pickByName(page, '季度总结');
  await page.fill('#ask', '总结');
  await page.click('#send');
  await page.locator('#chat').getByText('开始').waitFor();
  assert.ok(await page.isDisabled('#save'));
});

test('保存提示就近显示，不覆盖发送状态', async t => {
  const {page} = await launch(t);
  await pickByName(page, '季度总结');
  await ask(page);
  await page.locator('#askStatus').getByText('回答完成').waitFor();
  await readDownload(page, () => page.click('#save'));
  await page.locator('#chatStatus').getByText(/已保存为/).waitFor();
  assert.match(await page.textContent('#askStatus'), /回答完成/);
});

test('有未保存对话时退出先确认', async t => {
  const {page} = await launch(t);
  await pickByName(page, '季度总结');
  await ask(page);
  let msg = '';
  page.onDialog = d => { msg = d.message(); d.dismiss(); };
  await page.click('#quit');
  await page.waitForFunction(() => !document.querySelector('#quit').disabled);
  assert.match(msg, /1 轮问答.*未保存/s);
  assert.doesNotMatch(msg, /页面开着/);
  assert.ok(await page.isHidden('#closed'));
});

test('保存后退出不再确认', async t => {
  const {page} = await launch(t);
  await pickByName(page, '季度总结');
  await ask(page);
  await readDownload(page, () => page.click('#save'));
  let asked = false;
  page.onDialog = d => { asked = true; d.dismiss(); };
  await page.click('#quit');
  await page.waitForSelector('#closed', {state: 'visible'});
  assert.strictEqual(asked, false);
});

test('有未保存对话时关页先提醒', async t => {
  const {page} = await launch(t);
  await pickByName(page, '季度总结');
  await ask(page);
  let type = '';
  page.onDialog = d => { type = d.type(); d.dismiss(); };
  await page.close({runBeforeUnload: true});
  await new Promise(r => setTimeout(r, 500));
  assert.strictEqual(type, 'beforeunload');
});

test('心跳连续 3 次失败才判定服务已退出', async t => {
  const {page} = await launch(t, {clock: true});
  await page.locator('#ver').getByText(/v\d/).waitFor();
  await page.route(u => u.pathname === '/api/ping', r => r.abort());
  await page.clock.fastForward(10500);
  await page.waitForTimeout(300);
  assert.ok(await page.isHidden('#closed'), '一次失败不应判定退出');
  await page.clock.fastForward(10500);
  await page.waitForTimeout(300);
  await page.clock.fastForward(10500);
  await page.waitForSelector('#closed', {state: 'visible'});
});

test('失败提示写明下一步', async t => {
  const {page} = await launch(t);
  await page.route(u => u.pathname === '/api/refresh', r => r.abort());
  await page.click('#refresh');
  await page.locator('#findStatus').getByText(/刷新失败/).waitFor();
  assert.match(await page.textContent('#findStatus'), /刷新失败：[^。]+。\S+/);
  await page.route(u => u.pathname === '/api/settings', r => r.abort());
  await page.click('#modelSettings');
  await page.click('#saveLimit');
  await page.locator('#settingsStatus').getByText(/保存失败/).waitFor();
  assert.match(await page.textContent('#settingsStatus'), /保存失败：[^。]+。\S+/);
});

test('接口错误不再套用笼统的检查设置提示', async t => {
  const {page} = await launch(t, {model: 'err401'});
  await pickByName(page, '季度总结');
  await page.fill('#ask', '总结');
  await page.click('#send');
  await page.locator('#askStatus').getByText(/AK/).waitFor();
  assert.doesNotMatch(await page.textContent('#askStatus'), /检查「设置」/);
});

test('空状态区保持存在', async t => {
  const {page} = await launch(t);
  assert.notStrictEqual(await page.$eval('#pickStatus', e => getComputedStyle(e).display), 'none');
});

test('读取状态失败时页脚仍有版本号', async t => {
  const {page} = await launch(t, {beforeGoto: p => p.route(u => u.pathname === '/api/state', r => r.abort())});
  assert.match(await page.textContent('#ver'), /找文件问 AI v\d+\.\d+/);
});

test('360px 下各面板展开、有对话与预览时都没有横向滚动', async t => {
  const {page} = await launch(t, {width: 360, model: 'long', files: {'很长很长很长很长很长很长很长很长很长很长的文件名用于测试窄屏显示.txt': '己'.repeat(300)}});
  await page.click('#manageFolders');
  await page.click('#modelSettings');
  await pickByName(page, '很长很长');
  await page.locator('#picked li').getByRole('button', {name: /预览/}).click();
  await page.locator('#picked .preview').waitFor();
  await ask(page);
  await noHorizontalScroll(page);
  await page.click('#reselect');
  await noHorizontalScroll(page);
});

test('360px 下已选每行的预览与移除保持在同一行', async t => {
  const {page} = await launch(t, {width: 360});
  await pickByName(page, '季度总结');
  await pickByName(page, '会议记录');
  for (const name of ['季度总结', '会议记录']) {
    // 两个按钮在同一次布局里量，避免两次读取之间状态行变化让列表整体移动
    const [ay, by] = await page.locator('#picked li', {hasText: name}).evaluate(li =>
      ['preview', 'remove'].map(act => li.querySelector('[data-act="' + act + '"]').getBoundingClientRect().y));
    assert.ok(Math.abs(ay - by) < 2, name + ' 的按钮不在同一行');
  }
});


// ---------------- v1.4

async function lastRequest() {
  return (await fetch(modelBase.replace(/\/v1$/, '/last'))).json();
}

test('清空对话不弹确认，之后的提问是新对话', async t => {
  const {page} = await launch(t);
  let asked = false;
  page.onDialog = d => { asked = true; d.accept(); };
  await pickByName(page, '季度总结');
  await ask(page, '第一个问题');
  await page.click('#clear');
  assert.strictEqual(await page.locator('#chat .msg').count(), 0);
  assert.ok(!asked, '清空对话不应弹出确认');
  await ask(page, '第二个问题');
  const sent = await lastRequest();
  assert.deepStrictEqual(sent.messages.slice(1).map(m => m.content), ['第二个问题']);
});

test('回答没附原文时标出', async t => {
  const {page} = await launch(t);
  await pickByName(page, '季度总结');
  await ask(page);
  await page.locator('#chat .cite', {hasText: '没有附原文摘句'}).waitFor();
});

test('原文里找不到的摘句列在回答下方，并写进保存的对话', async t => {
  const {page} = await launch(t, {model: 'cite'});
  await pickByName(page, '季度总结');
  await ask(page);
  const note = page.locator('#chat .cite');
  await note.waitFor();
  const text = await note.textContent();
  assert.match(text, /文件 1：「编造的话」/);
  assert.doesNotMatch(text, /甲甲甲/);
  const [dl] = await Promise.all([page.waitForEvent('download'), page.click('#save')]);
  const md = fs.readFileSync(await dl.path(), 'utf-8');
  assert.match(md, /原文中未找到[\s\S]*文件 1：「编造的话」/);
});


test('搜索结果少于 50 个时可全部加入，跳过读不了的', async t => {
  const {page} = await launch(t, {files: {'报告甲.txt': '甲', '报告乙.md': '乙', '报告丙.rar': 'x'}});
  await page.getByRole('button', {name: '全部加入（4 个）'}).waitFor();     // 搜索框为空、列表少于 50 个时也出现
  await page.fill('#q', '报告');
  const b = page.getByRole('button', {name: '全部加入（2 个）'});
  await b.click();
  await page.locator('#picked li', {hasText: '报告乙'}).waitFor();
  assert.strictEqual(await page.locator('#picked li').count(), 2);
  assert.match(await page.textContent('#pickStatus'), /已加入 2 个，1 个读不了的已跳过/);
  await page.waitForFunction(() => document.querySelector('#addAllRow').hidden);
});

test('已选名额不够时全部加入不可点并写明', async t => {
  const files = {'报告1.txt': '1', '报告2.txt': '2', '报告3.txt': '3'};
  for (let i = 1; i <= 48; i++) files['其他' + i + '.txt'] = String(i);
  const {page} = await launch(t, {files});
  await page.fill('#q', '其他');
  await page.getByRole('button', {name: '全部加入（48 个）'}).click();
  await page.fill('#q', '报告');
  const b = page.getByRole('button', {name: '全部加入（3 个）'});
  await b.waitFor();
  assert.ok(await b.isDisabled());
  assert.match(await page.textContent('#addAllHint'), /还能加 2 个/);
});

test('搜索结果达到 50 个时不出现全部加入', async t => {
  const files = {};
  for (let i = 1; i <= 50; i++) files['报告' + i + '.txt'] = String(i);
  const {page} = await launch(t, {files});
  await page.fill('#q', '报告');
  await page.locator('#results li', {hasText: '报告50'}).waitFor();
  assert.ok(await page.isHidden('#addAllRow'));
});

test('有对话时全部加入不弹确认，直接开始新对话', async t => {
  const {page} = await launch(t, {files: {'报告甲.txt': '甲', '报告乙.txt': '乙'}});
  await pickByName(page, '季度总结');
  await ask(page);
  let n = 0;
  page.onDialog = d => { n++; d.accept(); };
  await page.fill('#q', '报告');
  await page.getByRole('button', {name: '全部加入（2 个）'}).click();
  await page.locator('#picked li', {hasText: '报告乙'}).waitFor();
  assert.strictEqual(n, 0);
  assert.strictEqual(await page.locator('#chat .msg').count(), 0);
  assert.match(await page.textContent('#chatStatus'), /已开始新对话/);
});


// ---------------- skill

const ECHO_SKILL = {
  'SKILL.md': '---\nname: echo\ndescription: 测试用，运行脚本\n---\n按要求运行脚本。\n',
  'scripts/echo.py': "import os\nprint('已运行')\nopen(os.path.join(os.environ['ASK_OUT'], 'out.txt'), 'w').write('生成的内容')\n"
};
const RUN_ECHO = 'call:run_script:{"script": "scripts/echo.py", "args": ["x"]}';

test('skill 下拉框默认不用，列出预装与自建，选中后写明说明', async t => {
  const {page} = await launch(t, {skills: {echo: ECHO_SKILL}});
  await page.locator('#skillSel option', {hasText: 'echo（自建，1 个脚本）'}).waitFor({state: 'attached'});
  assert.strictEqual(await page.inputValue('#skillSel'), '');
  assert.ok(await page.locator('#skillSel option', {hasText: '文档对比（预装'}).count());
  assert.strictEqual(await page.locator('#skillSel option', {hasText: 'doc-compare'}).count(), 0);
  assert.match(await page.textContent('#skillHint'), /opencode\/skills/);
  await page.selectOption('#skillSel', 'echo');
  assert.match(await page.textContent('#skillHint'), /测试用，运行脚本/);
  assert.doesNotMatch(await page.textContent('#skillHint'), /自动运行/, '说明默认收起');
  await page.locator('#skillHint').getByRole('button', {name: '展开 skill 说明'}).click();
  assert.match(await page.textContent('#skillHint'), /自动运行/);
  await page.locator('#skillHint').getByRole('button', {name: '收起 skill 说明'}).click();
  assert.doesNotMatch(await page.textContent('#skillHint'), /自动运行/);
  assert.match(await page.textContent('#dest'), /skill echo/);
});

test('重新读取后列出新放进来的 skill', async t => {
  const {page, home} = await launch(t);
  await page.locator('#skillSel option', {hasText: '文档对比'}).waitFor({state: 'attached'});
  const d = path.join(home, '.config', 'opencode', 'skills', 'echo');
  fs.mkdirSync(path.join(d, 'scripts'), {recursive: true});
  for (const [rel, text] of Object.entries(ECHO_SKILL)) fs.writeFileSync(path.join(d, rel), text);
  await page.click('#skillReload');
  await page.locator('#skillSel option', {hasText: 'echo（自建'}).waitFor({state: 'attached'});
  await page.locator('#chatStatus').getByText(/已读取 \d+ 个 skill/).waitFor();
});

test('选了 skill 可以不选文件发送；不选 skill 时仍要求选文件', async t => {
  const {page} = await launch(t);
  await page.fill('#ask', '写一则通知');
  await page.click('#send');
  await page.locator('#askStatus').getByText(/或选一个 skill/).waitFor();
  await page.locator('#skillSel option', {hasText: '会议纪要'}).waitFor({state: 'attached'});
  await page.selectOption('#skillSel', 'summarize-meeting');
  await page.click('#send');
  await page.locator('#chat').getByText('你好，世界').waitFor();
  await page.locator('#chat .who', {hasText: 'skill 会议纪要'}).waitFor();
});

test('脚本运行过程显示在回答上方，生成的文件可保存，并写进保存的对话', async t => {
  const {page} = await launch(t, {model: RUN_ECHO, skills: {echo: ECHO_SKILL}});
  await page.locator('#skillSel option', {hasText: 'echo（自建'}).waitFor({state: 'attached'});
  await page.selectOption('#skillSel', 'echo');
  await pickByName(page, '季度总结');
  await ask(page, '运行一下');
  const summary = page.locator('#chat .tools summary');
  await summary.getByText(/运行：scripts\/echo.py x → 退出码 0，生成 1 个文件/).waitFor();
  await page.locator('#chat').getByText(/结果：退出码 0/).waitFor();
  const saved = await readDownload(page, () => page.getByRole('button', {name: '保存 out.txt'}).click());
  assert.strictEqual(saved, '生成的内容');
  await page.locator('#chatStatus').getByText(/已保存 out.txt/).waitFor();
  const text = await readDownload(page, () => page.click('#save'));
  assert.match(text, /运行：scripts\/echo.py x → 退出码 0/);
  assert.match(text, /已运行/);
  assert.match(text, /skill echo/);
});

test('模型不支持函数调用时写明原因', async t => {
  const {page} = await launch(t, {model: 'notools'});
  await page.locator('#skillSel option', {hasText: '文档对比'}).waitFor({state: 'attached'});
  await page.selectOption('#skillSel', 'doc-compare');
  await pickByName(page, '季度总结');
  await page.fill('#ask', '写周报');
  await page.click('#send');
  await page.locator('#askStatus').getByText(/不支持 skill 工具/).waitFor();
});

test('office-gen：写入记录显示在回答上方，生成的 Word 可保存，写入内容进保存的对话', async t => {
  const steps = [['write_work_file', {path: '报告.md', content: '# 标题\n\n正文'}],
                 ['run_script', {script: 'scripts/make_docx.py', args: ['报告.md', '--out', '季度报告']}]];
  const {page} = await launch(t, {model: 'seq:' + JSON.stringify(steps)});
  await page.locator('#skillSel option', {hasText: '生成 Word 与 Excel（预装'}).waitFor({state: 'attached'});
  await page.selectOption('#skillSel', 'office-gen');
  await ask(page, '整理成 Word');
  const summary = page.locator('#chat .tools summary');
  await summary.getByText('写入：报告.md（8 字，覆盖）').waitFor();
  await summary.getByText(/运行：scripts\/make_docx.py 报告.md --out 季度报告 → 退出码 0，生成 1 个文件/).waitFor();
  const [d] = await Promise.all([page.waitForEvent('download'),
                                 page.getByRole('button', {name: '保存 季度报告.docx'}).click()]);
  await page.locator('#chatStatus').getByText(/已保存 季度报告.docx/).waitFor();
  assert.strictEqual(fs.readFileSync(await d.path()).subarray(0, 2).toString(), 'PK');
  const text = await readDownload(page, () => page.click('#save'));
  assert.match(text, /写入：报告.md（8 字，覆盖）\n\n```\n# 标题\n\n正文\n```/);
});

test('保存为 Word：单条回答与整个对话', async t => {
  const {page} = await launch(t);
  await pickByName(page, '季度总结');
  await ask(page, '总结一下');
  const one = page.locator('#chat .msg.assistant .who').getByRole('button', {name: '把这条回答保存为 Word'});
  const [d1] = await Promise.all([page.waitForEvent('download'), one.click()]);
  assert.strictEqual(fs.readFileSync(await d1.path()).subarray(0, 2).toString(), 'PK');
  await page.locator('#chatStatus').getByText(/已保存为 找文件问AI-\d{8}-\d{4}\.docx/).waitFor();
  const [d2] = await Promise.all([page.waitForEvent('download'), page.click('#saveDocx')]);
  const zip = fs.readFileSync(await d2.path());
  assert.strictEqual(zip.subarray(0, 2).toString(), 'PK');
  const unzip = execFileSync(PYTHON, ['-c', 'import sys,zipfile;print(zipfile.ZipFile(sys.argv[1]).read("word/document.xml").decode())',
                                       await d2.path()]).toString();
  assert.match(unzip, /找文件问 AI 对话/);
  assert.match(unzip, /总结一下/);
  assert.match(unzip, /你好，世界/);
});

test('360px 下 skill 说明与运行记录不横向溢出', async t => {
  const {page} = await launch(t, {width: 360, model: RUN_ECHO, skills: {echo: ECHO_SKILL}});
  await page.locator('#skillSel option', {hasText: 'echo（自建'}).waitFor({state: 'attached'});
  await page.selectOption('#skillSel', 'doc-compare');
  await noHorizontalScroll(page);
  await page.selectOption('#skillSel', 'echo');
  await pickByName(page, '季度总结');
  await ask(page, '运行一下');
  await page.locator('#chat .tools summary').first().click();
  await noHorizontalScroll(page);
});


// ---------------- v1.7：不弹确认、空文本发送、问答分色、新任务、浏览

test('改动文件清单不弹确认，直接开始新对话并在状态行说明', async t => {
  const {page} = await launch(t);
  await pickByName(page, '季度总结');
  await ask(page);
  let asked = false;
  page.onDialog = d => { asked = true; d.accept(); };
  await page.locator('#picked li', {hasText: '季度总结'}).getByRole('button', {name: /移除/}).click();
  await page.waitForFunction(() => !document.querySelector('#picked li .name'));
  assert.strictEqual(asked, false);
  assert.strictEqual(await page.locator('#chat .msg').count(), 0);
  assert.match(await page.textContent('#chatStatus'), /文件清单已改动，已开始新对话/);
  await pickByName(page, '会议记录');
  await ask(page);
  await pickByName(page, '季度总结');
  assert.strictEqual(asked, false);
  assert.strictEqual(await page.locator('#chat .msg').count(), 0);
});

test('选了 skill 可以不写问题直接发送，代为提问；没选 skill 时仍要求写问题', async t => {
  const {page} = await launch(t);
  await pickByName(page, '季度总结');
  await page.click('#send');
  await page.locator('#askStatus').getByText(/请先写下要 AI 做什么/).waitFor();
  assert.strictEqual(await page.locator('#chat .msg').count(), 0);
  await page.locator('#skillSel option', {hasText: '会议纪要'}).waitFor({state: 'attached'});
  await page.selectOption('#skillSel', 'summarize-meeting');
  await page.click('#send');
  await page.locator('#chat').getByText('你好，世界').waitFor();
  assert.strictEqual(await page.textContent('#chat .msg.user .msg-body'), '请按所选 skill 的说明处理已选文件。');
});

test('问与答用不同底色区分，回答底部有复制，半截回答也有', async t => {
  const {page} = await launch(t, {model: 'slow'});
  await pickByName(page, '季度总结');
  await page.fill('#ask', '总结');
  await page.click('#send');
  await page.locator('#chat').getByText('开始').waitFor();
  assert.strictEqual(await page.locator('#chat .foot').count(), 0, '生成中不显示底部复制');
  await page.click('#stop');
  await page.locator('#chat').getByText('（回答不完整）').waitFor();
  const [u, a] = await page.$$eval('#chat .msg', es => es.map(e => getComputedStyle(e).backgroundColor));
  assert.notStrictEqual(u, a);
  const foot = page.locator('#chat .msg.assistant .foot').getByRole('button', {name: '复制这条回答'});
  const head = await box(page, '#chat .msg.assistant .who'), f = await foot.boundingBox();
  assert.ok(f.y > head.y, '底部复制应在回答下方');
  await foot.click();
  await page.locator('#chatStatus').getByText(/已复制/).waitFor();
});

test('顶部描述写明新对话与支持格式，页脚写明多页面的退出规则', async t => {
  const {page} = await launch(t);
  const sub = await page.textContent('.sub');
  assert.match(sub, /选用 skill.*「＋ 新对话」/);
  assert.match(sub, /Word（docx）、Excel（xlsx、xlsm）、PowerPoint（pptx）、旧版 doc、xls、ppt（wps、et、dps 按同类格式试读）、PDF、邮件（eml）、网页（html、htm）、纯文本与代码文件/);
  assert.match(sub, /压缩包等读不了的文件可点「打开文件夹」自己处理/);
  assert.match(await page.textContent('footer'), /关闭全部页面后服务自动退出/);
});

test('另开的浏览器页面算一个页面；开着多个页面时退出先确认并写明个数', async t => {
  const {page, url} = await launch(t);
  await pickByName(page, '季度总结');
  const other = await page.context().newPage();
  await other.goto(url);
  await other.locator('#ver').getByText(/v\d/).waitFor();
  assert.strictEqual(await other.locator('#picked li .name').count(), 0, '新页面的已选应为空');
  let msg = '';
  page.onDialog = d => { msg = d.message(); d.dismiss(); };
  await page.click('#quit');
  await page.waitForFunction(() => !document.querySelector('#quit').disabled);
  assert.match(msg, /还有 1 个页面开着，退出会一起结束/);
  assert.ok(await page.isHidden('#closed'));
  await other.close();
});

test('浏览：标签页切换，逐层进入文件夹，全部加入含子文件夹', async t => {
  const {page} = await launch(t, {files: {'子目录/深一层/纪要.txt': '丙'.repeat(10)}});
  await page.click('#tabBrowse');
  assert.strictEqual(await page.getAttribute('#tabBrowse', 'aria-selected'), 'true');
  assert.ok(await page.isHidden('#q'));
  await page.locator('#crumbs [aria-current]', {hasText: '全部'}).waitFor();
  await page.locator('#browseList button.dir').first().click();
  await page.locator('#crumbs [aria-current]', {hasText: '资料'}).waitFor();
  await page.getByRole('button', {name: '打开文件夹 子目录'}).waitFor();
  assert.match(await page.locator('#browseList li', {hasText: '子目录'}).textContent(), /2 个文件/);
  assert.ok(await page.locator('#browseList li', {hasText: '资料包.zip'}).locator('[data-act="open-folder"]').isEnabled());
  assert.strictEqual(await page.textContent('#browseAdd'), '全部加入（3 个，含子文件夹）');
  await page.getByRole('button', {name: '打开文件夹 子目录'}).click();
  await page.locator('#crumbs [aria-current]', {hasText: '子目录'}).waitFor();
  await page.getByRole('button', {name: '全部加入（2 个，含子文件夹）'}).click();
  await page.locator('#picked li', {hasText: '纪要'}).getByText(/\d 字/).waitFor();
  await page.locator('#picked li', {hasText: '会议记录'}).waitFor();
  assert.ok(await page.isHidden('#browseAddRow'), '都已加入后不再显示全部加入');
  await page.locator('#crumbs').getByRole('button', {name: '资料'}).click();
  await page.locator('#browseAdd').getByText('全部加入（1 个，含子文件夹）').waitFor();
  await page.focus('#tabBrowse');
  await page.keyboard.press('ArrowLeft');
  assert.strictEqual(await page.getAttribute('#tabSearch', 'aria-selected'), 'true');
  assert.ok(await page.isVisible('#q'));
  assert.strictEqual(await page.evaluate(() => document.activeElement.id), 'tabSearch');
});

test('点搜索结果里的文件夹跳到浏览里那个文件夹', async t => {
  const {page} = await launch(t);
  await page.fill('#q', '会议');
  await page.getByRole('button', {name: '浏览文件夹 资料/子目录'}).click();
  assert.strictEqual(await page.getAttribute('#tabBrowse', 'aria-selected'), 'true');
  await page.locator('#crumbs [aria-current]', {hasText: '子目录'}).waitFor();
  await page.locator('#browseList li', {hasText: '会议记录.md'}).waitFor();
});

test('浏览里的全部加入超过已选名额时不可点并写明', async t => {
  const files = {};
  for (let i = 1; i <= 51; i++) files['大批/文件' + i + '.txt'] = String(i);
  const {page} = await launch(t, {files});
  await page.click('#tabBrowse');
  await page.locator('#browseList button.dir').first().click();
  await page.getByRole('button', {name: '打开文件夹 大批'}).click();
  const b = page.getByRole('button', {name: '全部加入（51 个，含子文件夹）'});
  await b.waitFor();
  assert.ok(await b.isDisabled());
  assert.match(await page.textContent('#browseAddHint'), /还能加的 50 个/);
});

test('360px 下浏览不横向溢出', async t => {
  const {page} = await launch(t, {width: 360});
  await page.click('#tabBrowse');
  await page.locator('#browseList button.dir').first().click();
  await page.getByRole('button', {name: '打开文件夹 子目录'}).waitFor();
  await noHorizontalScroll(page);
});


// ---------------- v1.8 继续与重新生成

// 拦下 /api/chat：第 n 次请求按 replies[n] 返回这些 ndjson 行，没给的照常交给服务；返回记下的各次请求正文
async function fakeChat(page, replies) {
  const bodies = [];
  await page.route('**/api/chat', route => {
    bodies.push(JSON.parse(route.request().postData()));
    const lines = replies[bodies.length - 1];
    if (!lines) return route.continue();
    return route.fulfill({status: 200, contentType: 'application/x-ndjson', body: lines.map(x => JSON.stringify(x)).join('\n') + '\n'});
  });
  return bodies;
}
const CUT = [{start: true, truncated: false}, {t: '前半段'}, {error: '连接中断：测试'}];
const CONTINUE_ASK = '继续：从上一条回答的断处接着写，不要重复已写的内容。';

test('回答中断后「继续」：新增一轮继续提问，不完整回答留在历史里并保留标记', async t => {
  const {page} = await launch(t);
  const bodies = await fakeChat(page, [CUT]);
  await pickByName(page, '季度总结');
  await page.fill('#ask', '总结');
  await page.click('#send');
  await page.locator('#chat').getByText('（回答不完整）').waitFor();
  await page.getByRole('button', {name: '继续这条不完整的回答'}).click();
  await page.locator('#chat').getByText('你好，世界').waitFor();
  const msgs = page.locator('#chat .msg');
  assert.strictEqual(await msgs.count(), 4);
  assert.strictEqual(await msgs.nth(2).locator('.msg-body').textContent(), CONTINUE_ASK);
  assert.strictEqual(await msgs.nth(1).locator('.partial').count(), 1, '上一条仍标不完整');
  assert.strictEqual(await page.locator('#chat button.resume').count(), 0, '最后一条完整时不再有按钮');
  assert.deepStrictEqual(bodies[1].messages, [{role: 'user', content: '总结'}, {role: 'assistant', content: '前半段'},
    {role: 'user', content: CONTINUE_ASK}]);
  const text = await readDownload(page, () => page.click('#save'));
  assert.match(text, /## 问\n\n继续：从上一条回答的断处接着写/);
});

test('「重新生成」换掉不完整回答，历史不含它；没拿到内容时恢复原回答与按钮', async t => {
  const {page} = await launch(t);
  const bodies = await fakeChat(page, [CUT, [{start: true, truncated: false}, {error: '接口 120 秒没有响应'}]]);
  await pickByName(page, '季度总结');
  await page.fill('#ask', '总结');
  await page.click('#send');
  await page.locator('#chat').getByText('（回答不完整）').waitFor();
  await page.getByRole('button', {name: '重新生成这条不完整的回答'}).click();
  await page.locator('#askStatus').getByText(/回答失败/).waitFor();
  await page.getByRole('button', {name: '重新生成这条不完整的回答'}).waitFor();
  assert.strictEqual(await page.locator('#chat .msg').count(), 2);
  assert.strictEqual(await page.locator('#chat .msg.assistant .msg-body').textContent(), '前半段');
  assert.deepStrictEqual(bodies[1].messages, [{role: 'user', content: '总结'}]);
  await page.getByRole('button', {name: '重新生成这条不完整的回答'}).click();
  await page.locator('#chat').getByText('你好，世界').waitFor();
  assert.strictEqual(await page.locator('#chat .msg').count(), 2, '新回答出现在原位置');
  assert.strictEqual(await page.locator('#chat .partial').count(), 0);
  assert.deepStrictEqual(bodies[2].messages, [{role: 'user', content: '总结'}]);
});

test('点「停止」后出现继续与重新生成，生成中不显示旧的，只在最后一条显示', async t => {
  const {page} = await launch(t, {model: 'slow'});
  await pickByName(page, '季度总结');
  await page.fill('#ask', '总结');
  await page.click('#send');
  await page.locator('#chat').getByText('开始').waitFor();
  await page.click('#stop');
  const go = page.getByRole('button', {name: '继续这条不完整的回答'});
  await go.waitFor();
  assert.ok(await go.isEnabled());
  await go.click();
  await page.locator('#chat .msg').nth(3).getByText('开始').waitFor();
  assert.strictEqual(await page.locator('#chat button.resume').count(), 0);
  await page.click('#stop');
  await go.waitFor();
  assert.strictEqual(await page.locator('#chat button.resume').count(), 2);
  assert.strictEqual(await page.locator('#chat .partial').count(), 2);
  assert.strictEqual(await page.locator('#chat .msg').last().locator('button.resume').count(), 2);
});

// ---------------- v1.8 file:/// 地址

test('文件夹输入框接受 file:/// 地址，带主机名时写明原因，补全写成普通路径', async t => {
  const {page, home} = await launch(t, {folders: false, homeDirs: ['文档/项目']});
  const url = 'file://' + home.split('/').map(encodeURIComponent).join('/');
  await page.fill('#folder', url + '/');
  await page.waitForFunction(h => [...document.querySelectorAll('#folderList option')].some(o => o.value === h + '/文档'), home);
  await page.fill('#folder', 'file://localhost' + home + '/文档');
  await page.click('#addFolder');
  await page.locator('#folderStatus', {hasText: '添加失败'}).waitFor();
  assert.strictEqual(await page.textContent('#folderStatus'), '添加失败：只支持本机 file:/// 地址。请改用以 / 开头的路径。');
  await page.fill('#folder', url + '/%E6%96%87%E6%A1%A3/');
  await page.click('#addFolder');
  await page.locator('#folderStatus').getByText(/已添加/).waitFor();
  assert.strictEqual(await page.textContent('#folders li .name'), home + '/文档');
  assert.match(await page.getAttribute('#folder', 'placeholder'), /file:\/\/\//);
});

// ---------------- v1.8 按文件名去重

test('同名规则：页面与服务端对同一组样例结果一致', async t => {
  const {page} = await launch(t);
  const cases = JSON.parse(fs.readFileSync(path.join(__dirname, 'same_name_cases.json'), 'utf-8'));
  const names = [...new Set(cases.same.flat().concat(cases.different.flat()))];
  const code = 'import importlib.util, json, sys\n' +
    "spec = importlib.util.spec_from_file_location('m', sys.argv[1])\n" +
    'm = importlib.util.module_from_spec(spec)\nspec.loader.exec_module(m)\n' +
    'print(json.dumps([m.same_name_key(n) for n in json.load(sys.stdin)]))';
  const py = JSON.parse(execFileSync(PYTHON, ['-c', code, path.join(TOOL, 'ask_local_files.py')], {input: JSON.stringify(names)}).toString());
  const js = await page.evaluate(ns => ns.map(sameNameKey), names);
  assert.deepStrictEqual(js, py);
  const key = Object.fromEntries(names.map((n, i) => [n, js[i]]));
  cases.same.forEach(g => assert.strictEqual(new Set(g.map(n => key[n])).size, 1, g.join('、')));
  cases.different.forEach(([a, b]) => assert.notStrictEqual(key[a], key[b], a + ' / ' + b));
});

test('搜索按文件名去重：写出去重前后个数，另有 N 个同名就地展开，单个加入同名时提示，开关联动并记住', async t => {
  const {page} = await launch(t, {files: {'报告.txt': '甲', '报告 (1).txt': '乙', '子目录/报告 - 副本.txt': '丙'}});
  await page.fill('#q', '报告');
  await page.locator('#findStatus').getByText('找到 3 个（去重后 1 个）').waitFor();
  assert.strictEqual(await page.locator('#results > li').count(), 1);
  const more = page.getByRole('button', {name: '另有 2 个与 报告.txt 同名的文件'});
  assert.strictEqual(await more.getAttribute('aria-expanded'), 'false');
  await more.click();
  assert.strictEqual(await more.getAttribute('aria-expanded'), 'true');
  assert.strictEqual(await page.locator('#results ul.dups > li').count(), 2);
  await page.getByRole('button', {name: '预览 资料/报告 (1).txt'}).click();
  await page.locator('#results ul.dups pre', {hasText: '乙'}).waitFor();
  await page.getByRole('button', {name: '加入 报告.txt', exact: true}).click();
  await page.locator('#picked li', {hasText: '报告.txt'}).getByText(/\d 字/).waitFor();
  await page.getByRole('button', {name: '加入 资料/报告 (1).txt'}).click();
  await page.locator('#pickStatus', {hasText: '已选里已有同名文件：报告.txt'}).waitFor();
  assert.match(await page.getAttribute('#pickStatus', 'class'), /\binfo\b/);
  assert.strictEqual(await page.locator('#picked > li').count(), 2, '同名文件照常加入');
  assert.strictEqual(await more.getAttribute('aria-expanded'), 'true', '重绘后保持展开');
  await page.locator('#results ul.dups pre', {hasText: '乙'}).waitFor();
  await page.uncheck('#dedupSearch');
  await page.locator('#results > li').nth(2).waitFor();
  assert.strictEqual(await page.isChecked('#dedupBrowse'), false, '两处联动');
  await page.reload();
  await page.waitForFunction(() => document.querySelector('#ver').textContent.includes('v') && !document.querySelector('#dedupSearch').checked);
});

test('搜索的全部加入只加去重后留下的，跳过已选里有同名的', async t => {
  const {page} = await launch(t, {files: {'甲/计划.txt': '甲', '乙/计划 (1).txt': '乙', '计划书.txt': '丙'}});
  await page.fill('#q', '计划 (1)');
  await page.getByRole('button', {name: '加入 计划 (1).txt'}).click();
  await page.locator('#picked li', {hasText: '计划 (1).txt'}).getByText(/\d 字/).waitFor();
  await page.fill('#q', '计划');
  const all = page.getByRole('button', {name: '全部加入（1 个）'});
  await all.click();
  await page.locator('#pickStatus', {hasText: '已加入 1 个，1 个与已选同名的已跳过'}).waitFor();
  assert.deepStrictEqual(await page.locator('#picked .name').allTextContents(), ['计划 (1).txt', '计划书.txt']);
});

test('浏览在服务端去重：状态行写出去重前后数量，全部加入按去重后计数，关掉后恢复', async t => {
  const {page} = await launch(t, {files: {'报告.txt': '甲', '报告 (1).txt': '乙', '子目录/报告_副本.txt': '丙'}});
  await page.click('#tabBrowse');
  await page.locator('#browseList button.dir').first().click();
  await page.locator('#findStatus').getByText('本层 4 个文件（去重后 3 个）、1 个子文件夹').waitFor();
  await page.locator('#browseAdd', {hasText: '全部加入（3 个，含子文件夹）'}).waitFor();
  assert.strictEqual(await page.locator('#browseList [data-act="dups"]').count(), 1);
  await page.uncheck('#dedupBrowse');
  await page.locator('#browseAdd', {hasText: '全部加入（5 个，含子文件夹）'}).waitFor();
  assert.ok(!(await page.isChecked('#dedupSearch')));
  assert.doesNotMatch(await page.textContent('#findStatus'), /去重后/);
});

// ---------------- v1.8 原生目录选择

function fakePickerBin(t) {
  const bin = fs.mkdtempSync(path.join(os.tmpdir(), 'ask-local-files-bin-'));
  fs.symlinkSync(path.join(__dirname, 'fake_picker.sh'), path.join(bin, 'zenity'));
  t.after(() => fs.rmSync(bin, {recursive: true, force: true}));
  return {bin, ctl: path.join(bin, 'pick.txt')};
}

test('「选择……」弹出选择框：期间禁用并提示，选好只填进输入框，取消不改不提示，失败写明原因', async t => {
  const {bin, ctl} = fakePickerBin(t);
  fs.writeFileSync(ctl, '/选中的/文件夹');
  const {page} = await launch(t, {folders: false, env: {PATH: bin + ':' + process.env.PATH, DISPLAY: ':99',
    FAKE_PICK_FROM: ctl, FAKE_PICK_SLEEP: '1'}});
  const b = page.getByRole('button', {name: '选择要添加的文件夹'});
  await b.click();
  await page.locator('#folderStatus', {hasText: '请在弹出的窗口里选择文件夹……（窗口可能在浏览器后面）'}).waitFor();
  assert.ok(await b.isDisabled());
  await page.waitForFunction(() => document.querySelector('#folder').value === '/选中的/文件夹');
  assert.strictEqual(await page.textContent('#folderStatus'), '');
  assert.match(await page.textContent('#folders'), /还没有添加文件夹/, '不自动添加');
  fs.writeFileSync(ctl, 'cancel');
  await b.click();
  await page.waitForFunction(() => !document.querySelector('#pickFolder').disabled);
  assert.strictEqual(await page.inputValue('#folder'), '/选中的/文件夹');
  assert.strictEqual(await page.textContent('#folderStatus'), '');
  fs.writeFileSync(ctl, 'fail');
  await b.click();
  await page.locator('#folderStatus', {hasText: '选择失败'}).waitFor();
  assert.strictEqual(await page.textContent('#folderStatus'), '选择失败：cannot open display。请直接输入路径。');
  await noHorizontalScroll(page);
});

test('没有可用的选择窗口时不显示「选择……」', async t => {
  const {page} = await launch(t, {env: {DISPLAY: '', WAYLAND_DISPLAY: ''}});
  await page.locator('#results li').first().waitFor();
  assert.ok(await page.isHidden('#pickFolder'));
});

test('360px 下选择按钮、去重开关与同名展开都不横向溢出', async t => {
  const {bin} = fakePickerBin(t);
  const {page} = await launch(t, {width: 360, files: {'很长很长的报告文件名称用来测试换行.txt': '甲', '很长很长的报告文件名称用来测试换行 - 副本.txt': '乙'},
    env: {PATH: bin + ':' + process.env.PATH, DISPLAY: ':99'}});
  await page.click('#manageFolders');
  await page.fill('#q', '很长');
  await page.getByRole('button', {name: /另有 1 个/}).click();
  await page.locator('#results ul.dups li').waitFor();
  await noHorizontalScroll(page);
});

// ---------------- v1.11 从列表移除、全部加入放宽、正则搜索

test('搜索里从列表移除：该行消失、已选不变、状态写明、全部加入减 1，焦点到下一行，改关键字后恢复', async t => {
  const {page} = await launch(t, {files: {'报告甲.txt': '甲', '报告乙.md': '乙', '报告丙.txt': '丙'}});
  await pickByName(page, '报告甲');
  await page.fill('#q', '报告');
  await page.getByRole('button', {name: '全部加入（2 个）'}).waitFor();
  await page.getByRole('button', {name: '从列表移除 报告甲.txt'}).click();
  await page.locator('#findStatus', {hasText: '找到 3 个，已从列表移除 1 个'}).waitFor();
  assert.strictEqual(await page.locator('#results > li').count(), 2);
  assert.strictEqual(await page.locator('#picked > li', {hasText: '报告甲'}).count(), 1, '已选不变');
  await page.getByRole('button', {name: '从列表移除 报告乙.md'}).click();
  await page.locator('#findStatus', {hasText: '已从列表移除 2 个'}).waitFor();
  await page.getByRole('button', {name: '全部加入（1 个）'}).waitFor();
  assert.strictEqual(await page.evaluate(() => document.activeElement.getAttribute('aria-label')), '从列表移除 报告丙.txt');
  await page.keyboard.press('Enter');
  await page.locator('#findStatus', {hasText: '已从列表移除 3 个'}).waitFor();
  assert.ok(await page.isHidden('#addAllRow'));
  await page.keyboard.press('Enter');     // 焦点回到搜索框，回车不加入被拿掉的
  assert.strictEqual(await page.locator('#picked > li').count(), 1);
  await page.fill('#q', '报告 ');
  await page.locator('#results > li').nth(2).waitFor();
  assert.doesNotMatch(await page.textContent('#findStatus'), /已从列表移除/);
});

test('去重时从列表移除连同同名一起不显示', async t => {
  const {page} = await launch(t, {files: {'报告.txt': '甲', '报告 (1).txt': '乙', '计划.txt': '丙'}});
  await page.fill('#q', '.txt');
  await page.locator('#results [data-act="dups"]').waitFor();
  const name = await page.locator('#results > li', {has: page.locator('[data-act="dups"]')}).locator('.name').textContent();
  await page.getByRole('button', {name: '从列表移除 ' + name}).click();
  await page.locator('#findStatus', {hasText: '已从列表移除 1 个'}).waitFor();
  await page.uncheck('#dedupSearch');
  await page.locator('#findStatus', {hasText: '已从列表移除 2 个'}).waitFor();
  assert.deepStrictEqual((await page.locator('#results .name').allTextContents()).sort(), ['季度总结.txt', '计划.txt']);
});

test('去重后 52 个时没有全部加入，从列表移除 3 个后出现全部加入（49 个）', async t => {
  const files = {};
  for (let i = 1; i <= 52; i++) files['报告' + i + '.txt'] = String(i);
  files['报告1 (1).txt'] = '副本';
  const {page} = await launch(t, {files});
  await page.fill('#q', '报告');
  await page.locator('#findStatus', {hasText: '找到 53 个（去重后 52 个）'}).waitFor();
  assert.ok(await page.isHidden('#addAllRow'));
  for (const i of [2, 3, 4]) {
    await page.getByRole('button', {name: '从列表移除 报告' + i + '.txt', exact: true}).click();
    await page.locator('#findStatus', {hasText: '已从列表移除 ' + (i - 1) + ' 个'}).waitFor();
  }
  await page.getByRole('button', {name: '全部加入（49 个）'}).waitFor();
});

test('浏览里从列表移除：该行消失、全部加入减 1，进别的文件夹后恢复', async t => {
  const {page} = await launch(t);
  await page.click('#tabBrowse');
  await page.locator('#browseList button.dir').first().click();
  await page.locator('#browseAdd', {hasText: '全部加入（2 个，含子文件夹）'}).waitFor();
  await page.getByRole('button', {name: '从列表移除 季度总结.txt'}).click();
  await page.locator('#findStatus', {hasText: '已从列表移除 1 个'}).waitFor();
  assert.strictEqual(await page.locator('#browseList li', {hasText: '季度总结'}).count(), 0);
  await page.locator('#browseAdd', {hasText: '全部加入（1 个，含子文件夹）'}).waitFor();
  await page.getByRole('button', {name: '打开文件夹 子目录'}).click();
  await page.locator('#crumbs [aria-current]', {hasText: '子目录'}).waitFor();
  await page.locator('#crumbs').getByRole('button', {name: '资料'}).click();
  await page.locator('#browseList li', {hasText: '季度总结'}).waitFor();
  assert.doesNotMatch(await page.textContent('#findStatus'), /已从列表移除/);
});

test('正则表达式：勾选后按正则搜索并记住，写错时写明原因、清空结果', async t => {
  const {page} = await launch(t, {files: {'季度报告3.txt': '丁'}});
  assert.ok(!(await page.isChecked('#regex')));
  await page.check('#regex');
  assert.match(await page.getAttribute('#q', 'placeholder'), /^正则表达式，如 需求\.\*v\\d$/);
  await page.fill('#q', '季度.*\\d');
  await page.locator('#findStatus', {hasText: '找到 1 个'}).waitFor();
  assert.deepStrictEqual(await page.locator('#results .name').allTextContents(), ['季度报告3.txt']);
  await page.fill('#q', '季度(');
  await page.locator('#findStatus', {hasText: '搜索失败：正则表达式有误：'}).waitFor();
  assert.match(await page.textContent('#findStatus'), /请修改后再搜。$/);
  assert.match(await page.getAttribute('#findStatus', 'class'), /\bbad\b/);
  assert.strictEqual(await page.locator('#results > li').count(), 0);
  await page.reload();
  await page.waitForFunction(() => document.querySelector('#regex').checked);
  await page.click('#tabSearch');      // 重新打开默认在「浏览文件夹」
  await page.uncheck('#regex');
  assert.doesNotMatch(await page.getAttribute('#q', 'placeholder'), /正则/);
});

// ---------------- v1.10 界面优化

test('打开页面默认显示「浏览文件夹」，排在「搜索」前面；Home、End 随之调整', async t => {
  const {page} = await launch(t, {tab: 'browse'});
  assert.deepStrictEqual(await page.locator('#findBody [role=tab]').allTextContents(), ['浏览文件夹', '搜索']);
  assert.strictEqual(await page.getAttribute('#tabBrowse', 'aria-selected'), 'true');
  await page.locator('#crumbs [aria-current]', {hasText: '全部'}).waitFor();
  assert.ok(await page.isHidden('#q'));
  await page.focus('#tabBrowse');
  await page.keyboard.press('End');
  assert.strictEqual(await page.getAttribute('#tabSearch', 'aria-selected'), 'true');
  await page.keyboard.press('Home');
  assert.strictEqual(await page.getAttribute('#tabBrowse', 'aria-selected'), 'true');
  assert.strictEqual(await page.evaluate(() => document.activeElement.id), 'tabBrowse');
});

test('管理文件夹、模型设置带图标与展开箭头', async t => {
  const {page} = await launch(t);
  for (const id of ['manageFolders', 'modelSettings']) {
    const b = page.locator('#' + id);
    assert.strictEqual(await b.locator('svg.icon').count(), 1);
    assert.strictEqual(await b.locator('.arrow').textContent(), '▸');
    assert.notStrictEqual(await b.evaluate(e => getComputedStyle(e).borderTopStyle), 'none');
    await b.click();
    assert.strictEqual(await b.locator('.arrow').textContent(), '▾');
    assert.strictEqual(await b.getAttribute('aria-expanded'), 'true');
  }
  assert.strictEqual(await page.getByRole('button', {name: '管理文件夹'}).count(), 1);
  assert.strictEqual(await page.getByRole('button', {name: '模型设置'}).count(), 1);
});

test('清空已选、清空对话用醒目的红色', async t => {
  const {page} = await launch(t);
  await pickByName(page, '季度总结');
  const bad = await page.evaluate(() => getComputedStyle(document.querySelector('.bad')).color);
  for (const id of ['#clearPicked', '#clear']) {
    assert.strictEqual(await page.$eval(id, e => getComputedStyle(e).color), bad);
    assert.strictEqual(await page.$eval(id, e => getComputedStyle(e).borderTopColor), bad);
  }
});

test('文件夹用文件夹图标与单独的颜色，与文件区分', async t => {
  const {page} = await launch(t, {tab: 'browse'});
  await page.locator('#browseList button.dir').first().click();
  const dir = page.getByRole('button', {name: '打开文件夹 子目录'});
  await dir.waitFor();
  assert.strictEqual(await dir.locator('svg.icon').count(), 1);
  const folder = await dir.evaluate(e => getComputedStyle(e).color);
  const file = await page.locator('#browseList .name').first().evaluate(e => getComputedStyle(e).color);
  assert.notStrictEqual(folder, file);
  await page.click('#tabSearch');
  await page.fill('#q', '会议');
  const link = page.getByRole('button', {name: '浏览文件夹 资料/子目录'});
  await link.waitFor();
  assert.strictEqual(await link.locator('svg.icon').count(), 1);
  assert.strictEqual(await link.evaluate(e => getComputedStyle(e).color), folder);
});

test('回答按 Markdown 显示，不用 innerHTML，链接不可点，复制仍是原文', async t => {
  const {page} = await launch(t, {model: 'md'});
  let alerted = false;
  page.onDialog = d => { alerted = true; d.dismiss(); };
  await pickByName(page, '季度总结');
  await ask(page);
  const body = page.locator('#chat .msg.assistant .msg-body');
  await body.locator('table').waitFor();
  assert.strictEqual(await body.locator('h1').textContent(), '标题');
  assert.strictEqual(await body.locator('strong').textContent(), '加粗');
  assert.strictEqual(await body.locator('em').textContent(), '斜体');
  assert.strictEqual(await body.locator('p code').textContent(), '代码');
  assert.strictEqual(await body.locator('a').count(), 0, '链接不应可点');
  assert.match(await body.textContent(), /说明（https:\/\/example\.com\/a）/);
  assert.deepStrictEqual(await body.locator('ul > li > ul > li').allTextContents(), ['甲一']);
  assert.deepStrictEqual(await body.locator('ol > li').allTextContents(), ['一', '二']);
  assert.strictEqual(await body.locator('blockquote').textContent(), '引用');
  assert.strictEqual(await body.locator('hr').count(), 1);
  assert.deepStrictEqual(await body.locator('th').allTextContents(), ['列一', '列二']);
  assert.strictEqual(await body.locator('td').nth(1).evaluate(e => e.style.textAlign), 'right');
  assert.strictEqual(await body.locator('table').evaluate(e => e.parentElement.className), 'table-wrap');
  assert.strictEqual(await body.locator('pre code').textContent(), 'x < y');
  assert.strictEqual(await body.locator('img').count(), 0, '原文里的 HTML 只当文字显示');
  assert.match(await body.textContent(), /<img src=x onerror=alert\(1\)>/);
  assert.ok(!alerted);
  await page.locator('#chat .msg.assistant .who').getByRole('button', {name: '复制这条回答'}).click();
  await page.locator('#chatStatus').getByText(/已复制/).waitFor();
  const copied = await page.evaluate(() => navigator.clipboard.readText());
  assert.match(copied, /^# 标题\n\n\*\*加粗\*\*/);
  const saved = await readDownload(page, () => page.click('#save'));
  assert.match(saved, /\| 列一 \| 列二 \|/);
});

test('360px 下 Markdown 表格只在表格容器里横向滚动', async t => {
  const wide = '| ' + Array.from({length: 12}, (_, i) => '很长的列标题' + i).join(' | ') + ' |\n|' + ' --- |'.repeat(12) + '\n';
  const {page} = await launch(t, {width: 360});
  await page.evaluate(text => {
    const d = document.createElement('div');
    d.className = 'msg assistant';
    d.appendChild(mdBody({content: text}));
    document.querySelector('#chat').appendChild(d);
  }, wide);
  await page.locator('#chat table').waitFor();
  await noHorizontalScroll(page);
  assert.ok(await page.$eval('#chat .table-wrap', e => e.scrollWidth > e.clientWidth));
});

test('宽屏左栏不整体滚动：结果列表占满剩余高度且只有它一条滚动条，已选最高约四成', async t => {
  const files = {};
  for (let i = 1; i <= 60; i++) files['多/文件' + i + '.txt'] = String(i);
  const {page} = await launch(t, {width: 1280, files});
  await page.fill('#q', '文件');
  await page.locator('#results li').nth(40).waitFor();
  const col = await page.$eval('.col-left', e => ({o: getComputedStyle(e).overflowY, sh: e.scrollHeight, h: e.clientHeight}));
  assert.strictEqual(col.o, 'hidden');
  assert.ok(col.sh <= col.h + 1, '左栏整体不应滚动');
  const r = await page.$eval('#results', e => ({sh: e.scrollHeight, h: e.clientHeight}));
  assert.ok(r.sh > r.h, '结果列表应自己滚动');
  const st = await box(page, '#findStatus'), res = await box(page, '#results'), card = await box(page, '#pickedCard');
  assert.ok(res.y > st.y && res.y + res.height <= card.y + 1, '列表在状态下方、已选上方');
  // 展开管理文件夹时面板在列表上方，列表变矮
  const before = (await box(page, '#results')).height;
  await page.click('#manageFolders');
  const panel = await box(page, '#folderPanel'), after = await box(page, '#results');
  assert.ok(panel.y < after.y && after.height < before, '面板在列表上方，列表相应变矮');
  assert.ok((await page.$eval('.col-left', e => e.scrollHeight - e.clientHeight)) <= 1);
  await page.click('#manageFolders');
  // 已选加多后高度有上限，自己滚动
  for (let i = 1; i <= 20; i++) await page.locator('#results li', {hasText: '文件' + i + '.txt'}).first().getByRole('button', {name: /加入/}).click();
  await page.locator('#picked li').nth(19).waitFor();
  const colBox = await box(page, '.col-left'), card2 = await box(page, '#pickedCard');
  assert.ok(card2.height <= colBox.height * 0.42, `已选高度应有上限：${card2.height} / ${colBox.height}`);
  const p = await page.$eval('#picked', e => ({sh: e.scrollHeight, h: e.clientHeight}));
  assert.ok(p.sh > p.h, '已选列表应自己滚动');
});

test('＋ 新对话：同一窗口内开标签，各自独立；方向键切换；最多 8 个', async t => {
  const {page} = await launch(t);
  assert.ok(await page.isHidden('#convBar'), '只有一个对话时不显示标签栏');
  await pickByName(page, '季度总结');
  await page.click('#newConv');
  assert.ok(await page.isVisible('#convBar'));
  assert.deepStrictEqual(await page.locator('#convTabs [role=tab]').allTextContents(), ['对话 1', '对话 2']);
  assert.ok(await page.isHidden('#convMain'));
  const f = page.frameLocator('#convFrames iframe');
  await f.locator('#crumbs [aria-current]', {hasText: '全部'}).waitFor();
  assert.ok(await f.locator('header').isHidden(), '标签里不显示页头');
  assert.ok(await f.locator('footer').isHidden(), '标签里不显示页脚');
  assert.strictEqual(await f.locator('#picked li .name').count(), 0, '新对话的已选为空');
  await page.locator('#convTab2').focus();
  await page.keyboard.press('ArrowLeft');
  assert.strictEqual(await page.getAttribute('#convTab1', 'aria-selected'), 'true');
  assert.ok(await page.isVisible('#convMain'));
  assert.strictEqual(await page.locator('#picked li .name').count(), 1, '对话 1 的已选保留');
  for (let i = 3; i <= 8; i++) await page.click('#newConv');
  assert.strictEqual(await page.locator('#convTabs [role=tab]').count(), 8);
  assert.ok(await page.isDisabled('#newConv'));
  assert.match(await page.textContent('#convStatus'), /最多同时开 8 个对话/);
  // 同一窗口的标签只算一个页面
  await page.waitForFunction(() => [...document.querySelectorAll('#convFrames iframe')].every(f => f.contentWindow.askConv));
  const pages = await page.evaluate(() => fetch('/api/ping', {method: 'POST', headers: {'X-Token': new URLSearchParams(location.search).get('t'), 'Content-Type': 'application/json'},
    body: JSON.stringify({page: 'probe'})}).then(r => r.json()).then(j => j.pages));
  assert.strictEqual(pages, 2, '本窗口加探测页面共 2 个');
  await page.getByRole('button', {name: '关闭 对话 8'}).click();
  assert.strictEqual(await page.locator('#convTabs [role=tab]').count(), 7);
  assert.ok(!(await page.isDisabled('#newConv')));
  await noHorizontalScroll(page);
});

test('关闭有未保存问答的标签先确认；退出按全部标签合计提醒，退出后标签一起结束', async t => {
  const {page} = await launch(t);
  await pickByName(page, '季度总结');
  await ask(page);
  await page.click('#newConv');
  const f = page.frameLocator('#convFrames iframe');
  await f.locator('#tabSearch').click();
  await f.locator('#q').fill('会议');
  await f.locator('#results li', {hasText: '会议记录'}).getByRole('button', {name: /加入/}).click();
  await f.locator('#picked li', {hasText: '会议记录'}).getByText(/\d 字/).waitFor();
  await f.locator('#ask').fill('总结');
  await f.locator('#ask').press('Enter');
  await f.locator('#chat .msg.assistant').getByText('你好，世界').waitFor();
  let msg = '';
  page.onDialog = d => { msg = d.message(); d.dismiss(); };
  await page.getByRole('button', {name: '关闭 对话 2'}).click();
  assert.match(msg, /关闭「对话 2」？\n1 轮问答未保存/);
  assert.strictEqual(await page.locator('#convFrames iframe').count(), 1, '取消后标签保留');
  msg = '';
  await page.click('#quit');
  await page.waitForFunction(() => !document.querySelector('#quit').disabled);
  assert.match(msg, /2 个对话标签共 2 轮问答未保存/);
  assert.doesNotMatch(msg, /还有 \d+ 个页面开着/, '同一窗口的标签不算别的页面');
  page.onDialog = d => d.accept();
  await page.click('#quit');
  await page.locator('#closed').waitFor({state: 'visible'});
  await f.locator('#closed').waitFor({state: 'visible'});
});

test('关闭没有问答的标签不确认，回到前一个标签', async t => {
  const {page} = await launch(t);
  let asked = false;
  page.onDialog = d => { asked = true; d.accept(); };
  await page.click('#newConv');
  await page.click('#newConv');
  await page.getByRole('button', {name: '关闭 对话 3'}).click();
  assert.ok(!asked);
  assert.strictEqual(await page.getAttribute('#convTab2', 'aria-selected'), 'true');
  assert.strictEqual(await page.evaluate(() => document.activeElement.id), 'convTab2');
});
