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
// 拼 query 一律走它：中文 IP 名（陈洁斌主号）不编码会被 http.server 读成乱码
const qs = o => Object.entries(o).filter(([, v]) => v !== undefined && v !== null && v !== '')
  .map(([k, v]) => encodeURIComponent(k) + '=' + encodeURIComponent(v)).join('&');

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

/* ============================================================
   长任务：真的按下去跑，日志就在眼前
   ------------------------------------------------------------
   为什么现在给执行按钮了：
     以前只给命令让人回终端，理由是不想让他「看不见死在哪」。
     可实际用下来，操作的人不想为了看一条片子来回复制粘贴 ——
     更好的答案不是「不给按钮」，而是「给了按钮就必须配一个能看能停的日志窗」。
   安全怎么守住：
     界面依然不发明命令。前端只报「要做什么」(kind) + IP/套次，
     argv 由后端 build_exec 拼，不经过 shell。吃 GPU 的动作后端会回 428，
     前端必须先弹确认才带 confirm 重发。
   ============================================================ */
const run = {
  panel: null, id: null, timer: null, offset: 0, label: '', cmdline: '',
  payload: null, onDone: null, state: 'idle', t0: 0,
};

function runUI() {
  if (!run.panel) run.panel = $('#runPanel');
  return run.panel;
}

function openRunPanel(label, cmdline) {
  const p = runUI();
  p.classList.add('show');
  p.classList.remove('min');
  $('#runLabel').textContent = label || '运行';
  $('#runSub').textContent = '';
  $('#runCmd').textContent = cmdline || '';
  $('#runLog').textContent = '';
  $('#runDot').className = 'dot run-dot busy';
  $('#runStop').disabled = false;
  $('#runAgain').disabled = true;
  run.t0 = Date.now();
}

function closeRunPanel() {
  // 只关窗，不停任务 —— 关窗就杀进程，那是「手滑就毁掉二十分钟」的设计
  runUI().classList.remove('show');
}

function setRunState(st, extra) {
  run.state = st;
  const dot = $('#runDot');
  dot.className = 'dot run-dot ' + (st === 'running' ? 'busy' : st === 'done' ? 'ok' : st === 'stopped' ? 'warn' : 'bad');
  const map = { running: '运行中', done: '完成', failed: '失败', stopped: '已停止' };
  const secs = Math.round((Date.now() - run.t0) / 1000);
  $('#runSub').textContent = `${map[st] || st}${st === 'running' ? ' · ' + secs + 's' : ' · 用时 ' + secs + 's'}`
    + (extra ? ' · ' + extra : '');
  $('#runStop').disabled = st !== 'running';
  $('#runAgain').disabled = st === 'running';
}

/**
 * 起一个长任务。payload 交给后端白名单翻译成命令。
 * opts.onDone(result)  跑成功后拿 JSON 结果（AI 类动作才有）
 * opts.then(result)    不分状态，跑完就调（用来刷新界面）
 * opts.quiet           不打开运行面板（极短的动作）
 */
async function startExec(payload, opts = {}) {
  let r = await api('exec', { body: payload });
  if (r._needConfirm) {
    const ok = await confirmBox('这一步要花点东西，确认跑？', `
      <div><b>${esc(r.label || '')}</b></div>
      ${r.why ? `<div class="hint" style="margin-top:8px">${esc(r.why)}</div>` : ''}
      <div class="hint" style="margin-top:8px">${esc(r.note || '')}</div>
      <div class="hint" style="margin-top:8px">跑起来之后日志就在右下角，随时能停。</div>`,
      '开始跑');
    if (!ok) return null;
    r = await api('exec', { body: { ...payload, confirm: true } });
  }
  if (r.error) { toast('没起来', r.error, 'err'); return null; }

  run.id = r.id; run.offset = 0;
  run.label = r.label; run.cmdline = r.cmdline;
  run.payload = payload; run.onDone = opts.onDone || null;
  if (!opts.quiet) { openRunPanel(r.label, r.cmdline); }
  setRunState('running');
  pollRun(opts);
  return r;
}

async function pollRun(opts = {}) {
  clearTimeout(run.timer);
  const tick = async () => {
    if (!run.id) return;
    const d = await api(`log?id=${encodeURIComponent(run.id)}&offset=${run.offset}`).catch(e => ({ error: String(e) }));
    if (d.gone || d.error) { setRunState('failed', d.error || ''); return; }
    if (d.text) {
      const log = $('#runLog');
      // 只在用户本来就贴着底的时候自动滚 —— 他往上翻着看时别把视线拽走
      const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 40;
      log.textContent += d.text;
      if (atBottom) log.scrollTop = log.scrollHeight;
      run.offset = d.offset;
    }
    if (d.state === 'running') {
      setRunState('running');
      run.timer = setTimeout(tick, 1200);
      return;
    }
    const okState = d.state === 'done';
    setRunState(d.state, d.rc !== 0 ? 'rc=' + d.rc : '');
    toast(okState ? '跑完了' : d.state === 'stopped' ? '已停止' : '跑失败了',
      run.label, okState ? 'ok' : 'err');
    if (okState && run.onDone && d.result) run.onDone(d.result);
    if (opts.then) opts.then(d);
  };
  tick();
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
  await resumeRunning();
}

/* 页面重开/刷新后把还在跑的任务接回面板。
   出片动辄二十分钟，中途刷一下页面是很平常的事 —— 以前刷新之后界面上什么都没有，
   进程还在烧 GPU，人既看不到进度也停不掉，只能去终端 kill。 */
async function resumeRunning() {
  if (run.id) return;                       // 本页已经在跟一个任务了，别抢
  const d = await api('execs').catch(() => null);
  const r = ((d && d.runs) || []).find(x => x.state === 'running');
  if (!r) return;
  run.id = r.id; run.offset = 0;
  run.label = r.label; run.cmdline = r.cmdline; run.payload = null;
  openRunPanel(r.label + '　（页面重开后接上的）', r.cmdline);
  setRunState('running');
  pollRun({});
}

/* 最近跑过什么 —— 从 .cache/runs/ 回填的记录。
   出片是十几分钟的事，跑完人往往过一阵才回来看；以前这些记录只在终端里，
   关掉窗口就再也查不到「昨天那条到底 rc 是多少」。
   现在起跑落盘、服务启动回填，这里点开就能回看，还能翻当时的日志。 */
async function showHistory() {
  const d = await api('execs').catch(() => null);
  const rows = ((d && d.runs) || []).filter(x => x.state !== 'running');
  const head = `<div class="modal-head"><b>${ico('hist')} 最近跑过</b>
      <div class="spacer"></div><button class="btn icon" id="mclose">${ico('close')}</button></div>`;
  if (!rows.length) {
    $('#modalBox').innerHTML = head + `<div class="hint">还没有跑完的记录。</div>`;
    return void $('#modal').classList.add('show');
  }
  const label = { done: '完成', failed: '失败', stopped: '已停' };
  $('#modalBox').innerHTML = head
    + `<div class="hist">${rows.slice(0, 12).map(r => `
        <div class="hist-row" data-hist="${esc(r.id)}" data-hist-log="${esc(r.log || '')}">
          <span class="hist-st st-${esc(r.state)}">${esc(label[r.state] || r.state)}</span>
          <span class="hist-lb">${esc(r.label || r.id)}</span>
          <span class="hist-meta">${r.secs ? Math.round(r.secs) + 's' : '—'}${r.rc !== null && r.rc !== undefined ? ' · rc=' + r.rc : ''}</span>
          <span class="hist-at">${esc((r.started || '').slice(5, 16))}</span>
        </div>`).join('')}</div>`
    + `<div class="hint" style="margin-top:10px">点一条看它当时的日志。记录在项目 <span class="mono">.cache/runs/</span> 里，重启看板也还在。</div>`;
  $('#modal').classList.add('show');
}

/* 回放一条历史任务的日志：只读，不接管状态。
   跑完的活儿再点「停止」没有意义，所以这里让停止/再跑都置灰，
   免得人以为自己在操控一个还活着的进程。 */
