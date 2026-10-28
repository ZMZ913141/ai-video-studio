/* ============================================================
   洁斌视频工作台 · 前端逻辑
   ------------------------------------------------------------
   两条硬约束压在这里：
   ① 数据只有一个来源：后端 API，而后端再去调 vstudio CLI。
      JS 里不许出现「自己算出来的参数」——显示什么就是真值文件里什么。
   ② 出片类动作不给执行按钮，只拼命令给用户复制回终端。
      界面不是另一条生产链路，它是驾驶舱。
   ============================================================ */
'use strict';

/* ---------- 小工具 ---------- */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const ico = (n, cls = 'ico') => `<svg class="${cls}"><use href="#i-${n}"/></svg>`;
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function api(path, opts) {
  // 带 body 的请求一律按 POST —— fetch 不允许 GET 带 body，写漏 method 会一路炸到首屏
  const method = (opts && opts.method) || (opts && opts.body ? 'POST' : 'GET');
  const r = await fetch('/api/' + path, opts ? {
    method,
    headers: opts.body ? { 'Content-Type': 'application/json' } : {},
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  } : {});
  let j;
  try { j = await r.json(); } catch (e) { throw new Error('后端返回的不是 JSON'); }
  if (j && j.error && r.status >= 400 && j.need_confirm) j._needConfirm = true;
  return j;
}
const get = (o, p) => p.split('.').reduce((a, k) => (a && typeof a === 'object' ? a[k] : undefined), o);
const humanSize = n => n < 1024 ? n + 'B' : n < 1048576 ? (n / 1024).toFixed(0) + 'K'
  : n < 1073741824 ? (n / 1048576).toFixed(1) + 'M' : (n / 1073741824).toFixed(2) + 'G';

function toast(title, body, kind = '') {
  const box = $('#toasts');
  const d = document.createElement('div');
  d.className = 'toast ' + kind;
  d.innerHTML = `<div style="flex:1"><b>${esc(title)}</b><div class="t-body">${esc(body || '')}</div></div>`;
  box.appendChild(d);
  setTimeout(() => { d.style.opacity = '0'; d.style.transform = 'translateY(8px)'; d.style.transition = '.3s'; setTimeout(() => d.remove(), 320); }, 4200);
}

function copyText(t) {
  navigator.clipboard.writeText(t).then(() => toast('已复制', t, 'ok')).catch(() => {
    const ta = document.createElement('textarea');
    ta.value = t; document.body.appendChild(ta); ta.select();
    document.execCommand('copy'); ta.remove(); toast('已复制', t, 'ok');
  });
}

/* ---------- 状态 ---------- */
const STAGES = ['文案', '音频', '视频', '后期', '字幕', '验收', '交付'];
const state = {
  ips: [], services: {}, writable: {}, actions: {}, archive: '',
  ip: null, setId: null, setPath: null, ipRoot: null,
  jobs: [], job: null, card: null, ipcfg: null, cfg: null, prompts: null,
  fs: [], fsPath: null, filter: 'ALL', query: '', tab: 'inspect', algo: null, guard: null,
};

/* ---------- 布局记忆 ---------- */
const LS = 'vstudio.ui.v1';
const ui = Object.assign({ left: 288, right: 372, density: 'comfy', collapsed: {}, rightOpen: false },
  JSON.parse(localStorage.getItem(LS) || '{}'));
const saveUI = () => localStorage.setItem(LS, JSON.stringify(ui));

function applyLayout() {
  document.documentElement.style.setProperty('--left-w', ui.left + 'px');
  document.documentElement.style.setProperty('--right-w', ui.right + 'px');
  document.body.dataset.density = ui.density;
  $$('.panel').forEach(p => p.classList.toggle('collapsed', !!ui.collapsed[p.dataset.panel]));
  $('#colRight').classList.toggle('drawer-open', ui.rightOpen);
}
function initPanelFold() {
  document.addEventListener('click', e => {
    const head = e.target.closest('.panel-head');
    if (!head || e.target.closest('button')) return;
    const p = head.parentElement;
    p.classList.toggle('collapsed');
    ui.collapsed[p.dataset.panel] = p.classList.contains('collapsed');
    saveUI();
  });
}
function initSplitters() {
  $$('.split').forEach(sp => {
    sp.addEventListener('mousedown', ev => {
      ev.preventDefault();
      sp.classList.add('dragging');
      document.body.classList.add('resizing');
      const side = sp.dataset.side, x0 = ev.clientX;
      const base = side === 'left' ? ui.left : ui.right;
      const move = e => {
        const dx = e.clientX - x0;
        if (side === 'left') ui.left = Math.max(190, Math.min(520, base + dx));
        else ui.right = Math.max(280, Math.min(680, base - dx));
        applyLayout();
      };
      const up = () => {
        document.removeEventListener('mousemove', move);
        document.removeEventListener('mouseup', up);
        sp.classList.remove('dragging');
        document.body.classList.remove('resizing');
        saveUI();
      };
      document.addEventListener('mousemove', move);
      document.addEventListener('mouseup', up);
    });
    // 双击复位
    sp.addEventListener('dblclick', () => {
      if (sp.dataset.side === 'left') ui.left = 288; else ui.right = 372;
      applyLayout(); saveUI();
    });
  });
}

