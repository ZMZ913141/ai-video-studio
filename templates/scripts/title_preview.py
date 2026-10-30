#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""标题风格 / 字体预览器：把若干方案渲染到真实成片上，拼成一张对比图给人挑。

为什么需要它
============
标题是**全 IP 通用的设计系统**（substyle.py），不是某个客户的东西。
改一次要重新烧一遍成片才知道好不好看，来回烧既慢又烧不出「对比」。
这个脚本一次把 N 个方案（风格 × 字体）按同样的时间点抽帧、裁到标题区、
拼成一张网格图 —— 一眼就能挑，挑完把名字写进 ip.json 的
subtitle.title_style / font_title 即为定板。

★ 只渲染「标题事件」，不渲染正文字幕：预览要看得清差异，正文会干扰判断。
  （想看正文样式用 --cue，会额外出一张正文字幕样式的对比图。）

用法
====
  title_preview.py --list-styles                    # 列出所有标题风格
  title_preview.py --list-fonts [--top 20]          # 列出系统里能用的中文字体
  title_preview.py <视频> <SRT> --compare-all       # 全部风格 × 当前字体 → 一张对比图
  title_preview.py <视频> <SRT> --fonts "思源宋体 CN,优设标题黑"   # 字体对比
  title_preview.py <视频> <SRT> --style magazine --font "思源宋体 CN"
  title_preview.py <视频> <SRT> --compare-all --at 0.55,1.60 --out 挑.png

参数
====
  --style NAME      风格名（可多次给，或给 all）
  --font NAME       字体名（可多次给；all = 内置推荐标题字体清单）
  --at 0.55,1.60    抽帧时间点（默认 1.60，落定后的样子）
  --out PATH        输出图（默认 08_过程/_标题预览/对比_<时间戳>.png）
  --crop-y H        截取画面顶部多高（默认 520，标题区）
  --scale W         每张缩略图宽度（默认 432）
  --no-label        不打标签
