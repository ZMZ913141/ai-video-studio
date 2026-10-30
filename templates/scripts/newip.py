#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""新建 IP 脚手架 —— 接新单子时第一步跑它

================================================================
管辖范围（2026-10-28 用户定）
================================================================
本体系只管 **本平台主号 + 其客户 IP**。
独立的合作生产线刻意不纳入 —— 它们有自己的目录树、规则文档与脚本。
跨体系只借算法，不借参数，也不共用档案卡。

================================================================
为什么要有它
================================================================
最早接第二个 IP 时，因为目录是手建的，出过两次错：
 ① 在主号的资产目录下建了新 IP 子目录 —— 把主号的重叠帧参数
    套到新 IP 身上（两家当时重叠帧不同）。事后才发现，删掉重建。
 ② 各 IP 目录结构不一致（早期手建用 00~07，标准树是 01~08），
    交叉参考脚本时路径永远对不上。

本脚本做四件事，把这类问题挡在建目录之前：
 1. **重名检查**：先查总档（IP总档.json）看这个 IP 是不是已经有工作区了
 2. **独立体系保护**：要建的地方属于独立生产线 → 直接拒绝
 3. **统一目录结构**：按 01~08 标准树建，不手搓
 4. **生成 ip.json 模板**：素材字段留 null，跑 ip_guard 会直接报「未登记」

⚠️ 为什么不再「扫 /cfu 全盘」：
   全盘扫描会把独立生产线也当成「本体系的 IP」报出来，
   进而诱导把建档文件放进别人的工作区。现在改成**总册登记制**：
   册子里的才是本体系的，册子外的明确标注 excluded，两者分开处理。

================================================================
用法
================================================================
    python newip.py                      # 交互式（推荐）
    python newip.py --name 某某品牌 --tag M --space 某某资产
    python newip.py --name 测试 --tag T --dry   # 只看会做什么，不落盘

================================================================
建完之后的必做清单（脚本会打印）
================================================================
  1. 录 8 秒干净人声 → 03_素材真值/<名>_声音参考_8s.flac
  2. 写逐字稿     → 03_素材真值/<名>_声音参考_逐字稿.txt
  3. 定起始静帧   → 03_素材真值/<名>_静帧_定版.png
  4. 回到 ip.json 把三处 null 填成真实路径
  5. 跑 ip_guard 自检，exit=0 才算出片
