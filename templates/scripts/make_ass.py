#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 SRT 生成剪映质感的 ASS 字幕（含开场标题、关键词花字、结尾 CTA）。

设计逻辑（针对「让准客户心动并行动」，不是简单上字幕）
==================================================
1. **开场 2.6s 放钩子标题**：前 3 秒决定 90% 的留存。标题压在画面上部空白区
   （y 6%–19% 无人脸、亮度 92–113 干净），不挡脸。
2. **关键词花字**：全片只有 4–6 处，挑真正能戳人的词（数字、对比、痛点），
   变色 + 微放大。全篇都花等于没有花。
3. **结尾 CTA 加强**：「私信两个字，分身」是转化动作，字号放大到 58 +
   橙色描边 + 缓慢呼吸，比正文更抢眼。
4. **安全区**：44px 字号 / 12 字上限，居中 x120–648，避开右侧点赞列 x661。
   底边 y=1088，避开底部用户名区 y>1118。

时间轴一律沿用已验收的 SRT（0 问题条），只做视觉增强，不动时间。
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import substyle as S


# ---------------- SRT 解析 ----------------

def parse_srt(path):
    txt = open(path, encoding="utf-8-sig").read()
    out = []
    for m in re.finditer(
            r"(\d+)\n(\d\d):(\d\d):(\d\d),(\d+) --> (\d\d):(\d\d):(\d\d),(\d+)\n(.*?)(?:\n\n|$)",
            txt, re.S):
        g = m.groups()
        t0 = int(g[1]) * 3600 + int(g[2]) * 60 + int(g[3]) + int(g[4]) / 1000
        t1 = int(g[5]) * 3600 + int(g[6]) * 60 + int(g[7]) + int(g[8]) / 1000
        out.append({"t0": t0, "t1": t1, "text": g[9].strip()})
    return out


# ---------------- 每套的「心动设计」 ----------------
# 结构：hook 钩子标题 / subs 副标题 / hl 花字关键词 / cta 结尾行动句
# 花字的 key 必须能在该条字幕里精确匹配到（不含标点），否则静默跳过并告警。
DESIGN = {
    "S01": {
        "hook": ("全是AI做的", "从长相到声音"),
        # 钩子标题/副标的金色锚点词：剪映模板的钩子从不整块同色，
        # 一定有一两个词变色，把眼睛钉在那儿。只给 1 个，多了就散。
        "hl_title": ["AI"],
        "hl_sub": ["长相", "声音"],
        "hl": {
            "不存在": "数字/反差",
            "AI做出来": "身份揭露",
            "不敢出镜": "痛点",
            "好几千": "价格锚",
            "来回折腾": "痛点",
            "二十分钟": "数字承诺",
            "不用出镜": "核心利益",
            "分身": "产品词",
        },
        "cta_cues": [12],
    },
    "S02": {
        "hook": ("不用再折腾", "你只需要10秒录音"),
        "hl_title": ["折腾"],
        "hl_sub": ["10秒"],
        "hl": {
            "不会拍": "否定安抚",
            "换衣服化妆": "痛点具象",
            "十秒录音": "输入成本",
            "二十分钟": "数字承诺",
            "这真是我吗": "情绪爆点",
            "不用你再折腾": "核心利益",
            "没时间": "痛点",
            "录十秒": "输入成本",
            "分身": "产品词",
        },
        "cta_cues": [16],
    },
    "S03": {
        # 角度：社会认同 + 紧迫感（「隔壁已经在拍」比「AI 有多强」更能逼人行动）
        "hook": ("同行都在拍", "你不拍 顾客就是别人的"),
        "hl_title": ["同行"],
        "hl_sub": ["就是别人的"],
        # 花字 9 处。挑词标准同前：数字 / 对比 / 痛点具象 / 金句。
        # 10-12 是「一张照片 十秒录音 二十分钟一条」的并列三连，
        # 三处同时上色反而强化了排比，不是滥花。
        "hl": {
            "天天发视频": "痛点具象",
            "省事的办法": "核心利益",
            "比一遍": "决策动作",
            "拼出现": "金句",
            "一张照片": "输入成本",
            "十秒录音": "输入成本",
            "二十分钟": "数字承诺",
            "不用他出镜": "核心利益",
            "就是别人": "情绪爆点",
        },
        # 转化动作只在「私信两个字 分身」这一条出现。
        # ⚠️ 序号跟 SRT 走。make_srt_asr.py 重切后由 19 条变 20 条，
        #    「私信两个字 分身」从 cue17 移到 cue18。
        #    以后换 SRT 生成方式都要回来核对这一行，否则 CTA 加错条。
        "cta_cues": [18],
    },
    "S04": {
        # 角度：流程透明（人设圣经 §9 ④）。
        # 这条片子的钩子是「你说过很多次二十分钟，但从没说过里面装了什么」——
        # 所以标题不吹能力，直接报账：把 20 分钟拆成 5/10/5 三段摆出来。
        "hook": ("二十分钟 拆开看", "5 + 10 + 5"),
        "hl_title": ["二十分钟"],
        "hl_sub": ["拆开看"],
        # 花字 8 处。挑词标准：数字 / 对比 / 痛点具象 / 金句。
        # 这条片子的结构是「三个数字各领一段」，所以 5/10/5 三个时间词
        # 必须上色（它们是全片的骨架），其余只挑能戳人的动作词。
        # ⚠️ 不要给「二十分钟」上花字：它出现在 cue1 和 cue13，
        #    标题已经用了，正文再花会显得全片都在喊同一个词。
        "hl": {
            "前五分钟": "数字骨架",
            "听你说话": "反常识动作",
            "中间十分钟": "数字骨架",
            "还像你": "核心利益",
            "最后五分钟": "数字骨架",
            "差半拍": "痛点具象",
            "不是点了下按钮": "对比否定",
            # ⚠️ 必须是「替你做完」不能是「替你做」：原句是「是我替你做完那些…」，
            #    用「替你做」会在「完」前面断开，把「做完」劈成「替你做|完」。
            "替你做完": "核心利益",
        },
        # 转化句「私信两个字 分身」在 cue16。
        # ⚠️ 这条 SRT 由 make_srt_asr.py 生成，共 18 条；
        #    若重切 SRT 必须回来核对，否则 CTA 会套在别的句子上。
        "cta_cues": [16],
    },
}


