#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用 TTS：读文案 txt → 出定板音频（配方 v2，参数一字不改）

相比 S01/S02 的一次性脚本，这里只为**效率**做了三件事：
1. 文案从文件读，不再硬编码在脚本里（改文案不用改代码）
2. 参考音频和参数全部从人设圣经的定板值取，集中在一个地方
3. 输出直接落进 `06_音频/Sxx_名/`，命名带实测时长

配方来源：用户 2026-10-26 手动定板，见人设圣经《七、声音真值》
  cfg 1.1 / temp 0.95 / seed 42 / max_new_tokens 1504 / rep_pen 1.1
  depth_temp 0.90 / top_k 50 / top_p 1.0 / depth_top_k 50 / depth_top_p 1.0
  warmup 0 / stitch none / decode cuda_graphs
  INSTR「用热情友好、有活力的语气，亲切自然，带笑容」永不改动

用法:
  gen_tts.py <文案txt> <输出目录> [--tag S03]
  例: gen_tts.py ../05_文案/S03_同行都在拍/S03_字幕文本.txt ../06_音频/S03_同行都在拍 --tag S03

  DRY=1  只打印文案与参数，不提交
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

COMFY = "http://127.0.0.1:8188"
COMFY_OUT = "/cfu/ComfyUI/output"
IN_DIR = "/cfu/ComfyUI/input/minimax_h3_timeline_director"
REL = "minimax_h3_timeline_director"
HERE = os.path.dirname(os.path.abspath(__file__))
FF = ("/home/qmr/.workbuddy/binaries/python/envs/default/lib/python3.13/"
      "site-packages/imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v7.0.2")

# ★★ 多 IP 隔离：真值全部来自本 IP 的 ip.json（按脚本路径自动定位）
#   2026-10-28 改。以前的写法有两个致命问题：
#     ① 参考音靠 glob 从 ../02_素材真值/*声音参考*.flac 里取，
#        在错的目录里跑就指向错的 IP，**且不报错**——这是最严重的 IP 混用。
#     ② 硬编码了「洁斌_声音参考_8s.flac」当兜底，等于默认用洁斌的音色。
#   现在改为：ip_guard 按路径锁定 IP → 素材不存在直接 sys.exit，
#   **绝不静默退回别人的素材**。环境变量仍可用，但指向本 IP 之外会被拒绝。
from ip_guard import load_ip

IP = load_ip()
REF_SRC = IP.asset("voice", "ref_audio")
REF_NAME = "cb_ref.wav"
# 参考音的逐字稿。**必须和 REF_SRC 是同一句**，否则音色克隆会跑偏。
REF_TEXT = os.environ.get("REF_TEXT") or IP.get("voice.ref_text") or ""
if os.environ.get("REF_TEXT") is None:
    # 没被环境变量覆盖时，优先读归档的逐字稿文件，保证与音频同源
    _tf = IP.asset("voice", "ref_text_file")
    if _tf and os.path.isfile(_tf):
        REF_TEXT = open(_tf, encoding="utf-8").read().strip()
INSTR = os.environ.get("INSTR") or IP.get("voice.tts.instruction") or ""
MODEL = IP.get("voice.tts.model") or "int8 hybrid (recommended)"
CHARS_PER_SEC = 3.85          # ★ 含标点字数/秒（S01/S02 实测）：78 字含标点 → 20.3s，实测 20.0s。
                              #   念字口径（spoken()，去标点）约 3.30 字/s —— 两者别混用。

# TTS 配方：档案里有就按档案，没有才用脚本内默认值
_T = IP._d.get("voice", {}).get("tts", {})
CFG = _T.get("cfg_scale", 1.1)
TEMP = _T.get("temperature", 0.95)
TOP_K = _T.get("top_k", 50)
TOP_P = _T.get("top_p", 1.0)
REP_PEN = _T.get("repetition_penalty", 1.1)
DEPTH_TEMP = _T.get("depth_temperature", 0.90)
DEPTH_TOP_K = _T.get("depth_top_k", 50)
DEPTH_TOP_P = _T.get("depth_top_p", 1.0)
MAX_NEW_TOKENS = _T.get("max_new_tokens", 1504)
WARMUP = _T.get("warmup_frames", 0)
STITCH = _T.get("stitch_reference", "none")
DECODE = _T.get("decode_mode", "cuda_graphs")
SEED = _T.get("seed", 42)


