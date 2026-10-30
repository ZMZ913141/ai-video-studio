#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vstudio —— 洁斌资产 · 视频生产总控 CLI

★ 这是整个生产体系的**唯一入口**。UI 是可选项，本 CLI 才是真接口：
  人可以在终端敲，agent 可以用 Bash 调（加 --json 拿结构化结果），
  也可以挂成 MCP 给别的 AI 调（见同目录 mcp_server.py）。

管辖范围：陈洁斌主号 + 其客户 IP（与 IP总档.json 一致）。雅太社（珠珠）不纳管。

常用：
  vstudio ips                          看有几个 IP、素材齐不齐
  vstudio jobs 洁斌                     看所有套次的进度
  vstudio job-new 洁斌 S05 讲AI变现      开一套新的
  vstudio job-show 洁斌 S04             看一套的详情
  vstudio job-set 洁斌 S05 文案 ok       推进阶段
  vstudio guard 洁斌                     开工前自检（缺素材直接非零退出）
  vstudio llm "写一段30秒口播"           走 LLM 路由
  vstudio scripts 洁斌                   列出该 IP 能跑的生产脚本

※ 全球化名：ip 参数写名字/别名/路径都行（"洁斌"、"主号"、"陈洁斌主号"、"S04"）。
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime

BIN_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BIN_DIR)                      # 项目根（引擎代码，进 git）
REGISTRY = os.path.join(ROOT, "registry.json")       # 总册：谁有档案卡
TEMPLATE = os.path.join(ROOT, "templates", "card.template.json")
INDEX_FILE = os.path.join(ROOT, "jobs.index.json")

# ★ 档案存储区 —— 与引擎分家，2026-10-28 定
#   结构：/cfu/ai视频档案库/<IP名>/{card.json, ip.json, 01~08/, 资产/}
#   为什么档案不放项目里：一套片子 500M+，项目目录会随出片数量线性膨胀，
#   而且素材/成片是二进制，放 git 仓库边上迟早被误提交。
#   现在项目只留引擎（代码/文档/模板），档案全在生产盘，一人一个目录互不干扰。
#   换盘/换位置：export STUDIO_ARCHIVE=/别的/路径（不设则用下面的默认值）
PROFILES = os.environ.get("STUDIO_ARCHIVE") or "/cfu/ai视频档案库"

# 生产阶段（顺序即流程，对齐 README 的开工顺序）
STAGES = ["文案", "音频", "视频", "后期", "字幕", "验收", "交付"]

# 每个阶段对应的产物识别规则（在套次同名目录里按后缀/关键词找）
# ★ 这些后缀只决定「进度条认不认这一步做完了」，不影响出片本身。
#   所以宁可放宽一点：录成 m4a / 导出成 flac 的音频也是「音频这一步做完了」，
#   以前只认 mp3/wav，用户放个 m4a 进去会显示「音频：缺」，纯属自己吓自己。
ARTIFACT_RULES = {
    "文案": [("文案", [".md", ".txt"]), ("字幕文本", [".txt"])],
    "音频": [("音频_定板", [".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"])],
    "视频": [("成片", [".mp4"])],
    "后期": [("字幕版", [".mp4"])],
    "字幕": [("", [".srt", ".ass"])],
    "验收": [("制作记录", [".md"])],
}


# ---------- 基础 ----------
def die(msg, code=1, quiet=False):
    if not quiet:
        print("✗ " + msg, file=sys.stderr)
    sys.exit(code)


def load_registry():
    if not os.path.exists(REGISTRY):
        die(f"找不到总册 {REGISTRY}")
    with open(REGISTRY, encoding="utf-8") as f:
        return json.load(f)


def all_entries(reg):
    """返回 [(entry, kind)]，kind ∈ ips / excluded"""
    out = [(e, "ips") for e in reg.get("ips", [])]
    out += [(e, "excluded") for e in reg.get("excluded", [])]
    return out


def resolve_ip(name, quiet=False):
    """按名字/别名/路径解析到一个本体系 IP。命中 excluded 要明确报错。"""
    reg = load_registry()
    for e, kind in all_entries(reg):
        if kind != "ips":
            cands = [e.get("name", "")] + e.get("aliases", [])
            if name in cands or name == e.get("path"):
                die(f"「{name}」属于独立体系（{e.get('reason','不纳管')}），本 CLI 不碰。", quiet=quiet)
            continue
        cands = [e.get("name", "")] + e.get("aliases", [])
        if name in cands or os.path.abspath(name) == os.path.abspath(e.get("path", "")):
            return e
    for e, kind in all_entries(reg):
        if kind == "ips" and name.lower() in e.get("name", "").lower():
            return e
    die(f"总册里没有「{name}」。现有 IP：{', '.join(e.get('name','?') for e in reg.get('ips',[]))}", quiet=quiet)


def opus_dirs(ip_root):
    """读 ip.json 的 01~08 目录名，读不到就用默认"""
    default = {"01": "01_人设与规范", "02": "02_素材真值", "03": "03_生产脚本",
               "04": "04_验收工具", "05": "05_文案", "06": "06_音频",
               "07": "07_成片", "08": "08_过程"}
    p = os.path.join(ip_root, "ip.json")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            return d.get("opus", {}).get("dirs", default)
        except Exception:
            pass
    return default


# ---------- 套次 ----------
def list_sets(ip_root):
    """扫 05_文案 下的 Sxx_名字 目录，返回套次名列表（按编号排序）"""
    d = opus_dirs(ip_root)
    base = os.path.join(ip_root, d.get("05", "05_文案"))
    if not os.path.isdir(base):
        return []
    out = []
    for n in sorted(os.listdir(base)):
        if os.path.isdir(os.path.join(base, n)):
            out.append(n)
    out.sort(key=lambda s: (re.match(r"^([A-Za-z]*)(\d+)", s).group(0) if re.match(r"^([A-Za-z]*)(\d+)", s) else s))
    return out


def script_text_path(ip_root, setname):
    """一套片子的**稿子文件**路径（05_文案/<套次目录>/<套次号>_字幕文本.txt）。

    ★ 这里必须是唯一真源。
    2026-10-29 实测踩过的坑：写稿的（ui_server / studio_ai）曾用
    `<目录全名>_字幕文本.txt`（M01_老王的第一条…_字幕文本.txt），
    而读稿的（batch.py / gen_tts.py）按 `<套次号>_字幕文本.txt` 找。
    两边各写各的 → 批量出片找不到稿子 → 一片空白，而且不报错。
    所以：**目录名带标题给人看，文件名只带套次号给脚本看**。
    """
    base = os.path.join(ip_root, "05_文案")
    cand = [x for x in (os.listdir(base) if os.path.isdir(base) else [])
            if x.startswith(setname)]
    folder = os.path.join(base, cand[0]) if cand else os.path.join(base, setname)
    os.makedirs(folder, exist_ok=True)
    name = setname.split("_")[0] + "_字幕文本.txt"
    return os.path.join(folder, name)


def set_dirs(ip_root, setname):
    """一套片子横跨的目录（05~08 同名）"""
    d = opus_dirs(ip_root)
    return {k: os.path.join(ip_root, d[k], setname) for k in ("05", "06", "07", "08")}


