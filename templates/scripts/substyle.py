#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""字幕 + 标题的视觉设计系统（基准 768×1376 竖屏，可按目标分辨率等比缩放）。

设计依据（全部实测，不是拍脑袋）
==============================
1. **安全区**：按 1080×1920 平台基准换算（系数 x0.711 / y0.717）→
   顶部 y<107、底部 y>1118、右侧 x>661 都不能放字。
2. **字号 48px**（2026-10-27 用户微调定板，原 44）：安全框宽 619px。
   **实测普惠体 2.0 的汉字实际进距 ≈ 0.745em**，48px×12 字 = 429px，
   居中 x170–598，右侧余量 61px，仍在点赞列 x661 之内。
   （实测数据：11 字 377px / 12 字 410px / 13 字 444px，进距 34.3px@46px）
   上限验算：48px 下 14 字 ≈ 500px 也放得下，但用户定的 7–12 字更舒服。
3. **白衬衫不迁就**：白字 + 6px 深棕描边 + 2px 阴影，实测在 180 亮度底上
   仍可读；不用底条（底条会把白衬衫压成灰色，画面发脏）。
   字号提档后描边反而收一号（7→6）：字变大后同宽描边视觉占比更粗，收 1px 才不笨重。
4. **配色取自画面本身**：墙暖棕 #6C5837 / #98825F、白衬衫 #C2B9AF、
   绿植 #343311。强调色用暖金 #FFC24B，和画面同色调，不打架。

