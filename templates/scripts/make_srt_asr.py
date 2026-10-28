#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用 ASR 强制对齐的时间轴生成 SRT —— 让字幕跟着人声走，而不是跟着静音段猜。

为什么不再用「静音段 + DP」
==========================
`make_srt.py` 靠 `silencedetect` 切发声段，再用 DP 把字幕条分配到连续段上。
这条路有两个结构性缺陷，S03 全踩到了：

1. **静音段不等于句读**
   实测 10.4–13.3s 这一段：
     silencedetect (-32dB/0.12s): 10.42-11.44 / 11.70-12.11 / 12.27-12.54 /
                                  12.72-12.93 / 13.06-13.23   ← 5 段
     ASR 对齐器:                  10.40-11.44 / 11.68-12.08 / 12.24-13.28  ← 3 段
   后面那两个 0.2s 的「静音」是音节之间的换气，不是句读。把换气当断句依据，
   等于让算法去切碎一句完整的话。

2. **DP 的语速口径自相矛盾**（这个 bug 已单独修掉）
   基准 rate 用「字数 / 纯发声时长」，而候选切分的 r 用「字数 / 首段起点到末段终点的
   总跨度（含静音）」。两套口径下，把 13 个字压进 1.69s 算出 7.69 字/s（合法），
   真实念速却是 9.12 字/s。DP 于是选了它，字幕比人声早 1.17s 收。

后果是 S03 从 cue6 开始整段错位一条（cue7 早 1.57s、cue8 早 1.85s），
用户听到的「中间有一段对不上节奏」就是这个。

做法
====
时间**只从 ASR 对齐器来**，字幕的**文本和断句只从定稿文案来**，两者用字符级对齐缝合：

1. `AILab_Qwen3ASRSubtitle`（split_by_pause, max_gap 0.12）→ 若干「短语段 + 起止时间」
2. 把 ASR 文本与定稿文案归一化（去标点、数字转中文），做字符级序列对齐
3. 得到「定稿字符下标 → 真实时间」的锚点表；段内按字数线性插值
4. 按**定稿标点**切 cue（语义优先，绝不按字数硬切），查锚点表取时间
5. 折行仍在显示层处理（标点只在最后 `display_text()` 抹掉）

这样字幕时间必然和人声同步，因为它是从人声反推出来的。

用法
====
  make_srt_asr.py <音频> <文案txt> <输出srt> [--maxline 12] [--gap 0.12]
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from difflib import SequenceMatcher

API = "http://127.0.0.1:8188"
CI = "/cfu/ComfyUI"
IN_DIR = f"{CI}/input/minimax_h3_timeline_director"

PUNCT = "，。？！、；：,.;:!?…—～「」“”‘’（）()"
NOISE = re.compile(f"[\\s{PUNCT}]")

SENT_END = "。？！"
CLAUSE_SEP = "，、；：,;:"
MAX_LINE = 12
HARD_LIMIT = 14
MIN_CUE_CHARS = 4     # 太碎的并回前一条（「分身。」两字不独立成条）

# 无标点长句的人工断点：键=原句（不含尾标点），值=断开处的字符下标。
# 只在「没有标点可依」且「超过 HARD_LIMIT 物理上限」时使用；断点必须落在词与词之间。
#
# ★ 2026-10-27 补：这个机制原来只在旧版 make_srt.py 里有，asr 版没有，
#   结果 S04 的 cue15 被 wrap() 在 15÷2=7 处一刀切成
#       「是我替你做完那 / 些你不想做的准备」
#   把「那些」这个词从中间劈开了。改完要重烧字幕才发现，成本很高。
#   现在两版都有这个表，**超物理上限的句子一律先查这里**，查不到才硬切。
MANUAL_BREAK = {
    # 只登记**超过 14 字物理上限**、且内部无标点的句子。
    # 13 字只超舒适区 1 字，整行比折成「11+2」好看，不登记。
    "你是每次拍都得换衣服化妆找地方摆灯": 12,   # 换衣服化妆 | 找地方摆灯
    "是我替你做完那些你不想做的准备": 6,        # 是我替你做完 | 那些你不想做的准备
}