def read_text(path):
    """读文案。**换行合并成一段**——换行会被读成 0.7-0.9s 长停顿（实测踩过）。"""
    t = open(path, encoding="utf-8-sig").read()
    t = re.sub(r"\s*\n+\s*", "", t.strip())
    t = re.sub(r"\s+", "", t)
    return t


def spoken(s):
    return len([c for c in s if re.match(r"[\u4e00-\u9fffA-Za-z0-9]", c)])


def graph(prefix, text):
    return {
        "1": {"class_type": "BreezeTTS2LoadModel", "inputs": {
            "model": MODEL, "dtype": "auto", "device": "auto", "attention": "auto",
            "decode_mode": DECODE, "download_if_missing": False}},
        "2": {"class_type": "LoadAudio", "inputs": {"audio": f"{REL}/{REF_NAME}"}},
        "3": {"class_type": "BreezeTTS2VoiceDirection", "inputs": {
            "breeze_model": ["1", 0],
            "text": text,
            "reference_audio": ["2", 0],
            "reference_text": REF_TEXT,
            "instruction": INSTR,
            "cfg_scale": CFG,
            "stitch_reference": STITCH,
            "max_new_tokens": MAX_NEW_TOKENS,
            "temperature": TEMP,
            "top_k": TOP_K,
            "top_p": TOP_P,
            "repetition_penalty": REP_PEN,
            "depth_temperature": DEPTH_TEMP,
            "depth_top_k": DEPTH_TOP_K,
            "depth_top_p": DEPTH_TOP_P,
            "warmup_frames": WARMUP,
            "seed": SEED,
        }},
        "4": {"class_type": "SaveAudioMP3", "inputs": {
            "audio": ["3", 0], "filename_prefix": f"audio/{prefix}", "quality": "320k"}},
    }


def post(data):
    req = urllib.request.Request(COMFY + "/prompt", data=json.dumps(data).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as ex:
        sys.exit(f"✗ ComfyUI 拒收这条 TTS 工作流（HTTP {ex.code}）：\n"
                 + ex.read().decode("utf-8", "ignore")[:800])
    except Exception as ex:
        # 连不上是最常见的：ComfyUI 没起 / 中途崩了。给一句话，不要甩 traceback
        sys.exit(f"✗ 提交 TTS 失败：连不上 ComfyUI（{COMFY}）\n"
                 f"  {type(ex).__name__}: {ex}\n"
                 f"  先确认它还在跑：curl -s --noproxy '*' {COMFY}/system_stats")


def poll(pid, timeout=1800):
    """等 ComfyUI 返回结果。

    ★ 服务挂了必须**报错退出**，不能死等。2026-10-30 实测：ComfyUI 中途崩掉，
      这里连不上就 sleep(5) 无限重试，批量任务卡了 12 分钟一个字都不打，
      人以为在排队，其实在等一个永远不会来的结果（内存还有 58G，不是 OOM）。
    """
    end = time.time() + timeout
    t0 = time.time()
    bad = 0
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"{COMFY}/history/{pid}", timeout=30) as r:
                h = json.loads(r.read().decode())
            bad = 0
        except Exception as ex:
            bad += 1
            if bad >= 6:              # 连续 30 秒都连不上 = 服务没了
                sys.exit(f"✗ 连不上 ComfyUI（{COMFY}）：{type(ex).__name__}: {ex}\n"
                         f"  任务 {pid} 拿不到结果，已停止等待。\n"
                         f"  先确认它还在跑：curl -s --noproxy '*' {COMFY}/system_stats\n"
                         f"  起来后接着跑：vstudio pipeline <IP> <套次> --from tts")
            time.sleep(5)
            continue
        if pid in h:
            return h[pid]
        waited = int(time.time() - t0)
        if waited >= 60 and waited % 60 < 6:
            print(f"  … 已等 {waited}s（正常 30–90s；超过 3 分钟请查 ComfyUI 日志）")
        time.sleep(5)
    return None


def dur(path):
    p = subprocess.run([FF, "-nostdin", "-hide_banner", "-i", path], capture_output=True)
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", p.stderr.decode())
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0


