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
import json
import os
import re
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
ARTIFACT_RULES = {
    "文案": [("文案", [".md", ".txt"]), ("字幕文本", [".txt"])],
    "音频": [("音频_定板", [".mp3", ".wav"])],
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


def set_dirs(ip_root, setname):
    """一套片子横跨的目录（05~08 同名）"""
    d = opus_dirs(ip_root)
    return {k: os.path.join(ip_root, d[k], setname) for k in ("05", "06", "07", "08")}


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


def cmd_card_new(a):
    """建档：建目录树（复用 newip.py）+ 生成 card.json"""
    try:                      # 已建档就拒绝，没建档 resolve_ip 会抛 SystemExit——忽略即可
        e = resolve_ip(a.name, quiet=True)
        if not a.force:
            die(f"「{a.name}」已建档：{e['path']}（要重建加 --force）")
    except SystemExit:
        pass
    script = None
    for x in load_registry().get("ips", []):
        p = os.path.join(x["path"], "03_生产脚本", "newip.py")
        if os.path.exists(p):
            script = p
            break
    if not script:
        die("找不到 newip.py")
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
    script = None
    for e in load_registry().get("ips", []):
        p = os.path.join(e["path"], "03_生产脚本", "newip.py")
        if os.path.exists(p):
            script = p
            break
    if not script:
        die("找不到 newip.py（现有 IP 的 03_生产脚本 下）")
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
    svc = {}
    for name, url in (("pipeline", "http://127.0.0.1:8000/health"),
                      ("n8n", "http://127.0.0.1:5678"),
                      ("comfyui", "http://127.0.0.1:8188/system_stats")):
        try:
            urllib.request.urlopen(url, timeout=3)
            svc[name] = "up"
        except Exception as ex:
            svc[name] = f"down({type(ex).__name__})"
    out["services"] = svc
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

    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