/* ---------- 面板 ---------- */
function panel(title, sub, bodyHtml, panelKey, actions = '') {
  return `<div class="panel glass" data-panel="${panelKey}">
    <div class="panel-head">
      <h3>${title}</h3>${sub ? `<span class="sub">${esc(sub)}</span>` : ''}
      <div class="panel-actions">${actions}</div>
      <svg class="caret"><use href="#i-chev"/></svg>
    </div>
    <div class="panel-body"><div class="inner">${bodyHtml}</div></div></div>`;
}

/* ============================================================
   首屏
   ============================================================ */
async function boot() {
  const b = await api('bootstrap');
  Object.assign(state, { ips: b.ips || [], services: b.services || {}, writable: b.writable || {}, actions: b.write_actions || {}, archive: b.archive });
  renderServices();
  renderTagFilter();
  await selectIP(state.ips[0]?.name);
}

function renderServices() {
  const names = { pipeline: '合成服务', n8n: '编排', comfyui: 'ComfyUI', 'zen-gate': 'LLM 网关' };
  $('#svcBar').innerHTML = Object.entries(state.services).map(([k, v]) =>
    `<div class="svc ${v === 'up' ? 'up' : 'down'}" title="${k} ${v}"><span class="dot"></span><span>${names[k] || k}</span></div>`).join('');
}

function renderTagFilter() {
  const tags = ['ALL', ...new Set(state.ips.map(i => i.tag).filter(Boolean))];
  $('#tagFilter').innerHTML = tags.map(t =>
    `<div class="filter-chip ${state.filter === t ? 'on' : ''}" data-tag="${t}">${t === 'ALL' ? '全部' : esc(t)}</div>`).join('');
}

/* ---------- 左栏树 ---------- */
async function renderTree() {
  const q = state.query.trim().toLowerCase();
  const ips = state.ips.filter(i =>
    (state.filter === 'ALL' || i.tag === state.filter) &&
    (!q || (i.name + (i.aliases || []).join(' ')).toLowerCase().includes(q)));

  $('#treeCount').textContent = `${ips.length}/${state.ips.length}`;

  const html = ips.map(i => {
    const open = state.ip === i.name;
    const kids = open ? (state.jobs || []).map(j => `
      <div class="tree-item ${state.setId === j.id ? 'active' : ''}" data-set="${esc(j.id)}">
        <span class="stage-dot" style="background:${j.delivered ? 'var(--ok)' : j.stage === j.done.length ? 'var(--busy)' : 'var(--warn)'}"></span>
        <span>${esc(j.id)}</span>
        <span style="color:var(--tx-3);font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(j.title || '')}</span>
        <span class="tag">${j.delivered ? '已交付' : esc(j.stage)}</span>
      </div>`).join('') : '';
    return `<div class="tree-item ${open ? 'open active' : ''}" data-ip="${esc(i.name)}">
        ${ico('chev', 'caret')}
        <span>${esc(i.name)}</span>
        <span class="tag">${esc(i.tag || '')}${i.ready ? '' : ' · 缺真值'}</span>
      </div>
      <div class="sub-tree ${open ? 'show' : ''}">${kids || '<div class="empty" style="padding:10px">没有套次</div>'}</div>`;
  }).join('');
  $('#ipTree').innerHTML = html || '<div class="empty">没有匹配的 IP</div>';
}

async function selectIP(name) {
  state.ip = name;
  state.ipRoot = (state.ips.find(i => i.name === name) || {}).path;
  state.setId = null; state.setPath = null; state.job = null;
  $('#btnGuard').disabled = false;
  await Promise.all([loadJobs(), loadCard(), loadIpCfg(), (async () => { state.cfg = await api('config'); })()]);
  await renderTree();
  renderHero();
  await selectSet((state.jobs.find(j => !j.delivered) || state.jobs[0] || {}).id);
}

async function loadJobs() {
  state.jobs = ((await api('jobs?ip=' + encodeURIComponent(state.ip))).jobs) || [];
}

async function loadCard() {
  state.card = await api('card?ip=' + encodeURIComponent(state.ip));
}

