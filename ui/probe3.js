/* 小白最常懵的三种情形（不是崩，是「不知道发生了什么」）：
   A 破坏性操作无确认 —— 点一下东西没了，还不告诉你
   B 面板折叠 / 内容没就绪就点了 —— 静默 return，纯粹没反应
   C 慢操作没有「正在…」反馈 —— 人以为没点上，再点一次（重复烧钱/重复出片） */
const fs = require('fs');
const { JSDOM, VirtualConsole } = require('jsdom');
const BASE = 'http://127.0.0.1:8791/';
const errs = [];
const vc = new VirtualConsole();
vc.on('jsdomError', e => errs.push('jsdomError: ' + (e.message || e)));
vc.on('error', (...a) => errs.push('console.error: ' + a.map(String).join(' ')));
const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  const html = fs.readFileSync('ui/index.html', 'utf8');
  const dom = new JSDOM(html, {
    url: BASE, runScripts: 'dangerously', resources: 'usable',
    pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(win) {
      win.fetch = (u, o) => fetch(String(u).startsWith('http') ? String(u) : new URL(u, BASE).href, o);
      win.navigator.clipboard = { writeText: async () => {} };
      win.confirm = () => { win.__confirm = (win.__confirm || 0) + 1; return false; };
      win.prompt = () => null; win.alert = () => {};
      win.open = () => null;
    },
  });
  const win = dom.window; const doc = win.document;
  await sleep(4000);
  await win.eval(`window.__t=[]; new MutationObserver(ms=>ms.forEach(m=>m.addedNodes.forEach(n=>window.__t.push((n.textContent||'').replace(/\\s+/g,' ').trim())))).observe(document.getElementById('toasts'),{childList:true});`);

  const toasts = () => win.eval(`JSON.stringify(window.__t)`);
  const reset = async () => { await win.eval(`window.__t=[]; window.__confirm=0;`); };
  const click = async sel => {
    const el = doc.querySelector(sel);
    if (!el) return '(不存在)';
    el.dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
  };
  const tab = name => doc.querySelectorAll('#midTabs .tab').forEach(t => {
    if (t.textContent.trim() === name) t.dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
  });

  const R = {};

  // ── B：面板折叠时点标题栏按钮 ────────────────────────────
  await tab('创作'); await sleep(1600);
  const briefPanel = doc.querySelector('[data-panel="brief"]');
  briefPanel.classList.add('collapsed');                 // 模拟小白把面板折叠了
  await reset();
  await click('#btnBriefSave');
  await sleep(1800);
  R['B 折叠「需求台」后点「存需求」'] = {
    toast: await toasts(),
    briefTextExists: await win.eval(`!!document.querySelector('#briefText')`),
    说明: '标题栏的按钮在折叠状态下照样能点',
  };
  briefPanel.classList.remove('collapsed');
  await sleep(300);

  // ── A：破坏性操作有没有确认 ──────────────────────────────
  await tab('对话'); await sleep(1400);
  await win.eval(`state.dialogue=[{role:'user',content:'第一条'},{role:'assistant',content:'第二条'},{role:'user',content:'第三条'}]; renderDialogue();`);
  await sleep(300);
  await reset();
  await click('#btnDialogueClear');
  await sleep(1200);
  R['A 点「清空对话」'] = {
    点前几条: 3,
    点后几条: await win.eval(`state.dialogue.length`),
    有确认弹框吗: await win.eval(`window.__confirm || 0`),
    有提示吗: await toasts(),
  };

  // ── C：慢操作有没有「正在…」 ─────────────────────────────
  await tab('创作'); await sleep(1600);
  await win.eval(`(document.querySelector('#personaText')||{}).value='我是老王，开火锅店二十年，靠一锅汤';`);
  await reset();
  const t0 = Date.now();
  await click('#btnPersonaDraft');
  await sleep(900);                                     // 只等 0.9 秒，看这一瞬间有没有反馈
  R['C 点「AI 起草」后 0.9 秒内'] = {
    toast: await toasts(),
    按钮是否禁用: await win.eval(`!!document.querySelector('#btnPersonaDraft')?.disabled`),
    页面有任何进行中字样吗: await win.eval(
      `/进行|起草中|思考|载入|正在|理解|…|\\.\\.\\./.test(document.body.textContent)`),
    panelBusyClass: await win.eval(`!!document.querySelector('.busy,.loading,[data-busy]')`),
  };
  // 等久一点，看最后到底出没出结果
  await sleep(25000);
  R['C 等满 26 秒后'] = { toast: await toasts(), 耗时秒: Math.round((Date.now() - t0) / 1000) };

  console.log(JSON.stringify(R, null, 2));
  console.log('errors:', errs.length ? errs.slice(0, 6) : 'none');
  win.close(); process.exit(0);
})();