多分辨率
========
所有尺寸常量都以 768×1376 为基准写。出片分辨率变了（如 896×1568 的 1.33MP 档），
必须调 `set_size(w, h)` 把所有几何量按比例换算 —— 否则 `PlayResX/Y` 还是 768×1376，
libass 会拿**旧的画布坐标**去渲染**新的实际画面**，字幕位置和字号全错。
字号本身按宽高几何平均缩放（等比），保证字与画面比例一致；
描边/阴影按宽度缩放，避免锐利度损失。
"""
import os

# ---------- 画布（基准） ----------
BASE_W, BASE_H = 768, 1376
W, H = BASE_W, BASE_H
FPS = 24

# ---------- 安全区（768×1376 实测换算） ----------
SAFE_TOP = 107
SAFE_BOTTOM = 1118
SAFE_RIGHT = 661
SAFE_LEFT = 42

# ---------- 字号 ----------
# 2026-10-27 用户两次微调：「标题大一号、字幕也大一号、描边小一号」
# → 第二轮：「字幕的字号可以再大一号，和描边小一号」
# 注意：普惠体实际进距 0.745em，每档只涨 4.5% 用户看不出来。
# **可感知幅度约 9%**，所以一档 = +4px（48→52）。这是幅度门槛，别只加 2px。
FS_TITLE = 80       # 开场钩子主标题（72→80，已定）
FS_SUB = 38         # 副标题（34→38，已定）
FS_CUE = 52         # 正文字幕（44→48→52，本轮 +4）
FS_CTA = 64         # 结尾行动号召（58→62→64，维持比正文高一档）
FS_TAG = 30         # 小标签

# 基准值快照：set_size() 反复调用时要能从基准重新算，不能基于已缩放的值再缩
_BASE = {}


def set_size(w, h):
    """按目标分辨率等比缩放所有几何量（字号/安全区/边距/描边）。

    只缩几何，不动颜色和设计比例。可重复调用（每次都从基准值算）。
    """
    global W, H, SAFE_TOP, SAFE_BOTTOM, SAFE_RIGHT, SAFE_LEFT
    global FS_TITLE, FS_SUB, FS_CUE, FS_CTA, FS_TAG
    global OUT_CUE, SHA_CUE, OUT_TITLE, SHA_TITLE, CUE_MARGIN_V, TITLE_MARGIN_V

    if not _BASE:
        _BASE.update({
            "SAFE_TOP": SAFE_TOP, "SAFE_BOTTOM": SAFE_BOTTOM,
            "SAFE_RIGHT": SAFE_RIGHT, "SAFE_LEFT": SAFE_LEFT,
            "FS_TITLE": FS_TITLE, "FS_SUB": FS_SUB, "FS_CUE": FS_CUE,
            "FS_CTA": FS_CTA, "FS_TAG": FS_TAG,
            "OUT_CUE": OUT_CUE, "SHA_CUE": SHA_CUE,
            "OUT_TITLE": OUT_TITLE, "SHA_TITLE": SHA_TITLE,
            "CUE_MARGIN_V": CUE_MARGIN_V, "TITLE_MARGIN_V": TITLE_MARGIN_V,
        })

    W, H = w, h
    rx, ry = w / BASE_W, h / BASE_H
    rg = (rx * ry) ** 0.5          # 几何平均：字号按面积比例走，视觉大小才一致

    SAFE_TOP = int(round(_BASE["SAFE_TOP"] * ry))
    SAFE_BOTTOM = int(round(_BASE["SAFE_BOTTOM"] * ry))
    SAFE_RIGHT = int(round(_BASE["SAFE_RIGHT"] * rx))
    SAFE_LEFT = int(round(_BASE["SAFE_LEFT"] * rx))

    FS_TITLE = int(round(_BASE["FS_TITLE"] * rg))
    FS_SUB = int(round(_BASE["FS_SUB"] * rg))
    FS_CUE = int(round(_BASE["FS_CUE"] * rg))
    FS_CTA = int(round(_BASE["FS_CTA"] * rg))
    FS_TAG = int(round(_BASE["FS_TAG"] * rg))

    OUT_CUE = max(3, int(round(_BASE["OUT_CUE"] * rx)))
    SHA_CUE = max(1, int(round(_BASE["SHA_CUE"] * rx)))
    OUT_TITLE = max(3, int(round(_BASE["OUT_TITLE"] * rx)))
    SHA_TITLE = max(1, int(round(_BASE["SHA_TITLE"] * rx)))
    CUE_MARGIN_V = int(round(_BASE["CUE_MARGIN_V"] * ry))
    TITLE_MARGIN_V = int(round(_BASE["TITLE_MARGIN_V"] * ry))
    return W, H

# ---------- 颜色（ASS 用 &HAABBGGRR，即 BGR 倒序） ----------
C_WHITE = "00FFFFFF"
C_GOLD = "004BC2FF"        # #FFC24B 暖金（关键词/数字）
C_ORANGE = "002E8AFF"      # #FF8A2E 橙金（CTA）
C_DARK = "00140E08"        # #080E14 近黑棕（描边）
C_WARM = "00EAF2F5"        # #F5F2EA 暖米白（副文案）

# ---------- 描边 / 阴影 ----------
# 实测：白衬衫亮度 180，任何亮色对比度都 <2.1:1 —— 靠颜色救不回来。
# 真正起作用的是**深色描边**（剪映「描边字」同理）。
# 2026-10-27 两轮用户微调：字号每提一档，描边就收一号（7→6→5）。
# 原因是字变大后同宽描边的视觉占比更粗，不跟着收就显得笨重、糊成一团。
# ⚠️ 下限 4px：实测在饱和度最高的衬衫高光处，4px 仍能包住字芯，
#    再细就会出现「字边渗到衬衫里」的发虚感，不要往下调了。
OUT_CUE = 5
SHA_CUE = 2
OUT_TITLE = 8
SHA_TITLE = 4

# ---------- 字体（已实测 fc-match 可解析） ----------
# ⚠️ 名字必须和字体文件里的真实 family 名一致，否则 libass 静默回退 Noto。
#    实测通过的是：AlibabaPuHuiTi_2_115_Black / YouSheBiaoTiHei / PangMenZhengDao-Cu
FONT_CUE = "阿里巴巴普惠体 2.0 75 SemiBold"     # 正文字幕：稳、清楚
FONT_CUE_B = "阿里巴巴普惠体 2.0 95 ExtraBold"  # CTA / 强调
FONT_TITLE = "阿里巴巴普惠体 2.0 115 Black"      # 标题：最硬最抓眼（不是仓耳渔阳）
FONT_TITLE_SOFT = "站酷仓耳渔阳体 W04"           # 备用：偏人文，适合金句
FONT_TITLE_ALT = "优设标题黑"                    # 备用：偏设计感，适合短钩子

# 字体要能被 IP 单独换 —— 写死在模块里就意味着"想换字体得改引擎代码"，
# 而每个 IP 的字幕气质本来就不该一样。
# 走环境变量而不是构造参数：调用方（make_ass）已经在 import 之后再决定用哪个字体，
# 常量是 import 时就固化的，改不动；ass_head() 是每次现拼的，那里读才生效。
_FONT_ENV = {"cue": ("SUB_FONT_CUE", FONT_CUE),
             "cue_b": ("SUB_FONT_CUE_B", FONT_CUE_B),
             "title": ("SUB_FONT_TITLE", FONT_TITLE)}


def font_of(kind):
    """取字体名；环境变量没设就用定板默认值（不设 = 行为与从前完全一致）"""
    env, default = _FONT_ENV[kind]
    return os.environ.get(env) or default

# ---------- 本 IP 的字号覆盖（ip.json） ----------
# ★ 2026-10-29 修的「假参数」问题：
#   ip.json 里一直有 subtitle.fs_cue / fs_title，界面上也一直能编辑，
#   但**没有任何脚本读它们** —— 字号是这里写死的 52。
#   人改完看界面显示"改了"，实际出片一点没变。这是最恶劣的一类 bug：
#   界面在骗人。
#   现在让它真读 ip.json（唯一真源），null/空 = 用这里的定板值。
#
#   应用时机：**set_size() 之后**。因为 set_size 会按分辨率把字号等比缩放，
#   先覆盖再缩放 = 覆盖值被缩掉，等于没覆盖。
_IP_OVERRIDE = {}       # {"fs_cue": 87, ...}
# ip.json 的字号是**在哪个分辨率下定板的**（subtitle.base_res，默认引擎基准）。
# ★ 2026-10-30 修：以前这里没有 base 概念，ip.json 的 52 被当绝对像素硬塞 ——
#   主号卡里明明写着 base_res [768,1376] 和「按 w/768 等比缩放」，
#   代码却完全不理。结果 std(864) 上字比设计值偏小、fast(544) 上直接溢出到流水线报错。
_IP_BASE_RES = (BASE_W, BASE_H)


def load_ip_overrides(start=None):
    """从本 IP 的 ip.json 读字号覆盖。找不到就什么都不改（行为与从前一致）。

    ★ 同时读 subtitle.base_res：那些字号是在 base_res 下定的（ip.json 里通常写着
      [768,1376] 并有「按 w/768 等比缩放」的注释）。跨分辨率时必须按 base_res 缩放，
      否则 std 上偏小、fast 上溢出。没有 base_res 就按引擎基准算。
    """
    global _IP_OVERRIDE, _IP_BASE_RES
    _IP_OVERRIDE = {}
    _IP_BASE_RES = (BASE_W, BASE_H)
    try:
        from ip_guard import find_ip_json
        path, root = find_ip_json(start)
        if not path:
            return _IP_OVERRIDE
        import json as _j
        with open(path, encoding="utf-8") as f:
            data = _j.load(f)
        sub = data.get("subtitle") or {}
        _br = sub.get("base_res") or sub.get("base_w")
        if isinstance(_br, (list, tuple)) and len(_br) >= 2:
            try:
                bw, bh = int(_br[0]), int(_br[1])
                if bw > 0 and bh > 0:
                    _IP_BASE_RES = (bw, bh)
            except (TypeError, ValueError):
                pass
        for key, name in (("fs_cue", "FS_CUE"), ("fs_title", "FS_TITLE"),
                          ("fs_sub", "FS_SUB"), ("fs_cta", "FS_CTA"),
                          ("fs_tag", "FS_TAG"), ("outline", "OUT_CUE")):
            v = sub.get(key)
            if v is None:
                continue
            try:
                v = int(v)
            except (TypeError, ValueError):
                continue
            if v > 0:
                _IP_OVERRIDE[name] = v
    except Exception:
        _IP_OVERRIDE = {}
        _IP_BASE_RES = (BASE_W, BASE_H)
    return _IP_OVERRIDE


def apply_ip_overrides():
    """把 ip.json 的字号覆盖应用到当前常量（必须在 set_size 之后调）。

    ★ 值先按 ip.json 的 base_res → 当前分辨率的几何比缩放，再赋值。
      这样「定板值」的语义才是对的：std 与 fast 的字幕视觉大小一致，
      fast 试片也不会因为 52 号字在 544 宽里放不下而把整条流水线卡住。
    """
    if not _IP_OVERRIDE:
        return {}
    bw, bh = _IP_BASE_RES or (BASE_W, BASE_H)
    rg = ((W / bw) * (H / bh)) ** 0.5      # 与 set_size 同一套几何平均
    g = globals()
    applied = {}
    for name, v in _IP_OVERRIDE.items():
        target = int(round(v * rg)) if rg > 0 else v
        target = max(1, target)
        if name in g and g[name] != target:
            applied[name] = {"from": g[name], "to": target}
            g[name] = target
    return applied

# ---------- 字号安全检查 ----------
# 普惠体 2.0 的汉字实际进距 ≈ 0.745em（实测：11 字 377px / 12 字 410px @46px）
CHAR_ADVANCE = 0.745


def check_font_fit(fs_cue=None, max_chars=12):
    """字号能不能在安全区里放下。

    ★ 为什么必须查：
      ip.json 的 subtitle.fs_cue 现在真生效了（以前是死的）。
      但那值常常是从别的 IP 抄来的 —— 2026-10-29 实测：老王抄了 87，
      而它出片是 544 宽（安全区可用 438px），87px×7 字就要 454px → 溢出。
      溢出 = 字幕被切掉，或者压到画面边缘。
      以前这个值是死的所以看不出来，现在生效了，就必须当场拦。

    返回 (是否放得下, 说明文字, 建议最大字号)。
    """
    fs = fs_cue if fs_cue is not None else FS_CUE
    usable = SAFE_RIGHT - SAFE_LEFT
    need = fs * CHAR_ADVANCE * max_chars
    if need <= usable:
        return True, f"字号 {fs}，{max_chars} 字宽 {need:.0f}px，安全区可用 {usable}px —— 放得下", fs
    # 给个建议值：刚好放下的最大字号
    adv = int(usable / (CHAR_ADVANCE * max_chars))
    return (False,
            f"字号 {fs} 太大：{max_chars} 字要 {need:.0f}px，安全区只有 {usable}px（会溢出）",
            max(adv, 12))


# ---------- 关键位置 ----------
CUE_MARGIN_V = 288          # 字幕底边落在 y≈1088，安全区内且不压下巴
TITLE_MARGIN_V = 150        # 标题顶部下移，避开系统状态栏

def ass_head():
    """生成 ASS 头。**必须是函数**：模块级 f-string 会在 import 时把当时的
    常量固化下来，之后 set_size() 改了 W/H/字号，头里还是旧值（实测踩过：
    调了 set_size(896,1568)，写出的 ass 仍是 PlayResX: 768 / 字号 52）。"""
    return f"""[Script Info]