CN_DIGIT = {"0": "零", "1": "一", "2": "二", "3": "三", "4": "四",
            "5": "五", "6": "六", "7": "七", "8": "八", "9": "九"}
DIGIT_CN = {v: k for k, v in CN_DIGIT.items()}


# ---------------- 文本归一化 ----------------

def norm_num(s):
    """把阿拉伯数字逐位转中文，让 ASR 的「20分钟」和定稿的「二十分钟」能对上。

    只做逐位转换，不做「二十」这种组合解析——本项目文案里的数字都是
    「十秒 / 二十分钟 / 一张」这类，逐位足够，且不会误伤「第1条」之类的场景。
    """
    return "".join(CN_DIGIT.get(c, c) for c in s)


def norm(s):
    """对齐用：去标点空白 + 数字归一化。"""
    return norm_num(NOISE.sub("", s))


def spoken_chars(s):
    return NOISE.sub("", s)


# ---------------- 音频 -> ASR ----------------

def duration(path):
    p = subprocess.run(
        ["/home/qmr/.workbuddy/binaries/python/envs/default/lib/python3.13/"
         "site-packages/imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v7.0.2",
         "-hide_banner", "-i", path], capture_output=True)
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", p.stderr.decode())
    if not m:
        raise SystemExit("读不到时长: " + path)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def api(path, data=None, timeout=60):
    url = API + path
    if data is None:
        return json.load(urllib.request.urlopen(url, timeout=timeout))
    req = urllib.request.Request(url, data=json.dumps(data).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def push_audio(src):
    """把音频放进 ComfyUI input 目录，返回文件名。"""
    os.makedirs(IN_DIR, exist_ok=True)
    name = "srt_" + re.sub(r"[^0-9A-Za-z_.-]", "_", os.path.basename(src))
    dst = os.path.join(IN_DIR, name)
    subprocess.run(["cp", "-f", src, dst], check=True)
    return f"minimax_h3_timeline_director/{name}"


def asr_segments(src, gap=0.12):
    """调 ASR 拿「短语段 + 时间」。返回 [(t0, t1, text), ...]。

    为什么用 split_by_pause 而不是 split_by_punctuation：
    ASR 自己的标点和定稿未必一致（神似/省事、被告/背稿 这类同音误识别会带标点差异），
    按停顿切只依赖声学边界，反而更接近人耳听到的分句。
    """
    audio = push_audio(src)
    cid = "c_" + uuid.uuid4().hex[:8]
    wf = {
        "1": {"class_type": "LoadAudio", "inputs": {"audio": audio}},
        "2": {"class_type": "AILab_Qwen3ASRSubtitle", "inputs": {
            "audio": ["1", 0],
            "model": "Qwen/Qwen3-ASR-0.6B-hf",
            "precision": "bf16", "attention": "auto",
            "forced_aligner": "Qwen/Qwen3-ForcedAligner-0.6B-hf",
            "language": "Chinese", "hints": "",
            "output_format": "none", "output_path": "",
            "split_mode": "split_by_pause",
            "max_gap_sec": float(gap),
            "max_chars": 0, "unload_models": False,
        }},
        "3": {"class_type": "PreviewAny", "inputs": {"source": ["2", 1]}},
    }
    try:
        pid = api("/prompt", {"prompt": wf, "client_id": cid})["prompt_id"]
    except urllib.error.HTTPError as e:
        raise SystemExit("ASR 提交失败: " + e.read().decode()[:800])

    for _ in range(300):
        time.sleep(1.5)
        h = api(f"/history/{pid}")
        if pid in h:
            if h[pid]["status"].get("status_str") != "success":
                raise SystemExit("ASR 执行失败: " +
                                 json.dumps(h[pid]["status"], ensure_ascii=False)[:800])
            for out in h[pid]["outputs"].values():
                for v in out.values():
                    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
                    try:
                        raw = json.loads(s)
                        if isinstance(raw, list) and raw:
                            s = raw[0]
                    except Exception:
                        pass
                    segs = []
                    for ln in s.split("\n"):
                        m = re.match(r"([\d.]+)-([\d.]+):\s*(.+)", ln.strip())
                        if m:
                            segs.append((float(m.group(1)), float(m.group(2)),
                                         m.group(3).strip()))
                    if segs:
                        return segs
            raise SystemExit("ASR 输出里没有可解析的分段")
    raise SystemExit("ASR 超时")


# ---------------- 字符级对齐 ----------------

def build_anchor(asr_segs, script):
    """把 ASR 分段时间映射到定稿字符下标，返回 [(归一化下标, 时间)] 锚点表。

    关键：锚点要**落在段的边界上**，而不是每段只给一个中点。
    之前只在段首/段尾打两个点、段内线性插值，等于假设「段内每个字匀速」，
    于是把段内静音也平均摊到每个字上 —— 结果是 cue 首尾相接，
    字幕提前出现又拖后消失，和人声还是对不齐。

    现在每个 ASR 段贡献两个锚点（段首字前 + 段末字后），
    段内若字数多于 2，则再按字数把段内时间平均分摊给每个字。
    这样 cue 边界正好落在段的边界上，静音留在 cue 之间，不进 cue 内部。
    """
    asr_all, owner = "", []          # owner[i] = 该字符属于第几段
    for k, (t0, t1, txt) in enumerate(asr_segs):
        n = norm(txt)
        asr_all += n
        owner += [k] * len(n)
    dst = norm(script)

    sm = SequenceMatcher(None, asr_all, dst, autojunk=False)
    a2d = {}
    for blk in sm.get_matching_blocks():
        for k in range(blk.size):
            a2d[blk.a + k] = blk.b + k

    # 每段映射到定稿（归一化）下标区间
    spans = []
    for k, (_t0, _t1, _tx) in enumerate(asr_segs):
        idxs = [a2d[i] for i, o in enumerate(owner) if o == k and i in a2d]
        spans.append((min(idxs), max(idxs) + 1) if idxs else None)

    for k, sp in enumerate(spans):
        if sp is not None:
            continue
        prev = next((spans[x] for x in range(k - 1, -1, -1) if spans[x]), None)
        nxt = next((spans[x] for x in range(k + 1, len(spans)) if spans[x]), None)
        spans[k] = prev or nxt

    if not any(spans):
        raise SystemExit("ASR 与定稿完全对不上，先检查文案是否与音频一致")

    # 逐段生成锚点：段内按字数线性分摊，但**只在段内摊**，绝不跨段
    anchors = []
    for k, sp in enumerate(spans):
        if sp is None:
            continue
        t0, t1, _ = asr_segs[k]
        a, b = sp
        L = max(1, b - a)
        for j in range(L + 1):
            anchors.append((a + j, t0 + (t1 - t0) * j / L))
    anchors.sort()

    # 单调化
    out, last = [], -1e9
    for i, t in anchors:
        t = max(t, last)
        out.append((i, t))
        last = t
    return out


def time_at(anchors, pos, side="start"):
    """查锚点表取时间。

    ⚠️ 同一个下标可能有**两个**锚点，因为它是上一段的段尾、又是下一段的段首：

        (40, 10.240)  ← 「顾客进店之前，」念完的时刻
        (40, 10.400)  ← 「先在手机上…」开口的时刻
        中间 0.16s 是真实静音

    所以必须按用途选边：给一条字幕**收尾**时取 side="end"（段尾，早的那个），
    **起头**时取 side="start"（段首，晚的那个）。如果一律取第一个，
    字幕会提前出现、拖后消失，首尾相接把静音吃进字幕里——正是 S03 原来的病。
    """
    if not anchors:
        return 0.0
    eps = 1e-9
    hits = [t for i, t in anchors if i == pos]
    if hits:
        return min(hits) if side == "end" else max(hits)
    if pos <= anchors[0][0]:
        return anchors[0][1]
    if pos >= anchors[-1][0]:
        return anchors[-1][1]
    for k in range(len(anchors) - 1):
        i0, t0 = anchors[k]
        i1, t1 = anchors[k + 1]
        if i0 < pos < i1:
            return t0 + (t1 - t0) * (pos - i0) / (i1 - i0)
    return anchors[-1][1]


# ---------------- 定稿 -> cue ----------------

def split_cues(text):
    """按**句读标点**切 cue，和 make_srt.py 的 split_cues 保持一致粒度。

    坑：第一版只按句末标点（。？！）切，逗号条不切 → 切出 14 条、其中
    「顾客进店之前，先在手机上把你和隔壁比一遍，」19 字一条跨两句，
    显示出来是两句话粘在一屏上，比原来更糟。
    句读必须一起切，这才是「一句话一个换气」的粒度。

    唯一例外：太碎的（<4 字）并回前一条，但**只限同一句内**，
    否则「…看见。」会跟下一句焊在一起。
    """
    text = re.sub(r"\s+", "", text)
    parts, cur = [], ""
    for ch in text:
        cur += ch
        if ch in SENT_END or ch in CLAUSE_SEP:
            if spoken_chars(cur):
                parts.append(cur)
            cur = ""
    if spoken_chars(cur):
        parts.append(cur)

    merged = []
    for c in parts:
        same_sent = merged and merged[-1][-1] not in SENT_END
        if same_sent and len(spoken_chars(c)) < MIN_CUE_CHARS:
            merged[-1] += c
        else:
            merged.append(c)
    return merged


def cue_display(c):
    return c


def display_text(s):
    """显示层抹掉标点：逗号类换半角空格，行末直接删。

    ⚠️ 只在最后一步做。内部必须留着标点，因为 切条 / 合并 / 判同句 / 折行
    四个决策全靠它。
    """
    out = []
    for ln in s.split("\n"):
        ln = re.sub(f"[{PUNCT}]", " ", ln)
        ln = re.sub(r"[ \t]{2,}", " ", ln).strip()
        if ln:
            out.append(ln)
    return "\n".join(out)


def wrap(s, maxline=MAX_LINE):
    """折行：有标点按标点断，超物理上限才硬断，否则整行。"""
    s = s.strip()
    n = len(spoken_chars(s))
    if n <= maxline + 2:
        return s
    # 找最靠近中间的标点
    cand = [i for i, c in enumerate(s) if c in PUNCT and 0 < i < len(s) - 1]
    if cand:
        mid = len(s) / 2
        cut = min(cand, key=lambda p: abs(p - mid))
        a, b = s[:cut + 1], s[cut + 1:]
        if spoken_chars(a) and spoken_chars(b):
            return a + "\n" + b
    if n <= HARD_LIMIT:
        return s
    # ★ 超物理上限：先查人工断点表，再硬切。
    #   硬切必然按「字数中点」下手，而中文词的边界跟字数中点毫无关系。
    #   S04 cue15「是我替你做完那些你不想做的准备」15 字 → 在 7 处切成
    #   「是我替你做完那 / 些你不想做的准备」，把「那些」劈开了。
    #   这类错肉眼扫 SRT 很难发现（两行都通顺），但观众读起来会卡。
    key = s.strip().rstrip("".join(PUNCT))
    if key in MANUAL_BREAK:
        cut = MANUAL_BREAK[key]
        a, b = s[:cut], s[cut:]
        if spoken_chars(a) and spoken_chars(b):
            return a.strip() + "\n" + b.strip()
        print(f"⚠ MANUAL_BREAK 断点无效（{key!r} cut={cut}），退回硬切", flush=True)
    else:
        print(f"⚠ 超 {HARD_LIMIT} 字且无人工断点，硬切：{s!r}\n"
              f"  → 请在 make_srt_asr.py 的 MANUAL_BREAK 登记语义断点", flush=True)
    cut = max(1, n // 2)
    return s[:cut] + "\n" + s[cut:]


def ts(t):
    ms = int(round(t * 1000))
    h, ms = ms // 3600000, ms % 3600000
    m, ms = ms // 60000, ms % 60000
    s, ms = ms // 1000, ms % 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) < 3:
        raise SystemExit("用法: make_srt_asr.py <音频> <文案txt> <输出srt> "
                         "[--maxline 12] [--gap 0.12]")
    src, txt, dst = args[0], args[1], args[2]
    maxline = int(sys.argv[sys.argv.index("--maxline") + 1]) \
        if "--maxline" in sys.argv else MAX_LINE
    gap = float(sys.argv[sys.argv.index("--gap") + 1]) \
        if "--gap" in sys.argv else 0.12

    script = re.sub(r"\s*\n+\s*", "", open(txt, encoding="utf-8-sig").read().strip())
    cues = split_cues(script)
    n_chars = len(spoken_chars(script))
    print(f"定稿 {n_chars} 字 / {len(cues)} 条字幕   音频 {duration(src):.2f}s")

    segs = asr_segments(src, gap)
    print(f"ASR 短语段 {len(segs)} 个（max_gap={gap}s）")
    for k, (t0, t1, t) in enumerate(segs, 1):
        print(f"  {k:2d} {t0:6.2f}-{t1:6.2f} {t1-t0:5.2f}s  {t}")

    anchors = build_anchor(segs, script)
    print(f"\n字符锚点 {len(anchors)} 个，覆盖定稿下标 "
          f"{anchors[0][0]}–{anchors[-1][0]}（共 {n_chars} 字）")
    if anchors[-1][0] < n_chars - 1:
        print(f"  ⚠️ 末 {n_chars - anchors[-1][0]} 字没有 ASR 锚点，用最后时间兜底")

    # 逐 cue 取时间。
    # ⚠️ 锚点表的下标是**归一化后**的下标（去标点、数字转中文），
    #    而 cue 是在**原文**上切的（原文含标点，下标更长）。
    #    必须先把原文下标换算成归一化下标，否则会整体错位。
    norm_pos = [None] * (len(script) + 1)
    k = 0
    for i, ch in enumerate(script):
        norm_pos[i] = k
        if spoken_chars(ch):
            k += 1
    norm_pos[len(script)] = k

    rows, pos = [], 0
    for c in cues:
        n_cue = len(spoken_chars(c))
        while pos < len(script) and not spoken_chars(script[pos]):
            pos += 1
        start_idx = pos
        consumed = 0
        while pos < len(script) and consumed < n_cue:
            if spoken_chars(script[pos]):
                consumed += 1
            pos += 1
        end_idx = pos
        t0 = time_at(anchors, norm_pos[start_idx], side="start")
        t1 = time_at(anchors, norm_pos[end_idx], side="end")
        rows.append([t0, t1, c])

    # 单调修复 + 最小显示时长
    for k in range(len(rows)):
        if k and rows[k][0] < rows[k - 1][1]:
            rows[k][0] = rows[k - 1][1]
        if rows[k][1] - rows[k][0] < 0.30:
            rows[k][1] = rows[k][0] + 0.30

    with open(dst, "w", encoding="utf-8") as f:
        for i, (t0, t1, c) in enumerate(rows, 1):
            f.write(f"{i}\n{ts(t0)} --> {ts(t1)}\n"
                    f"{display_text(wrap(cue_display(c), maxline))}\n\n")

    print(f"\n写出 {dst}   {len(rows)} 条\n")
    rates = []
    for i, (t0, t1, c) in enumerate(rows, 1):
        k = len(spoken_chars(c))
        r = k / max(t1 - t0, 0.01)
        rates.append(r)
        print(f"  {i:2d} {ts(t0)} → {ts(t1)} {t1-t0:5.2f}s {k:2d}字 "
              f"{r:4.1f}字/s  {c}")
    rs = sorted(rates)
    print(f"\n语速 中位 {rs[len(rs)//2]:.1f} 字/s  区间 {rs[0]:.1f}–{rs[-1]:.1f}")
    print(f"覆盖 {rows[0][0]:.2f}s → {rows[-1][1]:.2f}s")


if __name__ == "__main__":
    main()
