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

  // ---- 2026-10-29 新增：需求台 / 字体预览 / 恢复默认 / 音频 / 服务详情 ----
  report.brief_area = win.document.querySelectorAll('#briefBox textarea').length;
  report.brief_sub = ($('#briefSub') || {}).textContent;

  win.document.querySelector('.tab[data-tab="settings"]')
    ?.dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 800));
  report.font_selects = win.document.querySelectorAll('#tabSettings select#prevFontCue, #tabSettings select#prevFontTitle').length;
  report.font_options = win.document.querySelectorAll('#tabSettings #prevFontCue option').length;
  report.font_burnable_opt = [...win.document.querySelectorAll('#tabSettings #prevFontCue option')]
    .filter(o => o.textContent.startsWith('✓')).length;
  report.reset_buttons = win.document.querySelectorAll('#tabSettings [data-reset-key]').length;
  report.stage_rendered = win.document.querySelectorAll('#tabSettings .font-stage').length;

  // 服务徽章：以前点了没反应，现在必须弹出详情
  const svc = win.document.querySelector('#svcBar [data-svc]');
  if (svc) {
    svc.dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
    await new Promise(r => setTimeout(r, 900));
    report.svc_modal_rows = win.document.querySelectorAll('#modalBox .kv dt').length;
    win.document.querySelector('#mclose')?.dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
    await new Promise(r => setTimeout(r, 200));
  }

  // 音频预览：以前落到「不支持预览」，现在必须出 <audio>
  const au = win.document.querySelector('[data-file][data-type="audio"]');
  report.audio_files = win.document.querySelectorAll('[data-file][data-type="audio"]').length;
  if (au) {
    au.dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
    await new Promise(r => setTimeout(r, 400));
    report.audio_player = win.document.querySelectorAll('#modalBox audio').length;
    win.document.querySelector('#mclose')?.dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
  }

  // 断点重做按钮
  report.redo_buttons = win.document.querySelectorAll('#cmdBox [data-redo]').length;

  const closeAndWait = async () => {
    win.document.querySelector('#mclose')?.dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
    await new Promise(r => setTimeout(r, 200));
  };

  // 进度节点：点一下必须出「产物 + 重跑命令」，不是死节点
  const stageNode = win.document.querySelector('#overviewBox .stage[data-stage]');
  if (stageNode) {
    stageNode.dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
    await new Promise(r => setTimeout(r, 1200));
    report.stage_modal_artifacts = win.document.querySelectorAll('#modalBox .stage-art').length;
    report.stage_modal_plans = win.document.querySelectorAll('#modalBox .plan').length;
    report.stage_modal_cmds = [...win.document.querySelectorAll('#modalBox .plan .cmdbar')]
      .map(x => x.textContent.trim().slice(0, 80));
    await closeAndWait();
  }

  // LLM 车道：点徽章必须出车道列表（以前是纯装饰）
  const llmBadge = win.document.querySelector('#svcBar [data-svc="zen-gate"]');
  if (llmBadge) {
    llmBadge.dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
    await new Promise(r => setTimeout(r, 1500));
    report.llm_channels = win.document.querySelectorAll('#modalBox .chan').length;
    report.llm_prefer_btns = win.document.querySelectorAll('#modalBox [data-prefer]').length;
    report.llm_probe_btns = win.document.querySelectorAll('#modalBox [data-probe]').length;
    await closeAndWait();
  }

  // MCP 面板：必须列出工具，证明「别的 AI 能调用」
  const mcpBadge = win.document.querySelector('#svcBar [data-mcp]');
  if (mcpBadge) {
    mcpBadge.dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
    await new Promise(r => setTimeout(r, 3500));
    report.mcp_tools = win.document.querySelectorAll('#modalBox .chips .chip').length;
    report.mcp_register = !!win.document.querySelector('#modalBox [data-copy]');
    await closeAndWait();
  }

  // 建档第一步：必须先把「说人话」的框摆出来，而不是让人直接填字段
  win.document.querySelector('#btnNewCard')?.dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
  await new Promise(r => setTimeout(r, 300));
  report.card_step1_talk = win.document.querySelectorAll('#modalBox #ncTalk').length;
  report.card_step1_ai = win.document.querySelectorAll('#modalBox #ncAI').length;
  await closeAndWait();

  console.log(JSON.stringify(report, null, 2));
  console.log('\n=== errors (' + errors.length + ') ===');
  errors.slice(0, 12).forEach(e => console.log('- ' + e.slice(0, 300)));
  dom.window.close();
  process.exit(errors.length ? 2 : 0);
})();
