#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 88 BPM 温暖器乐垫底，叠在成片人声下方。

为什么自己合成
--------------
本机没有可用音乐生成模型：`yue2_3b_bf16.safetensors` 在 checkpoints 里，
但 T8 的 `YuE2MusicPromptEnhancer` 输出是 5 个 **String**（style/lyrics/abc/
request_json/report），源码明写不产出音频；也没有 ACE-Step / MusicGen / StableAudio 节点。
H3 的 `non_diegetic_music` 在 `audioMode: locked` 下不生效（实测停顿处与原声完全一致）。

所以：自己用加法合成做一段克制的器乐 bed。
- 88 BPM，4/4，C 大调 I-V-vi-IV 循环
- 指弹吉他：拨弦瞬态（快起指数衰减）+ 谐波叠加
- 立式贝斯：每小节根音，正弦 + 轻微二次谐波
- 轻刷：滤波噪声短包络，落在 2、4 拍
- 全段轻柔压限，避免任何突兀峰值

用法: make_bgm.py <输出wav> [时长秒]
"""
import subprocess
import sys

import numpy as np

FF = ("/home/qmr/.workbuddy/binaries/python/envs/default/lib/python3.13/"
      "site-packages/imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v7.0.2")
SR = 48000
BPM = 88.0
BEAT = 60.0 / BPM
BAR = 4 * BEAT
rng = np.random.default_rng(20261027)

# C 大调：I - V - vi - IV，每和弦两小节
CHORDS = [
    (0, [261.63, 329.63, 392.00, 523.25]),   # C
    (0, [261.63, 329.63, 392.00, 523.25]),
    (1, [196.00, 246.94, 293.66, 392.00]),   # G
    (1, [196.00, 246.94, 293.66, 392.00]),
    (2, [220.00, 261.63, 329.63, 440.00]),   # Am
    (2, [220.00, 261.63, 329.63, 440.00]),
    (3, [174.61, 220.00, 261.63, 349.23]),   # F
    (3, [174.61, 220.00, 261.63, 349.23]),
]
BASS = {0: 130.81, 1: 98.00, 2: 110.00, 3: 87.31}


def pluck(f, dur, amp=1.0):
    """指弹：快起 + 指数衰减 + 谐波叠加。"""
    n = int(dur * SR)
    t = np.arange(n) / SR
    env = np.exp(-t * 3.2) * (1 - np.exp(-t * 420))
    s = (np.sin(2 * np.pi * f * t) * 1.0
         + np.sin(2 * np.pi * f * 2 * t) * 0.34 * np.exp(-t * 5)
         + np.sin(2 * np.pi * f * 3 * t) * 0.16 * np.exp(-t * 7)
         + np.sin(2 * np.pi * f * 4 * t) * 0.07 * np.exp(-t * 9))
    return s * env * amp


def bass(f, dur, amp=1.0):
    n = int(dur * SR)
    t = np.arange(n) / SR
    env = np.exp(-t * 1.1) * (1 - np.exp(-t * 160))
    s = np.sin(2 * np.pi * f * t) + 0.18 * np.sin(2 * np.pi * f * 2 * t)
    return s * env * amp


def brush(dur, amp=1.0):
    """轻刷：带通噪声的短包络。"""
    n = int(dur * SR)
    t = np.arange(n) / SR
    x = rng.normal(0, 1, n)
    # 简单一阶高通去低频，再一阶低通去刺
    x = np.diff(np.concatenate([[0.0], x]))
    x = np.convolve(x, np.ones(4) / 4, mode="same")
    env = np.exp(-t * 11) * (1 - np.exp(-t * 900))
    return x * env * amp


def build(total):
    n = int(total * SR)
    out = np.zeros(n, dtype=np.float64)
    bassline = np.zeros(n, dtype=np.float64)
    perc = np.zeros(n, dtype=np.float64)

    nbars = int(np.ceil(total / BAR)) + 1
    for b in range(nbars):
        t0 = b * BAR
        if t0 >= total:
            break
        _, freqs = CHORDS[b % len(CHORDS)]
        idx0 = int(t0 * SR)
        # 指弹分解和弦：每拍一个音，第 3 拍补一个高音
        pattern = [(0.0, freqs[0]), (1.0, freqs[2]), (2.0, freqs[1]), (3.0, freqs[3])]
        for beat, f in pattern:
            s = pluck(f, min(2.4, total - (t0 + beat * BEAT)), 0.30)
            i = int((t0 + beat * BEAT) * SR)
            e = min(n, i + len(s))
            if e > i:
                out[i:e] += s[:e - i]
        # 贝斯：每小节根音，第 3 拍轻点一次
        for beat in (0.0, 2.5):
            s = bass(BASS[CHORDS[b % len(CHORDS)][0]], min(1.9, total - (t0 + beat * BEAT)), 0.5)
            i = int((t0 + beat * BEAT) * SR)
            e = min(n, i + len(s))
            if e > i:
                bassline[i:e] += s[:e - i]
        # 轻刷在第 2、4 拍
        for beat in (1.0, 3.0):
            s = brush(0.30, 0.11)
            i = int((t0 + beat * BEAT) * SR)
            e = min(n, i + len(s))
            if e > i:
                perc[i:e] += s[:e - i]

    mix = out + bassline + perc
    # 极轻的梳状"房间感"：短延迟叠加，避免干瘪
    d = int(0.075 * SR)
    wet = np.concatenate([np.zeros(d), mix[:-d]]) * 0.16
    mix = mix + wet
    # 柔和压限：tanh 软限幅
    mix = np.tanh(mix * 1.15) * 0.75
    # 首尾淡入淡出
    fi, fo = int(0.9 * SR), int(1.4 * SR)
    if len(mix) > fi + fo:
        mix[:fi] *= np.linspace(0, 1, fi)
        mix[-fo:] *= np.linspace(1, 0, fo)
    peak = np.max(np.abs(mix)) or 1.0
    return (mix / peak * 0.92).astype(np.float32)


def main():
    dst = sys.argv[1]
    total = float(sys.argv[2]) if len(sys.argv) > 2 else 34.92
    a = build(total)
    p = subprocess.run([FF, "-y", "-f", "f32le", "-ar", str(SR), "-ac", "1", "-i", "-",
                        "-ar", "48000", "-ac", "2", "-b:a", "192k", dst],
                       input=a.tobytes(), capture_output=True)
    if p.returncode != 0:
        print(p.stderr.decode()[-800:])
        sys.exit(1)
    print("OK", dst, f"{total:.2f}s", flush=True)


if __name__ == "__main__":
    main()
