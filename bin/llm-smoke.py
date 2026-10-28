#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM 供应商体检：对话 / 工具调用 / JSON 全测一遍，不打印密钥。

用法：
    set -a && source config/llm.env && set +a
    python3 bin/llm-smoke.py              # 体检主用 + 备用

注：TTS 已从本栈移除（云端 TTS 效果不达标），配音走本地方案。
多供应商自动切换请看 bin/llm_router.py。
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

TOOLS = [{
    "type": "function",
    "function": {
        "name": "comfyui_generate",
        "description": "提交 ComfyUI 工作流",
        "parameters": {"type": "object",
                       "properties": {"workflow": {"type": "string"},
                                      "params": {"type": "object",
                                                 "properties": {"prompt": {"type": "string"}}}},
                       "required": ["workflow", "params"]},
    }}]


def chat(base, key, model, messages, tools=None, tool_choice=None,
         response_format=None, timeout=90):
    body = {"model": model, "messages": messages, "max_tokens": 300, "temperature": 0.2}
    if tools:
        body["tools"] = tools
    if tool_choice:
        body["tool_choice"] = tool_choice
    if response_format:
        body["response_format"] = response_format
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode())
    return d, time.time() - t0


def probe(name, base, key, model):
    print(f"\n--- {name}: {model} @ {base}")
    if not key:
        print("    ⚠ 未配置 key，跳过")
        return False
    allok = True
    # 1) 对话
    try:
        d, dt = chat(base, key, model, [{"role": "user", "content": "回复四个字：可用正常"}])
        c = d["choices"][0]["message"].get("content", "")
        print(f"    ✓ 对话 {dt:.1f}s  {c[:24]!r}")
    except Exception as e:
        print(f"    ✗ 对话失败: {type(e).__name__} {str(e)[:120]}")
        return False
    # 2) 强制工具调用（编排能力的关键）
    try:
        d, dt = chat(base, key, model,
                     [{"role": "user", "content": "用 comfyui_generate 提交 wan22-t2v，prompt=雪地里的橘猫"}],
                     tools=TOOLS, tool_choice={"type": "function",
                                               "function": {"name": "comfyui_generate"}})
        msg = d["choices"][0]["message"]
        tcs = msg.get("tool_calls") or []
        if tcs:
            args = tcs[0]["function"]["arguments"]
            print(f"    ✓ 工具调用 {dt:.1f}s  {tcs[0]['function']['name']}({args[:60]}...)")
        else:
            print(f"    ✗ 未返回 tool_calls（只回了文本）→ 编排不可用")
            allok = False
    except Exception as e:
        print(f"    ✗ 工具调用失败: {type(e).__name__} {str(e)[:120]}")
        allok = False
    return allok


def main():
    env = os.environ
    ok_any = True
    ok_any &= probe("主用", env.get("LLM_BASE_URL", ""), env.get("LLM_API_KEY", ""),
                    env.get("LLM_MODEL", ""))
    if env.get("LLM_FALLBACK_API_KEY"):
        ok_any &= probe("备用", env.get("LLM_FALLBACK_BASE_URL", ""),
                        env.get("LLM_FALLBACK_API_KEY", ""),
                        env.get("LLM_FALLBACK_MODEL", ""))
    print("\n" + ("✓ 全部通过" if ok_any else "✗ 有项目失败"))
    sys.exit(0 if ok_any else 1)


if __name__ == "__main__":
    main()
