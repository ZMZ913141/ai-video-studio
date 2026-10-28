#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""字幕与人声的**同步**审计：拿 ASR 强制对齐的时间当真值，逐条比对。

为什么需要这个工具（srt_audit / srt_rhythm 都查不出）
=====================================================
- `srt_audit.py` 只判「cue 边界是否落在静音里」。S03 的错位恰恰**满足**这一点：
  字幕比人声早 1.5s，边界照样落在静音里，它报 0 问题。
- `srt_rhythm.py` 判「一条字幕内是否横跨多个停顿」。它只能发现「挂太久」，
  发现不了「整体错位一条」——因为错位后每条内部的碎片数反而可能更少。

真正的判据只有一条：**这条字幕里的字，ASR 说它们是在什么时候念的。**
  偏移 = cue 起点 − ASR 中该句首字的发声时刻
  |偏移| > 0.35s 即为错位（人眼在 0.2s 就能察觉）

用法
====
  srt_sync_audit.py <音频> <srт> <文案txt> [--gap 0.12] [--tol 0.35]
"""
import os
import re
import sys

# 验收工具在 04_验收工具/，生产脚本在 03_生产脚本/，必须把后者加进 sys.path
# 才能复用 make_srt_asr 的 ASR 调用与字符对齐（口径必须完全一致，
# 否则审计结果没有可比性）。
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROD = os.path.normpath(os.path.join(_HERE, "..", "03_生产脚本"))
if _PROD not in sys.path:
    sys.path.insert(0, _PROD)

import make_srt_asr as M       # noqa: E402  复用 ASR 调用与字符对齐，保证口径一致


def parse_srt(p):
    txt = open(p, encoding="utf-8-sig").read()
    out = []
    for m in re.finditer(
            r"(\d+)\n(\d\d):(\d\d):(\d\d),(\d+) --> (\d\d):(\d\d):(\d\d),(\d+)\n(.*?)(?:\n\n|$)",
            txt, re.S):
        g = m.groups()
        t0 = int(g[1]) * 3600 + int(g[2]) * 60 + int(g[3]) + int(g[4]) / 1000
        t1 = int(g[5]) * 3600 + int(g[6]) * 60 + int(g[7]) + int(g[8]) / 1000
        out.append((t0, t1, g[9].strip().replace("\n", "")))
    return out


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    audio, srt, txt = args[0], args[1], args[2]
    gap = float(sys.argv[sys.argv.index("--gap") + 1]) if "--gap" in sys.argv else 0.12
    tol = float(sys.argv[sys.argv.index("--tol") + 1]) if "--tol" in sys.argv else 0.35

    script = re.sub(r"\s*\n+\s*", "", open(txt, encoding="utf-8-sig").read().strip())
    cues = parse_srt(srt)
    segs = M.asr_segments(audio, gap)
    anchors = M.build_anchor(segs, script)

    # 原文下标 -> 归一化下标
    norm_pos, k = [None] * (len(script) + 1), 0
    for i, ch in enumerate(script):
        norm_pos[i] = k
        if M.spoken_chars(ch):
            k += 1
    norm_pos[len(script)] = k

    # 每条 cue 在文案里的位置（按顺序消耗字数）
    rows, pos = [], 0
    for t0, t1, disp in cues:
        n = len(M.spoken_chars(disp))
        while pos < len(script) and not M.spoken_chars(script[pos]):
            pos += 1
        s = pos
        cons = 0
        while pos < len(script) and cons < n:
            if M.spoken_chars(script[pos]):
                cons += 1
            pos += 1
        ns, ne = norm_pos[s], norm_pos[pos]
        asr0 = M.time_at(anchors, ns, "start")
        asr1 = M.time_at(anchors, ne, "end")
        rows.append(dict(t0=t0, t1=t1, asr0=asr0, asr1=asr1,
                         d0=t0 - asr0, d1=t1 - asr1, txt=disp))

    print(f"字幕 {len(cues)} 条   ASR 短语段 {len(segs)} 个   "
          f"容忍 ±{tol}s\n")
    print(f"{'#':>3s} {'字幕区间':>17s} {'ASR 真值':>17s} {'头偏':>7s} {'尾偏':>7s}  文本")
    bad = 0
    for i, r in enumerate(rows, 1):
        flag = ""
        if abs(r["d0"]) > tol or abs(r["d1"]) > tol:
            flag = "  ❌ 错位"
            bad += 1
        elif abs(r["d0"]) > tol * 0.6 or abs(r["d1"]) > tol * 0.6:
            flag = "  ⚠️ 临界"
        print(f"{i:3d} {r['t0']:7.2f}-{r['t1']:7.2f} "
              f"{r['asr0']:7.2f}-{r['asr1']:7.2f} "
              f"{r['d0']:+7.3f} {r['d1']:+7.3f}  {r['txt']}{flag}")

    d0 = [r["d0"] for r in rows]
    d1 = [r["d1"] for r in rows]
    print(f"\n头偏 中位 {sorted(d0)[len(d0)//2]:+.3f}s  "
          f"最大 {max(d0, key=abs):+.3f}s")
    print(f"尾偏 中位 {sorted(d1)[len(d1)//2]:+.3f}s  "
          f"最大 {max(d1, key=abs):+.3f}s")
    print(f"\n结论：{'✅ 全部同步（±%.2fs 内）' % tol if bad == 0 else '❌ %d 条错位' % bad}")


if __name__ == "__main__":
    main()