"""
import argparse
import json
import os
import re
import sys

# 标准目录树（所有 IP 对齐，跨 IP 参考时路径能对上）
TREE = {
    "01_人设与规范": None,
    "02_素材真值":   None,
    "03_生产脚本":   None,
    "04_验收工具":   None,
    "05_文案":       None,
    "06_音频":       None,
    "07_成片":       None,
    "08_过程":       None,
}
# 字体不再每 IP 建一份，也不建「全库共用」目录：
# 字体装在系统里（~/.local/share/fonts/），libass 直接走 fontconfig 就能拿到
# （2026-10-29 实测：不给 fontsdir 的输出与给字体目录逐字节一致，内存还少一半）。
# 换字体 = 改 ip.json 的 subtitle.font_* 名字，见 burn_ass.py 文件头。
SUB = {}

BASE_TEMPLATE = {
    "_schema": "ip-archive/1.0",
    "_说明": "新建 IP 模板。素材字段为 null 时，ip_guard 会拒绝出片（这是故意的）。",
    "_status": "素材待补",
    "ip": None,
    "display_name": None,
    "aliases": [],
    "_aliases_note": "口头叫法（人名/昵称/简称）。用户说话时用的常常不是目录名，没有别名就会搜不到已有工作区。",
    "root": ".",
    "space": None,
    "tag_prefix": None,
    "opus": {
        "word_in_name": "时长必须写进文件名，如 S02_不用再折腾_成片_39.9s.mp4",
        "delivery": ["mp4", "srt", "文案txt", "定板mp3"]
    },
    "voice": {
        "ref_audio": None,
        "ref_text_file": None,
        "ref_text": None,
        "_todo": "录 8 秒干净人声存为 flac，并配同句逐字稿（逐字、无标点）",
        "tts": {
            "instruction": None,
            "cfg_scale": 1.1, "temperature": 0.95, "top_k": 50, "top_p": 1.0,
            "repetition_penalty": 1.1, "depth_temperature": 0.90,
            "depth_top_k": 50, "depth_top_p": 1.0, "max_new_tokens": 1504,
            "warmup_frames": 0, "stitch_reference": "none",
            "decode_mode": "cuda_graphs", "seed": 42,
            "_todo": "这些是引擎中性起始值（不是为任何 IP 定过的），必须用本 IP 已知素材重新定板"
        }
    },
    "visual": {
        "still": None,
        "portrait": None,
        "_todo": "定起始静帧。字体是全库共用的，不用在这里配"
    },
    "video": {
        "orientation": None,
        "base_res": None,
        "overlap": None,
        "sampler": None,
        "second_pass": {"enabled": None, "model": None},
        # ★ 这三项**必须**按本 IP 定板。漏了不会报错，只会让片子悄悄继承上一个客户的设定：
        #   · gender      → 决定骨架里 he/his 的归一。漏填 = 全篇男性说法，
        #                   女性 IP 的提示词会被写成 "He ... his hair"，人还是那个人，动作描述全错。
        #   · prompt_vars → 决定「谁 / 在哪 / 穿什么 / 什么姿态」。
        #                   漏填 = 引擎中性默认（人物和场景交给 <Picture 1>，
        #                   但姿态、服装、环境描述都是通用版）。
        #   · posture / pose_verb / seated_where 三个尤其要显式写（站/坐/躺），
        #                   否则模型在段与段之间会漂姿势 —— 站姿 IP 实测会踩到。
        "gender": None,
        "prompt_vars": {},
        "_todo": ("全部按本 IP 实测重算。跨 IP 只借算法，不借参数。"
                  "gender 必填（male/female）；prompt_vars 至少写 posture / pose_verb / seated_where。")
    },
    "subtitle": {
        "base_res": None, "fs_cue": None, "fs_title": None, "outline": None,
        "_todo": "字号按本 IP 画幅定，不要套用别家的数字"
    },
    "rules": {
        "banned_words": [],
        "banned_tone": [],
        "_todo": "品牌红线（如面料红线、情绪红线）写在这里"
    },
    "known_traps": []
}

# 总档位置。刻意放在不属于任何单个 IP 的地方，
# 这样每个 IP 目录里的脚本副本都能找到同一份册子。
# 想换位置：export IP_REGISTRY=/path/to/registry.json
#
# ⚠️ 2026-10-28 改：原默认值指向旧资产目录。迁移到
#   ai-video-studio 后总册是项目根的 `registry.json`。旧的默认值有两个问题：
#     ① 旧目录清理后会变成死链，直接跑本脚本会往不存在的路径写；
#     ② 更隐蔽的：vstudio.py 通过 IP_REGISTRY 环境变量指路，但任何人**直接**
#        跑 newip.py（不经 CLI）时仍会写进旧册 → 两份总册分叉，新建的 IP 在
#        CLI 里查不到。（S05 建档前实测踩到过，旧册被写入脏数据。）
#   现在默认值直接指向总册，按本文件位置反推（<档案根目录>/<IP>/03_生产脚本/ → 上三层）。
_HERE = os.path.dirname(os.path.abspath(__file__))
REGISTRY = os.environ.get("IP_REGISTRY") or os.path.normpath(
    os.path.join(_HERE, "..", "..", "..", "registry.json")
)


def _norm(s):
    return re.sub(r"[\s_\-·]", "", s or "").lower()


def _read_registry():
    """读总档。读不到 = 册子丢了，必须明确报错。

    ⚠️ 「册子丢了」和「册子是空的」必须分清。
       册子丢了还照常建目录 → 重名检查等于没做 → 迟早又出现重复工作区。
       （册子真的空是合法的，那时 ips 为空列表，不是 None。）
    """
    if not os.path.isfile(REGISTRY):
        return None
    try:
        with open(REGISTRY, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"\n✗ 总档读不出来：{REGISTRY}\n  {e}")
        return None


def _match(name, entry):
    """名字 / 别名 / 目录名 三选一命中。子串互含（处理含别名的全称）。"""
    key = _norm(name)
    if not key:
        return False
    cands = {_norm(entry.get("name")),
             _norm(os.path.basename(entry.get("path") or ""))}
    for a in (entry.get("aliases") or []):
        cands.add(_norm(a))
    cands.discard("")
    return any(key in c or c in key for c in cands)


def scan_existing(name):
    """查总档，返回 (本体系已有, 独立体系)。

    ★ 两者必须分开返回，因为处置完全相反：
        本体系已有 → 「别新建，进那个目录改 ip.json」
        独立体系   → 「别碰，它不归这套体系管」
      混在一份清单里报出来，会让人以为独立 IP 也是本体系的，
      进而把建档文件放进别人的工作区 —— 2026-10-27 我真的这么做了，
      用户 2026-10-28 让我撤回。
    """
    reg = _read_registry()
    if reg is None:
        print(f"\n✗ 找不到总档：{REGISTRY}")
        print("  没有总档就做不了重名检查，而重名检查正是本脚本存在的理由。")
        print("  修好总档路径，或 export IP_REGISTRY=... 指过去。")
        sys.exit(1)

    mine = [e for e in (reg.get("ips") or []) if _match(name, e)]
    outside = [e for e in (reg.get("excluded") or []) if _match(name, e)]
    return mine, outside


def path_in_excluded(target):
    """目标路径是否落在独立体系的工作区内。落在就地拒绝，不建。"""
    reg = _read_registry() or {}
    t = os.path.normpath(os.path.abspath(target)) + os.sep
    for e in (reg.get("excluded") or []):
        base = os.path.normpath(e.get("path") or "")
        if base and t.startswith(os.path.normpath(base) + os.sep):
            return e
    return None


def registry_add(entry):
    """建完登记进总册。登记失败只警告，不影响目录本身（目录已经建好了）。"""
    reg = _read_registry()
    if reg is None:
        print(f"  ⚠ 总档不在，本次未登记。请手动把 {entry['path']} 记进总档。")
        return False
    reg.setdefault("ips", []).append(entry)
    try:
        with open(REGISTRY, "w", encoding="utf-8") as f:
            json.dump(reg, f, ensure_ascii=False, indent=2)
        return True
    except OSError as e:
        print(f"  ⚠ 登记进总档失败：{e}")
        return False


def ask(prompt, default=None):
    suffix = f"（默认 {default}）" if default else ""
    v = input(f"  {prompt}{suffix}: ").strip()
    return v or default


def slugify(s):
    """把中文/空格转成安全目录名，中文保留（便于辨认）。"""
    return re.sub(r"[\\/:*?\"<>|]", "", s).strip()


def build(parent, name, tag, space, dry=False, aliases=None):
    target = os.path.join(parent, slugify(name))
    if os.path.exists(target):
        print(f"\n✗ 目录已存在：{target}")
        print("  如果这是同一个 IP，直接进那个目录改 ip.json，不要新建。")
        return 1

    # ★ 独立体系保护：别把建档文件放进别人的工作区。
    ex = path_in_excluded(target)
    if ex:
        print(f"\n✗ 不能建在 {target}")
        print(f"  那里是「{ex.get('name')}」的工作区，属于独立体系。")
        print(f"  原因：{ex.get('reason', '不纳入本建档体系')}")
        print("  这类独立 IP 有自己的目录树和规则文档，本体系不纳管、不放文件。")
        return 1

    print(f"\n将创建：{target}")
    if dry:
        print("  [DRY] 目录：")
        for d in TREE:
            print(f"    {d}/")
        print("  [DRY] 文件：ip.json、README.md、ip_guard.py（副本）")
        return 0

    for d in TREE:
        os.makedirs(os.path.join(target, d), exist_ok=True)
        for s in SUB.get(d, []):
            os.makedirs(os.path.join(target, d, s), exist_ok=True)

    # ip.json
    data = json.loads(json.dumps(BASE_TEMPLATE))
    data["ip"] = name
    data["space"] = space or name
    data["tag_prefix"] = tag
    data["aliases"] = aliases or []
    with open(os.path.join(target, "ip.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    # ip_guard.py 副本（每个 IP 自带一份，保证路径自洽）
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ip_guard.py")
    if os.path.isfile(src):
        import shutil
        shutil.copy(src, os.path.join(target, "03_生产脚本", "ip_guard.py"))

    # README
    with open(os.path.join(target, "README.md"), "w", encoding="utf-8") as f:
        f.write(f"""# {name} · 目录说明

