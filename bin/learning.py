#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
learning —— 让项目真的会成长（自动学习闭环）

之前的状态（2026-10-30 之前）：
    教训全靠人手写进 card.json 的 habits / redlines，或者写进 docs/ 和我的记忆文件。
    结果是**项目本身什么都没学到** —— 同一个坑，下一次照样踩。

闭环怎么闭上的
==============
    ① 记：人在界面上做「重跑 / 重录 / 退回」这类动作时，问他一句为什么，
       原因落进 <IP>/08_过程/_学习.jsonl（一条一行，只增不改）。
    ② 提炼：**promote** 攒够一定条数后，让模型把同类教训归纳成几句「习惯」，
       产出一份**建议草案**（_习惯建议.json），不直接改 card.json。
       人在界面上看过、点「采纳」，才写进 card.json 的 habits。
    ③ 用：studio_ai 出选题 / 写文案 / 写提示词时，把 habits + 近期教训
       一起塞进 context，模型就应该照着做。

★ 为什么用 jsonl 而不是直接改 card.json：card.json 是**业务真值**，
  动它要走二次确认 + changelog（界面里已经这么做了）。
  学习记录是「流水」，量大、会重复、还可能说错 —— 不该污染真值。
  只有提炼之后那一条，才配进 card.json。

用法:
    python3 bin/learning.py add <IP> --kind 重跑 --reason "后段过曝"
    python3 bin/learning.py list <IP> [-n 20]
    python3 bin/learning.py digest <IP>          # 给 LLM 用的一段话
    python3 bin/learning.py habits <IP>          # 已有习惯 + 各类计数
    python3 bin/learning.py promote <IP>         # 让模型归纳 → 出建议草案（不写真值）
    python3 bin/learning.py promote <IP> --force # 不足阈值也硬提炼
    python3 bin/learning.py apply <IP> --items "习惯A" "习惯B"   # 采纳，写进 card.json
"""
import argparse
import json
import os
import shutil
import sys
import time
from datetime import datetime

BIN_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BIN_DIR)
sys.path.insert(0, BIN_DIR)
# ★ 必须在上面 sys.path 之后：本文件也会被 importlib 从文件路径加载（那时没有 bin/）
from filebak import bak_path  # noqa: E402

# 什么动作值得记一条。全是「上一次没做对」的信号。
KINDS = {
    "重跑": "这一步跑过了还要再跑一次 —— 上次的结果不满意",
    "重录": "配音重抽 —— 语气/气口/时长不对",
    "重做画面": "出片重做 —— 脸崩/动作错/接缝跳",
    "改文案": "文案改了 —— 落点不对/太长/不像他说话",
    "退回": "交付被退回 —— 客户不满意",
    "手动改": "人手工改了产物 —— 说明自动出来的不对",
}

LOG_NAME = "_学习.jsonl"
SUGGEST_NAME = "_习惯建议.json"
# 攒够几条才提示「可以提炼了」。太少归纳不出东西，太多人会拖着不点。
PROMOTE_THRESHOLD = 3
PROMOTE_SYSTEM = """你是「洁斌视频工作台」的学习归纳器。下面给你一个 IP 已经有的习惯，
和它最近真实发生过的返工记录（每一条都是人亲手记下的「上次为什么返工」）。

请把同类返工归纳成**几条可执行的正向做法**，作为以后出稿时该遵守的习惯。

严格要求：
1. 每条必须是**正向做法**，写「要怎么做」，不要写「不要/不许/避免」这种否定句 ——
   否定句喂给生成模型时它容易当没看见。例如返工原因是「后段过曝」，
   归纳成「出片时整段曝光压在上中间调，最亮处不贴白」而不是「不许过曝」。
2. 每条一句话，具体到能照着做，别写「注意质量」这种空话。
3. 最多 3 条。**已经存在的习惯里有的，不要重复归纳**。
4. 只归纳出现过的坑，不许凭空补「行业最佳实践」。记录里没提过的别编。
5. 如果记录彼此无关、归纳不出共同做法，就返回空数组，并说明原因 —— 硬凑更糟。

