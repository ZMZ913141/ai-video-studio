#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全片安全区审计：逐帧减原片取字幕像素，量真实包围盒。

为什么必须用「减原片」而不是「找亮像素」
====================================
白衬衫亮度 180、墙面 113——找亮像素会把衬衫、墙、灯全当成字幕，
包围盒直接铺满画面。只有跟**同一帧原片**做差，差出来的才是烧上去的字。

⚠️ 必须顺序解码，不能用 -ss 分别 seek
=====================================
烧录版和原片是**两个不同编码**（crf18 重新编码 → GOP 结构不同）。
对两个文件各自 `-ss T` 会落到不同的帧上，等于拿第 N 帧比第 N±1 帧，
人物一抬手的帧就整片差异 → 报出 y646–1296 这种"越界"，纯属假阳性。
所以这里改成两个进程**同时顺序解码**，严格第 i 帧比第 i 帧。

判据（768×1376，按 1080×1920 平台基准换算 x0.711 / y0.717）
=========================================================
- 顶部 y < SAFE_TOP(107)       → 状态栏/顶部搜索栏会压字
- 底部 y > SAFE_BOTTOM(1118)   → 昵称/文案/进度条会压字
- 右侧 x > SAFE_RIGHT(661)     → 点赞/评论/分享竖列会压字
- 左侧 x < SAFE_LEFT(42)       → 太贴边，折叠时被裁

用法:
  safe_zone_audit.py <成品mp4> <原片mp4> [--every 12]
