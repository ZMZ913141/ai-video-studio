#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对比成片音轨与定板原声：判断 H3 是否真的叠了配乐。

做法：
- 分别导出成片音轨和原始 mp3 到 48k mono wav
- 在句间停顿处（人声静音）比较两者 RMS：若成片明显高于原声 → 有配乐
- 全片也对比一次，给出整体增益
"""
import subprocess
import sys

import numpy as np

FF = ("/home/qmr/.workbuddy/binaries/python/envs/default/lib/python3.13/"
      "site-packages/imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v7.0.2")
SR = 48000


def pcm(path, extra=None):
    a = [FF, "-v", "error", "-i", path]
    if extra:
        a += extra
    a += ["-ac", "1", "-ar", str(SR), "-f", "f32le", "-"]
    p = subprocess.run(a, capture_output=True)
    return np.frombuffer(p.stdout, np.float32)


def rms(x):
    return float(np.sqrt(np.mean(x.astype(np.float64) ** 2))) if len(x) else 0.0


def seg(x, t0, t1):
    return x[int(t0 * SR):int(t1 * SR)]


def db(v):
    return 20 * np.log10(max(v, 1e-9))


def auto_pauses(ref, win=0.12, rel_db=-38.0, min_gap=0.30):
    """从**原声**自动找句间停顿，别再硬编码时间点。

    坑：原来 pauses 是写死的 S01/S02 那 7 个区间，换一条片子就全错——
    S03 的 17.94s 在原声里是静音（-99.9），却把它当「停顿」来比，
    算出 +53.6 dB 的假差值，结论直接反转成「无配乐」。
    正确做法：谁的停顿就该问谁 —— 用原声自己扫。
    """
    n = len(ref) // int(win * SR)
    out = []
    for i in range(n):
        s = ref[i * int(win * SR):(i + 1) * int(win * SR)]
        if db(rms(s)) < rel_db:
            out.append((i * win, (i + 1) * win))
    # 合并相邻窗口成区间，太短的丢掉
    merged = []
    for a, b in out:
        if merged and a - merged[-1][1] < 0.02:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    return [(a, b) for a, b in merged if b - a >= min_gap]


def main():
    vid, ref = sys.argv[1], sys.argv[2]
    a = pcm(vid)
    b = pcm(ref)
    print(f"成片音频 {len(a)/SR:.2f}s   原声 {len(b)/SR:.2f}s")
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]

    print(f"\n整体 RMS  成片 {db(rms(a)):6.1f} dBFS   原声 {db(rms(b)):6.1f} dBFS   "
          f"差 {db(rms(a))-db(rms(b)):+.1f} dB")

    # 句间停顿（人声静音）：**从原声自动检测**，不写死时间点
    pauses = auto_pauses(b)
    print(f"\n自动检测到 {len(pauses)} 处句间停顿（原声 < −38 dBFS 且 ≥0.30s）")
    print("  区间           成片dBFS  原声dBFS   差")
    tot_a, tot_b = [], []
    for t0, t1 in pauses:
        sa, sb = seg(a, t0, t1), seg(b, t0, t1)
        if not len(sa):
            continue
        tot_a.append(sa)
        tot_b.append(sb)
        print(f"  {t0:5.2f}-{t1:5.2f}   {db(rms(sa)):7.1f}  {db(rms(sb)):7.1f}  "
              f"{db(rms(sa))-db(rms(sb)):+5.1f} dB")
    if tot_a:
        ca, cb = np.concatenate(tot_a), np.concatenate(tot_b)
        print(f"  {'停顿合计':12s}  {db(rms(ca)):7.1f}  {db(rms(cb)):7.1f}  "
              f"{db(rms(ca))-db(rms(cb)):+5.1f} dB")
        d = db(rms(ca)) - db(rms(cb))
        # 人声基准：默认量参考音频。但 `mix_bgm.py` 现在会按系列目标做人声响度
        # 对齐（VO_NORM），交付人声可能与参考音频差几 dB；
        # 此时必须用 `--voice-db` 传交付后的真实人声电平，否则「差」会虚高。
        vd = None
        if "--voice-db" in sys.argv:
            vd = float(sys.argv[sys.argv.index("--voice-db") + 1])
        voice = vd if vd is not None else db(rms(b))
        src = "（--voice-db 指定）" if vd is not None else "（量自参考音频）"
        # 停顿处应比原声高（原声接近静），但必须**明显低于人声段**，否则会盖住说话。
        print(f"\n  停顿处配乐电平 {db(rms(ca)):.1f} dBFS"
              f"   人声段 {voice:.1f} dBFS {src}   差 {db(rms(ca))-voice:+.1f} dB")
        print("\n结论：" + ("✅ 有配乐，且落在人声下方"
                          if d > 3 and db(rms(ca)) - voice < -10
                          else "⚠️ 有配乐但偏高，可能盖人声"
                          if d > 3
                          else "❌ 基本无配乐（停顿处与原声接近）"))


if __name__ == "__main__":
    main()