def prep():
    src = os.path.abspath(REF_SRC)
    assert os.path.exists(src), src
    os.makedirs(IN_DIR, exist_ok=True)
    d = os.path.join(IN_DIR, REF_NAME)
    if not os.path.exists(d):
        subprocess.run([FF, "-nostdin", "-y", "-i", src, "-ar", "48000", "-ac", "1",
                        "-c:a", "pcm_s16le", d], check=True, capture_output=True)
        print("参考音频 →input")


def main():
    txt_path, out_dir = sys.argv[1], sys.argv[2]
    tag = sys.argv[sys.argv.index("--tag") + 1] if "--tag" in sys.argv else "S03"
    text = read_text(txt_path)
    n = spoken(text)                              # 真正念出来的字（不含标点）
    total = len(re.sub(r"\s", "", text))          # 含标点 = 与 pipeline 估片长同一口径
    est = total / CHARS_PER_SEC

    # ★ 口径必须统一：spoken() 去掉了标点，却拿 3.85（这是**含标点**字数/秒）去除，
    #   必然低估约 14%。实测 S02：66 念字/3.85=17.1s，实际 20.0s，于是每次跑都白报
    #   一条「时长偏离预估 +2.8s，检查是否有长停顿或漏读」——假警报比没警报更坏。
    print(f"文案 {total} 字（念 {n} 字）  预估 {est:.1f}s")
    # ★ 每次都必须明确打印「用的是谁的声音」——多 IP 下这是唯一的防混用护栏
    print(f"音色参考 {os.path.basename(REF_SRC)}   逐字稿「{REF_TEXT}」")
    if not os.path.isfile(REF_SRC):
        sys.exit(f"✗ 参考音不存在：{REF_SRC}\n"
                 f"  多 IP 场景请用 REF_SRC=<本IP声音参考.flac> 指定")
    if os.environ.get("DRY"):
        print("\n===== 文案（TTS 逐字）=====")
        print(text)
        if "、" in text:
            print("⚠️ 含顿号——会被逐字停顿，建议改连读")
        sys.exit(0)

    prep()
    prefix = f"{tag}_tts"
    print("提交 TTS ...", flush=True)
    res = post({"prompt": graph(prefix, text), "client_id": "c_%d" % int(time.time())})
    pid = res["prompt_id"]
    print("prompt_id", pid, flush=True)
    r = poll(pid)
    if r is None:
        # 轮询等到超时也没拿到这条任务的状态。ComfyUI 收下了 prompt（所以前面打出了
        # prompt_id），但一直没执行完 —— 几乎都是 GPU 推理起不来：CUDA 不可用 / 对应节点
        # 缺模型 / ComfyUI 中途崩了。这条信息以前只打「失败: {}」，完全看不出原因。
        print("失败: ComfyUI 在 1800s 内没返回这条任务的状态——多半是 GPU 推理没跑起来"
              "（CUDA 不可用 / TTS 节点缺模型 / ComfyUI 中途崩了）。去看 ComfyUI 控制台。")
        sys.exit(1)
    st = r.get("status", {})
    if st.get("status_str") != "success":
        err = ""
        for m in st.get("messages", []):
            if isinstance(m, list) and m[0] == "execution_error":
                err = str(m[1].get("exception_message"))[:400]
        print("失败:", err or st)
        sys.exit(1)

    fs = glob.glob(os.path.join(COMFY_OUT, "audio", f"{prefix}*.mp3"))
    assert fs, "找不到输出文件"
    src = max(fs, key=os.path.getmtime)
    os.makedirs(out_dir, exist_ok=True)
    d = dur(src)
    dst = os.path.join(out_dir, f"{tag}_音频_定板_{d:.1f}s.mp3")
    shutil.copy2(src, dst)
    print(f"OK {dst}  {os.path.getsize(dst)} bytes  {d:.2f}s  （预估 {est:.1f}s）")
    # 判据：实测与预估差 >2s 说明文案有长停顿/漏读，值得人看一眼
    if abs(d - est) > 2.0:
        print(f"⚠️ 时长偏离预估 {d-est:+.1f}s —— 检查是否有长停顿或漏读")
    print(f"   实测语速 {n/d:.2f} 字/s")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    main()