async function loadIpCfg() {
  state.ipcfg = await api('ipconfig?ip=' + encodeURIComponent(state.ip));
}

async function selectSet(id) {
  state.setId = id || null;
  state.setPath = null; state.prompts = null; state.algo = null; state.guard = null;
  if (!id) { renderTree(); renderHero(); return; }
  renderTree(); renderHero();
  const [job, root] = await Promise.all([
    api('job?ip=' + encodeURIComponent(state.ip) + '&id=' + encodeURIComponent(id)).catch(() => ({})),
    (async () => { const d = await api('fs?path=' + encodeURIComponent(state.ipRoot + '/07_成片'));
      return (d.items || []).find(x => x.type === 'dir' && x.name.startsWith(id + '_')); })(),
  ]);
  state.job = job && job.id ? job : null;
  state.setPath = root ? root.path : state.ipRoot + '/07_成片';
  await Promise.all([loadPrompts(), renderFiles(), renderOverview(), renderCmd(),
    renderInspect(), renderSettings(), renderLog()]);
  renderPrompts();
}

/* ============================================================
   Hero
   ============================================================ */
function renderHero() {
  const ip = state.ips.find(i => i.name === state.ip) || {};
  const j = state.setId ? (state.jobs.find(x => x.id === state.setId) || {}) : null;
  const card = state.card || {};
  const title = j ? `${j.id} ${j.title || ''}` : (ip.name || '未选中');
  const doneCount = j ? j.done.length : 0;
  $('#hero').innerHTML = `
    <div class="hero-top">
      <div class="hero-id">${esc(j ? j.id : (ip.tag || '·'))}</div>
      <div class="hero-meta">
        <h1>${esc(title)}</h1>
        <div class="sub">${esc(ip.name || '')} · ${esc(card.role || (state.ipcfg || {}).display_name || '')}
          ${j ? ` · 阶段 <b style="color:var(--accent-2)">${esc(j.stage)}</b>（${doneCount}/${STAGES.length}）
                 ${j.delivered ? '<span class="chip ok">已交付</span>' : ''}` : ' · 选左侧套次看详情'}</div>
        <div style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap">
          ${(ip.aliases || []).map(a => `<span class="chip">${esc(a)}</span>`).join('')}
          ${typeof (state.ipcfg || {}).mp === 'number' ? '' : `<span class="chip acc">${esc((state.ipcfg || {}).video?.orientation || '')}</span>`}
        </div>
      </div>
      <div class="hero-actions">
        <button class="btn sm" id="hGuard">${ico('shield')} 素材自检</button>
        <button class="btn sm" id="hAlgo">${ico('spark')} 参数预览</button>
        <button class="btn sm primary" id="hCmd">${ico('term')} 出片命令</button>
      </div>
    </div>`;
}

/* ============================================================
   概览：阶段时间线 + 自检
   ============================================================ */
async function renderOverview() {
  const box = $('#overviewBox');
  const j = state.job;
  if (!j) { box.innerHTML = '<div class="empty">没有 job.json，跑 <span class="mono">vstudio index --write</span> 补写</div>'; return; }
  const cur = STAGES.indexOf(j.stage);
  box.innerHTML = `
    <div class="stages">${STAGES.map((s, i) => {
      const st = (j.stages || {})[s]?.status || (j.done.includes(s) ? 'ok' : 'pending');
      const cls = st === 'ok' ? 'ok' : st === 'fail' ? 'fail' : (i === cur ? 'cur' : '');
      return `<div class="stage ${cls}" data-stage="${s}" title="${s}: ${st} ${(j.stages||{})[s]?.at || ''}">
        <div class="knob">${st === 'ok' ? '✓' : st === 'fail' ? '✕' : i + 1}</div>${s}</div>`;
    }).join('')}</div>
    <div class="kv" style="margin-top:14px">
      ${STAGES.filter(s => (j.artifacts || {})[s]?.length).map(s =>
        `<dt>${s}</dt><dd class="mono">${(j.artifacts[s].map(a => esc(a.split('/').pop()))).join('、')}</dd>`).join('')}
      ${(j.missing || []).length ? `<dt style="color:var(--warn)">缺</dt><dd>${j.missing.map(esc).join('、')}</dd>` : ''}
      ${(j.notes || []).length ? `<dt>备注</dt><dd>${j.notes.slice(-3).map(n => esc(n.text)).join('；')}</dd>` : ''}
    </div>`;
  if (state.algo) {
    box.insertAdjacentHTML('beforeend', `<div style="margin-top:12px" class="cmdbar">
      <span class="p">自适应</span> ${esc(state.algo.res?.[0])}×${esc(state.algo.res?.[1])}
      · ${esc(state.algo.segs)} 段 · 总帧 ${esc(state.algo.total_frames)} · 留白 ${esc(state.algo.pad_head)}/${esc(state.algo.pad_tail)}s
      · 配乐低 ${esc(state.algo.bgm_offset)} LU</div>`);
  }
}

