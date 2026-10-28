/* UI 冒烟：用 jsdom 真跑一遍前端，抓运行时异常 + 检查关键节点是否渲染出来 */
const fs = require('fs');
const { JSDOM, VirtualConsole } = require('jsdom');

const BASE = process.env.UI_BASE || 'http://127.0.0.1:8791/';
const errors = [];
const vc = new VirtualConsole();
vc.on('jsdomError', e => errors.push('jsdomError: ' + (e.stack || e.message)));
vc.on('error', (...a) => errors.push('console.error: ' + a.join(' ')));
vc.on('warn', (...a) => errors.push('console.warn: ' + a.join(' ')));

(async () => {
  const html = fs.readFileSync(process.env.UI_HTML || 'ui/index.html', 'utf8');
  const dom = new JSDOM(html, {
    url: BASE,
    runScripts: 'dangerously',
    resources: 'usable',
    pretendToBeVisual: true,
    virtualConsole: vc,
    beforeParse(win) {
      win.fetch = (u, o) => fetch(String(u).startsWith('http') ? u : new URL(u, BASE).href, o);
      win.navigator.clipboard = { writeText: async () => {} };
      win.prompt = () => '测试习惯文本';
      win.confirm = () => false;
    },
  });
  const win = dom.window;

  await new Promise(r => setTimeout(r, 4000));

  const $ = s => win.document.querySelector(s);
  const report = {
    ipTree_items: win.document.querySelectorAll('#ipTree [data-ip]').length,
    setItems: win.document.querySelectorAll('#ipTree [data-set]').length,
    heroHtml_len: ($('#hero') || {}).innerHTML ? $('#hero').innerHTML.trim().length : 0,
    heroText: ($('#hero') || {}).textContent ? $('#hero').textContent.replace(/\s+/g, ' ').trim().slice(0, 90) : '',
    files_count: win.document.querySelectorAll('#filesBox [data-file]').length,
    files_path: ($('#filesPath') || {}).textContent,
    stages: win.document.querySelectorAll('#overviewBox .stage').length,
    prompts_len: ($('#promptsBox') || {}).innerHTML ? $('#promptsBox').innerHTML.length : 0,
    cmd_rows: win.document.querySelectorAll('#cmdBox [data-copy]').length,
    inspect_panels: win.document.querySelectorAll('#tabInspect .panel').length,
    inspect_truth_rows: win.document.querySelectorAll('#tabInspect .kv dt').length,
    svc_dots: win.document.querySelectorAll('#svcBar .svc').length,
    toasts: [...win.document.querySelectorAll('#toasts .toast')].map(t => t.textContent.replace(/\s+/g,' ').trim()),
    script_rows: win.document.querySelectorAll('#scriptList [data-file]').length,
    deviation_len: ($('#deviationBox') || {}).innerHTML ? $('#deviationBox').innerHTML.length : 0,
  };

  // 切到「设置」tab，验证第二层参数与保存按钮挂载
  const tab = win.document.querySelector('.tab[data-tab="settings"]');
  if (tab) tab.dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 1200));
  report.settings_fields = win.document.querySelectorAll('#tabSettings [data-set-key]').length;
  report.settings_tier2 = win.document.querySelectorAll('#tabSettings [data-set-tier="2"]').length;

  const tab2 = win.document.querySelector('.tab[data-tab="log"]');
  if (tab2) tab2.dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 900));
  report.log_rows = win.document.querySelectorAll('#tabLog .timeline-item').length;

  console.log(JSON.stringify(report, null, 2));
  console.log('\n=== errors (' + errors.length + ') ===');
  errors.slice(0, 12).forEach(e => console.log('- ' + e.slice(0, 300)));
  dom.window.close();
  process.exit(errors.length ? 2 : 0);
})();
