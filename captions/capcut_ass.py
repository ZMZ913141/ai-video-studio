#!/usr/bin/env python3
"""
剪映级字幕生成器 —— 逐词高亮 / 填充（karaoke）ASS。

核心原理：ASS 的 \\kf 标签会把一个音节先用 SecondaryColour 绘制，再从左往右
用 PrimaryColour 逐步覆盖，正好就是剪映「词被说到时高亮填充」的效果。
再叠加 \\t 缩放变换即可做出「弹一下」的动态。

输入：word-level 时间戳（来自 faster-whisper / whisperx / 任意 ASR）
输出：可直接用 ffmpeg subtitles 滤镜烧录的 .ass

仅依赖标准库。
"""
import argparse
import json
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# 预设（剪映风格）
# ---------------------------------------------------------------------------
# ASS 颜色格式：&HAABBGGRR（注意是 BGR 顺序，不是 RGB）
# 注意：font_size / margin_v / outline_w 均以 1920 高度为基准，
# words_to_ass() 会按实际 height 等比缩放。
PRESETS = {
    # 剪映关键词高亮（默认，最醒目）：未读白 → 说到变黄，粗描边，底部居中
    "capcut": dict(
        font="Noto Sans CJK SC", font_size=92,
        primary="&H0000FFFF",    # 已读/高亮：黄（BGR: 00 FF FF）
        secondary="&H00FFFFFF",  # 未读：白
        outline="&H00000000", back="&H80000000",
        bold=1, outline_w=5, shadow=1, alignment=2, margin_v=150,
        pop=True, pop_scale=115, pop_ms=100,
    ),
    # 经典白字：未读灰 → 说到变白（克制干净）
    "capcut-white": dict(
        font="Noto Sans CJK SC", font_size=90,
        primary="&H00FFFFFF", secondary="&H00AAAAAA",
        outline="&H00000000", back="&H80000000",
        bold=1, outline_w=4, shadow=1, alignment=2, margin_v=150,
        pop=True, pop_scale=110, pop_ms=90,
    ),
    # 无弹跳，纯填充（兼容性最好）
    "capcut-flat": dict(
        font="Noto Sans CJK SC", font_size=90,
        primary="&H00FFFFFF", secondary="&H00AAAAAA",
        outline="&H00000000", back="&H80000000",
        bold=1, outline_w=4, shadow=1, alignment=2, margin_v=150,
        pop=False,
    ),
}

ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: CapCut,{font},{fs},{primary},{secondary},{outline},{back},{bold},0,0,0,100,100,0,0,1,{ow},{shadow},{align},60,60,{mv},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def ts(seconds: float) -> str:
    """秒 -> ASS 时间 H:MM:SS.cc"""
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int(round((seconds - int(seconds)) * 100))
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def normalize_words(data) -> list:
    """把不同 ASR 输出统一成 [{word,start,end}]。
    支持：
      1) [{"word":"x","start":0,"end":1}, ...]
      2) faster-whisper: {"segments":[{"words":[{"word","start","end"}]}]}
      3) [{"text":"x","start":0,"end":1}, ...]  (text 视为 word)
    """
    if isinstance(data, dict):
        segs = data.get("segments", [])
        words = []
        for s in segs:
            words.extend(s.get("words", []))
        data = words
    out = []
    for w in data:
        text = w.get("word") or w.get("text") or w.get("w") or ""
        text = str(text).strip()
        if not text:
            continue
        out.append({
            "word": text,
            "start": float(w.get("start", 0.0)),
            "end": float(w.get("end", w.get("start", 0.0))),
        })
    out.sort(key=lambda x: x["start"])
    return out


