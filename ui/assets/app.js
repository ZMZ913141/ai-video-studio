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
async function apiForm(path, fd) {
  // 上传走 FormData：boundary 由浏览器自动生成，后端那台零依赖 http.server 自己手撕分段。
  // ★ 千万别手动设 Content-Type —— 少带 boundary，后端就认不出分段在哪，
  //   表现就是「点了上传，什么都没发生」。
  const r = await fetch('/api/' + path, { method: 'POST', body: fd });
  let j;
  try { j = await r.json(); } catch (e) { throw new Error('后端返回的不是 JSON'); }
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

/* 慢操作的「忙碌锁」（2026-10-30 前端审计补的）
   ------------------------------------------------------------
   ★ 为什么必须有：实测点「AI 起草」到出结果要 26 秒，而这 26 秒里按钮既不禁用、
     页面也完全没有「正在跑」的迹象。小白的反应一定是「没点上」→ 再点一下，
     于是同一件事提交两次：双倍 token 费，两份草稿还互相覆盖。
     这里统一三件事：① 按钮立刻变成转圈+在干嘛 ② 期间禁用，双保险防重复提交
     ③ 无论成功失败都还原（放 finally，不然一抛异常按钮就永久卡在禁用态）。 */
async function busyBtn(el, doingText, fn) {
  if (!el) return await fn();
  if (el.dataset.busy === '1') { toast('已经在跑了', '等这一下出结果再说', ''); return undefined; }
  const html0 = el.innerHTML;
  el.dataset.busy = '1';
  el.disabled = true;
  el.style.minWidth = Math.max(el.offsetWidth || 0, 72) + 'px';
  el.innerHTML = `<span class="spin"></span>${esc(doingText)}`;
  try {
    return await fn();
  } finally {
    el.innerHTML = html0;
    el.style.minWidth = '';
    el.disabled = false;
    delete el.dataset.busy;
  }
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
  // retry：失败后后端从日志里读出来的「从哪一步接着跑」。
  // 有它的时候，「再跑一次」就不再是把整条命令重发一遍 —— 那会让人以为
  // 前面二十分钟白跑了。
  retry: null,
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
  // 每次开跑都复位：上一轮的 retry 不该被带到这一轮
  run.retry = null;
  $('#runAgain').textContent = '再跑一次';
  $('#runHint').textContent = '';
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
    if (d.state === 'failed') showResumeHint(d);
    toast(okState ? '跑完了' : d.state === 'stopped' ? '已停止' : '跑失败了',
      run.label, okState ? 'ok' : 'err');
    if (okState && run.onDone && d.result) run.onDone(d.result);
    if (opts.then) opts.then(d);
  };
  tick();
}


/**
 * 失败之后，把「已经跑到哪 / 能不能接着跑」直接摆在面板上。
 *
 * ★ 以前只有一句「跑失败了」+ 一屏要人自己翻的日志。可 pipeline.py 本来就是
 *   断点续跑的：产物在、且比上游新，那一步就跳过。也就是说失败重跑真的只重做
 *   失败那一步 —— 这个事实界面上一个字都没说，于是人默认「再跑一次 = 全部重来」，
 *   宁可干等也不敢点。
 *
 * (diag 由后端从日志里读出来：✓ 完成 / ⏭ 跳过 / ✗ 失败)
 */
function showResumeHint(d) {
  const g = d.diag || {};
  const doneN = (g.done || []).length + (g.skipped || []).length;
  if (d.retry) {
    run.retry = d.retry;
    const btn = $('#runAgain');
    if (btn) {
      btn.disabled = false;
      btn.textContent = g.failed ? `从 ${g.failed} 接着跑` : '接着跑（跳过已完成的）';
    }
  }
  const hint = $('#runHint');
  if (!hint) return;
  if (g.failed) {
    hint.textContent = `停在第 ${g.failed} 步`
      + (doneN ? ` · 前面 ${doneN} 步的产物都还在，接着跑不会重做` : '');
  } else if (doneN) {
    hint.textContent = `中间失败了 · 已有 ${doneN} 步产物可复用`;
  } else {
    hint.textContent = '看不出停在哪一步 —— 往上翻最后一条 ✗';
  }
}

