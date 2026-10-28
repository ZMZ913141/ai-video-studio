#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ip-studio MCP —— 把「洁斌资产」的生产控制面暴露成 MCP 工具。

★ 这是给「像我这样的 AI」用的接口：任何支持 MCP 的客户端（Claude Code、
  Cursor、WorkBuddy、Qwen Code…）挂上它，就能直接查进度、开新套、跑自检，
  不需要人去敲命令，也不依赖任何 UI。

底层与 CLI（vstudio.py）共用同一批函数，所以不会出现「CLI 说 ok、MCP 说不行」。

运行（本文件不带依赖，需在装了 mcp 的 venv 里跑）：
  /home/qmr/github/ai-video-studio/mcp-servers/ip-studio/.venv/bin/python \
      /home/qmr/github/ai-video-studio/bin/mcp_server.py

注册（Claude Code 示例）：
  claude mcp add ip-studio -- \
      /home/qmr/github/ai-video-studio/mcp-servers/ip-studio/.venv/bin/python \
      /home/qmr/github/ai-video-studio/bin/mcp_server.py

★ 与 CLI（bin/vstudio.py）共用同一批底层函数，不会出现「CLI 说 ok、MCP 说不行」。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:                                    # mcp 1.x
    from mcp.server.fastmcp import FastMCP
except ModuleNotFoundError:             # mcp 2.x：FastMCP 改名 MCPServer
    from mcp.server.mcpserver import MCPServer as FastMCP
import vstudio as vs  # noqa: E402

mcp = FastMCP("ip-studio")


@mcp.tool()
def ips() -> dict:
    """列出本体系所有 IP（名字/别名/路径/套次数），以及刻意不纳管的独立体系。"""
    reg = vs.load_registry()
    out = []
    for e in reg.get("ips", []):
        root = e.get("path", "")
        out.append({"name": e.get("name"), "tag": e.get("tag"), "path": root,
                    "aliases": e.get("aliases", []),
                    "ip_json": os.path.exists(os.path.join(root, "ip.json")),
                    "sets": len(vs.list_sets(root)) if os.path.isdir(root) else 0})
    return {"ips": out,
            "excluded": [{"name": x.get("name"), "path": x.get("path")}
                         for x in reg.get("excluded", [])]}


@mcp.tool()
def jobs(ip: str = "洁斌") -> dict:
    """看某个 IP 的所有套次进度（阶段、缺什么、是否已交付）。"""
    e = vs.resolve_ip(ip)
    root = e["path"]
    return {"ip": e["name"],
            "jobs": [vs.brief(vs.load_job(root, s) or vs.build_job(e, s))
                     for s in vs.list_sets(root)]}


@mcp.tool()
def job_show(ip: str, job: str) -> dict:
    """看一套片子的完整状态：各阶段、产物路径、验收、是否交付。"""
    e = vs.resolve_ip(ip)
    root = e["path"]
    for s in vs.list_sets(root):
        j = vs.load_job(root, s) or vs.build_job(e, s)
        if job.lower() in (j["id"].lower(), s.lower()) or job.lower() in s.lower():
            return j
    return {"error": f"没有这套：{job}"}


@mcp.tool()
def job_new(ip: str, set_name: str, title: str = "") -> dict:
    """开一套新片：建 05~08 四个目录并生成 job.json。set_name 如 S05 或 S05_讲AI变现。"""
    e = vs.resolve_ip(ip)
    if not vs.re.match(r"^[A-Za-z]+\d+", set_name):
        set_name = f"{e.get('tag','S')}{set_name}"
    if "_" not in set_name and title:
        set_name = f"{set_name}_{title}"
    for p in vs.set_dirs(e["path"], set_name).values():
        os.makedirs(p, exist_ok=True)
    job = vs.build_job(e, set_name, write=True)
    return {"ok": True, "job": vs.brief(job), "dirs": vs.set_dirs(e["path"], set_name)}


@mcp.tool()
def job_set(ip: str, job: str, stage: str, status: str = "ok",
            artifact: str = "", note: str = "") -> dict:
    """推进一套片子的阶段。stage ∈ 文案/音频/视频/后期/字幕/验收/交付。"""
    e = vs.resolve_ip(ip)
    root = e["path"]
    for s in vs.list_sets(root):
        j = vs.load_job(root, s) or vs.build_job(e, s)
        if job.lower() in (j["id"].lower(), s.lower()):
            j["stages"].setdefault(stage, {})["status"] = status
            j["stages"][stage]["at"] = vs.datetime.now().strftime("%Y-%m-%d %H:%M")
            if artifact:
                j["artifacts"].setdefault(stage, [])
                if artifact not in j["artifacts"][stage]:
                    j["artifacts"][stage].append(os.path.abspath(artifact))
            if status == "ok":
                j["stage"] = vs.derive_stage(j)
            if note:
                j.setdefault("notes", []).append(
                    {"at": vs.datetime.now().strftime("%Y-%m-%d %H:%M"), "text": note})
            with open(vs.job_path(root, s), "w", encoding="utf-8") as f:
                vs.json.dump(j, f, ensure_ascii=False, indent=2)
            return {"ok": True, "id": j["id"], "stage": j["stage"]}
    return {"error": f"没有这套：{job}"}