async function runGuard() {
  $('#guardSub').textContent = '自检中…';
  const g = await api('guard?ip=' + encodeURIComponent(state.ip));
  state.guard = g;
  $('#guardSub').textContent = g.passed ? '素材齐' : '有缺';
  const pre = document.createElement('pre');
  pre.style.cssText = 'white-space:pre-wrap;font-size:11.5px;margin-top:10px;padding:10px;border-radius:10px;background:rgba(0,0,0,.26)';
  pre.textContent = g.text || '';
  $('#overviewBox').appendChild(pre);
  toast(g.passed ? '自检通过' : '自检有缺', '详见「进度与自检」', g.passed ? 'ok' : 'err');
}

/* ============================================================
   文件浏览
   ============================================================ */
async function renderFiles(path) {
  const box = $('#filesBox');
  const p = path || state.setPath || state.ipRoot;
  box.innerHTML = '<div class="sk" style="width:60%"></div><div class="sk" style="width:80%"></div>';
  const d = await api('fs?path=' + encodeURIComponent(p));
  if (d.error) { box.innerHTML = `<div class="empty">${esc(d.error)}</div>`; return; }
  state.fsPath = d.path;
  state.fsParent = d.parent;
  $('#filesPath').textContent = d.path.replace(state.ipRoot + '/', '').replace(state.archive, '');

  const dirs = d.items.filter(i => i.type === 'dir');
  const files = d.items.filter(i => i.type !== 'dir');
  box.innerHTML = `
    ${dirs.length ? `<div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px">${dirs.map(x =>
      `<div class="chip" style="cursor:pointer" data-dir="${esc(x.path)}">${ico('folder')} ${esc(x.name)}</div>`).join('')}</div>` : ''}
    <div class="files">${files.map(f => {
      const thumb = (f.type === 'video' || f.type === 'image')
        ? `<img loading="lazy" src="/api/thumb?path=${encodeURIComponent(f.path)}" onerror="this.style.display='none'">`
        : `<div class="ico-wrap">${ico(f.type === 'audio' ? 'audio' : f.type === 'text' ? 'text' : 'file', 'ico')}</div>`;
      return `<div class="fcard" data-file="${esc(f.path)}" data-type="${f.type}">
        <div class="thumb">${thumb}<span class="badge">${f.mtime}</span></div>
        <div class="nm">${esc(f.name)}</div></div>`;
    }).join('')}</div>
    ${files.length ? `<div style="margin-top:10px">${files.map(f =>
      `<div class="frow" data-file="${esc(f.path)}" data-type="${f.type}">${ico(f.type === 'video' ? 'video' : f.type === 'audio' ? 'audio' : f.type === 'image' ? 'image' : 'file', 'ico')}
        <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(f.name)}</span>
        <span class="sz">${humanSize(f.size)}</span></div>`).join('')}</div>` : ''}
    ${!d.items.length ? '<div class="empty">空目录</div>' : ''}`;
}

function openFile(p, type) {
  const name = p.split('/').pop();
  if (type === 'video') {
    $('#modalBox').innerHTML = `<div class="modal-head"><b>${esc(name)}</b><div class="spacer"></div>
        <button class="btn icon" id="mcopy" data-src="${encodeURIComponent(p)}">${ico('copy')}</button><button class="btn icon" id="mclose">${ico('close')}</button></div>
      <video controls autoplay src="/api/raw?path=${encodeURIComponent(p)}"></video>`;
  } else if (type === 'image') {
    $('#modalBox').innerHTML = `<div class="modal-head"><b>${esc(name)}</b><div class="spacer"></div>
        <button class="btn icon" id="mclose">${ico('close')}</button></div><img src="/api/raw?path=${encodeURIComponent(p)}">`;
  } else if (type === 'text') {
    $('#modalBox').innerHTML = `<div class="modal-head"><b>${esc(name)}</b><div class="spacer"></div>
        <button class="btn icon" id="mclose">${ico('close')}</button></div><pre id="mtext">载入中…</pre>`;
    api('text?path=' + encodeURIComponent(p)).then(t =>
      ($('#mtext').textContent = t.text || t.error || '（空）'));
  } else {
    $('#modalBox').innerHTML = `<div class="modal-head"><b>${esc(name)}</b><div class="spacer"></div>
        <button class="btn icon" id="mclose">${ico('close')}</button></div>
      <div class="empty">不支持预览，<a href="/api/raw?path=${encodeURIComponent(p)}" download>点此下载</a></div>`;
  }
  $('#modal').classList.add('show');
}

