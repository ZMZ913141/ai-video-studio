#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多 IP 隔离护栏 —— 用**路径**而不是环境变量决定「这是谁的片子」

================================================================
为什么要有这个模块
================================================================
2026-10-27 之前，各脚本自己写一遍「找参考音」的逻辑：

    def _find_ref():
        env = os.environ.get("REF_SRC")
        if env: return env
        return glob("../02_素材真值/*声音参考*.flac")   # ← 靠 pwd

问题有三：
 ① 环境变量忘了设 → 静默用上一个 IP 的素材。**不报错**，出片后才发现脸和声音都不对。
 ② 每套脚本各写一遍，逻辑迟早不一致。
 ③ glob 依赖当前工作目录，在错的目录里跑就指向错的 IP。

本模块把真值收进每个 IP 根目录的 `ip.json`，**由脚本自身路径向上查找**决定 IP。

    from ip_guard import load_ip
    ip = load_ip()                    # 自动识别所属 IP + 校验素材
    ref = ip.abspath(ip.tts.ref_audio)  # 绝对路径，不存在已经在这之前退出了

用法见 `newip.py`（新建 IP）与 `多IP协作规范.md`。

================================================================
三条硬红线（违反直接 sys.exit，绝不静默退回别的素材）
================================================================
 1. 找不到 ip.json       → 退出。说明你在不属于任何 IP 的目录里。
 2. ip.json 里的素材不存在 → 退出。**不许拿别 IP 的顶替**（这是最危险的静默失败）。
 3. 环境变量覆盖了档案值  → 打印醒目标记，但**不允许跨 IP 借素材**
    （即 <IP_ROOT>/02_素材真值 之外的路径一律拒绝）。