def strip_punct(s):
    return re.sub(r"[，。？！、；：,\.\?!;:\s]", "", s.replace("\\N", ""))


def norm_lines(s):
    """SRT 里的真实换行必须转成 ASS 的 \\N 转义。

    坑：SRT 多行 cue 读进来是**真实换行符**，直接写进 .ass 会被当成两行
    物理数据 —— libass 只解析第一行，第二行整句丢失（S02 第 3 条实测丢字）。
    """
    s = re.sub(r"\r\n?", "\n", s)
    s = re.sub(r"[ \t]*\n[ \t]*", "\\\\N", s)   # 去行首尾空格再换 \\N
    return s.strip()


def apply_hl(text, keys, color_only=False):
    """把关键词变色+微放大。text 里的 \\N 换行保留。

    color_only=True 时只变色不缩放 —— 给标题用，缩放由头部 \\t 统一控制。
    """
    hits = []
    plain = strip_punct(text)
    for k in keys:
        kk = strip_punct(k)
        if kk and kk in plain:
            hits.append(kk)
    if not hits:
        return text, []
    # 按长度倒序，避免短词先吃掉长词
    hits.sort(key=len, reverse=True)
    out, i, used = [], 0, []
    skip = set("，。？！、；：,.;: \t\n\\N!?")
    skip.discard("\\")
    while i < len(text):
        matched = None
        for k in hits:
            # 从 i 起，跳过标点/空格，凑够 len(k) 个实字看是否等于 k
            seg, j, idx = "", i, []
            while j < len(text) and len(seg) < len(k):
                ch = text[j]
                if text.startswith("\\N", j):
                    j += 2
                    continue
                if ch not in skip:
                    seg += ch
                    idx.append(j)
                j += 1
            if seg == k and idx:
                matched = (k, idx[0], j)
                break
        if matched:
            k, s, e = matched
            out.append(S.hlcolor(text[s:e]) if color_only else S.hl(text[s:e]))
            used.append(k)
            i = e
        else:
            out.append(text[i])
            i += 1
    return "".join(out), used


