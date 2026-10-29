#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""音色画像：先量出物理量，再让能**真听**音频的模型说人话。

为什么要两条腿：

1. **物理测量**（纯标准库，永远可用）：基频 F0、时长、响度、静音占比、
   音节率。这些是实测值，不会编。缺点是说不出「沙哑」「有气声」这种质感。

2. **听觉描述**（需要 llama.cpp + Qwen2.5-Omni + mmproj）：模型真的把
   波形听了一遍，才能说出音色质感、口音、共鸣位置。
   ★ 不能拿纯文本模型代替 —— 路由里没有音频通道，让它「描述这段录音」
   只会照着字面编一段，而 TTS 会照着这段假描述去克隆，等于给真人
   安一个不存在的嗓子。宁可不说。

用法：
    python3 bin/voice_profile.py <音频>            # 人看的报告
    python3 bin/voice_profile.py <音频> --json     # 给程序吃

环境变量：
    VOICE_MTMD=/path/to/llama-mtmd-cli   指定可执行文件
    VOICE_OMNI_MODEL=...gguf             默认在本机 ComfyUI 模型目录里找
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# 本机已有的 Omni 权重（9GB，含音频投影器）—— 先找这些常见位置
MODEL_CANDIDATES = [
    "/cfu/ComfyUI/models/LLM/Qwen2.5-Omni-7B-GGUF/Qwen2.5-Omni-7B-Q8_0.gguf",
    "/cfu/ComfyUI/models/LLM/Qwen2.5-Omni-7B-GGUF/*.gguf",
]
MMPROJ_CANDIDATES = [
    "/cfu/ComfyUI/models/LLM/Qwen2.5-Omni-7B-GGUF/mmproj-Qwen2.5-Omni-7B-Q8_0.gguf",
    "/cfu/ComfyUI/models/LLM/Qwen2.5-Omni-7B-GGUF/mmproj*.gguf",
]
BIN_CANDIDATES = [
    "/cfu/llama.cpp/build/bin/llama-mtmd-cli",
    "/cfu/llama.cpp/bin/llama-mtmd-cli",
    "/cfu/llama.cpp/*/bin/llama-mtmd-cli",
    os.path.expanduser("~/llama.cpp/build/bin/llama-mtmd-cli"),
    "/usr/local/bin/llama-mtmd-cli",
]

# 问法很关键：逐项问，才拿得到能直接写进 ip.json 的字段；
# 明确禁止「凭常识补全」，否则模型会把「男声」脑补成「磁性低音炮」。
PROMPT = """你是资深配音导演。仔细听这段录音，只根据实际听到的声音回答。

严格按下面六行输出，每行一个字段，不要解释、不要加序号以外的内容：

性别与年龄段：
音区与音色质感：
语速与节奏：
口音或地域特征：
情绪与说话姿态：
一句话音色指令：

要求：
1. 只写你确实听到的。听不出来的项写「听不出」，不要推测。
2. 「音色质感」用具体词：沙哑 / 清亮 / 圆润 / 干燥 / 带气声 / 鼻音重 / 胸腔共鸣厚。
3. 「一句话音色指令」20 字内，是给语音合成用的，例如：
   中年男声，低沉温厚，语速偏慢，句尾略收。
4. 全程中文。"""


def _first(paths):
    for p in paths:
        for hit in sorted(glob.glob(p)):
            if os.path.isfile(hit):
                return hit
    return None


def find_tool():
    env = os.environ.get("VOICE_MTMD")
    if env and os.path.isfile(env):
        return env
    hit = _first(BIN_CANDIDATES)
    if hit:
        return hit
    for name in ("llama-mtmd-cli", "llama-mtmd"):
        p = shutil.which(name)
        if p:
            return p
    return None


def find_models():
    m = os.environ.get("VOICE_OMNI_MODEL") or _first(MODEL_CANDIDATES)
    mp = os.environ.get("VOICE_OMNI_MMPROJ") or _first(MMPROJ_CANDIDATES)
    return m, mp


def probe(audio):
    """物理测量。复用 studio_ai 里的实现，保证只有一份真源。"""
    import studio_ai
    return studio_ai.probe_audio(audio)