"""
import json
import os
import sys


# ============================================================ 数据结构
class IP:
    """一份 IP 档案。属性访问带默认值，缺字段不会 KeyError。"""

    def __init__(self, data, root, path):
        self._d = data
        self.root = root          # IP 工作区根目录（绝对路径）
        self.path = path          # ip.json 自身路径

    # ---- 顶层字段，缺失返回 None / 默认值 ----
    def __getattr__(self, k):
        if k.startswith("_"):
            raise AttributeError(k)
        v = self._d.get(k)
        if isinstance(v, dict):
            return _NS(v)
        return v

    def get(self, key, default=None):
        cur = self._d
        for part in key.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    # ---- 素材绝对路径 ----
    def abspath(self, rel):
        """把 ip.json 里的相对路径解析成绝对路径。"""
        if not rel:
            return None
        if os.path.isabs(rel):
            return rel
        return os.path.normpath(os.path.join(self.root, rel))

    def asset(self, *chain):
        """取素材绝对路径。`asset("tts","ref_audio")` 等价于先 get 再 abspath。"""
        return self.abspath(self.get(".".join(chain)))

    def __repr__(self):
        return f"<IP {self.get('ip','?')} root={self.root}>"


class _NS:
    """让 `ip.tts.ref_audio` 这种链式访问可用，缺 key 返回 None。"""

    def __init__(self, d):
        self._d = d

    def __getattr__(self, k):
        if k.startswith("_"):
            raise AttributeError(k)
        v = self._d.get(k)
        if isinstance(v, dict):
            return _NS(v)
        return v


# ============================================================ 查找
def find_ip_json(start=None):
    """从 `start`（默认：调用方文件路径）向上逐级找 ip.json。"""
    d = start or os.path.dirname(os.path.abspath(sys.argv[0]))
    d = os.path.abspath(d)
    while True:
        cand = os.path.join(d, "ip.json")
        if os.path.isfile(cand):
            return cand, d
        parent = os.path.dirname(d)
        if parent == d:                      # 到根目录了
            return None, None
        d = parent


def _looks_like_ip_root(d):
    return os.path.isabs(d) and os.path.isfile(os.path.join(d, "ip.json"))


# ============================================================ 校验
def check_assets(ip, strict=True, verbose=True):
    """校验 ip.json 里登记的素材是否真实存在。

    ★ 这是防止 IP 混用的最后一道闸：档案里写的文件若不存在，
      **绝不退回默认名 / 绝不 glob 兜底**，直接退出。
      （2026-10-27 红线：静默退回别人的素材是最严重的错误）
    """
    missing = []      # 路径写了但文件不存在  → 硬错
    unlisted = []     # 字段为 null / 空       → 未登记，同样拦
    checked = []

    # (组, 字段名, 是否必需)
    spec = (
        ("voice",  "ref_audio",     True),
        ("voice",  "ref_text_file", False),
        ("visual", "still",         True),
        ("visual", "portrait",      False),
    )

    for group, key, required in spec:
        g = ip._d.get(group)
        if not isinstance(g, dict) or key not in g:
            if required:
                unlisted.append(f"{group}.{key}")
            continue
        rel = g[key]
        # ★★ 关键：null 不能静默跳过。2026-10-28 踩到：雅太社档案里
        #    ref_audio/still 写的是 null，旧的 `if not rel: continue`
        #    直接跳过 → 自检输出「✓ 档案加载完成」，看起来一切正常，
        #    实际这个 IP 根本没法出片。**这正是要防的静默失败**。
        if rel is None or (isinstance(rel, str) and not rel.strip()):
            unlisted.append(f"{group}.{key}")
            continue
        abs_p = ip.abspath(rel)
        checked.append((f"{group}.{key}", abs_p))
        if not os.path.exists(abs_p):
            missing.append((f"{group}.{key}", abs_p))

    if verbose:
        _dn = ip.get('display_name') or ''
        if _dn.startswith(ip.get('ip', '') + '（'):
            _dn = ''                       # 显示名已含 IP 名时不再重复
        print(f"  IP        {ip.get('ip', '?')}" + (f"（{_dn}）" if _dn else ""))
        print(f"  档案      {ip.path}")
        if ip.get("space"):
            print(f"  空间      {ip.get('space')}")
        if ip.get("tag_prefix"):
            print(f"  套次前缀  {ip.get('tag_prefix')}")
        for name, p in checked:
            mark = "✓" if os.path.exists(p) else "✗"
            print(f"  {mark} {name:<22} {os.path.basename(p)}")
        for name in unlisted:
            print(f"  ？ {name:<22} 未登记（null）")

    problem = missing + unlisted
    if problem and strict:
        print(f"\n✗ 该 IP 素材未就绪（{len(problem)} 项），拒绝继续：", file=sys.stderr)
        for name, p in missing:
            print(f"    [文件不存在] {name}: {p}", file=sys.stderr)
        for name in unlisted:
            print(f"    [尚未登记]   {name} 为 null", file=sys.stderr)
        print("\n  在 ip.json 里填上真实路径，或把素材放进 02_素材真值/。", file=sys.stderr)
        print("  ⛔ 不会退回别的 IP 的素材来顶替 —— 静默顶替是最严重的错误。\n",
              file=sys.stderr)
        sys.exit(2)

    return problem


# ============================================================ 环境变量覆盖
def apply_env_overrides(ip, verbose=True):
    """环境变量只许**微调本 IP 内部**的值，不许跨 IP 借素材。

    合法的：本 IP 目录内的另一个文件（比如换一张备选静帧）
    非法的：指向别的 IP 工作区 → 拒绝
    """
    mapping = {
        "REF_SRC":  ("voice", "ref_audio"),
        "REF_TEXT": ("voice", "ref_text"),
        "STILL":    ("visual", "still"),
    }
    for env, (group, key) in mapping.items():
        v = os.environ.get(env)
        if not v:
            continue
        abs_v = os.path.abspath(v)
        # ★ 红线：不许跨 IP 借素材
        if not abs_v.startswith(ip.root + os.sep) and abs_v != ip.root:
            print(f"\n✗ {env} 指向了本 IP 之外：{abs_v}", file=sys.stderr)
            print(f"  本 IP 根目录：{ip.root}", file=sys.stderr)
            print("  跨 IP 借用素材是被禁止的（这正是 IP 混用的源头）。\n",
                  file=sys.stderr)
            sys.exit(2)
        ip._d.setdefault(group, {})[key] = os.path.relpath(abs_v, ip.root)
        if verbose:
            print(f"  ⚠ {env} 覆盖 {group}.{key} → {v}")

    return ip


# ============================================================ 主入口
def load_ip(strict=True, verbose=True, allow_env=True, start=None):
    """加载当前脚本所属 IP 的档案并校验素材。

    放在每个生产脚本开头：
        from ip_guard import load_ip
        ip = load_ip()

    `start` 用于自检时指定起点目录（不传则用调用方脚本自身路径）。
    """
    path, root = find_ip_json(start)
    if path is None:
        print("\n✗ 找不到 ip.json。", file=sys.stderr)
        print(f"  当前起点：{os.path.dirname(os.path.abspath(sys.argv[0]))}",
              file=sys.stderr)
        print("  请确认脚本位于某个 IP 的工作区内：", file=sys.stderr)
        print("    档案根目录（默认 /cfu/ai视频档案库，可由环境变量 STUDIO_ARCHIVE 改）",
              file=sys.stderr)
        print("    下面应该是 <档案根目录>/<IP名>/03_生产脚本/。", file=sys.stderr)
        print("  新建 IP 请用：python newip.py\n", file=sys.stderr)
        sys.exit(2)

    with open(path, encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            sys.exit(f"✗ ip.json 格式错误：{path}\n  {e}")

    data.setdefault("ip", os.path.basename(root))
    ip = IP(data, root, path)

    if allow_env:
        apply_env_overrides(ip, verbose=verbose)

    if verbose:
        print("\n" + "─" * 60)
    check_assets(ip, strict=strict, verbose=verbose)
    if verbose:
        print("─" * 60 + "\n")

    return ip


def forbid(words, text, where="文本"):
    """通用禁词检查：命中即退出。用于面料红线 / 否定词红线。"""
    hits = [w for w in words if w in text]
    if hits:
        print(f"\n✗ {where}命中禁用词：{hits}", file=sys.stderr)
        sys.exit(2)
    return True


if __name__ == "__main__":
    # 自检模式：python ip_guard.py [目录]
    # 用于开工前确认「这个 IP 能不能出片」，退出码可直接被 shell 判断。
    ip = load_ip(
        start=os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else None,
        strict=False, verbose=True)
    problems = check_assets(ip, strict=False, verbose=False)
    if problems:
        print(f"✗ {ip.get('ip', '?')} 素材未就绪（{len(problems)} 项），不能出片")
        sys.exit(1)
    print(f"✓ {ip.get('ip', '?')} 素材齐备，可以出片")
    sys.exit(0)