# ---------------- 没有人工 DESIGN 时的兜底 ----------------
# 2026-10-30 用户反馈「原本是有加高级版的标题的」。查出来是**结构问题**：
# DESIGN 是按 S01/S02 一套一套硬编码的字典，T01/T02 这种 `--topics` 现写文案的
# 临时套次根本不在里面 → d = {} → 开场标题 / 关键词花字 / 结尾 CTA 三样全落空，
# 整个 ASS 只剩单调的 Cue 一行行往下滚。批量出片越多，这个缺口越明显。
# 下面用规则从文案里长出一份，够不上人工的水准，但比「什么都没有」强得多。
NUM_UNIT = re.compile(
    r"[0-9一二三四五六七八九十百千万两]+"
    r"(?:条|秒|分钟|个钟头|个小时|小时|天|年|块|元|万|千|月|号|位|次|遍|倍|张|句|单)")

CTA_WORDS = ("私信", "评论", "主页", "关注", "下单", "回复", "留言", "找我", "戳")

# 标题里值得变色的锚点（钩子里没有数字时，至少有一个词是亮的）
TITLE_ANCHORS = ("其实", "不是", "为什么", "值不值", "别", "先", "千万不要", "劝")


def auto_design(cues):
    """从 SRT 反推一份「心动设计」。返回结构与 DESIGN 的条目一致。

    规则保守，宁少勿多：
    - 标题用第 1 句、副标用第 2 句 —— 短视频的钩子本来就是这两句，
      强行另行提炼只会写出不搭的句子。
    - 花字只挑**数字+量词**（剪映模板的标配），全片 4~6 处，不碰形容词。
    - CTA 找含转化动词的那一条。
    """
    texts = [c["text"] for c in cues]
    if not texts:
        return {}

    def cut(s, n):
        s = strip_punct(s)
        return s if len(s) <= n else s[:n]

    main = cut(texts[0], 10)
    sub = cut(texts[1], 14) if len(texts) > 1 and texts[1] != texts[0] else ""

    hl, seen = {}, set()
    for t in texts:
        # 每条只挑**一个**（最长的那个）：一条里给两个词上色，眼睛不知道看哪，
        # 等于没有花字。实测 T01「拍一条视频三个钟头」会同时命中「一条」和
        # 「三个钟头」，染成两段，读起来像乱码。
        best = None
        for m in NUM_UNIT.finditer(t):
            w = m.group(0)
            if len(w) < 2 or w in seen:
                continue
            # 「一条」「一个」「一次」这类没信息量，跳过；「一个月」「十五秒」留着
            if w[0] in "1一" and len(w) <= 2:
                continue
            if best is None or len(w) > len(best):
                best = w
        if best:
            hl[best] = "数字"
            seen.add(best)
        if len(hl) >= 6:
            break

    cta = [i for i, t in enumerate(texts, 1) if any(w in t for w in CTA_WORDS)]
    d = {"hl": hl, "cta_cues": cta[:1]}
    if main:
        d["hook"] = (main, sub)
        anchors = [a for a in TITLE_ANCHORS if a in main]
        if anchors:
            d["hl_title"] = [anchors[0]]
        if sub:
            sub_anchors = [a for a in TITLE_ANCHORS if a in sub]
            if sub_anchors:
                d["hl_sub"] = [sub_anchors[0]]
    return d


