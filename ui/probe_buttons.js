/* 定点复现那几个「点了没反应」的按钮 —— 看它们到底干了什么、有没有告诉用户 */
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
  const allToasts = [];
  const dom = new JSDOM(html, {
    url: BASE, runScripts: 'dangerously', resources: 'usable',
    pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(win) {
      win.fetch = (u, o) => fetch(String(u).startsWith('http') ? String(u) : new URL(u, BASE).href, o);
      win.navigator.clipboard = { writeText: async () => {} };
      win.confirm = () => { allToasts.push('[confirm弹了]'); return false; };
      win.prompt = () => null; win.alert = () => {};
      // 劫持 toast：记下每一次提示（它是唯一告诉用户「发生了什么」的通道）
      win.eval(`window.__toasts = [];`);
      win.open = () => null;
    },
  });
  const win = dom.window; const doc = win.document;
  await sleep(4000);

  // toast 劫持必须在 app.js 定义之后覆盖到它被调用的地方：改原型不行，
  // 直接监听插入：用 MutationObserver 抓 #toasts 的孩子
  await win.eval(`(() => {
    window.__toasts = [];
    const box = document.getElementById('toasts');
    new MutationObserver(ms => ms.forEach(m => m.addedNodes.forEach(n => {
      window.__toasts.push((n.textContent||'').replace(/\\s+/g,' ').trim());
    }))).observe(box, { childList: true });
  })()`);

  const grab = () => win.eval(`JSON.stringify(window.__toasts)`);
  const clear = () => win.eval(`window.__toasts = []`);
  const click = async sel => {
    await clear();
    const el = doc.querySelector(sel);
    if (!el) return '(元素不存在 ' + sel + ')';
    el.dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
    await sleep(2500);
    return await grab();
  };

  const out = {};
  // 切到创作
  doc.querySelectorAll('#midTabs .tab').forEach(t => {
    if (t.textContent.trim() === '创作') t.dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
  });
  await sleep(1800);

  out['人设：AI 起草（空白）'] = await click('#btnPersonaDraft');
  // 填点内容再点
  const pt = doc.querySelector('#personaText');
  if (pt) { pt.value = '我是老王，开火锅店二十年'; }
  out['人设：AI 起草（有内容）'] = await click('#btnPersonaDraft');

  out['需求台：存需求'] = await click('#btnBriefSave');

  // 对话：先造一条再清空
  doc.querySelectorAll('#midTabs .tab').forEach(t => {
    if (t.textContent.trim() === '对话') t.dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
  });
  await sleep(1500);
  const before = win.eval(`JSON.stringify(state.dialogue.length)`);
  await win.eval(`state.dialogue = [{role:'user',content:'帮我写一条关于开店二十年的口播'}]; renderDialogue();`);
  await sleep(300);
  const mid = win.eval(`JSON.stringify(state.dialogue.length)`);
  const res = await click('#btnDialogueClear');
  const after = win.eval(`JSON.stringify(state.dialogue.length)`);
  out['对话：清空'] = { 前: before, 点前: mid, 点后: after, toast与confirm: res };

  console.log(JSON.stringify(out, null, 2));
  console.log('errors:', errs.length ? errs.slice(0, 6) : 'none');
  win.close(); process.exit(0);
})();
