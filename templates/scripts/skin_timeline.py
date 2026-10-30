#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""逐段扫描脸油/身份漂移，找出「越到后面越油」到底从第几秒开始。

为什么需要这个工具
==================
`skin_check.py` 只测**你给的那一帧**。S03 验收时我只跑了 `@0.0`，
等于只看开头就宣布「成片比原片更低」—— 用户的反馈直接证明这是漏检。
肤色/高光是随时间漂移的，单帧采样在结构上就看不见趋势。

做法
====
按固定间隔全片抽帧，每帧取面部区，输出：
- 脸均 / p99 / >230%  —— 油光随时间的曲线
- 与**首帧**的直方图相关度 corr —— 越掉越不像初始帧
- 与**静帧**的 corr —— 与定版形象的偏移
最后按段落（可传分段边界）求均值，直接告出哪一段开始变差。

用法
  skin_timeline.py <成片> [--ref 静帧.png] [--step 0.5] [--segs 7.29,14.38,19.33,26.42,31.38]
"""
import subprocess
import sys

import numpy as np

FF = ("/home/qmr/.workbuddy/binaries/python/envs/default/lib/python3.13/"
      "site-packages/imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v7.0.2")

# 分析画布：默认按 S01/S02/S03 的 768x1376，main() 会按实际视频覆盖。
# ⚠️ 必须用**实际尺寸**做画布，不能一律缩到 768x1376 ——
# 896x1568 与 768x1376 的宽高比不同（0.5714 vs 0.5581），
# 硬缩会横向轻微拉伸，face_box 的 0.33–0.67W 区间和人脸实际位置对不上。
W, H = 768, 1376


def _probe_wh(path):
    import os
    import sys as _s
    _s.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        from probe_size import probe
        return probe(path)
    except Exception:
        return W, H


def gray_frame(path, t):
    p = subprocess.run([FF, "-nostdin", "-v", "error", "-ss", f"{t:.3f}", "-i", path,
                        "-frames:v", "1", "-vf", f"scale={W}:{H}",
                        "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"],
                       capture_output=True)
    b = np.frombuffer(p.stdout, np.uint8)
    if b.size < W * H:
        return None
    return b[:W * H].reshape(H, W).astype(np.float32)


def gray_still(path):
    p = subprocess.run([FF, "-nostdin", "-v", "error", "-i", path, "-frames:v", "1",
                        "-vf", f"scale={W}:{H}", "-f", "rawvideo",
                        "-pix_fmt", "gray", "pipe:1"], capture_output=True)
    b = np.frombuffer(p.stdout, np.uint8)
    return b[:W * H].reshape(H, W).astype(np.float32) if b.size >= W * H else None


def face_box(d):
    """取面部框。头顶定位改用**行均亮度最暗带**，不再依赖"暗带宽度"。

    为什么放弃旧判据（两个坑都踩过，别再退回去）
    ------------------------------------------
    旧判据是「暗像素(<85)宽度≥165 的连续 12 行 = 头发带」。它隐含假设
    **墙比头发亮**，但实测墙的行均从 t=1s 的 104 掉到 t=36s 的 86，
    头发也在 85 以下 → 后段墙本身就成了"暗带"，宽度从 0 涨到 649，
    判据在 y=0.10H 就成立，top 被钉死在扫描下界，面部框落到墙上，
    量出「脸均 84 / p99 190」这种假数据，看起来像曝光崩了。

    正解：头发是全片最暗的水平条带，改用**行均亮度的局部最小值**定位：
    在 0.10H–0.40H 内找行均最小的连续区域，取其顶部。
    这个判据只依赖"头发比脸暗"，与墙面绝对亮度无关，全片稳定。
    """
    y_lo, y_hi = int(H * 0.10), int(H * 0.40)
    rowmean = d.mean(axis=1)
    band = rowmean[y_lo:y_hi]
    if band.size == 0:
        return d[int(H * 0.24):int(H * 0.32), int(W * 0.33):int(W * 0.67)], int(H * 0.24)
    k = 11                                  # 平滑，避免单行噪声
    sm = np.convolve(np.pad(band, (k // 2, k // 2), mode="edge"),
                     np.ones(k) / k, mode="same")[k // 2:k // 2 + band.size]
    dark_y = y_lo + int(np.argmin(sm))
    # 最暗行往上退 12px 作为发顶（发际线在暗带中部）
    top = max(y_lo, dark_y - 12)
    y0 = int(top + 0.05 * H)
    y1 = min(H, int(top + 0.28 * H))
    return d[y0:y1, int(W * 0.33):int(W * 0.67)], top


def hist_corr(a, b):
    """两帧灰度直方图的相关系数，衡量"像不像"。"""
    ha, _ = np.histogram(a, bins=64, range=(0, 256), density=True)
    hb, _ = np.histogram(b, bins=64, range=(0, 256), density=True)
    ha, hb = ha - ha.mean(), hb - hb.mean()
    den = np.sqrt((ha ** 2).sum() * (hb ** 2).sum())
    return float((ha * hb).sum() / den) if den else 0.0


def dur(path):
    p = subprocess.run([FF, "-nostdin", "-hide_banner", "-i", path], capture_output=True)
    import re
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", p.stderr.decode())
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def main():
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    vid = a[0]
    ref = None
    if "--ref" in sys.argv:
        ref = gray_still(sys.argv[sys.argv.index("--ref") + 1])
    step = float(sys.argv[sys.argv.index("--step") + 1]) if "--step" in sys.argv else 0.5
    segs = None
    if "--segs" in sys.argv:
        segs = [float(x) for x in sys.argv[sys.argv.index("--segs") + 1].split(",")]

    # ★ 按实际分辨率设分析画布（多档出片，不能写死 768x1376）
    global W, H
    W, H = _probe_wh(vid)
    print(f"分析画布 {W}x{H}")

    total = dur(vid)
    ts = []
    t = 0.0
    while t < total - 0.05:
        ts.append(t)
        t += step

    rows = []
    first = None
    for t in ts:
        d = gray_frame(vid, t)
        if d is None:
            continue
        f, top = face_box(d)
        if first is None:
            first = f
        rows.append({
            "t": t, "top": top / H, "mean": float(f.mean()),
            "p99": float(np.percentile(f, 99)),
            "hot": float((f > 230).mean() * 100),
            "vhot": float((f > 245).mean() * 100),
            "corr0": hist_corr(f, first),
            "corrR": hist_corr(f, ref) if ref is not None else float("nan"),
        })

    if not rows:
        raise SystemExit("抽帧失败")

    print(f"共 {len(rows)} 帧（每 {step}s）  时长 {total:.2f}s")
    if segs:
        print(f"分段边界 {segs}")
    print()
    hdr = f"{'t':>6s} {'top':>6s} {'脸均':>6s} {'p99':>6s} {'>230%':>7s} {'>245%':>7s} {'corr首帧':>9s}"
    if ref is not None:
        hdr += f" {'corr静帧':>9s}"
    print(hdr)
    for r in rows:
        line = (f"{r['t']:6.2f} {r['top']:6.3f} {r['mean']:6.1f} {r['p99']:6.1f} "
                f"{r['hot']:7.3f} {r['vhot']:7.3f} {r['corr0']:9.4f}")
        if ref is not None:
            line += f" {r['corrR']:9.4f}"
        print(line)

    # 趋势：首 20% vs 末 20%
    k = max(1, len(rows) // 5)
    head, tail = rows[:k], rows[-k:]
    print("\n" + "=" * 64)
    print(f"{'':16s} {'脸均':>7s} {'p99':>7s} {'>230%':>8s} {'corr首帧':>9s}")
    for label, grp in (("开头 20%", head), ("结尾 20%", tail)):
        m = lambda key: float(np.mean([r[key] for r in grp]))
        print(f"{label:16s} {m('mean'):7.1f} {m('p99'):7.1f} {m('hot'):8.3f} {m('corr0'):9.4f}")
    dm = np.mean([r["mean"] for r in tail]) - np.mean([r["mean"] for r in head])
    dh = np.mean([r["hot"] for r in tail]) - np.mean([r["hot"] for r in head])
    dc = np.mean([r["corr0"] for r in tail]) - np.mean([r["corr0"] for r in head])
    print(f"{'末−首 差值':16s} {dm:+7.1f} {'':7s} {dh:+8.3f} {dc:+9.4f}")
    verdict = []
    if dh > 0.30:
        verdict.append(f"❌ 后段明显变油（>230% 涨 {dh:+.2f} 个百分点）")
    if dm > 6:
        verdict.append(f"❌ 后段整体变亮（脸均 {dm:+.1f}）")
    if dc < -0.02:
        verdict.append(f"❌ 后段偏离首帧（corr 掉 {dc:+.3f}）")
    print("\n结论：" + ("；".join(verdict) if verdict else "✅ 全片肤色稳定"))

    if segs:
        print("\n按分段统计：")
        bounds = [0.0] + segs + [total]
        print(f"{'段':>3s} {'区间':>16s} {'脸均':>7s} {'p99':>7s} {'>230%':>8s} {'corr首帧':>9s}")
        for i in range(len(bounds) - 1):
            lo, hi = bounds[i], bounds[i + 1]
            grp = [r for r in rows if lo <= r["t"] < hi]
            if not grp:
                continue
            m = lambda key: float(np.mean([r[key] for r in grp]))
            print(f"{i+1:3d} {lo:6.2f}-{hi:6.2f}s {m('mean'):7.1f} {m('p99'):7.1f} "
                  f"{m('hot'):8.3f} {m('corr0'):9.4f}")


if __name__ == "__main__":
    main()
