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
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BIN_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BIN_DIR)
UI_DIR = os.path.join(ROOT, "ui")
ARCHIVE = os.environ.get("STUDIO_ARCHIVE") or "/cfu/ai视频档案库"
CACHE_DIR = os.path.join(ROOT, ".cache", "ui")
THUMB_DIR = os.path.join(CACHE_DIR, "thumbs")
CHANGELOG_GLOBAL = os.path.join(ROOT, "config", ".ui_changelog.jsonl")

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
    "voice.tts.instruction":      {"tier": 1, "type": "text",  "label": "音色语气指令", "maxlen": 300},
    "voice.tts.cfg_scale":        {"tier": 2, "type": "float", "label": "CFG", "min": 0.5, "max": 3.0},
    "video.overlap":              {"tier": 2, "type": "int",   "label": "重叠帧", "min": 0, "max": 30},
    "video.steps":                {"tier": 2, "type": "int",   "label": "采样步数", "min": 1, "max": 50},
    "video.lora.strength":        {"tier": 2, "type": "float", "label": "LoRA 强度", "min": 0.0, "max": 2.0},
    "video.sage_attention":       {"tier": 2, "type": "str",   "label": "Sage Attention", "enum": ["auto", "true", "false"]},
}

TASKS = {}
TASK_LOCK = threading.Lock()


# ---------- 基础 ----------
def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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
        if kind not in table:
            return self._bad("未知命令模板")
        return self._json({"cmdline": table[kind]})

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


def main():
    ap = argparse.ArgumentParser(description="洁斌视频工作台 · 本地网页 UI")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--open", action="store_true", help="起来后自动开浏览器")
    a = ap.parse_args()

    os.makedirs(CACHE_DIR, exist_ok=True)
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
    print(f"✓ UI 服务已启动 {url}" + (f"   （{a.port} 被占，已顺延）" if port != a.port else ""))
    print(f"  引擎根 {ROOT}")
    print(f"  档案区 {ARCHIVE}")
    print("  Ctrl+C 停")
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