返回 JSON：{"habits": ["做法1", "做法2"], "reason": "为什么归纳出这几条（一句话）"}
"""


def ip_root(name):
    from vstudio import resolve_ip
    return resolve_ip(name)["path"]


def log_path(root):
    d = os.path.join(root, "08_过程")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, LOG_NAME)


def add(ip, kind, reason, setname="", step="", actor="ui"):
    """记一条教训。返回落盘的那条 dict。"""
    if kind not in KINDS:
        raise ValueError("不认识的 kind：%s（可选 %s）" % (kind, "、".join(KINDS)))
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("必须写原因 —— 不写原因的记录等于没记")
    root = ip_root(ip)
    rec = {"at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "ip": ip, "kind": kind, "reason": reason,
           "set": setname, "step": step, "actor": actor}
    with open(log_path(root), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def load(ip, limit=50):
    root = ip_root(ip)
    p = log_path(root)
    if not os.path.isfile(p):
        return []
    out = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out[-limit:]


def digest(ip, limit=12):
    """给 LLM 用的一段话：这个 IP 踩过的坑，按最近在前排列。

    ★ 只给「因」不给「果」—— 模型需要知道的是「上次为什么返工」，
      不是「上次返工了」。所以每条都以 kind + reason 的形式出现。
    """
    rows = load(ip, limit)
    if not rows:
        return ""
    lines = ["【这个 IP 之前的返工记录 —— 出稿时请主动避开】"]
    for r in rows:
        tail = "（%s）" % r["set"] if r.get("set") else ""
        lines.append("- %s：%s%s" % (r.get("kind", "?"), r.get("reason", ""), tail))
    lines.append("以上每一条都是**真实发生过的返工**，不是猜测。能避开的就避开。")
    return "\n".join(lines)


def count(ip):
    """总行数。load(limit) 只给尾部，判「有多少条没提炼过」必须用总数。"""
    p = log_path(ip_root(ip))
    if not os.path.isfile(p):
        return 0
    with open(p, encoding="utf-8") as f:
        return sum(1 for line in f if line.strip())


def suggest_path(root):
    return os.path.join(root, "08_过程", SUGGEST_NAME)


def load_suggest(ip):
    p = suggest_path(ip_root(ip))
    if not os.path.isfile(p):
        return {}
    try:
        return json.load(open(p, encoding="utf-8")) or {}
    except Exception:
        return {}


def habits(ip):
    """卡片里已有的习惯 + 学习记录里反复出现的坑（人看的那份统计）。"""
    root = ip_root(ip)
    try:
        card = json.load(open(os.path.join(root, "card.json"), encoding="utf-8"))
    except Exception:
        card = {}
    have = [str(x) for x in (card.get("habits") or [])]
    rows = load(ip, 200)
    tally = {}
    for r in rows:
        tally.setdefault(r.get("kind", "?"), []).append(r.get("reason", ""))
    prev = load_suggest(ip)
    total = count(ip)
    based = int(prev.get("based_on_count") or 0)
    return {"have": have, "tally": {k: len(v) for k, v in tally.items()},
            "samples": {k: v[:3] for k, v in tally.items()},
            "total": total, "new_since": max(0, total - based),
            "can_promote": max(0, total - based) >= PROMOTE_THRESHOLD,
            "threshold": PROMOTE_THRESHOLD,
            "suggest": prev}


def promote(ip, prefer=None, force=False):
    """把「上次提炼之后新增的」返工记录交给模型归纳成习惯建议。

    ★ 不写 card.json。card.json 是业务真值，动它要二次确认 + changelog。
      模型归纳的东西会重复、会说错 —— 只配当**建议**躺在 _习惯建议.json 里，
      等人点「采纳」才进真值。这跟「学习记录用 jsonl 不用 card.json」是同一条道理。

    为什么记 based_on_count：下次只把**新增**的记录喂给模型，不然每提炼一次
    都把老记录重讲一遍，模型会反复给出同一条建议。
    """
    root = ip_root(ip)
    prev = load_suggest(ip)
    total = count(ip)
    based = int(prev.get("based_on_count") or 0)
    unseen = load(ip, 100000)[based:]
    if len(unseen) < PROMOTE_THRESHOLD and not force:
        return {"ok": False, "total": total, "new": len(unseen),
                "threshold": PROMOTE_THRESHOLD,
                "reason": "新增返工记录还不到 %d 条，先攒着" % PROMOTE_THRESHOLD}

    try:
        card = json.load(open(os.path.join(root, "card.json"), encoding="utf-8"))
    except Exception:
        card = {}
    have = [str(x) for x in (card.get("habits") or [])]

    payload = json.dumps({
        "已有习惯": have,
        "本次要归纳的返工记录": [
            {"类型": r.get("kind"), "原因": r.get("reason"),
             "套次": r.get("set") or "", "时间": r.get("at")} for r in unseen],
    }, ensure_ascii=False, indent=2)

    # studio_ai.chat_json 强制 sensitive=True（客户返工记录是商业信息，只走付费池）。
    from studio_ai import chat_json
    data, provider = chat_json(PROMOTE_SYSTEM, payload,
                               max_tokens=800, prefer=prefer, temperature=0.3)
    items = [str(x).strip() for x in (data.get("habits") or []) if str(x).strip()][:3]
    doc = {
        "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "based_on_count": total, "new_count": len(unseen),
        "provider": provider, "reason": str(data.get("reason") or ""),
        "habits": items, "adopted": [],
    }
    p = suggest_path(root)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    doc["ok"] = True
    doc["path"] = p
    return doc


def apply(ip, items):
    """采纳建议：把选中的几条写进 card.json 的 habits。返回 (写入条数, 备份路径)。

    ★ 只做「写盘 + 备份」。changelog 由调用方（ui_server）记 —— learning.py
      独立跑时不 import ui_server，避免循环依赖。
    """
    items = [str(x).strip() for x in (items or []) if str(x).strip()]
    if not items:
        raise ValueError("没有选中任何一条建议")
    root = ip_root(ip)
    cp = os.path.join(root, "card.json")
    data = {}
    if os.path.isfile(cp):
        try:
            data = json.load(open(cp, encoding="utf-8")) or {}
        except Exception:
            data = {}
    cur = [str(x) for x in (data.get("habits") or [])]
    added = [x for x in items if x not in cur]      # 已存在的跳过，不重复加
    if not added:
        return 0, None
    data["habits"] = cur + added
    bak = bak_path(cp)
    if os.path.isfile(cp):
        shutil.copy2(cp, bak)
    else:
        bak = None
    tmp = cp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, cp)
    # 草案里记下已采纳的，界面就不会再把同一条摆出来让人重复点
    sp = suggest_path(root)
    if os.path.isfile(sp):
        try:
            sd = json.load(open(sp, encoding="utf-8"))
            sd["adopted"] = sorted(set([str(x) for x in (sd.get("adopted") or [])] + added))
            with open(sp, "w", encoding="utf-8") as f:
                json.dump(sd, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
    return len(added), bak


def main():
    ap = argparse.ArgumentParser(description="自动学习闭环：记教训 / 出摘要")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="记一条返工教训")
    a.add_argument("ip")
    a.add_argument("--kind", required=True, choices=list(KINDS))
    a.add_argument("--reason", required=True)
    a.add_argument("--set", default="")
    a.add_argument("--step", default="")

    l = sub.add_parser("list", help="列出记过的教训")
    l.add_argument("ip")
    l.add_argument("-n", type=int, default=20)

    d = sub.add_parser("digest", help="给 LLM 用的一段话")
    d.add_argument("ip")
    d.add_argument("-n", type=int, default=12)

    h = sub.add_parser("habits", help="已有习惯 + 计数 + 可否提炼")
    h.add_argument("ip")

    pm = sub.add_parser("promote", help="让模型归纳返工记录 → 出建议草案（不写真值）")
    pm.add_argument("ip")
    pm.add_argument("--force", action="store_true", help="不足阈值也硬提炼")
    pm.add_argument("--prefer", default="", help="指定 LLM 车道")
    pm.add_argument("--json", action="store_true", help="单行 JSON 输出（给 UI 读）")

    apy = sub.add_parser("apply", help="采纳建议，写进 card.json 的 habits")
    apy.add_argument("ip")
    apy.add_argument("--items", nargs="+", required=True)

    ar = ap.parse_args()
    try:
        if ar.cmd == "add":
            r = add(ar.ip, ar.kind, ar.reason, ar.set, ar.step)
            print("✓ 已记下：%s —— %s" % (r["kind"], r["reason"]))
        elif ar.cmd == "list":
            rows = load(ar.ip, ar.n)
            if not rows:
                print("（还没有记过任何返工教训）")
            for r in rows:
                print("%s  %-6s %s%s" % (r.get("at", ""), r.get("kind", ""),
                                         r.get("reason", ""),
                                         "  （%s）" % r["set"] if r.get("set") else ""))
        elif ar.cmd == "digest":
            t = digest(ar.ip, ar.n)
            print(t or "（还没有记录，出稿时没有历史教训可参考）")
        elif ar.cmd == "habits":
            print(json.dumps(habits(ar.ip), ensure_ascii=False, indent=2))
        elif ar.cmd == "promote":
            r = promote(ar.ip, prefer=ar.prefer or None, force=ar.force)
            print(json.dumps(r, ensure_ascii=False) if ar.json
                  else json.dumps(r, ensure_ascii=False, indent=2))
        elif ar.cmd == "apply":
            n, bak = apply(ar.ip, ar.items)
            print("✓ 已采纳 %d 条进 card.json 的 habits%s"
                  % (n, ("（备份 %s）" % bak) if bak else ""))
    except Exception as ex:
        print("✗ %s" % ex, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
