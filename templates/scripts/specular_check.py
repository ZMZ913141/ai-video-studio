#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""高光成斑检测：区分「整脸均匀亮」和「局部爆白斑」。

为什么不能只看 >230%
====================
`skin_check.py` 只看 >230 的**面积占比**。但「油腻」的视觉成因不是面积，
而是**高光收缩成少数几个亮点**——用户说「照在脸上有点过曝」，
实际测到的是：脸均从 121.5 **掉到** 117.5（整体反而变暗），
p99 却从 210 涨到 227。整幅没有变亮，是亮的地方更集中了。
面积占比对这种变化的敏感度很低（0.054% → 0.611% 只差半个百分点）。

真正该量的
==========
把面部区按 8x8 网格切块，逐块算 p95，然后看两件事：

1. **热点对比度 spark**：最亮块 p95 与面部中位块 p95 的差。
   均匀柔光下这个差值小；出现油光斑时它会跳起来。
2. **热点集中度 top3share**：最亮 3 块占全部「超中位数」亮度的比例。
   柔光漫开时亮部分散，油光时集中在额头/鼻梁。

判据（对同一片段的**首帧**取基准，看末帧相对漂移）：
  - Δspark  ≤ +8   （热点相对对比度不该显著加剧）
  - Δhot%   ≤ +0.30 个百分点（顺带兜住绝对过曝面积）

用法
  specular_check.py <视频> [--ts 0.5,10,20,30,37] [--ref 静帧.png]
  specular_check.py <视频> --trend [--step 1.0]     # 全片趋势，找劣化起点