def chunk_lines(words: list, max_chars: int = 18, max_gap: float = 0.7,
                max_dur: float = 4.0) -> list:
    """把词切成"行"——剪映一行通常 12~18 字，遇到长停顿或超时就断行。"""
    lines, cur = [], []
    for w in words:
        if cur:
            gap = w["start"] - cur[-1]["end"]
            chars = sum(len(x["word"]) for x in cur)
            dur = w["end"] - cur[0]["start"]
            if gap > max_gap or chars + len(w["word"]) > max_chars or dur > max_dur:
                lines.append(cur)
                cur = []
        cur.append(w)
    if cur:
        lines.append(cur)
    return lines


def build_line_text(line: list, p: dict) -> str:
    """一行 -> 带 \\kf 逐词填充的 ASS 文本。"""
    parts = []
    for w in line:
        dur_cs = max(1, int(round((w["end"] - w["start"]) * 100)))
        tag = ""
        if p.get("pop"):
            sc = p.get("pop_scale", 112)
            ms = p.get("pop_ms", 90)
            # 先放大，再在 pop_ms 内回到 100%——做出"弹一下"
            tag += f"\\fscx{sc}\\fscy{sc}\\t(0,{ms},\\fscx100\\fscy100)"
        tag += f"\\kf{dur_cs}"
        parts.append("{" + tag + "}" + esc(w["word"]))
    return "".join(parts)


def esc(text: str) -> str:
    """ASS 文本转义：大括号需转义，换行用 \\N。"""
    return text.replace("{", "\\{").replace("}", "\\}").replace("\n", "\\N")


def words_to_ass(words: list, preset: str = "capcut", width: int = 1080,
                 height: int = 1920, **overrides) -> str:
    p = dict(PRESETS[preset]); p.update({k: v for k, v in overrides.items() if v is not None})
    # 以 1920 高度为基准等比缩放字号/边距/描边，保证任意分辨率观感一致
    scale = height / 1920.0
    fs = max(12, int(round(p["font_size"] * scale)))
    mv = max(8, int(round(p["margin_v"] * scale)))
    ow = max(1, int(round(p["outline_w"] * scale)))
    header = ASS_HEADER.format(
        w=width, h=height, font=p["font"], fs=fs,
        primary=p["primary"], secondary=p["secondary"],
        outline=p["outline"], back=p["back"], bold=p["bold"],
        ow=ow, shadow=p["shadow"], align=p["alignment"], mv=mv,
    )
    events = []
    for line in chunk_lines(words):
        start, end = line[0]["start"], line[-1]["end"]
        if end <= start:
            end = start + 0.5
        events.append(
            f"Dialogue: 0,{ts(start)},{ts(end)},CapCut,,0,0,0,,{build_line_text(line, p)}")
    return header + "\n".join(events) + "\n"


def main():
    ap = argparse.ArgumentParser(description="剪映级逐词高亮字幕 (ASS)")
    ap.add_argument("--input", "-i", required=True, help="word-level JSON")
    ap.add_argument("--output", "-o", default="captions.ass", help="输出 ASS")
    ap.add_argument("--preset", "-p", default="capcut",
                    choices=list(PRESETS), help="样式预设")
    ap.add_argument("--width", type=int, default=1080)
    ap.add_argument("--height", type=int, default=1920)
    ap.add_argument("--font", help="覆盖字体名")
    ap.add_argument("--font-size", type=int, help="覆盖字号")
    ap.add_argument("--no-pop", action="store_true", help="关闭弹跳动画")
    a = ap.parse_args()

    data = json.loads(Path(a.input).read_text())
    words = normalize_words(data)
    if not words:
        print("没有可用的 word 时间戳", file=sys.stderr)
        sys.exit(1)

    overrides = {"font": a.font, "font_size": a.font_size}
    if a.no_pop:
        overrides["pop"] = False
    ass = words_to_ass(words, a.preset, a.width, a.height, **overrides)
    Path(a.output).write_text(ass, encoding="utf-8")
    print(f"已生成 {a.output}：{len(words)} 个词，样式={a.preset}，分辨率={a.width}x{a.height}")


if __name__ == "__main__":
    main()
