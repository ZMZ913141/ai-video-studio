/* 小白漫游 2026-10-30
   不是测「能不能跑通」，是测「一个什么都不懂的人点下去会怎样」。

   三条判据：
   ① 崩：console.error / jsdomError / 服务端 500
   ② 哑：点了之后页面 ANY 变化都没有 —— 小白会以为「按新建没反应」
   ③ 空：弹层开了但里面什么都没有 —— 比崩更让人懵

   写操作一律 confirm=false 挡住（不想测出脏数据），双击保护靠间隔。
*/
const fs = require('fs');
const { JSDOM, VirtualConsole } = require('jsdom');

const BASE = process.env.UI_BASE || 'http://127.0.0.1:8791/';
const SCENARIO = process.env.SCENARIO || 'all';   // all | noip | empty | offline

const errs = [];
const warns = [];
const http5xx = [];
const vc = new VirtualConsole();
vc.on('jsdomError', e => errs.push('jsdomError: ' + (e.message || String(e))));
vc.on('error', (...a) => errs.push('console.error: ' + a.map(String).join(' ')));
vc.on('warn', (...a) => warns.push('console.warn: ' + a.map(String).join(' ')));

const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  const html = fs.readFileSync('ui/index.html', 'utf8');
  const dom = new JSDOM(html, {
    url: BASE, runScripts: 'dangerously', resources: 'usable',
    pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(win) {
      const realFetch = (u, o) => {
        const url = String(u).startsWith('http') ? String(u) : new URL(u, BASE).href;
        return fetch(url, o).then(r => {
          if (r.status >= 500) http5xx.push(r.status + ' ' + url.replace(BASE, '/'));
          return r;
        });
      };
      win.fetch = realFetch;
      win.navigator.clipboard = { writeText: async () => {} };
      win.confirm = () => false;             // 拦所有二次确认
      win.prompt = () => null;               // 拦所有输入框
      win.alert = () => {};
      win.addEventListener('unhandledrejection', ev =>
        errs.push('unhandledrejection: ' + (ev.reason && ev.reason.message || ev.reason)));
      win.open = () => null;
    },
  });
  const win = dom.window;
  const doc = win.document;
  await sleep(3800);

  const hasDM = () => !!doc.querySelector('#modal.show');

  // ---------- 收集可点元素 ----------
  // 只用「视觉上像个能点的东西」来判定，跟小白的判断一致：看得见、鼠标变了形
  // jsdom 不做布局：offsetParent 恒为 null，必须自己判定「人眼看得见吗」。
  // 页面用了三种隐藏：标签组/右栏 tab 用 inline display:none，面板折叠用 class。
  function visible(el) {
    if (el.id === 'mclose' && !hasDM()) return false;
    for (let n = el; n && n !== doc.body; n = n.parentElement)
      if (n.style && n.style.display === 'none') return false;
    const collapsed = el.closest('.panel.collapsed');
    if (collapsed && !el.closest('.panel-head')) return false;   // 折叠面板里只有标题栏能点
    if (el.closest('#modal') && !hasDM()) return false;
    if (el.hasAttribute('disabled')) return false;
    return true;
  }

  const CLICKABLE = [
    'button', '.btn', '.tab', '.chip', '.stage', '.mat', '.svc', '.tree-item',
    '[data-file]', '[data-copy]', '[data-redo]', '[data-svc]', '[data-prefer]',
    '[data-probe]', '[data-stage]', '[data-open]', '[data-act]', '[data-bgmpick]',
    'summary', '.ico-btn', '.opt', 'input[type=radio]', 'input[type=checkbox]',
  ].join(',');

  const snapshot = () => {
    const m = doc.querySelector('#modalBox');
    return [
      doc.querySelector('#hero')?.textContent || '',
      doc.querySelector('#overviewBox')?.textContent || '',
      doc.querySelector('#filesBox')?.textContent || '',
      win.scrollY,
      m ? m.innerHTML.length : -1,
      hasDM(),
      doc.querySelectorAll('#toasts .toast').length,
      win.location.hash,
    ].join('|');
  };

  const seen = new Set();
  const results = { clicked: 0, dead: [], broke: [], empty_modal: [] };

  async function clickOne(el, labelOf) {
    const before = snapshot();
    let threw = null;
    const n0 = errs.length;
    try {
      el.dispatchEvent(new win.MouseEvent('click', { bubbles: true, cancelable: true }));
    } catch (e) { threw = e.message; }
    await sleep(1100);            // 等一轮异步
    if (hasDM()) {
      const box = doc.querySelector('#modalBox');
      // 弹层开了但没内容 = 小白最懵的情况
      if (box && box.textContent.trim().length < 8 && box.querySelectorAll('input,button,textarea,.kv').length === 0) {
        results.empty_modal.push(labelOf());
      }
      doc.querySelector('#mclose')?.dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
      await sleep(180);
    }
    const newErrs = errs.slice(n0);
    if (threw) { results.broke.push(labelOf() + ' → ' + threw); return; }
    if (newErrs.length) { results.broke.push(labelOf() + ' → ' + newErrs[0].slice(0, 160)); return; }
    if (snapshot() === before) results.dead.push(labelOf());
  }

  const labelOf = el => {
    const id = el.id ? '#' + el.id : '';
    const cls = (el.className && typeof el.className === 'string')
      ? '.' + el.className.trim().split(/\s+/).slice(0, 2).join('.') : '';
    const txt = (el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 22);
    return `${el.tagName.toLowerCase()}${id}${cls} "${txt}"`;
  };

  // 每次点击后 DOM 可能整块重绘（innerHTML=），手里旧引用立刻失效 ——
  // 所以写成「每次重新查询，挑第一个还没点过的」，而不是先抓一排再逐个点。
  async function sweep(scopeLabel, inScope, max = 260) {
    let guard = 0;
    while (guard++ < max) {
      const list = [...doc.querySelectorAll(CLICKABLE)].filter(el => visible(el) && inScope(el));
      const todo = list.find(el => !seen.has(scopeLabel + '|' + labelOf(el)));
      if (!todo) break;
      seen.add(scopeLabel + '|' + labelOf(todo));
      results.clicked++;
      await clickOne(todo, () => `[${scopeLabel}] ` + labelOf(todo));
    }
  }

  // 先保证选中了一个 IP + 套次（小白进来第一件事）
  await sweep('选IP', el => !!el.closest('#ipTree'), 40);
  await sweep('左栏', el => !!el.closest('#colLeft'), 120);
  await sweep('顶栏', el => !!el.closest('.topbar'), 60);

  // 中栏五个标签挨个翻过去，标签内所有按钮全点
  const midTabs = [...doc.querySelectorAll('#midTabs .tab')];
  console.log('中栏标签:', midTabs.map(t => t.textContent.trim()).join(' / ') || '(没找到)');
  for (const t of midTabs) {
    const name = t.textContent.trim();
    t.dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
    await sleep(1200);
    await sweep(name, el => !!el.closest('#colMid'), 200);
  }
  await sweep('右栏', el => !!el.closest('#colRight'), 200);

  console.log('\n=== 点了 ' + results.clicked + ' 个元素 ===');
  console.log('\n【① 崩了】' + (results.broke.length || '无'));
  results.broke.slice(0, 20).forEach(x => console.log('  ✗ ' + x));
  console.log('\n【② 点了完全没反应】' + (results.dead.length || '无'));
  results.dead.slice(0, 40).forEach(x => console.log('  · ' + x));
  console.log('\n【③ 弹层空的】' + (results.empty_modal.length || '无'));
  results.empty_modal.slice(0, 10).forEach(x => console.log('  · ' + x));
  console.log('\n【服务端 5xx】' + (http5xx.length || '无'));
  [...new Set(http5xx)].slice(0, 15).forEach(x => console.log('  ! ' + x));
  console.log('\n【console.warn】' + (warns.length || '无'));
  [...new Set(warns)].slice(0, 8).forEach(x => console.log('  ~ ' + String(x).slice(0, 140)));

  win.close();
  process.exit(0);
})();