@mcp.tool()
def cards() -> dict:
    """列出所有档案卡：类型（本人IP/客户）、角色、状态、有几套片子、卡是否齐全。"""
    rows = []
    for e in vs.load_registry().get("ips", []):
        root = e["path"]
        c = vs.load_card(root) or {}
        rows.append({"name": e.get("name"), "tag": e.get("tag"),
                     "aliases": e.get("aliases", []),
                     "type": c.get("type", "未分类"), "role": c.get("role", ""),
                     "status": c.get("status", "活跃"), "card": bool(c),
                     "sets": len(vs.list_sets(root)), "path": root})
    return {"cards": rows}


@mcp.tool()
def card_show(ip: str) -> dict:
    """看一张档案卡：需求、习惯、红线、数据策略。开工前先看这个，别凭记忆。"""
    e = vs.resolve_ip(ip)
    c = vs.load_card(e["path"])
    if not c:
        return {"error": f"「{e['name']}」还没有档案卡（用 card_new 建一张）"}
    return c


@mcp.tool()
def card_note(ip: str, text: str, kind: str = "habit", key: str = "") -> dict:
    """往档案卡里记一条：习惯(habit)/需求(need)/红线(redline)。客户的偏好都记这里。"""
    e = vs.resolve_ip(ip)
    c = vs.load_card(e["path"])
    if not c:
        return {"error": f"「{e['name']}」还没有档案卡"}
    bucket = {"habit": "habits", "need": "needs", "redline": "redlines"}.get(kind, "habits")
    at = vs.datetime.now().strftime("%Y-%m-%d %H:%M")
    if bucket == "needs":
        c["needs"][key or "备注"] = text
    else:
        c.setdefault(bucket, []).append({"at": at, "text": text})
    c.setdefault("changelog", []).append({"at": at, "text": f"{bucket} +{text[:30]}"})
    with open(vs.card_path(e["path"]), "w", encoding="utf-8") as f:
        vs.json.dump(c, f, ensure_ascii=False, indent=2)
    return {"ok": True, "ip": e["name"], "bucket": bucket}


@mcp.tool()
def guard(ip: str = "洁斌") -> dict:
    """开工前自检：音色参考/静帧/形象底图是否齐备，并校验没有被别的 IP 污染。
    返回 exit != 0 就是不能出片。"""
    e = vs.resolve_ip(ip)
    script = os.path.join(e["path"], "03_生产脚本", "ip_guard.py")
    if not os.path.exists(script):
        return {"error": f"没有 {script}"}
    r = vs.subprocess.run([sys.executable, script], cwd=e["path"],
                          capture_output=True, text=True)
    return {"ip": e["name"], "exit": r.returncode, "ok": r.returncode == 0,
            "output": (r.stdout or r.stderr).strip()}


@mcp.tool()
def scripts(ip: str = "洁斌") -> dict:
    """列出该 IP 的 03_生产脚本 里有哪些 py 可跑（带首行说明）。"""
    e = vs.resolve_ip(ip)
    d = os.path.join(e["path"], "03_生产脚本")
    rows = [{"file": fn, "path": os.path.join(d, fn), "desc": vs.script_desc(os.path.join(d, fn))}
            for fn in sorted(os.listdir(d)) if fn.endswith(".py")]
    return {"ip": e["name"], "scripts": rows}


@mcp.tool()
def status() -> dict:
    """总览：各 IP 套次 + pipeline/n8n/ComfyUI 三个服务是否在线。开工前先看这个。"""
    import urllib.request
    out = {"ips": []}
    for e in vs.load_registry().get("ips", []):
        root = e["path"]
        out["ips"].append({"name": e["name"], "jobs": [
            vs.brief(vs.load_job(root, s) or vs.build_job(e, s))
            for s in vs.list_sets(root)]})
    svc = {}
    for name, url in (("pipeline", "http://127.0.0.1:8000/health"),
                      ("n8n", "http://127.0.0.1:5678"),
                      ("comfyui", "http://127.0.0.1:8188/system_stats")):
        try:
            urllib.request.urlopen(url, timeout=3)
            svc[name] = "up"
        except Exception:
            svc[name] = "down"
    out["services"] = svc
    return out


if __name__ == "__main__":
    mcp.run()