/* ============================================================
   提示词
   ============================================================ */
async function loadPrompts() {
  state.prompts = await api('prompts?ip=' + encodeURIComponent(state.ip) + '&set=' + encodeURIComponent(state.setId || ''));
}

function renderPrompts() {
  const box = $('#promptsBox'), p = state.prompts || {};
  if (!p.exists) {
    box.innerHTML = `<div class="empty">还没有提示词。<br><span class="mono">${esc(p.dir || '')}/prompts.json</span><br>
      用右上角「AI 生成」拿到命令后回终端跑。</div>`; $('#promptsSub').textContent = '未生成'; return;
  }
  const d = p.data || {};
  const segs = Array.isArray(d) ? d : (d.segments || d.prompts || []);
  const gp = state.cfg?.pipeline?.global_prompt?.enabled
    ? `<div class="pseg"><div class="hd">${ico('shield')} 全局通用片段</div><div class="tx">${esc(state.cfg.pipeline.global_prompt.prefix)}</div></div>` : '';
  $('#promptsSub').textContent = `${segs.length} 段`;
  box.innerHTML = gp + (segs.length ? segs.map((s, i) => {
    const tx = typeof s === 'string' ? s : (s.prompt || s.text || JSON.stringify(s));
    return `<div class="pseg"><div class="hd"><b>#${i + 1}</b>${Object.keys(s).filter(k => !['prompt', 'text'].includes(k)).map(k =>
      `<span class="chip">${esc(k)}:${esc(String(s[k]).slice(0, 26))}</span>`).join('')}</div>
      <div class="tx">${esc(tx)}</div></div>`;
  }).join('') : '<div class="empty">文件里没找到分段</div>');
}

/* ============================================================
   命令区
   ============================================================ */
async function renderCmd() {
  const kinds = [
    ['pipeline-fast', '快速试跑（DRY 先看计划）', 'spark'],
    ['pipeline-std', '正式出片（std 档）', 'play'],
    ['prompts-gen', 'AI 生成提示词', 'plus'],
    ['prompts-opt', 'AI 优化提示词', 'refresh'],
    ['prompts-show', '查看当前提示词', 'eye'],
    ['algo', '自适应参数预览', 'sliders'],
  ];
  const rows = await Promise.all(kinds.map(async ([k, label, ic]) => {
    const c = await api('cmdline', { body: { kind: k, ip: state.ip, set: state.setId } });
    return { k, label, ic, cmd: c.cmdline || '' };
  }));
  $('#cmdBox').innerHTML = `
    <div class="hint" style="margin-bottom:9px">出片是重活（十几分钟到半小时、要吃 GPU），界面不代跑 —— 复制回终端，看得见全部输出。</div>
    ${rows.map(r => `<div style="margin-bottom:8px">
      <div style="display:flex;align-items:center;gap:7px;margin-bottom:4px">
        ${ico(r.ic, 'ico')}<span style="font-size:12px">${r.label}</span>
        <div class="spacer"></div><button class="btn sm" data-copy="${esc(r.cmd)}">${ico('copy')} 复制</button></div>
      <div class="cmdbar"><span class="p">$</span> ${esc(r.cmd)}</div></div>`).join('')}`;
}

/* ============================================================
   右栏：检查 / 设置 / 记录
   ============================================================ */
function kvRow(o, keys) {
  return keys.filter(k => typeof get(o, k) !== 'undefined').map(k => {
    const v = String(get(o, k));
    return '<dt>' + esc(k.split('.').pop()) + '</dt><dd class="'
      + (v.length > 40 ? 'mono' : '') + '">' + esc(v) + '</dd>';
  }).join('');
}