# ---------------------------------------------------------------- 音频时长探测
# ★ 段数必须由「喂给出片的那条音频」算，不能由 SRT 末条 / 字数估来算。
#   2026-10-30 的实测：同一套片，按字数估 20.3s → 498 帧 → 4 段；
#   出片脚本读真实留白音频 20.76s → 499 帧 → 吸附到 515 → 5 段。
#   于是提示词只写了 4 段，第 5 段静默退回默认描述（画面与台词不符），
#   而日志里除了那条"段数对不上"的告警，没有任何一步会失败。
#   差的那 0.46s 就是片尾静音 —— 光看 SRT 永远看不到。
#   ★ 探测必须用**出片脚本同一支 ffmpeg**（gen_video.py 里 FF 就是这个 imageio 自带版）。
#   同一份 mp3，系统 ffmpeg 报 20.73s、这支报 20.76s —— 差 0.03s，
#   但 20.73×24+1=498（正好落在 5+17n 网格上，4 段），
#   20.76×24+1=499（越界，吸附到 515，5 段）。差一个二进制就差了整整一段。
_FF_CANDIDATES = (
    "/home/qmr/.workbuddy/binaries/python/envs/default/lib/python3.13/"
    "site-packages/imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v7.0.2",
    "ffmpeg",
)


def _ffmpeg():
    import shutil
    for c in _FF_CANDIDATES:
        if os.path.exists(c):
            return c
        if c == "ffmpeg":
            p = shutil.which("ffmpeg")
            if p:
                return p
    return None


def probe_sec(path):
    """真实时长（秒）。本机 ffprobe 是坏的，只能解析 ffmpeg -i 的 Duration 行。"""
    ff = _ffmpeg()
    if not ff or not path or not os.path.isfile(path):
        return None
    try:
        r = subprocess.run([ff, "-nostdin", "-i", path], capture_output=True, text=True, timeout=30)
    except Exception:
        return None
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)",
                  (r.stderr or "") + (r.stdout or ""))
    if not m:
        return None
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def audio_sec_of(dir06):
    """这一步真正喂给出片的音频时长（**留白版优先**，那是出片读的那条）。

    返回 (秒, 路径)；找不到/探不到就 (None, None)，调用方退回估算。
    """
    for pat in ("*留白*.mp3", "*留白*.wav", "*留白*.m4a", "*.mp3", "*.wav"):
        fs = sorted(glob.glob(os.path.join(dir06, pat)),
                    key=os.path.getmtime, reverse=True)
        for f in fs:
            sec = probe_sec(f)
            if sec:
                return sec, f
    return None, None


def landing_path(ip_root, setname):
    """一套片子**自己的落点**文件（05_文案/<套次目录>/landing.md）。

    ★ 为什么落点要分「IP 级模板」和「套次级成品」两层
    ----------------------------------------------------
    2026-10-29 实测踩到的坑：老王的落点只存在 card.json 里一条 ——
      「到店说一句「刷到你们家视频」，这盘雪花我请你尝。你先吃，合口再来。」
    这条落点里有**具体货品**（这盘雪花 / 你先吃）。
    于是 M01（讲牛骨汤）那条片子的落点应该是「这锅汤…你先喝」，
    但卡里存的是「这盘雪花…你先吃」—— 一套 IP 共用一条落点，
    写 M01 时把 M02 的落点原样塞进去，观众听到的是「这盘雪花」，
    前后完全对不上。

    正确结构（参考珠珠那套人设圣经的「结尾固定动作」）：
      card.json 的 landing_template = **固定骨架**（那句 CTA 的样子）
      05_文案/<套次>/landing.md       = **这一套的具体落点**（骨架 + 本套的货品词）
    写稿时：优先用套次自己的 landing.md；没有才用卡里的模板。
    """
    base = os.path.join(ip_root, "05_文案")
    cand = [x for x in (os.listdir(base) if os.path.isdir(base) else [])
            if x.startswith(setname)]
    folder = os.path.join(base, cand[0]) if cand else os.path.join(base, setname)
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "landing.md")


def resolve_landing(ip_root, setname=""):
    """一套片子该用的落点：套次自己的 > 卡里的模板 > 空。

    返回 (落点文本, 来源说明)。来源说明会显示在界面上，
    让人一眼看出「这句是这套自己的」还是「这是卡的模板，还没为本套定制」。
    """
    card = load_card(ip_root) or {}
    tpl = str(card.get("landing") or card.get("landing_template") or "").strip()
    if setname:
        p = landing_path(ip_root, setname)
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    t = f.read().strip()
                if t:
                    return t, f"套次自己的落点（{os.path.basename(p)}）"
            except Exception:
                pass
    if tpl:
        return tpl, "档案卡里的落点模板（还没为本套定制，货品词可能是别的片子的）"
    return "", ""


# ---------- 文案去重（唯一真源） ----------
def script_norm(s):
    """归一化：只留文字，去掉所有空白和标点。

    ★ 为什么不能直接用 `line == landing` 比：
      落点是一整句，但模型常把它拆成两三行写进正文（实测 2026-10-29
      老王 M02：card.json 里落点是一行「到店说一句…这盘雪花我请你尝。你先吃，合口再来。」，
      模型在正文里拆成了两行）。整行相等永远匹配不上 → 一行都没删 →
      最后又把整块落点追加一次 → 观众连听两遍。
      归一化后按「包含」判定，拆行与否都能认出来。
    """
    return re.sub(r"""[\s，。！？、；：""''（）()「」『』【】…—·,.!?;:'"[\]]+""",
                  "", str(s or ""))