/* ---------- 状态 ---------- */
const STAGES = ['文案', '音频', '视频', '后期', '字幕', '验收', '交付'];
const state = {
  ips: [], services: {}, writable: {}, actions: {}, archive: '',
  ip: null, setId: null, setPath: null, ipRoot: null,
  jobs: [], job: null, card: null, ipcfg: null, cfg: null, prompts: null,
  fs: [], fsPath: null, filter: 'ALL', query: '', tab: 'inspect', algo: null, guard: null,
  persona: null, personaCur: '', inert: {},
  trial: { mode:'manual', enabled:{still:false,sample:false}, status:{}, gatePending:null, running:false },
  // 定帧素材的取法：ai=中文→AI写英文 / polish=润色现有英文 / pick=引用已有素材
  stillMode: 'ai', stillWho: '', stillScene: '', stillPromptEn: '', stillAssets: [],
  dialogue: [],
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
  Object.assign(state, { ips: b.ips || [], services: b.services || {}, writable: b.writable || {},
    actions: b.write_actions || {}, archive: b.archive, inert: b.inert || {} });
  renderServices();
  renderTagFilter();
  initMidTabs();                      // 中栏标签：先建好再选 IP，省得面板先全展开再收起
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
  renderTree(); renderHero();
  // ★ 即使没套次，人设/试片/对话面板也要能渲染（它们内部会检查 state.ip）
  //   以前 id 为空时直接 return，renderPersona()/renderTrial()/renderDialogue() 全被跳过，
  //   导致选了 IP 却看到"空的"。
  if (!id) {
    renderPersona(); renderTrial(); renderDialogue(); renderAudio(); renderMaterials();
    return;
  }
  const [job, root] = await Promise.all([
    api('job?ip=' + encodeURIComponent(state.ip) + '&id=' + encodeURIComponent(id)).catch(() => ({})),
    (async () => { const d = await api('fs?path=' + encodeURIComponent(state.ipRoot + '/07_成片'));
      return (d.items || []).find(x => x.type === 'dir' && x.name.startsWith(id + '_')); })(),
  ]);
  state.job = job && job.id ? job : null;
  state.setPath = root ? root.path : state.ipRoot + '/07_成片';
  await Promise.all([loadPrompts(), renderFiles(), renderOverview(), renderCmd(),
    renderInspect(), renderSettings(), renderLog(), renderBrief(), renderPersona(), renderTrial(), renderDialogue(), renderAudio(), renderMaterials()]);
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

/* 段内字段的中文名。键名是给程序看的，人只看懂「表情/动作/镜头」。 */
const SEG_FIELDS = [['expression', '表情'], ['motion', '动作'], ['camera', '镜头']];

function fmtClock(sec) {
  const s = Math.max(0, Number(sec) || 0);
  const m = Math.floor(s / 60);
  const r = s - m * 60;
  return `${m}:${(r < 10 ? '0' : '') + r.toFixed(1)}`;
}

function renderPrompts() {
  const box = $('#promptsBox'), p = state.prompts || {};
  if (!p.exists) {
    box.innerHTML = `<div class="empty">还没有提示词。<br><span class="mono">${esc(p.dir || '')}/prompts.json</span><br>
      用右上角「AI 生成」拿到命令后回终端跑。</div>`; $('#promptsSub').textContent = '未生成'; return;
  }
  const d = p.data || {};
  const segs = Array.isArray(d) ? d : (d.segments || d.prompts || []);
  const gmap = (p.guard || {}).segments || {};
  const bad = (p.guard || {}).bad || 0;
  $('#promptsSub').textContent = `${segs.length} 段` + (bad ? ` · ${bad} 段没过红线` : '');

  // ★ 先说清楚这份稿子到底进不进片。不说的话，人改完稿子去重跑视频，
  //   十几分钟 GPU 花完、画面一点没变，还以为是自己写错了。
  //
  //   2026-10-29 起这条路是「接通但要多一步」：中文分镜 → prompts.en.json → 出片。
  //   所以界面必须区分三种状态，而不是一句「不进片」糊过去：
  //     ① 没译     → 改了不影响画面，但要给出「译成英文」这条唯一的出路
  //     ② 译了但中文又改过 → 出片用的是**旧英文**（最难查的假片）
  //     ③ 同源     → 才是真的通了
  const w = p.wired || {};
  const en = p.en || {};
  let warn = '';
  if (w.mode === 'conditional') {
    if (!en.exists) {
      warn = `<div class="warnbox" style="margin-bottom:10px">
        <b>这份稿子还没译成英文，所以现在改它不影响画面。</b>
        出片提示词的骨架是英文 ${ico('alert')}：中文不能直塞进去 ——
        不在说话标记里的中文会被模型当成「该上屏的字」（S04 就这么烧过一版字幕）。
        <div class="rowbtns" style="margin-top:8px">
          <button class="btn primary" data-exe="prompts-en">译成 prompts.en.json</button>
        </div>
        <div class="hint" style="margin-top:6px">译完再回来改这里的文字，出片才认。</div>
      </div>`;
    } else if (en.stale) {
      warn = `<div class="warnbox bad" style="margin-bottom:10px">
        <b>中文稿改过，英文那版没跟上。</b>
        现在出片用的是<b>旧的那版英文分镜</b> —— 画面不会反映你刚改的内容。
        <div class="rowbtns" style="margin-top:8px">
          <button class="btn primary" data-exe="prompts-en-force">按现在的中文稿重新译</button>
        </div>
      </div>`;
    } else {
      warn = `<div class="okbox" style="margin-bottom:10px">
        ✓ 英文分镜已就绪（${en.segments} 段 · ${esc(en.updated || '')}${en.provider ? ' · 路由 ' + esc(en.provider) : ''}）：
        这里的每段表情与手势，出片时会按段覆盖默认的 english 描述。
      </div>`;
    }
  } else if (w.on === false) {
    warn = `<div class="warnbox" style="margin-bottom:10px">
      <b>这份分镜稿目前不进片。</b>出片用的是 ${esc(p.wired.what)}。
      所以改这里的文字<b>不会改变画面</b>；想看新画面要用「换一个种子」重跑，或者改 ip.json 里描述画面的那几项。
      <div class="hint" style="margin-top:6px">为什么不能直接接：${esc(p.wired.why)}</div>
    </div>`;
  }

  const gp = state.cfg?.pipeline?.global_prompt?.enabled
    ? `<div class="pseg"><div class="hd">${ico('shield')} 全局通用片段
        <span class="tag">每段都带，不必重复写</span></div>
        <div class="tx">${esc(state.cfg.pipeline.global_prompt.prefix)}</div></div>` : '';

  if (!segs.length) { box.innerHTML = warn + gp + '<div class="empty">文件里没找到分段</div>'; return; }

  box.innerHTML = warn + gp + segs.map((raw, i) => {
    const s = typeof raw === 'string' ? { prompt: raw } : raw;
    const n = s.i ?? (i + 1);
    const gi = gmap[String(n)] || {};
    const hasT = s.t0 !== undefined && s.t1 !== undefined;
    const dur = hasT ? (Number(s.t1) - Number(s.t0)) : null;
    const kv = SEG_FIELDS.filter(([k]) => s[k]).map(([k, label]) =>
      `<div class="kv"><span class="k">${label}</span><span class="v">${esc(String(s[k]))}</span></div>`).join('');
    const mark = gi.ok === false
      ? `<span class="chip bad">未过红线</span>`
      : gi.ok ? `<span class="chip ok">过线</span>` : '';
    const iss = (gi.issues || []).length
      ? `<div class="issues">${gi.issues.map(x => esc(String(x))).join('；')}</div>` : '';
    const text = s.prompt || s.text || '';
    return `<div class="pseg ${gi.ok === false ? 'bad' : ''}" data-seg="${n}">
      <div class="hd"><b>#${n}</b>
        ${hasT ? `<span class="mono">${fmtClock(s.t0)} – ${fmtClock(s.t1)}</span>` : ''}
        ${dur ? `<span class="tag">${dur.toFixed(1)}s</span>` : ''}
        ${s.manual ? `<span class="tag">手改过</span>` : ''}
        <div class="spacer"></div>${mark}
        <button class="btn sm ghost" data-pset="${n}" title="改这一段并保存（过红线才会写进文件）">改</button>
      </div>
      ${s.line ? `<div class="line">${esc(String(s.line))}</div>` : ''}
      <div class="tx">${esc(text) || '<span style="color:var(--tx-3)">（这段还没有提示词）</span>'}</div>
      ${kv}${iss}
      <div class="pedit" hidden>
        <textarea spellcheck="false">${esc(text)}</textarea>
        <div class="hint" style="margin:6px 0 7px">
          只改这一段的画面描述。写否定句（「不要皱眉」「避免抖动」）会被红线挡住 —— 要写正向的
          「眉部放松」「面部肌肉松弛」。</div>
        <div style="display:flex;gap:6px">
          <button class="btn sm primary" data-psave="${n}">保存</button>
          <button class="btn sm ghost" data-pcancel="${n}">取消</button>
          <div class="spacer"></div>
          <span class="hint" data-pmsg="${n}"></span>
        </div>
      </div>
    </div>`;
  }).join('');
}

async function savePromptSeg(n) {
  const card = document.querySelector(`.pseg[data-seg="${n}"]`);
  if (!card) return;
  const ta = card.querySelector('.pedit textarea');
  const msg = card.querySelector(`[data-pmsg="${n}"]`);
  const txt = (ta.value || '').trim();
  if (!txt) { msg.textContent = '空的不存'; return; }
  msg.textContent = '校验中…';
  await startExec({ kind: 'prompts-set', ip: state.ip, set: state.setId, seg: n, text: txt }, {
    // 必须看退出码：红线没过时命令是「正常退出但 rc=1」，只看有没有拿到任务 id
    // 会把「没落盘」当成功 —— 人改完看到已保存、回头发现文件还是旧的。
    then: async d => {
      if (d && d.state === 'done' && d.rc === 0) {
        await loadPrompts();
        renderPrompts();
        toast('第 ' + n + ' 段已更新', '要重跑视频这一步才会生效', 'ok');
      } else {
        const m2 = document.querySelector(`[data-pmsg="${n}"]`);
        if (m2) m2.textContent = '没存：还没过红线，原因看右下角运行日志';
      }
    },
  });
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
    ['prompts-en', '提示词译成英文（出片才认这份）', 'note', 0],
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
      // ★ 音色语气指令空着 → TTS 拿到的就是空字符串，音色是模型默认值，
      //   跟人设圣经里写的声音真值对不上（不报错、不崩，片子照出）。
      //   所以这一项给个「AI 设计」的入口：按人设推一条出来。
      const aiBtn = (key === 'voice.tts.instruction' && scope === 'ip')
        ? `<button class="btn sm" data-voice-design="1" title="按人设圣经里的声音真值推一条音色语气指令">${ico('spark')} AI 设计</button>` : '';
      const emptyWarn = (key === 'voice.tts.instruction' && scope === 'ip' && !String(cur || '').trim())
        ? `<div class="hint" style="color:var(--warn)">现在是空的 —— TTS 会用模型默认音色，跟人设写的声音真值对不上。</div>` : '';
      return `<label class="field"><span>${esc(m.label)} <code class="mono" style="color:var(--tx-3)">${esc(key)}</code></span>${ctrl}
        ${emptyWarn}
        <div style="display:flex;gap:6px;margin-top:6px;align-items:center">
          <span class="hint" style="flex:1">现为 <b class="mono">${esc(typeof cur === 'object' ? JSON.stringify(cur) : String(cur))}</b>${m.tier === 2 ? ' · 高级' : ''}</span>
          ${aiBtn}
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
        ${inertPanel()}
        <div class="hint">ip.json 里绝大多数参数界面不可写 —— 采样器、二采模型、画幅这类动一处就要重介整条链路的，只留脚本调用。</div>
        <div class="hint" style="margin-top:8px">
          「默认」按钮 = 回到<strong>上一次 git 提交</strong>的那个值，不是某个出厂常量。
          界面上试过但没提交的改动会被它撤掉，提交过的历史不动（撤前自动备份 ip.json）。</div>
      </div></div></div>`;
  $('#tabSettings').innerHTML = html;
  applyFontPreview();
  applyLayout();
}

/* ip.json 里存着、但没有脚本读的那些字段。
   ★ 以前它们是能编辑的输入框 —— 人改了、界面显示改了，出片一点没变。
     这比没有这个参数更糟：界面在骗人。
     现在只读展示，并写清楚「为什么不生效」。 */
function inertPanel() {
  const inert = (state.inert || {});
  const keys = Object.keys(inert);
  if (!keys.length) return '';
  const ipdata = state.ipcfg || {};
  return `<h2 class="sec-title">存着但不生效（只读）</h2>
    <div class="hint" style="margin-bottom:6px">
      这些字段在 ip.json 里，但<strong>没有任何生产脚本读它们</strong>。
      改了不起作用 —— 所以这里不给输入框。要让它们真生效，得在引擎里接上。</div>
    ${keys.map(k => {
      const cur = get(ipdata, k);
      const shown = (cur === null || cur === undefined || cur === '') ? '（null）' : String(cur);
      return `<div class="field" style="opacity:.75">
        <span>${esc(k)} <code class="mono" style="color:var(--tx-3)">${esc(shown)}</code></span>
        <div class="hint" style="color:var(--warn)">${esc(inert[k])}</div>
      </div>`;
    }).join('')}`;
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
  // ★ 空值在本地就拦掉，别让请求飞出去再让后端骂人。
  //   2026-10-29 实测：清空输入框点保存，界面弹的是 Python 的
  //   "could not convert string to float: ''"——用户完全看不懂。
  if (String(value).trim() === '') {
    elv.classList.add('bad');
    elv.focus();
    toast('这里不能空', '要恢复原来的值，点右边的「默认」', 'err');
    setTimeout(() => elv.classList.remove('bad'), 1600);
    return;
  }
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

/* 问一句话（返回字符串 / null）。confirmBox 是纯确认，它会把 DOM 关掉再返回，
   输入框里的值拿不到，所以这里单开一个 —— 另存人设要一个文件名。 */
function askText(title, hint, defVal = '', yesText = '确定') {
  return new Promise(res => {
    $('#modalBox').innerHTML = `<div class="modal-head"><b>${esc(title)}</b></div>
      <div class="hint" style="margin:6px 0 8px">${esc(hint)}</div>
      <input class="inp" id="askVal" style="width:100%" value="${esc(defVal)}">
      <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px">
        <button class="btn ghost" id="aNo">取消</button>
        <button class="btn primary" id="aYes">${esc(yesText)}</button></div>`;
    $('#modal').classList.add('show');
    const el = $('#askVal');
    el.focus(); el.select();
    const done = v => { closeModal(); res(v); };
    $('#aYes').onclick = () => done((el.value || '').trim() || null);
    $('#aNo').onclick = () => done(null);
    el.onkeydown = e => { if (e.key === 'Enter') { e.preventDefault(); done((el.value || '').trim() || null); } };
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

/* ---------- 音色语气：AI 设计 ---------- */
async function designVoice() {
  if (!state.ip) return toast('先选 IP', '', 'err');
  toast('正在按人设推音色…', '要调一次大模型，几秒到几十秒', '');
  const r = await api('voice-design', { body: { ip: state.ip } });
  if (r.error) return toast('没设计出来', r.error, 'err');
  const alts = (r.alternatives || []).map(a =>
    `<div class="pseg"><div class="tx">${esc(a)}</div></div>`).join('');
  const negWarn = (r.has_negative || []).length
    ? `<div class="warnbox">含否定词 ${(r.has_negative || []).join('、')} ——
        TTS 对否定句处理不可靠（"不要太快" 常常出来就是快的）。
        建议改成正向描述：写"语速偏慢"而不是"不要太快"。</div>` : '';
  $('#modalBox').innerHTML = `<div class="modal-head"><b>${ico('spark')} 音色语气指令</b>
      <span class="sub">${esc(r.provider || '?')}</span>
      <div class="spacer"></div><button class="btn icon" id="mclose">${ico('close')}</button></div>
    ${negWarn}
    <div class="sec-title">建议</div>
    <div class="pseg"><div class="tx">${esc(r.instruction || '')}</div></div>
    <div class="hint" style="margin-top:6px">${esc(r.rationale || '')}</div>
    ${alts ? `<div class="sec-title" style="margin-top:14px">备选</div>${alts}` : ''}
    <div class="hint" style="margin-top:14px">
      存进 <span class="mono">ip.json</span> 的 <span class="mono">voice.tts.instruction</span>，
      这是音色指令的<b>唯一真源</b>，TTS 直接读它。
      改了要重跑配音（<span class="mono">tts</span> 那一步）才生效。</div>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:16px;flex-wrap:wrap">
      <button class="btn ghost" id="vdCancel">不用了</button>
      <button class="btn primary" id="vdSave">${ico('save')} 存进 ip.json</button></div>`;
  $('#modal').classList.add('show');
  $('#mclose').onclick = closeModal;
  $('#vdCancel').onclick = closeModal;
  $('#vdSave').onclick = async () => {
    const rr = await api('voice-design', { body: { ip: state.ip, save: true } });
    if (rr.error) return toast('没存成', rr.error, 'err');
    toast('音色指令已存', '重跑配音那一步就生效', 'ok');
    closeModal();
    await loadIpCfg();
    renderSettings(); renderInspect(); renderLog();
  };
}

/* ---------- 本套素材：客户给的新素材 + 每段用哪张 ----------
   ★ 为什么要单开一块：IP 级素材（02_素材真值/ 根下）回答的是「这个人长什么样」，
     一套片子一张静帧就够。但客户常常是**一条片子给一批新素材**（门店、产品、
     不同场景），还想第 2 段用 A、第 3 段用 B。
     H3 原生支持这件事：每段最多 9 张参考图，且 `<Picture N>` 是**段内**重新
     从 1 编号 —— 所以出片提示词骨架一个字都不用改。规范见
     docs/H3时间轴与分段素材规范.md（从插件源码提取的，不是猜的）。 */

async function renderMaterials() {
  const box = $('#matBox');
  if (!box) return;
  if (!state.ip) { box.innerHTML = '<div class="empty">先选一个 IP</div>'; return; }
  if (!state.setId) {
    box.innerHTML = '<div class="empty">先选一套片子 —— 素材是跟着套次走的</div>';
    return;
  }
  const q = 'ip=' + encodeURIComponent(state.ip) + '&set=' + encodeURIComponent(state.setId);
  const d = await api('set-materials?' + q).catch(() => null);
  if (!d || d.error) {
    box.innerHTML = '<div class="empty">读不到素材清单：' + esc((d && d.error) || '接口没响应') + '</div>';
    return;
  }

  const mats = d.materials || {};
  const mine = mats.images || [];
  const segMap = mats.segments || {};
  const segs = d.segments || [];
  const files = d.items || [];
  const ipImgs = d.ip_images || [];
  const sub = $('#matSub');
  if (sub) sub.textContent = mine.length ? `本套 ${mine.length} 张` : '还没建清单';

  // 文件 ↔ id 的对应靠 file 后缀匹配（清单里存的是相对路径）
  const idOf = f => ((mine.find(x => String(x.file || '').endsWith('/' + f.name)) || {}).id) || '';

  const fileRows = files.length ? files.map(f => `
    <div class="mat">
      <span class="mn" title="${esc(f.name)}">${esc(f.name)}</span>
      <span class="ms">${esc(idOf(f) || '未登记')}</span>
      <span class="ms">${esc(f.mtime)}</span>
      <img src="/api/raw?path=${encodeURIComponent(f.path)}" alt=""
        style="width:38px;height:56px;object-fit:cover;border-radius:6px">
    </div>`).join('') : '<div class="empty">这个套次还没有自己的素材。上传几张，或从下面「IP 素材」里挑。</div>';

  // AI 上次分配时给的理由 —— 看不见理由，人就不会信这个分配
  const aiWhy = (mats._ai && mats._ai.reasons) || {};
  const aiAt = (mats._ai && mats._ai.at) || '';
  const aiNotes = (mats._ai && mats._ai.notes) || [];

  const segRows = segs.length ? segs.map(s => {
    const cur = segMap[String(s.i)] || [];
    const opts = [['img', '起始静帧']].concat(mine.map(x => [x.id, x.id]));
    const why = aiWhy[String(s.i)] || '';
    return `<div class="mat-seg">
      <div class="ms-head"><b>段 ${esc(String(s.i))}</b>
        <span class="hint">${esc(String(s.t0))}–${esc(String(s.t1))}s</span>
        <span class="hint" style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(s.line || '')}</span></div>
      <div class="ms-body">${opts.map(([v, l], k) => `
        <label class="chk"><input type="checkbox" data-seg="${esc(String(s.i))}" value="${esc(v)}"
          ${(cur.includes(v) || (!cur.length && k === 0)) ? 'checked' : ''}>
          ${k === 0 ? esc(l) : `Picture ${k + 1} · ${esc(l)}`}</label>`).join('')}</div>
      ${why ? `<div class="mat-why">${ico('spark')} ${esc(why)}</div>` : ''}
    </div>`;
  }).join('') : '<div class="empty">这套还没有分镜（prompts.json）。先出提示词，再来分配素材。</div>';

  box.innerHTML = `
    <div class="hint" style="margin-bottom:8px">
      把客户给的所有素材传进来，点 <b>AI 自动分配</b> —— 它会逐张看懂素材，
      再按每段文案分镜匹配，并且<b>给每段写一句理由</b>。不满意就在下面改，
      改哪段都行。<b>没建清单时整片只用起始静帧那一张</b>，跟以前完全一样。
    </div>
    <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-bottom:10px">
      <input type="file" id="matFile" accept="image/*" multiple style="display:none">
      <button class="btn sm primary" id="btnMatAI">${ico('spark')} AI 自动分配</button>
      <button class="btn sm" id="btnMatUpload">${ico('plus')} 上传素材</button>
      <button class="btn sm" id="btnMatRefresh">${ico('refresh')} 刷新</button>
      <span class="hint" id="matUpSub">${files.length ? files.length + ' 张' : ''}${aiAt ? ' · AI 上次分配于 ' + esc(aiAt) : ''}</span>
    </div>
    ${aiNotes.length ? `<div class="mat-notes">${aiNotes.map(n => '· ' + esc(n)).join('<br>')}</div>` : ''}

    <div class="sec-title">本套素材文件（${files.length}）</div>
    <div class="mat-list">${fileRows}</div>

    ${ipImgs.length ? `<div class="sec-title" style="margin-top:12px">从 IP 素材里引用（不复制文件）</div>
      <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">
        <select id="matIpsel" class="inp" style="max-width:320px">
          ${ipImgs.map(x => `<option value="${esc(x.rel)}">${esc(x.name)}</option>`).join('')}
        </select>
        <button class="btn sm" id="btnMatRefIp">${ico('plus')} 加进本套</button>
        <span class="hint">已有的图别重传一遍 —— 直接引用，改的是指针不是文件</span>
      </div>` : ''}

    <div class="sec-title" style="margin-top:14px">每段用哪张</div>
    ${segRows}

    <div style="display:flex;gap:8px;margin-top:12px;align-items:center;flex-wrap:wrap">
      <button class="btn primary" id="btnMatSave">${ico('save')} 存素材分配</button>
      <span class="hint">存完跑出片流水线的 <span class="mono">video</span> 那一步才生效；
        素材也算「人改过的源文件」，改了会触发重做画面</span>
    </div>`;

  const collect = () => {
    const segdoc = {};
    box.querySelectorAll('input[data-seg]').forEach(cb => {
      if (!cb.checked) return;
      const k = cb.dataset.seg;
      (segdoc[k] = segdoc[k] || []).push(cb.value);
    });
    return { set: d.set, images: mine, segments: segdoc };
  };

  $('#btnMatRefresh').onclick = () => renderMaterials();
  // AI 自动分配：逐张看图打标 + 按分镜匹配。第一次要调视觉模型，20 秒上下，
  // 所以必须挂忙碌锁 —— 不然人以为按钮坏了（见 busyBtn 那条注释）。
  $('#btnMatAI').onclick = () => busyBtn(
    $('#btnMatAI'), 'AI 看懂素材中…', async () => {
      const r = await api('assets-match',
        { body: { ip: state.ip, set: state.setId } });
      if (r.error) { toast('分配失败：' + r.error); return; }
      const n = Object.keys(r.segments || {}).length;
      const tag = (r.notes || []).length ? `（${r.notes.length} 条提醒）` : '';
      toast(`已分配 ${n} 段${tag}`);
      await renderMaterials();
    });
  $('#btnMatUpload').onclick = () => $('#matFile').click();
  $('#matFile').onchange = async e => {
    const list = [...(e.target.files || [])];
    e.target.value = '';
    if (!list.length) return;
    const tip = $('#matUpSub');
    if (tip) tip.textContent = '正在上传…';
    const fd = new FormData();
    for (const f of list) fd.append('file', f);
    try {
      const up = await apiForm('material-upload', fd);
      if (up.error) { if (tip) tip.textContent = '没传上去：' + up.error; return; }
      const r2 = await api('set-materials-install',
        { body: { ip: state.ip, set: state.setId, uid: up.uid } });
      if (r2.error) { if (tip) tip.textContent = '入库失败：' + r2.error; return; }
      toast('素材已入库', `${(r2.moved || []).length} 张进 02_素材真值/${esc(d.set.split('_')[0])}/`, 'ok');
      await renderMaterials();
    } catch (ex) {
      if (tip) tip.textContent = '上传失败：' + ex.message;
    }
  };
  const refIp = $('#btnMatRefIp');
  if (refIp) refIp.onclick = async () => {
    const sel = $('#matIpsel');
    const rel = sel && sel.value;
    if (!rel) return;
    const nm = rel.split('/').pop();
    const stem = nm.replace(/\.[^.]+$/, '').replace(/[^\w\u4e00-\u9fff-]/g, '_').slice(0, 24);
    if (mine.some(x => x.file === rel)) return toast('已经在清单里了', nm, 'ok');
    const doc = { set: d.set, images: mine.concat([{ id: stem, file: rel, note: '' }]),
                  segments: segMap };
    const r = await api('materials-save', { body: { ip: state.ip, set: state.setId, materials: doc } });
    if (r.error) return toast('没存成', r.error, 'err');
    toast('已引用', nm, 'ok');
    await renderMaterials();
  };
  $('#btnMatSave').onclick = async () => {
    const r = await api('materials-save',
      { body: { ip: state.ip, set: state.setId, materials: collect() } });
    if (r.error) return toast('没存成', r.error, 'err');
    toast('素材分配已存', `${r.images} 张素材 / ${r.segments} 段有指定`, 'ok');
    await renderMaterials();
  };
}

/* ---------- 声音：配音（TTS）+ 配乐（BGM）----------
   为什么这两块放一个面板：它们都是「重抽最多」的环节，而且改完都要回头听。
   分开摆两个面板，中间栏又要多滚一屏。 */

const BGM_SRC = [
  ['reuse',   '复用已有'],
  ['synth',   '本地合成'],
  ['comfy',   'ComfyUI 生成'],
  ['library', '本地曲库'],
  ['online',  '在线曲库'],
];
const BGM_HINT = {
  reuse:   '直接拿配乐库里已经攒下的那一首，几秒完事、不占显卡。库里空的就先合成或生成一首 —— 生成完会自动沉淀进库。',
  synth:   '88 BPM 温暖器乐，几秒出、不占显卡、每次一样。缺点：永远这一首。',
  comfy:   '本机 MiniMax-Music-3 三件套齐全，按描述生乐。占显卡约 10G，一次几分钟。',
  library: '你已经有的曲子。先在下面填曲库目录；勾「随机」就每套换一首。',
  online:  'Jamendo / Freesound 有 API。⚠ Pexels 和 Pixabay 的 API 只有图片视频、没有音乐。',
};

async function renderAudio() {
  const box = $('#audioBox');
  if (!box) return;
  if (!state.ip) { box.innerHTML = '<div class="empty">先选一个 IP</div>'; return; }

  const cfg = state.ipcfg || await api('ipconfig?ip=' + encodeURIComponent(state.ip)).catch(() => ({}));
  const bgm = cfg.bgm || {};
  const tts = (((cfg.voice || {}).tts) || {});
  const src = bgm.source || 'synth';

  // 配音候选：跟着套次走
  let ttsBlock = '<div class="empty">先左播选一套片子 —— 配音是跟着某套走的</div>';
  if (state.setId) {
    const d = await api('audio-list?ip=' + encodeURIComponent(state.ip) +
                        '&set=' + encodeURIComponent(state.setId)).catch(() => null);
    const items = (d && d.items) || [];
    $('#audioSub').textContent = items.length ? `${items.length} 条配音候选` : '还没配音';
    ttsBlock = `
      <div class="hint" style="margin-bottom:6px">改了语气或换了稿子之后，点「只重做配音」重抽 ——
        不必整套片子重出一遍。</div>
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px">
        <button class="btn sm primary" id="btnTtsRedoAudio">${ico('play')} 只重做配音（不重出画面）</button>
        <button class="btn sm" id="btnTtsRedoAll">${ico('play')} 从配音开始，后面全重做</button>
      </div>
      <textarea id="ttsInstr" class="brief-area" style="min-height:56px"
        placeholder="例：用热情友好、有活力的语气，亲切自然，带笑容">${esc(tts.instruction || '')}</textarea>
      <div style="display:flex;gap:6px;margin-top:6px;align-items:center;flex-wrap:wrap">
        <button class="btn sm" id="btnTtsInstrSave">${ico('save')} 存语气指令</button>
        <span class="hint">种子 ${esc(String(tts.seed ?? '—'))} · 存完要点上面的「重做配音」才生效</span>
      </div>
      ${items.length ? `<div class="sec-title" style="margin-top:10px">候选（新的在上，可直接试听）</div>
        <div class="mat-list">` + items.slice(0, 8).map(f => `
          <div class="mat">
            <span class="mn" title="${esc(f.name)}">${esc(f.name)}</span>
            <span class="ms">${f.mtime_str}</span>
            <audio controls preload="none" src="/api/raw?path=${encodeURIComponent(f.path)}"></audio>
          </div>`).join('') + `</div>`
        : '<div class="empty">这套还没出过配音。</div>'}`;
  }

  // 配乐库：复用那条路要能看见「库里有什么、现在用的是哪首」。
  // 没有这个列表，人只能猜「上次那首叫啥」，于是每次都重新生成一遍。
  let libBlock = '', libN = 0;
  if (src === 'reuse') {
    const L = await api('bgm-library?ip=' + encodeURIComponent(state.ip)).catch(() => null);
    const li = (L && L.items) || [];
    libN = li.length;
    libBlock = li.length ? `
      <div class="hint" style="margin-bottom:6px">当前用：
        <span class="mono">${esc(bgm.pick || '（库里最新的一首）')}</span> —— 点「用这首」就定下来，以后出片直接拿它。</div>
      <div class="mat-list">${li.map(it => `
        <div class="mat">
          <span class="mn" title="${esc(it.name)}">${esc(it.name)}</span>
          <span class="ms">${esc(it.source || '—')}${it.seconds ? ' · ' + esc(String(it.seconds)) + 's' : ''}</span>
          <audio controls preload="none" src="/api/raw?path=${encodeURIComponent(it.path)}"></audio>
          <button class="btn sm" data-bgmpick="${esc(it.name)}">用这首</button>
        </div>`).join('')}</div>`
      : `<div class="empty">配乐库还是空的。先切到「本地合成」或「ComfyUI 生成」跑一次 ——
          生成完那首会自动沉淀进 <span class="mono">02_素材真值/配乐/</span>，之后一直能复用。</div>`;
  }

  box.innerHTML = `
    <div class="sec-title">配音 TTS</div>
    ${ttsBlock}

    <div class="sec-title" style="margin-top:14px">配乐来源</div>
    <div class="hint" style="margin-bottom:6px">配乐不再是只有那一首。选一条路，改完重跑出片流水线的
      <span class="mono">bgm</span> 那一步就换了。</div>
    <div class="seg" id="bgmSource">
      ${BGM_SRC.map(([v, l]) => `
        <label class="seg-i ${src === v ? 'on' : ''}">
          <input type="radio" name="bgsrc" value="${v}" ${src === v ? 'checked' : ''}>${l}</label>`).join('')}
    </div>
    <div class="hint" style="margin-top:6px">${BGM_HINT[src] || ''}</div>

    ${['comfy', 'online'].includes(src) ? `
      <div class="hint" style="margin-top:8px">配乐怎么描述（英文，这条路必填）</div>
      <textarea id="bgmPrompt" class="brief-area" style="min-height:54px"
        placeholder="例：warm minimal fingerstyle guitar and soft upright bass, calm, no drums, no vocals">${esc(bgm.prompt || '')}</textarea>` : ''}
    ${src === 'reuse' ? `
      <div class="sec-title" style="margin-top:10px">配乐库${libN ? `（${libN} 首）` : ''}</div>
      ${libBlock}` : ''}
    <div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap">
      <button class="btn sm" id="btnBgmSave">${ico('save')} 存配乐参数</button>
      ${src === 'library' ? `<button class="btn sm" id="btnBgmRandom">${ico('spark')} 换一首随机的</button>` : ''}
      ${src === 'reuse' ? '' : `<button class="btn sm primary" id="btnBgmProbe">${ico('play')} 试听 12 秒</button>`}
    </div>
    ${src === 'library' ? `
      <label class="field" style="margin-top:8px"><span>本地曲库目录</span>
        <input id="bgmDir" class="inp" value="${esc(bgm.library_dir || '')}"
          placeholder="/你的/配乐库"></label>
      <label class="chk"><input type="checkbox" id="bgmRand" ${bgm.random ? 'checked' : ''}>每套片子随机换一首</label>` : ''}
    ${src === 'online' ? `<div class="hint" style="margin-top:8px">在线那条路要在 ip.json 的
      <span class="mono">bgm.online</span> 填 provider 和 api_key（界面不给填密钥，防止泄漏）。</div>` : ''}
  `;

  // 「用这首」= 把 source 切成 reuse + 记住文件名。两个字段都写进 ip.json，
  // 所以下次出片不用再问、也不会重新生成。
  box.querySelectorAll('[data-bgmpick]').forEach(btn => {
    btn.onclick = async () => {
      const name = btn.dataset.bgmpick;
      const jobs = [['bgm.source', 'reuse'], ['bgm.pick', name]];
      for (const [k, v] of jobs) {
        const r = await api('set', { body: { scope: 'ip', key: k, value: v,
                                            path: state.ip, confirm: true } });
        if (r.error) return toast('没存成', r.error, 'err');
      }
      await loadIpCfg();
      toast('配乐定了', name + ' —— 以后出片都拿这首，不再重新生成', 'ok');
      await renderAudio();
    };
  });
}

async function saveBgmParams(wantRandom) {
  const cfg = state.ipcfg || {};
  const src = ((cfg.bgm || {}).source) || 'synth';
  const prompt = (($('#bgmPrompt') || {}).value || '').trim();
  const dir = (($('#bgmDir') || {}).value || '').trim();
  const rand = wantRandom !== undefined ? wantRandom
    : (($('#bgmRand') || {}).checked) || false;
  const jobs = [];
  if (src === 'comfy' || src === 'online') {
    if (!prompt) return toast('这条路需要描述', '写一句「什么样的音乐」', 'err');
    jobs.push(['bgm.prompt', prompt]);
  } else if (src === 'library') {
    if (!dir) return toast('先填曲库目录', '指到放 mp3/wav 的那个文件夹', 'err');
    jobs.push(['bgm.library_dir', dir]);
  }
  if (src === 'library') jobs.push(['bgm.random', rand]);
  if (!jobs.length) { toast('这条路不用填参数', '直接用就行', 'ok'); return; }
  for (const [k, v] of jobs) {
    const r = await api('set', { body: { scope: 'ip', key: k, value: v, path: state.ip, confirm: true } });
    if (r.error) return toast('没存成', r.error, 'err');
  }
  await loadIpCfg();
  toast('配乐参数已存', wantRandom ? '换了一首随机的' : '下次跑到 bgm 那一步就生效', 'ok');
  await renderAudio();
}

async function probeBgm() {
  const cfg = state.ipcfg || {};
  const b = cfg.bgm || {};
  const src = b.source || 'synth';
  await saveBgmParams();                 // 先把正在改的参数存了，否则听的是上一版
  const payload = { kind: 'bgm-preview', ip: state.ip, source: src, seconds: 12 };
  if (b.prompt) payload.prompt = b.prompt;
  if (b.seed !== undefined && b.seed !== null) payload.seed = b.seed;
  if (b.random) payload.random = true;
  if (b.pick) payload.pick = b.pick;
  if (b.library_dir) payload.library_dir = b.library_dir;
  await startExec(payload, {
    then: (d) => {
      if (d.state === 'done') {
        // 产物落在 IP 自己的 06_音频/ 下，去那个目录听或塞进时间线都行
        toast('配乐试完了', '去 06_音频/_bgm_预览.wav 听；行就正常出片', 'ok');
      }
    }
  });
}

async function saveTtsInstr(silent) {
  const v = (($('#ttsInstr') || {}).value || '').trim();
  if (!v) return silent ? false : toast('语气指令是空的', '', 'err');
  const cur = ((((state.ipcfg || {}).voice) || {}).tts || {}).instruction || '';
  if (v === cur) return true;            // 没改就别写，省得 ip.json 堆一堆同样的备份
  const r = await api('set', { body: { scope: 'ip', key: 'voice.tts.instruction',
                                       value: v, path: state.ip } });
  if (r.error) { if (!silent) toast('没存成', r.error, 'err'); return false; }
  await loadIpCfg();
  if (!silent) toast('语气指令已存', '点「重做配音」才会重抽', 'ok');
  return true;
}

/* ---------- 试片台 ----------
   ★ 新客很多东西要试：定一帧形象、看一段画面。
      —— 定帧（首帧）和样片都是「可选项」，默认都不开，需要时才勾。
      —— 试片一律「一步一步跑、过审再下一步」，不一下子把后面都跑完。
      —— 三种模式：手动 / 从此处起跑完后面（过审再走）/ 一条龙全自动（不等人）。
*/
const TRIAL_STEPS = [
  { key:'still',  name:'首帧定帧',  hint:'定形象底图 + 起始静帧（接新客户第一步，没它出不了片）' },
  { key:'sample', name:'样片生成',  hint:'只出第 1 段，用正式分辨率验画面/身份（避免整条烧完才发现脸不对）' },
];
const STEP_BADGE = { idle:'待跑', running:'运行中', passed:'已过审', failed:'失败', skipped:'已跳过' };
const MODE_HINT = {
  manual: '手动：每步单独点「跑」，跑完过审再手动点下一步。',
  'run-rest': '从此处起跑完后面：一键连跑剩余步骤，但每步过完审才走下一步。',
  auto: '一条龙全自动：一键从当前步连跑到底，过审自动放行，不等人。',
};
const MODE_LABEL = { manual:'手动', 'run-rest':'从此处起跑完后面', auto:'一条龙全自动' };

function trialEnabledSteps() { return TRIAL_STEPS.filter(s => state.trial.enabled[s.key]); }
function trialFirstRunnable() {
  const steps = trialEnabledSteps();
  for (let i=0;i<steps.length;i++){
    const st = state.trial.status[steps[i].key];
    if (st!=='passed' && st!=='skipped') return i;
  }
  return 0;
}

async function renderTrial() {
  const box = $('#trialBox');
  if (!state.ip) { box.innerHTML = '<div class="empty">先选一个 IP</div>'; return; }
  const m = await api('materials?ip=' + encodeURIComponent(state.ip)).catch(() => null);
  state.materials = m || null;
  const miss = (m && m.items || []).filter(i => !i.file);
  $('#trialSub').textContent = m ? (m.all_ready ? '素材齐了' : `缺 ${miss.length} 项`) : '';

  // 拉试片配置（默认全关）
  const t = await api('trial?ip=' + encodeURIComponent(state.ip)).catch(()=>null);
  if (t && t.trial) {
    state.trial.mode = t.trial.mode || 'manual';
    state.trial.enabled = t.trial.enabled || {still:false,sample:false};
  }
  const en = state.trial.enabled;
  const steps = trialEnabledSteps();

  box.innerHTML = `
    <div class="sec-title">试片配置</div>
    <div class="hint" style="margin-bottom:8px">新客很多东西要试。定帧和样片都是<b>可选项</b>——
      不一定每轮都要，默认都不开；需要时才勾上。</div>
    <label class="chk"><input type="checkbox" id="trialStill" ${en.still?'checked':''}>
      启用 <b>首帧定帧</b></label>
    <label class="chk"><input type="checkbox" id="trialSample" ${en.sample?'checked':''}>
      启用 <b>样片生成</b></label>

    <div class="sec-title" style="margin-top:12px">执行模式</div>
    <div class="seg" id="trialMode">
      ${['manual','run-rest','auto'].map(v=>`
        <label class="seg-i ${state.trial.mode===v?'on':''}"><input type="radio" name="tmode" value="${v}" ${state.trial.mode===v?'checked':''}>
          ${MODE_LABEL[v]}</label>`).join('')}
    </div>
    <div class="hint" id="trialModeHint">${MODE_HINT[state.trial.mode]||''}</div>

    ${en.still ? `

    <div class="sec-title" style="margin-top:12px">定帧素材</div>
    <div class="hint" style="margin-bottom:8px">自己憋那段英文是最难的一步。三条路挑一条，
      <b>出图用的是下面「最终英文提示词」那框</b>，你也可以直接往里写。</div>
    <div class="seg" id="stillMode">
      ${[['ai','① 中文描述 → AI 写'],['polish','② 润色现有英文'],['pick','③ 引用已有素材']].map(([v,l])=>`
        <label class="seg-i ${state.stillMode===v?'on':''}"><input type="radio" name="smode" value="${v}" ${state.stillMode===v?'checked':''}>${l}</label>`).join('')}
    </div>

    ${state.stillMode === 'pick' ? `
      <div id="stillPick" class="hint" style="margin-top:8px">正在读素材目录…</div>` : `
      <textarea id="stillWho" class="brief-area" style="min-height:64px;margin-top:8px"
        placeholder="这个人长什么样（中文就行，AI 会翻成出图要的英文）。例：
老王，五十多岁，皮肤粗糙，短发花白，常穿深蓝围裙">${esc(state.stillWho || '')}</textarea>
      <textarea id="stillScene" class="brief-area" style="min-height:48px;margin-top:6px"
        placeholder="背景/环境（没有就留空，AI 按他的身份补一个）。例：小店后厨，暖黄灯，背景有蒸汽">${esc(state.stillScene || '')}</textarea>
      <div style="display:flex;gap:6px;margin-top:6px;flex-wrap:wrap">
        <button class="btn sm primary" id="btnStillAI">${ico('spark')} ${state.stillMode==='polish'?'AI 润色这段':'AI 写成英文'}</button>
      </div>
      <div class="hint" style="margin-top:6px">最终英文提示词（出图就用这段）</div>
      <textarea id="stillPromptEn" class="brief-area" style="min-height:70px">${esc(state.stillPromptEn || '')}</textarea>
    `}` : ''}

    <div class="sec-title" style="margin-top:12px">试片步骤</div>
    <div id="trialSteps">${steps.length ? renderTrialStepsInner() : '<div class="empty">两个都没启用 —— 这套不用试片，直接出片即可。</div>'}</div>
    <div id="trialRun">${renderTrialRunInner()}</div>
  `;
}

function renderTrialStepsInner() {
  const steps = trialEnabledSteps();
  return steps.map(s => {
    const st = state.trial.status[s.key] || 'idle';
    let ctrl;
    if (state.trial.gatePending === s.key) {
      ctrl = `<span class="hint">✅ 跑完了，过审？</span>
        <button class="btn sm primary" data-gate="pass">通过，下一步</button>
        <button class="btn sm" data-gate="retry">重跑这一帧</button>
        <button class="btn sm" data-gate="skip">跳过这步</button>`;
    } else if (st === 'running') {
      ctrl = `<span class="hint">运行中…</span>`;
    } else if (state.trial.mode === 'manual') {
      ctrl = `<button class="btn sm" data-trial-run="${s.key}">${ico('play')} 跑</button>`;
    } else {
      ctrl = `<button class="btn sm" data-trial-run="${s.key}">${ico('play')} 从这步起</button>`;
    }
    return `
      <div class="trial-step" data-step="${s.key}">
        <div class="ts-head">
          <b>${s.name}</b>
          <span class="badge" data-badge="${s.key}">${STEP_BADGE[st]||st}</span>
          <span class="hint" style="flex:1">${s.hint}</span>
        </div>
        <div data-step-ctrl="${s.key}" class="ts-ctrl">${ctrl}</div>
      </div>`;
  }).join('');
}

function renderTrialRunInner() {
  if (!trialEnabledSteps().length) return '';
  if (state.trial.mode === 'manual') {
    return `<div class="hint" style="margin-top:8px">手动模式：每步单独点「跑」，跑完过审再手动点下一步。</div>`;
  }
  const label = state.trial.mode==='auto' ? '开始试片（全自动，不等人）' : '开始试片（从此处跑到结尾，过审再走）';
  const disabled = state.trial.running ? 'disabled' : '';
  return `<button class="btn sm primary" id="btnTrialRun" style="margin-top:8px" ${disabled}>${ico('play')} ${label}</button>`;
}

function renderTrialSteps() {
  const el = $('#trialSteps');
  if (el) el.innerHTML = trialEnabledSteps().length ? renderTrialStepsInner() : '<div class="empty">两个都没启用。</div>';
}
function renderTrialRun() {
  const el = $('#trialRun');
  if (el) el.innerHTML = renderTrialRunInner();
}
function setStepStatus(key, status) {
  state.trial.status[key] = status;
  renderTrialSteps();
}

/* ---------- 定帧素材：三种取法 ---------- */

/* 引用已有素材：列出 02_素材真值 里的图，登记进 ip.json 的 visual.still / portrait。
   只改指针不搬文件 —— 登记错了改回来就行，原图还在原地。 */
async function renderStillPick() {
  const box = $('#stillPick');
  if (!box) return;
  const d = await api('assets?ip=' + encodeURIComponent(state.ip)).catch(() => null);
  if (!d || d.error) { box.innerHTML = '<div class="empty">读不到素材目录</div>'; return; }
  state.stillAssets = d.items || [];
  const cur = d.current || {};
  if (!state.stillAssets.length) {
    box.innerHTML = `<div class="empty">这个 IP 的 <span class="mono">02_素材真值/</span> 里还没有图。<br>
      换「① AI 写」让模型画一张，或者自己把照片拷进那个目录再来。</div>`;
    return;
  }
  box.innerHTML = `<div class="mat-list">` + state.stillAssets.map((f, i) => {
    const tags = (f.in_use || []).map(k => `<span class="badge ok">${k === 'still' ? '当前静帧' : '当前底图'}</span>`).join('');
    return `<div class="mat">
      <img class="mth" src="/api/thumb?path=${encodeURIComponent(f.path)}" alt="">
      <span class="mn" title="${esc(f.name)}">${esc(f.name)}</span>
      <span class="ms">${f.mtime}</span>
      ${tags}
      <button class="btn sm" data-visual="still" data-name="${esc(f.name)}">设为静帧</button>
      <button class="btn sm ghost" data-visual="portrait" data-name="${esc(f.name)}">设为底图</button>
    </div>`;
  }).join('') + `</div>`;
}

async function applyVisual(name, role) {
  const r = await api('visual-apply', { body: { ip: state.ip, name, role } });
  if (r.error) return toast('没登记成', r.error, 'err');
  toast('已登记', `${name} → ${r.label}（原图没动）`, 'ok');
  await renderStillPick();
}

/* AI 写 / 润色 定帧提示词。写回来的东西会进 #stillPromptEn，
   并同时提示 AI 补了哪些它猜的信息 —— 猜错的细节会一路印到成片上。 */
async function genStillPrompt() {
  const polishMode = state.stillMode === 'polish';
  const who = ($('#stillWho') || {}).value || '';
  const scene = ($('#stillScene') || {}).value || '';
  const cur = ($('#stillPromptEn') || {}).value || '';
  if (polishMode && !cur.trim()) {
    return toast('先把现有英文填进下面那框', '润色需要原材料', 'err');
  }
  if (!polishMode && !who.trim()) {
    return toast('先写这个人长什么样', '一句话也行', 'err');
  }
  await startExec({
    kind: 'still-prompt', ip: state.ip,
    who: polishMode ? who.trim() : who.trim(),
    scene: polishMode ? '' : scene.trim(),
    polish: polishMode ? cur.trim() : '',
  }, {
    onDone: (res) => {
      const p = String((res || {}).prompt || '').trim();
      if (!p) return toast('AI 没给出提示词', '再试一次，或自己手写', 'err');
      $('#stillPromptEn').value = p;
      if (res.who_en && $('#stillWho')) $('#stillWho').value = res.who_en;
      if (res.scene_en && $('#stillScene')) $('#stillScene').value = res.scene_en;
      const notes = res.notes || [];
      toast('提示词写好了', notes.length ? notes[0] : '已填进下面那框', 'ok');
      if (notes.length > 1) {
        // 补了什么是 model 猜的，必须让人看见，否则印到成片才发现
        setTimeout(() => toast('AI 补了这些，记得核', notes.slice(1).join('；'), 'warn'), 900);
      }
    }
  });
}

/* 跑单个试片步骤，返回 Promise<boolean>。完成由 startExec 的 poll 回调触发（opts.then）。 */
function runOneStep(key) {
  return new Promise(async (resolve) => {
    setStepStatus(key, 'running');
    const opts = { then: (d) => {
      const ok = d && d.state === 'done';
      setStepStatus(key, ok ? 'passed' : 'failed');
      resolve(ok);
    } };
    if (key === 'still') {
      // 「③ 引用已有素材」不用生成：登记完就直接用那张图，再抽一次反而可能抽成另一张脸
      if (state.stillMode === 'pick') {
        const used = (state.stillAssets || []).some(f => (f.in_use || []).includes('still'));
        if (!used) {
          toast('还没选静帧', '在上面那张图右边点「设为静帧」', 'err');
          setStepStatus(key, 'idle'); resolve(false); return;
        }
        setStepStatus(key, 'passed'); resolve(true); return;
      }
      // 优先用 AI 写出来那版英文；没走过 AI 就用人在输入框里写的原话
      const who = (($('#stillPromptEn') || {}).value || ($('#stillWho') || {}).value || '').trim();
      if (!who) { toast('先写这个人长什么样','或用上面的 AI 帮你写','err'); setStepStatus(key,'idle'); resolve(false); return; }
      const scene = ($('#stillScene')||{}).value || '';
      const r = await startExec({ kind:'still', ip:state.ip, who, scene:scene.trim() }, opts);
      if (r === null) { setStepStatus(key,'idle'); resolve(false); return; }
    } else if (key === 'sample') {
      if (!state.setId) { toast('先选一套片子','样片要跟着某一套走','err'); setStepStatus(key,'idle'); resolve(false); return; }
      const r = await startExec({ kind:'pipeline', ip:state.ip, set:state.setId, tier:'pilot' }, opts);
      if (r === null) { setStepStatus(key,'idle'); resolve(false); return; }
    }
    // 否则 poll 结束后 opts.then 触发 resolve
  });
}

/* 过审闸门：auto 模式直接放行；其它模式弹按钮等人点。返回 Promise<'pass'|'retry'|'skip'> */
function reviewGate(key, mode) {
  if (mode === 'auto') return Promise.resolve('pass');
  return new Promise((resolve) => {
    state.trial.gatePending = key;
    renderTrialSteps();
    state._gateResolve = resolve;
  });
}

/* 跑试片：chain=true 时从 key 这一步连跑到底（按模式过审）；否则只跑这一步。 */
async function runTrialStep(key, chain) {
  if (state.trial.running) return;
  state.trial.running = true; renderTrialRun();
  const mode = state.trial.mode;
  const steps = trialEnabledSteps();
  let i = steps.findIndex(s => s.key === key);
  if (i < 0) i = 0;
  for (; i < steps.length; i++) {
    const k = steps[i].key;
    const ok = await runOneStep(k);
    if (!ok) { state.trial.running = false; renderTrialRun(); return; }
    const decision = await reviewGate(k, chain ? mode : 'manual');
    if (decision === 'retry') { i--; continue; }
    if (decision === 'skip') { setStepStatus(k,'skipped'); continue; }
    setStepStatus(k,'passed');
    if (!chain) { state.trial.running = false; renderTrialRun(); return; }  // 单步就停
  }
  state.trial.running = false; renderTrialRun();
  toast('试片跑完','', 'ok');
  await loadIpCfg(); await renderTrial();
}

async function runTrialChain() {
  const steps = trialEnabledSteps();
  if (!steps.length) return toast('先启用至少一个试片步骤','', 'err');
  const start = steps[trialFirstRunnable()].key;
  await runTrialStep(start, true);
}

async function saveTrialCfg() {
  if (!state.ip) return;
  const en = {
    still: $('#trialStill') ? $('#trialStill').checked : false,
    sample: $('#trialSample') ? $('#trialSample').checked : false,
  };
  const modeEl = document.querySelector('input[name="tmode"]:checked');
  const mode = modeEl ? modeEl.value : 'manual';
  state.trial.enabled = en; state.trial.mode = mode;
  state.trial.status = {}; state.trial.gatePending = null;
  const r = await api('trial', { body: { ip:state.ip, save:true, trial:{mode, enabled:en} } });
  if (r && r.error) toast('没存成', r.error, 'err');
  await renderTrial();
}

/* ---------- 总沟通对话区 ----------
   ★ 用自然语言说需求，后端把意图拆成可执行动作（改档案卡/技术真值/人设/阶段/试片），
     先说「将做什么」，等你确认再执行。每一处写都记入 changelog。
   ★ 对话历史只存在当前页面会话里，刷新就清——涉密内容不落本地持久化。 */

function renderDialogue() {
  const box = $('#dlgMessages');
  if (!state.ip) {
    box.innerHTML = '<div class="dlg-sys">先在左栏选一个 IP，再跟我说需求。</div>';
    return;
  }
  box.innerHTML = state.dialogue.map(m => {
    const mine = m.role === 'user';
    const cls = mine ? 'dlg-msg dlg-mine' : 'dlg-msg dlg-theirs';
    let content = `<div class="dlg-bubble">${esc(m.content)}</div>`;
    if (m.actions && m.actions.length) {
      content += `<div class="dlg-actions">
        ${m.actions.map(a => {
          if (a.status === 'await_confirm') {
            return `<span class="dlg-act dlg-act-wait">⏸ ${a.explanation || a.action}（等确认）</span>`;
          }
          if (a.status === 'done') {
            return `<span class="dlg-act dlg-act-ok">✔ ${a.message || a.action}</span>`;
          }
          return `<span class="dlg-act dlg-act-bad">✗ ${a.message || a.action}</span>`;
        }).join('')}
      </div>`;
    }
    return `<div class="${cls}">${content}</div>`;
  }).join('');
  box.scrollTop = box.scrollHeight;
}

function appendMessage(role, content, actions) {
  state.dialogue.push({ role, content, actions, at: new Date().toLocaleString() });
  renderDialogue();
}

async function sendDialogue() {
  const input = $('#dlgInput');
  const text = (input.value || '').trim();
  if (!text || !state.ip) return;
  input.value = '';
  appendMessage('user', text);
  $('#dlgSend').disabled = true;
  $('#dlgHint').textContent = '正在理解…';
  try {
    const r = await api('dialogue', {
      body: { ip: state.ip, text, history: state.dialogue.slice(-10) },
    });
    if (r.error) {
      appendMessage('assistant', `抱歉，出了点问题：${r.error}`);
    } else {
      appendMessage('assistant', r.reply || '收到了。', r.actions || []);
    }
  } catch (ex) {
    appendMessage('assistant', `发送失败：${ex}`);
  }
  $('#dlgSend').disabled = false;
  $('#dlgHint').textContent = '一次说一件事最准；涉及改动我会先说「将做什么」，等你确认再执行。';
}

/* ---------- 人设与规范 ----------
   ★ 这个目录空着 → AI 写稿没东西可读 → 只能瞎编（2026-10-29 实测的老王就是这样）。
     所以它不是「档案的附属品」，是**出片的前置条件**，界面上必须能直接写。 */
async function renderPersona() {
  const box = $('#personaBox');
  if (!state.ip) { box.innerHTML = '<div class="empty">先选一个 IP</div>'; return; }
  // 换 IP 时不能带着上一个 IP 的文件名 —— 否则「存」会拿着别人的文件名
  // 在这个 IP 目录里造出一份同名文件。
  if (state.personaFor !== state.ip) { state.personaCur = ''; state.personaFor = state.ip; }
  // ★ 读失败 ≠ 没有人设。旧写法 `.catch(() => ({}))` 把任何故障都变成空对象，
  //   页面就显示「这个 IP 没有人设材料」+ 一个空白框（截图里那行
  //   「0 篇 / undefined 字」就是它）。人看到这句，会把手上的圣经粘进去"补上"，
  //   等于用一段临时话盖掉 16965 字的真身 —— 这种误伤比不显示更糟。
  let d;
  try {
    d = await api('persona?ip=' + encodeURIComponent(state.ip));
  } catch (e) {
    d = { error: String((e && e.message) || e) };
  }
  state.persona = d;
  if (!d || d.error || !Array.isArray(d.files)) {
    $('#personaSub').textContent = '读取失败';
    box.innerHTML = `
      <div class="warnbox" style="margin-bottom:8px">
        <b>人设目录没读出来。</b>这不代表没有资料 —— 是这次请求失败了，
        所以下面故意不给你空白编辑框，免得误覆盖。
      </div>
      <div class="hint mono" style="margin-bottom:8px">${esc((d && d.error) || '返回格式不对')}</div>
      <div class="hint" style="margin-bottom:8px">
        直接去看目录里到底有什么：<span class="mono">${esc((state.ipRoot || '') + '/01_人设与规范/')}</span>
      </div>
      <button class="btn sm" id="btnPersonaRetry">${ico('plus')} 重试读取</button>`;
    const rb = $('#btnPersonaRetry');
    if (rb) rb.onclick = () => renderPersona();
    return;
  }
  const files = d.files || [];
  const core = d.core || [];
  // 主号的 01_人设与规范/ 里混着真·人设圣经和 ComfyUI 速查、背景方案这类参考料。
  // 只报「4 篇」会让人以为有四份人设在打架，所以按性质拆开说。
  const nRef = files.length - core.length;
  $('#personaSub').textContent = d.empty ? '空的 —— AI 写稿会瞎编'
    : `${core.length} 篇人设${nRef ? ` + ${nRef} 篇参考` : ''} / ${(d.total || 0)} 字`;

  if (!files.length) {
    state.personaCur = '';
    box.innerHTML = `
      <div class="warnbox" style="margin-bottom:8px">
        <b>这个 IP 没有人设材料。</b>AI 写稿、选题全靠这份东西 ——
        它是空的，模型只能瞎编，写出来的东西跟你这个号没关系。
      </div>
      <div class="hint" style="margin-bottom:8px">
        写清楚这几件事就够了：<b>他是谁 / 干什么 / 讲给谁听 / 说话什么调调 / 绝对不能碰什么</b>。
        底下「AI 起草」能按这个结构写一份完整的（定位、目标人群、人格、形象、声音、
        口播结构、内容支柱、钩子库、视觉规范、禁止项、每周自检）。
      </div>
      <textarea id="personaText" class="brief-area" style="min-height:150px"
        placeholder="用客户自己的话写就行，不用工整。例如：
我是老王，在汕头开牛肉火锅店二十年了。
讲话实在，不夸张，不喊口号，粤语口音轻。
不夸自己的汤，不编客人故事，不说「全城第一」。
镜头前穿着围裙，在店里，背景是明档。"></textarea>
      <div style="display:flex;gap:6px;margin-top:8px;align-items:center;flex-wrap:wrap">
        <button class="btn sm primary" id="btnPersonaSave">${ico('save')} 存成人设圣经</button>
        <button class="btn sm" id="btnPersonaAI">${ico('spark')} AI 按结构起草</button>
        <span class="hint" style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
          title="${esc(d.dir || '')}">${esc(d.dir || '')}</span>
      </div>`;
    return;
  }

  box.innerHTML = `
    <div class="hint" style="margin-bottom:8px">
      AI 写稿、选题都读这里的 md。<b>改完不用重跑任何东西</b>，
      下次写稿就生效。文件名的真源是 <span class="mono">01_人设与规范/</span>。
    </div>
    <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px">
      ${files.map(f => {
        const isCore = core.includes(f.name);
        return `<button class="btn sm${isCore ? ' primary' : ''}" data-persona-file="${esc(f.name)}"
          title="${isCore ? '人设主干：存的时候默认落到这一篇' : '参考资料：只是喂给 AI 的背景材料'}">${esc(f.name)}
          <span class="hint" style="margin-left:4px">${Math.round(f.size / 100) / 10}k</span></button>`;
      }).join('')}
    </div>
    <div class="hint" style="margin-bottom:6px">
      加粗那篇是<b>人设主干</b>。存的时候默认<b>替换它</b>（先自动备份），
      不会另起一份跟它抢话；确实要写第二份，用下面的「另存为新一篇」。
    </div>
    <div id="personaEditor"></div>`;

  // 默认打开主干那篇 —— 打开参考资料再点存，很容易把资料改坏
  const first = files.find(f => core.includes(f.name)) || files[0];
  if (first) await openPersonaFile(first.name);
}

async function openPersonaFile(name) {
  const d = await api('persona?name=' + encodeURIComponent(name) + '&ip=' + encodeURIComponent(state.ip)).catch(() => null);
  const box = $('#personaEditor');
  if (!box) return;
  if (!d || d.error) { box.innerHTML = '<div class="empty">读不出来</div>'; return; }
  state.personaCur = d.name || name;
  box.innerHTML = `
    <textarea id="personaText" class="brief-area" style="min-height:320px">${esc(d.text || '')}</textarea>
    <div style="display:flex;gap:6px;margin-top:8px;align-items:center;flex-wrap:wrap">
      <button class="btn sm primary" id="btnPersonaSave">${ico('save')} 存这一篇（替换）</button>
      <button class="btn sm" id="btnPersonaSaveNew" title="写成新的一份，不动这一篇">${ico('plus')} 另存为新一篇</button>
      <span class="hint" style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
        title="${esc(d.path || '')}">${esc(d.name || '')} · ${(d.text || '').length} 字</span>
    </div>`;
}

async function savePersona(name, mode) {
  const ta = $('#personaText');
  if (!ta) return toast('没有内容可存', '', 'err');
  if (!state.ip) return toast('先选 IP', '', 'err');
  const body = { ip: state.ip, text: ta.value };
  if (name) body.name = name;
  if (mode) body.mode = mode;
  const r = await api('persona', { body });
  if (r.error) return toast('没存成', r.error, 'err');
  toast(r.replaced ? '已替换这一篇' : '已存成新的一篇',
    `${r.name || ''}${r.backup ? ' · 原稿备份了' : ''}`, 'ok');
  await renderPersona();
  if (r.name) await openPersonaFile(r.name);
}

/* AI 起草人设圣经：按成熟 IP 的圣经结构写一份完整的。
   和「对话式建档」那份粗的两篇不一样 —— 这份有内容支柱、钩子库、禁止项，
   这些是真正决定「每天发什么、片子长什么样」的部分。 */
async function aiDraftPersona() {
  const ta = $('#personaText');
  const text = ta ? ta.value.trim() : '';
  if (!text) return toast('先写几句这个号是谁', 'AI 要有素材才能起草', 'err');
  const r = await api('persona-draft', { body: { text, ip: state.ip } });
  if (r.error) return toast('没起草出来', r.error, 'err');
  const box = $('#personaEditor');
  if (box && ta) {
    ta.value = r.persona_md || '';
    ta.style.minHeight = '420px';
  }
  toast('起草好了', (r.provider || '') + ' · 看看有没有编的地方再存', 'ok');
  if ((r.questions || []).length) {
    console.log('[persona-draft] 还要问客户：', r.questions.join(' / '));
  }
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
  const landing = String((state.card || {}).landing || '').trim();
  // ★ 稿子本体也要在这块显示。
  //   以前写完只有一句 toast，界面上没有任何地方能确认「文件里换没换」——
  //   2026-10-29 实测：用户点了「写进这套片子」，跳回第一套，回头看到的
  //   还是旧内容，以为没写进去。看不到 = 没发生。
  const sc = await api('script-text?ip=' + encodeURIComponent(state.ip) + '&set=' + encodeURIComponent(state.setId))
    .catch(() => ({}));
  state.script = sc;
  const stale = sc.stale_copies || [];
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
    <div style="display:flex;gap:6px;margin-top:8px;align-items:center;flex-wrap:wrap">
      <button class="btn sm" id="btnBriefSave2">${ico('save')} 存需求</button>
      <button class="btn sm primary" id="btnScriptGen" title="按这张卡的角色需求 + 主号爆款标准写一条口播稿">${ico('spark')} 按角色写爆款文案</button>
      <button class="btn sm" id="btnBriefGen">${ico('plus')} 生成提示词</button>
      <span class="hint" style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
        title="${esc(d.path || '')}">${esc((d.path || '').replace(state.ipRoot || '', ''))}</span>
    </div>
    ${landing ? `<div class="hint" style="margin-top:6px">本号固定落点：<b style="color:var(--tx-1)">${esc(landing)}</b></div>`
      : `<div class="hint" style="margin-top:6px;color:var(--warn)">这张卡还没定落点 —— 第一次写文案时会让 AI 拟一条，确认后存进卡里，之后每条都用它。</div>`}

    <div class="sec-title" style="margin-top:16px">本套文案（出片念的就是这份）
      <span class="chip">${sc.chars || 0} 字 ≈ ${sc.est_secs || 0} 秒</span>
      <span class="chip">${sc.mtime ? '改于 ' + esc(sc.mtime) : '还没写'}</span></div>
    ${stale.length ? `<div class="warnbox" style="margin-bottom:8px">
      这个目录里还有 <b>${stale.length}</b> 份旧命名的稿子（<span class="mono">${esc(stale.join('、'))}</span>）。
      出片只认下面这份 —— 改文案请只改这里，改另一份不会生效。
      <div class="hint" style="margin-top:4px">下次保存时会自动把旧的那份改名留档，不会删。</div></div>` : ''}
    <textarea id="scriptText" class="brief-area" style="min-height:150px"
      placeholder="这一套还没有文案。点上面「按角色写爆款文案」让 AI 起一稿，或者直接把稿子粘进来。">${esc(sc.text || '')}</textarea>
    <div style="display:flex;gap:6px;margin-top:8px;align-items:center;flex-wrap:wrap">
      <button class="btn sm" id="btnScriptSave">${ico('save')} 存文案</button>
      <button class="btn sm" id="btnScriptPrompts" title="照这份稿子逐段切分镜">${ico('spark')} 按文案生成提示词</button>
      <span class="hint" style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
        title="${esc(sc.path || '')}">${esc((sc.path || '').replace(state.ipRoot || '', ''))}</span>
    </div>`;

  // 上面三个按钮（存需求 / 按角色写爆款文案 / 生成提示词）由 document 级
  // 事件委托统一接管（见文件底部），这里**不要**再 onclick 绑一遍 ——
  // 绑两次会双触发：点一次「写文案」会跑两遍模型，白烧两次 API。
  // 下面两个是本区新增的，没有委托，所以就地绑。
  $('#btnScriptSave').onclick = async () => {
    const text = ($('#scriptText')?.value || '').trim();
    if (!text) return toast('文案是空的', '空的稿子存下去，出片会一片空白', 'err');
    const rr = await api('script-write', { body: { ip: state.ip, set: state.setId, script: text } });
    if (rr.error) return toast('没存成', rr.error, 'err');
    toast('文案已存', `${rr.chars || 0} 字` + (rr.backup ? ' · 原稿已备份' : '')
      + ((rr.stale_backup || []).length ? ` · 旧稿改名 ${rr.stale_backup.length} 份` : ''), 'ok');
    await renderBrief();
  };
  $('#btnScriptPrompts').onclick = () => genPrompts('prompts-gen');
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

/* 按角色需求写爆款文案。
   判据是 templates/script_standard.md —— 从主号 S01–S05 跑量的稿子里提纯的，
   不是模型自己的网感。这里只负责把「这套片子讲什么」和角色需求一起交出去。 */
async function genScript() {
  if (!state.setId) return toast('先选套次', '文案是跟着某一套片子走的', 'err');
  const job = (state.jobs || []).find(j => j.id === state.setId);
  const topic = (job && job.title) || '';
  await startExec({ kind: 'ai-script', ip: state.ip, set: state.setId, topic }, {
    onDone: r => showScriptResult(r),
  });
}

/* 稿子出来了：先给判据下的结论，再给正文，最后才是动作按钮。
   顺序别反 —— 人第一眼要知道「这条能不能用」，不想先读一百多字。 */
function showScriptResult(r) {
  const c = r.checks || {};
  const prob = c.problems || [];
  const lines = (r.lines || []).length
    ? r.lines.map(l => `<div class="pseg"><div class="tx">${esc(l)}</div></div>`).join('')
    : `<pre>${esc(r.script || '')}</pre>`;
  // ★ 把文案生成结果也推进总沟通对话区，不只锁在 modal 里
  //   这样「按角色写爆款文案」出来的稿子，跟自然语言对话的其他记录在一起看。
  const summary = (r.script || '').slice(0, 120) + (r.script && r.script.length > 120 ? '…' : '');
  appendMessage('assistant', `文案草稿已出（${c.chars || 0}字 ≈ ${c.est_secs || 0}秒）：${summary}${prob.length ? ' ⚠ 有' + prob.length + ' 处自检问题' : ' ✓ 过自检'}`,
    prob.length ? [{ action: 'ai-script', status: 'done', message: '已生成（需先改自检问题才能出片）', params: {} }]
      : [{ action: 'ai-script', status: 'done', message: '文案生成完成', params: {} }]);
  $('#modalBox').innerHTML = `<div class="modal-head"><b>${ico('spark')} 文案草稿</b>
      <span class="sub">${esc(r.provider || '?')} · ${esc(r.hook_type || '?')} · ${c.chars || 0} 字 ≈ ${c.est_secs || 0} 秒</span>
      <div class="spacer"></div><button class="btn icon" id="mclose">${ico('close')}</button></div>
    ${prob.length
      ? `<div class="warnbox">没过自检：${prob.map(esc).join('；')}<br>
          稿子照样给你，但要改完再拿去配音 —— 超长或没落点的片子，观众会走。</div>`
      : `<div class="hint" style="color:var(--ok)">✓ 过自检：字数、红线、落点都在标准内</div>`}
    <div class="sec-title" style="margin-top:12px">逐行（念出来就是这个节奏）</div>
    ${lines}
    <div class="sec-title" style="margin-top:14px">落点</div>
    <div class="pseg ${r.landing_is_new ? 'bad' : ''}"><div class="tx">${esc(r.landing || '（没有落点 —— 这条等于白讲）')}</div></div>
    ${r.landing_is_new
      ? `<div class="hint" style="margin-top:4px">这句是这次<b>新拟</b>的。定下来存进卡里，以后每条都用它 ——
          每次换一句，观众记不住要他干什么。</div>`
      : `<div class="hint" style="margin-top:4px">本号固定话术，每条都一样。</div>`}
    ${(r.claims || []).length ? `<div class="sec-title" style="margin-top:14px">这条承诺了什么（要能兑现）</div>
      ${r.claims.map(x => `<div class="pseg"><div class="tx">${esc(x)}</div></div>`).join('')}` : ''}
    ${r.note ? `<div class="hint" style="margin-top:12px">模型备注：${esc(r.note)}</div>` : ''}
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:16px;flex-wrap:wrap">
      <button class="btn ghost" id="scAgain">换一条</button>
      ${r.landing_is_new ? `<button class="btn ghost" id="scLanding">存为本号固定落点</button>` : ''}
      <button class="btn primary" id="scWrite">${ico('save')} 写进这套片子</button></div>`;
  $('#modal').classList.add('show');
  $('#mclose').onclick = closeModal;
  $('#scAgain').onclick = () => { closeModal(); void genScript(); };
  if ($('#scLanding')) {
    $('#scLanding').onclick = async () => {
      const rr = await api('landing', { body: { ip: state.ip, landing: r.landing } });
      if (rr.error) return toast('没存成', rr.error, 'err');
      toast('落点已定', '以后每一条都用这句', 'ok');
      state.card = await api('card?ip=' + encodeURIComponent(state.ip));
      closeModal();
      await renderBrief();
    };
  }
  $('#scWrite').onclick = async () => {
    // 落点随稿子一起落：存进这套自己的 landing.md（不是卡里的固定落点）。
    // 只发一次请求 —— 发两次会写两遍、备份两份，纯属给自己添乱。
    const rr = await api('script-write', {
      body: { ip: state.ip, set: state.setId, script: r.script, landing: r.landing || '' }
    });
    if (rr.error) return toast('没写进去', rr.error, 'err');
    toast('已写进文案', `${rr.chars || 0} 字`
      + (rr.backup ? ' · 原稿已备份' : '')
      + ((rr.stale_backup || []).length ? ` · 旧稿改名 ${rr.stale_backup.length} 份` : ''), 'ok');
    closeModal();
    // ★ 只刷当前这套，绝不 boot()。
    //   boot() 会 selectIP(ips[0]) 再挑第一个未交付套次 —— 于是「写进这套片子」
    //   一点，界面就从老王 M02 跳回主号 S01，人以为写错了地方，
    //   回头看到的「文案」也是第一套的旧内容（2026-10-29 实测报告）。
    await refreshSet();
    // 让新稿子直接落到眼皮底下：切到「创作」组，滚到需求台
    openMidTab('create');
    $('#briefBox')?.scrollIntoView({ behavior: 'smooth', block: 'center' });
  };
}

/* 写完文件之后刷新的正确姿势：只重载当前这一套的数据，不动 IP、不动套次。
   和 boot() 的区别就在这一句「不重选」—— 重选是之前跳台的根源。 */
async function refreshSet() {
  if (!state.ip) return;
  const keep = state.setId;
  await loadJobs();
  await selectSet(keep);
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
/* 中栏面板按关联度分成几组。原来 8 个面板全堆在中栏，
   看完一个要滚很久；现在一次只摊开一组。 */
const MID_GROUPS = {
  progress: ['overview', 'files'],
  create:   ['persona', 'brief', 'prompts'],
  trial:  ['trial'],
  talk:   ['dialogue'],
  cmd:    ['cmd'],
};

function openTab(name) {
  state.tab = name;
  // ★ 选择器必须限定在 #rightTabs 内：中栏另有一组 .tab（见 openMidTab），
  //   用全局 '.tab' 会把中栏标签的选中态一起清掉 —— 表现就是「右栏一切，
  //   中栏的标签全灭了」。
  $$('#rightTabs .tab').forEach(t => t.classList.toggle('on', t.dataset.tab === name));
  ['inspect', 'settings', 'log'].forEach(k =>
    $('#tab' + k[0].toUpperCase() + k.slice(1)).style.display = state.tab === k ? '' : 'none');
  if (name === 'settings') renderSettings();
  if (name === 'log') renderLog();
}

/* 中栏标签切换。默认停在「进度」—— 进来第一眼该看的是片子做到哪了，
   不是一堆还没填的表单。 */
function openMidTab(name) {
  state.midTab = name;
  $$('#midTabs .tab').forEach(t => t.classList.toggle('on', t.dataset.group === name));
  const want = new Set(MID_GROUPS[name] || []);
  $$('#colMid > section.panel').forEach(p => {
    p.style.display = want.has(p.dataset.panel) ? '' : 'none';
  });
  try {
    localStorage.setItem('vstudio.midtab', JSON.stringify({ tab: name }));
  } catch (e) {
    // localStorage 不可用（隐私模式 / 配额满）不该连累界面，存不下就算了
  }
}

/* 标签栏事件 + 恢复上次停留的标签。
   放在 DOM 就绪之后跑一次就够。 */
function initMidTabs() {
  const bar = $('#midTabs');
  if (!bar) return;
  bar.addEventListener('click', e => {
    const t = e.target.closest('.tab');
    if (t && t.dataset.group) openMidTab(t.dataset.group);
  });
  let saved = '';
  try {
    saved = (JSON.parse(localStorage.getItem('vstudio.midtab') || '{}')).tab || '';
  } catch (e) {
    saved = '';
  }
  openMidTab(saved && MID_GROUPS[saved] ? saved : 'progress');
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
      <div class="plan-h"><b>${esc(p.label)}</b>${p.primary ? '<span class="chip ok">推荐</span>' : ''}
        <div class="spacer"></div>
        ${p.exec ? `<button class="btn sm ghost" data-exe="plan-dry" data-plan='${esc(JSON.stringify(p.exec))}'
            title="只算计划，不提交 GPU">${ico('eye')} 看计划</button>` : ''}
        ${p.exec ? `<button class="btn sm ${p.primary ? 'primary' : ''}" data-exe="plan" data-plan='${esc(JSON.stringify(p.exec))}'>${ico('play')} 跑</button>` : ''}
        ${p.run ? `<button class="btn sm" data-quick='${esc(JSON.stringify(p.run))}'>${ico('check')} 执行</button>` : ''}
        <button class="btn sm ghost" data-copy="${esc(p.cmd)}">${ico('copy')}</button></div>
      <div class="hint" style="margin:4px 0 6px">${esc(p.why)}</div>
      <div class="cmdbar"><span class="p">$</span> ${esc(p.cmd)}</div>
    </div>`).join('') || `<div class="empty">这一步没有流水线命令（人工环节）</div>`;

  // 种子：这套片用过哪几颗、每颗跑出过什么。点一颗就是「照着那次重跑」。
  const sd = d.seed || {};
  const hist = sd.history || [];
  const seedHtml = (sd.seed || hist.length) ? `
    <div style="margin-bottom:9px">
      <div class="hint" style="margin-bottom:6px">
        当前种子 <b class="mono">${esc(String(sd.seed ?? '—'))}</b>${sd.source === 'config'
          ? '（配置默认 —— 这套片还没记录过跑批）' : ''}。
        同一颗种子重跑会得到几乎一样的画面；想换画面点上面的「换一个种子」，
        ${hist.length ? '或者挑一颗旧的重现那一次的结果（见下）' : '跑过一次之后这里会列出用过的种子'}。
      </div>
      ${hist.length ? `<div style="display:flex;flex-wrap:wrap;gap:5px">${hist.map(h => `
        <button class="btn sm ghost" data-exe="plan"
          data-plan='${esc(JSON.stringify({ kind: 'pipeline', ip: d.ip, set: d.set, from: 'video', seed: h.seed }))}'
          title="用这颗种子重出画面 —— 复现那一次的结果">${ico('hist')} ${esc(String(h.seed))} · ${esc(h.at || '')}</button>`).join('')}</div>` : ''}
    </div>` : '';

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
    ${seedHtml}
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
  // 素材跨弹层保留：第 1 步选完，第 2 步确认时还要用，
  // 而弹层每次都整块重绘 —— 状态必须挂在 state 上，不能指望 DOM 记住。
  state.newCard = state.newCard || { uid: null, files: [], intro: '' };
  const s = state.newCard;

  $('#modalBox').innerHTML = `<div class="modal-head"><b>${ico('plus')} 新建档案卡</b>
      <span class="sub">第 1 步 · 说清楚是谁</span>
      <div class="spacer"></div><button class="btn icon" id="mclose">${ico('close')}</button></div>
    <label class="field"><span>用你自己的话介绍这个客户（越口语越好）</span>
      <textarea id="ncTalk" class="brief-area" style="min-height:126px"
        placeholder="例：客户是开潮汕牛肉火锅的，馆子叫八合里，老板姓林，四十多岁，不愿意上镜，想让我用 AI 给他做个口播号，一周三条，发抖音和小红书，主要讲牛肉怎么挑、火锅怎么涮，别讲加盟别讲价格，他说话有潮汕口音，语速慢点"></textarea>
      <span class="hint">说清四件事就够：他是谁 / 讲什么 / 发哪里多久一条 / 什么绝对不能碰</span></label>

    <div class="sec-title" style="margin-top:14px">形象素材（可选，给了形象卡才准）</div>
    <div class="hint" style="margin-bottom:8px">
      人像参考图会被看图写成外貌真值；音色音频会被<b>实测</b>基频 / 语速 / 响度
      —— 不是让模型「听一遍」，路由里没有音频输入通道，
      硬听只会编出一段假音色，而后面 TTS 会照着假描述去克隆。
    </div>
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
      <button class="btn sm" id="ncPick">${ico('plus')} 选择文件</button>
      <span class="hint" id="ncPickSub">图片 png/jpg/webp · 音频 wav/mp3/flac/m4a，可多选</span>
    </div>
    <input type="file" id="ncFile" multiple accept="image/*,audio/*" hidden>
    <div id="ncFiles" class="mat-list"></div>

    <label class="field" style="margin-top:10px"><span>其它介绍（补充上面没说到的）</span>
      <textarea id="ncIntro" class="brief-area" style="min-height:70px"
        placeholder="常戴眼镜、带潮汕口音、一般穿深色衬衫、不上镜只出声音"></textarea></label>

    <div class="hint" style="margin:10px 0 12px">
      AI 会整理成档案卡字段，并把人设文档写进
      <span class="mono">01_人设与规范/</span>（人设圣经 + 内容方向；传了素材还多一份形象卡）。
      以后 AI 选题就靠这几篇，没有它选题只能瞎编。</div>
    <div style="display:flex;gap:8px;justify-content:flex-end">
      <button class="btn ghost" id="ncManual">我手填</button>
      <button class="btn primary" id="ncAI">${ico('spark')} AI 出草案</button></div>`;
  $('#modal').classList.add('show');
  $('#mclose').onclick = closeModal;
  $('#ncManual').onclick = () => { s.intro = ($('#ncIntro')?.value || '').trim(); showNewCardManual(null); };
  $('#ncPick').onclick = () => $('#ncFile').click();

  // 选完立刻上传：草稿阶段可能来回改几次，但原始素材只传一次，
  // 后面分析/建档都靠这个 uid 认文件。
  $('#ncFile').onchange = async e => {
    const list = [...(e.target.files || [])];
    e.target.value = '';
    if (!list.length) return;
    const sub = $('#ncPickSub');
    if (sub) sub.textContent = '正在上传…';
    const fd = new FormData();
    for (const f of list) fd.append('file', f);
    try {
      const r = await apiForm('material-upload', fd);
      if (r.error) { if (sub) sub.textContent = '没传上去：' + r.error; return; }
      s.uid = r.uid;
      s.files = (s.files || []).concat(r.items || []);
      const bad = r.rejected || [];
      if (sub) {
        sub.textContent = `已传 ${s.files.length} 个`
          + (bad.length ? `（跳过 ${bad.length} 个不支持：${bad.map(x => x.name).join('、')}）` : '');
      }
      renderNewCardFiles();
    } catch (ex) {
      if (sub) sub.textContent = '上传失败：' + ex.message;
    }
  };

  $('#ncAI').onclick = async () => {
    const text = ($('#ncTalk')?.value || '').trim();
    const intro = ($('#ncIntro')?.value || '').trim();
    if (text.length < 12) return toast('再多说两句', '至少告诉我他是谁、要做什么号', 'err');
    s.intro = intro;
    closeModal();
    // 有素材就把 uid 一起带过去：后端会先看图测音，再据此整理草案。
    // 不带 uid 的话，AI 手里只剩一段文字，形象真值就只能靠编。
    //
    // 走执行引擎而不是干等：模型可能先试几条车道才通，那段过程要看得见，
    // 否则用户只会得到一个 20 秒不动的按钮，然后以为坏了。
    await startExec({ kind: 'ai-card-draft', text, intro, uid: s.uid || '' }, {
      onDone: r => showNewCardDraft(r, text),
      then: d => { if (d.state !== 'done') showNewCardManual(text, true); },
    });
  };
  renderNewCardFiles();
}

/* 已上传素材的清单。图标区分类型，方便一眼看出图传没传、音传没传。 */
function renderNewCardFiles() {
  const box = $('#ncFiles');
  if (!box) return;
  const s = state.newCard || {};
  const files = s.files || [];
  if (!files.length) { box.innerHTML = ''; return; }
  box.innerHTML = files.map((f, i) => `
    <div class="mat">
      ${f.kind === 'image'
        ? `<img class="mth" loading="lazy" src="${previewSrc(f.path)}"
                onerror="this.style.display='none'">`
        : `<span class="mk audio">音</span>`}
      <span class="mn" title="${esc(f.name)}">${esc(f.name)}</span>
      <span class="ms">${humanSize(f.size)}</span>
      <button class="btn icon sm" data-rm="${i}" title="移除">${ico('close')}</button>
    </div>`).join('');
  $$('#ncFiles [data-rm]').forEach(b => {
    b.onclick = () => {
      s.files.splice(+b.dataset.rm, 1);
      renderNewCardFiles();
      const sub = $('#ncPickSub');
      if (sub) {
        sub.textContent = s.files.length
          ? `已传 ${s.files.length} 个`
          : '图片 png/jpg/webp · 音频 wav/mp3/flac/m4a，可多选';
      }
    };
  });
}

/* 图片/音频预览：走后端的 raw 通道（只允许档案区和项目根，越界会被后端挡掉）。 */
function previewSrc(p) {
  return '/api/raw?path=' + encodeURIComponent(p);
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
      <label class="field"><span>内容能不能走云端模型</span>
        <select id="ncCloud">
          <option value="0" ${d.type === '本人IP' ? '' : 'selected'}>不许 —— 客户商业信息，只走本机模型</option>
          <option value="1" ${d.type === '本人IP' ? 'selected' : ''}>可以 —— 本人 IP，或客户已书面同意</option>
        </select>
        <span class="hint">云端模型的提示词可能被上游记录。选「不许」时，写文案/选题要本机模型能跑。</span></label>
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
    // ★ 值必须在「问确认」之前全部取出来。
    //   confirmBox 会把整个 #modalBox 换掉，之后 $('#ncName') 一律是 null ——
    //   那不只是按钮变灰失败：name 会变成空串，card-create 直接回「缺目录名」，
    //   而弹层已经关了，人看到的就是「点了一下，什么也没发生」。
    const vals = {
      name: v('ncName'),
      aliases: v('ncAlias').split(/[,，]/).map(s => s.trim()).filter(Boolean),
      type: v('ncType'), tag: v('ncTag'), role: v('ncRole'), space: v('ncSpace'),
      allow_cloud_llm: v('ncCloud') === '1',
    };
    if (!vals.name || !vals.aliases.length || !vals.space)
      return toast('还差东西', '名字、别名、空间名必填（没空间名脚本会卡住等输入）', 'err');
    if (!(await confirmBox('确认建档？',
      `<div>会在档案区建整套目录、写 card.json、登记进总册，并写 ${mdNames.length} 篇人设文档。</div>
       <div class="hint" style="margin-top:8px">名字就是目录名，建完再改名很脏 —— 确认无误再继续。</div>
       <div class="hint" style="margin-top:8px">内容${vals.allow_cloud_llm ? '<b>可以</b>走云端模型' : '<b>不许</b>走云端模型（走本机）'}。</div>`,
      '确认建档'))) return;
    $('#modalBox').innerHTML = `<div class="modal-head"><b>${ico('refresh')} 正在建档…</b>
        <div class="spacer"></div></div>
      <div class="hint">建目录树 → 写 card.json → 登记总册 → 写 ${mdNames.length} 篇人设文档。
        这一步不调模型，几秒就好。</div>`;
    const nc = state.newCard || {};
    const rr = await api('card-create', {
      body: {
        draft: { ...vals, needs: d.needs || {}, redlines: d.redlines || [], habits: d.habits || [] },
        persona_md: md,
        // 传上来的形象素材：后端建档后会把它们搬进 02_素材真值/ 并写进 ip.json
        materials: (nc.files || []).map(f => ({
          uid: nc.uid || '',
          saved: f.saved,
          kind: f.kind,
          role: f.kind === 'audio' ? 'ref' : (f.role || 'portrait'),
        })),
      }
    });
    if (rr.error) {
      closeModal();
      return toast('没建成', rr.error + (rr.detail ? '：' + rr.detail : ''), 'err');
    }
    closeModal();
    const nm = (rr.materials || []).length;
    toast('档案已建', `${rr.name} · 写了 ${(rr.md || []).length} 篇人设文档`
      + (nm ? ` · 入库 ${nm} 个素材` : ''), 'ok');
    // 素材已入库，暂存状态清掉 —— 下一张卡重新开始，别把上一家的图带过去
    state.newCard = { uid: null, files: [], intro: '' };
    await boot();
    // 建完直接停在新建的这张卡上 —— 不然人还得自己去左边找，第一反应是「没建成吧」
    await selectIP(rr.name);
    showNextStepHint(rr);
  };
}

/* 建完卡之后最该干的一件事是开第一套片。这里不替他点，只把路指到那一格。 */
/* 建完卡别就这么散了。
   人刚填完一堆需求，最想知道的不是"文件夹建在哪"，而是**这个角色开口是什么味道**。
   所以第一件事给「按这个角色写一条爆款文案」，第二件才是开第一套片。 */
function showNextStepHint(rr) {
  const name = (rr && rr.name) || state.ip || '';
  const mdCount = ((rr && rr.md) || []).length;
  $('#modalBox').innerHTML = `<div class="modal-head"><b>${ico('check')} 档案建好了</b>
      <span class="sub">${esc(name)}</span>
      <div class="spacer"></div><button class="btn icon" id="mclose">${ico('close')}</button></div>
    <div class="hint">目录、card.json、总册登记都办完了${mdCount ? `，还写了 ${mdCount} 篇人设文档` : ''}。
      界面已经停在这张卡上。</div>
    <div class="sec-title" style="margin-top:16px">下一步做哪个</div>
    <div class="pseg" id="nstepScript" style="cursor:pointer">
      <div class="tx">① 按这个角色写一条爆款文案</div>
      <div class="hint" style="margin-top:2px">用他刚说的需求 + 主号跑量稿的标准，先看一条样稿 ——
        能立刻判断这个号立不立得住。花一次模型调用。</div>
    </div>
    <div class="pseg" id="nstepSet" style="cursor:pointer">
      <div class="tx">② 开第一套片子</div>
      <div class="hint" style="margin-top:2px">建完就有编号，然后按流程走：文案 → 音频 → 画面 → 字幕。</div>
    </div>`;
  $('#modal').classList.add('show');
  $('#mclose').onclick = closeModal;
  $('#nstepScript').onclick = async () => {
    closeModal();
    await startExec({ kind: 'ai-script', ip: state.ip }, {
      onDone: r => showScriptResult(r),
    });
  };
  $('#nstepSet').onclick = () => { closeModal(); void showNewSet(); };
}

/* 手填（保留原表单，AI 不可用时也不堵路） */
function showBatch() {
  // 复用后端 build_exec 的 batch 分支：前端只组 payload（ip / topics / sets / tier / dry / stop_on_error），
  // 真正拼命令、串行跑、写报告都在后端。点「开始批量」走 startExec —— 它会在吃 GPU 时弹二次确认。
  const ips = state.ips || [];
  if (!ips.length) return toast('还没有 IP', '先去「新建档案卡」接一个客户', 'err');
  const opts = ips.map(i =>
    `<option value="${esc(i.name)}"${i.name === state.ip ? ' selected' : ''}>${esc(i.name)}</option>`).join('');
  $('#modalBox').innerHTML = `<div class="modal-head"><b>${ico('layers')} 批量出片</b>
      <span class="sub">一份队列 · 串行（GPU 只有一个）</span>
      <div class="spacer"></div><button class="btn icon" id="mclose">${ico('close')}</button></div>
    <div class="form-grid">
      <label class="field"><span>IP / 账号 *</span>
        <select id="bIp">${opts}</select></label>
      <label class="field"><span>档位</span>
        <select id="bTier">
          <option value="">默认（配置）</option>
          <option value="fast">fast · 快速档</option>
          <option value="pilot">pilot · 试跑</option>
          <option value="std">std · 标准</option>
          <option value="high">high · 高清</option>
        </select></label>
    </div>
    <label class="field" style="margin-top:6px"><span>选题（每行一条，逐条 AI 写文案 + 出片）</span>
      <textarea id="bTopics" class="brief-area" style="min-height:120px"
        placeholder="为什么我不用真人出镜&#10;一条片子到底多少钱&#10;AI 分身会不会显得假"></textarea>
      <span class="hint">套次号不写就按这个 IP 现有最大编号往后顺排（S05 之后即 S06）。</span></label>
    <label class="field" style="margin-top:6px"><span>套次号（可选，逗号分隔，数量须与选题一致）</span>
      <input id="bSets" placeholder="留空 = 自动顺排；要写就和上面行数对齐"></label>
    <div class="chk-row" style="margin:12px 0 4px">
      <label class="chk"><input type="checkbox" id="bDry"> ${ico('eye')} 先看计划（只算要出哪些，不真烧 GPU）</label>
      <label class="chk"><input type="checkbox" id="bStop"> ${ico('alert')} 某条失败就停（默认继续跑剩下的）</label>
    </div>
    <div class="hint" style="margin-bottom:12px">批量是排队不是并发：这台机器一块 4080S，并行只会撞 OOM 被内核杀。
      一条接一条出，结束给汇总报告（写在 08_过程/batch-*.json）。</div>
    <div style="display:flex;gap:8px;justify-content:flex-end">
      <button class="btn ghost" id="mclose2">取消</button>
      <button class="btn primary" id="bGo">${ico('play')} 开始批量</button></div>`;
  $('#modal').classList.add('show');
  $('#mclose').onclick = $('#mclose2').onclick = closeModal;
  $('#bGo').onclick = () => {
    const ip = ($('#bIp').value || '').trim();
    const topics = ($('#bTopics').value || '').split('\n').map(t => t.trim()).filter(Boolean);
    if (!ip) return toast('先选个 IP', '', 'err');
    if (!topics.length) return toast('至少写一条选题', '每行一条', 'err');
    const sets = ($('#bSets').value || '').split(',').map(s => s.trim()).filter(Boolean);
    if (sets.length && sets.length !== topics.length)
      return toast('套次号数量对不上', `选题 ${topics.length} 条，套次号 ${sets.length} 个`, 'err');
    const payload = {
      kind: 'batch', ip, topics: topics.join(';'),
      tier: ($('#bTier').value || '').trim(),
      dry: $('#bDry').checked, stop_on_error: $('#bStop').checked,
    };
    if (sets.length) payload.sets = sets.join(',');
    closeModal();
    startExec(payload);
  };
}

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
      手填只建目录 + 空卡；需求、红线、人设文档都得后面补。想让 AI 顺手写人设，回上一步。<br>
      上云许可按类型自动给：本人 IP 允许，客户默认不许（走本机模型）。要改去卡里的 policy。</div>
    <div style="display:flex;gap:8px;justify-content:flex-end">
      <button class="btn ghost" id="mclose2">取消</button>
      <button class="btn primary" id="ncGo">建档</button></div>`;
  $('#modal').classList.add('show');
  $('#mclose').onclick = $('#mclose2').onclick = closeModal;
  $('#ncGo').onclick = async () => {
    const v = id => ($('#' + id)?.value || '').trim();
    // 先取值再动弹层：后面 closeModal 之后还能读，但别依赖这个巧合
    const name = v('ncName'), aliases = v('ncAlias'), space = v('ncSpace');
    if (!name || !aliases || !space)
      return toast('还差东西', '名字、别名、空间名是必填（不给空间名脚本会卡住等输入）', 'err');
    // ★ 手填也走 card-create，别自己拼 card-new 的参：
    //   走 CLI 那条路不会写 policy，于是新卡「默认禁止上云」，
    //   连 AI 选题/写文案都跑不了，而人根本不知道是为什么。
    const nc = state.newCard || {};
    const r = await api('card-create', {
      body: {
        draft: {
          name,
          aliases: aliases.split(/[,，]/).map(s => s.trim()).filter(Boolean),
          type: v('ncType'), tag: v('ncTag') || 'C', role: v('ncRole'), space,
        },
        persona_md: {},
        materials: (nc.files || []).map(f => ({
          uid: nc.uid || '',
          saved: f.saved,
          kind: f.kind,
          role: f.kind === 'audio' ? 'ref' : (f.role || 'portrait'),
        })),
      }
    });
    if (r.error) return toast('没建成', r.error + (r.detail ? '：' + r.detail : ''), 'err');
    closeModal();
    const nm = (r.materials || []).length;
    toast('档案已建', name + ' · 手填的空卡，需求和红线后面补'
      + (nm ? ` · 入库 ${nm} 个素材` : ''), 'ok');
    state.newCard = { uid: null, files: [], intro: '' };
    await boot();
    await selectIP(name);
    showNextStepHint(r);
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
    //
    // ★ 但要先说清楚：这一步会花掉一次模型调用。以前是点了就直接跑，
    //   界面只变成「正在按人设出题…」，人既不知道在花钱、也不知道要等多久，
    //   第一反应就是「按钮坏了」。
    if (!title) {
      const go = await confirmBox('标题还空着',
        `<div>标题是后面提示词和人设对齐的依据，空着会一路歪。</div>
         <div class="hint" style="margin-top:8px">
           可以让 AI 按这个号的人设出一条（<b>花一次模型调用</b>，几秒到几十秒，
           走哪家车道会在右下角日志里看到）；也可以自己写一句再建。</div>`,
        '让 AI 出题');
      if (!go) {
        if ($('#nsTitle')) $('#nsTitle').focus();
        return toast('先写个标题', '一句话就够，比如「汤熬不白别怪火大」', '');
      }
      const b = $('#nsGo');
      b.disabled = true; b.textContent = 'AI 出题中…';
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

  /* ---------- 提示词：就地编辑 ---------- */
  document.addEventListener('click', e => {
    const ed = e.target.closest('[data-pset]');
    const sv = e.target.closest('[data-psave]');
    const cc = e.target.closest('[data-pcancel]');
    if (!ed && !sv && !cc) return;
    if (ed) {
      const n = ed.dataset.pset;
      const card = document.querySelector(`.pseg[data-seg="${n}"]`);
      const box = card && card.querySelector('.pedit');
      if (!box) return;
      box.hidden = !box.hidden;
      ed.textContent = box.hidden ? '改' : '收起';
      if (!box.hidden) {
        const ta = box.querySelector('textarea');
        ta.focus();
        ta.setSelectionRange(ta.value.length, ta.value.length);
      }
      return;
    }
    if (sv) { e.preventDefault(); return void savePromptSeg(sv.dataset.psave); }
    if (cc) {
      const n = cc.dataset.pcancel;
      const card = document.querySelector(`.pseg[data-seg="${n}"]`);
      if (card) {
        card.querySelector('.pedit').hidden = true;
        const b = card.querySelector('[data-pset]');
        if (b) b.textContent = '改';
      }
    }
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
        // 中文分镜 → prompts.en.json：通了这一步，这份稿子才真的能出到画面上
        'prompts-en': { kind: 'prompts-en' },
        'prompts-en-force': { kind: 'prompts-en', force: true },
      };
      if (map[kind]) {
        return void await startExec({ ...map[kind], ip: state.ip, set: state.setId },
          // 译完把状态条刷一遍：不然还得手动刷新才知道自己现在已经「同源」了
          kind.startsWith('prompts-en')
            ? { then: async d => { if (d.state === 'done') { await loadPrompts(); renderPrompts(); } } }
            : undefined);
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
      // 失败后点这个 = 接着跑：from 指向失败那一步，且**不带 force** ——
      // 已完成的步骤由 pipeline 按产物自动跳过，不会把 GPU 时间再烧一遍。
      if (run.retry) {
        // ★ 自动学习闭环的「记」那一半：重跑前先问一句为什么。
        //   不问的话，这次学费白交 —— 下一次照样踩同一个坑。
        //   填不填都不阻断重跑（留空 = 这次不记）。
        const why = await askText(
          '这版哪里不满意？',
          '写一句原因，会记进这个 IP 的返工记录；以后出稿 AI 会主动避开。留空就不记。',
          '');
        if (why && why.trim()) {
          api('learn-add', { body: { ip: state.ip, kind: '重跑',
            reason: why.trim(), set: state.setId } });
        }
        return void await startExec({ ...run.retry, confirm: true });
      }
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
    if (e.target.id === 'trialStill' || e.target.id === 'trialSample' || e.target.closest('#trialMode')) {
      return void saveTrialCfg();
    }
  });
  $('#dlgInput').addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendDialogue(); }
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
    if (e.target.closest('#btnBatch')) return void showBatch();
    if (e.target.closest('#btnNewCard')) return void showNewCard();
    if (e.target.closest('#hNewSet')) return void showNewSet();
    if (e.target.closest('#btnFontApply')) return void saveFont();
    if (e.target.closest('#btnBriefSave') || e.target.closest('#btnBriefSave2')) return void saveBrief(false);
    // ---- 人设与规范 ----
    // ★ 两个入口刻意分开：默认「替换这一篇」，要写第二份必须显式选 ——
    //   同一个 IP 有两份互相矛盾的人设，AI 会各信一半，比没有更糟。
    if (e.target.closest('#btnPersonaSave')) {
      return void savePersona(state.personaCur || '', state.personaCur ? 'replace' : 'new');
    }
    if (e.target.closest('#btnPersonaSaveNew')) {
      const nm = await askText('另存为新一篇',
        '起个文件名。已有的文件不会被改动，新内容写成独立的一份。', '人设-补充.md', '存成新的一篇');
      return void savePersona(nm, 'new');
    }
    if (e.target.closest('#btnPersonaAI') || e.target.closest('#btnPersonaDraft'))
      return void busyBtn(e.target.closest('button'), '起草中…', aiDraftPersona);
    const pfile = e.target.closest('[data-persona-file]');
    if (pfile) {
      state.personaCur = pfile.getAttribute('data-persona-file');
      return void openPersonaFile(state.personaCur);
    }
    if (e.target.closest('[data-voice-design]'))
      return void busyBtn(e.target.closest('button'), '设计中…', designVoice);
    // ---- 试片台 ----
    if (e.target.closest('[data-gate]')) {
      const g = e.target.closest('[data-gate]').getAttribute('data-gate');
      state.trial.gatePending = null; renderTrialSteps();
      if (state._gateResolve) { const r = state._gateResolve; state._gateResolve = null; r(g); }
      return;
    }
    const trun = e.target.closest('[data-trial-run]');
    if (trun) {
      const key = trun.getAttribute('data-trial-run');
      return void runTrialStep(key, state.trial.mode !== 'manual');
    }
    if (e.target.closest('#btnTrialRun')) return void runTrialChain();
    // ---- 定帧素材：三种取法 ----
    if (e.target.closest('#stillMode')) {
      // 先把正在写的存住：切模式会重画整个面板，不存就丢了
      state.stillWho = ($('#stillWho') || {}).value || state.stillWho || '';
      state.stillScene = ($('#stillScene') || {}).value || state.stillScene || '';
      state.stillPromptEn = ($('#stillPromptEn') || {}).value || state.stillPromptEn || '';
      const picked = document.querySelector('input[name="smode"]:checked');
      if (picked) state.stillMode = picked.value;
      await renderTrial();
      if (state.stillMode === 'pick') await renderStillPick();
      return;
    }
    if (e.target.closest('#btnStillAI'))
      return void busyBtn(e.target.closest('button'), '写 / 润色中…', genStillPrompt);
    if (e.target.closest('[data-visual]')) {
      const el = e.target.closest('[data-visual]');
      return void applyVisual(el.getAttribute('data-name'), el.getAttribute('data-visual'));
    }

    // ---- 声音面板：配音 + 配乐 ----
    if (e.target.closest('#btnAudioRefresh')) return void renderAudio();
    if (e.target.closest('#bgmSource')) {
      const picked = document.querySelector('input[name="bgsrc"]:checked');
      if (picked) {
        const r = await api('set', { body: { scope: 'ip', key: 'bgm.source',
                                             value: picked.value, path: state.ip } });
        if (r.error) return toast('没改成', r.error, 'err');
        await loadIpCfg(); await renderAudio();
      }
      return;
    }
    if (e.target.closest('#btnBgmSave')) return void saveBgmParams(false);
    if (e.target.closest('#btnBgmRandom')) return void saveBgmParams(true);
    if (e.target.closest('#btnBgmProbe'))
      return void busyBtn(e.target.closest('button'), '生成 12 秒…', probeBgm);
    if (e.target.closest('#btnTtsInstrSave')) return void saveTtsInstr();
    if (e.target.closest('#btnTtsRedoAudio') || e.target.closest('#btnTtsRedoAll')) {
      const only = !!e.target.closest('#btnTtsRedoAudio');
      if (!state.setId) return void toast('先选一套片子', '', 'err');
      await saveTtsInstr(true);          // 「改了没生效」的头号成因：改了不存就重抽
      return void startExec({ kind: 'tts-redo', ip: state.ip, set: state.setId, only_audio: only });
    }
    // 写文案前先把需求存一次：人刚改完需求就点写文案，不先存等于白改
    if (e.target.closest('#btnScriptGen'))
      return void busyBtn(e.target.closest('button'), '先看需求…', async () => { await saveBrief(false); await genScript(); });
    if (e.target.closest('#btnBriefGen'))
      return void busyBtn(e.target.closest('button'), '存并生成…', () => saveBrief(true));
    if (e.target.closest('#hGuard') || e.target.closest('#btnGuard'))
      return void busyBtn(e.target.closest('button'), '自检中…', runGuard);
    if (e.target.closest('#hAlgo')) return void showAlgo();
    if (e.target.closest('#hCmd')) {
      // 以前这里是「复制命令」。人真正想点的是「开跑」，而且要先看一眼计划。
      return void startExec({ kind: 'pipeline', ip: state.ip, set: state.setId, tier: 'fast', dry: true });
    }
    if (e.target.closest('#btnUp')) return void renderFiles(state.fsParent);
    if (e.target.closest('#btnCmdGen')) {
      return void busyBtn(e.target.closest('button'), '生成中…', () => genPrompts('prompts-gen'));
    }
    if (e.target.closest('#btnCmdOpt')) {
      return void busyBtn(e.target.closest('button'), '优化中…', () => genPrompts('prompts-opt'));
    }
    if (e.target.closest('#actNote')) {
      return void showNoteDialog();
    }
    if (e.target.closest('#actIndex')) {
      const r = await api('run', { body: { cmd: 'index', args: ['--write'] } });
      toast(r.cmdline ? '已提交' : '失败', r.cmdline || r.error || '', r.error ? 'err' : 'ok');
      setTimeout(async () => { await loadJobs(); await renderTree(); }, 1500);
    }
    // ---- 总沟通对话区 ----
    if (e.target.closest('#dlgSend')) return void sendDialogue();
    if (e.target.closest('#btnDialogueClear')) {
      // ★ 以前是「一点就清」：没有确认、没有提示、也找不回来。
      //   小白手一抖点上去，前面聊了十几轮的需求上下文全没了。
      //   2026-10-30 改成先问一句，并且明说「丢的是什么、不丢的是什么」。
      const n = (state.dialogue || []).length;
      if (!n) { toast('对话区已经是空的'); return; }
      const ok = await confirmBox(
        `清空这 ${n} 条对话？`,
        `<p>清空的是<b>界面上的对话记录</b>，清除后<b>找不回来</b>。</p>
         <p class="hint" style="margin-top:6px">已经存进档案卡 / ip.json / brief.md 的东西
         不受影响——那些是确认过的落盘数据。<br>
         如果只是想把地方腾出来，用这块面板右上角的折叠就行，不用清空。</p>`,
        '清空');
      if (!ok) return;
      state.dialogue = []; renderDialogue(); toast(`已清空 ${n} 条对话`); return;
    }
  });
}

/* 记一条习惯 / 红线 / 需求。
   以前是 prompt() + confirm()：浏览器原生弹框，在这种玻璃界面上像从别人家借来的；
   更糟的是两段问 —— 先打字、再确认「是不是红线」，填错了只能从头再来。
   现在一张表单，一次填完，且能选三种类型（需求以前根本记不了）。 */
function showNoteDialog() {
  $('#modalBox').innerHTML = `<div class="modal-head"><b>${ico('plus')} 记一条</b>
      <span class="sub">写进 ${esc(state.ip || '')} 的 card.json</span>
      <div class="spacer"></div><button class="btn icon" id="mclose">${ico('close')}</button></div>
    <label class="field"><span>内容</span>
      <textarea id="noteText" class="brief-area" style="min-height:88px"
        placeholder="例：片子里不许出现同行品牌名 ／ 字幕一律用品牌蓝 #1A5FA8 ／ 每周三交付两条"></textarea></label>
    <label class="field"><span>记成什么</span>
      <select id="noteKind">
        <option value="habit">习惯 —— 他做事的方式（烧字幕、交付节奏这类）</option>
        <option value="redline">红线 —— 绝对不能碰，会进护栏拦提示词</option>
        <option value="need">需求 —— 他要什么（内容方向、平台、频率）</option>
      </select></label>
    <div class="hint">记成红线的，生成提示词时命中就直接被拦下来，不会先跑一遍再报错。</div>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px">
      <button class="btn ghost" id="noteCancel">取消</button>
      <button class="btn primary" id="noteSave">${ico('save')} 记下来</button></div>`;
  $('#modal').classList.add('show');
  $('#mclose').onclick = closeModal;
  $('#noteCancel').onclick = closeModal;
  $('#noteSave').onclick = async () => {
    const t = ($('#noteText').value || '').trim();
    const kind = $('#noteKind').value;
    if (t.length < 2) return toast('写点什么', '一句话就够', 'err');
    const args = [state.ip, t, '--kind', kind];
    const r = await api('run', { body: { cmd: 'card-note', args } });
    if (r.error) return toast('没记上', r.error, 'err');
    toast('已记下', kind === 'redline' ? '这条会进护栏' : '', 'ok');
    closeModal();
    await waitTask(r.task || r.id, 8000);
    await loadCard();
    if (state.tab === 'inspect') await renderInspect();
  };
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