function renderInspect() {
  const c = state.card || {}, g = state.ipcfg || {};

  let demand = kvRow(c, ['type', 'role', 'status', 'created'])
    + Object.entries(c.needs || {}).map(([k, v]) =>
      '<dt>' + esc(k) + '</dt><dd>' + esc(Array.isArray(v) ? v.join('、') : String(v)) + '</dd>').join('');
  demand = '<div class="kv">' + demand + '</div>';

  const redlines = (c.redlines || []).map(r =>
    '<div class="pseg bad"><div class="tx">' + esc(r) + '</div></div>').join('')
    || '<div class="empty">没记红线</div>';

  const habits = (c.habits || []).slice().reverse().map(h =>
    '<div class="timeline-item"><span class="t">' + esc(h.at) + '</span>'
    + '<span style="color:var(--tx-2)">' + esc(h.text) + '</span></div>').join('')
    || '<div class="empty">还没记习惯</div>';

  const truth = '<div class="kv">' + kvRow(g, ['video.orientation', 'video.mp', 'video.sampler',
    'video.steps', 'video.overlap', 'video.unet', 'video.lora.name', 'video.lora.strength',
    'voice.tts.model', 'voice.tts.cfg_scale', 'subtitle.fs_cue', 'subtitle.fs_title']) + '</div>'
    + '<div class="hint">这些值来自 ip.json，界面只负责显示；要改去「设置 → 个人」。</div>';

  const quick = '<div style="display:flex;flex-wrap:wrap;gap:7px">'
    + '<button class="btn sm" id="actNote">' + ico('plus') + ' 记一条习惯</button>'
    + '<button class="btn sm" id="actIndex">' + ico('refresh') + ' 重建索引</button></div>';

  $('#tabInspect').innerHTML = [
    panel(ico('note') + ' 档案卡 · 需求', '', demand, 'card-demand'),
    panel(ico('alert') + ' 红线', (c.redlines || []).length + ' 条', redlines, 'card-redline'),
    panel(ico('hist') + ' 习惯', (c.habits || []).length + ' 条', habits, 'card-habit'),
    panel(ico('sliders') + ' 技术真值（只读）', '', truth, 'tech-truth'),
    panel(ico('term') + ' 快速动作', '', quick, 'quick-actions'),
  ].join('');
  applyLayout();
}

async function renderSettings() {
  if (!state.cfg) state.cfg = await api('config');
  const W = state.writable || {};
  const cfgsample = state.cfg.pipeline || {};
  const ipdata = state.ipcfg || {};

  const build = (table, sample, scope, tier, label) => {
    const rows = Object.entries(table).filter(([, m]) => m.tier === tier);
    if (!rows.length) return '';
    return `<h2 class="sec-title">${label}</h2>` + rows.map(([key, m]) => {
      const cur = get(sample, key);
      const id = `set-${scope}-${key.replace(/\./g, '_')}`;
      let ctrl = '';
      if (m.enum) ctrl = `<select id="${id}">${m.enum.map(v => `<option ${v === cur ? 'selected' : ''}>${v}</option>`).join('')}</select>`;
      else if (m.type === 'bool') ctrl = `<select id="${id}"><option value="true" ${cur ? 'selected' : ''}>开</option><option value="false" ${!cur ? 'selected' : ''}>关</option></select>`;
      else if (m.type === 'int' || m.type === 'float') ctrl = `<input type="number" id="${id}" value="${esc(cur)}" step="${m.type === 'float' ? '0.01' : '1'}" min="${m.min ?? ''}" max="${m.max ?? ''}">`;
      else if (m.type === 'text' && m.maxlen > 200) ctrl = `<textarea id="${id}">${esc(cur)}</textarea>`;
      else ctrl = `<input type="text" id="${id}" value="${esc(cur)}">`;
      return `<label class="field"><span>${esc(m.label)} <code class="mono" style="color:var(--tx-3)">${esc(key)}</code></span>${ctrl}
        <div style="display:flex;gap:6px;margin-top:6px;align-items:center">
          <span class="hint" style="flex:1">现为 <b class="mono">${esc(typeof cur === 'object' ? JSON.stringify(cur) : String(cur))}</b>${m.tier === 2 ? ' · 高级' : ''}</span>
          <button class="btn sm" data-set-key="${esc(key)}" data-set-scope="${scope}" data-set-tier="${m.tier}" data-set-ctrl="${id}">${ico('save')} 保存</button>
        </div></label>`;
    }).join('');
  };

  const html = `
    <div class="panel glass" data-panel="set-global">
      <div class="panel-head"><h3>${ico('sliders')} 全局设置</h3><span class="sub">影响所有 IP</span>
        <svg class="caret"><use href="#i-chev"/></svg></div>
      <div class="panel-body"><div class="inner">
        ${build(W.global || {}, cfgsample, 'global', 1, '常用')}
        ${build(W.global || {}, cfgsample, 'global', 2, '高级（改前请想清楚）')}
      </div></div></div>

    <div class="panel glass" data-panel="set-ip">
      <div class="panel-head"><h3>${ico('shield')} 个人设置</h3><span class="sub">${esc(state.ip || '')}</span>
        <svg class="caret"><use href="#i-chev"/></svg></div>
      <div class="panel-body"><div class="inner">
        ${build(W.ip || {}, ipdata, 'ip', 1, '常用')}
        ${build(W.ip || {}, ipdata, 'ip', 2, '高级（改前请想清楚）')}
        <div class="hint">ip.json 里绝大多数参数界面不可写 —— 采样器、二采模型、画幅这类动一处就要重介整条链路的，只留脚本调用。</div>
      </div></div></div>`;
  $('#tabSettings').innerHTML = html;
  applyLayout();
}