async function replayLog(tid) {
  if (!tid) return;
  run.id = '';                    // 关键：不给轮询接管的机会
  run.payload = null;
  run.offset = 0;
  openRunPanel('历史记录 · ' + tid, '');
  const d = await api(`log?id=${encodeURIComponent(tid)}&offset=0`).catch(e => ({ error: String(e) }));
  const log = $('#runLog');
  log.textContent = d.text || (d.error ? '读不到日志：' + d.error : '（这条没有日志）');
  log.scrollTop = 0;
  const near = { done: '完成', failed: '失败', stopped: '已停' };
  setRunState(d.state || 'done', '');
  $('#runSub').textContent = (near[d.state] || d.state || '') +
    (d.secs ? ' · 当时用时 ' + Math.round(d.secs) + 's' : '') +
    (d.rc !== undefined && d.rc !== null ? ' · rc=' + d.rc : '');
  $('#runStop').disabled = true;
  $('#runAgain').disabled = true;
  if (d.gone) $('#runHint').textContent = '这条的记录已经不在内存里了，日志文件还在 .cache/runs/';
}

function renderServices() {
  const names = { pipeline: '合成服务', n8n: '编排', comfyui: 'ComfyUI', 'zen-gate': 'LLM 网关' };
  $('#svcBar').innerHTML = Object.entries(state.services).map(([k, v]) =>
    `<div class="svc ${v === 'up' ? 'up' : 'down'}" data-svc="${esc(k)}"
       title="${esc(k)} · ${esc(v)}　点开看它是什么、怎么起"><span class="dot"></span><span>${names[k] || k}</span></div>`).join('')
    // MCP 不是常驻服务（stdio 起停），但「能不能被别的 AI 调用」是必须一眼能看到的事
    + `<div class="svc mcp" data-mcp="1" title="MCP 接口：给别的 AI 调用本项目用"><span class="dot"></span><span>MCP</span></div>`;
}

/* 点服务徽章 → 说清楚它是什么、地址、怎么起。
   以前这几个点子是个纯装饰，点了没反应，看着像坏了。 */
async function showSvc(name) {
  const d = await api('svc?name=' + encodeURIComponent(name));
  if (d.error) return toast('打不开', d.error, 'err');
  const rows = [
    ['状态', d.up ? '在跑' : '没起来'],
    ['地址', d.url],
    ['作用', d.role],
    ['怎么起', d.start],
  ];
  if (d.models !== undefined) rows.push(['模型数', d.models + ' 个' + (d.models ? '' : '（一条都用不了时先查登录态）')]);
  if (d.vram_total) rows.push(['显存', `${d.vram_free} / ${d.vram_total} GB 可用`]);
  $('#modalBox').innerHTML = `<div class="modal-head"><b>${ico('term')} ${esc(d.label)}</b>
      <div class="spacer"></div><button class="btn icon" id="mclose">${ico('close')}</button></div>
    <div class="kv">${rows.map(([k, v]) =>
      `<dt>${esc(k)}</dt><dd class="${String(v).length > 30 ? 'mono' : ''}">${esc(v)}</dd>`).join('')}</div>
    ${d.model_sample?.length ? `<div class="hint" style="margin-top:10px">样例模型：${d.model_sample.map(esc).join('、')}</div>` : ''}
    ${name === 'zen-gate' ? `<div class="hint" style="margin-top:8px;color:var(--warn)">⚠ 免费车道，提示词可能被上游记录 —— 客户台词与档案不要走这里。</div>` : ''}`;
  $('#modal').classList.add('show');
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
        <span class="stage-dot" style="background:${j.delivered ? 'var(--ok)' : j.stage === (j.done || []).length ? 'var(--busy)' : 'var(--warn)'}"></span>
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
  await Promise.all([loadJobs(), loadCard(), loadIpCfg(), loadFonts(),
                     (async () => { state.cfg = await api('config'); })()]);
  await renderTree();
  renderHero();
  await selectSet((state.jobs.find(j => !j.delivered) || state.jobs[0] || {}).id);
}

async function loadFonts() {
  if (state.fonts) return state.fonts;
  state.fonts = await api('fonts').catch(() => ({ fonts: [] }));
  return state.fonts;
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
    renderInspect(), renderSettings(), renderLog(), renderBrief()]);
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
  // j 可能是 {}（刚建的套次还没进索引）—— 直接 .done.length 会整页崩，
  // 崩在 renderHero 里，表现就是「点了建，界面没反应」。默认值必须给。
  const doneCount = (j && j.done || []).length;
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
        <button class="btn sm" id="hNewSet">${ico('plus')} 新套次</button>
        <button class="btn sm" id="hGuard">${ico('shield')} 素材自检</button>
        <button class="btn sm" id="hAlgo">${ico('spark')} 参数预览</button>
        <button class="btn sm primary" id="hCmd">${ico('play')} 开跑（先看计划）</button>
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
      const st = (j.stages || {})[s]?.status || ((j.done || []).includes(s) ? 'ok' : 'pending');
      const cls = st === 'ok' ? 'ok' : st === 'fail' ? 'fail' : (i === cur ? 'cur' : '');
      return `<div class="stage ${cls}" data-stage="${s}" role="button" tabindex="0"
        title="点开看这一步的产物，并拿到「从此处重跑」的命令">
        <div class="knob">${st === 'ok' ? '✓' : st === 'fail' ? '✕' : i + 1}</div>${s}</div>`;
    }).join('')}</div>
    <div class="kv" style="margin-top:14px">
      ${STAGES.filter(s => (j.artifacts || {})[s]?.length).map(s =>
        `<dt>${s}</dt><dd class="mono">${(j.artifacts[s].map(a => esc(a.split('/').pop()))).join('、')}</dd>`).join('')}
      ${(j.missing || []).length ? `<dt style="color:var(--warn)">缺</dt><dd>${j.missing.map(esc).join('、')}</dd>` : ''}
      ${(j.notes || []).length ? `<dt>备注</dt><dd>${j.notes.slice(-3).map(n => esc(n.text)).join('；')}</dd>` : ''}
    </div>
    <div class="hint" style="margin-top:10px">点上面任意一个阶段 → 看这一步的产物、以及「只重做这一段 / 从这里做到最后」的命令。</div>`;
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
  const head = `<div class="modal-head"><b>${esc(name)}</b><div class="spacer"></div>
      <button class="btn icon" id="mcopy" data-src="${encodeURIComponent(p)}">${ico('copy')}</button>
      <button class="btn icon" id="mclose">${ico('close')}</button></div>`;
  if (type === 'video') {
    $('#modalBox').innerHTML = head + `<video controls autoplay src="/api/raw?path=${encodeURIComponent(p)}"></video>`;
  } else if (type === 'audio') {
    // 音频以前落到「不支持预览」那一支 —— 而配音恰恰是最该先听的东西：
    // 定板音频是后面整条流水线的基准，靠文件名判断不了对不对。
    $('#modalBox').innerHTML = head + `<div class="audio-box">
        <div class="wave">${ico('audio', 'ico')}</div>
        <audio controls autoplay preload="metadata" src="/api/raw?path=${encodeURIComponent(p)}"></audio>
        <div class="hint">${esc(typeOf(name))} · 直接拖进度条即可（支持 Range）</div>
      </div>`;
  } else if (type === 'image') {
    $('#modalBox').innerHTML = head + `<img src="/api/raw?path=${encodeURIComponent(p)}">`;
  } else if (type === 'text') {
    $('#modalBox').innerHTML = head + `<pre id="mtext">载入中…</pre>`;
    api('text?path=' + encodeURIComponent(p)).then(t =>
      ($('#mtext').textContent = t.text || t.error || '（空）'));
  } else {
    $('#modalBox').innerHTML = head +
      `<div class="empty">这类文件浏览器放不了，<a href="/api/raw?path=${encodeURIComponent(p)}" download>点此下载</a></div>`;
  }
  $('#modal').classList.add('show');
}

const typeOf = n => {
  const e = (n.split('.').pop() || '').toLowerCase();
  return e ? e.toUpperCase() + ' 文件' : '文件';
};

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
    ['pipeline-fast', '快速试跑（先看计划）', 'spark', 0],
    ['pipeline-std', '正式出片（std 档）', 'play', 1],
    ['prompts-gen', 'AI 生成提示词', 'plus', 0],
    ['prompts-opt', 'AI 优化提示词', 'refresh', 0],
    ['prompts-show', '查看当前提示词', 'eye', -1],
  ];
  const rows = await Promise.all(kinds.map(async ([k, label, ic, canExec]) => {
    const c = await api('cmdline', { body: { kind: k, ip: state.ip, set: state.setId } });
    return { k, label, ic, cmd: c.cmdline || '', canExec };
  }));
  // 断点重做：交付之后最常见的动作就是"只重做后半段"。
  // 步骤名从 config/pipeline.json 实时取 —— 不在 JS 里硬列一遍（列了就会跟真值分叉）。
  const steps = Object.keys((state.cfg && state.cfg.pipeline && state.cfg.pipeline.steps) || {});
  const redo = steps.length ? `
    <div style="margin-top:14px;padding-top:12px;border-top:1px solid var(--line)">
      <div class="hint" style="margin-bottom:8px">
        <b>断点重做</b>：改了某一环（换音色 / 改字幕 / 换 BGM），只想从那里往后跑。
        点「跑」直接开跑（前面已完成的产物保持不动），点「复制」回终端跑。</div>
      <div style="display:flex;flex-wrap:wrap;gap:6px">${steps.map(s => {
        const meta = (state.cfg.pipeline.steps || {})[s] || {};
        const label = meta.name || meta.desc || s;
        return `<span class="btn-group">
          <button class="btn sm" data-exe="redo" data-step="${esc(s)}" title="跑到这里为止之后的全部重做">${ico('play')} ${esc(String(label).slice(0, 8))}</button>
          <button class="btn sm ghost" data-redo="${esc(s)}" title="只复制命令">${ico('copy')}</button></span>`;
      }).join('')}</div>
    </div>` : '';
  $('#cmdBox').innerHTML = `
    <div class="hint" style="margin-bottom:9px">
      这些都能直接跑：点「跑」就在后台执行，日志在右下角实时滚，随时能停。
      想自己盯整段输出，点「复制」回终端也一样。</div>
    ${rows.map(r => `<div style="margin-bottom:8px">
      <div style="display:flex;align-items:center;gap:7px;margin-bottom:4px">
        ${ico(r.ic, 'ico')}<span style="font-size:12px">${r.label}</span>
        <div class="spacer"></div>
        ${r.canExec >= 0 ? `<button class="btn sm ${r.k === 'pipeline-std' ? 'primary' : ''}"
            data-exe="${esc(r.k)}">${ico('play')} 跑</button>` : ''}
        <button class="btn sm ghost" data-copy="${esc(r.cmd)}">${ico('copy')}</button></div>
      <div class="cmdbar"><span class="p">$</span> ${esc(r.cmd)}</div></div>`).join('')}
    ${redo}`;
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
          <button class="btn sm ghost" data-reset-key="${esc(key)}" data-reset-scope="${scope}"
            title="回到上一次 git 提交的版本">${ico('undo')} 默认</button>
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
        ${fontPanel()}
        ${build(W.ip || {}, ipdata, 'ip', 2, '高级（改前请想清楚）')}
        <div class="hint">ip.json 里绝大多数参数界面不可写 —— 采样器、二采模型、画幅这类动一处就要重介整条链路的，只留脚本调用。</div>
        <div class="hint" style="margin-top:8px">
          「默认」按钮 = 回到<strong>上一次 git 提交</strong>的那个值，不是某个出厂常量。
          界面上试过但没提交的改动会被它撤掉，提交过的历史不动（撤前自动备份 ip.json）。</div>
      </div></div></div>`;
  $('#tabSettings').innerHTML = html;
  applyFontPreview();
  applyLayout();
}

