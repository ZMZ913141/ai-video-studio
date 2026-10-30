#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""音色自检：这条配音到底是不是本 IP 的声音。

为什么要有它（参考音落地名曾写死成某个 IP 的缩写 + 「文件已存在就跳过」）
=====================================
`gen_tts.py` 曾把参考音落地名写死成 `cb_ref.wav`（某个 IP 的缩写），且「文件已存在就跳过」。
该 IP 先跑过一次 → 后建的 IP 这条 TTS 直接复用前一个 IP 的参考音，生成的配音是**别人的声音**，
而日志照样打印新 IP 的参考音文件名，一切看起来正常。

人耳听出来花了半小时；机器量出来只要 1 秒。
所以把它做成流水线的一步：**每次配音后自动验，串了就停**。

怎么判
======
用 MFCC（26 mel × 13 cepstrum，取均值+标准差）算余弦相似度。
实测同一人的配音与参考：0.97–0.99；不同人：0.65–0.75。
阈值 0.90 —— 离两边都远，不会误判。

同时扫一遍兄弟档案：如果它跟**别的 IP** 更像，直接把「串给谁了」点出来，
人不用挨个试听。

用法
====
  voice_check.py <生成音频> <本IP参考音频> [--peers /cfu/ai视频档案库] [--min 0.90]

退出码：0 通过 ／ 2 判为串档或音色不符 ／ 1 工具自身出错（缺文件、numpy 等）
"""
import argparse
import json
import os
import subprocess
import sys
import wave

FF_CANDIDATES = [
    os.environ.get("FFMPEG", ""),
    "/home/qmr/.workbuddy/binaries/python/envs/default/lib/python3.13/"
    "site-packages/imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v7.0.2",
]


def ffmpeg():
    for p in FF_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return "ffmpeg"


def load16k(src):
    """解码成 16k 单声道 float32。mp3/flac/wav 一视同仁（ffmpeg 兜底）。"""
    import tempfile
    fd, dst = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        r = subprocess.run([ffmpeg(), "-y", "-i", src, "-ac", "1", "-ar", "16000", dst],
                           capture_output=True)
        if r.returncode != 0:
            raise RuntimeError("解码失败：%s\n%s" % (src, r.stderr.decode("utf-8", "ignore")[-300:]))
        w = wave.open(dst, "rb")
        n = w.getnframes()
        a = w.readframes(n)
        w.close()
    finally:
        try:
            os.remove(dst)
        except OSError:
            pass
    import numpy as np
    return np.frombuffer(a, dtype=np.int16).astype("float32") / 32768.0


def mfcc_stats(x, sr=16000, n_mel=26, n_ceps=13):
    import numpy as np
    win, hop = int(0.025 * sr), int(0.010 * sr)
    frames = []
    for i in range(0, len(x) - win, hop):
        f = x[i:i + win]
        if float(np.sqrt((f ** 2).mean())) < 0.015:   # 丢掉静音帧，否则算的是房间不是人
            continue
        frames.append(np.abs(np.fft.rfft(f * np.hanning(win))) ** 2)
    if len(frames) < 8:
        raise RuntimeError("有效语音帧太少（%d）——音频太短或几乎全是静音" % len(frames))
    S = np.array(frames)
    fr = np.fft.rfftfreq(win, 1 / sr)

    def hz2mel(h):
        return 2595 * np.log10(1 + h / 700)

    def mel2hz(m):
        return 700 * (10 ** (m / 2595) - 1)

    pts = mel2hz(np.linspace(hz2mel(60), hz2mel(7600), n_mel + 2))
    fb = np.zeros((n_mel, len(fr)))
    for m in range(n_mel):
        a, b, c = pts[m], pts[m + 1], pts[m + 2]
        fb[m, :] = np.where((fr >= a) & (fr <= b), (fr - a) / (b - a + 1e-9),
                            np.where((fr > b) & (fr <= c), (c - fr) / (c - b + 1e-9), 0))
    M = np.log(S @ fb.T + 1e-9)
    n = M.shape[1]
    k = np.arange(n_ceps)[:, None]
    i2 = np.arange(n)[None, :]
    C = M @ (np.cos(np.pi / n * (i2 + 0.5) * k) * np.sqrt(2 / n)).T
    return np.concatenate([C.mean(0), C.std(0)])


def sim(va, vb):
    import numpy as np
    return float(np.dot(va, vb) / (np.linalg.norm(va) * np.linalg.norm(vb) + 1e-9))


def ip_refs(peers, skip_path):
    """扫兄弟档案，收「IP 名 → 参考音绝对路径」。用于回答「那这条是谁的声音」。"""
    out = []
    if not peers or not os.path.isdir(peers):
        return out
    for name in sorted(os.listdir(peers)):
        ipj = os.path.join(peers, name, "ip.json")
        if not os.path.isfile(ipj):
            continue
        try:
            d = json.load(open(ipj, encoding="utf-8"))
        except Exception:
            continue
        ra = str(((d.get("voice") or {}).get("ref_audio")) or "")
        if not ra:
            continue
        p = ra if os.path.isabs(ra) else os.path.join(peers, name, ra)
        if not os.path.isfile(p) or os.path.realpath(p) == os.path.realpath(skip_path):
            continue
        out.append((d.get("ip") or name, p))
    return out


def main():
    ap = argparse.ArgumentParser(description="音色自检：这条配音是不是本 IP 的声音")
    ap.add_argument("audio", help="生成的配音（mp3/wav）")
    ap.add_argument("ref", help="本 IP 的参考音频（ip.json 的 voice.ref_audio）")
    ap.add_argument("--peers", default="/cfu/ai视频档案库", help="兄弟档案根目录（用来指出串给了谁）")
    ap.add_argument("--min", type=float, default=0.90, help="与本人参考的最低相似度（默认 0.90）")
    a = ap.parse_args()

    for p in (a.audio, a.ref):
        if not os.path.isfile(p):
            print("✗ 文件不存在：%s" % p, file=sys.stderr)
            return 1

    try:
        fa = mfcc_stats(load16k(a.audio))
        fr = mfcc_stats(load16k(a.ref))
        me = sim(fa, fr)
        peers = []
        for name, p in ip_refs(a.peers, a.ref):
            try:
                peers.append((name, sim(fa, mfcc_stats(load16k(p)))))
            except Exception:
                continue
    except Exception as ex:
        print("✗ 音色自检跑不起来：%s: %s" % (type(ex).__name__, ex), file=sys.stderr)
        return 1

    peers.sort(key=lambda x: -x[1])
    top = peers[0] if peers else ("", 0.0)

    print("── 音色自检")
    print("   配音     %s" % os.path.basename(a.audio))
    print("   本人参考 %s" % os.path.basename(a.ref))
    print("   相似度   %.3f  （阈值 %.2f）" % (me, a.min))
    if peers:
        print("   其它档案 %s" % "，".join("%s %.3f" % (n, s) for n, s in peers[:3]))

    if me >= a.min:
        print("   ✅ 是本 IP 的声音")
        return 0

    print("   ❌ 这不是本 IP 的声音（%.3f < %.2f）" % (me, a.min))
    if top[1] >= a.min:
        print("      → 最像「%s」（%.3f）——**串档了**：这条配音是别人的嗓子。" % (top[0], top[1]))
        print("      查 gen_tts.py：参考音落地名是否按 IP/内容区分、是否「已存在就跳过」。")
    else:
        print("      → 谁都不像（最高 %.3f）。先确认参考音频与逐字稿同源，再重跑 TTS。" % top[1])
    return 2


if __name__ == "__main__":
    sys.exit(main())
