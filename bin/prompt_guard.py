#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
prompt_guard —— 提示词红线校验（引擎侧）

只有一条铁律：**AI 生成的提示词必须先过这里，才准落盘。**

为什么需要它
============
2026-10-27 用户定板：H3 的提示词里禁用负向约束（no / not / without …），
一律改成正向描述。原因是否定式在 H3 上不可靠，写了常常没用，偶尔还会反向。
但 LLM 写提示词的本能就是「不要 xxx」——它不知道这条红线。
所以必须有一个不看情面的校验层：不管提示词是人写的还是 AI 写的，
过不了就是不落盘，而不是「先存下来再说」。

用法:
  prompt_guard.py "提示词文本" [--ip /path/to/ip.json] [--json]
  echo $?   # 0=通过  1=违规

作为库:
  from prompt_guard import check
  ok, issues = check(text, GuardRules.from_files())
"""
import json
import os
import re
import sys

BIN_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BIN_DIR)
DEFAULT_CFG = os.path.join(ROOT, "config", "pipeline.json")


class GuardRules:
    """红线规则：配置层（全局）+ IP 层（ip.json）合成。"""

    def __init__(self, cfg=None, ip=None):
        cfg = cfg or {}
        g = cfg.get("guard", {})
        self.neg_en = [w.lower() for w in g.get("negative_words_en", [])]
        self.neg_cn = list(g.get("negative_words_cn", []))
        self.require_if = g.get("require_if", {})
        self.max_chars = int(g.get("max_chars", 700))
        self.banned = []
        self.ip_rules = (ip or {}).get("rules", {}) if isinstance(ip, dict) else {}
        if self.ip_rules.get("banned_words"):
            self.banned = list(self.ip_rules["banned_words"])

    @classmethod
    def from_files(cls, cfg_path=None, ip_path=None):
        cfg = {}
        p = cfg_path or DEFAULT_CFG
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                cfg = json.load(f)
        ip = None
        if ip_path and os.path.exists(ip_path):
            with open(ip_path, encoding="utf-8") as f:
                ip = json.load(f)
        return cls(cfg, ip)


def check(text, rules=None, where=""):
    """返回 (ok, issues)。issues 是给人看的中文说明，可直接打印。"""
    rules = rules or GuardRules()
    issues = []
    t = (text or "").strip()
    low = " " + re.sub(r"[^0-9a-zA-Z一-鿿]+", " ", t.lower()) + " "

    if not t:
        return False, ["提示词为空"]

    # ① 负向约束（红线）：中英文都查
    if rules.ip_rules.get("negative_free"):
        for w in rules.neg_en:
            if w.strip() and re.search(r"\b" + re.escape(w.strip()) + r"\b", low):
                issues.append(f"否定式（红线）：出现 “{w.strip()}” —— 必须改成正向描述")
        for w in rules.neg_cn:
            if w and w in t:
                issues.append(f"否定式（红线）：出现 “{w}” —— 必须改成正向描述")

    # ② IP 禁用词
    for w in rules.banned:
        if w and w in t:
            issues.append(f"禁用词：出现 “{w}”")

    # ③ 正向必备项（ip.json 开了哪条规则，就要有对应的正向描述）
    for key, spec in (rules.require_if or {}).items():
        if not isinstance(spec, dict) or key.startswith("_"):
            continue
        if rules.ip_rules.get(key):
            words = spec.get("words", [])
            if words and not any(w in t for w in words):
                issues.append(spec.get("hint", f"{key} 已开启，提示词缺少对应正向描述"))

    # ④ 长度：太长的提示词 H3 会顾此失彼
    if len(t) > rules.max_chars:
        issues.append(f"过长：{len(t)} 字 > 上限 {rules.max_chars}")

    return (len(issues) == 0), issues


def main():
    args = [a for a in sys.argv[1:]]
    flags = {k: v for k, v in zip(args, args[1:])} if args else {}
    text = next((a for a in args if not a.startswith("--") and a != flags.get("--ip")), None)
    if text is None:
        raise SystemExit(__doc__)
    ip_path = flags.get("--ip")
    rules = GuardRules.from_files(ip_path=ip_path)
    ok, issues = check(text, rules)
    if "--json" in args:
        print(json.dumps({"ok": ok, "issues": issues}, ensure_ascii=False, indent=2))
    elif ok:
        print("✓ 通过红线")
    else:
        print("✗ 未通过红线：")
        for s in issues:
            print("   -", s)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