"""
import os
import subprocess
import sys

import numpy as np

APP = "/var/lib/flatpak/app/org.kde.kdenlive/current/active/files"
RT = "/var/lib/flatpak/runtime/org.kde.Platform/x86_64/6.11/active/files"
LIB = f"{APP}/lib:{APP}/lib/x86_64-linux-gnu:{RT}/lib:{RT}/lib/x86_64-linux-gnu"
LOADER = f"{RT}/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2"
FFMPEG = f"{APP}/bin/ffmpeg"
FFPROBE = f"{APP}/bin/ffprobe"
ENV = {"LD_LIBRARY_PATH": LIB, "PATH": "/usr/bin:/bin", "HOME": os.path.expanduser("~")}

# ---------- 分辨率与安全区 ----------
# ⚠️ 这里原来写死 `W, H = 768, 1376` + 绝对像素安全区。
# 2026-10-27 起出片支持多档分辨率（1.0MP=768x1376 / 1.33MP=896x1568 / 2.0MP=1088x1920），
# 写死坐标换档后**全部失效且不会报错** —— 会给出一个看起来正常、实际错位的结论。
# 所以安全区改成**相对比例**（以 768x1376 实测值为基准换算），分辨率由实际视频决定。
BASE_W, BASE_H = 768, 1376
SAFE_TOP_R = 107 / BASE_H        # 0.0778
SAFE_BOTTOM_R = 1118 / BASE_H    # 0.8125
SAFE_LEFT_R = 42 / BASE_W        # 0.0547
SAFE_RIGHT_R = 661 / BASE_W      # 0.8607

W, H = BASE_W, BASE_H            # 运行期由 main() 用实际尺寸覆盖
SAFE_TOP = SAFE_BOTTOM = SAFE_LEFT = SAFE_RIGHT = 0


def apply_size(w, h):
    """按实际分辨率换算安全区。会同时更新模块级变量。"""
    global W, H, SAFE_TOP, SAFE_BOTTOM, SAFE_LEFT, SAFE_RIGHT
    W, H = w, h
    SAFE_TOP = int(round(SAFE_TOP_R * h))
    SAFE_BOTTOM = int(round(SAFE_BOTTOM_R * h))
    SAFE_LEFT = int(round(SAFE_LEFT_R * w))
    SAFE_RIGHT = int(round(SAFE_RIGHT_R * w))
    return W, H


DIFF_TH = 26          # 差分阈值：字幕描边和底色的差远大于此
MIN_PIX = 150         # 一帧至少这么多差异像素才算有字幕（防编码噪声）

# ---------- 容忍度（2026-10-27 用户定板：安全区相对就好，不用太严格）----------
# 原来写成硬门槛，越 1px 就判整项失败，等于用一把过严的尺子卡自己。
# 改成三级：
#   OK      ≤ TOL_SOFT  完全没问题（平台 UI 是浮层，几像素内互相让开很正常）
#   WARN    ≤ TOL_HARD  轻微超出，可接受，记录但不拦
#   FAIL    >  TOL_HARD 真的会字被压住，必须改
TOL_SOFT = 4          # 4px 内视为精确贴边（测量误差量级）
TOL_HARD = 16         # 16px 内仍可接受（约 1 个字宽的 1/3，UI 浮层会遮但不影响读）
# 底部额外放宽：抖音底部文案区本身是半透明浮层，字幕略微进入不致命
TOL_BOTTOM_HARD = 24


def probe(video, entries):
    r = subprocess.run([LOADER, "--library-path", LIB, FFPROBE, "-v", "error",
                        "-select_streams", "v:0", "-show_entries", entries,
                        "-of", "csv=p=0", video], env=ENV, capture_output=True)
    return r.stdout.decode().strip()


def stream_gray(video):
    """顺序解码成灰度原始帧流，逐帧 yield (H,W) int16。"""
    p = subprocess.Popen(
        [LOADER, "--library-path", LIB, FFMPEG, "-v", "error", "-i", video,
         "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"],
        env=ENV, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    n = W * H
    try:
        while True:
            buf = p.stdout.read(n)
            if len(buf) < n:
                break
            yield np.frombuffer(buf, dtype=np.uint8).reshape(H, W).astype(np.int16)
    finally:
        p.stdout.close()
        p.wait()


def bbox(mask, min_row=6, min_col=6):
    """取包围盒，但**先滤掉零散噪声点**。

    坑：crf18 重编码在人物抬手/画面边缘会产生零星 1–3 px 的差异点，
    裸 bbox 会被这几个坏点拉到 y1190（画面底），误报「越界」。
    解法：只有某一行/列有 ≥min_row/min_col 个差异像素，才认为它是字幕的一部分。
    实测字幕行的差异像素密度在 100+ / 行，噪声行 ≤3，门槛 6 分得很干净。
    """
    rows = np.where(mask.sum(1) >= min_row)[0]
    if rows.size == 0:
        return None
    keep_r = np.zeros(mask.shape[0], bool)
    keep_r[rows] = True
    sub = mask[keep_r]
    cols = np.where(sub.sum(0) >= min_col)[0]
    if cols.size == 0:
        return None
    return int(cols.min()), int(rows.min()), int(cols.max()), int(rows.max())


def audit(dst, src, every=1):
    fps = float(probe(dst, "stream=r_frame_rate").split("/")[0]) / \
        float(probe(dst, "stream=r_frame_rate").split("/")[1])
    # ★ 分辨率必须从实际成品读，不能沿用 768x1376 的写死值
    wh = probe(dst, "stream=width,height").split(",")
    if len(wh) >= 2 and wh[0].strip().isdigit() and wh[1].strip().isdigit():
        apply_size(int(wh[0]), int(wh[1]))
    print(f"成品 {os.path.basename(dst)}  {W}x{H}")
    print(f"顺序解码对齐（每 {every} 帧取 1），帧率 {fps:g}")

    rows, i = [], 0
    gs, gd = stream_gray(src), stream_gray(dst)
    while True:
        try:
            a = next(gs)
            b = next(gd)
        except StopIteration:
            break
        if i % every == 0:
            m = np.abs(a - b) > DIFF_TH
            s = int(m.sum())
            if s >= MIN_PIX:
                bb = bbox(m)
                # 噪声帧（张力点滤完什么都不剩）直接丢掉
                if bb and (bb[2] - bb[0]) >= 40 and (bb[3] - bb[1]) >= 12:
                    rows.append((i, bb, s))
        i += 1
    gs.close()
    gd.close()

    if not rows:
        print("❌ 没有检测到任何字幕像素 —— 怀疑没烧上")
        return 1

    gx0 = min(r[1][0] for r in rows)
    gy0 = min(r[1][1] for r in rows)
    gx1 = max(r[1][2] for r in rows)
    gy1 = max(r[1][3] for r in rows)
    print(f"共解码 {i} 帧，有字幕 {len(rows)} 帧")
    print(f"字幕总包围盒  x {gx0}–{gx1}   y {gy0}–{gy1}")
    print(f"安全区        x {SAFE_LEFT}–{SAFE_RIGHT}   y {SAFE_TOP}–{SAFE_BOTTOM}")
    print(f"容忍度        轻微 ≤{TOL_SOFT}px  可接受 ≤{TOL_HARD}px"
          f"（底部 ≤{TOL_BOTTOM_HARD}px）")

    # 逐边判级：over = 越界像素数（正数是超出，负数是余量）
    edges = [
        (f"顶部 y<{SAFE_TOP}（状态栏）",  SAFE_TOP - gy0,    TOL_HARD),
        (f"底部 y>{SAFE_BOTTOM}（昵称文案）", gy1 - SAFE_BOTTOM, TOL_BOTTOM_HARD),
        (f"右侧 x>{SAFE_RIGHT}（点赞竖列）", gx1 - SAFE_RIGHT, TOL_HARD),
        (f"左侧 x<{SAFE_LEFT}（贴边裁切）",  SAFE_LEFT - gx0,   TOL_HARD),
    ]
    warn = fail = 0
    for label, over, hard in edges:
        if over <= 0:
            mark, note = "✅", f"余量 {-over}px"
        elif over <= TOL_SOFT:
            mark, note = "✅", f"越界 {over}px（测量误差级）"
        elif over <= hard:
            mark, note = "⚠️", f"越界 {over}px（可接受）"
            warn += 1
        else:
            mark, note = "❌", f"越界 {over}px（超容忍 {hard}px）"
            fail += 1
        print(f"  {mark} {label}  {note}")

    # 只有 FAIL 级才列具体帧
    if fail:
        def bad_edge(bb):
            return (SAFE_TOP - bb[1] > TOL_HARD
                    or bb[3] - SAFE_BOTTOM > TOL_BOTTOM_HARD
                    or bb[2] - SAFE_RIGHT > TOL_HARD
                    or SAFE_LEFT - bb[0] > TOL_HARD)
        print("严重越界帧:")
        for n, bb, _ in rows:
            if bad_edge(bb):
                print(f"  第{n}帧 ({n/fps:.2f}s)  x {bb[0]}–{bb[2]}  y {bb[1]}–{bb[3]}")

    print(f"\n余量: 上 {gy0 - SAFE_TOP:+d}px  下 {SAFE_BOTTOM - gy1:+d}px  "
          f"左 {gx0 - SAFE_LEFT:+d}px  右 {SAFE_RIGHT - gx1:+d}px")
    if fail:
        print(f"结论: {fail} 项严重越界 ❌  需要调整")
    elif warn:
        print(f"结论: {warn} 项轻微超出，在容忍范围内 ✅  可以发布")
    else:
        print("结论: 全部在安全区内 ✅")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    if len(a) < 2:
        raise SystemExit(__doc__)
    ev = 1
    if "--every" in sys.argv:
        ev = int(sys.argv[sys.argv.index("--every") + 1])
    sys.exit(audit(a[0], a[1], ev))
