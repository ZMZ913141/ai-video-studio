#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""引擎真源静态扫描：拦住「把某个 IP 写死进引擎」的代码。

为什么要有它
============
2026-10-31 珠珠整条片子用了主号的声音。根因不是逻辑写错，是一行常量：

    REF_NAME = "cb_ref.wav"          # 洁斌的缩写，所有 IP 共用
    if not os.path.exists(d):        # 已存在就跳过
        <把参考音转成这个文件>

主号先跑过一次 → 珠珠直接复用主号的参考音。**日志打印的还是珠珠的文件名**，
一路跑到交付都没人发现，最后靠人耳听出来。

同类问题以前也发生过（gen_still 的 cb_base.png、ip.json 默认值里留着上个客户的外貌）。
共同点是：**引擎本该 IP 中立，却混进了某个 IP 的私有标识**。

所以把它做成同步前的一道闸：templates/scripts/ 是唯一真源，
**这里出现任何一个 IP 的名字/别名/素材名，都不该被推到别的档案去**。

规则
====
R1  IP 私有标识（名字、别名、`cb_` 这类缩写），命中即阻断
R2  「只判存在就跳过」的写文件（下次换素材不生效）—— 必须带 mtime/md5 判断，命中即阻断
R3  固定落地文件名（*_NAME = "字面量"）—— 仅提示，人确认是否该按 IP/内容区分

用法
====
  python3 bin/scripts_lint.py                  # 扫引擎真源
  python3 bin/scripts_lint.py --json           # 给机器读
