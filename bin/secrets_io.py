#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地密钥的唯一读取入口。

为什么要单开一个小文件，而不是到处 os.environ.get 或者直接 open json：
--------------------------------
密钥只有一个真源（`config/secrets.local.json`，不进 git）。散着读的话，
哪天换了文件名、或者有人把 key 硬编码进某个脚本，就没有一处能兜住。
这里统一收口，顺带保证「读不到就说清楚，不静默当成没配」。

用法
----
    from secrets_io import get
    key = get("pexels")             # 没有就返回 ""，并在 stderr 说一句

    python3 bin/secrets_io.py list      # 看有哪些键（只显示前 6 位）
    python3 bin/secrets_io.py get pexels

★ 关于 Pexels（2026-10-30 实测，别再试第二遍）
  它的 API 只有 **图片和视频** 两类，没有音乐端点：/v1/music、/v1/audio
  之类一律 404。所以它**不能**当 BGM 源；能用来搜 B-roll / 配图。
  配乐要在线曲库请用 Jamendo / Freesound（make_bgm.py --source online）。
"""
import json
import os
import sys

# ★ 文件名刻意叫 secrets_io 而不是 secrets：bin/ 会进 sys.path，
#   叫 secrets.py 会把标准库的 secrets（token 生成）顶掉，报错还很难查。

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "config", "secrets.local.json")


def load():
    if not os.path.isfile(PATH):
        return {}
    try:
        with open(PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as ex:
        print(f"⚠ 密钥文件读不了（{PATH}）：{ex}", file=sys.stderr)
        return {}


def get(name, default=""):
    """取一个密钥。空值和「没有这个键」都返回 default。"""
    v = load().get(name)
    if v is None or (isinstance(v, str) and not v.strip()):
        return default
    return v


def _mask(s):
    s = str(s or "")
    return (s[:6] + "…" + str(len(s)) + "位") if s else "（空）"


def main():
    a = sys.argv[1:]
    if not a or a[0] in ("list", "ls"):
        d = load()
        keys = [k for k in d if not k.startswith("_")]
        if not keys:
            print(f"还没有密钥：{PATH}\n复制 config/secrets.example.json 过来再填。")
            return
        print(f"密钥文件：{PATH}")
        for k in sorted(keys):
            print(f"  {k:12s} {_mask(d.get(k))}")
        return
    if a[0] == "get" and len(a) > 1:
        v = get(a[1])
        if not v:
            print(f"没有 {a[1]}", file=sys.stderr)
            sys.exit(1)
        print(v)
        return
    print(__doc__)
    sys.exit(2)


if __name__ == "__main__":
    main()
