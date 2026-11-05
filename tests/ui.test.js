// ask_local_files.html 的界面测试：node --test ask-local-files/tests/ui.test.js
// 需要 Playwright（云端会话已预装，NODE_PATH 指向全局模块）；离线工作站不需要运行。
'use strict';
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const net = require('node:net');
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

// 取一个空闲端口：服务默认用固定端口，各测试各起一份，互不占用
function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.on('error', reject);
    srv.listen(0, '127.0.0.1', () => { const {port} = srv.address(); srv.close(() => resolve(port)); });
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
// 页面打开时显示「全部」的文件夹，opts.tab 为 'browse' 时等它加载完
// opts.height 为视口高度；opts.mdDefault 时「忽略 Markdown 文件」按默认（勾选），否则测试里关闭；
// 打开后切到知识库卡片的「文件」标签，opts.kbTab 为 false 时不切；opts.optsOpen 为 false 时「选项」按默认收起
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
  if (!opts.mdDefault) conf.ignore_md = false;     // 各测试照常列出 .md；opts.mdDefault 时按默认（忽略）
  if (opts.model) Object.assign(conf, {custom: {base: modelBase, model: opts.model, key: 'k'}, model: 'custom'});
  Object.assign(conf, opts.conf || {});      // opts.conf 为另加的设置项
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
  // 默认保存位置指到临时目录，不在仓库的工具目录里建「数据」；opts.ownTool 时按工具目录下的「数据」照常运行
  if (!opts.ownTool) env.ASK_LOCAL_FILES_DATA = path.join(home, 'data');
  if (opts.noPdf) env.PATH = '';
  Object.assign(env, opts.env || {});
  if (opts.beforeStart) opts.beforeStart(home);
  // opts.ownTool 时把程序复制到临时目录运行，工具目录里的位置文件 data-dir.txt 不碰到仓库里的
  let tool = TOOL;
  if (opts.ownTool) {
    tool = path.join(home, 'tool');
    fs.mkdirSync(tool);
    for (const f of ['ask_local_files.py', 'ask_local_files.html', 'ask_memory.py', 'doc2kb_core.py']) fs.copyFileSync(path.join(TOOL, f), path.join(tool, f));
    if (opts.pointer) fs.writeFileSync(path.join(tool, 'data-dir.txt'), opts.pointer + '\n');
  }
  const args = [path.join(tool, 'ask_local_files.py'), '--no-browser', '--port', String(await freePort())];
  const proc = spawn(PYTHON, args.concat(opts.args || []), {env, stdio: ['ignore', 'pipe', 'ignore']});
  const url = await firstLine(proc, /网页界面：(\S+)/);
  if (opts.noPage) {      // 不打开页面：只看服务自己的行为
    t.after(() => { try { proc.kill(); } catch (e) { /* 已退出 */ } });
    return {page: null, home, docs, url, proc, env, args, tool};
  }
  const context = await browser.newContext({viewport: {width: opts.width, height: opts.height || 800}});
  const page = await context.newPage();
  page.setDefaultTimeout(5000);
  page.on('dialog', d => (page.onDialog ? page.onDialog(d) : d.accept()));
  if (opts.clock) await page.clock.install();
  // 找文件的「选项」默认收起；各测试照常要用排序与勾选框，打开前先记为展开（opts.optsOpen 为 false 时不动）
  if (opts.optsOpen !== false) await page.addInitScript(() => {
    try {
      const k = 'ask-local-files.find', v = JSON.parse(localStorage.getItem(k) || '{}');
      if (v.opts === undefined) { v.opts = true; localStorage.setItem(k, JSON.stringify(v)); }
    } catch (e) { /* 读不到时按默认 */ }
  });
  if (opts.beforeGoto) await opts.beforeGoto(page);
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  await page.goto(url);
  // 知识库为空时默认打开「概览」；找文件类的测试先切到「文件」标签，刷新或重新打开后同样切过去
  // （opts.kbTab 为 false 时不切，看打开时的默认标签）
  if (opts.kbTab !== false) {
    const toFiles = async () => {
      await page.locator('#kbtab-files').waitFor();
      await page.waitForFunction(() => document.querySelector('#findSummary').textContent !== '', null, {timeout: 3000});     // 状态已读到，默认标签已选好
      await page.click('#kbtab-files');
    };
    await toFiles().catch(() => {});     // 读不到状态的测试照常往下走
    for (const k of ['reload', 'goto']) {
      const orig = page[k].bind(page);
      page[k] = async (...a) => { const r = await orig(...a); await toFiles().catch(() => {}); return r; };
    }
  }
  if (opts.tab === 'browse') await page.locator('#findStatus:not(:empty)').waitFor({timeout: 3000}).catch(() => {});
  t.after(async () => {
    await context.close();
    proc.kill();
    fs.rmSync(home, {recursive: true, force: true});
  });
  return {page, home, docs, url, proc, context, env, args, tool};
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

test('窄屏开始对话后收起知识库卡片，点标题展开，自动收起不记住', async t => {
  const {page} = await launch(t, {width: 800});
  await pickByName(page, '季度总结');
  await ask(page);
  assert.ok(await page.isHidden('#findBody'));
  assert.strictEqual(await page.getAttribute('#foldFind', 'aria-expanded'), 'false');
  assert.match(await page.textContent('#findSummary'), /还是空的|个文件/);     // 收起时写知识库的一行数字
  await page.click('#foldFind');
  assert.ok(await page.isVisible('#findBody'));
  assert.ok(await page.isHidden('#findSummary'));
  assert.strictEqual(await page.evaluate(() => JSON.parse(localStorage.getItem('ask-local-files.fold')).find), false);
});

test('宽屏开始对话后找文件不收起', async t => {
  const {page} = await launch(t, {width: 1280});
  await pickByName(page, '季度总结');
  await ask(page);
  assert.ok(await page.isVisible('#findBody'));
});

test('模块点标题收起，标题旁显示摘要，同栏另一个模块占满，刷新后保持', async t => {
  const {page} = await launch(t, {width: 1280});
  await pickByName(page, '季度总结');
  await ask(page);
  const before = await box(page, '#findCard');
  await page.click('#foldPicked');
  assert.ok(await page.isHidden('#picked'));
  assert.strictEqual(await page.getAttribute('#foldPicked', 'aria-expanded'), 'false');
  assert.match(await page.textContent('#pickCount'), /1 \/ 50/);
  const after = await box(page, '#findCard');
  assert.ok(after.height > before.height + 50, '已选收起后找文件应变高');
  await page.click('#foldAsk');
  assert.ok(await page.isHidden('#ask'));
  assert.ok(await page.isVisible('#save'), '标题行的按钮不随收起隐藏');
  assert.match(await page.textContent('#askSummary'), /，1 轮问答$/);
  const wide = await box(page, '#findCard');
  assert.ok(wide.width > 1100, '右栏全部收起后找文件应占满宽度，宽 ' + wide.width);
  await page.reload();
  await page.locator('#foldPicked[aria-expanded="false"]').waitFor();
  assert.strictEqual(await page.getAttribute('#foldAsk', 'aria-expanded'), 'false');
  await page.focus('#foldAsk');
  await page.keyboard.press('Enter');
  assert.ok(await page.isVisible('#ask'));
  assert.strictEqual(await page.locator('#foldAsk .arrow').textContent(), '▾');
});

test('左栏全部收起时问 AI 占满宽度，360px 下收起也没有横向滚动', async t => {
  const {page} = await launch(t, {width: 1280});
  await page.click('#foldFind');
  await page.click('#foldPicked');
  const a = await box(page, '#askCard');
  assert.ok(a.width > 1100, '问 AI 宽 ' + a.width);
  assert.ok(await page.isVisible('#findSummary'));
  await page.setViewportSize({width: 360, height: 800});
  await noHorizontalScroll(page);
});

test('没有可用模型时窄屏顺序为找文件、问 AI、已选', async t => {
  const {page} = await launch(t, {model: null, width: 360});
  await page.locator('#noAiNote').waitFor();
  const f = await box(page, '#findCard'), a = await box(page, '#askCard'), p = await box(page, '#pickedCard');
  assert.ok(f.y < a.y && a.y < p.y, '顺序不对');
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
  await li.getByRole('button', {name: '进入文件夹 资料/子目录'}).waitFor();
  assert.doesNotMatch(meta, /会议记录/);
  await pickByName(page, '会议记录');
  const pl = page.locator('#picked li', {hasText: '会议记录'});
  const pb = await pl.getByRole('button', {name: /移除/}).boundingBox(), pn = await pl.locator('.name').boundingBox();
  assert.ok(pb.x > pn.x);
});

test('设置按用处就近放', async t => {
  const {page} = await launch(t);
  assert.strictEqual(await page.$('#settings'), null);
  assert.ok(await page.isHidden('#folderDlg'));
  await openFolders(page);
  assert.ok(await page.isVisible('#folder'));
  await closeFolders(page);
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

// 管理文件夹是模态大窗口：打开后页面其他部分不能点，操作完点「完成」关闭
async function openFolders(page) {
  await page.click('#manageFolders');
  await page.locator('#folderDlg[open]').waitFor();
}
async function closeFolders(page) {
  await page.click('#folderDone');
  await page.locator('#folderDlg[open]').waitFor({state: 'detached'});
}

// 改写某个接口的返回：fn 收到原返回的 JSON，返回改过的
async function patchApi(page, pathPart, fn) {
  await page.route(u => u.pathname === pathPart, async route => {
    const res = await route.fetch();
    await route.fulfill({response: res, json: fn(await res.json())});
  });
}

test('首次启动：停在「概览」提示先添加文件夹，点「添加文件夹」展开文件夹管理并聚焦路径输入框', async t => {
  const {page} = await launch(t, {folders: false, width: 800, kbTab: false});
  await page.waitForFunction(() => document.querySelector('#findSummary').textContent !== '');
  assert.strictEqual(await page.getAttribute('#kbtab-overview', 'aria-selected'), 'true');
  assert.match(await page.textContent('#kbEmptyText'), /先在「文件」标签添加文件夹/);
  await page.click('#kbEmptyGo');
  await page.waitForFunction(() => document.activeElement && document.activeElement.id === 'folder');
  assert.strictEqual(await page.getAttribute('#kbtab-files', 'aria-selected'), 'true');
  assert.ok(await page.isVisible('#folderDlg'));
  assert.ok(await inViewport(page, '#folder'));
});

test('缺少 pdftotext 的提醒常驻，发送后仍在', async t => {
  const {page} = await launch(t, {noPdf: true});
  await page.locator('#banner').getByText(/pdftotext/).waitFor();
  await pickByName(page, '季度总结');
  await ask(page);
  assert.ok(await page.isVisible('#banner'));
});

test('没有可用模型时仍为两栏，「问 AI」收成一行在找文件下方，已选在右栏', async t => {
  const {page} = await launch(t, {model: null, width: 1600});
  await page.locator('#noAiNote').waitFor();
  assert.ok(await page.evaluate(() => document.body.classList.contains('no-ai')));
  assert.ok(await page.isHidden('#modelPanel'));
  for (const sel of ['#ask', '#send', '#chatWrap', '#skillRow', '#model', '#save']) assert.ok(await page.isHidden(sel), sel);
  assert.ok(await page.isVisible('#modelSettings'));
  assert.strictEqual(await page.textContent('#askSummary'), '没有可用的模型');
  const find = await box(page, '#findCard'), ask = await box(page, '#askCard'), pick = await box(page, '#pickedCard');
  assert.ok(find.width < 700, '找文件宽度 ' + find.width);
  assert.ok(Math.abs(ask.x - find.x) < 2 && ask.y >= find.y + find.height - 1, '问 AI 应在找文件下方');
  assert.ok(pick.x > find.x + find.width && pick.y < find.y + 5, '已选应在右栏');
  assert.ok(pick.height >= ask.y + ask.height - find.y - 2, '已选应占满右栏，高 ' + pick.height);
  await page.click('#modelSettings');
  await page.waitForSelector('#modelPanel', {state: 'visible'});
  await page.fill('#cBase', modelBase);
  await page.fill('#cModel', 'ok');
  await page.fill('#cKey', 'k');
  await page.click('#saveCustom');
  await page.waitForFunction(() => !document.body.classList.contains('no-ai'));
  assert.ok(await page.isHidden('#noAiNote'));
  assert.ok(await page.isVisible('#ask'));
  assert.ok(await page.$eval('#pickedCard', e => !!e.closest('.col-left')), '有模型后已选回到左栏');
  assert.ok(await page.$eval('#askCard', e => !!e.closest('.col-right')), '有模型后问 AI 回到右栏');
  assert.ok(await isFocused(page, '#saveCustom'), '换栏后焦点仍在保存按钮');
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
  assert.match(text, /^用使用者提问的语言回答/);
  assert.ok(text.includes('<document index="1">\n<source>资料/季度总结.txt</source>'));
  assert.ok(text.includes('<document index="2">\n<source>资料/子目录/会议记录.md</source>'));
  assert.match(text, /先给结论，再分点。$/);
  assert.ok(!text.includes(docs), '路径从所添加文件夹的名字起写');
  assert.ok(text.includes('甲'.repeat(100)));
});

test('「保存全部文字」存成 .md', async t => {
  const {page} = await launch(t);
  await pickByName(page, '季度总结');
  const [dl] = await Promise.all([page.waitForEvent('download'), page.click('#saveTexts')]);
  const body = fs.readFileSync(await dl.path(), 'utf8');
  assert.ok(body.includes('<document index="1">') && body.includes('甲'.repeat(100)));
  await page.locator('#pickStatus').getByText(/^已保存为 资料管家-文字-\d{8}-\d{4}\.md（在浏览器的下载文件夹里）：1 个文件/).waitFor();
});

test('路径输入补全与常用位置', async t => {
  const {page} = await launch(t, {folders: false, homeDirs: ['文档']});
  await openFolders(page);
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

test('取消勾选自动收录：有排队时确认框写明数量，点取消复原勾选；确定后写明撤出几个', async t => {
  const {page} = await launch(t);
  await page.click('#manageFolders');
  const cb = page.locator('#folders li input[type=checkbox]').first();
  if (!await cb.isChecked()) {
    await cb.check();
    await page.waitForFunction(() => { const c = document.querySelector('#folders li input[type=checkbox]'); return c.checked && !c.disabled; });
  }
  await page.route(u => u.pathname === '/api/folders', async route => {
    const body = route.request().postDataJSON();
    if (body.count) { await route.fulfill({json: {queued: 4}}); return; }
    const res = await route.fetch();
    await route.fulfill({response: res, json: Object.assign(await res.json(), {parked: 4})});
  });
  let msg = '';
  page.onDialog = d => { msg = d.message(); d.dismiss(); };
  await cb.click();
  await page.waitForFunction(() => { const c = document.querySelector('#folders li input[type=checkbox]'); return c.checked && !c.disabled; });
  assert.match(msg, /知识库里有 4 个文件待抽结论，会撤出排队，已收录的保留。/);
  page.onDialog = d => d.accept();
  await cb.click();
  await page.locator('#folderStatus').getByText(/排队中的 4 个文件已撤出/).waitFor();
  assert.ok(!await cb.isChecked());
});

test('概览统计写明不在自动收录范围内的文件数', async t => {
  const base = {files: 3, facts: 5, topics: 1, queued: 0, extract: 0, reextract: 0, bytes: 1000, max_bytes: 1 << 30};
  let outside = 2;
  const {page} = await launch(t);
  await patchApi(page, '/api/memory', r => Object.assign(r, {stats: Object.assign({outside}, base)}));
  await page.click('#kbtab-overview');
  await page.evaluate(() => refreshKb());
  await page.locator('#memStats').getByText('已收录 3 个文件（其中 2 个不在自动收录范围内：不在勾选的文件夹里，或格式不再自动收录，不再更新）、5 条结论、1 个主题。', {exact: true}).waitFor();
  outside = 0;
  await page.evaluate(() => refreshKb());
  await page.locator('#memStats').getByText('已收录 3 个文件、5 条结论、1 个主题。', {exact: true}).waitFor();
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

test('自定义接口没填齐时写明暂不可用，填齐地址和模型名后自动选中，没有 AK 时标未鉴权', async t => {
  const {page} = await launch(t, {model: null});
  await page.click('#modelSettings');
  await page.fill('#cBase', 'http://model.example.com/v1');
  await page.click('#saveCustom');
  await page.locator('#settingsStatus').getByText(/暂不可用/).waitFor();
  await page.fill('#cModel', 'ok');
  await page.click('#saveCustom');
  await page.waitForFunction(() => document.querySelector('#model').value === 'custom');
  assert.match(await page.textContent('#dest'), /未鉴权：model\.example\.com/);
  await page.fill('#cKey', 'k');
  await page.click('#saveCustom');
  await page.waitForFunction(() => !document.querySelector('#dest').textContent.includes('未鉴权'));
});

test('本机地址的自定义接口不标未鉴权', async t => {
  const {page} = await launch(t, {model: null});
  await page.click('#modelSettings');
  await page.fill('#cBase', modelBase);
  await page.fill('#cModel', 'ok');
  await page.click('#saveCustom');
  await page.waitForFunction(() => document.querySelector('#model').value === 'custom');
  assert.doesNotMatch(await page.textContent('#dest'), /未鉴权/);
});

test('没有模型时也能点「重新查找」，找不到本机模型服务时写明端口', async t => {
  const {page} = await launch(t, {model: null});
  assert.ok(await page.isVisible('#findLocal'));
  await page.click('#findLocal');
  await page.locator('#localStatus').getByText(/llama-server（端口 8080）或 Ollama（端口 11434）/).waitFor();
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
  // 等文字取完再按：取完时会重写一次状态行
  await page.locator('#picked li', {hasText: '季度总结'}).getByText(/\d 字/).waitFor();
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
  await page.fill('#q', '资料');
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
  await page.fill('#q', '');
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

test('复制网址：复制固定网址并提示', async t => {
  const {page, url} = await launch(t);
  await page.locator('#ver').getByText(/v\d/).waitFor();
  await page.click('#copyUrl');
  await page.locator('#urlStatus.ok').waitFor();
  assert.strictEqual(await page.evaluate(() => navigator.clipboard.readText()), url);
  assert.match(await page.textContent('#urlStatus'), /^已复制网址：/);
});

test('关掉全部页面几秒后服务退出；刷新页面不退出；再次启动只打印网址不另起一份', async t => {
  const {page, url, proc, context, env, args} = await launch(t);
  await page.locator('#ver').getByText(/v\d/).waitFor();
  await page.reload();
  await page.locator('#ver').getByText(/v\d/).waitFor();
  await new Promise(r => setTimeout(r, 7000));
  assert.strictEqual(proc.exitCode, null, '刷新后服务仍在运行');
  const second = spawn(PYTHON, args, {env, stdio: ['ignore', 'pipe', 'ignore']});
  assert.strictEqual(await firstLine(second, /网页界面：(\S+)/), url);
  const code2 = await new Promise(r => (second.exitCode !== null ? r(second.exitCode) : second.on('exit', r)));
  assert.strictEqual(code2, 0);
  await page.close();
  const code = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('关页后未在时限内退出')), 20000);
    proc.on('exit', c => { clearTimeout(timer); resolve(c); });
  });
  assert.strictEqual(code, 0);
  void context;
});

test('从没有页面连上时按 --idle-timeout 退出', async t => {
  const {page, proc} = await launch(t, {args: ['--idle-timeout', '3'], noPage: true});
  void page;
  const code = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('未在时限内退出')), 15000);
    proc.on('exit', c => { clearTimeout(timer); resolve(c); });
  });
  assert.strictEqual(code, 0);
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
  assert.match(await page.textContent('#ver'), /资料管家 v\d+\.\d+/);
});