def build(tag, srt_path, out_ass, dur=None):
    cues = parse_srt(srt_path)
    d = DESIGN.get(tag) or {}
    auto = {}
    if not d.get("hook"):
        # 没人工设计（或人工漏了 hook）→ 用规则兜底，并在日志里说清楚，
        # 免得以后有人以为「字幕本来就没标题」。
        auto = auto_design(cues)
        d = {**auto, **d}
        if auto.get("hook"):
            print(f"  · {tag} 没有人工 DESIGN → 自动生成钩子标题「{auto['hook'][0]}」"
                  f"、花字 {len(auto.get('hl') or {})} 处、CTA {len(auto.get('cta_cues') or [])} 条",
                  file=sys.stderr)
    ev = []
    total = dur or (cues[-1]["t1"] + 0.4)

    # ---- 1. 开场钩子（前 2.6s，压在画面上部空白区）----
    hook_txt = set()
    if d.get("hook"):
        main, sub = d["hook"]
        m2, _ = apply_hl(main, d.get("hl_title", []), color_only=True)
        ev.append(S.dialog(2, 0.0, min(2.6, total), "Title",
                           "{" + S.pop(7, 114) + "}" + m2))
        hook_txt.add(strip_punct(main))
        if sub:
            s2, _ = apply_hl(sub, d.get("hl_sub", []), color_only=True)
            ev.append(S.dialog(2, 0.20, min(2.6, total), "Sub",
                               "{" + S.pop(5, 108) + "}" + s2))
            hook_txt.add(strip_punct(sub))

    # ---- 2. 正文逐条 ----
    hl_keys = list(d.get("hl", {}).keys())
    cta_idx = set(d.get("cta_cues", [])) & set(range(1, len(cues) + 1))
    if d.get("cta_cues") and not cta_idx:
        # ⚠ DESIGN 里的 cta_cues 是**按当时的字幕条数**硬编码的。文案改短之后
        #   这个下标就指到空气里了，而且不报错 —— 表现为「正式片一直没有结尾
        #   CTA 强调」。S02 就是：DESIGN 写 [16]，实际只有 9 条。
        #   越界就按转化词重新找，并明确告警，别让它继续静默失效。
        found = [i for i, c in enumerate(cues, 1)
                 if any(w in c["text"] for w in CTA_WORDS)]
        if found:
            cta_idx = {found[-1]}
            print(f"  ⚠ {tag} 的 cta_cues={d['cta_cues']} 越界（共 {len(cues)} 条）→ "
                  f"按转化词改判第 {found[-1]} 条「{cues[found[-1] - 1]['text'][:14]}」",
                  file=sys.stderr)
        else:
            print(f"  ⚠ {tag} 的 cta_cues={d['cta_cues']} 越界（共 {len(cues)} 条），"
                  f"且没找到含 {'/'.join(CTA_WORDS[:4])} 的条 → 本片无 CTA 强调",
                  file=sys.stderr)
    warns = []
    used_hl = set()     # 每个关键词**全片只染一次**
    skipped_title = 0
    for i, c in enumerate(cues, 1):
        style = "CTA" if i in cta_idx else "Cue"
        body = norm_lines(c["text"])       # 多行 cue → \N
        # 钩子期间（前 2.6s）如果这条字幕就是标题那句话，别再在底部重复一遍 ——
        # 实测 T01 开场是「你觉得分身贵」在屏幕上方（大标题）和下方（字幕）同时出现，
        # 一句话看两遍，比没有标题更难看。
        if i not in cta_idx and c["t0"] < 2.7 and strip_punct(c["text"]) in hook_txt:
            skipped_title += 1
            continue
        if i in cta_idx:
            # CTA 不套花字，整条都已经是重点
            txt = "{" + S.pop(6, 112) + "}" + body
        else:
            # ⚠ 只用还没染过的词。否则同一个词在相邻两条里重复出现时会连染两遍
            #   （T01「就是十二个钟头」/「这十二个钟头」连着两条都紫了，很闹）。
            #   全篇只染 4~6 处是设计初衷，重复染等于把花字当成默认样式。
            keys = [k for k in hl_keys if strip_punct(k) not in used_hl]
            decorated, used = apply_hl(body, keys)
            if used:
                used_hl.update(used)
                txt = "{" + S.fade_only() + "}" + decorated
            else:
                txt = body
        ev.append(S.dialog(1, c["t0"], c["t1"], style, txt))

    if skipped_title:
        print(f"  · 开场标题已承担 {skipped_title} 条同文字幕，它们不再上屏（避免一句话看两遍）",
              file=sys.stderr)

    # ★ DESIGN.hl 的词如果没出现在定稿文案里，就是**静默丢花字**（文案改过就会这样）。
    #   2026-10-30 实测 S02：手工配了 9 个词，实际只染上 1 个（「换衣服化妆」），
    #   其余 8 个一个不响 —— 看成品只会觉得「怎么没花字」，翻代码才知道 key 早失配了。
    #   CTA 那条按设计不套花字，所以它命中的词不算失配，别刷这条告警。
    cta_text = "".join(strip_punct(cues[i - 1]["text"]) for i in cta_idx)
    lost = [k for k in hl_keys
            if strip_punct(k) not in used_hl and strip_punct(k) not in cta_text]
    if lost:
        print(f"  ⚠ {tag} 的 DESIGN.hl 有 {len(lost)} 个词没出现在字幕里（文案改过？"
              f"不会上屏）：{'、'.join(lost[:6])}{' …' if len(lost) > 6 else ''}",
              file=sys.stderr)
        warns.append(f"DESIGN.hl 失配 {len(lost)} 个：{lost[:6]}")
    S.write_ass(out_ass, ev)
    return ev, warns