ScriptType: v4.00+
Title: 陈洁斌主号 字幕标题
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cue,{font_of("cue")},{FS_CUE},&H{C_WHITE},&H000000FF,&H{C_DARK},&H64000000,0,0,0,0,100,100,1,0,1,{OUT_CUE},{SHA_CUE},2,{SAFE_LEFT},{SAFE_LEFT},{CUE_MARGIN_V},1
Style: CueB,{font_of("cue_b")},{FS_CUE},&H{C_WHITE},&H000000FF,&H{C_DARK},&H64000000,0,0,0,0,100,100,1,0,1,{OUT_CUE},{SHA_CUE},2,{SAFE_LEFT},{SAFE_LEFT},{CUE_MARGIN_V},1
Style: Title,{font_of("title")},{FS_TITLE},&H{C_WHITE},&H000000FF,&H{C_DARK},&H78000000,0,0,0,0,100,100,2,0,1,{OUT_TITLE},{SHA_TITLE},8,{SAFE_LEFT},{SAFE_LEFT},{TITLE_MARGIN_V},1
Style: Sub,{font_of("cue")},{FS_SUB},&H{C_WARM},&H000000FF,&H{C_DARK},&H64000000,0,0,0,0,100,100,3,0,1,3,2,8,{SAFE_LEFT},{SAFE_LEFT},{int(round(260*H/BASE_H))},1
Style: CTA,{font_of("cue_b")},{FS_CTA},&H{C_WHITE},&H000000FF,&H{C_ORANGE},&H90000000,0,0,0,0,100,100,2,0,1,6,3,2,60,60,{int(round(300*H/BASE_H))},1
Style: Tag,{font_of("cue")},{FS_TAG},&H{C_GOLD},&H000000FF,&H{C_DARK},&H64000000,0,0,0,0,100,100,4,0,1,3,0,8,{SAFE_LEFT},{SAFE_LEFT},{int(round(200*H/BASE_H))},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def ts(t):
    """秒 → ASS 时间 H:MM:SS.cc"""
    t = max(0.0, t)
    h, r = int(t // 3600), t % 3600
    m, s = int(r // 60), r % 60
    cs = int(round((s - int(s)) * 100))
    if cs == 100:
        s, cs = s + 1, 0
    return f"{h}:{m:02d}:{int(s):02d}.{cs:02d}"


def pop(frames=6, overshoot=108):
    """剪映式弹入：快速放大过冲再回落。

    frames 用 @24fps 折成毫秒；overshoot 是过冲百分比。
    实测 libass 支持 \\t 与 \\fscx/\\fscy，动画确实生效。
    """
    ms = int(frames / FPS * 1000)
    over = overshoot
    return (f"\\fad(80,60)"
            f"\\t(0,{ms},\\fscx{over}\\fscy{over})"
            f"\\t({ms},{int(ms*1.8)},\\fscx100\\fscy100)")


def rise(frames=8):
    """轻微上浮入场（\\move 从下 18px 到原位）。"""
    import math
    ms = int(frames / FPS * 1000)
    return f"\\move(0,18,0,0)\\fad(0,0,{ms},0)\\t(0,{ms},\\alpha&H00&)"


def fade_only(ms_in=60, ms_out=40):
    """正文条的入场：只做极轻淡入，不做缩放。

    为什么正文不放动画：观众在读字，字在动会打断阅读。剪映里也一样——
    正文基本是硬出硬收，动画只留给标题和花字。
    """
    return f"\\fad({ms_in},{ms_out})"


def hl(text, color=C_GOLD, scale=104):
    """高亮一段词：变色 + 轻微放大（剪映「关键词描红」的等价做法）。"""
    return f"{{\\c&H{color}&\\fscx{scale}\\fscy{scale}}}{text}{{\\c&H{C_WHITE}&\\fscx100\\fscy100}}"


def hlcolor(text, color=C_GOLD):
    """只变色、**不碰缩放**——给带头部缩放动画的标题用。

    坑：标题用了 \\t(0,291,\\fscx114\\fscy114) 做弹入，如果高亮词里再写
    \\fscx104，那一段就被钉死在 104，整行放大时它不动 → 一眼看出是贴上去的。
    所以标题层只改颜色，缩放交给头部的 \\t 统一管。
    """
    return f"{{\\c&H{color}&}}{text}{{\\c&H{C_WHITE}&}}"


def dialog(layer, t0, t1, style, text):
    return f"Dialogue: {layer},{ts(t0)},{ts(t1)},{style},,0,0,0,,{text}"


def write_ass(path, events):
    with open(path, "w", encoding="utf-8") as f:
        f.write(ass_head())
        for e in events:
            f.write(e + "\n")
    return path
