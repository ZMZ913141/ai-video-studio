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
    ② 提炼：攒够一定条数（或手动触发）后，把同类教训归纳成一句「习惯」，
       写进 card.json 的 habits —— 这一步可以让模型做，也可以人自己写。
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
    python3 bin/learning.py habits <IP>          # 建议归纳成哪几条习惯
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime

BIN_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BIN_DIR)
sys.path.insert(0, BIN_DIR)

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


def habits(ip):
    """卡片里已有的习惯 + 学习记录里反复出现的坑。

    这是「提炼」那一半：把流水里同类问题归并，给出建议写进 card.json 的条目。
    """
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
    return {"have": have, "tally": {k: len(v) for k, v in tally.items()},
            "samples": {k: v[:3] for k, v in tally.items()}}


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

    h = sub.add_parser("habits", help="建议归纳成哪几条习惯")
    h.add_argument("ip")

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
    except Exception as ex:
        print("✗ %s" % ex, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