/* ---------- 字体：选 + 预览 ---------- */
function fontPanel() {
  const F = state.fonts || { fonts: [] };
  const sub = (state.ipcfg || {}).subtitle || {};
  const list = F.fonts || [];
  // 可烧录的排前面 —— 一堆不能进片子的字体混在里面只会误导
  const sorted = [...list].sort((a, b) => (b.burnable ? 1 : 0) - (a.burnable ? 1 : 0) || a.family.localeCompare(b.family));
  const opt = sel => sorted.map(f =>
    `<option value="${esc(f.family)}" ${f.family === sel ? 'selected' : ''}>${f.burnable ? '✓ ' : ''}${esc(f.family)}</option>`).join('');
  return `<h2 class="sec-title">字幕外观（选完能立刻看字形）</h2>
    <div class="font-panel">
      <label class="field"><span>正文字体 <span class="hint">✓ = 已在精简字体目录里，能真烧进片子</span></span>
        <select id="prevFontCue">${opt(sub.font_cue || '')}</select></label>
      <label class="field"><span>标题字体</span>
        <select id="prevFontTitle">${opt(sub.font_title || '')}</select></label>
      <div class="font-stage" id="fontStage">
        <div class="fs-title" id="fsTitle">二十分钟里做了什么</div>
        <div class="fs-cue" id="fsCue">这套活儿，一个人干完</div>
      </div>
      <label class="field" style="margin-top:10px"><span>正文字号
        <b class="mono" id="fsVal">${esc(sub.fs_cue || 52)}</b>
        <span class="hint">定板画布 1080 宽；预览按比例缩小显示</span></span>
        <input type="range" id="prevFs" min="20" max="120" value="${esc(sub.fs_cue || 52)}">
      </label>
      <div style="display:flex;gap:6px;margin-top:8px">
        <button class="btn sm" id="btnFontApply">${ico('save')} 存进 ip.json</button>
        <span class="hint" style="flex:1;align-self:center">
          存完要重跑 <span class="mono">ass</span> 和 <span class="mono">burn</span> 两步才看得到（见「命令」里的断点重做）。</span>
      </div>
      <div class="hint" style="margin-top:8px;color:var(--warn)">
        ⚠ 只有 ${(F.burn_files || []).length} 个字体文件在精简目录里（libass 给它几百个字体目录会吃爆内存）。
        想用别的字体，先把 ttf 放进 <span class="mono">${esc(F.burn_dir || '02_素材真值/字体_字幕标题')}</span>，别一次塞一堆。
      </div>
    </div>`;
}