def dedupe_script(lines, landing=""):
    """把稿子里的重复去掉，落点只留一份且必须在最后。

    返回 (lines, 说明)。三道防线：
      ① 落点去重：任何一行只要（归一化后）包含落点，或落点包含这一行，都算落点，
         只保留最后一份。
      ② 尾部通用重复：最后 N 行与前面某段完全重复 → 删掉后面那份。
         （不针对落点，是给「模型把任意一段写两遍」兜底的。）
      ③ 落点补齐：真的一处都没有 → 在末尾补上。

    ★ 这是护栏，不是装饰。同一段话在 TTS 里念两遍，观众直接划走；
      而这事**不报错、不崩，片子照出**，只能靠文本层拦。
    """
    notes = []
    L = [str(x).strip() for x in (lines or []) if str(x).strip()]
    key = script_norm(landing)

    # ---- ① 落点去重 ----
    if key:
        kept, dropped = [], 0
        for ln in L:
            n = script_norm(ln)
            if n and (n in key or key in n):
                dropped += 1
                continue
            kept.append(ln)
        if dropped:
            notes.append(f"删掉重复的落点 {dropped} 处")
        # 最后补一份完整落点
        if not kept or script_norm(kept[-1]) != key:
            kept.append(str(landing).strip())
            notes.append("末尾补上落点")
        L = kept

    # ---- ② 尾部通用重复 ----
    # 判据：末尾连续 k 行（归一化后）等于前面某处连续 k 行 → 后面那份删掉。
    # 从长往短试，先删大段重复。
    changed = True
    while changed and len(L) >= 4:
        changed = False
        for k in range(len(L) // 2, 1, -1):
            tail = "".join(script_norm(x) for x in L[-k:])
            if not tail:
                continue
            for i in range(0, len(L) - k):
                if "".join(script_norm(x) for x in L[i:i + k]) == tail:
                    L = L[:-k]
                    notes.append(f"删掉尾部重复 {k} 行")
                    changed = True
                    break
            if changed:
                break

    # ---- ③ 落点必须真在最后 ----
    if key and L and script_norm(L[-1]) != key:
        L.append(str(landing).strip())
        notes.append("落点不在末尾，挪到最后")

    return L, notes


def dedupe_script_text(text, landing=""):
    """对一整篇稿子文本去重（按行拆 → 去重 → 重拼）。

    ★ 这是**落盘前最后一道闸**。生成端（studio_ai）已经去重过，
      但稿子还可能被手工改过、或从别处粘进来 —— 只要重复还在，
      TTS 就会照着念两遍。宁可在这里拦下来报个错，也别出一片口播事故。
    """
    raw = str(text or "")
    lines = [x.strip() for x in raw.splitlines() if x.strip()]
    out, notes = dedupe_script(lines, landing)
    return "\n".join(out), notes


def job_path(ip_root, setname):
    return os.path.join(set_dirs(ip_root, setname)["05"], "job.json")


def asset_dir(ip_root):
    """交付目录：每个 IP 自己的 资产/（不共享，避免客户之间互相看见）"""
    return os.path.join(ip_root, "资产")


def delivered_files(ip_root, setname):
    """交付判据：该 IP 的资产目录里有这一套的成片（不是靠猜，是看文件在不在）"""
    d = asset_dir(ip_root)
    if not os.path.isdir(d):
        return []
    m = re.match(r"^([A-Za-z]+\d+)", setname)
    jid = m.group(1) if m else setname
    return [os.path.join(d, fn) for fn in sorted(os.listdir(d))
            if fn.startswith(jid) and fn.lower().endswith((".mp4", ".srt", ".mp3"))]


def scan_artifacts(ip_root, setname):
    """按真实文件推导各阶段产物（不做任何猜测，文件不在就是 None）"""
    dirs = set_dirs(ip_root, setname)
    found = {}
    for stage, rules in ARTIFACT_RULES.items():
        hits = []
        for key, exts in rules:
            for k in ("05", "06", "07", "08"):
                bd = dirs[k]
                if not os.path.isdir(bd):
                    continue
                for fn in sorted(os.listdir(bd)):
                    if not fn.lower().endswith(tuple(exts)):
                        continue
                    if key and key not in fn:
                        continue
                    hits.append(os.path.join(bd, fn))
        if hits:
            found[stage] = hits
    return found


def derive_stage(job):
    """当前该干哪一步：产物和显式标记里，最后一个已完成的阶段的下一步。
    「交付」不算步骤（它是资产目录里有成片这个事实）。"""
    arts = job.get("artifacts", {})
    stages = job.get("stages", {})
    last_i = -1
    for i, s in enumerate(STAGES):
        if s == "交付":
            continue
        done = bool(arts.get(s)) or stages.get(s, {}).get("status") == "ok"
        if done:
            last_i = i
    nxt = last_i + 1
    while nxt < len(STAGES) and STAGES[nxt] == "交付":
        nxt += 1
    return STAGES[nxt] if nxt < len(STAGES) else "交付"


def load_job(ip_root, setname):
    """读一套片子。

    ★ 文件系统是真相来源，job.json 只存「人工标记」（stages/qa）。
    所以每次读都用当前目录刷新 artifacts —— 否则 job.json 是建套那一刻的
    快照，之后生成的音频/成片/字幕全都不算数，列表会一直显示"缺"。
    （S05 就是这样：文件早就齐了，列表还显示缺文案。）
    """
    p = job_path(ip_root, setname)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        job = json.load(f)
    job["artifacts"] = scan_artifacts(ip_root, setname)
    dl = delivered_files(ip_root, setname)
    job["delivered"] = bool(dl)
    if dl:
        job["artifacts"]["交付"] = dl
        job["stages"].setdefault("交付", {})["status"] = "ok"
    job["stage"] = derive_stage(job)
    return job


def build_job(ip_entry, setname, write=False):
    """从真实目录推导一套片子的 job（write=True 时落盘）"""
    ip_root = ip_entry["path"]
    m = re.match(r"^([A-Za-z]+\d+)_?(.*)$", setname)
    jid = m.group(1) if m else setname
    title = m.group(2) if m and m.group(2) else setname
    arts = scan_artifacts(ip_root, setname)
    job = {
        "_schema": "video-job/1.0",
        "ip": ip_entry["name"],
        "id": jid,
        "title": title,
        "set": setname,
        "created": datetime.fromtimestamp(
            os.path.getmtime(set_dirs(ip_root, setname)["05"])).strftime("%Y-%m-%d")
        if os.path.isdir(set_dirs(ip_root, setname)["05"]) else datetime.now().strftime("%Y-%m-%d"),
        "stage": None,
        "artifacts": arts,
        "stages": {},
        "qa": {},
        "delivered": False,
    }
    for s in STAGES:
        job["stages"][s] = {"status": "ok" if arts.get(s) else "pending", "at": None}
    job["stage"] = derive_stage(job)
    # 交付是「资产目录里有成片」，不是某个阶段产物
    dl = delivered_files(ip_root, setname)
    if dl:
        job["delivered"] = True
        job["artifacts"]["交付"] = dl
        job["stages"]["交付"] = {"status": "ok", "at": None}
        job["stage"] = "交付"
    if write:
        p = job_path(ip_root, setname)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(job, f, ensure_ascii=False, indent=2)
    return job


def brief(job):
    # ★ 判据必须和 derive_stage 一致：文件在，或人工标记过 ok，都算完成。
    #   曾只按文件算，导致 job-set 推进后列表仍显示"缺"，看着像没生效。
    arts = job.get("artifacts", {})
    stages = job.get("stages", {})
    def is_done(s):
        return bool(arts.get(s)) or stages.get(s, {}).get("status") == "ok"
    done = [s for s in STAGES if is_done(s)]
    missing = [s for s in STAGES if not is_done(s) and s != "交付"]
    if not job.get("delivered", False):
        missing.append("交付")
    return {
        "id": job["id"], "title": job["title"], "stage": job["stage"],
        "done": done, "missing": missing,
        "delivered": job.get("delivered", False),
    }


# ---------- 命令 ----------
def cmd_ips(a):
    reg = load_registry()
    out = []
    for e in reg.get("ips", []):
        root = e.get("path", "")
        ok = os.path.isdir(root) and os.path.exists(os.path.join(root, "ip.json"))
        n_jobs = len(list_sets(root)) if os.path.isdir(root) else 0
        out.append({"name": e.get("name"), "tag": e.get("tag"), "path": root,
                    "aliases": e.get("aliases", []), "ip_json": ok, "sets": n_jobs})
    if a.json:
        print(json.dumps({"ips": out,
                          "excluded": [x.get("name") for x in reg.get("excluded", [])]},
                         ensure_ascii=False, indent=2))
        return
    print("本体系 IP：")
    for o in out:
        flag = "✓" if o["ip_json"] else "✗ 缺 ip.json"
        print(f"  {o['name']}  [{o['tag']}]  {o['sets']} 套  {flag}")
        print(f"     {o['path']}   别名：{'/'.join(o['aliases'])}")
    ex = reg.get("excluded", [])
    if ex:
        print("\n不纳管（独立体系）：")
        for x in ex:
            print(f"  {x.get('name')}  {x.get('path','')}")


def cmd_jobs(a):
    e = resolve_ip(a.ip)
    root = e["path"]
    sets = list_sets(root)
    if a.only:
        sets = [s for s in sets if a.only.lower() in s.lower()]
    rows = []
    for s in sets:
        j = load_job(root, s) or build_job(e, s)
        rows.append(brief(j))
    if a.json:
        print(json.dumps({"ip": e["name"], "jobs": rows}, ensure_ascii=False, indent=2))
        return
    print(f"{e['name']} · 共 {len(rows)} 套\n")
    for r in rows:
        mark = "✓已交付" if r["delivered"] else f"→{r['stage']}"
        print(f"  {r['id']:<5} {r['title'][:24]:<26} {mark}")
        if r["missing"]:
            print(f"        缺：{' '.join(r['missing'])}")


def cmd_job_new(a):
    e = resolve_ip(a.ip)
    root = e["path"]
    tag = e.get("tag", "S")
    if not a.set:
        die("需要套次名，如 S05 或 S05_讲AI变现")
    s = a.set
    if not re.match(r"^[A-Za-z]+\d+", s):
        s = f"{tag}{s}"
    if "_" not in s and a.title:
        s = f"{s}_{a.title}"
    dirs = set_dirs(root, s)
    for k, p in dirs.items():
        os.makedirs(p, exist_ok=True)
    job = build_job(e, s, write=True)
    print(f"✓ 建套 {s}")
    for k in ("05", "06", "07", "08"):
        print(f"   {dirs[k]}")
    print(f"   job.json → {job_path(root, s)}")
    print(f"   当前阶段：{job['stage']}")
    if a.json:
        print(json.dumps(job, ensure_ascii=False, indent=2))


def cmd_job_show(a):
    e = resolve_ip(a.ip)
    root = e["path"]
    target = None
    for s in list_sets(root):
        j = load_job(root, s) or build_job(e, s)
        if j["id"].lower() == a.job.lower() or s.lower() == a.job.lower() or a.job.lower() in s.lower():
            target = j
            break
    if not target:
        die(f"没有这套：{a.job}")
    if a.json:
        print(json.dumps(target, ensure_ascii=False, indent=2))
        return
    print(f"{target['ip']} · {target['id']} {target['title']}   阶段：{target['stage']}"
          f"{'  [已交付]' if target['delivered'] else ''}")
    print("\n阶段：")
    for s in STAGES:
        st = target["stages"].get(s, {})
        mark = {"ok": "✓", "fail": "✗", "pending": "·"}.get(st.get("status"), "·")
        line = f"  {mark} {s}"
        arts = target.get("artifacts", {}).get(s)
        if arts:
            line += "  " + ", ".join(os.path.basename(x) for x in arts[:2])
        print(line)


def cmd_job_set(a):
    e = resolve_ip(a.ip)
    root = e["path"]
    job = None
    setname = None
    for s in list_sets(root):
        j = load_job(root, s) or build_job(e, s)
        if j["id"].lower() == a.job.lower() or a.job.lower() in s.lower():
            job, setname = j, s
            break
    if not job:
        die(f"没有这套：{a.job}")
    if a.stage not in STAGES:
        die(f"阶段必须是：{'/'.join(STAGES)}")
    job["stages"].setdefault(a.stage, {})["status"] = a.status
    job["stages"][a.stage]["at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    if a.artifact:
        job["artifacts"].setdefault(a.stage, [])
        if a.artifact not in job["artifacts"][a.stage]:
            job["artifacts"][a.stage].append(os.path.abspath(a.artifact))
    if a.status == "ok":
        job["stage"] = derive_stage(job)
        if job["stage"] == "交付" and job["artifacts"].get("视频"):
            job["delivered"] = True
    if a.note:
        job.setdefault("notes", []).append({"at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                                            "text": a.note})
    with open(job_path(root, setname), "w", encoding="utf-8") as f:
        json.dump(job, f, ensure_ascii=False, indent=2)
    print(f"✓ {job['id']} {a.stage} → {a.status}   当前阶段：{job['stage']}")


def cmd_index(a):
    reg = load_registry()
    out = {"generated": datetime.now().strftime("%Y-%m-%d %H:%M"), "ips": {}}
    for e in reg.get("ips", []):
        root = e["path"]
        rows = []
        for s in list_sets(root):
            j = load_job(root, s) or build_job(e, s, write=a.write)
            rows.append(brief(j))
        out["ips"][e["name"]] = rows
    with open(INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"✓ 索引写入 {INDEX_FILE}")
    for name, rows in out["ips"].items():
        print(f"  {name}: {len(rows)} 套")


# ---------- 档案卡 ----------
def ensure_registered(name, path, tag="", space="", aliases=None):
    """兜底登记：newip.py 未必写进了本项目的册子（它默认写旧位置），这里补一刀。
    同一个参数绝不写两处——这里只存「在哪」，业务参数一律在 card.json / ip.json。"""
    reg = load_registry()
    for e in reg.get("ips", []):
        if e.get("name") == name or e.get("path") == path:
            return False
    entry = {"name": name, "path": path, "tag": tag, "space": space,
             "aliases": aliases or [name]}
    reg.setdefault("ips", []).append(entry)
    with open(REGISTRY, "w", encoding="utf-8") as f:
        json.dump(reg, f, ensure_ascii=False, indent=2)
    return True


def card_path(ip_root):
    return os.path.join(ip_root, "card.json")


def load_card(ip_root):
    p = card_path(ip_root)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return None


def cmd_cards(a):
    """列出所有档案卡：角色、类型、状态、有没有技术真值、几套片子"""
    reg = load_registry()
    rows = []
    for e in reg.get("ips", []):
        root = e["path"]
        c = load_card(root) or {}
        rows.append({
            "name": e.get("name"), "tag": e.get("tag"), "aliases": e.get("aliases", []),
            "type": c.get("type", "未分类"), "role": c.get("role", ""),
            "status": c.get("status", "活跃"),
            "card": bool(c), "ip_json": os.path.exists(os.path.join(root, "ip.json")),
            "sets": len(list_sets(root)), "path": root,
        })
    if a.json:
        print(json.dumps({"cards": rows}, ensure_ascii=False, indent=2))
        return
    print(f"档案卡 {len(rows)} 张\n")
    for r in rows:
        mark = "✓卡" if r["card"] else "✗无卡"
        print(f"  [{r['type']}] {r['name']}  {r['role'][:20]}  {r['status']}  "
              f"{r['sets']}套  {mark}")
        print(f"        {r['path']}")
        if r["aliases"]:
            print(f"        别名：{'/'.join(r['aliases'])}")


def _find_newip():
    """找 newip.py：优先引擎真源 templates/scripts/，退回旧行为（扫现有 IP 的副本）。

    2026-10-29：newip.py 以前只有 IP 档案里有（陈洁斌主号那份），
    引擎的 `new-ip` / `card-new` 反而依赖某一个客户的私人副本——
    那个 IP 一删或那份副本一改走样，接新客户就整个跑不了。
    现已收回引擎真源；这里保留旧扫描顺序做兜底，行为不变，只是先问自家人。
    """
    can = os.path.join(ROOT, "templates", "scripts", "newip.py")
    if os.path.exists(can):
        return can
    for x in load_registry().get("ips", []):
        p = os.path.join(x["path"], "03_生产脚本", "newip.py")
        if os.path.exists(p):
            return p
    return None


def cmd_card_new(a):
    """建档：建目录树（复用 newip.py）+ 生成 card.json"""
    try:                      # 已建档就拒绝，没建档 resolve_ip 会抛 SystemExit——忽略即可
        e = resolve_ip(a.name, quiet=True)
        if not a.force:
            die(f"「{a.name}」已建档：{e['path']}（要重建加 --force）")
    except SystemExit:
        pass
    script = _find_newip()
    if not script:
        die("找不到 newip.py（引擎真源 templates/scripts/ 或现有 IP 的 03_生产脚本 下）")
    cmd = [sys.executable, script, "--name", a.name, "--parent", PROFILES]
    for flag, val in (("--tag", a.tag), ("--space", a.space), ("--aliases", a.aliases)):
        if val:
            cmd += [flag, val]
    # 三个坑（2026-10-28 逐个踩过）：
    #   ① newip.py 按自身位置反推总册，档案挪到独立存储区后会算错
    #      （/cfu/ai视频档案库/<IP>/03_生产脚本 → 上三级 = /cfu/，册子不在那）
    #      → 必须用 IP_REGISTRY 显式指回项目册
    #   ② 缺参数会交互式等输入 → 堵死 stdin，否则挂起到超时被杀（exit 137 像崩溃）
    #   ③ 加 timeout 兜底
    env = dict(os.environ, IP_REGISTRY=REGISTRY)
    r = subprocess.run(cmd, capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, timeout=120, env=env)
    if r.returncode != 0:
        print(r.stdout.strip() or r.stderr.strip())
        sys.exit(r.returncode)
    root = os.path.join(PROFILES, a.name)
    card = {
        "_schema": "ip-card/1.0",
        "_说明": "档案卡 = 业务与习惯层。技术真值（音色/静帧/参数）在 ip.json，两者不重复、不互相引用。",
        "name": a.name,
        "type": a.type,
        "tag": a.tag or "",
        "aliases": [x.strip() for x in a.aliases.split(",")] if a.aliases else [],
        "role": a.role,
        "created": datetime.now().strftime("%Y-%m-%d"),
        "status": "活跃",
        "needs": {"内容方向": "", "出片频率": "", "交付平台": [], "备注": ""},
        "habits": [],
        "redlines": [],
        "policy": {"allow_cloud_llm": a.type != "客户",
                   "_policy_note": "客户档案默认禁止内容上云（走本地 9B）；本人 IP 允许走云端。"},
        "changelog": [{"at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                       "text": "建档"}],
    }
    with open(card_path(root), "w", encoding="utf-8") as f:
        json.dump(card, f, ensure_ascii=False, indent=2)
    added = ensure_registered(a.name, root, a.tag or "", a.space or "",
                              card["aliases"])
    print(f"✓ 建档 {a.name}  [{a.type}]" + ("（已补登记进总册）" if added else ""))
    print(f"   目录 {root}")
    print(f"   卡   {card_path(root)}")
    print("   下一步：录 8 秒人声 / 写逐字稿 / 定起始静帧 → 填进 ip.json 的三处 null")
    if a.json:
        print(json.dumps(card, ensure_ascii=False, indent=2))


def cmd_card_show(a):
    e = resolve_ip(a.ip)
    root = e["path"]
    c = load_card(root)
    if not c:
        die(f"「{e['name']}」还没有档案卡（跑 card-new 建一张）")
    if a.json:
        print(json.dumps(c, ensure_ascii=False, indent=2))
        return
    print(f"[{c.get('type','?')}] {c['name']}  ·  {c.get('role','')}  ·  {c.get('status','')}")
    print(f"  别名：{'/'.join(c.get('aliases', [])) or '—'}    套次前缀：{c.get('tag') or '—'}")
    print(f"  建档：{c.get('created','')}     目录：{root}\n")
    print("需求：")
    for k, v in (c.get("needs") or {}).items():
        if v:
            print(f"  {k}: {v}")
    if c.get("redlines"):
        print("\n红线：")
        for x in c["redlines"]:
            print(f"  ! {x.get('text', x) if isinstance(x, dict) else x}")
    if c.get("habits"):
        print(f"\n习惯（{len(c['habits'])} 条，最近在前）：")
        for h in reversed(c["habits"][-8:]):
            print(f"  [{h.get('at','')}] {h.get('text','')}")
    pol = c.get("policy") or {}
    print(f"\n数据策略：云端 LLM {'允许' if pol.get('allow_cloud_llm') else '禁止（走本地模型）'}")


def cmd_card_note(a):
    """往档案卡里追加一条：习惯 / 需求 / 红线"""
    e = resolve_ip(a.ip)
    root = e["path"]
    c = load_card(root)
    if not c:
        die(f"「{e['name']}」还没有档案卡")
    bucket = {"habit": "habits", "need": "needs", "redline": "redlines"}.get(a.kind, "habits")
    entry = {"at": datetime.now().strftime("%Y-%m-%d %H:%M"), "text": a.text}
    if bucket == "needs":
        c["needs"][a.key or "备注"] = a.text
    else:
        c.setdefault(bucket, []).append(entry)
    c.setdefault("changelog", []).append(
        {"at": entry["at"], "text": f"{bucket} +{a.text[:30]}"})
    with open(card_path(root), "w", encoding="utf-8") as f:
        json.dump(c, f, ensure_ascii=False, indent=2)
    print(f"✓ {e['name']} 已记入 {bucket}：{a.text}")


def cmd_guard(a):
    e = resolve_ip(a.ip)
    root = e["path"]
    script = os.path.join(root, "03_生产脚本", "ip_guard.py")
    if not os.path.exists(script):
        die(f"该 IP 没有 ip_guard.py：{script}")
    r = subprocess.run([sys.executable, script], cwd=root,
                       capture_output=True, text=True)
    print(r.stdout.strip() or r.stderr.strip())
    if a.json:
        print(json.dumps({"ip": e["name"], "exit": r.returncode}, ensure_ascii=False))
    sys.exit(r.returncode)


def cmd_new_ip(a):
    if not a.name:
        die("需要 --name")
    script = _find_newip()
    if not script:
        die("找不到 newip.py（引擎真源 templates/scripts/ 或现有 IP 的 03_生产脚本 下）")
    cmd = [sys.executable, script, "--name", a.name, "--parent", a.parent]
    for flag, val in (("--tag", a.tag), ("--space", a.space), ("--aliases", a.aliases)):
        if val:
            cmd += [flag, val]
    if a.dry:
        cmd.append("--dry")
    # 两个坑：①newip.py 默认把新 IP 写进旧总册，必须用 IP_REGISTRY 指回项目册；
    #         ②它缺参数会交互式等输入 → 堵死 stdin，否则挂起到超时
    env = dict(os.environ, IP_REGISTRY=REGISTRY)
    r = subprocess.run(cmd, capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, timeout=120, env=env)
    print(r.stdout.strip() or r.stderr.strip())
    sys.exit(r.returncode)


def script_desc(path, limit=60):
    """取脚本的一句话说明：优先 docstring 首行，其次 # 注释（跳过 shebang / coding）"""
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            head = f.read(6000)
    except Exception:
        return ""
    m = re.search(r'^"""(.*?)"""', head, re.S | re.M)
    if m:
        for line in m.group(1).splitlines():
            t = line.strip()
            if t and not t.startswith("coding"):
                return t[:limit]
    for line in head.splitlines():
        t = line.strip()
        if t.startswith("#!") or t.startswith("# -*-") or "coding" in t:
            continue
        if t.startswith("#"):
            t = t.lstrip("# ").strip()
            if t:
                return t[:limit]
    return ""


def cmd_scripts(a):
    e = resolve_ip(a.ip)
    d = os.path.join(e["path"], "03_生产脚本")
    if not os.path.isdir(d):
        die(f"没有 {d}")
    rows = []
    for fn in sorted(os.listdir(d)):
        if fn.endswith(".py"):
            p = os.path.join(d, fn)
            rows.append({"file": fn, "path": p, "desc": script_desc(p)})
    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    print(f"{e['name']} · 生产脚本 {len(rows)} 个\n")
    for r in rows:
        print(f"  {r['file']:<22} {r['desc']}")


def cmd_llm(a):
    router = os.path.join(BIN_DIR, "llm_router.py")
    env = os.path.join(ROOT, "config", "llm.env")
    if not os.path.exists(router):
        die(f"没有 {router}")
    cmd = [sys.executable, router, "chat", a.text]
    if os.path.exists(env):
        with open(env, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    # 两个坑：①newip.py 默认把新 IP 写进旧总册，必须用 IP_REGISTRY 指回项目册；
    #         ②它缺参数会交互式等输入 → 堵死 stdin，否则挂起到超时
    env = dict(os.environ, IP_REGISTRY=REGISTRY)
    r = subprocess.run(cmd, capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, timeout=120, env=env)
    print(r.stdout.strip() or r.stderr.strip())
    sys.exit(r.returncode)


def cmd_status(a):
    """总览：IP + 套次 + 关键服务是否在线"""
    out = {}
    reg = load_registry()
    out["ips"] = []
    for e in reg.get("ips", []):
        root = e["path"]
        rows = []
        for s in list_sets(root):
            j = load_job(root, s) or build_job(e, s)
            rows.append(brief(j))
        out["ips"].append({"name": e["name"], "jobs": rows})
    import urllib.request
    # ★ 必须绕代理：本机设了 http_proxy，不绕会拿到反代的 502，
    #   把「服务活着」误报成 down —— 这类假警报比没警报更坏。
    _op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    svc = {}
    for name, url in (("pipeline", "http://127.0.0.1:8000/health"),
                      ("n8n", "http://127.0.0.1:5678"),
                      ("comfyui", "http://127.0.0.1:8188/system_stats")):
        try:
            _op.open(url, timeout=3)
            svc[name] = "up"
        except Exception as ex:
            svc[name] = f"down({type(ex).__name__})"
    out["services"] = svc
    # 出片服务崩过几次 / 上次怎么死的 —— 只看 up/down 看不出「它在反复崩」
    _sl = os.path.join(os.path.dirname(BIN_DIR), ".cache", "comfy", "supervisor.log")
    if os.path.exists(_sl):
        try:
            with open(_sl, encoding="utf-8", errors="replace") as f:
                _txt = f.read()
            _n = _txt.count("════ ComfyUI 退出")
            _last = ""
            if _n:
                for _ln in _txt.rsplit("════ ComfyUI 退出", 1)[-1].splitlines():
                    if "退出码" in _ln:
                        _last = _ln.split("退出码", 1)[1].strip()
                        break
            out["comfy_crashes"] = {"count": _n, "last_rc": _last}
        except OSError:
            pass
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return
    for ip in out["ips"]:
        print(f"{ip['name']}: {len(ip['jobs'])} 套")
        for r in ip["jobs"]:
            print(f"   {r['id']:<5} {r['title'][:22]:<24} {'✓交付' if r['delivered'] else '→'+r['stage']}")
    print("\n服务：")
    for k, v in svc.items():
        print(f"   {k:<10} {v}")
    cc = out.get("comfy_crashes")
    if cc and cc.get("count"):
        print(f"   出片服务崩过 {cc['count']} 次"
              + (f"，上次退出码 {cc['last_rc']}" if cc.get("last_rc") else "")
              + "（死因看 bash bin/comfy_supervisor.sh status）")


def cmd_pipeline(a):
    """一条龙出片 —— 转发给 bin/pipeline.py（流程定义在 config/pipeline.json）"""
    import pipeline
    argv = [a.ip, a.set]
    for k in ("mode", "tier", "start", "end", "segs", "grid", "seed"):
        v = getattr(a, k, None)
        if v is not None:
            argv += ["--" + k.replace("start", "from").replace("end", "to"), str(v)]
    for k in ("dry", "force", "reseed", "json"):
        if getattr(a, k, False):
            argv.append("--" + k)
    sys.exit(pipeline.main(argv) or 0)
    # pipeline.main 内部 sys.exit，这里只是兜底


def cmd_batch(a):
    """批量出片 —— 转发给 bin/batch.py

    ★ 这里的参数表跟 batch.py 里那份是同一个意思，转发而不是重写逻辑：
      批量跑的是 `vstudio pipeline` 子进程，参数语义必须完全一致，
      否则「批量和单条出的片不一样」这种鬼问题会查很久。
    """
    import batch
    argv = []
    if getattr(a, "ip", ""):
        argv.append(a.ip)
    for k, flag in (("queue", "--queue"), ("topics", "--topics"),
                    ("count", "--count"), ("sets", "--sets"), ("tier", "--tier"),
                    ("start", "--from"), ("end", "--to")):
        v = getattr(a, k, "")
        if v:
            argv += [flag, str(v)]
    if getattr(a, "limit", 0):
        argv += ["--limit", str(a.limit)]
    for k in ("dry", "force", "stop_on_error", "no_guard", "json"):
        if getattr(a, k, False):
            argv.append("--" + k.replace("_", "-"))
    sys.exit(batch.main(argv))


def cmd_prompts(a):
    """提示词工作台 —— 转发给 bin/prompts.py"""
    import prompts
    argv = [a.ip, a.set]
    for k in ("show", "gen", "optimize", "dry", "json", "to_en", "force"):
        if getattr(a, k, False):
            argv.append("--" + k.replace("_", "-"))
    if getattr(a, "tier", ""):
        argv += ["--tier", a.tier]
    if a.seg is not None:
        argv += ["--set", str(a.seg)]
    if a.text:
        argv += ["--text", a.text]
    prompts.main(argv)


def cmd_algo(a):
    """自适应参数预览：给定片长/档位，算帧数网格、段数、留白、配音增益"""
    import studio_algo as A
    # ★ 档位优先：段数/单段上限都随档位变（fast 与 std 可能差一段），
    #   只给 mp 容易和出片用的档位对不上，预览就成了假预览。
    mp = a.mp
    tname = (getattr(a, "tier", "") or "").strip()
    if tname:
        try:
            cfg = json.load(open(os.path.join(ROOT, "config", "pipeline.json"),
                                encoding="utf-8"))
            t = (cfg.get("tiers") or {}).get(tname)
            if not t:
                die(f"没有这个档位：{tname}（可选 fast / pilot / std / high）")
            mp = float(t.get("mp", mp))
        except SystemExit:
            raise
        except Exception as ex:
            die(f"读档位失败：{ex}")
    sec = a.sec
    src = "命令行 --sec"
    if sec is None:
        e = resolve_ip(a.ip)
        sets = list_sets(e["path"])
        name = a.set if a.set in sets else \
            next((s for s in sets if s.startswith(a.set)), None)
        if not name:
            die(f"没有这套：{a.set}（现有 {' / '.join(sets) or '无'}）")
        # ★ 优先用**真实音频**：出片脚本读的就是它。
        #   以前这里直接按字数估（20.3s），出片读留白 mp3（20.76s），
        #   差 0.46s 正好跨过 5+17n 网格边界 → 预览 4 段、实出 5 段。
        sec, apath = audio_sec_of(set_dirs(e["path"], name)["06"])
        if sec:
            src = f"真实音频 {os.path.basename(apath)}"
        else:
            # 没有音频（还没跑 tts）才退回按文案字数估
            base = os.path.join(e["path"], opus_dirs(e["path"]).get("05", "05_文案"), name)
            txt = None
            for pat in ("*字幕文本*.txt", "*.txt", "*.md"):
                fs = [f for f in sorted(glob.glob(os.path.join(base, pat)))
                      if not os.path.basename(f).startswith("job")]
                if fs:
                    txt = fs[0]
                    break
            if not txt:
                die(f"没给 --sec、也没有音频和文案：{base}")
            n = len(re.sub(r"\s", "", open(txt, encoding="utf-8-sig").read()))
            sec = n / 3.85
            src = "按字数估（还没有音频，跑完 tts 再算才准）"
    p = A.plan(sec, mp, target_segs=a.segs)
    if a.json:
        print(json.dumps(dict(p, sec_source=src), ensure_ascii=False, indent=2))
        return
    print(f"{mp}MP{'（档位 ' + tname + '）' if tname else ''}"
          f" → {p['res'][0]}x{p['res'][1]}（实际 {p['real_mp']}MP）"
          f"{'  [快速档]' if p['fast'] else ''}")
    print(f"  时长来源 {src}  {sec:.2f}s")
    print(f"  片长 {sec:.1f}s  留白 前 {p['pad_head']}s / 后 {p['pad_tail']}s"
          f"  → 对齐后 {p['padded_sec']}s")
    print(f"  总帧 {p['total_frames']}（{p['total_sec']}s，≡5 mod 17）"
          f"  单段上限 {p['maxlen']}")
    print(f"  {p['segs']} 段  格数 {'/'.join(str(x) for x in p['grid'])}"
          f"  切点 {p['cuts_sec']}")
    print(f"  段长 {p['seg_sec']}   配乐低人声 {p['bgm_offset']} LU")
    print(f"\n  SEG_GRID={','.join(str(x) for x in p['grid'])}"
          f"  （手动锁定时用；不传则由脚本按停顿自适应）")


def cmd_scripts_sync(a):
    """把引擎真源 templates/scripts/ 推给某个 IP（——folders 收 1 份，别处都是副本）"""
    import hashlib
    import difflib
    from datetime import datetime

    mpath = os.path.join(ROOT, "templates", "scripts", "manifest.json")
    if not os.path.exists(mpath):
        die(f"引擎真源不存在：{mpath}")
    man = json.loads(open(mpath, encoding="utf-8").read())
    targets = man.get("targets", {})
    if a.file:
        want = set(a.file.split(","))
        unknown = sorted(want - set(targets))
        if unknown:
            die(f"真源里没有这些文件：{unknown}")
        targets = {k: v for k, v in targets.items() if k in want}

    e = resolve_ip(a.ip)

    def read(p):
        return open(p, encoding="utf-8", errors="ignore").read().splitlines() if os.path.exists(p) else []

    new, chg, same, miss_dir = [], [], [], []
    for fn, sub in sorted(targets.items()):
        src = os.path.join(ROOT, "templates", "scripts", fn)
        dst_dir = os.path.join(e["path"], sub)
        dst = os.path.join(dst_dir, fn)
        if not os.path.isdir(dst_dir):
            miss_dir.append((fn, sub))
            continue
        if not os.path.exists(dst):
            new.append((fn, sub, src, dst))
            continue
        sh = hashlib.md5(open(src, "rb").read()).hexdigest()
        dh = hashlib.md5(open(dst, "rb").read()).hexdigest()
        if sh == dh:
            same.append(fn)
        else:
            chg.append((fn, sub, src, dst))

    if a.json:
        print(json.dumps({"ip": e["name"], "path": e["path"],
                          "new": [x[0] for x in new], "changed": [x[0] for x in chg],
                          "same": same, "missing_dir": sorted({x[1] for x in miss_dir})},
                         ensure_ascii=False, indent=2))
        return

    print(f"引擎真源 → {e['name']}（{e['path']}）\n")
    print(f"① 需更新 {len(chg)} 个")
    for fn, sub, src, dst in chg:
        nd = len([x for x in difflib.unified_diff(read(dst), read(src), n=0)
                  if x[:1] in "+-" and x[:3] not in ("+++", "---")])
        print(f"   {fn:<22} → {sub}   差 {nd} 行")
    if new:
        print(f"\n② IP 里还没有 {len(new)} 个（会新增）")
        for fn, sub, _s, _d in new:
            print(f"   {fn:<22} → {sub}")
    if miss_dir:
        print(f"\n③ 目标目录不存在 {len(miss_dir)} 个：{sorted({x[1] for x in miss_dir})}")
    print(f"\n④ 已一致 {len(same)} 个，不用动")

    if not (chg or new):
        print("\n没有需要同步的内容。")
        return
    if not a.force:
        print("\n以上是预览。真要覆盖加 --force（旧文件会自动备份到 08_过程/_脚本备份/）。")
        print("⚠ 覆盖前先确认 diff：某个 IP 若被手工改过且没写回 ip.json，覆盖会丢失那部分改动。")
        return

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = os.path.join(e["path"], "08_过程", "_脚本备份", stamp)
    done = []
    for fn, sub, src, dst in chg + new:
        if os.path.exists(dst):
            os.makedirs(bak, exist_ok=True)
            os.replace(dst, os.path.join(bak, fn))   # 挪进备份（不是复制，保持目录干净）
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        done.append(fn)
    print(f"\n✓ 已同步 {len(done)} 个到 {e['name']}")
    if os.path.isdir(bak):
        print(f"  旧文件备份在：{bak}（确认没问题后可以删）")


def cmd_scripts_diff(a):
    """脚本偏离比对：50 个 IP 时，谁改了什么、哪些该抽回引擎"""
    import hashlib
    reg = load_registry()
    ips = [e for e in reg.get("ips", [])
           if os.path.isdir(os.path.join(e["path"], "03_生产脚本"))]
    if not ips:
        die("没有任何 IP 带 03_生产脚本")
    names = [e["name"] for e in ips]
    base_e = None
    if a.base:
        base_e = resolve_ip(a.base)
    else:
        base_e = ips[0]

    table = {}          # file -> {ip: (hash, path)}
    for e in ips:
        d = os.path.join(e["path"], "03_生产脚本")
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".py"):
                continue
            p = os.path.join(d, fn)
            with open(p, "rb") as f:
                table.setdefault(fn, {})[e["name"]] = (hashlib.md5(f.read()).hexdigest(), p)

    only, same, fork = [], [], []
    for fn, m in sorted(table.items()):
        if len(m) == 1:
            only.append((fn, list(m)[0]))
            continue
        hs = {v[0] for v in m.values()}
        if len(hs) == 1:
            same.append(fn)
        else:
            fork.append(fn)

    if a.json:
        print(json.dumps({"base": base_e["name"], "ips": names,
                          "only_one": only, "identical": same, "forked": fork},
                         ensure_ascii=False, indent=2))
        return

    print(f"基准 {base_e['name']}   参与比对 {len(ips)} 个 IP：{'/'.join(names)}\n")
    print(f"① 全一致 {len(same)} 个（内容逐字节相同 → 可以安全抽回引擎，各 IP 只留参数）")
    for fn in same:
        print(f"   {fn}")
    if fork:
        print(f"\n② 已分叉 {len(fork)} 个（同名不同内容 —— 判断是参数差异还是算法差异）")
        for fn in fork:
            bp = os.path.join(base_e["path"], "03_生产脚本", fn)
            if not os.path.exists(bp):
                print(f"   {fn:<22} 基准没有这个文件")
                continue
            import difflib
            bl = open(bp, encoding="utf-8", errors="ignore").read().splitlines()
            for e in ips:
                p = os.path.join(e["path"], "03_生产脚本", fn)
                if not os.path.exists(p):
                    continue
                ol = open(p, encoding="utf-8", errors="ignore").read().splitlines()
                nd = len([x for x in difflib.unified_diff(bl, ol, n=0)
                          if x[:1] in "+-" and x[:3] not in ("+++", "---")])
                if nd:
                    print(f"   {fn:<22} {e['name']} 差 {nd} 行")
    if only:
        print(f"\n③ 只有个别 IP 有 {len(only)} 个")
        for fn, ipname in only:
            print(f"   {fn:<22} 仅 {ipname}")


def main():
    ap = argparse.ArgumentParser(description="洁斌资产 · 视频生产总控")
    ap.add_argument("--json", action="store_true", help="输出 JSON（给 agent 用）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ips", help="列出 IP")
    p.set_defaults(func=cmd_ips)

    p = sub.add_parser("jobs", help="列出套次进度")
    p.add_argument("ip", nargs="?", default="洁斌")
    p.add_argument("--only", help="过滤套次名")
    p.set_defaults(func=cmd_jobs)

    p = sub.add_parser("job-new", help="开一套新片")
    p.add_argument("ip", nargs="?", default="洁斌")
    p.add_argument("set", help="套次名，如 S05 或 S05_讲AI变现")
    p.add_argument("--title", help="主题（set 里没写时补）")
    p.set_defaults(func=cmd_job_new)

    p = sub.add_parser("job-show", help="看一套详情")
    p.add_argument("ip", nargs="?", default="洁斌")
    p.add_argument("job")
    p.set_defaults(func=cmd_job_show)

    p = sub.add_parser("job-set", help="推进阶段")
    p.add_argument("ip", nargs="?", default="洁斌")
    p.add_argument("job")
    p.add_argument("stage", help="/".join(STAGES))
    p.add_argument("status", nargs="?", default="ok", choices=["ok", "fail", "pending"])
    p.add_argument("--artifact", help="登记产物路径")
    p.add_argument("--note", help="备注")
    p.set_defaults(func=cmd_job_set)

    p = sub.add_parser("index", help="重建 jobs 索引")
    p.add_argument("--write", action="store_true", help="顺带给没 job.json 的套次补写")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("cards", help="列出所有档案卡")
    p.set_defaults(func=cmd_cards)

    p = sub.add_parser("card-new", help="给新客户/新角色建档")
    p.add_argument("--name", required=True)
    p.add_argument("--type", default="客户", choices=["本人IP", "客户"])
    p.add_argument("--tag", help="套次前缀（本人 S / 客户 M…）")
    p.add_argument("--role", default="", help="角色定位，如 财经口播·竖屏")
    p.add_argument("--aliases", help="别名，逗号分隔")
    p.add_argument("--space", help="资料库空间名")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_card_new)

    p = sub.add_parser("card-show", help="看一张档案卡（需求/习惯/红线）")
    p.add_argument("ip")
    p.set_defaults(func=cmd_card_show)

    p = sub.add_parser("card-note", help="记一条习惯/需求/红线")
    p.add_argument("ip")
    p.add_argument("text")
    p.add_argument("--kind", default="habit", choices=["habit", "need", "redline"])
    p.add_argument("--key", help="kind=need 时的字段名，如 出片频率")
    p.set_defaults(func=cmd_card_note)

    p = sub.add_parser("guard", help="开工前素材自检（缺则非零退出）")
    p.add_argument("ip", nargs="?", default="洁斌")
    p.set_defaults(func=cmd_guard)

    p = sub.add_parser("new-ip", help="接新客户")
    p.add_argument("--name", required=True)
    p.add_argument("--tag")
    p.add_argument("--space")
    p.add_argument("--aliases")
    p.add_argument("--parent", default=PROFILES, help=f"建在哪（默认 {PROFILES}）")
    p.add_argument("--dry", action="store_true")
    p.set_defaults(func=cmd_new_ip)

    p = sub.add_parser("scripts", help="列出生产脚本")
    p.add_argument("ip", nargs="?", default="洁斌")
    p.set_defaults(func=cmd_scripts)

    p = sub.add_parser("llm", help="走 LLM 路由问一句")
    p.add_argument("text")
    p.set_defaults(func=cmd_llm)

    p = sub.add_parser("status", help="总览：IP + 套次 + 服务")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("pipeline", help="一条龙出片（流程见 config/pipeline.json）")
    p.add_argument("ip", nargs="?", default="洁斌")
    p.add_argument("set")
    p.add_argument("--mode", default="auto", choices=["auto", "step"])
    p.add_argument("--tier", default=None, help="fast/pilot/std/high")
    p.add_argument("--from", dest="start")
    p.add_argument("--to", dest="end")
    p.add_argument("--segs", type=int)
    p.add_argument("--grid")
    p.add_argument("--dry", action="store_true")
    p.add_argument("--force", action="store_true",
                   help="已有产物也重做（不看输入变没变）")
    p.add_argument("--seed", type=int, help="指定种子，复现某一次的画面")
    p.add_argument("--reseed", action="store_true",
                   help="换一个新种子（同一套提示词再赌一次画面）")
    p.set_defaults(func=cmd_pipeline)

    p = sub.add_parser("batch", help="批量出片：一份队列串行出多条（GPU 独占，不能并行）")
    p.add_argument("ip", nargs="?", default="")
    p.add_argument("--queue", help="队列文件（JSON），格式见 bin/batch.py 顶部")
    p.add_argument("--topics", default="", help="快捷方式：分号分隔的选题 '甲;乙;丙'")
    p.add_argument("--count", type=int, default=0, help="选题留空时：AI 按人设自动出 N 条选题（1-20），逐条写文案+出片")
    p.add_argument("--sets", default="", help="配 --topics 用，逗号分隔的套次名")
    p.add_argument("--tier", default="")
    p.add_argument("--from", dest="start", default="")
    p.add_argument("--to", dest="end", default="")
    p.add_argument("--dry", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--stop-on-error", action="store_true")
    p.add_argument("--no-guard", action="store_true")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_batch)

    p = sub.add_parser("prompts", help="提示词工作台（AI 生成/优化/微调，过红线才落盘）")
    p.add_argument("ip", nargs="?", default="洁斌")
    p.add_argument("set")
    p.add_argument("--show", action="store_true")
    p.add_argument("--gen", action="store_true")
    p.add_argument("--optimize", action="store_true")
    p.add_argument("--set", dest="seg", type=int, help="手动改第 N 段")
    p.add_argument("--text", help="与 --set 同用的提示词")
    p.add_argument("--dry", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--to-en", dest="to_en", action="store_true",
                   help="把中文分镜译成 prompts.en.json（★出片只认这份英文）")
    p.add_argument("--force", action="store_true", help="与 --to-en 同用：强制重译")
    p.add_argument("--tier", default="",
                   help="按哪个出片档位算段数（fast/pilot/std/high）；★段数依赖档位，先定档再出提示词")
    p.set_defaults(func=cmd_prompts)

    p = sub.add_parser("algo", help="自适应参数预览（帧数/段数/留白）")
    p.add_argument("ip", nargs="?", default="洁斌")
    p.add_argument("set")
    p.add_argument("--sec", type=float, help="片长秒（不给则按文案字数估）")
    p.add_argument("--mp", type=float, default=1.26)
    p.add_argument("--tier", default="", help="按档位取 mp（fast/pilot/std/high），优先于 --mp")
    p.add_argument("--segs", type=int)
    p.set_defaults(func=cmd_algo)

    p = sub.add_parser("scripts-diff", help="各 IP 脚本偏离比对")
    p.add_argument("--base", help="基准 IP（默认第一个）")
    p.set_defaults(func=cmd_scripts_diff)

    p = sub.add_parser("scripts-sync", help="把引擎真源推给某个 IP（先预览，加 --force 才写）")
    p.add_argument("ip", help="目标 IP 名/别名")
    p.add_argument("--file", help="只同步指定文件，逗号分隔")
    p.add_argument("--force", action="store_true", help="实际覆盖（旧文件备份到 08_过程/_脚本备份/）")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_scripts_sync)

    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