退出码：0 干净 / 1 有阻断项
"""
import argparse
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(ROOT, "templates", "scripts")
REGISTRY = "/cfu/ai视频档案库/registry.json"

# 允许出现的例外：极少数脚本本来就只服务一个 IP（应该搬出真源，见 R1 提示）
ALLOW = {
    # 文件名: {规则: 理由}
}

WRITE_CALL = re.compile(r"(shutil\.copy2|shutil\.copy|subprocess\.run\(|open\([^)]*['\"]w)")
FRESH = re.compile(r"(getmtime|md5|hashlib|st_mtime|sha[0-9]*|filecmp)")


def load_ips():
    """收集所有 IP 的名字/别名 + 它们的私有素材名。"""
    names, assets = set(), set()
    paths = []
    try:
        reg = json.load(open(REGISTRY, encoding="utf-8"))
        for it in (reg.get("ips") or []):
            if isinstance(it, dict) and it.get("path"):
                paths.append(it["path"])
    except Exception:
        pass
    if os.path.isdir("/cfu/ai视频档案库"):
        for n in os.listdir("/cfu/ai视频档案库"):
            p = os.path.join("/cfu/ai视频档案库", n)
            if os.path.isdir(p) and os.path.isfile(os.path.join(p, "ip.json")):
                paths.append(p)
    for p in sorted(set(paths)):
        try:
            d = json.load(open(os.path.join(p, "ip.json"), encoding="utf-8"))
        except Exception:
            continue
        for k in [d.get("ip"), d.get("display_name")] + list(d.get("aliases") or []):
            k = (k or "").strip()
            if len(k) >= 2:
                names.add(k)
        # 素材文件名里的辨识片段（如 洁斌_声音参考_8s.flac → 洁斌_）
        for sub in ("02_素材真值",):
            dd = os.path.join(p, sub)
            if os.path.isdir(dd):
                for f in os.listdir(dd):
                    head = re.split(r"[_（(]", f)[0]
                    if len(head) >= 2 and not head.isascii():
                        assets.add(head + "_")
    # 已知的历史缩写（这类最危险：不查名字查不出来）
    assets.update({"cb_", "yts_", "zhu_"})
    return sorted(names, key=len, reverse=True), sorted(assets, key=len, reverse=True)


def strip_noise(line):
    """去掉行尾注释，避免把解释文字当成代码（但保留字符串里的内容）。"""
    out, q, i = [], None, 0
    while i < len(line):
        c = line[i]
        if q:
            out.append(c)
            if c == q and (i == 0 or line[i - 1] != "\\"):
                q = None
        else:
            if c in "\"'":
                q = c
                out.append(c)
            elif c == "#":
                break
            else:
                out.append(c)
        i += 1
    return "".join(out)


def doc_lines(src_text):
    """用 AST 找出所有 docstring 占的行 —— 文档里提到 IP 名是正常的，
    那是在讲历史，不是把 IP 写进逻辑。靠 startswith('\"\"\"') 猜会把
    单行 docstring 和多行块搞错（第一版就误报了 substyle 的注释）。"""
    import ast
    out = set()
    try:
        tree = ast.parse(src_text)
    except SyntaxError:
        return out
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                b = body[0]
                out.update(range(b.lineno, (getattr(b, "end_lineno", b.lineno) or b.lineno) + 1))
    return out


# 「路径样式」：真的会被当文件/目录用，而不是一句说明文字
# ★ 只认「真的像文件路径」的字符串。第一版用了裸 "/"，结果提示语
#   ask("IP 名（例：甲 / 乙）") 里的分隔斜杠也被当成路径，误报一堆。
PATHY = re.compile(r"(/cfu/|/home/|/tmp/|/usr/|\.\./|\./|\.(png|jpe?g|wav|mp3|flac|mp4|ass|srt)\b)")


def scan_file(path, names, assets):
    hits = []
    with open(path, encoding="utf-8", errors="ignore") as f:
        raw = f.readlines()
    skip = doc_lines("".join(raw))
    code = [strip_noise(x) for x in raw]

    for i, ln in enumerate(code, 1):
        if i in skip:
            continue
        for kw in assets:
            if kw in ln:
                hits.append((i, "R1", "IP 私有素材标识 %r —— 引擎真源必须 IP 中立" % kw, ln.strip()[:90]))
        for nm in names:
            if nm and nm in ln:
                # 说明文字里提一句（print/提示语）不算串档风险，降为提示；
                # 真拿去拼路径/赋值才阻断 —— 误报多了人就会把闸门关掉。
                if PATHY.search(ln):
                    hits.append((i, "R1", "IP 名字 %r 被拼进路径/赋值" % nm, ln.strip()[:90]))
                else:
                    hits.append((i, "R3", "说明文字里提到 IP 名 %r（建议改中性措辞）" % nm,
                                 ln.strip()[:90]))
        # R2：if not os.path.exists(...) 后面几步里有写操作，但整段没有 mtime/md5
        if re.search(r"if\s+not\s+os\.path\.exists\(", ln):
            block = "\n".join(code[max(0, i - 1): i + 5])
            if WRITE_CALL.search(block) and not FRESH.search(block):
                hits.append((i, "R2", "「存在就跳过」的写文件：换了素材不生效（必须判 mtime/md5）",
                             ln.strip()[:90]))
        # R3：固定落地文件名
        # ★ 只认**纯字面量**：`REF_NAME = "ref_%s.wav" % _h` 是按内容哈希命名的正确写法，
        #   正则原来只看引号里有没有扩展名，把带格式化的模板也报了出来 ——
        #   假警告会让人习惯性忽略这一栏，真出问题时反而看不见。
        m = re.match(r"\s*([A-Z][A-Z0-9_]*_NAME)\s*=\s*[\"']([^\"']+\.(?:wav|mp3|png|jpg))[\"']\s*$", ln)
        if m and "%" not in m.group(2) and "{" not in m.group(2):
            hits.append((i, "R3", "%s 是字面量文件名 —— 多 IP 会互相覆盖（应按 IP/内容区分）" % m.group(1),
                         ln.strip()[:90]))
    return hits


def main():
    ap = argparse.ArgumentParser(description="引擎真源静态扫描（防 IP 串档）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dir", default=SRC_DIR)
    a = ap.parse_args()

    if not os.path.isdir(a.dir):
        print("✗ 目录不存在：%s" % a.dir, file=sys.stderr)
        return 1
    names, assets = load_ips()
    report, bad = {}, 0
    for fn in sorted(os.listdir(a.dir)):
        if not fn.endswith(".py"):
            continue
        hits = [h for h in scan_file(os.path.join(a.dir, fn), names, assets)
                if h[1] not in ALLOW.get(fn, {})]
        if hits:
            report[fn] = hits
            bad += sum(1 for h in hits if h[1] in ("R1", "R2"))

    if a.json:
        print(json.dumps({"ok": bad == 0, "files": report}, ensure_ascii=False, indent=2))
        return 0 if bad == 0 else 1

    print("引擎真源扫描  %s" % a.dir)
    print("已知 IP 名 %d 个 / 私有素材标识 %d 个" % (len(names), len(assets)))
    if not report:
        print("✅ 干净：没有任何 IP 私有标识，也没有「存在就跳过」的写文件")
        return 0
    for fn, hits in report.items():
        print("\n%s" % fn)
        for ln, rule, msg, code in hits:
            mark = "✗" if rule in ("R1", "R2") else "·"
            print("  %s %4d  [%s] %s" % (mark, ln, rule, msg))
            if code:
                print("          %s" % code)
    print("\n%s" % ("✗ 有阻断项（R1/R2），同步会被拒绝" if bad else "· 只有提示项（R3），可继续"))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
