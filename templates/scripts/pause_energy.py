#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用真实音频能量找句间停顿 —— 不依赖 ASR / SRT 的时间戳。

★ 为什么需要它（2026-10-27）
分段切点（`SEG_GRID` 的格数）必须落在句间停顿上，而停顿窗口一直是
**从 SRT 的 cue 间隙反推**的。这条路有个隐含假设：
「ASR 时间戳是准的」。一旦时间戳越界（把换气也吞进字里），
窗口本身就会偏，于是「切点落停顿」这个判断从根上不可靠。

思路直接来自开源项目 ai-jian-koubo 的 `refine_boundaries.js`
（https://github.com/lcbuaaliu/ai-jian-koubo，AGPL-3.0）：
**不信时间戳，直接看音频能量。** 本文件用 Python 重写并做了适配，
不是原样搬运（原版是为剪口播删口误设计的，我们是给分段找切点）。

算法
====
1. 解码成 8kHz 单声道 PCM，每 10ms 算一次 RMS → dB 能量包络
2. **max-pool 平滑**（±2 帧 ≈ ±20ms）：填掉字内音素的短瞬停（<20ms），
   否则会把一个音节内部误判成静音；句尾真静音是持续低能量区，填不平
3. 人声线 = `百分位(p90) − offsetDb`（全局自适应，不做逐字窗口）
4. 低于人声线且持续 ≥ minSec 的连续段 = 一个停顿
5. 输出停顿列表，并标出**每个停顿里有哪些 17 卡点**（供 SEG_GRID 直接用）

与 SRT 窗口的关系（S03 实测，906 帧 / 37.75s）
=============================================
- SRT 推出 **15 个窗口 / 16 个卡点**，能量检出 **24 段 / 23 个卡点**
- 两者**互有独有**：
  能量独有 8 个 → `493 612 714 748 765 782 833 884`
  SRT 独有 1 个 → `136`
  并集 **24 个**
- 可行方案数（**已计入 MAXLEN=192 段长上限**，±2 帧容差）：

    | 段数 | 接缝 | 仅 SRT | 仅能量 | 并集 | vs现状 |
    |---|---|---|---|---|---|
    | 5 段 | 4 | 0 | 0 | 0 | 192 上限下无解 |
    | 6 段 | 5 | 11 | 57 | 105 | **9.5×** |
    | 7 段 | 6 | 139 | 1159 | 1970 | 14.2× |
    | 8 段 | 7 | 674 | 9051 | 15214 | 22.6× |

  ⚠️ 必须带 MAXLEN 约束计数。不带的组合数会偏大两个数量级（相邻切点
  跨距 >187 帧的方案提交不上去），那种数字没有意义。
  ⚠️ 「vs现状」基准是**仅 SRT**（cut_quality 现在只用 SRT 窗口）。
  ⇒ 6 段这个常用档位，只查 SRT 只剩 11 种活路，加能量法才 105 种。
  ⇒ 段数越多 SRT 越不够用（7 段 14×、8 段 23×）。
  ⇒ 应把**并集**作为候选池，而不是二选一。

顺带验证：我们的 SRT 本身很干净
================================
S03 的 20 条 cue 里只有 **1/20 轻微越界**（cue14 尾端落在 −75dB 静音里）。
因为走 Qwen3-ASR pause 切分 + 定稿文案字符级锚点，比直接拿原始时间戳准得多。
⇒ **本工具是补漏，不是纠错。**
⚠️ 判据别写错：查「cue 起始/结尾 0.3s 是否静音」，
   **不是**查 cue 内部最静值 —— 一条字幕常跨多个 ASR 段，内部有静音是正常的。

⚠️ 边界：能量只说明「这里安静」，不说明「这里该切」。
   语义上是不是一句话的结束，仍要看 SRT 文本。所以本工具
   **只用来补漏，不用来替代** SRT 判断。

用法
====
  pause_energy.py <音频> [--min 0.15] [--offset 20] [--fps 24] [--total 906]
                 [--srt xx.srt]      # 给了就对比 SRT 窗口，输出差集 + 可行方案数
                 [--tol 2]           # 卡点容差帧数（默认 ±2；±6 是宽松档）
                 [--maxlen 192] [--overlap 5]
                 [--json out.json]   # 机读输出
                 [-h / --help]