async function renderLog() {
  const d = await api('changelog?ip=' + encodeURIComponent(state.ip || ''));
  const items = d.items || [];
  $('#tabLog').innerHTML = panel(`${ico('hist')} 界面改动记录`, items.length + ' 条',
    items.length ? items.map(i => `<div class="timeline-item">
      <span class="t">${esc(i.at.slice(5))}</span>
      <span><span class="chip ${i.scope === 'ip' ? 'acc' : ''}">${esc(i.scope === 'ip' ? i.ip : '全局')}</span>
      <code class="mono" style="color:var(--accent-2)">${esc(i.key)}</code>
      <span style="color:var(--tx-3)"> ${esc(String(i.old ?? '—'))}</span> → <span>${esc(String(i.new ?? '—'))}</span></span>
    </div>`).join('') : '<div class="empty">还没有从界面改过东西</div>', 'log');
  applyLayout();
}

/* ---------- 设置保存 ---------- */
async function saveSetting(scope, key, ctrlId, tier) {
  const elv = document.getElementById(ctrlId);
  let value = elv.value;
  const OK = async (withConfirm) => {
    const r = await api('set', { body: { scope, key, value, path: state.ip, confirm: withConfirm } });
    if (r._needConfirm) {
      $('#modalBox').innerHTML = `<div class="modal-head"><b>${ico('alert')} 改的是第二层参数</b></div>
        <div style="margin:6px 0 14px"><code class="mono">${esc(key)}</code> → <b>${esc(String(value))}</b>
        <div class="hint">这是高级参数，改错了不会立刻报错，但会一路带到出片才发现。确定要改吗？</div></div>
        <div style="display:flex;gap:8px;justify-content:flex-end">
          <button class="btn ghost" id="mNo">取消</button>
          <button class="btn danger" id="mYes">确认修改</button></div>`;
      $('#modal').classList.add('show');
      $('#mYes').onclick = async () => { closeModal(); await OK(true); };
      $('#mNo').onclick = closeModal;
      return;
    }
    if (r.ok) {
      toast('已保存', `${key} → ${r.new}`, 'ok');
      await Promise.all([loadIpCfg(), (async () => { state.cfg = await api('config'); })()]);
      renderSettings(); renderLog(); renderInspect();
    } else toast('没改成', r.error || '', 'err');
  };
  await OK(false);
}

function closeModal() { $('#modal').classList.remove('show'); }

/* ============================================================
   偏离 / 脚本列表
   ============================================================ */
async function renderScripts() {
  const d = await api('scripts?ip=' + encodeURIComponent(state.ip || '洁斌'));
  const arr = Array.isArray(d) ? d : [];
  $('#scriptCount').textContent = arr.length + ' 个';
  $('#scriptList').innerHTML = arr.map(s =>
    `<div class="frow" data-file="${esc(s.path)}" data-type="text">${ico('term', 'ico')}
      <span>${esc(s.file)}</span><span class="sz">${esc((s.desc || '').slice(0, 22))}</span></div>`).join('')
    || '<div class="empty">该 IP 没有生产脚本</div>';
}

async function renderDeviation() {
  $('#deviationBox').innerHTML = '<div class="sk" style="width:70%"></div>';
  const d = await api('scripts-diff');
  if (!Array.isArray(d)) {
    $('#deviationBox').innerHTML = `<div class="cmdbar" style="white-space:pre-wrap">${esc(d.text || JSON.stringify(d).slice(0, 400))}</div>`;
    return;
  }
  $('#deviationBox').innerHTML = d.length ? d.map(x => `
    <div class="timeline-item"><span class="t">${esc(x.ip || '')}</span>
      <span>${esc(x.file || '')} <span class="chip ${x.diff > 0 ? 'warn' : 'ok'}">${x.diff > 0 ? `差 ${x.diff} 行` : '一致'}</span></span></div>`).join('')
    : '<div class="empty">全部与真源一致</div>';
}

/* ============================================================
   事件
   ============================================================ */