"""
import os
import subprocess
import sys

import numpy as np

FF = ("/home/qmr/.workbuddy/binaries/python/envs/default/lib/python3.13/"
      "site-packages/imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v7.0.2")

# 分析画布：必须用**实际**分辨率（宽高比不同会横向拉伸，
# 0.33–0.67W 的人脸窗口就对不上）。main() 里按素材覆盖。
W, H = 768, 1376

GRID = 8          # 面部区切 GRID x GRID 块


def _probe_wh(path):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        from probe_size import probe
        return probe(path)
    except Exception:
        return W, H


def gray(path, t):
    p = subprocess.run([FF, "-v", "error", "-ss", f"{t:.3f}", "-i", path,
                        "-frames:v", "1", "-vf", f"scale={W}:{H}",
                        "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                       capture_output=True)
    b = np.frombuffer(p.stdout, np.uint8)
    if b.size < W * H:
        return None
    return b[:W * H].reshape(H, W).astype(np.float32)


def face_box(d):
    """头顶定位：行均亮度最暗带（头发是画面里最暗的水平条带）。

    与 `skin_timeline.py` 同一判据 —— 那个是唯一在 768/896 两档都稳定的。
    不使用「暗像素宽度>=阈值」，因为后段墙面本身变暗会让它崩（见 skin_timeline 注释）。
    """
    y_lo, y_hi = int(H * .10), int(H * .40)
    band = d.mean(axis=1)[y_lo:y_hi]
    if band.size == 0:
        top = int(H * .24)
    else:
        k = 11
        sm = np.convolve(np.pad(band, (k // 2, k // 2), mode="edge"),
                         np.ones(k) / k, mode="same")[k // 2:k // 2 + band.size]
        top = max(y_lo, y_lo + int(np.argmin(sm)) - 12)
    y0 = int(top + .03 * H)
    y1 = min(H, int(top + .34 * H))          # 覆盖额头到下巴下部
    x0, x1 = int(W * .30), int(W * .70)
    return d[y0:y1, x0:x1]


def metrics(f):
    """返回 spark / top3share / p99 / hot%，以及最亮块坐标（便于人工核对）。"""
    h, w = f.shape
    bh, bw = h // GRID, w // GRID
    if bh < 4 or bw < 4:
        return None
    blocks = []
    for gy in range(GRID):
        for gx in range(GRID):
            blk = f[gy * bh:(gy + 1) * bh, gx * bw:(gx + 1) * bw]
            blocks.append((float(np.percentile(blk, 95)), gy, gx))
    blocks.sort(reverse=True)
    p95s = np.array([b[0] for b in blocks])
    med = float(np.median(p95s))
    spark = float(p95s.max() - med)

    # 集中度：最亮 3 块超出中位数的量，占全部超出量的比例
    excess = np.clip(p95s - med, 0, None)
    tot = excess.sum()
    top3 = excess[:3].sum()
    top3share = float(top3 / tot) if tot > 1e-6 else 0.0

    return {
        "spark": spark,
        "top3share": top3share,
        "p99": float(np.percentile(f, 99)),
        "hot": float((f > 230).mean() * 100),
        "peak_gy": blocks[0][1], "peak_gx": blocks[0][2],
        "med": med, "max": float(p95s.max()),
    }


def one(path, t):
    d = gray(path, t)
    if d is None:
        return None
    f = face_box(d)
    return metrics(f)


def duration(path):
    import re
    p = subprocess.run([FF, "-hide_banner", "-i", path], capture_output=True)
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", p.stderr.decode())
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def main():
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    if not a:
        raise SystemExit(__doc__)
    vid = a[0]

    global W, H
    W, H = _probe_wh(vid)
    print(f"分析画布 {W}x{H}  网格 {GRID}x{GRID}")

    trend = "--trend" in sys.argv
    step = float(sys.argv[sys.argv.index("--step") + 1]) if "--step" in sys.argv else 1.0
    if "--ts" in sys.argv:
        ts = [float(x) for x in sys.argv[sys.argv.index("--ts") + 1].split(",")]
    elif trend:
        total = duration(vid)
        ts, t = [], 0.0
        while t < total - 0.05:
            ts.append(t)
            t += step
    else:
        ts = [0.5, 10.0, 20.0, 30.0, 37.0]

    print(f"{'t':>6} {'spark':>7} {'top3%':>7} {'max块p95':>9} {'中位块':>7} "
          f"{'p99':>6} {'>230%':>7} {'最亮块':>8}")
    rows = []
    for t in ts:
        m = one(vid, t)
        if m is None:
            continue
        rows.append((t, m))
        print(f"{t:6.2f} {m['spark']:7.1f} {m['top3share']*100:7.1f} {m['max']:9.1f} "
              f"{m['med']:7.1f} {m['p99']:6.1f} {m['hot']:7.3f} "
              f"({m['peak_gx']},{m['peak_gy']})")

    if len(rows) < 2:
        return

    # 趋势判定：首帧 vs 末帧（trend 模式用首/末 10% 均值更稳）
    if trend and len(rows) >= 10:
        k = max(2, len(rows) // 10)
        head = [m for _, m in rows[:k]]
        tail = [m for _, m in rows[-k:]]
        avg = lambda g, key: float(np.mean([x[key] for x in g]))
        base, last = {key: avg(head, key) for key in ("spark", "hot", "p99")}, \
                     {key: avg(tail, key) for key in ("spark", "hot", "p99")}
        lab0, lab1 = "首10%", "末10%"
    else:
        base, last = rows[0][1], rows[-1][1]
        lab0, lab1 = f"{rows[0][0]:.1f}s", f"{rows[-1][0]:.1f}s"

    dspark = last["spark"] - base["spark"]
    dhot = last["hot"] - base["hot"]
    print("\n" + "=" * 60)
    print(f"{'':10} {'spark':>8} {'>230%':>8} {'p99':>7}")
    print(f"{lab0:10} {base['spark']:8.1f} {base['hot']:8.3f} {base['p99']:7.1f}")
    print(f"{lab1:10} {last['spark']:8.1f} {last['hot']:8.3f} {last['p99']:7.1f}")
    print(f"{'末−首':10} {dspark:+8.1f} {dhot:+8.3f} {last['p99']-base['p99']:+7.1f}")

    bad = []
    if dspark > 8:
        bad.append(f"❌ 热点对比度加剧（spark {dspark:+.1f}）")
    if dhot > 0.30:
        bad.append(f"❌ 过曝面积扩大（>230% {dhot:+.3f} 个百分点）")
    print("\n结论：" + ("；".join(bad) if bad else "✅ 高光分布稳定，无成斑趋势"))


if __name__ == "__main__":
    main()