test('页头副标题：宽屏在标题旁，360px 换到标题下方，浅色深色都用次要文字色', async t => {
  for (const width of [1280, 360]) {
    const {page} = await launch(t, {width});
    assert.strictEqual(await page.title(), '资料管家', '网页标题不带副标题');
    for (const colorScheme of ['light', 'dark']) {
      await page.emulateMedia({colorScheme});
      const r = await page.evaluate(() => {
        const h = document.querySelector('header h1'), s = document.getElementById('tagline');
        const muted = getComputedStyle(document.documentElement).getPropertyValue('--muted').trim();
        const probe = document.createElement('span');
        probe.style.color = muted; document.body.appendChild(probe);
        const mutedRgb = getComputedStyle(probe).color; probe.remove();
        const hb = h.getBoundingClientRect(), sb = s.getBoundingClientRect();
        const btns = ['help', 'newConv', 'quit'].map(id => document.getElementById(id).getBoundingClientRect());
        return {text: s.textContent, color: getComputedStyle(s).color, mutedRgb,
          hSize: parseFloat(getComputedStyle(h).fontSize), sSize: parseFloat(getComputedStyle(s).fontSize),
          below: sb.top >= hb.bottom - 1, beside: Math.abs((sb.top + sb.bottom) / 2 - (hb.top + hb.bottom) / 2) < hb.height / 2,
          btnRow: btns.every(b => Math.abs(b.top - btns[0].top) < 2 && b.width > 0), btnRight: btns[2].right <= window.innerWidth};
      });
      assert.strictEqual(r.text, '把本机文件收录到知识库，用 AI 查找与问答');
      assert.strictEqual(r.color, r.mutedRgb, `${width}px ${colorScheme}：副标题用 --muted`);
      assert.ok(r.sSize < r.hSize, '副标题字号小于标题');
      if (width === 360) assert.ok(r.below, '360px 时在标题下方'); else assert.ok(r.beside, '宽屏时在标题旁');
      assert.ok(r.btnRow && r.btnRight, `${width}px：帮助、新对话、退出仍在一行且不出界`);
      await noHorizontalScroll(page);
    }
  }
});

test('360px 下各面板展开、有对话与预览时都没有横向滚动', async t => {
  const {page} = await launch(t, {width: 360, model: 'long', files: {'很长很长很长很长很长很长很长很长很长很长的文件名用于测试窄屏显示.txt': '己'.repeat(300)}});
  await openFolders(page);
  await noHorizontalScroll(page);
  await closeFolders(page);
  await page.click('#modelSettings');
  await pickByName(page, '很长很长');
  await page.locator('#picked li').getByRole('button', {name: /预览/}).click();
  await page.locator('#picked .preview').waitFor();
  await ask(page);
  await noHorizontalScroll(page);
  await page.click('#foldFind');
  await noHorizontalScroll(page);
});

test('保存位置：默认在工具目录下的「数据」，不提示；可改到空文件夹并把数据移过去', async t => {
  const {page, home, tool} = await launch(t, {ownTool: true});
  await page.locator('#findStatus:not(:empty)').waitFor();
  assert.doesNotMatch(await page.textContent('#askStatus'), /保存位置/);
  const own = path.join(tool, '数据');
  assert.ok(fs.existsSync(path.join(own, 'token')));
  assert.strictEqual(fs.readFileSync(path.join(tool, 'data-dir.txt'), 'utf8').trim(), own);
  await page.click('#memSettings');
  assert.match(await page.textContent('#dataDirNow'), /^当前：默认（安装目录下的「数据」）：/);
  const dest = path.join(home, '数据盘');
  fs.mkdirSync(dest);
  let msg = '';
  page.onDialog = d => { msg = d.message(); d.accept(); };
  await page.fill('#dataDir', dest);
  await page.click('#setDataDir');
  await page.locator('#dataStatus.ok', {hasText: '已把数据移到'}).waitFor();
  assert.match(msg, /将移动 \d 个文件：/);
  assert.strictEqual(await page.textContent('#dataDirNow'), '当前：' + fs.realpathSync(dest));
  assert.ok(fs.existsSync(path.join(dest, 'token')));
  assert.ok(!fs.existsSync(path.join(own, 'token')));
  assert.strictEqual(fs.readFileSync(path.join(tool, 'data-dir.txt'), 'utf8').trim(), fs.realpathSync(dest));
});

test('保存位置：家目录里的旧数据第一次启动时搬进工具目录的「数据」', async t => {
  const {page, tool} = await launch(t, {ownTool: true, beforeStart: home => {
    const d = path.join(home, '.config', 'ask-local-files');
    fs.mkdirSync(d, {recursive: true});
    fs.writeFileSync(path.join(d, 'token'), 'old-token-old-token-12\n');
  }});
  assert.match(page.url(), /t=old-token-old-token-12/);
  assert.ok(fs.existsSync(path.join(tool, '数据', 'token')));
});

test('保存位置：取消确认不改动；不存在的文件夹写明原因', async t => {
  const {page, home} = await launch(t, {ownTool: true});
  await page.click('#memSettings');
  await page.fill('#dataDir', path.join(home, '没有这个文件夹'));
  await page.click('#setDataDir');
  await page.locator('#dataStatus.bad', {hasText: '更改失败：文件夹不存在'}).waitFor();
  const dest = path.join(home, 'empty');
  fs.mkdirSync(dest);
  page.onDialog = d => d.dismiss();
  await page.fill('#dataDir', dest);
  await page.click('#setDataDir');
  await page.locator('#dataStatus', {hasText: '没有更改保存位置'}).waitFor();
  assert.deepStrictEqual(fs.readdirSync(dest), []);
});

test('保存位置：新位置已有数据时改用那一份，网址变了按新网址重新打开', async t => {
  const {page, home, docs, tool} = await launch(t, {ownTool: true});
  const dest = path.join(home, '旧数据');
  fs.mkdirSync(dest);
  fs.writeFileSync(path.join(dest, 'config.json'), JSON.stringify({folders: [path.join(docs, '子目录')], ignore_md: false}));
  fs.writeFileSync(path.join(dest, 'token'), 'other-token-other-token\n');
  let msg = '';
  page.onDialog = d => { msg = d.message(); d.accept(); };
  await page.click('#memSettings');
  await page.fill('#dataDir', dest);
  await page.click('#setDataDir');
  await page.waitForURL(/t=other-token-other-token/);
  assert.match(msg, /改用新位置已有的数据/);
  assert.match(msg, /不合并/);
  assert.match(msg, /网址会随之改变/);
  assert.ok(fs.existsSync(path.join(tool, '数据', 'config.json')));     // 原处保留
});

test('保存位置不可用：醒目提示，知识库关闭且不能打开', async t => {
  const {page, home} = await launch(t, {ownTool: true, pointer: '/nonexistent-mount/ask'});
  await page.locator('#dataBanner', {hasText: '保存位置不可用：/nonexistent-mount/ask'}).waitFor();
  await page.locator('#dataBanner').getByRole('button', {name: '打开知识库设置里的「保存位置」'}).click();
  await page.locator('#kbSetPanel #dataDirNow').waitFor();
  assert.match(await page.textContent('#dataDirNow'), /不可用/);
  assert.ok(!await page.isChecked('#memoryOn'));
  await page.click('#memoryOn');       // 服务端拒绝后立即改回不勾，不用 check（它要求点完仍是勾选）
  await page.locator('#memStatus.bad', {hasText: '保存位置不可用'}).waitFor();
  assert.ok(!await page.isChecked('#memoryOn'));
  assert.ok(!fs.existsSync(path.join(home, '.local', 'share', 'ask-local-files')));
});