def _apply_ip_fonts():
    """字体取本 IP 的 ip.json（subtitle.font_cue / font_cue_b / font_title）。
    没写就沿用 substyle 的定板默认值 —— 也就是"不填 = 行为与以前一模一样"。

    为什么用环境变量传：substyle 的字体常量是 import 时固化的，而 ass_head()
    是每次现拼的。要在"已经 import 之后"还能改，只有 env 这条路。
    ⚠️ 名字必须和字体文件里的真实 family 完全一致，否则 libass 静默回退 Noto，
       字形全错还不报错（别信"看起来能跑"）。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    p = os.path.normpath(os.path.join(here, "..", "ip.json"))
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            sub = (json.load(f) or {}).get("subtitle") or {}
    except Exception:
        return {}
    for key, env in (("font_cue", "SUB_FONT_CUE"),
                     ("font_cue_b", "SUB_FONT_CUE_B"),
                     ("font_title", "SUB_FONT_TITLE")):
        v = sub.get(key)
        if v:
            os.environ[env] = str(v)
    return sub


if __name__ == "__main__":
    # 用法: make_ass.py <tag> <srt> <out.ass> [宽 高]
    # 给了宽高就按该分辨率等比缩放字幕几何（PlayRes/字号/安全区/边距），
    # 不给则沿用定板 768x1376。
    _apply_ip_fonts()
    if len(sys.argv) >= 6:
        W_, H_ = int(sys.argv[4]), int(sys.argv[5])
        S.set_size(W_, H_)
        # ★ 字号覆盖必须在 set_size 之后 —— set_size 会按分辨率把字号等比缩放，
        #   先覆盖再缩放 = 覆盖值被缩掉，等于没覆盖（2026-10-29 实测）。
        S.load_ip_overrides()
        _ov = S.apply_ip_overrides()
        if _ov:
            print("  字号覆盖（本 IP 的 ip.json）：" +
                  " ".join(f"{k} {v['from']}→{v['to']}" for k, v in _ov.items()))
        # ★ 字号溢出会让字幕被切掉/压边，所以必须在烧之前查。
        #   但「查到就 exit 2」会把整条流水线停在这一步 —— 只是字幕样式而已，
        #   试片/低分辨率尤其容易撞（2026-10-30 实跑 S02 fast：52 号字在 544 宽要 465px，
        #   安全区只有 438px，视频/音频/配乐全都做好了，就卡在字幕上）。
        #   默认改成「自动降到安全字号 + 明确告警」，定板值仍留在 ip.json 不动；
        #   要严格失败（正式片验收）就 STRICT_FONT=1。
        _ok, _msg, _suggest = S.check_font_fit()
        if not _ok:
            _strict = (os.environ.get("STRICT_FONT") or "").strip().lower() \
                not in ("", "0", "false", "off", "no")
            if _strict:
                print(f"\n✗ {_msg}", file=sys.stderr)
                print(f"  建议字号 ≤ {_suggest}（按安全区 {S.SAFE_RIGHT - S.SAFE_LEFT}px 反算）", file=sys.stderr)
                print("  改 ip.json 的 subtitle.fs_cue，或把它设成 null 用引擎定板值。\n", file=sys.stderr)
                sys.exit(2)
            print(f"  ⚠ {_msg}", file=sys.stderr)
            print(f"    → 本片自动降到 {_suggest} 号（ip.json 定板值没动，正式片不受影响）。"
                  f"要严格失败请设 STRICT_FONT=1", file=sys.stderr)
            S.FS_CUE = _suggest
        print(f"  字幕画布 {W_}x{H_}  正文字号 {S.FS_CUE}  标题 {S.FS_TITLE}")
    tag, srt, out = sys.argv[1], sys.argv[2], sys.argv[3]
    ev, warns = build(tag, srt, out)
    print(f"{tag}: {len(ev)} 条事件 → {out}")
    for w in warns:
        print("  ⚠", w)