一条视频 = 一套（{tag}01、{tag}02……）。每套自己的文案、音频、成片、过程各自独立命名。

| 目录 | 放什么 |
|---|---|
| `01_人设与规范` | 人设/品牌红线、背景方案 |
| `02_素材真值` | 形象底图、声音参考 flac+逐字稿、定版静帧（字体全库共用） |
| `03_生产脚本` | 出静帧 / TTS / 视频 / 配乐 / 混音 / 收尾 |
| `04_验收工具` | 接缝、油光、音轨、安全区 检查 |
| `05_文案` | 每套一个 `{tag}xx_名字/` |
| `06_音频` | 每套一个 `{tag}xx_名字/` |
| `07_成片` | 每套一个 `{tag}xx_名字/`，含制作记录 |
| `08_过程` | 每套一个 `{tag}xx_名字/`，未调色原片、抽帧 |

## 命名
时长一律写进文件名：`{tag}02_某某_成片_39.9s.mp4`

## 空间
{space or name}
""")

    print(f"\n✓ 已创建 {target}")

    # 登记进总册 —— 否则下次建同名 IP 时查不到，重名检查就失效了
    if registry_add({"name": name, "path": target, "tag": tag,
                     "space": space or name, "aliases": aliases or []}):
        print(f"  已登记进总档：{REGISTRY}")

    print("\n" + "=" * 56)
    print("接下来必做（顺序不能变）")
    print("=" * 56)
    print(f"  1. 录 8 秒干净人声 → 02_素材真值/{name}_声音参考_8s.flac")
    print(f"  2. 写逐字稿       → 02_素材真值/{name}_声音参考_逐字稿.txt")
    print(f"  3. 定起始静帧     → 02_素材真值/{name}_静帧_定版.png")
    print(f"  4. 编辑 {target}/ip.json，把上面 3 处 null 填成真实路径")
    print(f"  5. 自检：python {target}/03_生产脚本/ip_guard.py")
    print("     exit=0 才算出片；exit=1 会明确告诉你缺哪几项\n")
    return 0


def main():
    ap = argparse.ArgumentParser(description="新建 IP 工作区")
    ap.add_argument("--name", help="IP 名（如：某某 / 某某品牌）")
    ap.add_argument("--tag", help="套次前缀（如 S / Z / M）")
    ap.add_argument("--space", help="资料库空间名")
    ap.add_argument("--parent", default="/cfu", help="建在哪（默认 /cfu）")
    ap.add_argument("--aliases", help="别名，逗号分隔（口头叫法）")
    ap.add_argument("--dry", action="store_true", help="只打印不落盘")
    a = ap.parse_args()

    name = a.name
    if not name:
        print("\n新建 IP 工作区\n")
        name = ask("IP 名（例：某某 / 某某品牌）")
    if not name:
        print("✗ 必须有 IP 名")
        return 1

    # ★ 先查总档，避免在不同 IP 资产目录下又建出本该在另一个体系的 IP
    mine, outside = scan_existing(name)

    # ① 独立体系：直接拒绝，问都不问
    if outside:
        print(f"\n✗ 「{name}」属于独立体系，不在本建档范围内：")
        for e in outside:
            print(f"    {e.get('name')}  →  {e.get('path')}")
            print(f"      {e.get('reason', '')}")
        print("\n  它有自己的目录树和规则文档。跨体系只借算法，不借参数，不共用档案卡。")
        return 1

    # ② 本体系已有：别新建，去改那一份
    if mine:
        print(f"\n⚠ 本体系已有 {len(mine)} 个同名/近名 IP：")
        for e in mine:
            print(f"    {e.get('name')}  →  {e.get('path')}  （套次 {e.get('tag')}）")
        print("\n  直接进那个目录改 ip.json，不要新建。")
        print("  重复目录会串参数 —— 2026-10-27 踩过一次，把上一个 IP 的重叠帧参数套到了别家。")
        if a.name:  # 非交互模式：直接退出
            return 1
        if ask("确认仍要新建？输入 yes 继续", "").lower() != "yes":
            print("已取消")
            return 1

    tag = a.tag or (ask("套次前缀（1 个大写字母，如 S/Z/M）") if not a.dry else "X")
    space = a.space or (ask("空间名（可留空）", "") if not a.dry else "")
    if tag and (len(tag) != 1 or not tag.isalpha()):
        print(f"✗ 套次前缀必须是 1 个字母，收到：{tag!r}")
        return 1

    aliases = [x.strip() for x in (a.aliases or "").split(",") if x.strip()]
    if not aliases and not a.dry and not a.name:
        # 交互模式才问。别名是防重复的关键：口头叫的常常是人名，目录名是品牌名。
        raw = ask("别名/口头叫法（逗号分隔，可留空）", "")
        aliases = [x.strip() for x in raw.split(",") if x.strip()]

    return build(a.parent, name, tag.upper() if tag else tag, space,
                 dry=a.dry, aliases=aliases)


if __name__ == "__main__":
    sys.exit(main())