function bind() {
  // 树
  document.addEventListener('click', async e => {
    const ipItem = e.target.closest('[data-ip]');
    if (ipItem) return void await selectIP(ipItem.dataset.ip);
    const setItem = e.target.closest('[data-set]');
    if (setItem) return void await selectSet(setItem.dataset.set);
    const tagItem = e.target.closest('[data-tag]');
    if (tagItem) { state.filter = tagItem.dataset.tag; renderTagFilter(); await renderTree(); return; }

    const copy = e.target.closest('[data-copy]');
    if (copy) return void copyText(copy.dataset.copy);

    const file = e.target.closest('[data-file]');
    if (file) return void openFile(file.dataset.file, file.dataset.type);
    const dir = e.target.closest('[data-dir]');
    if (dir) return void renderFiles(dir.dataset.dir);

    const saveBtn = e.target.closest('[data-set-key]');
    if (saveBtn) return void saveSetting(saveBtn.dataset.setScope, saveBtn.dataset.setKey,
      saveBtn.dataset.setCtrl, +saveBtn.dataset.setTier);

    const tab = e.target.closest('.tab');
    if (tab) {
      state.tab = tab.dataset.tab;
      $$('.tab').forEach(t => t.classList.toggle('on', t.dataset.tab === state.tab));
      ['inspect', 'settings', 'log'].forEach(k => $('#tab' + k[0].toUpperCase() + k.slice(1)).style.display = state.tab === k ? '' : 'none');
      if (state.tab === 'settings') renderSettings();
      if (state.tab === 'log') renderLog();
      return;
    }
    if (e.target.closest('#mclose')) return closeModal();
    if (e.target.closest('#mcopy')) return copyText(decodeURIComponent(e.target.closest('#mcopy').dataset.src || ''));
  });

  // 顶栏
  $('#globalSearch').addEventListener('input', async e => {
    state.query = e.target.value; await renderTree();
  });
  document.addEventListener('keydown', e => {
    if (e.key === '/' && document.activeElement.tagName !== 'INPUT' && document.activeElement.tagName !== 'TEXTAREA') {
      e.preventDefault(); $('#globalSearch').focus();
    }
    if (e.key === 'Escape') closeModal();
  });
  $('#modal').addEventListener('click', e => { if (e.target.id === 'modal') closeModal(); });
  $('#btnRefresh').addEventListener('click', async () => {
    if (!state.ip) return;
    await Promise.all([loadJobs(), loadCard(), loadIpCfg()]);
    state.cfg = await api('config');
    await selectSet(state.setId);
    renderScripts(); renderDeviation();
    toast('已刷新', state.ip + (state.setId ? ' / ' + state.setId : ''), 'ok');
  });
  $('#btnDensity').addEventListener('click', () => {
    ui.density = ui.density === 'comfy' ? 'compact' : 'comfy';
    applyLayout(); saveUI();
  });

  // 中栏按钮：hero 里的三个 + overview 里的自检
  document.addEventListener('click', async e => {
    if (e.target.closest('#hGuard') || e.target.closest('#btnGuard')) return void runGuard();
    if (e.target.closest('#hAlgo')) return void showAlgo();
    if (e.target.closest('#hCmd')) {
      const c = await api('cmdline', { body: { kind: 'pipeline-fast', ip: state.ip, set: state.setId } });
      copyText(c.cmdline || '');
    }
    if (e.target.closest('#btnUp')) return void renderFiles(state.fsParent);
    if (e.target.closest('#btnCmdGen')) {
      const c = await api('cmdline', { body: { kind: 'prompts-gen', ip: state.ip, set: state.setId } });
      copyText(c.cmdline || '');
    }
    if (e.target.closest('#btnCmdOpt')) {
      const c = await api('cmdline', { body: { kind: 'prompts-opt', ip: state.ip, set: state.setId } });
      copyText(c.cmdline || '');
    }
    if (e.target.closest('#actNote')) {
      const t = prompt('记一条习惯/需求/红线（会被写进 card.json）：');
      if (!t) return;
      const kind = confirm('是红线吗？（取消 = 记为习惯）') ? 'redline' : 'habit';
      const r = await api('run', { body: { cmd: 'card-note', args: [state.ip, t, '--kind', kind] } });
      toast(r.cmdline ? '已提交' : '失败', r.cmdline || r.error || '', r.error ? 'err' : 'ok');
      setTimeout(async () => { await loadCard(); renderInspect(); }, 1200);
    }
    if (e.target.closest('#actIndex')) {
      const r = await api('run', { body: { cmd: 'index', args: ['--write'] } });
      toast(r.cmdline ? '已提交' : '失败', r.cmdline || r.error || '', r.error ? 'err' : 'ok');
      setTimeout(async () => { await loadJobs(); await renderTree(); }, 1500);
    }
  });
}

async function showAlgo() {
  const d = await api('algo?ip=' + encodeURIComponent(state.ip) + '&set=' + encodeURIComponent(state.setId || ''));
  if (d._error) return toast('算不出来', d._error, 'err');
  state.algo = d;
  await renderOverview();
  toast('自适应参数已刷新', `${d.segs} 段 / ${d.total_frames} 帧`, 'ok');
}

/* ---------- 启动 ---------- */
(async function () {
  applyLayout();
  initPanelFold();
  initSplitters();
  bind();
  try {
    await boot();
    renderScripts();
    renderDeviation();
  } catch (e) {
    toast('起不来', String(e), 'err');
  }
})();
