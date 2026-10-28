#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""接新客户第二步：造一条「声音参考」出来（BreezeTTS2 VoiceDesign）

为什么需要它
============
BreezeTTS2 是零样本音色克隆：给它 8 秒参考音 + 同一句逐字稿，它就能用那个音色念
任意台词。新客户手上没有录音时，整条流水线就卡在第一步 —— 以前只能等客户录音。

现在：先用 VoiceDesign 按「文字描述的音色」合成一句，转成 8s flac 当参考音，
之后所有台词都用 VoiceClone 按这个音色念。

⚠ 这条参考音是**合成起点，不是真人**。交付前必须让客户本人重录 8 秒替换掉，
  否则等于用一个不存在的嗓音给真人立人设。文件里会带 `_合成` 字样提醒。

用法:
  python3 bin/gen_voice_ref.py --ip 演示客户老王 \
      --text "各位好，我是老王，今天想跟您聊聊咱们店里这十几年。" \
      --voice "五十多岁的面馆老板，嗓音偏厚略带沙哑，语速偏慢，说话实在，带一点笑意"
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

COMFY = os.environ.get("COMFY_URL", "http://127.0.0.1:8188")
COMFY_OUT = os.environ.get("COMFY_OUT", "/cfu/ComfyUI/output")
MODEL = os.environ.get("TTS_MODEL", "int8 hybrid (recommended)")
FF = os.environ.get("FFMPEG") or (
    "/home/qmr/.workbuddy/binaries/python/envs/default/lib/python3.13/"
    "site-packages/imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v7.0.2")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def post(api, data):
    req = urllib.request.Request(COMFY + api, data=json.dumps(data).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        print("HTTP %s: %s" % (e.code, e.read().decode("utf-8", "ignore")[:1200]))
        raise


def poll(pid, timeout=1800):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(COMFY + "/history/" + pid, timeout=30) as r:
                h = json.loads(r.read().decode())
        except Exception:
            time.sleep(3)
            continue
        if pid in h:
            return h[pid]
        time.sleep(3)
    raise SystemExit("ComfyUI 超时（%s）" % pid)


def graph(text, voice, seed, max_tokens):
    return {
        "1": {"class_type": "BreezeTTS2LoadModel", "inputs": {
            "model": MODEL, "dtype": "auto", "device": "auto", "attention": "auto",
            "decode_mode": "cuda_graphs", "download_if_missing": False}},
        "2": {"class_type": "BreezeTTS2VoiceDesign", "inputs": {
            "breeze_model": ["1", 0],
            "text": text,
            "instruction": voice,
            "cfg_scale": 4.0,
            "max_new_tokens": max_tokens,
            "temperature": 0.9,
            "top_k": 50,
            "top_p": 1.0,
            # 这几个是节点必填项，少一个 ComfyUI 直接 400（报错只说「Bad Request」，
            # 不会告诉你是哪个字段——所以照抄节点的 required 列表，别凭印象写）
            "repetition_penalty": 1.1,
            "depth_temperature": 0.9,
            "depth_top_k": 50,
            "depth_top_p": 1.0,
            "warmup_frames": 0,
            "seed": seed,
        }},
        "3": {"class_type": "SaveAudioMP3", "inputs": {
            "audio": ["2", 0], "filename_prefix": "voice/ref", "quality": "320k"}},
    }


def grab(hist):
    for node in (hist.get("outputs") or {}).values():
        for a in (node.get("audio") or []):
            return os.path.join(COMFY_OUT, a.get("subfolder") or "", a["filename"])
    raise SystemExit("跑完了但没有音频输出：%s" % json.dumps(hist)[:400])


def dur(path):
    p = subprocess.run([FF, "-hide_banner", "-i", path], capture_output=True)
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", p.stderr.decode("utf-8", "ignore"))
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0


def main():
    ap = argparse.ArgumentParser(description="合成一条声音参考（8s flac + 逐字稿）")
    ap.add_argument("--ip", required=True)
    ap.add_argument("--text", required=True, help="要念的那句话（8 秒左右，别太长）")
    ap.add_argument("--voice", required=True, help="音色描述（用中文写，跟台词同语言）")
    ap.add_argument("--name", default="", help="输出文件名前缀，默认 <IP简称>_声音参考_合成8s")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--max-tokens", type=int, default=300)
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()

    from vstudio import resolve_ip
    e = resolve_ip(a.ip)
    out_dir = os.path.join(e["path"], "02_素材真值")
    short = e["name"].replace("演示客户", "") or "声音"
    name = a.name or ("%s_声音参考_合成8s" % short)
    flac = os.path.join(out_dir, name + ".flac")
    txt = os.path.join(out_dir, name + "_逐字稿.txt")

    print("IP      %s" % e["name"])
    print("文本    %s" % a.text)
    print("音色    %s" % a.voice)
    print("输出    %s" % flac)
    if a.dry:
        print("（--dry：只打印）")
        return 0

    seed = a.seed or int(time.time())
    pid = post("/prompt", {"prompt": graph(a.text, a.voice, seed, a.max_tokens)})["prompt_id"]
    hist = poll(pid)
    src = grab(hist)
    os.makedirs(out_dir, exist_ok=True)
    # 48k / 单声道 / flac：gen_tts.py 的 prep() 就是这么转的，参考音统一规格最省事
    subprocess.run([FF, "-y", "-v", "error", "-i", src, "-t", "12",
                    "-ar", "48000", "-ac", "1", "-c:a", "flac", flac], check=True)
    with open(txt, "w", encoding="utf-8") as f:
        f.write(a.text.strip() + "\n")
    print("✓ %s  (%.1fs, seed=%s)" % (flac, dur(flac), seed))
    print("✓ %s" % txt)
    print("\n下一步：把这两条填进 ip.json：")
    print('  voice.ref_audio      = "02_素材真值/%s.flac"' % name)
    print('  voice.ref_text_file  = "02_素材真值/%s_逐字稿.txt"' % name)
    print("⚠ 这是合成起点，交付前让客户本人重录 8 秒替换。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
