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


def build(tag, srt_path, out_ass, dur=None):
    cues = parse_srt(srt_path)
    d = DESIGN.get(tag, {})
    ev = []
    total = dur or (cues[-1]["t1"] + 0.4)

    # ---- 1. 开场钩子（前 2.6s，压在画面上部空白区）----
    if d.get("hook"):
        main, sub = d["hook"]
        m2, _ = apply_hl(main, d.get("hl_title", []), color_only=True)
        ev.append(S.dialog(2, 0.0, min(2.6, total), "Title",
                           "{" + S.pop(7, 114) + "}" + m2))
        if sub:
            s2, _ = apply_hl(sub, d.get("hl_sub", []), color_only=True)
            ev.append(S.dialog(2, 0.20, min(2.6, total), "Sub",
                               "{" + S.pop(5, 108) + "}" + s2))

    # ---- 2. 正文逐条 ----
    hl_keys = list(d.get("hl", {}).keys())
    cta_idx = set(d.get("cta_cues", []))
    warns = []
    for i, c in enumerate(cues, 1):
        style = "CTA" if i in cta_idx else "Cue"
        body = norm_lines(c["text"])       # 多行 cue → \N
        if i in cta_idx:
            # CTA 不套花字，整条都已经是重点
            txt = "{" + S.pop(6, 112) + "}" + body
        else:
            decorated, used = apply_hl(body, hl_keys)
            if used:
                txt = "{" + S.fade_only() + "}" + decorated
            else:
                txt = body
        ev.append(S.dialog(1, c["t0"], c["t1"], style, txt))

    S.write_ass(out_ass, ev)
    return ev, warns


if __name__ == "__main__":
    # 用法: make_ass.py <tag> <srt> <out.ass> [宽 高]
    # 给了宽高就按该分辨率等比缩放字幕几何（PlayRes/字号/安全区/边距），
    # 不给则沿用定板 768x1376。
    if len(sys.argv) >= 6:
        W_, H_ = int(sys.argv[4]), int(sys.argv[5])
        S.set_size(W_, H_)
        print(f"  字幕画布 {W_}x{H_}  正文字号 {S.FS_CUE}  标题 {S.FS_TITLE}")
    tag, srt, out = sys.argv[1], sys.argv[2], sys.argv[3]
    ev, warns = build(tag, srt, out)
    print(f"{tag}: {len(ev)} 条事件 → {out}")
    for w in warns:
        print("  ⚠", w)
