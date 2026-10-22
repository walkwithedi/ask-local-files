// ask_local_files.html 的界面测试：node --test ask-local-files/tests/ui.test.js
// 需要 Playwright（云端会话已预装，NODE_PATH 指向全局模块）；离线工作站不需要运行。
'use strict';
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn, execSync} = require('node:child_process');
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
// opts.clock 时接管页面计时器（用 page.clock.fastForward 快进），opts.beforeGoto(page) 在打开页面前调用
async function launch(t, opts) {
  opts = Object.assign({folders: true, model: 'ok', width: 1280, files: {}, homeDirs: []}, opts || {});
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'ask-local-files-ui-'));
  const docs = path.join(home, '资料');
  const files = Object.assign({'季度总结.txt': '甲'.repeat(100), '子目录/会议记录.md': '乙'.repeat(50), '旧文件.doc': 'x'}, opts.files);
  for (const [rel, text] of Object.entries(files)) {
    fs.mkdirSync(path.dirname(path.join(docs, rel)), {recursive: true});
    fs.writeFileSync(path.join(docs, rel), text);
  }
  for (const d of opts.homeDirs) fs.mkdirSync(path.join(home, d), {recursive: true});
  const conf = {folders: opts.folders ? [docs] : [], char_limit: opts.limit || 100000};
  if (opts.model) Object.assign(conf, {custom: {base: modelBase, model: opts.model, key: 'k'}, model: 'custom'});
  fs.mkdirSync(path.join(home, '.config', 'ask-local-files'), {recursive: true});
  fs.writeFileSync(path.join(home, '.config', 'ask-local-files', 'config.json'), JSON.stringify(conf));
  const env = Object.assign({}, process.env, {HOME: home, XDG_CONFIG_HOME: path.join(home, '.config'),
    XDG_CACHE_HOME: path.join(home, '.cache'), LOG_LEVEL: 'ERROR'});
  if (opts.noPdf) env.PATH = '';
  const proc = spawn(PYTHON, [path.join(TOOL, 'ask_local_files.py'), '--no-browser'], {env, stdio: ['ignore', 'pipe', 'ignore']});
  const url = await firstLine(proc, /网页界面：(\S+)/);
  const context = await browser.newContext({viewport: {width: opts.width, height: 800}});
  const page = await context.newPage();
  page.setDefaultTimeout(5000);
  page.on('dialog', d => (page.onDialog ? page.onDialog(d) : d.accept()));
  if (opts.clock) await page.clock.install();
  if (opts.beforeGoto) await opts.beforeGoto(page);
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  await page.goto(url);
  t.after(async () => {
    await context.close();
    proc.kill();
    fs.rmSync(home, {recursive: true, force: true});
  });
  return {page, home, docs, url};
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

test('宽屏分两栏，问 AI 在右栏并固定在视口里', async t => {
  const {page} = await launch(t, {width: 1280});
  const left = await box(page, '#findCard'), right = await box(page, '#askCard');
  assert.ok(right.x >= left.x + left.width, '问 AI 应在找文件右侧');
  assert.strictEqual(await page.$eval('.col-right', e => getComputedStyle(e).position), 'sticky');
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
  const {page} = await launch(t);
  await pickByName(page, '季度总结');
  assert.match(await page.textContent('#pickCount'), /1 \/ 20，共 100 字/);
  assert.doesNotMatch(await page.textContent('#pickStatus'), /合计/);
});

