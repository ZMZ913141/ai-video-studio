#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ui_server —— 本地网页工作台的后端（零第三方依赖）

★ 铁律延续（前三条 UI 护栏）：
  ① 界面不发明逻辑 —— 每个数字、每个按钮背后就是 vstudio CLI 或某份真值文件
  ② 同一参数只存一处 —— UI 从不缓存一份自己的副本，改哪读哪
  ③ 重活不出界面 —— pipeline 出片只给命令串让用户回终端跑；界面只做「安全动作」

所以本文件里没有半个业务判断：数据一律 subprocess 调 bin/vstudio.py --json，
或只读 specs 的真值文件（ip.json / card.json / config/pipeline.json）。
写操作走白名单 + 二次确认 + 自动 changelog。

跑起来：
    python3 bin/ui_server.py            # http://127.0.0.1:8790
    python3 bin/ui_server.py --port 9000 --open
"""
import argparse
import atexit
import hashlib
import json
import mimetypes
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BIN_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BIN_DIR)
# 让 server 能直接 import 同目录的 studio_ai / prompt_guard（只读调用，不复制逻辑）
if BIN_DIR not in sys.path:
    sys.path.insert(0, BIN_DIR)
UI_DIR = os.path.join(ROOT, "ui")
ARCHIVE = os.environ.get("STUDIO_ARCHIVE") or "/cfu/ai视频档案库"
CACHE_DIR = os.path.join(ROOT, ".cache", "ui")
THUMB_DIR = os.path.join(CACHE_DIR, "thumbs")
CHANGELOG_GLOBAL = os.path.join(ROOT, "config", ".ui_changelog.jsonl")
# 端口会被自动顺延，光看命令猜不准。写个小纸条，谁来查地址都一致。
PORTFILE = os.path.join(tempfile.gettempdir(), "vstudio-ui.json")

PY = sys.executable if sys.version_info >= (3, 9) else "python3"
VSTUDIO = os.path.join(BIN_DIR, "vstudio.py")

# 允许被浏览/读取的根（防路径越界：任何 fs 请求都必须在这些前缀之内）
ALLOW_ROOTS = [
    os.path.realpath(ARCHIVE),
    os.path.realpath(ROOT),
]

MEDIA_EXT = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".mp3", ".wav", ".flac", ".m4a", ".ogg"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}

# ---------- 写操作白名单 ----------
#   tier2=True 的命令必须带 confirm=1 才放行，且自动记 changelog
#   这里的 key 就是 vstudio 的子命令名，参数一律以数组传入（不走 shell，天然免疫注入）
WRITE_ACTIONS = {
    "index":        {"tier": 1, "desc": "重建套次索引"},
    "job-new":      {"tier": 1, "desc": "开一套新片"},
    "job-set":      {"tier": 1, "desc": "推进阶段"},
    "card-new":     {"tier": 1, "desc": "新建档案卡"},
    "card-note":    {"tier": 1, "desc": "记一条习惯/需求/红线"},
    "scripts-sync": {"tier": 2, "desc": "把引擎脚本真源覆盖到该 IP（会先备份）"},
}
READ_ACTIONS = {
    "ips", "jobs", "job-show", "cards", "card-show", "scripts", "scripts-diff",
    "status", "algo", "guard", "llm", "prompts", "pipeline",
}

# ---------- 可写配置（第二层=高级，需二次确认） ----------
#   全局层 config/pipeline.json
GLOBAL_WRITABLE = {
    "defaults.tier":              {"tier": 1, "type": "str",   "label": "默认出片档位", "enum": ["fast", "pilot", "std", "high"]},
    "defaults.crf":               {"tier": 1, "type": "int",   "label": "默认 CRF", "min": 12, "max": 28},
    "defaults.maxline":           {"tier": 1, "type": "int",   "label": "字幕每行字数", "min": 6, "max": 20},
    "defaults.overlap":           {"tier": 1, "type": "int",   "label": "重叠帧", "min": 0, "max": 30},
    "defaults.target_seg_secs":   {"tier": 1, "type": "float", "label": "目标段长(秒)", "min": 3, "max": 20},
    "global_prompt.enabled":      {"tier": 1, "type": "bool",  "label": "全局通用片段启用"},
    "global_prompt.prefix":       {"tier": 2, "type": "text",  "label": "全局通用片段（正向描述，禁否定式）", "maxlen": 800},
    "guard.max_chars":            {"tier": 2, "type": "int",   "label": "提示词最大字数", "min": 100, "max": 2000},
    "pythons.std":                {"tier": 2, "type": "str",   "label": "标准解释器路径"},
    "pythons.numpy":              {"tier": 2, "type": "str",   "label": "numpy 解释器路径"},
    "ffmpeg":                     {"tier": 2, "type": "str",   "label": "ffmpeg 路径"},
}
#   个人层 <ip>/ip.json
IP_WRITABLE = {
    "video.mp":                   {"tier": 1, "type": "float", "label": "出片分辨率 MP", "min": 0.3, "max": 4.0},
    "subtitle.fs_cue":            {"tier": 1, "type": "int",   "label": "正文字号", "min": 20, "max": 120},
    "subtitle.fs_title":          {"tier": 1, "type": "int",   "label": "标题字号", "min": 20, "max": 140},
    # 字体：名字必须和字体文件里的真实 family 一致，写错 libass 会静默回退 Noto
    # （字形全错、还不报错），所以算第二层，改完要重跑 ass + burn 两步
    "subtitle.font_cue":          {"tier": 2, "type": "str",   "label": "正文字体"},
    "subtitle.font_cue_b":        {"tier": 2, "type": "str",   "label": "强调字体（CTA/花字）"},
    "subtitle.font_title":        {"tier": 2, "type": "str",   "label": "标题字体"},
    "voice.tts.instruction":      {"tier": 1, "type": "text",  "label": "音色语气指令", "maxlen": 300},
    "voice.tts.cfg_scale":        {"tier": 2, "type": "float", "label": "CFG", "min": 0.5, "max": 3.0},
    "video.overlap":              {"tier": 2, "type": "int",   "label": "重叠帧", "min": 0, "max": 30},
    "video.steps":                {"tier": 2, "type": "int",   "label": "采样步数", "min": 1, "max": 50},
    "video.lora.strength":        {"tier": 2, "type": "float", "label": "LoRA 强度", "min": 0.0, "max": 2.0},
    "video.sage_attention":       {"tier": 2, "type": "str",   "label": "Sage Attention", "enum": ["auto", "true", "false"]},
}

TASKS = {}
TASK_LOCK = threading.Lock()

# 中文业务阶段 → 流水线步骤区间 (from, to)。
# ★ 这个映射只在这里写一份：前端点进度节点时不该自己再列一遍步骤名，
#   否则 config/pipeline.json 一改，界面就开始说谎。
#   文案没有 step（人写的），所以不在表里 —— 缺表 = 给出「手工处理」的指引。
STAGE_SPAN = {
    "音频": ("tts", "pad"),
    "视频": ("video", "grade"),
    "后期": ("bgm", "mix"),
    "字幕": ("ass", "burn"),
    "验收": ("audit", "audit"),
}

# ---------- 基础 ----------
def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_llm_env():
    """把 config/llm.env 灌进本进程环境。

    服务进程不是登录 shell，不会自动 `set -a && . config/llm.env`。
    不灌的话，凡是要用 key 的子调用（探活、AI 选题、建档草案）
    都会报 MISSING_KEY —— 看着像「key 没配」，其实只是没人给它。

    setdefault 而非硬覆盖：用户自己在终端 export 的优先。
    """
    p = os.path.join(ROOT, "config", "llm.env")
    if not os.path.exists(p):
        return 0
    n = 0
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            if k and k not in os.environ:
                os.environ[k] = v.strip().strip('"').strip("'")
                n += 1
    return n

def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json_atomic(path, data):
    tmp = path + ".ui_tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def within_roots(real):
    return any(real == r or real.startswith(r + os.sep) for r in ALLOW_ROOTS)


def safe_path(p):
    """把请求里的路径关进 ALLOW_ROOTS 的笼子。越界一律 None。"""
    if not p:
        return None
    real = os.path.realpath(p)
    return real if within_roots(real) and os.path.exists(real) else None


def append_changelog(scope, ip, path, key, old, new, actor="ui"):
    """自动记录第二层（其实所有写都记）——违反了「界面改了没人知道」这条大忌"""
    line = {
        "at": now(), "scope": scope, "ip": ip or "", "file": os.path.basename(path),
        "key": key, "old": old, "new": new, "actor": actor,
    }
    target = CHANGELOG_GLOBAL
    if scope == "ip" and ip:
        root = ip_root(ip)
        if root:
            d = os.path.join(root, "08_过程")
            os.makedirs(d, exist_ok=True)
            target = os.path.join(d, "_UI变更.jsonl")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")
    return target


# ---------- CLI 转发 ----------
def run_cli(args, timeout=60):
    """--json 是全局参数，必须放在子命令之前，否则 argparse 吃不到"""
    cmd = [PY, VSTUDIO] + args
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           cwd=ROOT, errors="replace")
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"超时 {timeout}s"
    except Exception as ex:
        return 125, "", f"{type(ex).__name__}: {ex}"


def run_cli_script(args, timeout=60, python=None):
    """跑 bin/ 下的独立脚本（不是 vstudio 子命令）。

    为什么不 import 了直接调：这些脚本可能在别的解释器里才跑得动
    （MCP 那套要 mcp 包的 venv，numpy 那套要 envs/default）。
    用子进程 = 坏了解释器也只坏那一个功能，不会把整个网页服务拖崩。
    """
    cmd = [python or PY] + [os.path.join(BIN_DIR, a) if i == 0 else a
                            for i, a in enumerate(args)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           cwd=ROOT, errors="replace", stdin=subprocess.DEVNULL)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"超时 {timeout}s"
    except Exception as ex:
        return 125, "", f"{type(ex).__name__}: {ex}"


def run_git(args, cwd=None, timeout=20):
    """只读地取 git 里的旧版本（恢复默认要用）。
    绝不碰工作区：只 show / rev-parse，不做 checkout、reset。"""
    try:
        p = subprocess.run(["git"] + list(args), cwd=cwd or ROOT,
                           capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except Exception as ex:
        return 1, "", f"{type(ex).__name__}: {ex}"


def cli_json(args, timeout=60):
    rc, out, err = run_cli(["--json"] + args, timeout)
    if rc != 0 and not out.strip():
        return {"_error": err.strip() or f"exit {rc}"}
    try:
        # guard 之类「非零退出也是正常结果」的命令，stdout 可能有 JSON
        return json.loads(out)
    except Exception:
        # CLI 同时在 stdout 打了人类可读文本（job-new 就是这种混着来的）
        m = re.search(r"\{[\s\S]*\}$", out.strip())
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
        return {"_raw": out, "_stderr": err, "_rc": rc}


def ip_root(name):
    """由总册解析 IP → 目录。总册是唯一入口，不在 UI 里另猜路径。"""
    reg = load_json(os.path.join(ROOT, "registry.json"))
    if not name:
        return None
    for e in reg.get("ips", []):
        if name == e.get("name") or name in (e.get("aliases") or []):
            return e.get("path")
    return None


# ---------- 任务（长时间动作异步跑，前端轮询） ----------
def task_run(args, label):
    tid = hashlib.md5((str(args) + str(time.time())).encode()).hexdigest()[:12]
    with TASK_LOCK:
        TASKS[tid] = {"id": tid, "label": label, "args": args, "state": "running",
                      "out": "", "rc": None, "started": now()}
    t = threading.Thread(target=_worker, args=(tid, args), daemon=True)
    t.start()
    return tid


def _worker(tid, args):
    rc, out, err = run_cli(args, timeout=900)
    with TASK_LOCK:
        TASKS[tid].update({"state": "done" if rc == 0 else "failed",
                           "out": (out or "") + (("\n[stderr]\n" + err) if err.strip() else ""),
                           "rc": rc, "ended": now()})


# ---------- 视频规格探测（交付前自检要用） ----------
def probe_video(path):
    """用 `ffmpeg -i` 的 stderr 读规格。

    ★ 不用 ffprobe：这台机器上早前解出来的 ffprobe 是坏文件（已删），
      `ffmpeg -i` 什么都不输出到 stdout、全部规格都在 stderr 里，
      所以必须合并抓 stderr 再正则，别写成 capture stdout。
    """
    try:
        p = subprocess.run([ffmpeg_bin(), "-hide_banner", "-i", path],
                           capture_output=True, text=True, timeout=30,
                           errors="replace", stdin=subprocess.DEVNULL)
    except Exception:
        return None
    txt = (p.stderr or "") + (p.stdout or "")
    m = re.search(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})", txt)
    if not m:
        return None
    w, h = int(m.group(1)), int(m.group(2))
    d = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", txt)
    dur = (int(d.group(1)) * 3600 + int(d.group(2)) * 60 + float(d.group(3))) if d else None
    return {"w": w, "h": h, "mp": round(w * h / 1e6, 3), "dur": dur}


def deliver_precheck(ip, root, job, items):
    """交付前必须能回答的两个问题：齐不齐？这条片子的规格够不够格交付？

    ★ 为什么非查分辨率：S05 是 0.5MP 快速档试跑（本意就是给流程定性，不交付）。
      光看「文件都在、验收 ok」会得出「可以交付」的结论 —— 那是错的。
      规格对不上 ip.json 的目标 MP 时，必须当面说清这是试跑档。
    """
    checks = []
    stages = job.get("stages") or {}
    order = ["文案", "音频", "视频", "后期", "字幕", "验收"]
    bad = [s for s in order if (stages.get(s) or {}).get("status") != "ok"]
    checks.append({
        "name": "各阶段状态", "ok": not bad,
        "detail": "全部完成" if not bad else "还没完成：" + "、".join(bad),
    })

    # 交付阶段本身通常没有自己的产物目录，成片要从后期/视频那两栏里找
    arts = list(items)
    if not any(a.get("kind") == "video" for a in arts):
        allarts = job.get("artifacts") or {}
        for s in ("后期", "视频", "字幕"):
            for p in (allarts.get(s) or []):
                if os.path.exists(p):
                    arts.append({"name": os.path.basename(p), "path": p,
                                 "kind": "video" if p.lower().endswith((".mp4", ".mov"))
                                 else "other"})
    video = next((it for it in arts
                  if it.get("kind") == "video" and "混音" in it["name"]), None) \
        or next((it for it in arts if it.get("kind") == "video"), None)
    if not video:
        checks.append({"name": "成片", "ok": False, "detail": "没有可交付的视频"})
        return checks

    spec = probe_video(video["path"])
    target = 0.0
    try:
        target = float(load_json(os.path.join(root, "ip.json"))
                       .get("video", {}).get("mp") or 0)
    except Exception:
        pass
    if spec:
        ratio = (spec["mp"] / target) if target else 1.0
        trial = bool(target) and ratio < 0.75
        checks.append({
            "name": "分辨率", "ok": not trial,
            "detail": (f"{video['name']} 是 {spec['w']}×{spec['h']} = {spec['mp']}MP"
                       + (f"，目标是 {target}MP" if target else "")
                       + ("　→ 这是快速档试跑，不是正式交付件" if trial else "")),
            "trial": trial,
        })
        checks.append({"name": "时长", "ok": bool(spec["dur"]),
                       "detail": f"{spec['dur']:.1f}s" if spec["dur"] else "读不出时长"})
    else:
        checks.append({"name": "分辨率", "ok": False, "detail": "读不出规格（ffmpeg 没解析到）"})
    return checks


# ---------- 缩略图 ----------
def ffmpeg_bin():
    try:
        cfg = load_json(os.path.join(ROOT, "config", "pipeline.json"))
        ff = cfg.get("ffmpeg")
        if ff and os.path.exists(ff):
            return ff
    except Exception:
        pass
    return shutil.which("ffmpeg")


def make_thumb(src):
    """视频抽首帧当封面；图片直接返回原图（小于 300KB 才直接给，否则也缩一版）"""
    os.makedirs(THUMB_DIR, exist_ok=True)
    key = hashlib.md5(src.encode()).hexdigest()[:16]
    ext = os.path.splitext(src)[1].lower()
    dst = os.path.join(THUMB_DIR, key + (".png" if ext in MEDIA_EXT else ext))
    if os.path.exists(dst) and os.path.getmtime(dst) >= os.path.getmtime(src):
        return dst
    ff = ffmpeg_bin()
    if ext in IMAGE_EXT:
        if os.path.getsize(src) <= 400 * 1024:
            return src
        if not ff:
            return None
        subprocess.run([ff, "-nostdin", "-y", "-i", src, "-vf", "scale=420:-2", dst],
                       capture_output=True, timeout=60)
    elif ext in MEDIA_EXT:
        if not ff:
            return None
        subprocess.run([ff, "-nostdin", "-y", "-ss", "1", "-i", src,
                        "-frames:v", "1", "-vf", "scale=420:-2", dst],
                       capture_output=True, timeout=90)
    else:
        return None
    return dst if os.path.exists(dst) else None


# ---------- HTTP ----------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "vstudio-ui/0.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("  %s - %s\n" % (self.address_string(), fmt % args))

    # --- helpers ---
    def _send(self, code, ctype, body=b"", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, "application/json; charset=utf-8",
                   json.dumps(obj, ensure_ascii=False))

    def _404(self, msg="not found"):
        self._json({"error": msg}, 404)

    def _bad(self, msg):
        self._json({"error": msg}, 400)

    def _path_and_query(self):
        """★ http.server 的坑：self.path 是按 iso-8859-1 解出来的，
        中文（URL 直传或 percent-decode 前）会被切成一堆乱码字符。
        这里把 latin-1 的壳子退掉重新按 UTF-8 解码，否则「洁斌」会变成「æ´æ」。"""
        raw = self.path
        try:
            raw = raw.encode("iso-8859-1").decode("utf-8")
        except Exception:
            pass
        u = urllib.parse.urlparse(raw)
        return u.path, urllib.parse.parse_qs(u.query)

    def _query(self):
        return self._path_and_query()[1]

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        raw = self.rfile.read(n).decode("utf-8")
        try:
            return json.loads(raw)
        except Exception:
            return {}

    # --- routes ---
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path, q = self._path_and_query()
        if path == "/":
            path = "/index.html"
        if path.startswith("/api/"):
            self._api(path[5:], q)
            return
        self._static(path)

    def do_POST(self):
        path, q = self._path_and_query()
        if not path.startswith("/api/"):
            return self._404()
        self._api(path[5:], q, self._body())

    # 静态资源（只认 ui/ 目录）
    def _static(self, path):
        rel = path.lstrip("/")
        target = os.path.realpath(os.path.join(UI_DIR, rel))
        base = os.path.realpath(UI_DIR)
        if not (target == base or target.startswith(base + os.sep)) or not os.path.isfile(target):
            return self._404()
        ctype = mimetypes.guess_type(target)[0]
        if rel.endswith(".js"):
            ctype = "text/javascript; charset=utf-8"
        elif rel.endswith(".css"):
            ctype = "text/css; charset=utf-8"
        elif rel.endswith((".html", ".json", ".md")):
            ctype = (ctype or "text/plain") + "; charset=utf-8"
        else:
            ctype = ctype or "application/octet-stream"
        with open(target, "rb") as f:
            self._send(200, ctype, f.read())

    # ================= API =================
    def _api(self, name, q, body=None):
        get = lambda k, d=None: (q.get(k) or [d])[0]
        try:
            if name == "health":
                return self._json({"ok": True, "py": PY, "archive": ARCHIVE, "time": now()})
            if name == "services":
                return self._json({"services": self._services()})
            if name == "bootstrap":
                return self._json(self._bootstrap())
            if name == "ips":
                return self._json(cli_json(["ips"]))
            if name == "jobs":
                return self._json(cli_json(["jobs", get("ip", "洁斌")]))
            if name == "job":
                rc, out, err = run_cli(["--json", "job-show", get("ip", "洁斌"), get("id", "")])
                try:
                    return self._json(json.loads(out))
                except Exception:
                    return self._json({"_error": err.strip() or out.strip() or f"exit {rc}"}, 404)
            if name == "cards":
                return self._json(cli_json(["cards"]))
            if name == "card":
                ip = get("ip")
                root = ip_root(ip)
                if not root:
                    return self._bad("未知 IP")
                cp = os.path.join(root, "card.json")
                if not os.path.exists(cp):
                    return self._json({"error": "该 IP 没有 card.json"}, 404)
                return self._json(load_json(cp))
            if name == "guard":
                args = ["guard", get("ip", "洁斌")]
                rc, out, err = run_cli(args, timeout=120)
                return self._json({"rc": rc, "passed": rc == 0, "text": (out + err).strip()})
            if name == "scripts":
                return self._json(cli_json(["scripts", get("ip", "洁斌")]))
            if name == "scripts-diff":
                rc, out, err = run_cli(["--json", "scripts-diff"], timeout=120)
                try:
                    return self._json(json.loads(out))
                except Exception:
                    return self._json({"rc": rc, "text": out.strip(), "stderr": err.strip()})
            if name == "algo":
                args = ["algo", get("ip", "洁斌"), get("set", "")]
                for k in ("sec", "mp", "segs"):
                    if get(k):
                        args += ["--" + k, get(k)]
                return self._json(cli_json(args, timeout=60))
            if name == "status":
                return self._json(cli_json(["status"], timeout=90))
            if name == "config":
                return self._json(self._read_config())
            if name == "ipconfig":
                root = ip_root(get("ip"))
                if not root or not os.path.exists(os.path.join(root, "ip.json")):
                    return self._bad("该 IP 没有 ip.json")
                return self._json(load_json(os.path.join(root, "ip.json")))
            if name == "prompts":
                return self._json(self._read_prompts(get("ip"), get("set")))
            if name == "fs":
                return self._api_fs(get)
            if name == "raw":
                return self._api_raw(get)
            if name == "thumb":
                return self._api_thumb(get)
            if name == "text":
                p = safe_path(get("path"))
                if not p or os.path.isdir(p):
                    return self._bad("文件不可读")
                try:
                    with open(p, encoding="utf-8", errors="replace") as f:
                        txt = f.read()
                except Exception as ex:
                    return self._bad(str(ex))
                return self._json({"path": p, "size": len(txt), "text": txt[:200000]})
            if name == "task":
                tid = get("id")
                with TASK_LOCK:
                    t = TASKS.get(tid)
                if not t:
                    return self._404("任务不存在")
                return self._json(t)
            if name == "changelog":
                return self._api_changelog(get)
            if name == "run":
                return self._api_run(body or {})
            if name == "set":
                return self._api_set(body or {})
            if name == "cmdline":
                return self._api_cmdline(body or {})
            if name == "fonts":
                return self._api_fonts()
            if name == "defaults":
                return self._api_defaults(get)
            if name == "reset":
                return self._api_reset(body or {})
            if name == "brief":
                return self._api_brief(get, body)
            if name == "svc":
                return self._api_svc(get)
            if name == "llm-channels":
                return self._api_llm_channels()
            if name == "llm-prefer":
                return self._api_llm_prefer(body or {})
            if name == "llm-probe":
                return self._api_llm_probe(body or {})
            if name == "topics":
                return self._api_topics(body or {})
            if name == "stage":
                return self._api_stage(get)
            if name == "card-draft":
                return self._api_card_draft(body or {})
            if name == "card-create":
                return self._api_card_create(body or {})
            if name == "mcp":
                return self._api_mcp()
            return self._404("未知接口 " + name)
        except Exception as ex:
            import traceback
            traceback.print_exc()
            self._json({"error": f"{type(ex).__name__}: {ex}"}, 500)

    # --- 具体接口 ---
    def _services(self):
        import urllib.request as U
        out = {}
        targets = [
            ("pipeline", "http://127.0.0.1:8000/health", " 视频合成"),
            ("n8n", "http://127.0.0.1:5678", " 自动化编排"),
            ("comfyui", "http://127.0.0.1:8188/system_stats", " 图像/视频生成"),
            ("zen-gate", "http://127.0.0.1:8787/v1/models", " LLM 网关"),
        ]

        def probe(item):
            name, url = item[0], item[1]
            try:
                U.urlopen(url, timeout=3)
                return name, "up"
            except Exception as ex:
                return name, "down"

        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as ex:
            for name, st in ex.map(probe, targets):
                out[name] = st
        return out

    def _bootstrap(self):
        """首屏一次拿齐：IP 列表 + 服务状态 + 允许写的配置项元数据"""
        reg = load_json(os.path.join(ROOT, "registry.json"))
        ips = []
        for e in reg.get("ips", []):
            p = e.get("path", "")
            ips.append({
                "name": e.get("name"), "tag": e.get("tag"), "path": p,
                "aliases": e.get("aliases", []),
                "ready": os.path.isdir(p) and os.path.exists(os.path.join(p, "ip.json")),
            })
        return {
            "ips": ips,
            "excluded": [x.get("name") for x in reg.get("excluded", [])],
            "services": self._services(),
            "writable": {"global": GLOBAL_WRITABLE, "ip": IP_WRITABLE},
            "write_actions": WRITE_ACTIONS,
            "archive": ARCHIVE,
        }

    def _read_config(self):
        cfg = load_json(os.path.join(ROOT, "config", "pipeline.json"))
        # 密钥类字段不外发
        try:
            prov = load_json(os.path.join(ROOT, "config", "providers.json"))
        except Exception:
            prov = {}
        return {"pipeline": cfg, "providers_meta": {
            k: (("capabilities" in v) or ("models" in v))
            for k, v in prov.items() if isinstance(v, dict)}}

    def _read_prompts(self, ip, setname):
        root = ip_root(ip)
        if not root or not setname:
            return {"error": "缺 IP 或套次"}
        d = os.path.join(root, "05_文案", setname)
        pf = os.path.join(d, "prompts.json")
        if not os.path.exists(pf):
            return {"exists": False, "dir": d}
        try:
            return {"exists": True, "dir": d, "data": load_json(pf)}
        except Exception as ex:
            return {"exists": True, "dir": d, "error": str(ex)}

    def _api_fs(self, get):
        """列目录 —— 只在允许根内，且隐藏垃圾件"""
        p = get("path") or (ip_root(get("ip")) or ARCHIVE)
        real = safe_path(p)
        if not real:
            return self._bad("路径越界或不存在")
        if os.path.isfile(real):
            return self._json({"path": real, "is_file": True, "size": os.path.getsize(real)})
        try:
            names = sorted(os.listdir(real))
        except PermissionError:
            return self._bad("无权限")
        items = []
        skip = {"__pycache__", ".git", ".DS_Store", "_UI变更.jsonl"}
        for n in names:
            if n in skip or n.startswith("."):
                continue
            full = os.path.join(real, n)
            if os.path.isdir(full):
                items.append({"name": n, "type": "dir", "path": full})
            else:
                ext = os.path.splitext(n)[1].lower()
                sz = os.path.getsize(full)
                kind = "video" if ext in MEDIA_EXT and ext not in {".mp3", ".wav", ".flac", ".m4a", ".ogg"} else \
                       "audio" if ext in MEDIA_EXT else \
                       "image" if ext in IMAGE_EXT else \
                       "text" if ext in {".md", ".txt", ".srt", ".ass", ".json", ".py", ".csv"} else "file"
                items.append({"name": n, "type": kind, "path": full,
                              "size": sz, "mtime": datetime.fromtimestamp(os.path.getmtime(full)).strftime("%m-%d %H:%M")})
        items.sort(key=lambda x: (x["type"] != "dir", x["name"]))
        return self._json({"path": real, "parent": os.path.dirname(real), "items": items})

    def _api_thumb(self, get):
        p = safe_path(get("path"))
        if not p or os.path.isdir(p):
            return self._404()
        t = make_thumb(p)
        if not t:
            return self._404("无法生成缩略图")
        with open(t, "rb") as f:
            body = f.read()
        ext = os.path.splitext(t)[1].lower()
        ct = {".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp"}.get(ext, "image/png")
        self._send(200, ct, body)

    def _api_raw(self, get):
        """原文件/媒体流，支持 Range（视频拖动必须）"""
        p = safe_path(get("path"))
        if not p or not os.path.isfile(p):
            return self._404()
        size = os.path.getsize(p)
        ctype = mimetypes.guess_type(p)[0] or "application/octet-stream"
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        code = 200
        hdrs = {"Accept-Ranges": "bytes"}
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
                code = 206
                hdrs["Content-Range"] = f"bytes {start}-{end}/{size}"
        length = end - start + 1
        hdrs["Content-Length"] = str(length)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in hdrs.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(p, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                chunk = f.read(min(65536, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def _api_changelog(self, get):
        ip = get("ip")
        target = CHANGELOG_GLOBAL
        if ip:
            root = ip_root(ip)
            if root:
                target = os.path.join(root, "08_过程", "_UI变更.jsonl")
        lines = []
        if os.path.exists(target):
            with open(target, encoding="utf-8") as f:
                lines = [l.strip() for l in f if l.strip()]
        try:
            items = [json.loads(l) for l in lines]
        except Exception:
            items = []
        return self._json({"items": list(reversed(items[-100:])), "file": target})

    def _api_run(self, body):
        cmd = body.get("cmd")
        args = [str(x) for x in (body.get("args") or [])]
        confirm = bool(body.get("confirm"))
        if cmd not in WRITE_ACTIONS:
            return self._json({"error": f"界面不允许执行该动作：{cmd}（出片类请回终端跑）"}, 403)
        meta = WRITE_ACTIONS[cmd]
        if meta["tier"] == 2 and not confirm:
            return self._json({"error": "这是第二层动作，需要二次确认", "need_confirm": True,
                               "desc": meta["desc"]}, 428)
        if cmd == "scripts-sync" and "--force" in args and not confirm:
            return self._json({"error": "覆盖脚本需要二次确认", "need_confirm": True}, 428)
        for a in args:
            if a.startswith("--") and a[2:].split("=")[0] not in {
                "write", "force", "file", "name", "type", "tag", "role", "aliases",
                "space", "title", "artifact", "note", "stage", "json"}:
                return self._json({"error": f"不允许的参数：{a}"}, 400)
        full = [cmd] + args
        tid = task_run(full, meta["desc"])
        return self._json({"task": tid, "cmdline": "python3 bin/vstudio.py " + " ".join(full)})

    def _api_cmdline(self, body):
        """出片类：不执行，只把命令串拼好给用户复制回终端"""
        kind = body.get("kind")
        ip = body.get("ip", "洁斌")
        setname = body.get("set", "")
        step = body.get("step", "")
        table = {
            "pipeline":      f"python3 bin/vstudio.py pipeline {ip} {setname}",
            "pipeline-fast": f"python3 bin/vstudio.py pipeline {ip} {setname} --tier fast --dry",
            "pipeline-std":  f"python3 bin/vstudio.py pipeline {ip} {setname} --tier std",
            "prompts-gen":   f"python3 bin/vstudio.py prompts {ip} {setname} --gen",
            "prompts-opt":   f"python3 bin/vstudio.py prompts {ip} {setname} --optimize",
            "prompts-show":  f"python3 bin/vstudio.py prompts {ip} {setname} --show",
            "guard":         f"python3 bin/vstudio.py guard {ip}",
            "sync":          f"python3 bin/vstudio.py scripts-sync {ip}",
            "algo":          f"python3 bin/vstudio.py algo {ip} {setname}",
        }
        if kind == "redo-from":
            if not step:
                return self._bad("缺少 step")
            return self._json({"cmdline":
                f"python3 bin/vstudio.py pipeline {ip} {setname} --from {step}",
                "note": f"从「{step}」重跑，之前的产物保留不动；"
                        f"要先看它打算干什么就再加 --dry"})
        if kind == "redo-rest":
            # --to 是"只跑到这里为止"，不是"从这之后重做"，两者别混
            return self._json({"cmdline":
                f"python3 bin/vstudio.py pipeline {ip} {setname} --tier fast --dry"})
        if kind not in table:
            return self._bad("未知命令模板")
        return self._json({"cmdline": table[kind]})

    # ---------- 字体 ----------
    def _api_fonts(self):
        """两种字体要分清，混了就出废片：
        ① 系统里装了 270+ 个中文字体，**浏览器能预览**（前端直接用 CSS 渲染）；
        ② 真正能烧进字幕的只有 `02_素材真值/字体_字幕标题/` 里那几个 ——
           libass 的 fontsdir 给大目录会吃爆内存（沙箱 4G 上限），
           给空目录又会静默回退 Noto，字形全错还不报错。"""
        fams, burn_files = [], {}
        # 烧录白名单：按 IP 找精简字体目录
        try:
            ip_entries = cli_json(["ips"]).get("ips") or []
        except Exception:
            ip_entries = []
        for e in ip_entries:
            d = os.path.join(e["path"], "02_素材真值", "字体_字幕标题")
            if os.path.isdir(d):
                for fn in sorted(os.listdir(d)):
                    if fn.lower().endswith((".ttf", ".otf", ".ttc")):
                        burn_files[fn] = os.path.join(d, fn)
        try:
            r = subprocess.run(["fc-list", ":lang=zh", "family"],
                               capture_output=True, text=True, timeout=10)
            seen = set()
            for line in r.stdout.splitlines():
                for part in line.split(","):
                    p = part.strip()
                    # libass/fontconfig 认的是第一个族名，其余是别名
                    if p and not p.startswith(("Alibaba", "Noto Sans", "Source Han")) and p not in seen:
                        seen.add(p)
                        fams.append(p)
                        break
        except Exception:
            pass
        # 把烧录字体文件的族名查出来，标到列表上
        # fc-scan 一个文件常返回 4 个名字：家族英文名、家族中文名、具体英文名、具体中文名。
        # libass 必须拿**具体字重名**（"阿里巴巴普惠体 2.0 95 ExtraBold"），
        # 给家族名只会得到默认字重 —— 看着能跑，字形是错的。取最后一个。
        burn_fams = set()
        for fn, fp in burn_files.items():
            try:
                rr = subprocess.run(["fc-scan", "--format", "%{family}\n", fp],
                                    capture_output=True, text=True, timeout=8)
                names = [x.strip() for x in (rr.stdout or "").split(",") if x.strip()]
                if names:
                    burn_fams.add(names[-1])
            except Exception:
                pass
        items = [{"family": f, "burnable": f in burn_fams} for f in sorted(fams)]
        for f in sorted(burn_fams):
            if f not in fams:
                items.append({"family": f, "burnable": True})
        cur = ""
        root = ip_root("陈洁斌主号")
        if root and os.path.exists(os.path.join(root, "ip.json")):
            cur = (load_json(os.path.join(root, "ip.json")).get("subtitle") or {}).get("font", "")
        return self._json({
            "fonts": items, "current": cur,
            "burn_dir": (list(burn_files.values())[0].rsplit("/", 1)[0] if burn_files else ""),
            "burn_files": sorted(burn_files),
            "note": "标「可烧录」的才是真能进字幕的字体；其余只是浏览器预览，"
                    "要用于出片得先把字体文件放进精简目录（别一次塞几百个）。",
        })

    # ---------- 恢复默认 ----------
    def _api_defaults(self, get):
        """基线从哪来：git HEAD。不做"猜一个默认值"——猜出来的默认比没有更危险。"""
        scope = get("scope") or "global"
        info = {"scope": scope, "source": "git HEAD"}
        if scope == "global":
            info["file"] = "config/pipeline.json"
            rc, out, _ = run_git(["show", "HEAD:config/pipeline.json"], cwd=ROOT)
            info["available"] = rc == 0 and bool(out.strip())
        else:
            root = ip_root(get("path"))
            if not root:
                return self._bad("未知 IP")
            info["file"] = os.path.join(root, "ip.json")
            rel = os.path.relpath(os.path.join(root, "ip.json"), ARCHIVE)
            rc, out, _ = run_git(["show", "HEAD:" + rel], cwd=ARCHIVE)
            info["available"] = rc == 0 and bool(out.strip())
        info["hint"] = ("「恢复默认」= 回到上一次 git 提交的版本。"
                        "界面上试过的改动都会被撤掉，提交过的历史不动。")
        return self._json(info)

    def _api_reset(self, body):
        scope = body.get("scope") or "global"
        key = body.get("key") or ""
        confirm = bool(body.get("confirm"))
        if not key:
            return self._bad("缺少 key")
        table = GLOBAL_WRITABLE if scope == "global" else IP_WRITABLE
        meta = table.get(key)
        if not meta:
            return self._json({"error": f"该参数界面不可写：{key}"}, 403)
        if not confirm:
            return self._json({"error": "恢复默认会覆盖当前值，需要确认",
                               "need_confirm": True, "desc": meta["label"]}, 428)

        if scope == "global":
            cwd, rel, target = ROOT, "config/pipeline.json", os.path.join(ROOT, "config", "pipeline.json")
        else:
            root = ip_root(body.get("path"))
            if not root:
                return self._bad("未知 IP")
            cwd = ARCHIVE
            rel = os.path.relpath(os.path.join(root, "ip.json"), ARCHIVE)
            target = os.path.join(root, "ip.json")
        rc, out, err = run_git(["show", "HEAD:" + rel], cwd=cwd)
        if rc != 0 or not out.strip():
            return self._bad("取不到基线版本（这个文件还没进过 git？）" + (err.strip()[:120]))
        try:
            base = json.loads(out)
        except Exception as ex:
            return self._bad(f"基线不是合法 JSON：{ex}")
        node = base
        for p in key.split("."):
            if not isinstance(node, dict) or p not in node:
                return self._bad(f"基线里没有这个键：{key}")
            node = node[p]
        if isinstance(node, (dict, list)):
            return self._bad("这是个对象，恢复默认要逐个字段来")

        cur_data = load_json(target)
        cur_node = cur_data
        for p in key.split(".")[:-1]:
            cur_node = cur_node.get(p, {}) if isinstance(cur_node, dict) else {}
        old = cur_node.get(key.split(".")[-1]) if isinstance(cur_node, dict) else None
        if old == node:
            return self._json({"ok": True, "new": node, "unchanged": True,
                               "msg": "当前值和基线一样，不用改"})

        bak = target + ".bak." + datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(target, bak)
        parts = key.split(".")
        cur = cur_data
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = node
        write_json_atomic(target, cur_data)
        append_changelog(scope, None if scope == "global" else body.get("path"),
                         target, key, old, node, actor="ui-reset")
        return self._json({"ok": True, "old": old, "new": node})

    # ---------- 需求稿（和 AI 沟通的落点） ----------
    def _api_brief(self, get, body):
        """需求不是聊天记录，是文件：`05_文案/<套次>/brief.md`。
        聊天窗口一关就没了，而这条需求后面每个环节都要回头看。"""
        ip = (body or {}).get("ip") or get("ip")
        setname = (body or {}).get("set") or get("set")
        root = ip_root(ip)
        if not root or not setname:
            return self._bad("缺少 IP 或套次")
        base = os.path.join(root, "05_文案")
        cand = [x for x in (os.listdir(base) if os.path.isdir(base) else [])
                if x.startswith(setname)]
        folder = os.path.join(base, cand[0]) if cand else os.path.join(base, setname)
        path = os.path.join(folder, "brief.md")
        if body is not None and body.get("text") is not None:
            os.makedirs(folder, exist_ok=True)
            old = ""
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    old = f.read()
            if old.strip():
                bak = path + ".bak." + datetime.now().strftime("%Y%m%d_%H%M%S")
                shutil.copy2(path, bak)
            with open(path, "w", encoding="utf-8") as f:
                f.write(body["text"].rstrip() + "\n")
            return self._json({"ok": True, "path": path, "len": len(body["text"])})
        txt = ""
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                txt = f.read()[:20000]
        # 空文件不算「写过」：界面上写「已写」但点开是空白，比没有更让人火
        return self._json({"path": path, "text": txt,
                           "exists": bool(txt.strip()),
                           "empty_file": os.path.exists(path) and not txt.strip(),
                           "folder": folder})

    # ---------- 服务详情 ----------
    def _api_svc(self, get):
        """点徽章要能看到"它是什么、在哪、怎么起"——否则那个点就是个死的装饰。"""
        which = get("name") or ""
        detail = {
            "pipeline": {"label": "视频合成服务", "url": "http://127.0.0.1:8000",
                         "start": "bash bin/start-native.sh",
                         "role": "n8n / 外部系统调流水线的 HTTP 入口"},
            "n8n": {"label": "自动化编排", "url": "http://127.0.0.1:5678",
                    "start": "bash bin/start-native.sh --n8n",
                    "role": "可选。这边一条龙走 CLI，n8n 只在接外部系统时用"},
            "comfyui": {"label": "ComfyUI", "url": "http://127.0.0.1:8188",
                        "start": "（你自己终端里的 ComfyUI 启动脚本）",
                        "role": "真正吃 GPU 出视频的，出片前先看显存水位"},
            "zen-gate": {"label": "LLM 网关", "url": "http://127.0.0.1:8787",
                         "start": "systemctl --user start zen-gate（或 /usr/bin/zen-gate）",
                         "role": "免费车道聚合。★ 客户内容不许走这里——免费池提示词可能被上游记录"},
        }.get(which)
        if not detail:
            return self._bad("未知服务")
        info = dict(detail, name=which)
        if which == "zen-gate":
            try:
                import urllib.request as U
                with U.urlopen("http://127.0.0.1:8787/v1/models", timeout=3) as r:
                    data = json.loads(r.read().decode("utf-8"))
                ids = [m.get("id") for m in (data.get("data") or [])]
                info["models"] = len(ids)
                info["model_sample"] = ids[:12]
            except Exception as ex:
                info["models"] = 0
                info["err"] = str(ex)[:120]
        if which == "comfyui":
            try:
                import urllib.request as U
                with U.urlopen("http://127.0.0.1:8188/system_stats", timeout=3) as r:
                    st = json.loads(r.read().decode("utf-8"))
                dev = (st.get("devices") or [{}])[0]
                info["vram_free"] = round((dev.get("vram_free") or 0) / 1073741824, 1)
                info["vram_total"] = round((dev.get("vram_total") or 0) / 1073741824, 1)
            except Exception as ex:
                info["err"] = str(ex)[:120]
        info["up"] = self._services().get(which) == "up"
        return self._json(info)

    # ---------------- LLM 车道（人能选，且首选不排他） ----------------
    def _api_llm_channels(self):
        from llm_router import LLMRouter
        r = LLMRouter()
        rows = r.status()
        pre = r.prefs().get("prefer")
        for x in rows:
            # 免费池那条要在界面上说清代价，别让人以为只是「更快的一条」
            x["warn"] = ("提示词可能被上游记录" if x["pool"] == "免费池" else "")
        return self._json({
            "prefer": pre or None,
            "channels": rows,
            "usable": [x["name"] for x in rows if x["enabled"] and x["state"] == "可用"],
        })

    def _api_llm_prefer(self, body):
        from llm_router import LLMRouter
        name = (body.get("name") or "").strip() or None
        r = LLMRouter()
        if name and not any(p["name"] == name for p in r.cfg["providers"]):
            return self._bad(f"没有叫 {name} 的通道")
        d = r.set_prefer(name)
        append_changelog("global", None, os.path.join(ROOT, "config", ".llm_prefs.json"),
                         "llm.prefer", None, d.get("prefer"), actor="ui-llm")
        return self._json({"ok": True, "prefer": d.get("prefer"),
                           "note": "首选只是「优先」：这条挂了会自动换下一家，不会卡住。"})

    def _api_llm_probe(self, body):
        """只测一条通道，且**不写冷却** —— 诊断不该污染真实路由状态。"""
        name = (body.get("name") or "").strip()
        if not name:
            return self._bad("缺通道名")
        rc, out, err = run_cli_script(["llm_router.py", "probe", "--provider", name],
                                      timeout=45)
        txt = (out or "") + (err or "")
        ok = rc == 0 and "✓" in txt
        return self._json({"ok": ok, "channel": name,
                           "detail": txt.strip()[:300]}, 200 if ok else 200)

    # ---------------- AI：选题 ----------------
    def _api_topics(self, body):
        import studio_ai
        ip = body.get("ip") or "洁斌"
        n = max(1, min(int(body.get("n") or 5), 8))
        hint = body.get("hint") or ""
        prefer = body.get("prefer") or None
        try:
            r = studio_ai.suggest_topics(ip, n, hint, prefer)
        except RuntimeError as ex:
            # 没人设材料 → 这是**可解释的拒绝**，不是 bug，要给用户看得懂的话
            return self._json({"error": str(ex), "need_persona": True}, 409)
        return self._json(r)

    # ---------------- 阶段详情（点进度节点 → 内容 + 重跑命令） ----------------
    def _api_stage(self, get):
        ip, jid, stage = get("ip"), get("id"), get("stage")
        root = ip_root(ip)
        if not root or not jid or not stage:
            return self._bad("缺 ip / id / stage")
        rc, out, err = run_cli(["--json", "job-show", ip, jid])
        try:
            job = json.loads(out)
        except Exception:
            return self._json({"error": err.strip() or "job-show 失败"}, 404)
        setname = job.get("set") or jid
        arts = (job.get("artifacts") or {}).get(stage) or []
        st = (job.get("stages") or {}).get(stage) or {}
        span = STAGE_SPAN.get(stage)

        # 每条产物带上「能不能预览」，前端不必自己猜后缀
        items = []
        for p in arts:
            ext = os.path.splitext(p)[1].lower()
            kind = ("video" if ext in (".mp4", ".mov", ".webm") else
                    "audio" if ext in (".mp3", ".wav", ".m4a", ".aac", ".flac") else
                    "text" if ext in (".md", ".txt", ".json", ".srt", ".ass") else
                    "image" if ext in (".jpg", ".jpeg", ".png", ".webp") else "other")
            items.append({"path": p, "name": os.path.basename(p), "kind": kind,
                          "exists": os.path.exists(p),
                          "size": os.path.getsize(p) if os.path.exists(p) else 0,
                          "mtime": (datetime.fromtimestamp(os.path.getmtime(p))
                                    .strftime("%m-%d %H:%M") if os.path.exists(p) else "")})

        plans = []
        if span:
            f, t = span
            plans.append({
                "label": f"只重做「{stage}」这一段（{f} → {t}）",
                "why": "上游产物不动，最省时间；适合只改了本阶段的东西",
                "cmd": f"python3 bin/vstudio.py pipeline {ip} {setname} --from {f} --to {t}",
            })
            plans.append({
                "label": f"从「{stage}」一路做到最后（{f} → 结束）",
                "why": "上游不动、下游全部重算；本阶段改动会影响后面每一步时用",
                "cmd": f"python3 bin/vstudio.py pipeline {ip} {setname} --from {f}",
            })
        if stage == "交付":
            plans.append({
                "label": "先出正式档（推荐，若当前是试跑档）",
                "why": "视频段按 ip.json 的目标分辨率重生成，约 19-25 分钟。交付件要用这一版",
                "cmd": f"python3 bin/vstudio.py pipeline {ip} {setname} --from video",
            })
            plans.append({
                "label": "标记为已交付",
                "why": "产物齐了、验收也过了才点。这条改的是 job.json，不动视频",
                "cmd": f"python3 bin/vstudio.py job-set {ip} {jid} 交付 ok",
            })
        if stage == "文案":
            plans.append({
                "label": "重写文案（手工）",
                "why": "文案不在流水线里，得人写或让 AI 出草稿",
                "cmd": f"python3 bin/vstudio.py prompts {ip} {setname} --gen",
            })

        return self._json({
            "ip": ip, "id": jid, "set": setname, "stage": stage,
            "status": st.get("status"), "at": st.get("at"), "by": st.get("by"),
            "items": items, "plans": plans,
            "next": self._next_stage(job, stage),
            "precheck": (deliver_precheck(ip, root, job, items)
                         if stage == "交付" else None),
        })

    @staticmethod
    def _next_stage(job, stage):
        order = ["文案", "音频", "视频", "后期", "字幕", "验收", "交付"]
        try:
            i = order.index(stage)
        except ValueError:
            return None
        stages = job.get("stages") or {}
        for s in order[i + 1:]:
            if (stages.get(s) or {}).get("status") != "ok":
                return {"stage": s, "status": (stages.get(s) or {}).get("status")
                        or "pending"}
        return None

    # ---------------- AI：对话式建档 ----------------
    def _api_card_draft(self, body):
        import studio_ai
        text = (body.get("text") or "").strip()
        if not text:
            return self._bad("先说说这个客户是谁、要做什么")
        try:
            r = studio_ai.draft_card(text, body.get("prefer") or None)
        except Exception as ex:
            return self._json({"error": f"{type(ex).__name__}: {ex}"}, 502)
        # 顺带告一下会不会撞名，省得填完才被拒
        r["dup"] = bool(ip_root(r["draft"]["name"]))
        return self._json(r)

    def _api_card_create(self, body):
        """确认后的落盘：走 CLI 建档 + 记需求/红线 + 写人设 md。

        顺序有意为之：先 card-new（它会建目录树并登记总册），
        再 card-note 补业务内容，最后写 md。
        中途任一步失败就停在原地 —— 半截档案比没有更糟，宁可留个空壳让人看见。
        """
        d = body.get("draft") or {}
        md = body.get("persona_md") or {}
        name = (d.get("name") or "").strip()
        if not name:
            return self._bad("缺目录名")
        if ip_root(name):
            return self._json({"error": f"「{name}」已存在，换个名字或去改那张卡"}, 409)

        args = ["card-new", "--name", name, "--type", d.get("type") or "客户"]
        for flag, key in (("--tag", "tag"), ("--role", "role"),
                          ("--space", "space"), ("--aliases", "aliases")):
            v = d.get(key)
            if isinstance(v, list):
                v = ",".join(v)
            if v:
                args += [flag, str(v)]
        rc, out, err = run_cli(args, timeout=180)
        if rc != 0:
            return self._json({"error": "card-new 失败", "detail": (out + err).strip()[:600]},
                              500)

        notes = []
        for k, v in (d.get("needs") or {}).items():
            txt = v if isinstance(v, str) else "、".join(map(str, v))
            if txt.strip():
                r2, o2, e2 = run_cli(["card-note", name, txt, "--kind", "need",
                                      "--key", str(k)], timeout=60)
                notes.append({"kind": "need", "key": k, "ok": r2 == 0})
        for kind, bucket in (("redline", "redlines"), ("habit", "habits")):
            for t in (d.get(bucket) or []):
                if str(t).strip():
                    r2, o2, e2 = run_cli(["card-note", name, str(t), "--kind", kind],
                                         timeout=60)
                    notes.append({"kind": kind, "text": str(t), "ok": r2 == 0})

        root = ip_root(name)
        written = []
        if root and md:
            pdir = os.path.join(root, "01_人设与规范")
            os.makedirs(pdir, exist_ok=True)
            for fn, text in md.items():
                if not str(text).strip():
                    continue
                fp = os.path.join(pdir, os.path.basename(fn))
                with open(fp, "w", encoding="utf-8") as f:
                    f.write(str(text).rstrip() + "\n")
                written.append(fp)

        append_changelog("ip", name, os.path.join(root or "", "card.json"),
                         "card.new", None, name, actor="ui-card")
        return self._json({"ok": True, "name": name, "root": root,
                           "notes": notes, "md": written,
                           "next": [f"把音色参考 / 起始静帧 / 形象底图 填进 {root}/ip.json",
                                    f"python3 bin/vstudio.py guard {name}"]})

    # ---------------- MCP 自检 ----------------
    def _api_mcp(self):
        """真跑一次 MCP 握手 —— 不靠「文件存在」判断能不能对接。"""
        venv = os.path.join(ROOT, "mcp-servers", "ip-studio", ".venv", "bin", "python")
        srv = os.path.join(BIN_DIR, "mcp_server.py")
        if not os.path.exists(srv):
            return self._json({"ok": False, "error": "没有 bin/mcp_server.py"})
        rc, out, err = run_cli_script(["mcp_probe.py"], timeout=90,
                                      python=venv if os.path.exists(venv) else None)
        info = {}
        for line in reversed((out or "").strip().splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    info = json.loads(line)
                    break
                except Exception:
                    continue
        py = venv if os.path.exists(venv) else PY
        return self._json({
            "ok": bool(info.get("tools")) and bool(info.get("probe_ok")),
            "tools": info.get("tools") or [],
            "server": info.get("server") or {},
            "probe": info.get("probe_call"),
            "cmd": f"{py} {srv}",
            "register": f"claude mcp add ip-studio -- {py} {srv}",
            "detail": (info.get("error") or (out or err or "").strip()[:400]),
        })

    def _api_set(self, body):
        """改一处必须有讲究：只允许白名单键、类型校验、第二层要 confirm、改动前后都记 changelog"""
        scope = body.get("scope")
        key = body.get("key")
        path = body.get("path")
        value = body.get("value")
        confirm = bool(body.get("confirm"))
        table = GLOBAL_WRITABLE if scope == "global" else IP_WRITABLE
        meta = (table or {}).get(key or "")
        if not meta:
            return self._json({"error": f"该参数界面不可写：{key}"}, 403)
        if meta["tier"] == 2 and not confirm:
            return self._json({"error": "第二层参数需二次确认", "need_confirm": True}, 428)

        # 类型/范围校验
        try:
            t = meta["type"]
            if t == "int":
                value = int(value)
            elif t == "float":
                value = float(value)
            elif t == "bool":
                value = bool(value) if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes")
            elif t in ("str", "text"):
                value = str(value)
                if meta.get("maxlen") and len(value) > meta["maxlen"]:
                    return self._bad(f"超过 {meta['maxlen']} 字")
            if "min" in meta and value < meta["min"]:
                return self._bad(f"不能小于 {meta['min']}")
            if "max" in meta and value > meta["max"]:
                return self._bad(f"不能大于 {meta['max']}")
            if meta.get("enum") and value not in meta["enum"]:
                return self._bad(f"只能是：{'/'.join(meta['enum'])}")
        except Exception as ex:
            return self._bad(f"取值不合法：{ex}")

        if scope == "global":
            target = os.path.join(ROOT, "config", "pipeline.json")
        else:
            root = ip_root(path)
            if not root:
                return self._bad("未知 IP")
            target = os.path.join(root, "ip.json")
        if not os.path.exists(target):
            return self._bad("真值文件不存在")

        # 备份（用户规矩：从不用破坏性删除，这里也留后路）
        bak = target + ".bak." + datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(target, bak)

        data = load_json(target)
        parts = key.split(".")
        cur = data
        for p in parts[:-1]:
            if p not in cur or not isinstance(cur[p], dict):
                cur[p] = {}
            cur = cur[p]
        old = cur.get(parts[-1])
        cur[parts[-1]] = value
        write_json_atomic(target, data)
        log = append_changelog(scope, None if scope == "global" else path,
                               target, key, old, value)
        return self._json({"ok": True, "file": target, "key": key, "old": old,
                           "new": value, "backup": bak, "changelog": log})


def die_safe(msg):
    print("✗ " + msg, file=sys.stderr)
    sys.exit(1)


def write_portfile(url, port):
    """把真实端口和 PID 落到固定小纸条 —— 端口会自动顺延，
    光看启动命令猜不出地址，第二次开时常一堆旧实例堆在那儿。"""
    try:
        info = {"pid": os.getpid(), "port": port, "url": url,
                "started": now(), "root": ROOT}
        with open(PORTFILE, "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False, indent=2)
        atexit.register(lambda: os.path.exists(PORTFILE) and os.remove(PORTFILE))
    except Exception:
        pass


def read_portfile():
    if not os.path.exists(PORTFILE):
        return None
    try:
        with open(PORTFILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def pid_is_ours(pid):
    """★ 杀之前先验明正身：PID 是会复用的，纸条上那个号可能早就是别的进程了。
    不做这一步，--stop 会变成隔了半天的误杀。"""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmd = f.read().decode("utf-8", "replace")
    except Exception:
        return None          # 读不到 = 进程不在了
    return "ui_server.py" in cmd


def stop_running():
    info = read_portfile()
    if not info:
        print("没有发现正在运行的实例（小纸条不在：" + PORTFILE + "）")
        return 1
    pid = info.get("pid")
    who = pid_is_ours(pid)
    if who is None:
        if os.path.exists(PORTFILE):
            try:
                os.remove(PORTFILE)
            except OSError:
                pass
        print(f"进程 {pid} 已经不在了，小纸条清掉")
        return 0
    if not who:
        print(f"✗ pid={pid} 已经不是 UI 服务了（系统把号复用给了别的进程），不敢动手。")
        print(f"  自己查一下：ps -p {pid} -o pid,cmd")
        return 1
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        print(f"进程 {pid} 早就不在了，清掉小纸条")
    except PermissionError:
        print(f"没权限停 {pid}，你自己 kill 一下")
        return 1
    for _ in range(30):
        time.sleep(0.1)
        try:
            os.kill(pid, 0)
        except OSError:
            break
    print(f"✓ 已停 pid={pid} port={info.get('port')}（起于 {info.get('started')}）")
    # 小纸条原本靠进程的 atexit 收尾，但它要是被 SIGKILL 就来不及写 ——
    # 残留的纸条会让下一次 --status 说谎，这里主动清掉。
    if os.path.exists(PORTFILE):
        try:
            os.remove(PORTFILE)
        except OSError:
            pass
    return 0


def main():
    ap = argparse.ArgumentParser(description="洁斌视频工作台 · 本地网页 UI")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--open", action="store_true", help="起来后自动开浏览器")
    ap.add_argument("--stop", action="store_true", help="停掉上一次起的那一个")
    ap.add_argument("--status", action="store_true", help="看当前有没有在跑、地址是多少")
    a = ap.parse_args()

    if a.status:
        info = read_portfile()
        if not info:
            print("没在跑。启动：python3 bin/ui_server.py")
            return 0
        alive = pid_is_ours(info["pid"])          # 顺便确认那个号确实是我们的
        if alive is None:
            print(f"没在跑（小纸条是上次没收尾的残留：{info['url']}）。"
                  f"启动：python3 bin/ui_server.py")
        elif alive:
            print(f"运行中：{info['url']}  pid={info['pid']}  起于 {info['started']}")
        else:
            print(f"⚠ pid={info['pid']} 已经被系统复用给别的进程了，工作台其实没在跑"
                  f"（小纸条记的是 {info['url']}）。启动：python3 bin/ui_server.py")
        return 0
    if a.stop:
        sys.exit(stop_running())

    old = read_portfile()
    if old:
        try:
            os.kill(old["pid"], 0)
            print(f"⚠ 已经有一个在跑了：{old['url']} (pid={old['pid']}，起于 {old['started']})")
            print(f"  要到 {old['port'] + 1} 之后的端口？加 --port；要换掉它？先 --stop")
        except OSError:
            pass

    os.makedirs(CACHE_DIR, exist_ok=True)
    nk = load_llm_env()          # 子进程（探活/AI）要靠它拿到 key
    if nk:
        print(f"  已载入 {nk} 个 LLM 环境变量（config/llm.env）")
    os.makedirs(THUMB_DIR, exist_ok=True)
    mimetypes.init()

    # 端口被占就往后顺延（本机常驻服务多，8790 撞车是常态，不该让人去查 PID）
    srv, port = None, a.port
    for _ in range(30):
        try:
            srv = ThreadingHTTPServer((a.host, port), Handler)
            break
        except OSError as ex:
            if ex.errno not in (98, 48):  # EADDRINUSE / macOS EADDRINUSE
                raise
            port += 1
    if not srv:
        die_safe(f"连续 30 个端口都被占用，换 --port 试试")

    url = f"http://{a.host}:{port}"
    write_portfile(url, port)
    # SIGTERM 默认直接终止进程、不走 atexit → 小纸条会残留成"假在跑"。
    # 转成 SystemExit，让它在退栈时把纸条收掉。
    try:
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    except Exception:
        pass
    print(f"✓ UI 服务已启动 {url}" + (f"   （{a.port} 被占，已顺延）" if port != a.port else ""))
    print(f"  引擎根 {ROOT}")
    print(f"  档案区 {ARCHIVE}")
    print(f"  小纸条 {PORTFILE}   ← 记着 pid 和真实端口，查地址/停服都看它")
    print("  Ctrl+C 停，或另开一个终端跑：python3 bin/ui_server.py --stop")
    if a.open:
        try:
            subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
        srv.server_close()


if __name__ == "__main__":
    main()