function applyFontPreview() {
  const st = $('#fontStage');
  if (!st) return;
  const cue = $('#prevFontCue'), title = $('#prevFontTitle'), fs = $('#prevFs');
  const baseFs = +(fs?.value || 52);
  // 预览按 1080 宽等比缩到当前容器宽 —— 比例对，观感才对；硬给一个 14px 只是"能看见"而已
  const scale = Math.max(0.14, st.clientWidth / 1080);
  const t = $('#fsTitle'), c = $('#fsCue');
  if (t) { t.style.fontFamily = `"${title?.value || ''}"`; t.style.fontSize = (baseFs * 1.55 * scale) + 'px'; }
  if (c) { c.style.fontFamily = `"${cue?.value || ''}"`; c.style.fontSize = (baseFs * scale) + 'px'; }
  if ($('#fsVal')) $('#fsVal').textContent = baseFs;
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

/* ---------- 通用确认框 ---------- */
function confirmBox(title, bodyHtml, yesText = '确认') {
  return new Promise(res => {
    $('#modalBox').innerHTML = `<div class="modal-head"><b>${ico('alert')} ${esc(title)}</b></div>
      <div style="margin:6px 0 14px">${bodyHtml}</div>
      <div style="display:flex;gap:8px;justify-content:flex-end">
        <button class="btn ghost" id="cNo">取消</button>
        <button class="btn danger" id="cYes">${esc(yesText)}</button></div>`;
    $('#modal').classList.add('show');
    const done = v => { closeModal(); res(v); };
    $('#cYes').onclick = () => done(true);
    $('#cNo').onclick = () => done(false);
  });
}

/* ---------- 恢复默认 ---------- */
async function saveReset(scope, key) {
  const where = scope === 'global' ? 'config/pipeline.json' : (state.ip + '/ip.json');
  const ok = await confirmBox('恢复默认', `
    <div><code class="mono">${esc(key)}</code> 会改回<b>上一次 git 提交</b>的值。</div>
    <div class="hint" style="margin-top:8px">文件：<span class="mono">${esc(where)}</span><br>
      改动前会自动备份一份，撤错了还能捞回来。</div>`, '恢复');
  if (!ok) return;
  const r = await api('reset', { body: { scope, key, path: state.ip, confirm: true } });
  if (r.ok) {
    toast(r.unchanged ? '本来就是这个值' : '已恢复',
      r.unchanged ? key : `${key}：${r.old} → ${r.new}`, 'ok');
    await Promise.all([loadIpCfg(), (async () => { state.cfg = await api('config'); })()]);
    renderSettings(); renderInspect(); renderLog();
  } else toast('没恢复成', r.error || '', 'err');
}

/* ---------- 字体保存（三项一起提交，一次确认） ---------- */
async function saveFont() {
  const cue = $('#prevFontCue')?.value, title = $('#prevFontTitle')?.value;
  const fs = +($('#prevFs')?.value || 0);
  const changes = [
    ['subtitle.font_cue', cue],
    ['subtitle.font_title', title],
    ['subtitle.fs_cue', fs],
  ].filter(([k, v]) => String(get(state.ipcfg, k) ?? '') !== String(v ?? ''));
  if (!changes.length) return toast('没变', '和当前值一样', '');

  const rows = changes.map(([k, v], i) => {
    const steps = (state.cfg?.pipeline?.steps || {});
    const order = Object.keys(steps);
    const ass = order.indexOf('ass'), burn = order.indexOf('burn');
    return `<div style="display:flex;gap:8px;padding:4px 0">
      <span class="mono" style="color:var(--accent-2);min-width:150px">${esc(k)}</span>
      <span style="color:var(--tx-3)">${esc(String(get(state.ipcfg, k) ?? '—'))}</span>
      <span>→</span><b>${esc(String(v))}</b></div>`;
  }).join('');
  const ok = await confirmBox('改字幕外观', `
    ${rows}
    <div class="hint" style="margin-top:10px">
      改完不会自动重出片子：要重跑 <span class="mono">ass</span>（重排字幕文件）和
      <span class="mono">burn</span>（烧进画面）两步才看得到效果。
      去「命令」面板最下面的「断点重做」，点「从 ass 起」复制命令。</div>`, '保存');
  if (!ok) return;

  for (const [k, v] of changes) {
    const r = await api('set', { body: { scope: 'ip', key: k, value: v, path: state.ip, confirm: true } });
    if (!r.ok) return toast('第 ' + k + ' 项失败', r.error || '', 'err');
  }
  toast('已存进 ip.json', '记得重跑 ass + burn', 'ok');
  await loadIpCfg();
  renderSettings(); renderInspect(); renderLog();
}

/* ---------- 需求台 ---------- */
async function renderBrief() {
  const box = $('#briefBox');
  if (!state.setId) {
    box.innerHTML = '<div class="empty">先选左侧一个套次 —— 需求是跟着某一套片子走的</div>';
    $('#briefSub').textContent = '';
    return;
  }
  const d = await api('brief?ip=' + encodeURIComponent(state.ip) + '&set=' + encodeURIComponent(state.setId));
  state.brief = d;
  $('#briefSub').textContent = d.exists ? '已写' : '还没写';
  box.innerHTML = `
    <div class="hint" style="margin-bottom:8px">
      开工前先把自己的要求写清楚：讲什么、给谁看、多长、什么语气、绝对不能出现什么。
      这段会存成 <span class="mono">brief.md</span> 放在这套片子旁边 ——
      生成提示词、返工、跟客户对齐，后面每一步都回来看它。
      <b>聊天窗口会关，文件不会。</b></div>
    <textarea id="briefText" class="brief-area"
      placeholder="例：这条讲「一条片子为什么值这个价」，35 秒左右，给犹豫中的准客户看。
语气：平实、有底气，不吹。画面：白衬衫、暖墙、固定机位近景。
必须说到：二十分钟、一条龙、不用客户出镜。不要提：具体报价数字。">${esc(d.text || '')}</textarea>
    <div style="display:flex;gap:6px;margin-top:8px;align-items:center">
      <button class="btn sm" id="btnBriefSave2">${ico('save')} 存需求</button>
      <button class="btn sm primary" id="btnBriefGen">${ico('spark')} 生成提示词</button>
      <span class="hint" style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
        title="${esc(d.path || '')}">${esc((d.path || '').replace(state.ipRoot || '', ''))}</span>
    </div>`;
}

async function saveBrief(thenGen) {
  const ta = $('#briefText');
  if (!ta) return;
  if (!state.setId) return toast('先选套次', '', 'err');
  const r = await api('brief', { body: { ip: state.ip, set: state.setId, text: ta.value } });
  if (!r.ok) return toast('没存成', r.error || '', 'err');
  toast('需求已存', r.path || '', 'ok');
  await renderBrief();
  if (thenGen) await genFromBrief();
}

/* 生成/优化提示词：调 LLM、几十秒起，落盘前必须过红线校验。
   现在直接在界面里跑 —— 但日志不藏起来：被哪条红线拦下、走了哪家车道，都在右下角。

   ★ 点之前先看有没有能切的源。提示词是照着文案切的，没文案就没东西喂模型。
   后端 CLI 也拦（"切不出段：…没有文案 txt，也没有 SRT"），但让人先点一次确认、
   等十几秒再看一句失败，体验上跟"点了没反应"是一回事。 */
async function genPrompts(kind) {
  if (!state.setId) return toast('先选套次', '', 'err');
  await loadPrompts().catch(() => { });
  const src = (state.prompts || {}).source || {};
  if (!src.ok) {
    const dir = (state.prompts || {}).set || state.setId;   // 说完整目录名，别让人去猜
    return toast('这套片子还没有文案',
      `05_文案/${dir}/ 里没有 txt，07_成片 里也没有 srt。`
      + `提示词是照着文案逐段切的 —— 先把文案放进去，再点这里。`, 'err');
  }
  await startExec({ kind, ip: state.ip, set: state.setId }, {
    then: async d => { if (d.state === 'done') { await loadPrompts(); renderPrompts(); } },
  });
}

async function genFromBrief() { return genPrompts('prompts-gen'); }

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
function openTab(name) {
  state.tab = name;
  $$('.tab').forEach(t => t.classList.toggle('on', t.dataset.tab === name));
  ['inspect', 'settings', 'log'].forEach(k =>
    $('#tab' + k[0].toUpperCase() + k.slice(1)).style.display = state.tab === k ? '' : 'none');
  if (name === 'settings') renderSettings();
  if (name === 'log') renderLog();
}

/* ---------- 阶段详情：产物 + 从此处重跑 ---------- */
async function showStage(stage) {
  const j = state.job;
  if (!j) return;
  $('#modalBox').innerHTML = `<div class="modal-head"><b>${ico('hist')} ${esc(stage)}</b>
      <span class="sub">载入中…</span>
      <div class="spacer"></div><button class="btn icon" id="mclose">${ico('close')}</button></div>
    <div class="sk" style="width:70%"></div><div class="sk" style="width:50%;margin-top:8px"></div>`;
  $('#modal').classList.add('show');
  $('#mclose').onclick = closeModal;

  const d = await api('stage?' + qs({ ip: state.ip, id: j.id, stage }));
  if (d.error) { $('#modalBox').querySelector('.sub').textContent = d.error; return; }
  const sub = $('#modalBox').querySelector('.sub');
  sub.textContent = `${d.status === 'ok' ? '已完成' : d.status === 'fail' ? '失败' : '未开始'}`
    + (d.at ? ` · ${d.at}` : '') + (d.by ? ` · ${d.by}` : '');

  const items = (d.items || []).map(it => `
    <div class="stage-art" data-file="${esc(it.path)}" data-type="${esc(it.kind)}">
      <span class="k">${it.kind === 'video' ? '▶' : it.kind === 'audio' ? '♪' : '≡'}</span>
      <span class="n">${esc(it.name)}</span>
      <span class="sz">${it.size ? (it.size / 1024 > 900 ? (it.size / 1048576).toFixed(1) + ' MB' : (it.size / 1024).toFixed(0) + ' KB') : ''} ${esc(it.mtime)}</span>
    </div>`).join('') || `<div class="empty">这一步还没有产物文件</div>`;

  const plans = (d.plans || []).map(p => `
    <div class="plan">
      <div class="plan-h"><b>${esc(p.label)}</b>
        <div class="spacer"></div>
        ${p.exec ? `<button class="btn sm ghost" data-exe="plan-dry" data-plan='${esc(JSON.stringify(p.exec))}'
            title="只算计划，不提交 GPU">${ico('eye')} 看计划</button>` : ''}
        ${p.exec ? `<button class="btn sm primary" data-exe="plan" data-plan='${esc(JSON.stringify(p.exec))}'>${ico('play')} 跑</button>` : ''}
        ${p.run ? `<button class="btn sm" data-quick='${esc(JSON.stringify(p.run))}'>${ico('check')} 执行</button>` : ''}
        <button class="btn sm ghost" data-copy="${esc(p.cmd)}">${ico('copy')}</button></div>
      <div class="hint" style="margin:4px 0 6px">${esc(p.why)}</div>
      <div class="cmdbar"><span class="p">$</span> ${esc(p.cmd)}</div>
    </div>`).join('') || `<div class="empty">这一步没有流水线命令（人工环节）</div>`;

  const next = d.next
    ? `<div class="hint" style="margin-top:10px">下一步是 <b style="color:var(--accent-2)">${esc(d.next.stage)}</b>（${esc(d.next.status)}）。</div>`
    : `<div class="hint" style="margin-top:10px">后面没有未完成的阶段了。</div>`;

  // 交付前自检：齐不齐 + 这条片子的规格够不够格当交付件
  const pre = (d.precheck || []);
  const preHtml = pre.length ? `
    <div class="sec-title" style="margin-top:16px">交付前自检</div>
    ${pre.map(c => `<div class="chk ${c.ok ? 'ok' : 'bad'}">
      <span class="m">${c.ok ? '✓' : '✕'}</span>
      <span class="k">${esc(c.name)}</span>
      <span class="v">${esc(c.detail)}</span></div>`).join('')}
    ${pre.some(c => c.trial) ? `<div class="warnbox">
      这条片子是<b>快速档试跑</b>，分辨率只有目标档的一半左右 —— 它的本意是给流程定性，不是交付件。
      要么先按下面的命令出正式档再交付，要么明确接受用试跑档交付。</div>` : ''}` : '';

  $('#modalBox').innerHTML = `<div class="modal-head"><b>${ico('hist')} ${esc(stage)}</b>
      <span class="sub" style="color:var(--tx-3);font-size:11.5px"></span>
      <div class="spacer"></div><button class="btn icon" id="mclose">${ico('close')}</button></div>
    <div class="sec-title">产物</div>${items}
    ${next}
    ${preHtml}
    <div class="sec-title" style="margin-top:16px">重做</div>
    <div class="hint" style="margin-bottom:8px">
      点「跑」就地执行（日志在右下角，随时能停）；只想拿命令就点复制。</div>
    ${plans}`;
  $('#modal').classList.add('show');
  $('#mclose').onclick = closeModal;
  $('#modalBox').querySelector('.sub').textContent = sub.textContent;
}

/* ---------- LLM 车道选择 ---------- */
async function showLlmPanel() {
  $('#modalBox').innerHTML = `<div class="modal-head"><b>${ico('spark')} LLM 车道</b>
      <span class="sub">载入中…</span>
      <div class="spacer"></div><button class="btn icon" id="mclose">${ico('close')}</button></div>
    <div class="sk" style="width:60%"></div>`;
  $('#modal').classList.add('show');
  $('#mclose').onclick = closeModal;

  const d = await api('llm-channels');
  const rows = (d.channels || []).filter(c => c.enabled);
  const off = (d.channels || []).filter(c => !c.enabled).map(c => c.name);
  const cooling = rows.filter(c => c.state !== '可用').length;

  const row = c => {
    const st = c.state === '可用' ? 'ok' : 'cool';
    return `<div class="chan ${c.prefer ? 'star' : ''}">
      <div class="chan-main">
        <div class="chan-name">${esc(c.name)}
          ${c.vision ? '<span class="tag2">看图</span>' : ''}
          ${c.pool === '免费池' ? '<span class="tag2 warn2">免费池</span>' : ''}
        </div>
        <div class="chan-meta">${esc(c.model)} · 优先级 ${c.priority}
          <span class="dot ${st}"></span>${esc(c.state)}
          ${c.state !== '可用' && c.reason ? ' · ' + esc(c.reason) : ''}
          ${c.warn ? ' · ' + esc(c.warn) : ''}</div>
      </div>
      <button class="btn sm" data-probe="${esc(c.name)}">测一下</button>
      <button class="btn sm ${c.prefer ? 'primary' : 'ghost'}" data-prefer="${esc(c.name)}">
        ${c.prefer ? '★ 首选' : '设为首选'}</button>
    </div>`;
  };

  $('#modalBox').innerHTML = `<div class="modal-head"><b>${ico('spark')} LLM 车道</b>
      <div class="spacer"></div><button class="btn icon" id="mclose">${ico('close')}</button></div>
    <div class="hint" style="margin-bottom:10px">
      首选<b>只是优先，不是只用</b> —— 这条挂了会自动换下一家，不会把流程卡死。
      现在有 ${rows.length} 条启用、${cooling} 条在冷却${d.prefer ? `，首选 <b>${esc(d.prefer)}</b>` : '，还没设首选'}。
    </div>
    <div class="chans">${rows.map(row).join('')}</div>
    ${off.length ? `<div class="hint" style="margin-top:10px">未启用：${off.map(esc).join('、')}（去 <span class="mono">config/providers.json</span> 打开）</div>` : ''}
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px">
      <button class="btn ghost" id="llmClear">清掉首选（按优先级）</button>
      <button class="btn" id="mclose2">关闭</button></div>`;
  $('#modal').classList.add('show');
  $('#mclose').onclick = $('#mclose2').onclick = closeModal;
  $('#llmClear').onclick = async () => {
    await api('llm-prefer', { body: { name: '' } });
    toast('已清掉首选', '之后按 providers.json 的优先级走', 'ok');
    await showLlmPanel();
  };
}

/* ---------- MCP 面板：证明「别的 AI 能调用本项目」 ---------- */
async function showMcpPanel() {
  $('#modalBox').innerHTML = `<div class="modal-head"><b>${ico('term')} MCP 接口</b>
      <span class="sub">正在真握手一次…</span>
      <div class="spacer"></div><button class="btn icon" id="mclose">${ico('close')}</button></div>
    <div class="sk" style="width:70%"></div><div class="sk" style="width:45%;margin-top:8px"></div>`;
  $('#modal').classList.add('show');
  $('#mclose').onclick = closeModal;
  const d = await api('mcp');
  const tools = d.tools || [];
  $('#modalBox').innerHTML = `<div class="modal-head"><b>${ico('term')} MCP 接口</b>
      <span class="sub" style="color:${d.ok ? 'var(--ok)' : 'var(--warn)'};font-size:11.5px">
        ${d.ok ? `握手成功 · ${tools.length} 个工具` : '接不通'}</span>
      <div class="spacer"></div><button class="btn icon" id="mclose">${ico('close')}</button></div>
    <div class="hint" style="margin-bottom:10px">
      MCP 是给<b>别的 AI</b> 用的口子：任何支持 MCP 的客户端（Claude Code / Cursor / Qwen Code）
      挂上它就能查进度、开新套、跑自检，不用人来敲命令，也不依赖这个网页。
      它和 CLI 共用同一批函数，所以不会出现「CLI 说行、MCP 说不行」。
    </div>
    ${d.ok ? `<div class="sec-title">可用工具（${tools.length}）</div>
      <div class="chips">${tools.map(t => `<span class="chip mono">${esc(t)}</span>`).join('')}</div>` :
      `<div class="warnbox">${esc(d.detail || '握手失败')}</div>`}
    <div class="sec-title" style="margin-top:16px">怎么挂上</div>
    <div class="cmdbar" style="margin-bottom:8px"><span class="p">$</span> ${esc(d.register || '')}</div>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px">
      <button class="btn sm" data-copy="${esc(d.register || '')}">${ico('copy')} 复制注册命令</button>
      <button class="btn" id="mclose2">关闭</button></div>`;
  $('#modal').classList.add('show');
  $('#mclose').onclick = $('#mclose2').onclick = closeModal;
}

/* ---------- 建档 / 新套次 ---------- */
/* 建档改成两段：先把人话说完，AI 出草案，人改到自己满意再落盘。
   为什么不直接一次建：档案卡一落盘就进总册，名字还等于目录名，改起来很脏。
   先给草案 = 让判断发生在「不可逆」之前。 */
function showNewCard() {
  $('#modalBox').innerHTML = `<div class="modal-head"><b>${ico('plus')} 新建档案卡</b>
      <span class="sub">第 1 步 · 说清楚是谁</span>
      <div class="spacer"></div><button class="btn icon" id="mclose">${ico('close')}</button></div>
    <label class="field"><span>用你自己的话介绍这个客户（越口语越好）</span>
      <textarea id="ncTalk" class="brief-area" style="min-height:150px"
        placeholder="例：客户是开潮汕牛肉火锅的，馆子叫八合里，老板姓林，四十多岁，不愿意上镜，想让我用 AI 给他做个口播号，一周三条，发抖音和小红书，主要讲牛肉怎么挑、火锅怎么涮，别讲加盟别讲价格，他说话有潮汕口音，语速慢点"></textarea>
      <span class="hint">说清四件事就够：他是谁 / 讲什么 / 发哪里多久一条 / 什么绝对不能碰</span></label>
    <div class="hint" style="margin:10px 0 12px">
      AI 会把它整理成档案卡字段，顺手写两篇人设文档（人设圣经 + 内容方向）放进
      <span class="mono">01_人设与规范/</span>。这两篇东西以后 AI 选题就靠它，
      没有它选题只能瞎编。</div>
    <div style="display:flex;gap:8px;justify-content:flex-end">
      <button class="btn ghost" id="ncManual">我手填</button>
      <button class="btn primary" id="ncAI">${ico('spark')} AI 出草案</button></div>`;
  $('#modal').classList.add('show');
  $('#mclose').onclick = closeModal;
  $('#ncManual').onclick = () => showNewCardManual(null);
  $('#ncAI').onclick = async () => {
    const text = ($('#ncTalk')?.value || '').trim();
    if (text.length < 12) return toast('再多说两句', '至少告诉我他是谁、要做什么号', 'err');
    closeModal();
    // 走执行引擎而不是干等：模型可能先试几条车道才通，那段过程要看得见，
    // 否则用户只会得到一个 20 秒不动的按钮，然后以为坏了。
    await startExec({ kind: 'ai-card-draft', text }, {
      onDone: r => showNewCardDraft(r, text),
      then: d => { if (d.state !== 'done') showNewCardManual(text, true); },
    });
  };
}

/* 第 2 步：草案可改，人设文档可预览 */
function showNewCardDraft(r, rawText) {
  const d = r.draft || {};
  const md = r.persona_md || {};
  const mdNames = Object.keys(md);
  const qsHtml = (r.questions || []).length
    ? `<div class="sec-title" style="margin-top:14px">还该问客户的</div>
       ${r.questions.map(q => `<div class="pseg"><div class="tx">${esc(q)}</div></div>`).join('')}` : '';
  const dupWarn = r.dup
    ? `<div class="warnbox">总册里已经有叫「${esc(d.name)}」的 IP 了，建不了。改个目录名。</div>` : '';

  $('#modalBox').innerHTML = `<div class="modal-head"><b>${ico('spark')} 草案</b>
      <span class="sub" style="color:var(--tx-3);font-size:11.5px">由 ${esc(r.provider || '?')} 整理 · 可改</span>
      <div class="spacer"></div><button class="btn icon" id="mclose">${ico('close')}</button></div>
    ${r.summary ? `<div class="hint" style="margin-bottom:10px">一句话理解：<b style="color:var(--tx-1)">${esc(r.summary)}</b></div>` : ''}
    ${dupWarn}
    <div class="form-grid">
      <label class="field"><span>目录名（= 文件夹名）*</span><input id="ncName" value="${esc(d.name)}"></label>
      <label class="field"><span>口头叫法，逗号分隔 *</span><input id="ncAlias" value="${esc((d.aliases || []).join(','))}">
        <span class="hint">没别名会重复建档</span></label>
      <label class="field"><span>类型</span>
        <select id="ncType">
          <option value="客户" ${d.type !== '本人IP' ? 'selected' : ''}>客户</option>
          <option value="本人IP" ${d.type === '本人IP' ? 'selected' : ''}>本人IP</option>
        </select></label>
      <label class="field"><span>标签（套次前缀）</span><input id="ncTag" value="${esc(d.tag)}"></label>
      <label class="field"><span>定位（一句话）</span><input id="ncRole" value="${esc(d.role)}"></label>
      <label class="field"><span>存储空间名</span><input id="ncSpace" value="${esc(d.space)}"></label>
    </div>

    <div class="sec-title" style="margin-top:16px">需求</div>
    <div class="kv">${Object.entries(d.needs || {}).map(([k, v]) =>
      `<dt>${esc(k)}</dt><dd>${esc(Array.isArray(v) ? v.join('、') : String(v))}</dd>`).join('') || '<dd class="empty">没提炼出需求</dd>'}</div>

    ${(d.redlines || []).length ? `<div class="sec-title" style="margin-top:14px">红线</div>
      ${d.redlines.map(x => `<div class="pseg bad"><div class="tx">${esc(x)}</div></div>`).join('')}` : ''}
    ${(d.habits || []).length ? `<div class="sec-title" style="margin-top:14px">习惯</div>
      ${d.habits.map(x => `<div class="pseg"><div class="tx">${esc(x)}</div></div>`).join('')}` : ''}

    <div class="sec-title" style="margin-top:16px">会写进 01_人设与规范 的文档（${mdNames.length} 篇）</div>
    ${mdNames.map(n => `<details class="mddoc"><summary>${esc(n)} <span class="hint">${String(md[n]).length} 字</span></summary>
      <pre>${esc(md[n])}</pre></details>`).join('') || '<div class="empty">没生成人设文档</div>'}
    ${qsHtml}

    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:16px">
      <button class="btn ghost" id="ncBack">重说一遍</button>
      <button class="btn primary" id="ncGo">${ico('plus')} 确认创建档案</button></div>`;
  $('#modal').classList.add('show');
  $('#mclose').onclick = closeModal;
  $('#ncBack').onclick = () => showNewCardManual(rawText, true);

  $('#ncGo').onclick = async () => {
    const v = id => ($('#' + id)?.value || '').trim();
    if (!v('ncName') || !v('ncAlias') || !v('ncSpace'))
      return toast('还差东西', '名字、别名、空间名必填（没空间名脚本会卡住等输入）', 'err');
    if (!(await confirmBox('确认建档？',
      `<div>会在档案区建整套目录、写 card.json、登记进总册，并写 ${mdNames.length} 篇人设文档。</div>
       <div class="hint" style="margin-top:8px">名字就是目录名，建完再改名很脏 —— 确认无误再继续。</div>`,
      '确认建档'))) return;
    const btn = $('#ncGo');
    btn.disabled = true; btn.textContent = '创建中…';
    const rr = await api('card-create', {
      body: {
        draft: {
          name: v('ncName'), aliases: v('ncAlias').split(/[,，]/).map(s => s.trim()).filter(Boolean),
          type: v('ncType'), tag: v('ncTag'), role: v('ncRole'), space: v('ncSpace'),
          needs: d.needs || {}, redlines: d.redlines || [], habits: d.habits || [],
        },
        persona_md: md,
      }
    });
    btn.disabled = false; btn.textContent = '确认创建档案';
    if (rr.error) return toast('没建成', rr.error + (rr.detail ? '：' + rr.detail : ''), 'err');
    closeModal();
    toast('档案已建', `${rr.name} · 写了 ${(rr.md || []).length} 篇人设文档`, 'ok');
    await boot();
    // 建完直接停在新建的这张卡上 —— 不然人还得自己去左边找，第一反应是「没建成吧」
    await selectIP(rr.name);
    showNextStepHint(rr.name);
  };
}

/* 建完卡之后最该干的一件事是开第一套片。这里不替他点，只把路指到那一格。 */
function showNextStepHint(name) {
  if (!($('#modal')?.classList.contains('show'))) {
    toast('下一步：开第一套片', `${name} → 右上「＋ 新套次」`, '');
  }
}

/* 手填（保留原表单，AI 不可用时也不堵路） */
function showNewCardManual(prefill, fromDraft) {
  $('#modalBox').innerHTML = `<div class="modal-head"><b>${ico('plus')} 新建档案卡</b>
      <span class="sub">手填</span>
      <div class="spacer"></div><button class="btn icon" id="mclose">${ico('close')}</button></div>
    ${fromDraft ? `<label class="field"><span>客户原话（留着，AI 可再整理）</span>
      <textarea id="ncTalk" class="brief-area" style="min-height:96px">${esc(prefill || '')}</textarea></label>` : ''}
    <div class="form-grid">
      <label class="field"><span>名字（= 目录名）*</span>
        <input id="ncName" placeholder="陈洁斌主号"></label>
      <label class="field"><span>口头叫法，逗号分隔 *</span>
        <input id="ncAlias" placeholder="洁斌,主号,陈洁斌"></label>
      <label class="field"><span>类型</span>
        <select id="ncType"><option>客户</option><option>本人IP</option></select></label>
      <label class="field"><span>标签</span><input id="ncTag" placeholder="S / A / B"></label>
      <label class="field"><span>定位（一句话）</span><input id="ncRole" placeholder="AI 口播 · 讲交付真相"></label>
      <label class="field"><span>存储空间名</span><input id="ncSpace" placeholder="陈洁斌主号资产"></label>
    </div>
    <div class="hint" style="margin:8px 0 12px">
      手填只建目录 + 空卡；需求、红线、人设文档都得后面补。想让 AI 顺手写人设，回上一步。</div>
    <div style="display:flex;gap:8px;justify-content:flex-end">
      <button class="btn ghost" id="mclose2">取消</button>
      <button class="btn primary" id="ncGo">建档</button></div>`;
  $('#modal').classList.add('show');
  $('#mclose').onclick = $('#mclose2').onclick = closeModal;
  $('#ncGo').onclick = async () => {
    const v = id => ($('#' + id)?.value || '').trim();
    if (!v('ncName') || !v('ncAlias') || !v('ncSpace'))
      return toast('还差东西', '名字、别名、空间名是必填（不给空间名脚本会卡住等输入）', 'err');
    const args = ['--name', v('ncName'), '--type', v('ncType'), '--tag', v('ncTag') || 'C',
      '--role', v('ncRole') || '', '--aliases', v('ncAlias'), '--space', v('ncSpace')];
    const r = await api('run', { body: { cmd: 'card-new', args } });
    if (r.error) return toast('没建成', r.error, 'err');
    closeModal();
    toast('已提交建档', r.cmdline || '', 'ok');
    await sleep(2500);
    await boot();
    await selectIP(v('ncName'));
  };
}

function showNewSet() {
  // 套次编号的默认值按这个 IP 自己的 tag 走（老王的 M 就别给 S03）
  const tag = (state.ips.find(i => i.name === state.ip) || {}).tag || 'S';
  const used = (state.jobs || []).map(j => j.id);
  let n = used.length + 1;
  while (used.includes(tag + String(n).padStart(2, '0'))) n++;
  const guess = tag + String(n).padStart(2, '0');

  $('#modalBox').innerHTML = `<div class="modal-head"><b>${ico('plus')} 开一套新片</b>
      <span class="sub">${esc(state.ip || '')}</span>
      <div class="spacer"></div><button class="btn icon" id="mclose">${ico('close')}</button></div>
    <div class="form-grid">
      <label class="field"><span>套次编号 *</span><input id="nsId" value="${esc(guess)}">
        <span class="hint">前缀跟着这个 IP 的标签走，别手改前缀</span></label>
      <label class="field"><span>标题 / 主题</span><input id="nsTitle" placeholder="留空就按人设自动出题">
        <span class="hint">标题会写进 job.json，是后面提示词的依据</span></label>
    </div>
    <div style="display:flex;gap:7px;align-items:center;margin:12px 0 4px">
      <button class="btn sm" id="nsIdea">${ico('spark')} AI 想几个选题</button>
      <span class="hint" id="nsIdeaSub">读 01_人设与规范，避开做过的角度</span>
    </div>
    <div id="nsTopics"></div>
    <div class="hint" style="margin:12px 0 12px">
      会在 <span class="mono">${esc(state.ip || '')}</span> 下建这一套的目录，并写进索引。
      建完先去「需求台」把要求写清楚，再生成提示词。</div>
    <div style="display:flex;gap:8px;justify-content:flex-end">
      <button class="btn ghost" id="mclose2">取消</button>
      <button class="btn primary" id="nsGo">建</button></div>`;
  $('#modal').classList.add('show');
  $('#mclose').onclick = $('#mclose2').onclick = closeModal;

  const renderTopics = r => {
    const list = r.topics || [];
    if (!list.length) {
      $('#nsTopics').innerHTML = `<div class="warnbox">没生成出选题${r.dropped?.length ? `（${r.dropped.length} 条被红线拦下）` : ''}</div>`;
      return;
    }
    $('#nsTopics').innerHTML = `<div class="topics">${list.map((t, i) => `
      <div class="topic" data-topic="${i}">
        <div class="t">${esc(t.title)}</div>
        <div class="h">${esc(t.hook)}</div>
        <div class="a">${esc(t.angle)}</div>
      </div>`).join('')}</div>
      ${r.note ? `<div class="hint" style="margin-top:6px">${esc(r.note)}</div>` : ''}
      <div class="hint">点一条填进标题。${r.provider ? `（${esc(r.provider)} 出的）` : ''}</div>`;
    $$('#nsTopics .topic').forEach(el => {
      el.onclick = () => {
        const t = list[+el.dataset.topic];
        $('#nsTitle').value = t.title;
        $$('#nsTopics .topic').forEach(x => x.classList.toggle('on', x === el));
      };
    });
  };

  $('#nsIdea').onclick = async () => {
    $('#nsIdeaSub').textContent = '正在想…（跑了几条车道会在右下角日志里看到）';
    await startExec({ kind: 'ai-topics', ip: state.ip, n: 5, hint: ($('#nsTitle')?.value || '').trim() }, {
      onDone: r => { $('#nsIdeaSub').textContent = `人设 ${r.persona_chars || 0} 字 · 通道 ${r.provider || '?'}`; renderTopics(r); },
      then: d => { if (d.state !== 'done') $('#nsIdeaSub').textContent = '没出题（看日志）'; },
    });
  };

  const createSet = async (id, title) => {
    const b = $('#nsGo');
    if (b) { b.disabled = true; b.textContent = '建中…'; }
    // ★ 标题必须走 --title：job-new 的第二个位置参数是套次名，不是标题。
    //   以前写成位置参数，CLI 直接 "unrecognized arguments" 退出，界面却只当成功 ——
    //   于是「点了建，什么都没发生」。这类错必须看回执，不能只看有没有抛异常。
    const r = await api('run', { body: { cmd: 'job-new', args: [state.ip, id, '--title', title] } });
    if (r.error) { if (b) { b.disabled = false; b.textContent = '建'; } return toast('没建成', r.error, 'err'); }
    // 等 CLI 真跑完再刷新 —— 以前固定 sleep(1500)，机器慢一点就刷出「套次不存在」
    const done = await waitTask(r.task, 20000);
    if (!done || done.state !== 'done' || done.rc !== 0) {
      if (b) { b.disabled = false; b.textContent = '建'; }
      return toast('没建成', (done && done.out || '').slice(-300) || '看运行日志', 'err');
    }
    closeModal();
    toast('已建', `${id} ${title}`, 'ok');
    await loadJobs();
    // 建完必须自己核对一遍：索引里没有这一套，就说明磁盘上没建成，
    // 别把「没建成」当成功 —— 用户下一步点哪都会报「没有这个套次」。
    if (!(state.jobs || []).some(j => j.id === id)) {
      const fix = await api('run', { body: { cmd: 'index', args: ['--write'] } });
      await waitTask(fix.task, 15000);
      await loadJobs();
    }
    await renderTree();
    await selectSet(id);
    openTab('inspect');
  };

  $('#nsGo').onclick = async () => {
    const id = ($('#nsId')?.value || '').trim().toUpperCase();
    if (!id) return toast('还差东西', '套次编号要填', 'err');
    if ((state.jobs || []).some(j => j.id === id)) return toast('这套已经在了', id + ' 换个编号', 'err');
    let title = ($('#nsTitle')?.value || '').trim();

    // 没写标题 → 按人设自动出一条。放这里而不是建完再补：
    // job.json 里标题是后面提示词和人设对齐的依据，空着会一路歪。
    if (!title) {
      const b = $('#nsGo');
      b.disabled = true; b.textContent = '没写标题，正在按人设出题…';
      await startExec({ kind: 'ai-topics', ip: state.ip, n: 1 }, {
        quiet: false,
        onDone: r => {
          const t = (r.topics || [])[0];
          if (!t) return toast('标题还是得有', 'AI 没给出选题，自己写一个吧', 'err');
          toast('自动取了标题', t.title, 'ok');
          createSet(id, t.title);
        },
        then: d => { if (d.state !== 'done') { b.disabled = false; b.textContent = '建'; } },
      });
      return;
    }
    await createSet(id, title);
  };
}

/* 写操作走 /api/run 是异步任务，返回 task id。轮询到它结束再刷新，
   比固定 sleep 靠谱 —— 机器忙的时候 sleep 永远不够久。 */
async function waitTask(tid, timeout = 15000) {
  if (!tid) return;
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    const d = await api('task?id=' + encodeURIComponent(tid)).catch(() => null);
    if (!d || d.state !== 'running') return d;
    await sleep(400);
  }
  return null;
}

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

    const rst = e.target.closest('[data-reset-key]');
    if (rst) return void await saveReset(rst.dataset.resetScope, rst.dataset.resetKey);

    const svc = e.target.closest('[data-svc]');
    if (svc) {
      // 「LLM 网关」这个徽章不跳服务详情 —— 用户想点开的多半是「换一条车道」
      if (svc.dataset.svc === 'zen-gate') return void await showLlmPanel();
      return void await showSvc(svc.dataset.svc);
    }

    if (e.target.closest('[data-mcp]')) return void await showMcpPanel();

    const stageEl = e.target.closest('.stage[data-stage]');
    if (stageEl) return void await showStage(stageEl.dataset.stage);

    const probe = e.target.closest('[data-probe]');
    if (probe) {
      const b = probe;
      b.disabled = true; const old = b.textContent; b.textContent = '测…';
      const r = await api('llm-probe', { body: { name: b.dataset.probe } });
      b.disabled = false; b.textContent = old;
      toast(r.ok ? '这条通' : '这条不通', (r.detail || '').replace(/^[✓✗]\s*/, ''), r.ok ? 'ok' : 'err');
      return;
    }

    const pref = e.target.closest('[data-prefer]');
    if (pref) {
      await api('llm-prefer', { body: { name: pref.dataset.prefer } });
      toast('已设为首选', `${pref.dataset.prefer} —— 挂了会自动换下一家`, 'ok');
      await showLlmPanel();
      return;
    }

    const redo = e.target.closest('[data-redo]');
    if (redo) {
      const c = await api('cmdline', { body: { kind: 'redo-from', ip: state.ip, set: state.setId, step: redo.dataset.redo } });
      if (c.error) return toast('拼不出命令', c.error, 'err');
      copyText(c.cmdline);
      return;
    }

    const tab = e.target.closest('.tab');
    if (tab) return void openTab(tab.dataset.tab);
    if (e.target.closest('#mclose')) return closeModal();
    if (e.target.closest('#mcopy')) return copyText(decodeURIComponent(e.target.closest('#mcopy').dataset.src || ''));
  });

  /* ---------- 长任务：所有「跑」按钮都从这里进 ---------- */
  document.addEventListener('click', async e => {
    const exe = e.target.closest('[data-exe]');
    if (exe) {
      e.preventDefault();
      const kind = exe.dataset.exe;
      if (kind === 'plan' || kind === 'plan-dry') {
        let payload = null;
        try { payload = JSON.parse(exe.dataset.plan); } catch (err) { }
        if (!payload) return toast('这条计划读不出来', '刷新一下再试', 'err');
        // 「看计划」= 加 --dry：把流水线打算跑的步骤和参数打出来，不提交 GPU
        return void await startExec(kind === 'plan-dry' ? { ...payload, dry: true } : payload);
      }
      if (kind === 'redo') {
        return void await startExec({
          kind: 'pipeline', ip: state.ip, set: state.setId, from: exe.dataset.step,
        });
      }
      // 命令区那几行
      const map = {
        'pipeline-fast': { kind: 'pipeline', tier: 'fast', dry: true },
        'pipeline-std': { kind: 'pipeline', tier: 'std' },
        'prompts-gen': { kind: 'prompts-gen' },
        'prompts-opt': { kind: 'prompts-opt' },
      };
      if (map[kind]) {
        return void await startExec({ ...map[kind], ip: state.ip, set: state.setId });
      }
      return;
    }

    // 短动作（改 job.json 之类）：走原来的写白名单，不占运行面板
    const quick = e.target.closest('[data-quick]');
    if (quick) {
      let q = null;
      try { q = JSON.parse(quick.dataset.quick); } catch (err) { }
      if (!q) return;
      const r = await api('run', { body: { cmd: q.cmd, args: q.args } });
      if (r.error) return toast('没执行成', r.error, 'err');
      toast('已提交', r.cmdline || '', 'ok');
      await waitTask(r.task, 15000);
      if (q.cmd === 'job-set') { await loadJobs(); await selectSet(state.setId); }
      await renderOverview();
      return;
    }

    if (e.target.closest('#runClose')) return closeRunPanel();
    if (e.target.closest('#runMin')) return runUI().classList.toggle('min');
    if (e.target.closest('#runCopy')) return copyText(run.cmdline);
    if (e.target.closest('#runHist')) {
      closeModal();
      return void showHistory();
    }
    // 翻历史日志：只读回放，不接管状态（那是过去的事了，别让「停止」以为它还活着）
    const histRow = e.target.closest('.hist-row');
    if (histRow) {
      closeModal();
      return void replayLog(histRow.dataset.hist);
    }
    if (e.target.closest('#runStop')) {
      if (!run.id) return;
      const r = await api('exec-stop', { body: { id: run.id } });
      // 任务可能在你按下之前就跑完了 —— 那时报「已停止」是假话
      if (r.note) toast('没有可停的', r.note, '');
      else toast('已发停止信号', '会先把当前这一段收尾，不会留半成品', '');
      return;
    }
    if (e.target.closest('#runAgain')) {
      if (run.payload) return void await startExec({ ...run.payload, confirm: true });
      return;
    }
  });

  // 字体预览：改一下立刻看字形，不落盘
  document.addEventListener('input', e => {
    if (e.target.id === 'prevFs') applyFontPreview();
  });
  document.addEventListener('change', e => {
    if (e.target.id === 'prevFontCue' || e.target.id === 'prevFontTitle') applyFontPreview();
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
    if (e.target.closest('#btnSettings')) return void openTab('settings');
    if (e.target.closest('#btnNewCard')) return void showNewCard();
    if (e.target.closest('#hNewSet')) return void showNewSet();
    if (e.target.closest('#btnFontApply')) return void saveFont();
    if (e.target.closest('#btnBriefSave') || e.target.closest('#btnBriefSave2')) return void saveBrief(false);
    if (e.target.closest('#btnBriefGen')) return void saveBrief(true);
    if (e.target.closest('#hGuard') || e.target.closest('#btnGuard')) return void runGuard();
    if (e.target.closest('#hAlgo')) return void showAlgo();
    if (e.target.closest('#hCmd')) {
      // 以前这里是「复制命令」。人真正想点的是「开跑」，而且要先看一眼计划。
      return void startExec({ kind: 'pipeline', ip: state.ip, set: state.setId, tier: 'fast', dry: true });
    }
    if (e.target.closest('#btnUp')) return void renderFiles(state.fsParent);
    if (e.target.closest('#btnCmdGen')) {
      return void genPrompts('prompts-gen');
    }
    if (e.target.closest('#btnCmdOpt')) {
      return void genPrompts('prompts-opt');
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
