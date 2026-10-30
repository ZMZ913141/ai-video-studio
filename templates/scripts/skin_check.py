#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""脸油/曝光检测：对比静帧与成片的面部高光分布。

脸油的量化特征：面部区域出现**小面积、极高亮度**（>230）的成片高光斑，
而不是大面积中高亮度。所以统计两个数：
- 面部区（按头顶位置推算）的 p99 亮度
- >230 像素占面部面积的比例
若成片比例显著高于静帧 → 有油光。
"""
import subprocess
import sys

import numpy as np

FF = ("/home/qmr/.workbuddy/binaries/python/envs/default/lib/python3.13/"
      "site-packages/imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v7.0.2")
W, H = 768, 1376


def _probe_wh(path):
    import os
    _p = os.path.dirname(os.path.abspath(__file__))
    if _p not in sys.path:
        sys.path.insert(0, _p)
    try:
        from probe_size import probe
        return probe(path)
    except Exception:
        return W, H


def gray(path, t=0.0):
    p = subprocess.run([FF, "-nostdin", "-v", "error", "-ss", str(t), "-i", path, "-frames:v", "1",
                        "-vf", f"scale={W}:{H}", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                       capture_output=True)
    return np.frombuffer(p.stdout, np.uint8)[:W * H].reshape(H, W).astype(np.float32)


def face_box(d):
    """头顶往下取面部区域：宽取中间 34%，高取头顶下方 8%~30%。"""
    widths = (d < 85).sum(axis=1)
    # ★ 发宽门槛必须按画布宽度换算：768 宽下的实测门槛是 165px。
    #   写死 165 在高分辨率下等于相对变窄，会把「头发边缘/眉骨阴影」误判成头顶，
    #   导致 face_box 整体上移 → 量到的是额头以上的背景或发际线，p99 全错。
    min_w = max(60, int(round(165 * W / 768)))
    ok = widths >= min_w
    run, top = 0, None
    for y in range(int(H * 0.10), H):
        if ok[y]:
            run += 1
            if run >= 12:
                top = y - 11
                break
        else:
            run = 0
    if top is None:
        return None
    y0 = int(top + 0.05 * H)
    y1 = int(top + 0.28 * H)
    x0, x1 = int(W * 0.33), int(W * 0.67)
    return d[y0:y1, x0:x1], top


def report(path, t=0.0):
    d = gray(path, t)
    r = face_box(d)
    if r is None:
        return None
    f, top = r
    return {
        "top": top / H, "mean": float(f.mean()), "p99": float(np.percentile(f, 99)),
        "p999": float(np.percentile(f, 99.9)),
        "hot": float((f > 230).mean() * 100), "vhot": float((f > 245).mean() * 100),
        "wall": float(d[max(0, top - 120):max(1, top - 40), W // 3:2 * W // 3].mean()),
    }


def main():
    # ★ 逐文件按实际分辨率重设画布（多档出片，不能写死 768x1376）
    global W, H
    print(f"{'源':28s} {'top':>6s} {'脸均':>6s} {'p99':>6s} {'p99.9':>6s} {'>230%':>7s} {'>245%':>7s} {'墙':>6s}")
    for spec in sys.argv[1:]:
        if ":" in spec:
            path, t = spec.rsplit(":", 1)
            t = float(t)
        else:
            path, t = spec, 0.0
        W, H = _probe_wh(path)
        r = report(path, t)
        name = f"{path.split('/')[-1][:20]}@{t}"
        if r is None:
            print(f"{name:28s}   定位失败")
            continue
        print(f"{name:28s} {r['top']:6.3f} {r['mean']:6.1f} {r['p99']:6.1f} "
              f"{r['p999']:6.1f} {r['hot']:7.3f} {r['vhot']:7.3f} {r['wall']:6.1f}")


if __name__ == "__main__":
    main()