test('按钮统一在右侧，结果第二行只写所在文件夹', async t => {
  const {page} = await launch(t);
  await page.fill('#q', '会议');
  const li = page.locator('#results li', {hasText: '会议记录'});
  const b = await li.getByRole('button').boundingBox(), n = await li.locator('.name').boundingBox();
  assert.ok(b.x > n.x, '加入按钮应在文件名右侧');
  const meta = await li.locator('.meta').textContent();
  assert.match(meta, /^资料\/子目录/);
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
  const {page} = await launch(t, {folders: false, width: 800});
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

test('没有可用模型时展开模型设置，发送去向用 warn 样式', async t => {
  const {page} = await launch(t, {model: null});
  await page.waitForSelector('#modelPanel', {state: 'visible'});
  assert.match(await page.getAttribute('#dest', 'class'), /warn/);
  assert.match(await page.textContent('#model option'), /「模型设置」/);
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
  await page.locator('#results li', {hasText: '批量30'}).getByRole('button').focus();
  await page.waitForTimeout(2000);
  assert.strictEqual(await page.$eval('#results li', e => e.dataset.mark), '1', '结果没变时不应重绘');
  assert.ok(Math.abs(await page.$eval('#results', e => e.scrollTop) - 300) < 2);
  assert.strictEqual((await activeInfo(page)).row, '批量30.txt');
});

test('点加入后焦点回到同一行', async t => {
  const {page} = await launch(t);
  await page.fill('#q', '会议');
  await page.locator('#results li', {hasText: '会议记录'}).getByRole('button').focus();
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

test('读不了的格式在结果里标出并禁用加入', async t => {
  const {page} = await launch(t);
  await page.fill('#q', '旧文件');
  const li = page.locator('#results li', {hasText: '旧文件.doc'});
  assert.ok(await li.getByRole('button').isDisabled());
  assert.match(await li.locator('.meta').textContent(), /另存为/);
});

test('按钮的可访问名称带文件名', async t => {
  const {page} = await launch(t);
  await page.fill('#q', '季度');
  assert.strictEqual(await page.locator('#results li', {hasText: '季度总结'}).getByRole('button').getAttribute('aria-label'), '加入 季度总结.txt');
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

test('选满 20 个时禁用全部加入并说明', async t => {
  const {page} = await launch(t, {files: manyFiles(21)});
  for (let i = 1; i <= 20; i++) await pickByName(page, '批量' + String(i).padStart(2, '0'));
  await page.fill('#q', '批量21');
  const b = page.locator('#results li', {hasText: '批量21'}).getByRole('button');
  await page.waitForFunction(() => { const x = [...document.querySelectorAll('#results li button')]; return x.length && x.every(y => y.disabled); });
  assert.ok(await b.isDisabled());
  assert.match(await page.textContent('#findStatus'), /已选满 20 个/);
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
  await page.locator('#results li', {hasText: '数据.bin'}).getByRole('button').click();
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
  await page.locator('#picked .preview').getByText('显示前 20,000 字，共 25,000 字，发送时用全文').waitFor();
});

// ---------------- 第 5 批 提问、复制、保存、退出、约定一致性

async function readDownload(page, click) {
  const [d] = await Promise.all([page.waitForEvent('download'), click()]);
  return fs.readFileSync(await d.path(), 'utf-8');
}

test('发送按钮旁写明 Ctrl+Enter', async t => {
  const {page} = await launch(t);
  assert.match(await page.textContent('#sendHint'), /Ctrl\+Enter 发送/);
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
  const {page} = await launch(t);
  await pickByName(page, '季度总结');
  assert.match(await page.textContent('#dest'), /文件 100 字 \+ 对话 0 字/);
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
  await page.locator('#chat').getByRole('button', {name: /复制/}).click();
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
  assert.match(msg, /1 轮问答.*未保存/s);
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
    const li = page.locator('#picked li', {hasText: name});
    const a = await li.getByRole('button', {name: /预览/}).boundingBox(), b = await li.getByRole('button', {name: /移除/}).boundingBox();
    assert.ok(Math.abs(a.y - b.y) < 2, name + ' 的按钮不在同一行');
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


test('搜索结果少于 20 个时可全部加入，跳过读不了的', async t => {
  const {page} = await launch(t, {files: {'报告甲.txt': '甲', '报告乙.md': '乙', '报告丙.doc': 'x'}});
  assert.ok(await page.isHidden('#addAllRow'), '搜索框为空时不出现');
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
  for (let i = 1; i <= 18; i++) files['其他' + i + '.txt'] = String(i);
  const {page} = await launch(t, {files});
  await page.fill('#q', '其他');
  await page.getByRole('button', {name: '全部加入（18 个）'}).click();
  await page.fill('#q', '报告');
  const b = page.getByRole('button', {name: '全部加入（3 个）'});
  await b.waitFor();
  assert.ok(await b.isDisabled());
  assert.match(await page.textContent('#addAllHint'), /还能加 2 个/);
});

test('搜索结果达到 20 个时不出现全部加入', async t => {
  const files = {};
  for (let i = 1; i <= 20; i++) files['报告' + i + '.txt'] = String(i);
  const {page} = await launch(t, {files});
  await page.fill('#q', '报告');
  await page.locator('#results li', {hasText: '报告20'}).waitFor();
  assert.ok(await page.isHidden('#addAllRow'));
});

test('有对话时全部加入只确认一次', async t => {
  const {page} = await launch(t, {files: {'报告甲.txt': '甲', '报告乙.txt': '乙'}});
  await pickByName(page, '季度总结');
  await ask(page);
  let n = 0;
  page.onDialog = d => { n++; d.accept(); };
  await page.fill('#q', '报告');
  await page.getByRole('button', {name: '全部加入（2 个）'}).click();
  await page.locator('#picked li', {hasText: '报告乙'}).waitFor();
  assert.strictEqual(n, 1);
  assert.strictEqual(await page.locator('#chat .msg').count(), 0);
});