"""
import argparse
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import substyle as S          # noqa: E402
import burn_ass as B          # noqa: E402  复用它的 ffmpeg 封装（flatpak kdenlive）

# 内置推荐：系统里 107 个可选择名字里真正适合「标题」的这批
# （手写体/书法体不进 —— 口播片用书法体会显得不专业，且笔画细、压不住画面）
# ★ 宋/黑体系列必须点名**重字重**：思源宋体 CN Regular / 思源黑体 CN Regular
#   是正文用字重，放标题上笔画太细，浅背景一压就飘（2026-10-30 对比图实测）。
RECOMMENDED_FONTS = [
    "仓耳渔阳体 W05", "仓耳渔阳体 W04",
    "思源宋体 CN Heavy", "思源宋体 CN SemiBold",
    "思源黑体 CN Heavy", "思源黑体 CN Bold",
    "优设标题黑", "优设标题圆",
    "庞门正道标题体", "站酷高端黑",
    "阿里巴巴普惠体 2.0 115 Black", "千图厚黑体",
]

# 正文字幕的可选样式（--cue 时对比）：(名, 说明, override tag)
CUE_VARIANTS = [
    ("定板", "白字 + 细描边（当前）", ""),
    ("粗描边", "描边加粗，压花背景更稳", "\\bord6"),
    ("关键词金", "整条不变，仅花字金色（对照）", ""),
]


# ---------------- 字体清单 ----------------

_FONT_CACHE = {}


def _resolve(name):
    """一个字体名实际会落到哪个字体文件（fontconfig 的解析结果）。"""
    if name not in _FONT_CACHE:
        _FONT_CACHE[name] = subprocess.run(
            ["fc-match", "-f", "%{file}", name],
            capture_output=True, text=True).stdout.strip()
    return _FONT_CACHE[name]


def list_fonts(top=0):
    """系统里能解析到的中文字体，**按字重展开**，只收「名下落在自己文件上」的名字。

    两个必须做的事：
    1) 多字重家族要拆开：`仓耳渔阳体` 家族有 W01~W05，只按 family 列会只剩 W01，
       而 W05 才是做标题要的那一档 —— 名字写成「家族 + 字重」才挑得到。
    2) **严格校验**：`fc-match <名字>` 必须解析回**这个字体自己的文件**。
       只判「不是 Noto」不够 —— 错名会静默落到同家族另一档（比如 W04 的名字
       落到 W01）或别的字体，烧出来字形不对但不报错，肉眼很难发现。
    """
    out = subprocess.run(["fc-list", "-f", "%{family}|%{style}|%{file}\n"],
                         capture_output=True, text=True).stdout
    res, seen_file = [], set()
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) < 3:
            continue
        fam, style, path = parts[0], parts[1], parts[2]
        main = fam.split(",")[0].strip()
        if not main:
            continue
        # 只要中文家族（纯拉丁字形渲染不出汉字）
        if re.fullmatch(r"[A-Za-z0-9 \-_.]+", main):
            continue
        cands = [main]
        # ⚠ %{style} 可能返回多个本地化值（"W05,Regular"），逗号拼接 → 只取第一个，
        #   否则拼出来的字体名是「仓耳渔阳体 W05,Regular」，libass 认不出、静默回退。
        st = style.split(",")[0].strip()
        if st and st.lower() not in ("regular", "normal", "book"):
            cands.append(f"{main} {st}")       # 家族 + 字重（W05 / 115 Black / Bold…）
        for name in cands:
            hit = _resolve(name)
            if os.path.basename(hit) != os.path.basename(path):
                continue                        # 名字解析不到自己 → 会烧错字形，剔除
            if hit in seen_file:
                continue
            seen_file.add(hit)
            res.append((name, os.path.basename(path)))
    res.sort()
    if top:
        res = res[:top]
    return res


# ---------------- 抽帧 ----------------

def parse_srt_head(path, n=2):
    """取 SRT 前 n 条的纯文本 → 当主标/副标。"""
    txt = open(path, encoding="utf-8-sig").read()
    out = []
    for m in re.finditer(
            r"\d+\n\d\d:\d\d:\d\d,\d+ --> \d\d:\d\d:\d\d,\d+\n(.*?)(?:\n\n|$)", txt, re.S):
        s = m.group(1).strip().replace("\n", "")
        s = re.sub(r"[，。？！、；：\s]", "", s)
        if s:
            out.append(s)
        if len(out) >= n:
            break
    return out


def render_frame(video, ass, t, dst, crop_h, scale_w, vw):
    """在真实画面上渲染 t 秒那一帧，裁出标题区并缩放。

    ★ 缩放必须给死宽高（不能用 -1）：xstack 要求所有输入尺寸一致，
      用 -1 时奇偶舍入会让某张图差 1px，拼图直接失败。
    """
    th = int(round(scale_w * crop_h / vw / 2)) * 2
    vf = B.vf_for(ass) + f",crop=iw:{crop_h}:0:0,scale={scale_w}:{th},setsar=1"
    # ★ -ss 必须放在 -i 之后（输出侧 seek）：放 -i 前是输入 seek，帧的 PTS 会归零，
    #   libass 于是按 **0 秒** 渲染字幕 —— 抽出来的是淡入中间态：标题半透明、
    #   副标还没出现、印章底块只剩初始细条。这个坑看起来像「样式没生效」，
    #   其实是时间轴错了（2026-10-30 实测踩过）。
    r = B.run(["-v", "error", "-i", video, "-vf", vf, "-ss", str(t),
               "-frames:v", "1", "-update", "1", "-y", dst])
    if r.returncode != 0:
        raise SystemExit("抽帧失败：\n" + r.stderr.decode()[:1200])
    return dst


def label_frame(src, dst, text):
    """在缩略图左上角打一行标签（找不到中文字体就不打，宁缺勿乱）。"""
    font_file = ""
    for fam in ("思源黑体 CN", "阿里巴巴普惠体 2.0", "Noto Sans CJK SC"):
        p = subprocess.run(["fc-match", "-f", "%{file}\n", fam],
                           capture_output=True, text=True).stdout.strip()
        if p and "noto" not in p.lower():
            font_file = p
            break
    if not font_file:
        return src
    dt = (f"drawtext=fontfile='{font_file}':text='{text}':"
          f"x=12:y=10:fontsize=30:fontcolor=white:borderw=3:bordercolor=black@0.8")
    r = B.run(["-v", "error", "-i", src, "-vf", dt, "-y", dst])
    if r.returncode != 0:
        return src        # drawtext 不可用就退回无标签，别让整张图失败
    return dst


def tile(images, out, cols=2, pad=8):
    """把多张缩略图拼成网格（ffmpeg tile 滤镜，避免依赖 PIL）。

    ★ 为什么用 tile 而不是 xstack：xstack 的 layout 要手写 `w0_h0` 这类坐标，
      列数一变就容易写错（实测报 Failed to configure output pad）。
      tile 只吃「列数×行数」，把缩略图当连续帧喂进去就行。
      代价是必须先把文件重命名成连续编号序列（image2 的要求）。
    """
    if not images:
        return None
    if len(images) == 1:
        if os.path.abspath(images[0]) != os.path.abspath(out):
            os.replace(images[0], out)
        return out
    rows = (len(images) + cols - 1) // cols
    w, h = probe_size(images[0])
    args = []
    for p in images:
        args += ["-i", p]
    # 末行不足 cols 时补黑块：hstack/vstack 要求各行宽度、各列高度严格一致，
    # 少一个就整图失败（实测报 Error reinitializing filters）。
    for _ in range(rows * cols - len(images)):
        args += ["-f", "lavfi", "-i", f"color=c=0x1A1A1A:s={w}x{h}:d=1"]
    fc, row_lbl = [], []
    for r in range(rows):
        ins = "".join(f"[{r * cols + k}:0]" for k in range(cols))
        fc.append(f"{ins}hstack=inputs={cols}[r{r}]")
        row_lbl.append(f"[r{r}]")
    fc.append("".join(row_lbl) + f"vstack=inputs={rows}[v]")
    r = B.run(["-v", "error"] + args + ["-filter_complex", ";".join(fc),
                                        "-map", "[v]", "-frames:v", "1", "-y", out])
    if r.returncode != 0:
        raise SystemExit("拼图失败：\n" + r.stderr.decode()[:1500])
    return out


def probe_size(path):
    """图片实际尺寸（必须问出来，不能算/不能兜默认值）。

    ★ 踩过的坑：正则写死成 ", 430x228" 这种格式，PNG 的流日志是
      "Video: png, rgb24(pc), 430x228" —— 匹配不上就静默退回默认 432x260，
      补位的黑块于是比缩略图高 32px，hstack 直接报
      "Input 1 height 260 does not match input 0 height 228"。
      表现像「拼图代码坏了」，其实是尺寸探测失效。取所有 x 形数字里的最后一个。
    """
    # ⚠ 不能用 -v error：流信息是 info 级日志，压掉就什么都读不到
    #   （曾经因此拿到空文本 → 退回默认尺寸 → 拼图高度不匹配）。
    r = B.run(["-i", path, "-f", "null", "-"])
    txt = (r.stderr or b"").decode(errors="ignore")
    ms = re.findall(r"(\d{2,5})x(\d{2,5})", txt)
    if ms:
        return int(ms[-1][0]), int(ms[-1][1])
    raise SystemExit(f"探测不到图片尺寸：{path}\n{txt[-400:]}")


# ---------------- 主流程 ----------------

def build_ass(items, out_dir, video_w, video_h, main, sub, label=True, hold=None):
    """items: [(tag, cfg)] → 每个方案一份 ASS。

    cfg 是标题配置的**子集**（style/font/motion/anchor/offset/glow/sweep/hold），
    走 substyle.build_title(**kw) 临时覆盖 —— 用完自动还原，不会串味到下一条。

    标签直接写进 ASS（\an7 左上角），而不是 ffmpeg drawtext ——
    flatpak 里的 ffmpeg 常常没编译 drawtext，失败还静默回退，
    结果就是「一排图长得都不一样，但不知道哪个是哪个」（实测踩过）。
    libass 渲染中文本来就没问题，用同一套机制最稳。
    """
    S.set_size(video_w, video_h)
    S.load_ip_overrides()
    S.apply_ip_overrides()
    S.apply_title_cfg()          # 先吃 ip.json / env 的定板，再逐条临时覆盖
    _hold = hold or S.title_hold()
    asses = []
    for i, (tag, cfg) in enumerate(items):
        cfg = dict(cfg)
        font = cfg.pop("font", "")
        # ★ 位置是**持久**全局（要写进 ASS 样式的 MarginV），所以每个方案都必须
        #   显式带上，否则会残留上一个方案的位置（2026-10-30 踩过）。
        cfg.setdefault("anchor", S.TITLE_ANCHOR)
        cfg.setdefault("offset", S.TITLE_OFFSET)
        if font:
            os.environ["SUB_FONT_TITLE"] = font
        style = cfg.pop("style", "") or S.title_style()
        ev = S.build_title(style, main, 0.0, _hold, sub=sub, **cfg)
        if label:
            ev.append(S.dialog(9, 0.0, _hold, "Tag",
                "{\\an7\\pos(26,22)\\fs30\\bord4\\shad0"
                "\\1c&H00FFFFFF&\\3c&H00101010&}" + tag))
        safe = re.sub(r"[^0-9A-Za-z一-龥]+", "_", tag)[:40]
        p = os.path.join(out_dir, f"_{i}_{safe}.ass")
        S.write_ass(p, ev)
        asses.append((p, tag))
    return asses


def probe_video_wh(video):
    r = B.run(["-i", video])
    txt = (r.stderr or b"").decode(errors="ignore")
    m = re.search(r", (\d{2,5})x(\d{2,5})", txt)
    return (int(m.group(1)), int(m.group(2))) if m else (864, 1536)


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("video", nargs="?")
    ap.add_argument("srt", nargs="?")
    ap.add_argument("--style", action="append", default=[])
    ap.add_argument("--font", action="append", default=[])
    ap.add_argument("--motion", action="append", default=[],
                    help="入场动效（可多次 / all）")
    ap.add_argument("--pos", action="append", default=[],
                    help="标题位置（可多次 / all）：top third center lower")
    ap.add_argument("--offset", type=int, default=None,
                    help="位置微调，像素（+ 下移 / - 上移）")
    ap.add_argument("--glow", type=int, default=None, help="辉光强度 0-3")
    ap.add_argument("--sweep", default=None, help="扫光开关 on/off")
    ap.add_argument("--hold", type=float, default=None, help="标题停留秒数")
    ap.add_argument("--config", metavar="JSON",
                    help="从一个配置文件读整套标题参数（ip.json 也行，取 subtitle.title）")
    ap.add_argument("--compare-all", action="store_true")
    ap.add_argument("--at", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--crop-y", type=int, default=520)
    ap.add_argument("--scale", type=int, default=432)
    ap.add_argument("--apply", metavar="IP_JSON",
                    help="把选定的风格/字体/动效/位置写进该 ip.json（subtitle.title）")
    ap.add_argument("--list-styles", action="store_true")
    ap.add_argument("--list-motions", action="store_true")
    ap.add_argument("--list-pos", action="store_true")
    ap.add_argument("--list-fonts", action="store_true")
    ap.add_argument("--top", type=int, default=0)
    ap.add_argument("--no-label", action="store_true")
    a = ap.parse_args()

    if a.list_styles:
        print("可用标题风格（ip.json: subtitle.title.style）：")
        for k, cn, desc in S.title_style_list():
            print(f"  {k:<10} {cn:<4} {desc}")
        return
    if a.list_motions:
        print("可用入场动效（ip.json: subtitle.title.motion）：")
        for k, cn, desc in S.motion_list():
            print(f"  {k:<10} {cn:<6} {desc}")
        return
    if a.list_pos:
        print("可用标题位置（ip.json: subtitle.title.anchor + offset 像素微调）：")
        for k, desc in S.TITLE_ANCHORS.items():
            print(f"  {k:<8} {desc}")
        return
    if a.list_fonts:
        fs = list_fonts(a.top)
        print(f"系统可解析的中文字体 {len(fs)} 个（ip.json: subtitle.font_title）：")
        for n, f in fs:
            print(f"  {n}\t{f}")
        print("\n推荐做标题的：")
        for f in RECOMMENDED_FONTS:
            print(f"  · {f}")
        return
    if a.apply:
        import json
        style = (a.style[0].strip() if a.style else "")
        font = (a.font[0].strip() if a.font else "")
        if style and style not in S._TITLE_BUILDERS:
            raise SystemExit(f"未知风格 {style!r}，可选：{', '.join(S._TITLE_BUILDERS)}")
        with open(a.apply, encoding="utf-8") as f:
            d = json.load(f)
        sub_cfg = d.setdefault("subtitle", {})
        t_cfg = sub_cfg.setdefault("title", {})
        # 旧字段（subtitle.title_style / font_title）继续兼容，新定板写进 title 块
        if style:
            t_cfg["style"] = style
            sub_cfg["title_style"] = style
        if font:
            t_cfg["font"] = font
            sub_cfg["font_title"] = font
        if a.motion:
            t_cfg["motion"] = a.motion[0].strip()
        if a.pos:
            t_cfg["anchor"] = a.pos[0].strip()
        if a.offset is not None:
            t_cfg["offset"] = a.offset
        if a.glow is not None:
            t_cfg["glow"] = a.glow
        if a.sweep is not None:
            t_cfg["sweep"] = str(a.sweep).strip().lower() not in ("0", "off", "false", "no")
        if a.hold is not None:
            t_cfg["hold"] = a.hold
        with open(a.apply, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        print(f"✓ 已写回 {a.apply}：subtitle.title = "
              + json.dumps(t_cfg, ensure_ascii=False))
        print("  下一步：make_ass.py 重新生成 ASS → 重烧字幕版")
        return

    if not a.video or not a.srt:
        ap.print_help()
        return

    vw, vh = probe_video_wh(a.video)
    heads = parse_srt_head(a.srt, 2)
    main_txt = heads[0] if heads else "标题预览"
    sub_txt = heads[1] if len(heads) > 1 else ""

    # ---- 四个可挑维度：风格 / 字体 / 动效 / 位置 ----
    # 给了多个（或 all）→ 当维度，出对比图；只给一个 → 当基础配置，全场生效。
    def _dim(vals, universe):
        v = [x.strip() for x in vals if x.strip()]
        if any(x.lower() == "all" for x in v):
            return list(universe), True
        return v, len(v) > 1

    motions, m_dim = _dim(a.motion, S.TITLE_MOTIONS.keys())
    poss, p_dim = _dim(a.pos, S.TITLE_ANCHORS.keys())
    styles, s_dim = _dim(a.style, S._TITLE_BUILDERS.keys())
    fonts, f_dim = _dim(a.font, RECOMMENDED_FONTS)
    if a.compare_all or (not styles and not fonts and not motions and not poss):
        styles, s_dim = [k for k, _, _ in S.title_style_list()], True
    if fonts and not styles and not a.style:
        # ★ 比字体时默认用 pure（极简）：cinematic 的辉光/扫光/渐变会把字形
        #   细节全盖住，几款字体看起来几乎一样，等于没法挑（实测踩过）。
        styles, s_dim = ["pure"], False

    base = {}
    if a.config:
        import json as _json
        with open(a.config, encoding="utf-8") as f:
            cd = _json.load(f)
        base = ((cd.get("subtitle") or {}).get("title")
                or cd.get("title") or cd)
        base = {k: v for k, v in base.items() if v is not None}
    if a.glow is not None:
        base["glow"] = a.glow
    if a.sweep is not None:
        base["sweep"] = str(a.sweep).strip().lower() not in ("0", "off", "false", "no")
    if a.offset is not None:
        base["offset"] = a.offset
    if a.hold is not None:
        base["hold"] = a.hold
    # 单值的维度退化成基础配置（不是「对比」，是「按这个来」）
    for name, vals, is_dim in (("motion", motions, m_dim), ("anchor", poss, p_dim),
                               ("style", styles, s_dim), ("font", fonts, f_dim)):
        if vals and not is_dim:
            base[name] = vals[0]
    motions = motions if m_dim else []
    poss = poss if p_dim else []
    styles = styles if s_dim else []
    fonts = fonts if f_dim else []

    dims = [(k, v) for k, v in (("motion", motions), ("anchor", poss),
                                ("font", fonts), ("style", styles)) if v]
    combos = [{}]
    for k, v in dims:
        combos = [dict(c, **{k: x}) for c in combos for x in v]
    if len(combos) > 24:
        print(f"  ⚠ {len(combos)} 个组合太多，只出前 24 个")
        combos = combos[:24]

    cn_style = {k: v[0] for k, v in S.TITLE_STYLES.items()}
    cn_motion = {k: v[0] for k, v in S.TITLE_MOTIONS.items()}
    items = []
    for c in combos:
        cfg = dict(base)
        cfg.update(c)
        parts = []
        if c.get("style"):
            parts.append(f"{c['style']}·{cn_style.get(c['style'], '')}")
        if c.get("motion"):
            parts.append(f"动效 {cn_motion.get(c['motion'], c['motion'])}")
        if c.get("anchor"):
            parts.append(f"位置 {c['anchor']}")
        if c.get("font"):
            parts.append(c["font"])
        items.append((" · ".join(parts) or "定板", cfg))

    # ---- 时间点 ----
    # ★ 动效是**时间维度**的东西：只看落定帧根本看不出入场方式（都长一个样）。
    #   比动效必须抽「入场进行中」这一刻 —— 定板动画 460ms，0.30s 时 fade 已经
    #   淡完了（和 none 一模一样，实测像素差 0），得早到 0.15s 才看得出区别。
    times = [float(x) for x in a.at.split(",") if x.strip()]
    if not times:
        times = [0.15, 0.45, 1.60] if any(k == "motion" for k, _ in dims) else [1.60]

    out_dir = "/cfu/ai视频档案库/陈洁斌主号/08_过程/_标题预览"
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError:
        out_dir = os.path.join(os.path.dirname(os.path.abspath(a.video)), "_标题预览")
        os.makedirs(out_dir, exist_ok=True)

    print(f"主标「{main_txt}」 副标「{sub_txt}」  画面 {vw}x{vh}")
    asses = build_ass(items, out_dir, vw, vh, main_txt, sub_txt,
                      label=not a.no_label, hold=base.get("hold"))
    thumbs = []
    for (p, tag) in asses:
        for t in times:
            label = tag + (f" @{t}s" if len(times) > 1 else "")
            raw = os.path.join(out_dir, f"_{abs(hash(label)) % 100000}.png")
            render_frame(a.video, p, t, raw, a.crop_y, a.scale, vw)
            thumbs.append(raw)
            print("  ✓", label)

    # ⚠ cols 必须 ≥2：hstack 的 inputs 参数不接受 1（报 out of range [2-...]）。
    #    只有 1 张图时根本不会进 tile（上面直接返回），所以这里钳到 2 是安全的。
    cols = max(2, min(len(times), 3))
    out = a.out or os.path.join(out_dir, f"对比_{time.strftime('%m%d-%H%M%S')}.png")
    tile(thumbs, out, cols=cols)
    print("\n对比图 →", out)


if __name__ == "__main__":
    main()