"""
import json
import os
import re
import subprocess
import sys

FF = os.environ.get(
    "FF",
    "/home/qmr/.workbuddy/binaries/python/envs/default/lib/python3.13/"
    "site-packages/imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v7.0.2")

SR = 8000
FRAME_MS = 10
POOL = 2          # max-pool ±2 帧 ≈ ±20ms


def envelope(path):
    """返回 (每 10ms 的 dB 列表, 总时长秒)。"""
    import numpy as np
    p = subprocess.run(
        [FF, "-nostdin", "-v", "quiet", "-i", path, "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
        capture_output=True)
    if not p.stdout:
        raise SystemExit(f"解码失败：{path}\n{p.stderr.decode('utf-8', 'replace')[:500]}")
    pcm = np.frombuffer(p.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    win = SR * FRAME_MS // 1000
    n = len(pcm) // win
    rms = np.sqrt((pcm[:n * win].reshape(n, win) ** 2).mean(axis=1))
    db = np.where(rms > 0, 20 * np.log10(np.maximum(rms, 1e-10)), -90.0)

    # max-pool：填掉字内短瞬停
    out = np.empty_like(db)
    for i in range(len(db)):
        out[i] = db[max(0, i - POOL):min(len(db), i + POOL + 1)].max()
    return out, len(pcm) / SR


def find_pauses(env, min_sec=0.15, offset_db=20):
    """低于「p90 − offset」且持续 ≥min_sec 的连续段。"""
    import numpy as np
    p90 = float(np.percentile(env, 90))
    thr = p90 - offset_db
    quiet = env < thr
    out, st = [], None
    for i, q in enumerate(quiet):
        if q and st is None:
            st = i
        elif not q and st is not None:
            if (i - st) * FRAME_MS / 1000 >= min_sec:
                out.append((st * FRAME_MS / 1000, i * FRAME_MS / 1000))
            st = None
    if st is not None and (len(quiet) - st) * FRAME_MS / 1000 >= min_sec:
        out.append((st * FRAME_MS / 1000, len(quiet) * FRAME_MS / 1000))
    return out, p90, thr


def parse_srt_gaps(path, min_gap=0.20):
    txt = open(path, encoding="utf-8-sig").read()
    cues = []
    for m in re.finditer(r"(\d\d):(\d\d):(\d\d),(\d+) --> (\d\d):(\d\d):(\d\d),(\d+)", txt):
        g = m.groups()
        cues.append((int(g[0]) * 3600 + int(g[1]) * 60 + int(g[2]) + int(g[3]) / 1000,
                     int(g[4]) * 3600 + int(g[5]) * 60 + int(g[6]) + int(g[7]) / 1000))
    return [(cues[i][1], cues[i + 1][0]) for i in range(len(cues) - 1)
            if cues[i + 1][0] - cues[i][1] >= min_gap]


def grid_points(pairs, fps, total, tol=2):
    """每个停顿窗口内（±tol 帧）的 17 卡点。"""
    s = set()
    for a, b in pairs:
        for x in range(int(a * fps) - tol, int(b * fps) + tol + 1):
            if x % 17 == 0 and 0 < x < total:
                s.add(x)
    return sorted(s)


def count_layouts(cands, nseg, total, maxlen=192, overlap=5):
    """★ 数出「用这些卡点切成 nseg 段」有多少种**真的能提交**的方案。

    ⚠️ 必须带 MAXLEN 约束。不加约束的纯组合数会偏大两个数量级——
    相邻切点间隔 > maxlen-overlap 的方案采样器直接截断，等于不存在。
    """
    cs = sorted(c for c in cands if 0 < c < total)
    span = maxlen - overlap          # 187：相邻切点的最大跨距
    memo = {}

    def f(last, k):
        # k = 已放的切点数；总共要放 nseg-1 个，末尾那段靠 total 收
        if k == nseg - 1:
            return 1 if total - last <= maxlen else 0
        key = (last, k)
        if key in memo:
            return memo[key]
        t = 0
        for c in cs:
            if c <= last:
                continue
            if c - last > span:
                break
            t += f(c, k + 1)
        memo[key] = t
        return t

    return f(0, 0)


def slack_of(total, n, maxlen=192, overlap=5):
    """★ 切成 n 段还剩多少**帧余量**（自由度）—— 纯数学，与停顿点无关。

    `ΣL = total + OVERLAP×(n-1)`（每个接缝多算一次重叠），上限 `MAXLEN×n`：
        slack = MAXLEN×n − (total + OVERLAP×(n−1))
    slack < 0        → 装不下
    slack < 17×切点数 → 切点被锁死在极窄范围，**排不出全落停顿的方案**
                         （S03 5 段 slack=34=2格，要摊给 4 个切点 → 最好 3/4）
    """
    return maxlen * n - (total + overlap * (n - 1))


def min_segments(total, maxlen=192, overlap=5, need_healthy=True):
    """最少几段。need_healthy=True 时要求 slack ≥ 17×切点数（即真的排得开）。"""
    n = 1
    cuts = 34          # 兜底上限
    while n <= cuts:
        s = slack_of(total, n, maxlen, overlap)
        if s >= 0 and (not need_healthy or s >= 17 * (n - 1)):
            return n
        n += 1
    return n


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        sys.exit(__doc__)
    audio = sys.argv[1]
    arg = lambda k, d: (sys.argv[sys.argv.index(k) + 1]
                        if k in sys.argv and sys.argv.index(k) + 1 < len(sys.argv) else d)
    min_sec = float(arg("--min", 0.15))
    offset = float(arg("--offset", 20))
    fps = float(arg("--fps", 24))
    total = int(arg("--total", 906))
    srt = arg("--srt", "")
    js = arg("--json", "")
    tol = int(arg("--tol", 2))
    maxlen = int(arg("--maxlen", 192))
    overlap = int(arg("--overlap", 5))

    env, dur = envelope(audio)
    pauses, p90, thr = find_pauses(env, min_sec, offset)

    print(f"音频 {dur:.2f}s  能量包络 {len(env)} 帧（{FRAME_MS}ms/帧）")
    print(f"人声线 = p90({p90:.1f}dB) − {offset:g} = {thr:.1f}dB   "
          f"最短停顿 {min_sec:g}s")
    print(f"检出 {len(pauses)} 段停顿：")
    for a, b in pauses:
        cs = [x for x in range(int(a * fps) - tol, int(b * fps) + tol + 1)
              if x % 17 == 0 and 0 < x < total]
        tag = f"  卡点 {cs}" if cs else ""
        print(f"  {a:6.2f}-{b:6.2f}s  ({b - a:4.2f}s){tag}")

    pts_e = grid_points(pauses, fps, total, tol)
    print(f"\n能量卡点 {len(pts_e)} 个：{pts_e}")

    result = {"audio": audio, "duration": dur, "threshold_db": thr,
              "pauses": pauses, "grid_points": pts_e}

    if srt:
        sg = parse_srt_gaps(srt)
        pts_s = grid_points(sg, fps, total, tol)
        only_e = sorted(set(pts_e) - set(pts_s))
        only_s = sorted(set(pts_s) - set(pts_e))
        union = sorted(set(pts_e) | set(pts_s))
        print(f"\n对比 SRT（{len(sg)} 个窗口，{len(pts_s)} 个卡点，容差 ±{tol} 帧）：")
        print(f"  能量独有 {len(only_e)} 个：{only_e}")
        print(f"  SRT 独有 {len(only_s)} 个：{only_s}")
        print(f"  并集 {len(union)} 个：{union}")
        print(f"  ⚠️ 两者互有独有 → 候选池应该是**并集**，不是二选一")

        # ★ 带 MAXLEN 约束数可行方案 —— 这才是「有没有活路」的真实答案
        nmin = min_segments(total, maxlen, overlap)
        nraw = min_segments(total, maxlen, overlap, need_healthy=False)
        print(f"\n段数与余量（纯数学，与停顿无关）：")
        print(f"    slack(n) = MAXLEN×n − (total + 重叠×(n−1))，"
              f"slack ≥ 17×切点数 才算「排得开」")
        print(f"    {'n':<4}{'ΣL需':>8}{'上限':>8}{'slack':>8}{'17×切点':>9}  判定")
        for n in range(max(4, nraw - 1), nraw + 3):
            need = total + overlap * (n - 1)
            cap = maxlen * n
            s = slack_of(total, n, maxlen, overlap)
            v = ("装不下" if s < 0 else
                 ("排得开 ✓" if s >= 17 * (n - 1) else "装得下但切点被锁死"))
            print(f"    {n:<4}{need:>8}{cap:>8}{s:>8}{17 * (n - 1):>9}  {v}")
        if nmin > nraw:
            print(f"    ⇒ 最少 {nraw} 段能装下，但要 {nmin} 段才排得开切点")
        print(f"    ⇒ 建议段数 ≥ {nmin}")

        print(f"\n可行分段方案数（MAXLEN={maxlen}, 重叠={overlap} → 跨距上限 "
              f"{maxlen - overlap} 帧）：")
        print(f"    {'段数':<4}{'接缝':<4}{'仅SRT':>8}{'仅能量':>9}{'并集':>8}"
              f"{'vs现状':>9}")
        rows = {}
        for n in range(nraw, nraw + 4):
            a = count_layouts(pts_s, n, total, maxlen, overlap)
            b = count_layouts(pts_e, n, total, maxlen, overlap)
            u = count_layouts(union, n, total, maxlen, overlap)
            # 「vs 现状」= 并集 / 仅SRT —— 现在 cut_quality() 只用 SRT 窗口
            r = f"{u / a:.1f}x" if a else ("∞" if u else "—")
            mark = "  ← 建议" if n == nmin else ("  ← 仅能装下" if n < nmin else "")
            print(f"    {n:<4}{n - 1:<4}{a:>8}{b:>9}{u:>8}{r:>9}{mark}")
            rows[n] = {"srt": a, "energy": b, "union": u}
        zero = [n for n, v in rows.items() if v["union"] == 0]
        if zero:
            print(f"    ⚠️ {zero} 段无全落停顿的方案"
                  f"（段长被 MAXLEN 锁死，切点动不了 —— 不是停顿点不够）")
        print(f"    ⚠️ 必须带 MAXLEN 约束计数。不带约束的组合数偏大两个数量级，没有意义。")
        print(f"    ⇒ 「vs现状」= 并集 ÷ 仅SRT，即接入能量法后可行域扩大多少倍")

        print(f"\n  可直接用的 SEG_GRID 卡点（并集）：")
        print(f"    {union}")
        result.update({"srt_windows": sg, "srt_grid": pts_s,
                       "only_energy": only_e, "only_srt": only_s,
                       "union": union, "layouts": rows})

    if js:
        json.dump(result, open(js, "w"), ensure_ascii=False, indent=1)
        print(f"\n已写 {js}")


if __name__ == "__main__":
    main()
