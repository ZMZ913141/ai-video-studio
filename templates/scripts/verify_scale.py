#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""景别量化验证 v2：用「黑发定位头顶」判断是否推近。

原理：画面中心列上，墙是亮的（lum>150），头发是最暗的（lum<85）。
沿中心列从上往下扫，第一个长度>=8 行的暗段起点 = 头顶 → top_frac = 头顶行/总高。
推近 → 头顶上移 → top_frac 变小；锁机位 → top_frac 恒定。
hair_frac = 该暗段长度/总高，随头变大而变大，作辅助交叉验证。
"""
import subprocess, sys, os, tempfile
import numpy as np
from PIL import Image

FF = "/home/qmr/.workbuddy/binaries/python/envs/default/lib/python3.13/site-packages/imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v7.0.2"
DEVNULL = subprocess.DEVNULL
DARK = 85        # 头发亮度上限
GAP = 3          # 允许的间断行（高光/反光）
MINRUN = 8       # 头发段最短行数


def frame_at(vid, t, out):
    subprocess.run([FF, "-nostdin", "-y", "-i", vid, "-ss", str(t), "-frames:v", "1", out],
                   check=True, stdout=DEVNULL, stderr=DEVNULL)


def metrics(png):
    im = np.asarray(Image.open(png).convert("RGB")).astype(float)
    H, W, _ = im.shape
    lum = 0.299 * im[..., 0] + 0.587 * im[..., 1] + 0.114 * im[..., 2]
    cx = W // 2
    band = lum[:, cx - 12:cx + 13].mean(axis=1)
    dark = band < DARK
    idx = np.where(dark)[0]
    groups = []
    if len(idx):
        s = p = idx[0]
        for i in idx[1:]:
            if i - p <= GAP:
                p = i
            else:
                groups.append((s, p)); s = p = i
        groups.append((s, p))
    groups = [g for g in groups if g[1] - g[0] >= MINRUN and g[0] > H * 0.03]
    if not groups:
        return None
    top, bot = groups[0]
    return dict(top_frac=round(top / H, 4), hair_frac=round((bot - top) / H, 4),
                head_px=bot - top)


def report(label, vid, ts_list):
    print(f"\n=== {label} ===")
    print(f"{'t(s)':>7}  {'top_frac':>9}  {'hair_frac':>10}  {'head_px':>7}")
    rows = []
    with tempfile.TemporaryDirectory() as td:
        for t in ts_list:
            p = os.path.join(td, f"f_{t}.png")
            frame_at(vid, t, p)
            m = metrics(p)
            rows.append((t, m))
            if m is None:
                print(f"{t:>7}  (未检出头顶)")
            else:
                print(f"{t:>7}  {m['top_frac']:>9}  {m['hair_frac']:>10}  {m['head_px']:>7}")
    vals = [m["top_frac"] for _, m in rows if m]
    hts = [m["head_px"] for _, m in rows if m]
    if len(vals) >= 2:
        d = max(vals) - min(vals)
        print(f"top_frac Δ={d:+.4f} → {'稳定（无推拉）' if d < 0.012 else '镜头在动'}")
        print(f"head_px  Δ={max(hts)-min(hts):+.0f}px "
              f"({(max(hts)/min(hts)-1)*100:+.1f}% 头围变化)")
    return rows


if __name__ == "__main__":
    ts_env = os.environ.get("TS")
    if ts_env:
        ts = [float(x) for x in ts_env.split(",") if x != ""]
    else:
        ts = [0.0, 0.9, 1.83, 2.7, 3.625, 4.5, 5.42, 6.3, 7.25]
    for arg in sys.argv[1:]:
        if arg.lower().endswith((".png", ".jpg", ".jpeg")):
            report(os.path.basename(arg), arg, [0.0])   # 图片只取 1 帧
        else:
            report(os.path.basename(arg), arg, ts)