def omni_describe(audio, timeout=300, extra=None):
    """让 Omni 真的听一遍。失败一律返回 ok=False + 原因，绝不编。"""
    tool = find_tool()
    model, mmproj = find_models()
    if not tool:
        return {"ok": False, "why": "没找到 llama-mtmd-cli（先装 llama.cpp，见 docs/音色识别方案.md）"}
    if not model or not mmproj:
        return {"ok": False, "why": "没找到 Qwen2.5-Omni 权重或 mmproj"}
    argv = [tool, "-m", model, "--mmproj", mmproj, "--audio", audio,
            "-p", PROMPT, "-n", "256", "--temp", "0.2", "-ngl", "99"]
    if extra:
        argv += list(extra)
    try:
        p = subprocess.run(argv, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "why": f"跑超时（>{timeout}s）"}
    except Exception as e:
        return {"ok": False, "why": f"起不来：{e}"}
    out = (p.stdout or b"").decode("utf-8", "ignore")
    err = (p.stderr or b"").decode("utf-8", "ignore")
    if p.returncode != 0:
        tail = (err.strip().splitlines() or [""])[-3:]
        return {"ok": False, "why": f"exit={p.returncode} " + " / ".join(tail)[:300]}
    # llama-mtmd-cli 会把 prompt 回显一遍，取最后一个「性别与年龄段」之后的部分
    idx = out.rfind("性别与年龄段")
    text = out[idx:] if idx >= 0 else out
    text = re.sub(r"^\s*(assistant|Assistant)\s*", "", text.strip())
    fields = {}
    for line in text.splitlines():
        line = line.strip()
        for key in ("性别与年龄段", "音区与音色质感", "语速与节奏", "口音或地域特征",
                    "情绪与说话姿态", "一句话音色指令"):
            if line.startswith(key):
                v = line[len(key):].lstrip("：: 　").strip()
                if v:
                    fields[key] = v
    return {"ok": True, "text": text.strip()[:1200], "fields": fields,
            "tool": tool, "model": os.path.basename(model)}


def profile(audio, want_omni=True, timeout=300):
    out = {"audio": audio, "metrics": None, "omni": None}
    try:
        pr = probe(audio)                     # studio_ai 返回 {ok, metrics}
        out["metrics"] = (pr or {}).get("metrics") if pr.get("ok") else {"error": pr.get("why")}
    except Exception as e:
        out["metrics"] = {"error": f"{type(e).__name__}: {e}"}
    if want_omni:
        out["omni"] = omni_describe(audio, timeout=timeout)
    return out


def main():
    ap = argparse.ArgumentParser(description="音色画像：物理量 + Omni 听觉描述")
    ap.add_argument("audio")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-omni", action="store_true", help="只跑物理测量（不加载 9GB 模型）")
    ap.add_argument("--timeout", type=int, default=300)
    a = ap.parse_args()
    if not os.path.isfile(a.audio):
        sys.exit(f"没有这个文件：{a.audio}")
    r = profile(a.audio, want_omni=not a.no_omni, timeout=a.timeout)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return
    m = r.get("metrics") or {}
    print(f"音频　{os.path.basename(a.audio)}")
    if m.get("error"):
        print(f"  物理测量失败：{m['error']}")
    else:
        print(f"  基频 F0　{m.get('f0_hz')} Hz")
        print(f"  时长　　{m.get('duration')} s　响度 {m.get('loudness_dbfs')} dBFS")
        print(f"  静音占比 {round((m.get('silence_ratio') or 0) * 100)}%"
              f"　音节率 {m.get('syllables_per_sec')} /s")
    o = r.get("omni") or {}
    if a.no_omni:
        print("\n听觉描述：已跳过（--no-omni，只量物理量）")
        return
    print()
    if o.get("ok"):
        print("听觉描述（Omni 实听）：")
        for k, v in (o.get("fields") or {}).items():
            print(f"  {k}：{v}")
        if not o.get("fields"):
            print(o.get("text", ""))
    else:
        print(f"听觉描述：未运行 —— {o.get('why')}")
        print("  （物理量仍然可用；要听出音色质感需装 llama.cpp，见 docs/音色识别方案.md）")


if __name__ == "__main__":
    main()