test('360px 下知识库设置与保存位置没有横向滚动', async t => {
  const {page} = await launch(t, {width: 360});
  await page.click('#memSettings');
  await page.locator('#dataDirNow:not(:empty)').waitFor();
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
  // 只有 system 与最新一问：清空前的问答不再发送
  assert.strictEqual(sent.messages.length, 2);
  assert.match(sent.messages[1].content, /使用者的问题：\n第二个问题$/);
  assert.ok(!JSON.stringify(sent).includes('第一个问题'));
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

// ---------------- 知识库

const PLAN = {'项目计划.txt': '项目计划\n\n第一阶段在三月底完成验收。\n\n预算另行说明。'};

test('首次打开提示知识库已默认打开，只提示一次；显示引导时不另提示', async t => {
  const {page: g} = await launch(t);
  await g.locator('#guide').waitFor();
  assert.doesNotMatch(await g.locator('#askStatus').textContent(), /知识库已默认打开/);
  const {page, url} = await launch(t, {conf: {guide_skip: true}});
  await page.locator('#askStatus', {hasText: '知识库已默认打开'}).waitFor();
  await page.waitForTimeout(300);
  await page.goto(url);
  await page.waitForTimeout(500);
  assert.doesNotMatch(await page.locator('#askStatus').textContent(), /知识库已默认打开/);
});

test('收录核对通过的结论，不选文件也能参考知识库提问，回答上方写明用了哪些知识库', async t => {
  const {page, docs} = await launch(t, {model: 'memo', files: PLAN});
  await pickByName(page, '项目计划');
  await ask(page, '第一阶段什么时候验收');
  await page.locator('#chat .foot', {hasText: '记下 1 条结论，下次提问会参考'}).waitFor();
  await page.locator('#picked li').getByRole('button', {name: /移除/}).click();
  await page.fill('#ask', '第一阶段验收');
  await page.click('#send');
  // 改动文件清单即开始新对话，这一轮是新对话的第一条回答
  const memo = page.locator('#chat .msg.assistant').first().locator('details.memo');
  await memo.waitFor();
  assert.match(await memo.locator('summary').textContent(), /参考了知识库：1 条结论、1 段原文/);
  await memo.locator('summary').click();
  assert.ok((await memo.textContent()).includes('文件：' + path.join(docs, '项目计划.txt') + '（文件 1，1 段原文）'));
  assert.match(await memo.textContent(), /结论：第一阶段三月底验收/);
  await page.waitForFunction(() => !document.querySelector('#send').disabled);
  const [dl] = await Promise.all([page.waitForEvent('download'), page.click('#save')]);
  const md = fs.readFileSync(await dl.path(), 'utf-8');
  assert.match(md, /参考了知识库：1 条结论、1 段原文/);
  assert.match(md, /- 文件 1：资料\/项目计划\.txt（1 段原文）/, '保存时知识库里的路径从所添加文件夹的名字起写');
  assert.ok(!md.includes(docs), '不写完整路径');
  assert.doesNotMatch(md, /## 参考文件/, '没选文件时不写参考文件');
});

test('取消「这次提问参考知识库」且没选文件时不发送', async t => {
  const {page} = await launch(t);
  await page.uncheck('#useMemory');
  await page.fill('#ask', '随便问问');
  await page.click('#send');
  await page.locator('#askStatus', {hasText: '还没有选文件'}).waitFor();
});

test('「这条不对」直接移出这条回答记下的结论，不弹窗', async t => {
  const {page} = await launch(t, {model: 'memo', files: PLAN});
  let dialogs = 0;
  page.onDialog = d => { dialogs++; d.accept(); };
  await pickByName(page, '项目计划');
  await ask(page, '第一阶段什么时候验收');
  assert.strictEqual(await page.getByRole('button', {name: '这条回答不对：移出它记下的结论'}).count(), 2, '回答顶部与底部各一个');
  await page.locator('#chat .foot').getByRole('button', {name: '这条回答不对：移出它记下的结论'}).click();
  await page.locator('#chatStatus', {hasText: '已移出这条回答记下的 1 条结论'}).waitFor();
  await page.locator('#chat .foot', {hasText: '已移出这条回答记下的结论'}).waitFor();
  assert.strictEqual(await page.getByRole('button', {name: '这条回答不对：移出它记下的结论'}).count(), 0);
  assert.strictEqual(dialogs, 0);
});

test('主题与结论：搜索、「这条不对」、从文件标签移出文件、页面内确认后清空，关掉知识库后隐藏「这次提问参考知识库」；360px 不溢出', async t => {
  const {page} = await launch(t, {model: 'memo', files: PLAN, width: 360});
  await pickByName(page, '项目计划');
  await ask(page, '第一阶段什么时候验收');
  await page.click('#foldFind');       // 窄屏开始对话后知识库卡片自动收起
  await page.click('#kbtab-overview');
  await page.locator('#memStats', {hasText: '已收录 1 个文件、1 条结论'}).waitFor();
  await page.click('#kbtab-topics');
  await page.locator('#memFacts li', {hasText: '第一阶段三月底验收'}).waitFor();
  await noHorizontalScroll(page);
  await page.fill('#memQ', '没有这句');
  await page.locator('#kbNone', {hasText: '没有找到包含「没有这句」的主题或结论'}).waitFor();
  await page.click('#memQClear');
  assert.strictEqual(await page.evaluate(() => document.activeElement.id), 'memQ');
  await page.getByRole('button', {name: '这条不对：移出这条结论'}).click();
  await page.locator('#kbListStatus', {hasText: '已移出这条结论，之后不再收录'}).waitFor();
  await page.locator('#memFacts li', {hasText: '还没有结论'}).waitFor();
  // 「文件」标签里的收录标记：点了只列这份文件的结论，可移出这份文件
  await page.click('#kbtab-files');
  await page.fill('#q', '项目计划');
  await page.locator('#results button.tag.in', {hasText: '已收录 · 0 条结论'}).click();
  await page.locator('#kbFocusText', {hasText: '只显示这份文件的 0 条结论'}).waitFor();
  await page.click('#kbForgetFile');
  await page.locator('#kbListStatus', {hasText: '已从知识库移出这份文件'}).waitFor();
  await page.locator('#findSummary', {hasText: '还是空的'}).waitFor({state: 'attached'});
  // 清空知识库：页面内确认框写明数量，取消不清，确认后清空
  await page.click('#memSettings');
  await page.click('#memClear');
  await page.locator('#clearDlg[open]').waitFor();
  assert.match(await page.textContent('#clearText'), /^将删除：已收录的 0 个文件的文字（约 0 B）、待整理 0 项、0 条结论、0 条要点、0 个主题，以及整理记录和文件索引，不能撤销。设置、网址和原文件不受影响；/);
  await noHorizontalScroll(page);
  await page.click('#clearNo');
  assert.strictEqual(await page.locator('#clearDlg[open]').count(), 0);
  assert.strictEqual(await page.evaluate(() => document.activeElement.id), 'memClear');
  await page.click('#memClear');
  await page.click('#clearOk');
  await page.locator('#memStatus', {hasText: '知识库已清空'}).waitFor();
  await page.uncheck('#memoryOn');
  await page.locator('#memStatus', {hasText: '已关闭知识库'}).waitFor();
  assert.ok(await page.locator('#useMemRow').isHidden());
  assert.ok(await page.locator('#memSettings').isVisible());      // 关掉知识库后仍能打开知识库设置
  assert.ok(await page.locator('#offNote').isVisible());
});

test('知识库设置在卡片头的齿轮里：默认收起，齿轮打开收起、Esc 收起并还焦点，首次提示里的「知识库设置」打开它；五项加每批大小与保存位置；360px 不溢出', async t => {
  const {page} = await launch(t, {model: 'memo', files: PLAN, width: 360, conf: {guide_skip: true}});
  assert.ok(await page.locator('#kbSetPanel').isHidden());
  assert.strictEqual(await page.getAttribute('#memSettings', 'aria-label'), '知识库设置');
  assert.strictEqual(await page.locator('#setPanel #memoryOn').count(), 0);     // 设置面板里不再放知识库
  assert.strictEqual(await page.locator('#tab-mem').count(), 0);
  await page.locator('#askStatus').getByRole('button', {name: '打开知识库设置'}).click();
  assert.ok(await page.locator('#kbSetPanel').isVisible());
  assert.strictEqual(await page.evaluate(() => document.activeElement.id), 'memoryOn');
  assert.strictEqual(await page.getAttribute('#memSettings', 'aria-expanded'), 'true');
  assert.deepStrictEqual(await page.$$eval('#kbSetPanel input, #kbSetPanel button', els => els.map(e => e.id)),
    ['memoryOn', 'tocPick', 'memAuto', 'xbChars', 'xbFiles', 'saveXb', 'memMb', 'saveMemMb', 'dataRestore', 'dataToTool', 'dataDir', 'pickDataDir', 'setDataDir', 'memClear']);
  await noHorizontalScroll(page);
  await page.keyboard.press('Escape');
  assert.ok(await page.locator('#kbSetPanel').isHidden());
  assert.strictEqual(await page.evaluate(() => document.activeElement.id), 'memSettings');
  await page.keyboard.press('Enter');
  assert.ok(await page.locator('#kbSetPanel').isVisible());
  await page.click('#memSettings');
  assert.ok(await page.locator('#kbSetPanel').isHidden());
  assert.strictEqual(await page.getAttribute('#memSettings', 'aria-expanded'), 'false');
});

test('知识库卡片：三个标签用方向键与 Home、End 切换，只有选中的可 Tab 到；空库时停在「概览」，有内容后记住上次的标签', async t => {
  // 已提示过界面变化（不算升级后首次打开）
  const {page, url} = await launch(t, {model: 'memo', files: PLAN, kbTab: false, conf: {layout_notice: true}});
  await page.waitForFunction(() => document.querySelector('#findSummary').textContent !== '');
  assert.strictEqual(await page.getAttribute('#kbtab-overview', 'aria-selected'), 'true');
  assert.deepStrictEqual(await page.$$eval('#kbTabs [role=tab]', els => els.map(e => e.textContent)), ['文件', '概览', '主题与结论']);
  await page.focus('#kbtab-overview');
  const keys = [['ArrowRight', 'kbtab-topics'], ['ArrowRight', 'kbtab-files'], ['ArrowLeft', 'kbtab-topics'], ['Home', 'kbtab-files'], ['End', 'kbtab-topics']];
  for (const [k, id] of keys) {
    await page.keyboard.press(k);
    assert.strictEqual(await page.evaluate(() => document.activeElement.id), id, k);
    assert.strictEqual(await page.getAttribute('#' + id, 'aria-selected'), 'true');
    assert.ok(await page.locator('#' + await page.getAttribute('#' + id, 'aria-controls')).isVisible());
  }
  assert.strictEqual(await page.locator('#kbTabs [role=tab][tabindex="0"]').count(), 1);
  await page.click('#kbtab-files');
  await pickByName(page, '项目计划');
  await ask(page, '第一阶段什么时候验收');
  await page.click('#kbtab-topics');
  await page.goto(url);
  await page.waitForFunction(() => document.querySelector('#findSummary').textContent !== '');
  assert.strictEqual(await page.getAttribute('#kbtab-topics', 'aria-selected'), 'true', '有内容后回到上次的标签');
});

test('回答上方的引用可跳到知识库：目标高亮并获得焦点，定位栏读出，「返回回答」回到引用；「记下 N 条结论」只列这几条', async t => {
  const {page} = await launch(t, {model: 'memo', files: PLAN});
  await pickByName(page, '项目计划');
  await ask(page, '第一阶段什么时候验收');
  await page.locator('#chat .foot').getByRole('button', {name: /记下 1 条结论/}).click();
  await page.locator('#kbFocusText', {hasText: '只显示定位到的 1 条结论'}).waitFor();
  await page.waitForFunction(() => document.activeElement.matches('#memFacts li.hl'));
  await page.getByRole('button', {name: '返回回答里的引用'}).click();
  assert.match(await page.evaluate(() => document.activeElement.textContent), /记下 1 条结论/);
  await page.click('#kbShowAll');
  await page.locator('#picked li').getByRole('button', {name: /移除/}).click();
  await ask(page, '第一阶段验收');
  await page.locator('#chat details.memo summary').click();
  await page.locator('#chat details.memo').getByRole('button', {name: /在知识库中定位结论：第一阶段三月底验收/}).click();
  await page.locator('#kbJumpText', {hasText: '已在知识库中定位到：第一阶段三月底验收'}).waitFor();
  assert.strictEqual(await page.getAttribute('#kbJumpText', 'aria-live'), 'polite');
  assert.ok(await page.locator('#kbJumpBar').isVisible(), '定位结果写在知识库卡片里');
  await page.waitForFunction(() => document.activeElement.matches('#memFacts li.hl[tabindex="-1"]'));
  await page.getByRole('button', {name: '返回回答里的引用'}).click();
  assert.match(await page.evaluate(() => document.activeElement.getAttribute('aria-label')), /在知识库中定位结论/);
  assert.ok(await page.locator('#kbJumpBar').isHidden(), '返回后定位栏收起');
  // 文件引用：切到「文件」标签，进入所在文件夹并定位到这一行
  await page.locator('#chat details.memo').getByRole('button', {name: /在知识库中定位文件：/}).click();
  await page.waitForFunction(() => document.activeElement.matches('#browseList li.hl'));
  assert.strictEqual(await page.getAttribute('#kbtab-files', 'aria-selected'), 'true');
});

test('找文件的「选项」：排序与三个勾选框默认收起，改了的项数写在按钮上，展开与否记在本浏览器', async t => {
  const {page} = await launch(t, {optsOpen: false, mdDefault: true});
  assert.ok(await page.locator('#findOpts').isHidden());
  assert.strictEqual(await page.getAttribute('#findOptsBtn', 'aria-expanded'), 'false');
  assert.strictEqual(await page.textContent('#findOptsN'), '');
  await page.click('#findOptsBtn');
  for (const id of ['sortBy', 'dedup', 'regex', 'ignoreMd']) assert.ok(await page.locator('#findOpts #' + id).isVisible(), id);
  await page.check('#regex');
  await page.selectOption('#sortBy', 'name_asc');
  assert.strictEqual(await page.textContent('#findOptsN'), '（2 项已改）');
  await page.click('#findOptsBtn');
  assert.ok(await page.locator('#findOpts').isHidden());
  assert.match(await page.textContent('#findOptsBtn'), /选项（2 项已改）/, '收起时也看得到改过几项');
  await page.click('#findOptsBtn');
  await page.reload();
  assert.ok(await page.locator('#findOpts').isVisible(), '展开记在本浏览器');
  assert.strictEqual(await page.textContent('#findOptsN'), '（2 项已改）');
});

test('知识库设置的齿轮带文字「设置」；没选 skill 时不常驻自建 skill 的路径；发送摘要不写接口地址', async t => {
  const {page} = await launch(t);
  assert.strictEqual((await page.textContent('#memSettings')).trim(), '设置');
  assert.strictEqual(await page.getAttribute('#memSettings', 'aria-label'), '知识库设置');
  assert.strictEqual(await page.textContent('#skillHint'), '');
  assert.match(await page.textContent('#dest'), /^这次提问：参考知识库 · 没选文件 · 发送到 /);
  assert.doesNotMatch(await page.textContent('#dest'), /127\.0\.0\.1/);
  assert.match(await page.getAttribute('#dest', 'title'), /^接口：127\.0\.0\.1:\d+/);
});

test('Ctrl+L 与「跳到提问框」聚焦提问框；跳转链接平时不占位置', async t => {
  const {page} = await launch(t);
  await page.focus('#q');
  await page.keyboard.press('Control+l');
  assert.strictEqual(await page.evaluate(() => document.activeElement.id), 'ask');
  assert.ok(await page.evaluate(() => document.getElementById('skipAsk').getBoundingClientRect().bottom <= 0));
  await page.focus('#skipAsk');
  assert.ok(await page.evaluate(() => document.getElementById('skipAsk').getBoundingClientRect().top >= 0));
  await page.keyboard.press('Enter');
  assert.strictEqual(await page.evaluate(() => document.activeElement.id), 'ask');
});

test('整理知识库：「现在整理」整理出主题，整理记录可撤销，「空闲时自动整理」记住；360px 不溢出', async t => {
  const {page, url} = await launch(t, {model: 'memo', files: PLAN, width: 360});
  await pickByName(page, '项目计划');
  await ask(page, '第一阶段什么时候验收');
  await page.click('#foldFind');
  await page.click('#kbtab-overview');
  await page.locator('#kbTidyText', {hasText: '整理：1 项等空闲时进行'}).waitFor();
  await page.click('#memUpdate');
  await page.locator('#memUpdStatus', {hasText: '知识库已整理：新建主题 1 个。'}).waitFor();
  assert.ok(await page.locator('#memUpdStop').isHidden());
  await page.locator('#kbRecentList li', {hasText: '验收安排'}).waitFor();
  await page.click('#kbtab-topics');
  await page.locator('#memTopics li', {hasText: '验收安排'}).locator('.rowbtn').click();
  assert.match(await page.locator('#memTopics li').first().textContent(), /第一阶段三月底验收（「第一阶段在三月底完成验收」/);
  await page.click('#kbtab-overview');
  await page.click('#kbMore summary');
  await page.click('#memLog');
  const rec = page.locator('#memLogList li').first();
  assert.match(await rec.textContent(), /手动 · 新建主题 1 个新建主题：验收安排/);
  await noHorizontalScroll(page);
  await rec.getByRole('button', {name: '撤销这次整理'}).click();
  await page.locator('#memUpdStatus', {hasText: '已撤销'}).waitFor();
  await page.locator('#memLogList li', {hasText: '还没有整理记录'}).waitFor();
  await page.click('#kbtab-topics');
  await page.locator('#memTopics li', {hasText: '还没有主题'}).waitFor();
  await page.click('#memSettings');
  await page.uncheck('#memAuto');
  await page.locator('#memStatus', {hasText: '已关闭空闲时自动整理'}).waitFor();
  await page.goto(url);
  await page.click('#memSettings');
  assert.strictEqual(await page.locator('#memAuto').isChecked(), false);
});

test('「忽略 Markdown 文件」默认勾选，搜索与浏览都不列 .md，扩展名填 md 时照列，取消后恢复并记住', async t => {
  const {page, url} = await launch(t, {mdDefault: true, files: {'说明.markdown': '丙'}});
  await page.locator('#browseList li').first().waitFor();
  assert.ok(await page.locator('#ignoreMd').isChecked());
  assert.strictEqual(await page.locator('#findBody label', {hasText: '忽略 Markdown 文件'}).count(), 1, '只有一个勾选框');
  const names = () => page.$$eval('#results li .name', els => els.map(e => e.textContent));
  await page.fill('#q', '资料');
  await page.waitForFunction(() => document.querySelectorAll('#results li').length === 2);
  assert.ok(!(await names()).some(n => /\.(md|markdown)$/.test(n)));
  await page.fill('#q', '');
  await page.fill('#ext', 'md');
  await page.locator('#results li', {hasText: '会议记录.md'}).waitFor();
  await page.fill('#ext', '');
  await page.locator('#browseList button.dir').first().click();
  await page.locator('#browseList li', {hasText: '季度总结.txt'}).waitFor();
  assert.strictEqual(await page.locator('#browseList li', {hasText: '说明.markdown'}).count(), 0);
  assert.strictEqual(await page.locator('#browseList li:has(button.dir)', {hasText: '子目录'}).count(), 0);   // 只有 .md 的子文件夹不列
  await page.uncheck('#ignoreMd');
  await page.locator('#browseList li', {hasText: '说明.markdown'}).waitFor();
  await page.locator('#browseList li:has(button.dir)', {hasText: '子目录'}).waitFor();
  await page.waitForTimeout(300);
  await page.goto(url);
  await page.locator('#findStatus:not(:empty)').waitFor();
  assert.strictEqual(await page.locator('#ignoreMd').isChecked(), false);
  await noHorizontalScroll(page);
});

test('页头写明本机知识库：徽标、一句话与两句说明，空库时说明隐藏、由引导代替', async t => {
  const {page} = await launch(t);
  assert.strictEqual(await page.textContent('header .badge'), '本机知识库');
  assert.strictEqual(await page.textContent('#sub'), '添加文件夹后其中的文件自动收录；提问时参考知识库，回答附出处。');
  assert.ok(await page.locator('#sub').isHidden());
  assert.ok(await page.locator('#guide').isVisible());
  await page.click('#skipGuide');
  assert.ok(await page.locator('#guide').isHidden());
  await page.reload();
  assert.ok(await page.locator('#guide').isHidden(), '「先不建库」记在设置里');
});


test('搜索结果少于 50 个时可全部加入，跳过读不了的', async t => {
  const {page} = await launch(t, {files: {'报告甲.txt': '甲', '报告乙.md': '乙', '报告丙.rar': 'x'}});
  await page.fill('#q', '资料');
  await page.getByRole('button', {name: '全部加入（4 个）'}).waitFor();     // 不限关键字，列表少于 50 个时就出现
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
  assert.ok(await page.isHidden('#addAll'));
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
  assert.strictEqual(await page.textContent('#skillHint'), '', '没选 skill 时不常驻自建目录的路径');
  await page.click('#skillManage');
  assert.match(await page.textContent('#skUserDir'), /opencode\/skills/, '自建目录写在 skill 设置里');
  await page.click('#skillManage');
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
  await page.locator('#chatStatus').getByText(/已保存为 资料管家-(总结一下|季度要点)-\d{8}-\d{4}\.docx/).waitFor();
  const docxText = file => execFileSync(PYTHON, ['-c', 'import sys,zipfile;print(zipfile.ZipFile(sys.argv[1]).read("word/document.xml").decode())',
                                                 file]).toString();
  const single = docxText(await d1.path());
  assert.match(single, /参考文件[\s\S]*资料\/季度总结\.txt/, '单条回答末尾附参考文件');
  const [d2] = await Promise.all([page.waitForEvent('download'), page.click('#saveDocx')]);
  const zip = fs.readFileSync(await d2.path());
  assert.strictEqual(zip.subarray(0, 2).toString(), 'PK');
  const unzip = execFileSync(PYTHON, ['-c', 'import sys,zipfile;print(zipfile.ZipFile(sys.argv[1]).read("word/document.xml").decode())',
                                       await d2.path()]).toString();
  assert.match(unzip, /资料管家 对话/);
  assert.match(unzip, /总结一下/);
  assert.match(unzip, /你好，世界[\s\S]*参考文件/, '参考文件在最后');
  assert.doesNotMatch(unzip, new RegExp(path.sep === '/' ? os.tmpdir() : '^$'), '不写上级路径');
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

test('帮助页写明新对话与支持格式（页头不再写），页脚写明多页面的退出规则', async t => {
  const {page} = await launch(t);
  assert.doesNotMatch(await page.textContent('#sub'), /支持|skill/);
  const help = fs.readFileSync(path.join(TOOL, 'help.html'), 'utf-8');
  assert.match(help, /「＋ 新对话」/);
  assert.match(help, /支持 Word、Excel、PowerPoint（含旧版 doc、xls、ppt 与 wps、et、dps）、PDF/);
  assert.match(help, /<dt>收录<\/dt>.*<dt>整理<\/dt>.*<dt>结论<\/dt>.*<dt>主题<\/dt>.*<dt>原文段落<\/dt>.*<dt>已选<\/dt>/s, '开头的术语表');
  assert.match(await page.textContent('footer'), /关闭全部页面几秒后服务退出，点「退出」立即结束/);
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

test('浏览：搜索框为空时显示文件夹内容，逐层进入文件夹，全部加入含子文件夹', async t => {
  const {page} = await launch(t, {files: {'子目录/深一层/纪要.txt': '丙'.repeat(10)}});
  assert.strictEqual(await page.locator('#kbFiles [role=tab]').count(), 0, '「文件」标签里没有再分标签页');
  assert.ok(await page.isVisible('#q'));
  await page.locator('#crumbs [aria-current]', {hasText: '全部'}).waitFor();
  await page.locator('#browseList button.dir').first().click();
  await page.locator('#crumbs [aria-current]', {hasText: '资料'}).waitFor();
  await page.getByRole('button', {name: '打开文件夹 子目录'}).waitFor();
  assert.match(await page.locator('#browseList li:has(button.dir)', {hasText: '子目录'}).textContent(), /2 个文件/);
  // 与在这个文件夹里空关键字搜索相同：各层文件都列出，写明所在文件夹，点它进入
  const deep = page.locator('#browseList li', {hasText: '纪要.txt'});
  assert.strictEqual(await deep.locator('[data-act="folder"]').textContent(), '资料/子目录/深一层');
  assert.match(await page.textContent('#findStatus'), /共 4 个文件（含子文件夹），1 个子文件夹/);
  const files = () => page.$$eval('#browseList li .name', els => els.map(e => e.textContent));
  await page.selectOption('#sortBy', 'name_asc');
  await page.waitForFunction(() => document.querySelector('#browseList li .name').textContent === '会议记录.md');
  assert.deepStrictEqual(await files(), ['会议记录.md', '季度总结.txt', '纪要.txt', '资料包.zip']);
  await page.selectOption('#sortBy', 'name_desc');
  await page.waitForFunction(() => document.querySelector('#browseList li .name').textContent === '资料包.zip');
  assert.deepStrictEqual(await files(), ['资料包.zip', '纪要.txt', '季度总结.txt', '会议记录.md']);
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
});

test('在文件夹里搜索：含各层子文件夹，只比相对路径；清空后回到文件夹内容', async t => {
  const {page} = await launch(t, {files: {'子目录/深一层/纪要.txt': '丙', '其他/资料清单.txt': '丁'}});
  await page.locator('#browseList button.dir').first().click();
  await page.getByRole('button', {name: '打开文件夹 子目录'}).click();
  await page.locator('#crumbs [aria-current]', {hasText: '子目录'}).waitFor();
  assert.match(await page.getAttribute('#q', 'placeholder'), /^在「子目录」里搜索：/);
  await page.fill('#q', '纪要');
  await page.locator('#results li', {hasText: '纪要.txt'}).waitFor();
  assert.ok(await page.isHidden('#browsePane'));
  assert.match(await page.textContent('#findStatus'), /在「子目录」里找到 1 个/);
  // 上级文件夹名「资料」不算命中
  await page.fill('#q', '资料');
  await page.locator('#findStatus', {hasText: '在「子目录」里找到 0 个'}).waitFor();
  // 只填扩展名也算搜索
  await page.fill('#q', '');
  await page.fill('#ext', 'md');
  await page.locator('#results li', {hasText: '会议记录.md'}).waitFor();
  assert.strictEqual(await page.locator('#results li').count(), 1);
  await page.fill('#ext', '');
  await page.locator('#browseList li', {hasText: '会议记录.md'}).waitFor();
  assert.ok(await page.isHidden('#searchPane'));
  // 点「全部」保留关键字，搜全部文件夹
  await page.fill('#q', '资料');
  await page.locator('#crumbs').getByRole('button', {name: '全部'}).click();
  await page.locator('#results li', {hasText: '资料清单.txt'}).waitFor();
  assert.strictEqual(await page.inputValue('#q'), '资料');
  await page.click('#qClear');
  await page.locator('#crumbs [aria-current]', {hasText: '全部'}).waitFor();
  assert.strictEqual(await page.inputValue('#q'), '');
  assert.ok(await page.isHidden('#qClear'));
  await page.locator('#browseList button.dir', {hasText: '资料'}).waitFor();
});

test('点搜索结果里的所在文件夹：进入那个文件夹，关键字保留；重新打开回到上次的文件夹', async t => {
  const {page} = await launch(t);
  await page.fill('#q', '会议');
  await page.getByRole('button', {name: '进入文件夹 资料/子目录'}).click();
  await page.locator('#crumbs [aria-current]', {hasText: '子目录'}).waitFor();
  assert.strictEqual(await page.inputValue('#q'), '会议');
  await page.locator('#results li', {hasText: '会议记录.md'}).waitFor();
  await page.reload();
  await page.locator('#crumbs [aria-current]', {hasText: '子目录'}).waitFor();
  assert.strictEqual(await page.inputValue('#q'), '', '关键字不记');
  await page.locator('#browseList li', {hasText: '会议记录.md'}).waitFor();
  // 记住的文件夹已不在索引里：静默回到「全部」
  await page.evaluate(() => localStorage.setItem('ask-local-files.find', JSON.stringify({dir: '/不存在/的文件夹', sort: 'time_desc'})));
  await page.reload();
  await page.locator('#crumbs [aria-current]', {hasText: '全部'}).waitFor();
  await page.locator('#browseList button.dir', {hasText: '资料'}).waitFor();
  assert.doesNotMatch(await page.textContent('#findStatus'), /失败/);
});

test('浏览里的全部加入超过已选名额时不可点并写明', async t => {
  const files = {};
  for (let i = 1; i <= 51; i++) files['大批/文件' + i + '.txt'] = String(i);
  const {page} = await launch(t, {files});
  await page.locator('#browseList button.dir').first().click();
  await page.getByRole('button', {name: '打开文件夹 大批'}).click();
  const b = page.getByRole('button', {name: '全部加入（51 个，含子文件夹）'});
  await b.waitFor();
  assert.ok(await b.isDisabled());
  assert.match(await page.textContent('#browseAddHint'), /还能加的 50 个/);
});

test('360px 下浏览不横向溢出', async t => {
  const {page} = await launch(t, {width: 360});
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
  await openFolders(page);
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
  const code = 'import importlib.util, json, os, sys\nsys.path.insert(0, os.path.dirname(sys.argv[1]))\n' +
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
  await page.uncheck('#dedup');
  await page.locator('#results > li').nth(2).waitFor();
  await page.reload();
  await page.waitForFunction(() => document.querySelector('#ver').textContent.includes('v') && !document.querySelector('#dedup').checked);
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
  await page.locator('#browseList button.dir').first().click();
  await page.locator('#findStatus').getByText('共 6 个文件（含子文件夹），去重后 4 个，1 个子文件夹').waitFor();
  await page.locator('#browseAdd', {hasText: '全部加入（3 个，含子文件夹）'}).waitFor();
  assert.strictEqual(await page.locator('#browseList [data-act="dups"]').count(), 1);
  await page.uncheck('#dedup');
  await page.locator('#browseAdd', {hasText: '全部加入（5 个，含子文件夹）'}).waitFor();
  assert.strictEqual(await page.locator('#findBody label.dedup', {hasText: '按文件名去重'}).count(), 1, '只有一个去重勾选框');
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
  await openFolders(page);
  const b = page.getByRole('button', {name: '选择要添加的文件夹'});
  await b.click();
  await page.locator('#folderStatus', {hasText: /^正在打开选择窗口……已等 \d+ 秒$/}).waitFor();
  assert.match(await page.getAttribute('#folderStatus', 'class'), /\bbusy\b/);
  assert.ok(await b.isDisabled());
  await page.waitForFunction(() => document.querySelector('#folder').value === '/选中的/文件夹');
  assert.strictEqual(await page.textContent('#folderStatus'), '');
  assert.match(await page.textContent('#folders'), /列表是空的/, '不自动添加');
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

test('「选择……」等待时显示秒数，10 秒后提示窗口可能在后面，点「取消」结束选择窗口、按钮恢复、状态清空', async t => {
  const {bin, ctl} = fakePickerBin(t);
  fs.writeFileSync(ctl, '/选中的/文件夹');
  const {page} = await launch(t, {folders: false, width: 360, env: {PATH: bin + ':' + process.env.PATH, DISPLAY: ':99',
    FAKE_PICK_FROM: ctl, FAKE_PICK_SLEEP: '60'}});
  await openFolders(page);
  const b = page.getByRole('button', {name: '选择要添加的文件夹'});
  await b.focus();
  await page.keyboard.press('Enter');
  const cancel = page.locator('#pickFolder + button', {hasText: '取消'});
  await cancel.waitFor();
  assert.ok(await cancel.evaluate(e => e === document.activeElement), '键盘操作时焦点移到「取消」');
  await page.locator('#folderStatus', {hasText: '已等 1 秒'}).waitFor({timeout: 4000});
  await noHorizontalScroll(page);
  await page.locator('#folderStatus', {hasText: '窗口可能在浏览器后面；也可以点「取消」后直接输入路径'}).waitFor({timeout: 15000});
  assert.match(await page.textContent('#folderStatus'), /已等 1\d 秒/);
  await noHorizontalScroll(page);
  await cancel.click();
  await page.waitForFunction(() => !document.querySelector('#pickFolder').disabled, null, {timeout: 5000});
  assert.strictEqual(await page.textContent('#folderStatus'), '');
  assert.strictEqual(await page.locator('#pickFolder + button', {hasText: '取消'}).count(), 0);
  assert.strictEqual(await page.inputValue('#folder'), '', '取消不改输入框');
  // 服务端的选择窗口已结束：马上再点不会报「已有选择窗口打开」，再取消一次
  await b.click();
  await page.locator('#folderStatus', {hasText: '已等 1 秒'}).waitFor({timeout: 4000});
  assert.match(await page.getAttribute('#folderStatus', 'class'), /\bbusy\b/);
  await cancel.click();
  await page.waitForFunction(() => !document.querySelector('#pickFolder').disabled, null, {timeout: 5000});
  assert.strictEqual(await page.textContent('#folderStatus'), '');
});

test('进行中提示蓝底转圈，结果提示带同色底色条；减少动态效果时不转', async t => {
  const {page} = await launch(t, {model: 'slow'});
  await pickByName(page, '季度总结');
  await page.fill('#ask', '总结');
  await page.click('#send');
  await page.locator('#askStatus', {hasText: '已等待'}).waitFor();
  assert.match(await page.getAttribute('#askStatus', 'class'), /\bbusy\b/);
  const busy = await page.$eval('#askStatus', e => {
    const s = getComputedStyle(e), b = getComputedStyle(e, '::before');
    return {bg: s.backgroundColor, anim: b.animationName, border: b.borderTopColor};
  });
  assert.strictEqual(busy.anim, 'spin');
  assert.notStrictEqual(busy.bg, 'rgba(0, 0, 0, 0)');
  await page.click('#stop');
  await page.locator('#askStatus:not(.busy)').waitFor();
  // 结果提示：ok、warn、bad 都有底色，且三种不同
  const bgs = await page.evaluate(() => ['ok', 'warn', 'bad', 'info'].map(k => {
    const d = document.createElement('div');
    d.className = 'status ' + k;
    d.textContent = '提示';
    document.body.appendChild(d);
    const c = getComputedStyle(d).backgroundColor;
    d.remove();
    return c;
  }));
  for (const c of bgs.slice(0, 3)) assert.notStrictEqual(c, 'rgba(0, 0, 0, 0)');
  assert.strictEqual(new Set(bgs.slice(0, 3)).size, 3);
  assert.strictEqual(bgs[3], 'rgba(0, 0, 0, 0)', '普通说明不加底色');
  await page.emulateMedia({reducedMotion: 'reduce'});
  const still = await page.evaluate(() => {
    const d = document.createElement('div');
    d.className = 'status info busy';
    d.textContent = '正在……';
    document.body.appendChild(d);
    const a = getComputedStyle(d, '::before').animationName;
    d.remove();
    return a;
  });
  assert.strictEqual(still, 'none');
});

test('没有可用的选择窗口时不显示「选择……」', async t => {
  const {page} = await launch(t, {env: {DISPLAY: '', WAYLAND_DISPLAY: ''}});
  await page.locator('#browseList li').first().waitFor();
  assert.ok(await page.isHidden('#pickFolder'));
});

test('360px 下选择按钮、去重开关与同名展开都不横向溢出', async t => {
  const {bin} = fakePickerBin(t);
  const {page} = await launch(t, {width: 360, files: {'很长很长的报告文件名称用来测试换行.txt': '甲', '很长很长的报告文件名称用来测试换行 - 副本.txt': '乙'},
    env: {PATH: bin + ':' + process.env.PATH, DISPLAY: ':99'}});
  await openFolders(page);
  await noHorizontalScroll(page);
  await closeFolders(page);
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
  await page.uncheck('#dedup');
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
  assert.ok(await page.isHidden('#addAll'));
  for (const i of [2, 3, 4]) {
    await page.getByRole('button', {name: '从列表移除 报告' + i + '.txt', exact: true}).click();
    await page.locator('#findStatus', {hasText: '已从列表移除 ' + (i - 1) + ' 个'}).waitFor();
  }
  await page.getByRole('button', {name: '全部加入（49 个）'}).waitFor();
});

test('浏览里从列表移除：该行消失、全部加入减 1，进别的文件夹后恢复', async t => {
  const {page} = await launch(t);
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
  assert.ok(await page.isVisible('#regex'), '「正则表达式」常驻');
  await page.uncheck('#regex');
  assert.doesNotMatch(await page.getAttribute('#q', 'placeholder'), /正则/);
});

// ---------------- v1.10 界面优化

test('打开页面停在「全部」，搜索框常驻；在「全部」里搜索即全局搜索', async t => {
  const {page} = await launch(t);
  await page.locator('#crumbs [aria-current]', {hasText: '全部'}).waitFor();
  assert.ok(await page.isVisible('#q'));
  await page.locator('#findStatus', {hasText: '已添加 1 个文件夹'}).waitFor();
  await page.fill('#q', '会议');
  await page.locator('#results li', {hasText: '会议记录.md'}).waitFor();
  assert.doesNotMatch(await page.textContent('#findStatus'), /在「/);
});

test('排序：四种顺序，搜索与浏览都生效，选择记在本浏览器', async t => {
  const {page, docs} = await launch(t, {files: {'排序/b.txt': 'b', '排序/C.txt': 'c', '排序/a.txt': 'a'}});
  [['b.txt', 1700000000], ['C.txt', 1700000100], ['a.txt', 1700000200]].forEach(([n, ts]) => fs.utimesSync(path.join(docs, '排序', n), ts, ts));
  await page.evaluate(() => post('/api/refresh', {full: true}));      // 改了修改时间、文件夹没变，须重建索引
  await page.fill('#q', '排序');
  const names = sel => page.locator(sel + ' .name').allTextContents();
  await page.waitForFunction(() => [...document.querySelectorAll('#results .name')].map(x => x.textContent).join() === 'a.txt,C.txt,b.txt');
  assert.strictEqual(await page.inputValue('#sortBy'), 'time_desc');
  await page.selectOption('#sortBy', 'name_asc');
  await page.waitForFunction(() => [...document.querySelectorAll('#results .name')].map(x => x.textContent).join() === 'a.txt,b.txt,C.txt');
  await page.selectOption('#sortBy', 'time_asc');
  await page.waitForFunction(() => [...document.querySelectorAll('#results .name')].map(x => x.textContent).join() === 'b.txt,C.txt,a.txt');
  await page.reload();
  assert.strictEqual(await page.inputValue('#sortBy'), 'time_asc');
  await page.locator('#browseList button.dir').first().click();
  await page.getByRole('button', {name: '打开文件夹 排序'}).click();
  await page.locator('#crumbs [aria-current]', {hasText: '排序'}).waitFor();
  await page.selectOption('#sortBy', 'name_desc');
  await page.waitForFunction(() => [...document.querySelectorAll('#browseList .name')].map(x => x.textContent).join() === 'C.txt,b.txt,a.txt');
  assert.deepStrictEqual(await names('#browseList'), ['C.txt', 'b.txt', 'a.txt']);
});

test('加入前 10 个、前 30 个：按当前排序取，名额不足时不可点', async t => {
  const files = {};
  for (let i = 0; i < 35; i++) files['批量/' + String(i).padStart(2, '0') + '.txt'] = 'x';
  const {page, docs} = await launch(t, {files});
  await page.selectOption('#sortBy', 'name_asc');
  await page.fill('#q', '批量');
  await page.locator('#findStatus', {hasText: '找到 35 个'}).waitFor();
  assert.ok(await page.isVisible('#addAll'));
  assert.ok(await page.isVisible('#addTop10'));
  assert.ok(await page.isVisible('#addTop30'));
  await page.click('#addTop10');
  await page.locator('#pickStatus', {hasText: '已加入列表里的前 10 个'}).waitFor();
  assert.deepStrictEqual((await page.locator('#picked li .name').allTextContents()).slice(0, 2), ['00.txt', '01.txt']);
  assert.strictEqual(await page.locator('#picked > li').count(), 10);
  // 还剩 25 个可加入：前 10 个仍有，前 30 个不再显示
  assert.ok(await page.isVisible('#addTop10'));
  assert.ok(await page.isHidden('#addTop30'));
  await page.click('#addTop10');
  await page.click('#addTop10');
  await page.waitForFunction(() => document.querySelectorAll('#picked > li').length === 30);
  // 浏览：已选还能加 20 个，「加入前 30 个」不可点
  for (let i = 0; i < 31; i++) fs.writeFileSync(path.join(docs, '批量', 'z' + i + '.txt'), 'x');
  await page.click('#refresh');
  await page.fill('#q', '');
  await page.locator('#browseList button.dir').first().click();
  await page.getByRole('button', {name: '打开文件夹 批量'}).click();
  await page.locator('#crumbs [aria-current]', {hasText: '批量'}).waitFor();
  await page.locator('#browseTop30').waitFor();
  assert.ok(await page.isDisabled('#browseTop30'));
  assert.ok(!(await page.isDisabled('#browseTop10')));
  assert.match(await page.textContent('#browseAddHint'), /点「加入前 N 个」/);
  await page.click('#browseTop10');
  await page.locator('#pickStatus', {hasText: '已加入本文件夹（含子文件夹）里的前 10 个'}).waitFor();
  assert.strictEqual(await page.locator('#picked > li').count(), 40);
  await page.setViewportSize({width: 360, height: 800});
  await noHorizontalScroll(page);
});

test('复制已选择的文件路径：一行一个，提示可交给智能体', async t => {
  const {page, docs} = await launch(t);
  assert.ok(await page.isHidden('#copyPaths'));
  await pickByName(page, '季度总结');
  await pickByName(page, '会议记录');
  assert.strictEqual(await page.textContent('#copyPaths'), '复制已选择的文件路径');
  await page.click('#copyPaths');
  await page.locator('#pickStatus', {hasText: '已复制 2 个已选择的文件路径'}).waitFor();
  assert.match(await page.textContent('#pickStatus'), /OpenCode 等智能体/);
  const clip = await page.evaluate(() => navigator.clipboard.readText());
  assert.strictEqual(clip, [path.join(docs, '季度总结.txt'), path.join(docs, '子目录', '会议记录.md')].join('\n'));
});

test('管理文件夹与两个设置入口带图标与展开箭头，知识库设置是卡片头的齿轮图标按钮', async t => {
  const {page} = await launch(t);
  assert.strictEqual(await page.locator('#manageFolders svg.icon').count(), 1);
  assert.strictEqual(await page.locator('#manageFolders .arrow').count(), 0, '管理文件夹打开大窗口，不是展开');
  for (const id of ['modelSettings', 'skillManage']) {
    const b = page.locator('#' + id);
    assert.strictEqual(await b.locator('svg.icon').count(), 1);
    assert.strictEqual(await b.locator('.arrow').textContent(), '▸');
    assert.notStrictEqual(await b.evaluate(e => getComputedStyle(e).borderTopStyle), 'none');
    await b.click();
    assert.strictEqual(await b.locator('.arrow').textContent(), '▾');
    assert.strictEqual(await b.getAttribute('aria-expanded'), 'true');
  }
  const gear = page.locator('#findCard .card-head #memSettings');
  assert.strictEqual(await gear.locator('svg.icon').count(), 1);
  assert.strictEqual(await gear.getAttribute('aria-expanded'), 'false');
  await gear.click();
  assert.strictEqual(await gear.getAttribute('aria-expanded'), 'true');
  assert.strictEqual(await page.getByRole('button', {name: '管理文件夹'}).count(), 1);
  assert.strictEqual(await page.getByRole('button', {name: '模型设置'}).count(), 1);
  for (const name of ['skill 设置', '知识库设置']) assert.strictEqual(await page.getByRole('button', {name, exact: true}).count(), 1);
  assert.strictEqual(await page.locator('#askCard #memSettings').count(), 0, '问 AI 里只留「这次提问参考知识库」');
});

// ---------------- 设置面板：模型、skill 两个标签（v2.29 起保存位置在知识库设置里）
const SET_TABS = ['tab-model', 'tab-skill'];
async function selectedTab(page) {
  return page.evaluate(() => [...document.querySelectorAll('#setTabs [role=tab]')]
    .filter(b => b.getAttribute('aria-selected') === 'true').map(b => b.id));
}
// 一次只显示一页：所选标签的页可见，其余隐藏
async function onlyPage(page, tab) {
  for (const id of SET_TABS) {
    const panel = await page.getAttribute('#' + id, 'aria-controls');
    assert.strictEqual(await page.locator('#' + panel).isVisible(), id === tab, panel);
  }
}

test('设置面板：两个入口打开对应标签，再点收起；位于选项行下方，打开时焦点在当前标签', async t => {
  const {page} = await launch(t);
  assert.ok(await page.locator('#setPanel').isHidden());
  for (const [entry, tab] of [['modelSettings', 'tab-model'], ['skillManage', 'tab-skill']]) {
    await page.click('#' + entry);
    assert.deepStrictEqual(await selectedTab(page), [tab]);
    await onlyPage(page, tab);
    assert.strictEqual(await page.evaluate(() => document.activeElement.id), tab);
    assert.strictEqual(await page.getAttribute('#' + entry, 'aria-expanded'), 'true');
    for (const other of ['modelSettings', 'skillManage'].filter(x => x !== entry)) {
      assert.strictEqual(await page.getAttribute('#' + other, 'aria-expanded'), 'false');
    }
  }
  const [optBottom, top] = await page.evaluate(() => [document.querySelector('#optRow').getBoundingClientRect().bottom,
                                                       document.querySelector('#setPanel').getBoundingClientRect().top]);
  assert.ok(top >= optBottom, '设置面板在选项行下方');
  // 已开在该标签时再点收起，焦点留在入口
  await page.click('#skillManage');
  assert.ok(await page.locator('#setPanel').isHidden());
  assert.strictEqual(await page.getAttribute('#skillManage', 'aria-expanded'), 'false');
  // 没有「通用」标签，保存位置在知识库设置里；「重新查找」「重新读取」仍在选项行
  assert.strictEqual(await page.locator('#tab-general').count(), 0);
  assert.strictEqual(await page.locator('#kbSetPanel #dataDir').count(), 1);
  assert.ok(await page.locator('#optRow #findLocal').isVisible());
  assert.ok(await page.locator('#optRow #skillReload').isVisible());
});

test('设置面板：方向键切换标签，Esc 与「收起」关闭并把焦点还给入口，输入框内容保留', async t => {
  const {page} = await launch(t);
  await page.click('#modelSettings');
  await page.keyboard.press('ArrowRight');
  assert.deepStrictEqual(await selectedTab(page), ['tab-skill']);
  assert.strictEqual(await page.evaluate(() => document.activeElement.id), 'tab-skill');
  await onlyPage(page, 'tab-skill');
  await page.keyboard.press('ArrowRight');           // 末尾再右移回到第一个
  assert.deepStrictEqual(await selectedTab(page), ['tab-model']);
  await page.keyboard.press('ArrowLeft');
  assert.deepStrictEqual(await selectedTab(page), ['tab-skill']);
  await page.keyboard.press('Home');
  assert.deepStrictEqual(await selectedTab(page), ['tab-model']);
  await page.keyboard.press('End');
  assert.deepStrictEqual(await selectedTab(page), ['tab-skill']);
  // 只有当前标签可用 Tab 键聚焦
  assert.deepStrictEqual(await page.evaluate(() => [...document.querySelectorAll('#setTabs [role=tab]')].map(b => b.tabIndex)), [-1, 0]);
  // 输入框里按 Esc 也收起，焦点回到打开它的入口；切换标签与收起不丢输入
  await page.click('#tab-model');
  await page.fill('#cBase', 'http://127.0.0.1:9/v1');
  await page.click('#tab-skill');
  await page.click('#tab-model');
  assert.strictEqual(await page.inputValue('#cBase'), 'http://127.0.0.1:9/v1');
  await page.focus('#cBase');
  await page.keyboard.press('Escape');
  assert.ok(await page.locator('#setPanel').isHidden());
  assert.strictEqual(await page.evaluate(() => document.activeElement.id), 'modelSettings');
  await page.click('#skillManage');
  await page.click('#setClose');
  assert.ok(await page.locator('#setPanel').isHidden());
  assert.strictEqual(await page.evaluate(() => document.activeElement.id), 'skillManage');
  await page.click('#modelSettings');
  assert.strictEqual(await page.inputValue('#cBase'), 'http://127.0.0.1:9/v1');
});

test('设置面板：记住上次打开的标签，刷新后面板收起；读不到时默认模型标签', async t => {
  const {page} = await launch(t);
  assert.strictEqual(await page.evaluate(() => shownTab()), 'model');
  await page.click('#skillManage');
  assert.strictEqual(await page.evaluate(() => localStorage.getItem('ask-local-files.setTab')), 'skill');
  await page.reload();
  await page.locator('#findStatus:not(:empty)').waitFor();
  assert.ok(await page.locator('#setPanel').isHidden());
  assert.strictEqual(await page.evaluate(() => shownTab()), 'skill');
  // 旧版记下的「通用」已没有：回到模型标签
  await page.evaluate(() => localStorage.setItem('ask-local-files.setTab', 'general'));
  await page.reload();
  await page.locator('#findStatus:not(:empty)').waitFor();
  assert.strictEqual(await page.evaluate(() => shownTab()), 'model');
  await page.evaluate(() => localStorage.setItem('ask-local-files.setTab', 'nope'));
  await page.reload();
  await page.locator('#findStatus:not(:empty)').waitFor();
  assert.strictEqual(await page.evaluate(() => shownTab()), 'model');
});

test('设置面板：读不了本地存储时照常打开，默认模型标签', async t => {
  const {page} = await launch(t, {beforeGoto: p => p.addInitScript(() => {
    Object.defineProperty(window, 'localStorage', {get() { throw new Error('denied'); }});
  })});
  await page.locator('#findStatus:not(:empty)').waitFor();
  await page.click('#skillManage');
  assert.deepStrictEqual(await selectedTab(page), ['tab-skill']);
  await page.click('#tab-model');
  assert.deepStrictEqual(await selectedTab(page), ['tab-model']);
});

test('设置面板：有未保存改动的标签加圆点，保存或改回原值后消失，不弹窗', async t => {
  const {page} = await launch(t);
  let dialogs = 0;
  page.onDialog = d => { dialogs++; d.accept(); };
  await page.click('#modelSettings');
  const dot = id => page.locator('#' + id + ' .dirty');
  for (const id of SET_TABS) assert.ok(await dot(id).isHidden(), id);
  const old = await page.inputValue('#limit');
  await page.fill('#limit', '200000');
  assert.ok(await dot('tab-model').isVisible());
  assert.match(await page.locator('#tab-model').textContent(), /有未保存的改动/);
  assert.strictEqual(await page.getByRole('tab', {name: /模型.*有未保存的改动/}).count(), 1);
  await page.fill('#limit', old);                       // 改回原值
  assert.ok(await dot('tab-model').isHidden());
  await page.fill('#limit', '200000');
  await page.click('#saveLimit');
  await page.locator('#settingsStatus.ok', {hasText: '字数上限已保存'}).waitFor();
  assert.ok(await dot('tab-model').isHidden());
  // skill：填了就算改动
  await page.click('#tab-skill');
  await page.fill('#skDir', '/tmp/some-skill');
  assert.ok(await dot('tab-skill').isVisible());
  await page.fill('#skDir', '');
  assert.ok(await dot('tab-skill').isHidden());
  assert.strictEqual(dialogs, 0);
});

test('设置面板：本机没有可用模型时只显示模型标签', async t => {
  const {page} = await launch(t, {model: null});
  await page.locator('#noAiNote').waitFor();
  await page.click('#modelSettings');
  assert.ok(await page.locator('#tab-skill').isHidden());
  assert.ok(await page.locator('#skillManage').isHidden());
  assert.deepStrictEqual(await page.locator('#setTabs [role=tab]:visible').evaluateAll(bs => bs.map(b => b.id)),
                         ['tab-model']);
  await page.keyboard.press('ArrowRight');              // 方向键跳过隐藏的 skill 标签
  assert.deepStrictEqual(await selectedTab(page), ['tab-model']);
  // 上次停在 skill 标签时改显示模型标签
  await page.evaluate(() => localStorage.setItem('ask-local-files.setTab', 'skill'));
  await page.reload();
  await page.locator('#noAiNote').waitFor();
  assert.strictEqual(await page.evaluate(() => shownTab()), 'model');
});

test('设置面板：每节常驻一句要点，「详细说明」可展开；知识库设置的五项顺序', async t => {
  const {page} = await launch(t);
  await page.click('#memSettings');
  const order = await page.locator('#kbSetPanel').evaluate(p =>
    ['#memoryOn', '#tocPick', '#memAuto', '#memMb', '#dataDir', '#memClear']
      .map(sel => p.querySelector(sel)).map(e => [...p.querySelectorAll('*')].indexOf(e)));
  assert.deepStrictEqual(order, [...order].sort((a, b) => a - b), '知识库设置自上而下的顺序');
  assert.ok(order.every(i => i >= 0));
  await page.click('#modelSettings');
  const more = page.locator('#modelPanel details.more').first();
  assert.ok(await more.locator('.hint').isHidden());
  await more.locator('summary').click();
  assert.ok(await more.locator('.hint').isVisible());
  // 每页都有「详细说明」；「保存位置」在知识库设置里
  for (const panel of ['modelPanel', 'skillPanel', 'kbSetPanel']) {
    assert.ok(await page.locator('#' + panel + ' details.more summary', {hasText: '详细说明'}).count() > 0, panel);
  }
  assert.strictEqual(await page.locator('#kbSetPanel #dataDir').count(), 1);
  assert.strictEqual(await page.locator('#modelPanel #limit').count(), 1);
  assert.strictEqual(await page.locator('#modelPanel #ocKey').count(), 1);
  assert.strictEqual(await page.locator('#skillPanel #skCreate').count(), 1);
});

test('设置面板在 360px 下没有横向滚动，深色模式下标签与圆点可辨', async t => {
  for (const scheme of ['light', 'dark']) {
    const {page} = await launch(t, {width: 360, beforeGoto: p => p.emulateMedia({colorScheme: scheme})});
    await page.click('#modelSettings');
    for (const id of SET_TABS) {
      await page.click('#' + id);
      await page.locator('#setPanel details.more summary >> visible=true').first().click();
      await noHorizontalScroll(page);
    }
    await page.click('#tab-model');
    await page.fill('#limit', '4096');
    const [bg, sel, unsel, dotColor, warn] = await page.evaluate(() => {
      const css = v => { const d = document.createElement('div'); d.style.color = v; document.body.appendChild(d); const c = getComputedStyle(d).color; d.remove(); return c; };
      return [getComputedStyle(document.body).backgroundColor, getComputedStyle(document.querySelector('#tab-model')).color,
              getComputedStyle(document.querySelector('#tab-skill')).color, getComputedStyle(document.querySelector('#tab-model .dirty')).color, css('var(--warn)')];
    });
    assert.strictEqual(dotColor, warn, scheme);
    assert.notStrictEqual(sel, unsel, scheme);
    assert.notStrictEqual(sel, bg, scheme);
    assert.strictEqual(await page.evaluate(() => matchMedia('(prefers-color-scheme: dark)').matches), scheme === 'dark');
  }
});

test('清空已选、清空对话用醒目的红色', async t => {
  const {page} = await launch(t);
  await pickByName(page, '季度总结');
  await page.fill('#q', '资料包');
  await page.locator('#results .bad').waitFor();
  const bad = await page.evaluate(() => getComputedStyle(document.querySelector('#results .bad')).color);
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
  await page.fill('#q', '会议');
  const link = page.getByRole('button', {name: '进入文件夹 资料/子目录'});
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
  await page.locator('#findStatus:not(:empty)').waitFor();     // 页面读完状态后会重画对话区，先等它
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
  // 知识库卡片的标签行与「知识库：已收录……」一行占去约 60px，窗口取 900 高
  const {page} = await launch(t, {width: 1280, height: 900, files});
  await page.fill('#q', '文件');
  await page.locator('#results li').nth(40).waitFor();
  const col = await page.$eval('.col-left', e => ({o: getComputedStyle(e).overflowY, sh: e.scrollHeight, h: e.clientHeight}));
  assert.strictEqual(col.o, 'hidden');
  assert.ok(col.sh <= col.h + 1, '左栏整体不应滚动');
  const r = await page.$eval('#results', e => ({sh: e.scrollHeight, h: e.clientHeight}));
  assert.ok(r.sh > r.h, '结果列表应自己滚动');
  const st = await box(page, '#findStatus'), res = await box(page, '#results'), card = await box(page, '#pickedCard');
  assert.ok(res.y > st.y && res.y + res.height <= card.y + 1, '列表在状态下方、已选上方');
  // 管理文件夹在大窗口里打开，不挤占文件列表
  const before = (await box(page, '#results')).height;
  await openFolders(page);
  assert.strictEqual((await box(page, '#results')).height, before);
  await closeFolders(page);
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
  await f.locator('#findSummary:not(:empty)').waitFor({state: 'attached'});
  await f.locator('#kbtab-files').click();
  await f.locator('#findStatus', {hasText: '个文件'}).waitFor();
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

test('关闭有未保存问答的标签不确认；退出按全部标签合计提醒，退出后标签一起结束', async t => {
  const {page} = await launch(t);
  await pickByName(page, '季度总结');
  await ask(page);
  await page.click('#newConv');
  const f = page.frameLocator('#convFrames iframe');
  await f.locator('#q').fill('会议');
  await f.locator('#results li', {hasText: '会议记录'}).getByRole('button', {name: /加入/}).click();
  await f.locator('#picked li', {hasText: '会议记录'}).getByText(/\d 字/).waitFor();
  await f.locator('#ask').fill('总结');
  await f.locator('#ask').press('Enter');
  await f.locator('#chat .msg.assistant').getByText('你好，世界').waitFor();
  let msg = '';
  page.onDialog = d => { msg = d.message(); d.dismiss(); };
  await page.click('#newConv');
  assert.strictEqual(await page.locator('#convFrames iframe').count(), 2);
  assert.match(await page.getAttribute('#convClose2', 'aria-label'), /^关闭 (季度要点|总结)$/, '关闭按钮写对话标题');
  await page.click('#convClose2');
  assert.strictEqual(msg, '', '关闭标签不弹确认');
  assert.strictEqual(await page.locator('#convFrames iframe').count(), 1, '对话 2 已关闭');
  await page.getByRole('button', {name: '关闭 对话 3'}).click();
  assert.strictEqual(await page.locator('#convFrames iframe').count(), 0);
  await page.click('#quit');
  await page.waitForFunction(() => !document.querySelector('#quit').disabled);
  assert.match(msg, /1 轮问答.*未保存/s);
  assert.doesNotMatch(msg, /还有 \d+ 个页面开着/, '同一窗口的标签不算别的页面');
  page.onDialog = d => d.accept();
  await page.click('#quit');
  await page.locator('#closed').waitFor({state: 'visible'});
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


// ---------------- v2.4 合并搜索与浏览、参考文件放最后、页头说明收起、对话标题

test('保存对话：参考文件放在最后，路径从所添加文件夹的名字起写，文件名带对话标题', async t => {
  const {page, docs} = await launch(t);
  await pickByName(page, '季度总结');
  await pickByName(page, '会议记录');
  await ask(page, '把这两份材料的要点列出来');
  await page.waitForFunction(() => convTitle().text === '季度要点');
  const [dl] = await Promise.all([page.waitForEvent('download'), page.click('#save')]);
  await page.locator('#chatStatus').getByText(/^已保存为 资料管家-季度要点-\d{8}-\d{4}\.md/).waitFor();
  const md = fs.readFileSync(await dl.path(), 'utf-8');
  assert.match(md, /^# 资料管家 对话 \d{8}-\d{4}\n\n## 问\n/, '从第一问开始');
  assert.match(md, /核实后再用。\n\n## 参考文件\n\n1\. 资料\/季度总结\.txt\n2\. 资料\/子目录\/会议记录\.md\n$/);
  assert.ok(!md.includes(docs), '不写完整路径');
  // 复制全部文字同样只写相对路径
  await page.click('#copyTexts');
  await page.locator('#pickStatus', {hasText: '已复制'}).waitFor();
  const clip = await page.evaluate(() => navigator.clipboard.readText());
  assert.match(clip, /<source>资料\/季度总结\.txt<\/source>\n<document_content>\n路径：资料\/季度总结\.txt\n/);
  assert.ok(!clip.includes(docs));
});

test('引导：空库时显示三步，添加了文件夹的第 1 步打勾，第 1 步的链接切到「文件」并打开管理文件夹', async t => {
  const {page} = await launch(t, {folders: false, kbTab: false});
  await page.locator('#guide').waitFor();
  assert.strictEqual(await page.locator('#guide li.done').count(), 0);
  assert.match(await page.textContent('#g2Text'), /收录完的文件马上能用；整理成主题在空闲时进行，不用等。/);
  await page.click('#g1Go');
  await page.waitForFunction(() => document.activeElement && document.activeElement.id === 'folder');
  assert.strictEqual(await page.getAttribute('#kbtab-files', 'aria-selected'), 'true');
  const {page: p2} = await launch(t);
  await p2.locator('#guide').waitFor();
  assert.strictEqual(await p2.locator('#g1.done').count(), 1);
  await p2.click('#kbtab-files');
  await pickByName(p2, '季度总结');
  await ask(p2);
  assert.ok(await p2.locator('#guide').isHidden(), '发出第一问后收起');
});

test('对话标签：先写第一问的前 12 字，回答结束后换成模型概括的标题；清空对话后回到「对话 N」', async t => {
  const {page} = await launch(t);
  await page.click('#newConv');
  await page.locator('#convTab1').click();
  await pickByName(page, '季度总结');
  await page.fill('#ask', '请把这份季度总结的要点整理成三条');
  await page.click('#send');
  await page.locator('#convTab1', {hasText: '季度要点'}).waitFor();
  assert.strictEqual(await page.getAttribute('#convTab1', 'title'), '请把这份季度总结的要点整理成三条');
  await page.getByRole('button', {name: '关闭 对话 2'}).waitFor();
  await page.fill('#ask', '再详细一点');
  await page.click('#send');
  await page.waitForFunction(() => document.querySelectorAll('#chat .msg.assistant').length === 2 && !document.querySelector('#send').disabled);
  assert.strictEqual(await page.textContent('#convTab1'), '季度要点', '追问不改标题');
  await page.click('#clear');
  await page.locator('#convTab1', {hasText: '对话 1'}).waitFor();
  let msg = '';
  page.onDialog = d => { msg = d.message(); d.dismiss(); };
  await ask(page, '再问一次');
  await page.locator('#convTab1', {hasText: '季度要点'}).waitFor();
  await page.click('#quit');
  await page.waitForFunction(() => !document.querySelector('#quit').disabled);
  assert.match(msg, /当前 1 轮问答未保存|1 个对话标签（季度要点）/);
});

test('对话标题概括失败时保留第一问的前 12 字；只选 skill 没写问题时用 skill 名', async t => {
  const {page} = await launch(t, {model: 'titlefail'});
  await pickByName(page, '季度总结');
  await ask(page, '一二三四五六七八九十十一十二');
  await page.waitForTimeout(300);
  assert.strictEqual(await page.evaluate(() => convTitle().text), '一二三四五六七八九十十一……');
  const [dl] = await Promise.all([page.waitForEvent('download'), page.click('#save')]);
  await page.locator('#chatStatus').getByText(/^已保存为 资料管家-一二三四五六七八九十十一……-\d{8}-\d{4}\.md/).waitFor();
  assert.ok(dl);
  await page.click('#clear');
  assert.strictEqual(await page.evaluate(() => convTitle().text), '');
  assert.strictEqual(await page.evaluate(() => saveStem('20260101-0000')), '资料管家-20260101-0000', '还没有标题时不带');
  await page.selectOption('#skillSel', 'summarize-meeting');
  await page.click('#send');
  await page.waitForFunction(() => !document.querySelector('#send').disabled && document.querySelector('#chat .msg.assistant'));
  assert.strictEqual(await page.evaluate(() => convTitle().text), await page.evaluate(() => currentSkill().label));
});

test('加入前 50 个：可加入的多于 50 个时出现，已选不为空时不可点', async t => {
  const files = {};
  for (let i = 0; i < 55; i++) files['批量/' + String(i).padStart(2, '0') + '.txt'] = 'x';
  const {page} = await launch(t, {files});
  await page.selectOption('#sortBy', 'name_asc');
  await page.locator('#browseList button.dir').first().click();
  await page.getByRole('button', {name: '打开文件夹 批量'}).click();
  await page.locator('#browseTop50').waitFor();
  assert.strictEqual(await page.textContent('#browseTop50'), '加入前 50 个');
  assert.ok(!(await page.isDisabled('#browseTop50')));
  await page.fill('#q', '.txt');
  await page.locator('#addTop50').waitFor();
  await page.locator('#results li', {hasText: '00.txt'}).getByRole('button', {name: /加入/}).click();
  await page.waitForFunction(() => document.querySelector('#addTop50').disabled);
  assert.match(await page.textContent('#addAllHint'), /已选还能加 49 个/);
  await page.locator('#picked li').getByRole('button', {name: /移除/}).click();
  await page.waitForFunction(() => !document.querySelector('#addTop50').disabled);
  await page.click('#addTop50');
  await page.waitForFunction(() => document.querySelectorAll('#picked > li').length === 50);
  await page.setViewportSize({width: 360, height: 800});
  await noHorizontalScroll(page);
});

// ---------------- v2.6 skill 设置：新建、安装

// 用 Python 标准库打一个 .zip：entries 为 {包内路径: 文字}
function makeZip(file, entries) {
  execFileSync(PYTHON, ['-c', 'import json,sys,zipfile\nz=zipfile.ZipFile(sys.argv[1],"w")\n' +
    'for k,v in json.loads(sys.argv[2]).items(): z.writestr(k,v)\nz.close()', file, JSON.stringify(entries)]);
  return file;
}
const PACK_SKILL = {
  'SKILL.md': '---\nname: packer\ndescription: 测试用，打出一个 skill 包\n---\n运行脚本。\n',
  'scripts/pack.py': "import os, zipfile\nz = zipfile.ZipFile(os.path.join(os.environ['ASK_OUT'], 'made.zip'), 'w')\n" +
    "z.writestr('made/SKILL.md', chr(10).join(['---', 'name: made', 'description: AI 写的 skill', '---', '正文', '']))\nz.close()\nprint('已打包')\n"
};

test('skill 设置：新建后选中，同名时确认写明替换的文件夹与文件数，取消则不改', async t => {
  const {page, home} = await launch(t, {skills: {weekly: {'SKILL.md': '---\nname: weekly\ndescription: 旧的\n---\n旧正文\n', 'a.md': '1'}}});
  await page.locator('#skillSel option', {hasText: 'weekly（自建'}).waitFor({state: 'attached'});
  assert.ok(await page.isHidden('#skillPanel'));
  await page.click('#skillManage');
  assert.strictEqual(await page.getAttribute('#skillManage', 'aria-expanded'), 'true');
  await page.click('#skCreate');
  await page.locator('#skStatus.bad', {hasText: '新建失败：还没填名称、用途、说明正文'}).waitFor();
  await page.fill('#skName', 'Bad Name');
  await page.fill('#skDesc', '写周报');
  await page.fill('#skBody', '按周汇总要点。');
  await page.click('#skCreate');
  await page.locator('#skStatus.bad', {hasText: '名称只能用小写字母'}).waitFor();
  await page.fill('#skName', 'report');
  await page.click('#skCreate');
  await page.locator('#skStatus.ok', {hasText: '已新建 skill「report」并选中'}).waitFor();
  assert.strictEqual(await page.inputValue('#skillSel'), 'report');
  assert.match(await page.textContent('#skillHint'), /写周报/);
  assert.ok(fs.existsSync(path.join(home, '.config', 'opencode', 'skills', 'report', 'SKILL.md')));
  // 新建成功后清空输入框，「skill」标签上没有未保存圆点
  assert.deepStrictEqual(await Promise.all(['#skName', '#skDesc', '#skBody'].map(sel => page.inputValue(sel))), ['', '', '']);
  assert.ok(await page.locator('#tab-skill .dirty').isHidden());
  // 同名：取消则不改
  let msg = '';
  page.onDialog = d => { msg = d.message(); d.dismiss(); };
  await page.fill('#skName', 'weekly');
  await page.fill('#skDesc', '写周报');
  await page.fill('#skBody', '按周汇总要点。');
  await page.click('#skCreate');
  await page.locator('#skStatus', {hasText: '没有新建'}).waitFor();
  assert.match(msg, /替换已有的同名自建 skill/);
  assert.match(msg, /weekly/);
  assert.match(msg, /2 个文件会被删除/);
  assert.ok(fs.existsSync(path.join(home, '.config', 'opencode', 'skills', 'weekly', 'a.md')));
  page.onDialog = d => d.accept();
  await page.click('#skCreate');
  await page.locator('#skStatus.ok', {hasText: '替换了原来的同名 skill'}).waitFor();
  assert.ok(!fs.existsSync(path.join(home, '.config', 'opencode', 'skills', 'weekly', 'a.md')));
  assert.strictEqual(await page.inputValue('#skillSel'), 'weekly');
});

test('skill 设置：安装 .zip（键盘打开选文件、带脚本先确认）与文件夹，失败写明原因', async t => {
  const {page, home} = await launch(t);
  await page.locator('#skillSel option', {hasText: '创建 skill（预装，1 个脚本）'}).waitFor({state: 'attached'});
  await page.click('#skillManage');
  const zip = makeZip(path.join(home, 'tool.zip'), {'tool/SKILL.md': '---\nname: tool\ndescription: 带脚本的\n---\n正文\n',
                                                    'tool/scripts/a.py': 'print(1)'});
  let msg = '';
  page.onDialog = d => { msg = d.message(); d.accept(); };
  await page.focus('#skDrop');
  const [chooser] = await Promise.all([page.waitForEvent('filechooser'), page.keyboard.press('Enter')]);
  await chooser.setFiles(zip);
  await page.locator('#skStatus.ok', {hasText: '已安装 skill「tool」并选中'}).waitFor();
  assert.match(msg, /带 1 个脚本/);
  assert.match(msg, /当前用户身份运行/);
  assert.strictEqual(await page.inputValue('#skillSel'), 'tool');
  // 不是 skill 包
  await page.setInputFiles('#skZip', makeZip(path.join(home, 'bad.zip'), {'readme.md': 'x'}));
  await page.locator('#skStatus.bad', {hasText: '安装失败：没有找到 SKILL.md'}).waitFor();
  await page.setInputFiles('#skZip', {name: 'a.txt', mimeType: 'text/plain', buffer: Buffer.from('x')});
  await page.locator('#skStatus.bad', {hasText: 'a.txt 不是 .zip 文件'}).waitFor();
  // 文件夹
  const dir = path.join(home, '下载', 'notes');
  fs.mkdirSync(dir, {recursive: true});
  fs.writeFileSync(path.join(dir, 'SKILL.md'), '---\nname: notes\ndescription: 文件夹里的\n---\n正文\n');
  await page.click('#skInstallDir');
  await page.locator('#skStatus.bad', {hasText: '还没填文件夹'}).waitFor();
  await page.fill('#skDir', dir);
  await page.press('#skDir', 'Enter');
  await page.locator('#skStatus.ok', {hasText: '已安装 skill「notes」并选中'}).waitFor();
  assert.strictEqual(await page.inputValue('#skillSel'), 'notes');
  await page.setViewportSize({width: 360, height: 800});
  await noHorizontalScroll(page);
});

test('回答下方的 skill 包有「安装」，点了才装上并选中', async t => {
  const {page, home} = await launch(t, {model: 'call:run_script:{"script": "scripts/pack.py", "args": []}', skills: {packer: PACK_SKILL}});
  await page.locator('#skillSel option', {hasText: 'packer（自建'}).waitFor({state: 'attached'});
  await page.selectOption('#skillSel', 'packer');
  await page.fill('#ask', '打包');
  await page.click('#send');
  const ins = page.getByRole('button', {name: '安装 made.zip'});
  await ins.waitFor();
  assert.ok(!fs.existsSync(path.join(home, '.config', 'opencode', 'skills', 'made')), '点之前不安装');
  await ins.click();
  await page.locator('#chatStatus.ok', {hasText: '已安装 skill「made」并选中'}).waitFor();
  assert.strictEqual(await page.inputValue('#skillSel'), 'made');
});

// ---------------- 自动收录、容量、保存为 Markdown、配置 OpenCode、帮助页

async function openMemSettings(page) {
  await page.click('#memSettings');
  await page.locator('#kbSetPanel').waitFor();
}
// 概览的「⋯ 更多」：整理记录、没有收录的文件、保存为 Markdown
async function openKbMore(page) {
  await page.click('#kbtab-overview');
  await page.locator('#kbMore').waitFor();
  if (!(await page.locator('#kbMore[open]').count())) await page.click('#kbMore summary');
}

test('管理文件夹里勾选「自动收录到知识库」：后台收录，概览写明收录情况与没有收录的文件、各附下一步，文件标签加收录标记', async t => {
  const {page, docs} = await launch(t, {model: 'memo', files: {'计划.txt': '第一阶段在三月底完成验收。', '坏文件.docx': 'not a zip'}});
  await page.click('#manageFolders');
  const cb = page.getByRole('checkbox', {name: '自动收录到知识库：' + docs});
  assert.strictEqual(await cb.isChecked(), false, '默认不勾');
  await cb.check();
  await page.locator('#folderStatus').getByText(/已打开自动收录/).waitFor();
  await closeFolders(page);
  await page.click('#kbtab-overview');
  await page.locator('#memStats').getByText(/已收录 3 个文件/).waitFor({timeout: 10000});
  await openKbMore(page);
  await page.locator('#ingestStats').getByText(/共 4 个文件：已收录 3 个，没有收录 1 个。/).waitFor({timeout: 10000});
  await page.locator('#ingestReasons').getByText('取不出文字 1 个：另存为可复制文字的 PDF 后再放回').waitFor();
  await page.click('#ingestSkipped');
  await page.locator('#ingestSkippedList').getByText(/坏文件\.docx/).waitFor();
  await page.locator('#ingestSkippedList').getByText(/^取不出文字：/).waitFor();
  await page.click('#ingestRun');
  await page.locator('#ingestStatus:not(:empty)').waitFor();
  // 文件标签：已收录与没收录的都加标记
  await page.click('#kbtab-files');
  await page.fill('#q', '.');
  await page.locator('#results li', {hasText: '计划.txt'}).locator('.tagline-kb', {hasText: '已收录 · '}).waitFor();
  await page.locator('#results li', {hasText: '坏文件.docx'}).locator('.tagline-kb', {hasText: '未收录：取不出文字'}).waitFor();
  await page.fill('#q', '');
  // 取消勾选：有待抽结论的文件时先确认；排队撤出，已收录的保留
  let msg = '';
  page.onDialog = d => { msg = d.message(); d.accept(); };
  await openFolders(page);
  await cb.uncheck();
  await page.locator('#folderStatus').getByText(/已关闭自动收录：不再收录这个文件夹的文件，排队中的 \d+ 个文件已撤出，已收录的保留。/).waitFor();
  assert.match(msg, /知识库里有 \d+ 个文件待抽结论，会撤出排队，已收录的保留。/);
  await closeFolders(page);
  await page.click('#kbtab-overview');
  await page.locator('#memStats').getByText(/其中 \d+ 个不在自动收录范围内/).waitFor();
  await openKbMore(page);
  await page.locator('#ingestStats').getByText(/还没有勾选「自动收录到知识库」的文件夹/).waitFor();
  assert.strictEqual(await page.isDisabled('#ingestRun'), true);
  // 知识库关闭时勾选框不可用
  await openMemSettings(page);
  await page.uncheck('#memoryOn');
  await page.locator('#memStatus').getByText(/已关闭知识库/).waitFor();
  await page.click('#kbtab-files');
  await openFolders(page);
  assert.strictEqual(await cb.isDisabled(), true);
});

test('容量上限：范围外不保存并写明原因，保存后显示', async t => {
  const {page} = await launch(t);
  await openMemSettings(page);
  assert.strictEqual(await page.inputValue('#memMb'), '1024');
  await page.fill('#memMb', '10');
  await page.click('#saveMemMb');
  await page.locator('#memStatus.warn').getByText(/容量上限须为 50 到 102400 的整数/).waitFor();
  await page.fill('#memMb', '2048');
  await page.press('#memMb', 'Enter');
  await page.locator('#memStatus.ok').getByText('容量上限已改为 2,048 MB。').waitFor();
  await page.locator('#memCap').getByText(/上限 2\.00 GB/).waitFor();
});

test('从文件抽出的结论整理成主题；保存为 Markdown 得到 zip', async t => {
  const {page, docs} = await launch(t, {model: 'memo', files: {'计划.txt': '第一阶段在三月底完成验收。\n\n预算总额为一百万元。'}});
  assert.strictEqual(await page.isDisabled('#memSaveMd'), true, '知识库为空时不可点');
  await page.click('#manageFolders');
  await page.getByRole('checkbox', {name: '自动收录到知识库：' + docs}).check();
  await page.locator('#folderStatus').getByText(/已打开自动收录/).waitFor();
  await closeFolders(page);
  await page.click('#kbtab-overview');
  await page.locator('#memStats').getByText(/已收录 3 个文件/).waitFor({timeout: 10000});
  await page.click('#memUpdate');
  await page.locator('#memUpdStatus').getByText(/知识库已整理：从文件抽出结论 \d+ 条、新建主题 1 个/).waitFor({timeout: 15000});
  await page.locator('#kbRecentList').getByText('验收安排').waitFor();
  await openKbMore(page);
  await page.locator('#memSaveMd:enabled').waitFor();
  const [dl] = await Promise.all([page.waitForEvent('download'), page.click('#memSaveMd')]);
  await page.locator('#kbMdStatus.ok').getByText(/^已保存为 知识库-\d{8}-\d{4}\.zip（浏览器的下载文件夹）/).waitFor();
  const out = path.join(os.tmpdir(), 'alf-export-' + process.pid + '.zip');
  await dl.saveAs(out);
  const list = execFileSync(PYTHON, ['-c', 'import sys,zipfile;print("\\n".join(zipfile.ZipFile(sys.argv[1]).namelist()))', out]).toString();
  fs.rmSync(out, {force: true});
  assert.match(list, /索引\.md/);
  assert.match(list, /主题\/验收安排\.md/);
  await page.locator('#kbMdStatus').getByText(/Obsidian/).waitFor();
});

test('配置 OpenCode：选服务商、填 AK，测试通过才写入，可回滚', async t => {
  const conf = '{\n  // 说明\n  "provider": {"corp": {"options": {"baseURL": "BASE", "apiKey": "old"}, "models": {"ok": {}}}},\n  "model": "corp/ok"\n}\n';
  const {page, home} = await launch(t);
  // 服务已启动：在它读取的 OpenCode 目录里放好配置（每次打开「模型设置」时重新读取）
  const file = path.join(home, '.config', 'opencode', 'opencode.jsonc');
  fs.mkdirSync(path.dirname(file), {recursive: true});
  fs.writeFileSync(file, conf.replace('BASE', modelBase));
  await page.click('#modelSettings');
  await page.locator('#ocBox').waitFor();
  await page.locator('#ocPath').getByText(file).waitFor();
  assert.strictEqual(await page.isHidden('#ocRollback'), true);
  await page.click('#ocSave');
  await page.locator('#ocStatus.warn').getByText('请先填写新的 AK。').waitFor();
  await page.fill('#ocKey', 'NEW-KEY');
  await page.click('#ocSave');
  await page.locator('#ocStatus.ok').getByText(/已写入：「corp」的 AK 已替换，测试通过/).waitFor({timeout: 10000});
  assert.strictEqual(await page.inputValue('#ocKey'), '', '写入后清空输入框');
  assert.match(fs.readFileSync(file, 'utf8'), /\/\/ 说明[\s\S]*"apiKey": "NEW-KEY"/);
  await page.locator('#model option', {hasText: 'ok（OpenCode：corp）'}).waitFor({state: 'attached'});
  let asked = '';
  page.onDialog = d => { asked = d.message(); d.accept(); };
  await page.click('#ocRollback');
  await page.locator('#ocStatus.ok').getByText(/已回滚/).waitFor();
  assert.match(asked, /回滚后这份备份删除/);
  assert.match(fs.readFileSync(file, 'utf8'), /"apiKey": "old"/);
});

test('本机没有 OpenCode 配置时只提示填自定义接口', async t => {
  const {page} = await launch(t);
  await page.click('#modelSettings');
  await page.locator('#ocStatus.warn').getByText(/本机没有 OpenCode 配置.*自定义接口/).waitFor();
  assert.strictEqual(await page.isHidden('#ocBox'), true);
});

test('页头「帮助」在新标签打开帮助页：目录可跳转、可搜索，360px 无横向滚动', async t => {
  const {page} = await launch(t, {width: 360});
  await noHorizontalScroll(page);
  const link = page.locator('#help');
  assert.strictEqual(await link.getAttribute('target'), '_blank');
  const [help] = await Promise.all([page.context().waitForEvent('page'), link.click()]);
  await help.waitForLoadState();
  assert.strictEqual(await help.title(), '资料管家 帮助');
  assert.match(await help.textContent('#ver'), /资料管家 帮助 v\d+\.\d+/);
  await noHorizontalScroll(help);
  await help.click('#toc a[href="#auto"]');
  assert.strictEqual(new URL(help.url()).hash, '#auto');
  await help.fill('#q', 'Obsidian');
  await help.locator('#searchStatus.ok').getByText(/找到 \d+ 节含「Obsidian」/).waitFor();
  assert.strictEqual(await help.isVisible('#topics'), true);
  assert.strictEqual(await help.isHidden('#find'), true);
  await help.fill('#q', '不会出现的词语');
  await help.locator('#searchStatus.warn').getByText(/没有找到/).waitFor();
  await help.click('#clearQ');
  assert.strictEqual(await help.isVisible('#find'), true);
});

test('帮助页「原理与提示词」：目录可跳转，提示词全文展开后 360px 无横向滚动', async t => {
  const {page} = await launch(t, {width: 360});
  const [help] = await Promise.all([page.context().waitForEvent('page'), page.locator('#help').click()]);
  await help.waitForLoadState();
  await help.click('#toc a[href="#principles"]');
  assert.strictEqual(new URL(help.url()).hash, '#principles');
  assert.ok(await help.locator('#principles pre[data-prompt]').count() >= 18);
  await help.locator('#principles summary').evaluateAll(els => els.forEach(e => e.click()));
  assert.match(await help.textContent('pre[data-prompt="EXTRACT_PROMPT"]'), /只输出一个 JSON 对象/);
  assert.ok(await help.isVisible('pre[data-prompt="UPDATE_PROMPT"]'));
  await noHorizontalScroll(help);
});

test('概览的整理状态：有按旧规则抽的文件时写明几个文件要按新规则重新抽取；没有可用模型时写整理已暂停', async t => {
  const {page} = await launch(t);
  await page.click('#kbtab-overview');
  const base = {files: 3, facts: 5, topics: 1, queued: 2, extract: 2, bytes: 1000, max_bytes: 1 << 30};
  await page.evaluate(st => showMemStats(st), Object.assign({reextract: 2}, base));
  assert.match(await page.textContent('#kbTidyText'), /^整理：2 项等空闲时进行（10 分钟没有提问后），不用等。其中 2 个文件按新规则重新抽取。$/);
  await page.evaluate(st => showMemStats(st), Object.assign({reextract: 0}, base));
  assert.doesNotMatch(await page.textContent('#kbTidyText'), /按新规则/);
  const {page: p2} = await launch(t, {model: null});
  await p2.click('#kbtab-overview');
  await p2.evaluate(st => showMemStats(st), Object.assign({reextract: 0}, base));
  assert.strictEqual(await p2.textContent('#kbTidyText'), '整理已暂停：没有可用的模型。收录照常进行；配好模型后自动继续。');
});

test('知识库设置：「提问前先让模型看主题目录」默认勾选，改了即保存并提示，刷新后照旧', async t => {
  const {page} = await launch(t);
  await openMemSettings(page);
  const box = page.getByRole('checkbox', {name: '提问前先让模型看主题目录'});
  assert.ok(await box.isChecked());
  await box.uncheck();
  await page.locator('#tocStatus', {hasText: '已关闭：提问时只按关键字检索知识库。'}).waitFor();
  assert.match(await page.getAttribute('#tocStatus', 'class'), /\bok\b/);
  await page.reload();
  await openMemSettings(page);
  assert.ok(!(await page.getByRole('checkbox', {name: '提问前先让模型看主题目录'}).isChecked()));
  await page.getByRole('checkbox', {name: '提问前先让模型看主题目录'}).check();
  await page.locator('#tocStatus', {hasText: '已打开'}).waitFor();
});

test('知识库设置与配置 OpenCode 在 360px 下没有横向滚动', async t => {
  const {page} = await launch(t, {width: 360});
  await openFolders(page);
  await noHorizontalScroll(page);
  await closeFolders(page);
  await openMemSettings(page);
  await page.click('#modelSettings');
  await noHorizontalScroll(page);
});

test('关系图：没有主题时只提示先整理；有主题时显示前几个主题，点节点展开、详情栏三个按钮，列表可用键盘，Esc 关闭并还焦点', async t => {
  const {page} = await launch(t, {model: 'memo', files: PLAN, width: 1280});
  await page.click('#kbtab-overview');
  await page.click('#kbGraph');
  await page.locator('#graphStatus', {hasText: '还没有主题：整理后才有主题'}).waitFor();
  assert.strictEqual(await page.locator('#graphSvg g.gn').count(), 0);
  await page.keyboard.press('Escape');
  assert.strictEqual(await page.evaluate(() => document.activeElement.id), 'kbGraph');
  await page.click('#kbtab-files');
  await pickByName(page, '项目计划');
  await ask(page, '第一阶段什么时候验收');
  await page.click('#kbtab-overview');
  await page.click('#memUpdate');
  await page.locator('#memUpdStatus', {hasText: '知识库已整理'}).waitFor();
  await page.click('#kbGraph');
  await page.locator('#graphSvg g.gn.topic').waitFor();
  await page.locator('#graphSvg g.gn.topic .shape').first().click();
  await page.locator('#graphSvg g.gn.fact').first().waitFor();
  await page.locator('#graphSvg g.gn.path').first().waitFor();
  await page.locator('#graphDetail', {hasText: '主题：验收安排'}).waitFor();
  for (const name of ['展开', '在主题与结论里查看', '把出处文件加入已选']) assert.ok(await page.locator('#graphDetail').getByRole('button', {name}).isVisible());
  // 列表：方向键逐层，回车看详情
  await page.locator('#graphTree [role=treeitem]').first().focus();
  assert.strictEqual(await page.getAttribute('#graphTree [role=treeitem] >> nth=0', 'aria-expanded'), 'true');
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('Enter');
  await page.locator('#graphDetail', {hasText: '结论：'}).waitFor();
  // 搜索后以结果为中心
  await page.fill('#graphQ', '验收');
  await page.locator('#graphHits li').first().waitFor();
  await page.locator('#graphHits li button').first().click();
  await page.locator('#graphDetail:not(:empty)').waitFor();
  // 在主题与结论里查看：关掉关系图，跳到对应条目
  await page.locator('#graphTree [role=treeitem] > .gi').first().click();
  await page.locator('#graphDetail', {hasText: '主题：'}).waitFor();
  await page.locator('#graphDetail').getByRole('button', {name: '在主题与结论里查看'}).click();
  assert.ok(!await page.$eval('#graphDlg', d => d.open));
  await page.locator('#kbJumpText', {hasText: '关系图里选中的主题'}).waitFor();
});

test('关系图在 360px 下只显示列表与详情，没有横向滚动；暗色下节点颜色可辨', async t => {
  const {page} = await launch(t, {model: 'memo', files: PLAN, width: 360});
  await pickByName(page, '项目计划');
  await ask(page, '第一阶段什么时候验收');
  await page.click('#foldFind');
  await page.click('#kbtab-overview');
  await page.click('#memUpdate');
  await page.locator('#memUpdStatus', {hasText: '知识库已整理'}).waitFor();
  await page.click('#kbGraph');
  await page.locator('#graphTree [role=treeitem]').first().waitFor();
  assert.ok(await page.locator('#graphSvg').isHidden());
  await page.locator('#graphTree [role=treeitem] > .gi').first().click();
  await page.locator('#graphDetail', {hasText: '主题：'}).waitFor();
  await noHorizontalScroll(page);
  await page.emulateMedia({colorScheme: 'dark'});
  const c = await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--g-topic').trim());
  assert.strictEqual(c, '#4493f8');
});

test('关系图：亮色下节点与连线对比度至少 3:1，颜色由样式表给出；减少动态效果时一次排好不移动', async t => {
  const {page} = await launch(t, {model: 'memo', files: PLAN, width: 1280});
  await page.emulateMedia({reducedMotion: 'reduce', colorScheme: 'light'});
  await pickByName(page, '项目计划');
  await ask(page, '第一阶段什么时候验收');
  await page.click('#kbtab-overview');
  await page.click('#memUpdate');
  await page.locator('#memUpdStatus', {hasText: '知识库已整理'}).waitFor();
  await page.click('#kbGraph');
  await page.locator('#graphSvg g.gn.topic .shape').first().click();
  await page.locator('#graphSvg g.gn.fact').first().waitFor();
  await page.locator('#graphStatus', {hasText: '已选中主题'}).waitFor();
  const ratios = await page.evaluate(() => {
    const lum = h => { const c = h.match(/\w\w/g).map(x => parseInt(x, 16) / 255).map(v => v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4); return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]; };
    const s = getComputedStyle(document.documentElement), bg = lum(s.getPropertyValue('--bg').trim().slice(1));
    return ['--g-topic', '--g-fact', '--g-file', '--g-edge'].map(k => { const l = lum(s.getPropertyValue(k).trim().slice(1)); return (Math.max(l, bg) + 0.05) / (Math.min(l, bg) + 0.05); });
  });
  assert.ok(ratios.every(r => r >= 3), JSON.stringify(ratios));
  assert.strictEqual(await page.locator('#graphSvg .shape[fill]').count(), 0);
  assert.notStrictEqual(await page.$eval('#graphSvg g.gn.fact .shape', e => getComputedStyle(e).fill), 'rgb(0, 0, 0)');
  const before = await page.$$eval('#graphSvg g.gn', es => es.map(e => e.getAttribute('transform')).join());
  await page.waitForTimeout(300);
  assert.strictEqual(await page.$$eval('#graphSvg g.gn', es => es.map(e => e.getAttribute('transform')).join()), before);
  assert.strictEqual(await page.getAttribute('#graphDetail', 'aria-live'), null);
});

// ---- v2.29：输入框宽度、悬停说明、术语说明、自动刷新、格式不自动收录

test('每批与容量等数字框填最大值时完整显示，360px 不横向滚动', async t => {
  for (const width of [1280, 360]) {
    const {page} = await launch(t, {width});
    await page.click('#memSettings');
    await page.click('#modelSettings');
    const r = await page.evaluate(() => [['xbFiles', '100'], ['xbChars', '200000'], ['memMb', '102400'], ['limit', '2000000']].map(([id, v]) => {
      const e = document.getElementById(id);
      e.value = v;
      const cs = getComputedStyle(e), c = document.createElement('canvas').getContext('2d');
      c.font = cs.fontSize + ' ' + cs.fontFamily;
      // 数字调节按钮约占 15px
      return [id, Math.ceil(c.measureText(v).width), e.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight) - 15];
    }));
    for (const [id, need, have] of r) assert.ok(need <= have, id + ' 需要 ' + need + 'px，只有 ' + have + 'px');
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
  }
});

test('页面上每个按钮都有悬停说明', async t => {
  const {page} = await launch(t, {files: {'原型/页面.html': '<p>丙</p>'}});
  await openFolders(page);
  await closeFolders(page);
  await page.fill('#q', '季度');
  await page.locator('#results li').first().waitFor();
  await pickByName(page, '季度总结');
  await page.click('#kbtab-topics');
  await page.click('#kbtab-overview');
  const missing = await page.evaluate(() => Array.from(document.querySelectorAll('button')).filter(b => !b.title.trim())
    .map(b => b.id || b.textContent.trim() || b.outerHTML.slice(0, 80)));
  assert.deepStrictEqual(missing, []);
  assert.match(await page.getAttribute('#ingestRun', 'title'), /不用模型/);
  assert.match(await page.getAttribute('#memUpdate', 'title'), /模型/);
});

test('概览可展开「收录、整理是什么」，链到帮助', async t => {
  const {page} = await launch(t, {kbTab: false});
  await page.click('#kbtab-overview');
  await page.click('#kbTerms summary');
  assert.match(await page.textContent('#kbTerms'), /收录：.*不用模型[\s\S]*整理：由所选模型/);
  assert.strictEqual(await page.getAttribute('#kbTerms a', 'href'), '/help.html#kb');
});

test('没有收录的文件：网页、代码归为「格式不自动收录」', async t => {
  const {page} = await launch(t, {kbTab: false});
  await page.click('#kbtab-overview');
  await page.evaluate(() => showIngest(Object.assign({}, ingest, {running: false, paused: false,
    summary: {total: 2, done: 1, waiting: 0, skipped_total: 1, skipped: [{path: '/文件/原型/页面.html', state: 'format', reason: ''}]}})));
  assert.match(await page.textContent('#ingestReasons'), /格式不自动收录 1 个：自动收录只收 Word、Excel、PPT、WPS、PDF、邮件、txt 和 Markdown/);
});

test('点「立即收录」后收录结束不停在「正在收录」', async t => {
  const {page} = await launch(t, {kbTab: false});
  await page.click('#kbtab-overview');
  await page.evaluate(() => {
    say('ingestStatus', '正在收录：查找要收录的文件……', 'busy');
    showIngest(Object.assign({}, ingest, {running: true, stage: '正在收录第 1 / 2 个文件'}));
  });
  assert.strictEqual(await page.textContent('#ingestStatus'), '正在收录：正在收录第 1 / 2 个文件……');
  await page.evaluate(() => showIngest(Object.assign({}, ingest, {running: false, result: ingest.result})));
  assert.strictEqual(await page.textContent('#ingestStatus'), '自动收录：没有新增或改过的文件。');
});

test('整理进行中按 5 秒排刷新，每做完一批刷新数字', async t => {
  const {page} = await launch(t, {kbTab: false});
  await page.click('#kbtab-overview');
  // 页面自己的进度轮询会拿回真实的「没在整理」，这里不让它覆盖
  await page.route(u => u.pathname === '/api/memory-update', r => r.fulfill({json: {running: true, mode: 'auto', done: 2, total: 5, auto: true, history: [], log: []}}));
  const hits = [];
  page.on('request', q => { const u = new URL(q.url()); if (u.pathname === '/api/memory') hits.push(u.search); });
  await page.evaluate(() => showMemUpdate({running: true, mode: 'auto', done: 1, total: 5, auto: true, history: [], log: []}));
  assert.ok(await page.evaluate(() => kbTimer && kbDue - Date.now() <= KB_BUSY_MS));
  const before = hits.length;
  await page.evaluate(() => showMemUpdate({running: true, mode: 'auto', done: 2, total: 5, auto: true, history: [], log: []}));
  await page.waitForTimeout(300);
  assert.ok(hits.length > before, '做完一批后应刷新数字');
});

test('「主题与结论」自动刷新保留搜索与展开，刚操作过或页面在后台时不刷', async t => {
  const {page} = await launch(t, {kbTab: false});
  const topic = {id: 1, title: '主题甲', points: [{text: '要点一', quote: '原句一', path: '/文件/甲.txt', stale: false}], related: []};
  const fact = (id, text) => ({id, path: '/文件/甲.txt', conclusion: text, quote: '原句', count: 1, last: id});
  let facts = [fact(1, '旧结论')];
  const urls = [];
  await page.route(u => u.pathname === '/api/memory', async route => {
    const u = new URL(route.request().url());
    if (route.request().method() !== 'GET' || u.searchParams.get('limit') === '5') return route.fallback();
    urls.push(u.search);
    const res = await route.fetch(), r = await res.json();
    await route.fulfill({json: Object.assign(r, {topics: [topic], facts, topics_total: 1, facts_total: facts.length})});
  });
  await page.click('#kbtab-topics');
  await page.locator('#memFacts').getByText('旧结论').waitFor();
  await page.fill('#memQ', '甲');
  await page.waitForFunction(() => kbList.q === '甲');
  await page.locator('#memTopics .rowbtn').click();
  assert.strictEqual(await page.getAttribute('#memTopics .rowbtn', 'aria-expanded'), 'true');
  // 刚点过：不刷
  facts = [fact(2, '新结论乙'), fact(1, '旧结论')];
  await page.evaluate(() => quietKbList());
  assert.strictEqual(await page.locator('#memFacts').getByText('新结论乙').count(), 0);
  // 过了 10 秒（这里直接改记下的时间）：刷新，保留搜索词与展开
  await page.evaluate(() => { kbHands.topics = 0; document.activeElement.blur(); return quietKbList(); });
  await page.locator('#memFacts').getByText('新结论乙').waitFor();
  assert.strictEqual(await page.inputValue('#memQ'), '甲');
  assert.strictEqual(await page.getAttribute('#memTopics .rowbtn', 'aria-expanded'), 'true');
  const last = new URLSearchParams(urls[urls.length - 1]);
  assert.strictEqual(last.get('q'), '甲');
  assert.ok(+last.get('limit') >= 50);
  // 页面在后台：定时到了也不发请求
  const n = urls.length;
  await page.evaluate(() => { Object.defineProperty(document, 'hidden', {configurable: true, get: () => true}); kbAuto(); });
  await page.waitForTimeout(300);
  assert.strictEqual(urls.length, n);
  assert.ok(await page.evaluate(() => !kbTimer));
});

test('「文件」标签在收录进行中与收录结束时刷新收录标记', async t => {
  const {page} = await launch(t);
  await page.locator('#browseList li').first().waitFor();
  const hits = [];
  page.on('request', q => { const u = new URL(q.url()); if (u.pathname === '/api/browse' || u.pathname === '/api/search') hits.push(u.pathname); });
  // 空闲时不刷文件列表
  await page.evaluate(() => kbAuto());
  await page.waitForTimeout(300);
  assert.strictEqual(hits.length, 0);
  // 收录进行中：定时刷新；收录结束：立即刷新
  await page.evaluate(() => { ingest = Object.assign({}, ingest, {running: true}); kbHands.files = 0; kbAuto(); });
  await page.waitForFunction(() => !findQuiet);
  await page.waitForTimeout(200);
  assert.ok(hits.length >= 1, '收录进行中应刷新');
  await page.waitForTimeout(300);
  const n = hits.length;
  await page.evaluate(() => { ingest = Object.assign({}, ingest, {running: true}); showIngest(Object.assign({}, ingest, {running: false})); });
  await page.waitForTimeout(300);
  assert.ok(hits.length > n, '收录结束应刷新');
});

test('保存位置：退回家目录时常驻写明原因，「改存到安装目录」确认后搬进安装目录的「数据」', async t => {
  const {page, tool} = await launch(t, {ownTool: true, kbTab: false});
  const own = path.join(tool, '数据');
  // 模拟启动时没能用上安装目录：服务端改回家目录并记下原因
  await patchApi(page, '/api/state', r => Object.assign(r, {data: Object.assign({}, r.data,
    {home: true, custom: false, path: '/家目录/.local/share/ask-local-files', notice: '安装目录不可写（Permission denied），知识库与设置仍存在家目录；', tool: own})}));
  await page.reload();
  await page.click('#memSettings');
  await page.locator('#dataWhy', {hasText: '安装目录不可写（Permission denied）'}).waitFor();
  assert.match(await page.textContent('#dataWhy'), new RegExp('安装目录下的「数据」：' + own.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
  assert.ok(await page.isVisible('#dataToTool'));
  // 实际已在安装目录：服务端写明这已是当前位置
  await page.click('#dataToTool');
  await page.locator('#dataStatus.bad', {hasText: '这已是当前的保存位置'}).waitFor();
});

test('保存位置：「改存到安装目录」先建好「数据」，把当前数据搬过去', async t => {
  const {page, home, tool} = await launch(t, {ownTool: true, kbTab: false});
  // 先改到别处，再改回安装目录
  const other = path.join(home, '数据盘');
  fs.mkdirSync(other);
  page.onDialog = d => d.accept();
  await page.click('#memSettings');
  await page.fill('#dataDir', other);
  await page.click('#setDataDir');
  await page.locator('#dataStatus.ok', {hasText: '已把数据移到'}).waitFor();
  fs.rmSync(path.join(tool, '数据'), {recursive: true, force: true});
  let msg = '';
  page.onDialog = d => { msg = d.message(); d.accept(); };
  await page.evaluate(() => changeDataDir({tool: true}, $('dataToTool')));
  await page.locator('#dataStatus.ok', {hasText: '已把数据移到'}).waitFor();
  assert.match(msg, /将移动 \d 个文件：/);
  assert.ok(fs.existsSync(path.join(tool, '数据', 'token')));
  assert.match(await page.textContent('#dataDirNow'), /^当前：默认（安装目录下的「数据」）：/);
  assert.ok(await page.isHidden('#dataToTool'));
});

test('保存位置：填程序目录本身按其中的「数据」算，填程序目录里的其他文件夹不保存并写明原因', async t => {
  const {page, tool} = await launch(t, {ownTool: true, kbTab: false});
  await page.click('#memSettings');
  await page.fill('#dataDir', tool);
  await page.click('#setDataDir');
  await page.locator('#dataStatus.bad', {hasText: '这已是当前的保存位置'}).waitFor();
  fs.mkdirSync(path.join(tool, '库'));
  await page.fill('#dataDir', path.join(tool, '库'));
  await page.click('#setDataDir');
  await page.locator('#dataStatus.bad', {hasText: '升级时不会带到新版'}).waitFor();
  assert.match(await page.textContent('#dataStatus'), /^更改失败：程序目录里的文件夹升级时不会带到新版/);
});

test('保存位置：知识库是空的而上一版里有时，提示并给「从上一版取回」，确认框写明复制哪些文件，取回后提示', async t => {
  const {page} = await launch(t, {kbTab: false});
  const prev = {path: '/某处/.ask-local-files.上一版/数据', files: 12,
    items: [{name: 'memory.sqlite', label: '知识库', bytes: 2048}, {name: 'token', label: '网址令牌', bytes: 33}]};
  await patchApi(page, '/api/state', r => Object.assign(r, {data: Object.assign({}, r.data, {previous: prev})}));
  let calls = 0;
  await page.route(u => u.pathname === '/api/data-restore', async route => {
    calls++;
    const body = JSON.parse(route.request().postData() || '{}');
    const data = {path: '/某处/数据', tool: '/某处/数据', custom: false, home: false, ok: true, notice: '', previous: null};
    await route.fulfill({json: body.confirm ? {ok: true, items: ['memory.sqlite', 'token'], data, url: page.url(), url_changed: false}
      : {ok: true, plan: Object.assign({to: '/某处/数据'}, prev)}});
  });
  await page.reload();
  await page.locator('#askStatus', {hasText: '上一版里的知识库有 12 个文件'}).waitFor();
  await page.locator('#askStatus').getByRole('button', {name: '打开知识库设置里的「保存位置」'}).click();
  await page.locator('#dataPrev', {hasText: '可以取回'}).waitFor();
  assert.ok(await isFocused(page, '#dataRestore'));
  let msg = '';
  page.onDialog = d => { msg = d.message(); d.accept(); };
  await page.click('#dataRestore');
  await page.locator('#dataStatus.ok', {hasText: '已从上一版取回'}).waitFor();
  assert.match(msg, /将复制 2 个文件：知识库（memory\.sqlite，2\.0 KB）、网址令牌（token，33 B）/);
  assert.match(msg, /上一版里的文件不动/);
  assert.match(msg, /取回前/);
  assert.strictEqual(calls, 2);
  assert.ok(await page.isHidden('#dataRestore'));
});

test('管理文件夹：大窗口打开时页面其他部分不能操作，Esc 与「完成」关闭并把焦点还给按钮，添加后文件列表刷新', async t => {
  const {page, home} = await launch(t, {width: 1280, homeDirs: ['另一处']});
  fs.writeFileSync(path.join(home, '另一处', '新文件.txt'), '丁');
  await page.click('#kbtab-files');
  await openFolders(page);
  assert.ok(await page.$eval('#folderDlg', d => d.matches(':modal')));
  const dlg = await box(page, '#folderDlg');
  assert.ok(dlg.width >= 900 && dlg.height >= 760, `大窗口：${dlg.width}x${dlg.height}`);
  // 背景被遮住：在搜索框位置点到的是对话框或遮罩，不是搜索框
  const q = await box(page, '#q');
  assert.ok(await page.evaluate(([x, y]) => document.elementFromPoint(x, y) !== document.querySelector('#q'), [q.x + 5, q.y + 5]));
  await page.keyboard.press('Escape');
  await page.locator('#folderDlg[open]').waitFor({state: 'detached'});
  assert.ok(await isFocused(page, '#manageFolders'));
  await openFolders(page);
  await page.fill('#folder', path.join(home, '另一处'));
  await page.click('#addFolder');
  await page.locator('#folderStatus').getByText(/已添加/).waitFor();
  assert.strictEqual(await page.locator('#folders li').count(), 2);
  await closeFolders(page);
  assert.ok(await isFocused(page, '#manageFolders'));
  await page.locator('#browseList', {hasText: '另一处'}).waitFor();
});

test('管理文件夹：360px 下占满屏幕、没有横向滚动', async t => {
  const {page} = await launch(t, {width: 360});
  await page.click('#kbtab-files');
  await openFolders(page);
  const dlg = await box(page, '#folderDlg');
  assert.ok(dlg.width >= 359 && dlg.x <= 1, `窄屏占满：${dlg.x} ${dlg.width}`);
  await noHorizontalScroll(page);
  const sw = await page.$eval('#folderDlg', d => d.scrollWidth - d.clientWidth);
  assert.ok(sw <= 1, '对话框内没有横向滚动');
});

test('没有文件夹时：进「文件」不自动弹出，空状态只说一次并给「添加文件夹」，点了打开大窗口并聚焦路径框', async t => {
  const {page} = await launch(t, {folders: false});
  await page.click('#kbtab-files');
  await page.locator('#browseList .empty-folders').waitFor();
  assert.ok(await page.isHidden('#folderDlg'), '不自动弹出');
  const text = await page.$eval('#kbFiles', e => e.innerText);
  assert.strictEqual((text.match(/还没有添加文件夹/g) || []).length, 1, text);
  await page.locator('#browseList').getByRole('button', {name: '添加文件夹'}).click();
  await page.locator('#folderDlg[open]').waitFor();
  await page.waitForFunction(() => document.activeElement && document.activeElement.id === 'folder');
});
