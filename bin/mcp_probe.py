#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP 自检：真握手一次，把「能不能被别的 AI 调用」这个事实测出来。

为什么不能只看文件在不在
========================
`mcp_server.py` 存在、venv 也建好了，不代表挂得上：
mcp 包升级会改 FastMCP 的导入路径、协议版本号会变、工具函数签名写错会
在握手阶段就抛异常。这些**只有真的走一遍 stdio 握手才会暴露**。

输出：最后一行是 JSON（给 ui_server 的 /api/mcp 解析），前面的行是人话。
退出码：0 = 能用；1 = 挂不上。

单独跑：
  mcp-servers/ip-studio/.venv/bin/python bin/mcp_probe.py
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRV = os.path.join(HERE, "mcp_server.py")
VENV_PY = os.path.join(ROOT, "mcp-servers", "ip-studio", ".venv", "bin", "python")
PROTOCOL = "2024-11-05"


def main():
    py = VENV_PY if os.path.exists(VENV_PY) else sys.executable
    if not os.path.exists(SRV):
        return fail(f"没有 {SRV}")

    try:
        p = subprocess.Popen([py, SRV], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, bufsize=1,
                             cwd=ROOT, env=dict(os.environ, PYTHONUNBUFFERED="1"))
    except Exception as ex:
        return fail(f"起不来：{type(ex).__name__} {ex}")

    def send(obj):
        p.stdin.write(json.dumps(obj) + "\n")
        p.stdin.flush()

    def recv(want_id=None, limit=40):
        """收到匹配 id 的那条就返回。服务端会插 notifications/progress 之类，跳过。"""
        for _ in range(limit):
            line = p.stdout.readline()
            if not line:
                return None
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if want_id is None or msg.get("id") == want_id:
                return msg
        return None

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": PROTOCOL, "capabilities": {},
                         "clientInfo": {"name": "ui-probe", "version": "1"}}})
        init = recv(1)
        if not init or "result" not in init:
            return fail(f"initialize 没成功：{json.dumps(init, ensure_ascii=False)[:200]}")
        server = init["result"].get("serverInfo") or {}

        send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tl = recv(2)
        tools = ((tl or {}).get("result") or {}).get("tools") or []
        if not tools:
            return fail("tools/list 是空的 —— 服务起来了但一个工具都没注册")

        names = [t["name"] for t in tools]
        print(f"✓ 握手成功  服务 {server.get('name')} {server.get('version')}")
        print(f"✓ 工具 {len(tools)} 个：{', '.join(names)}")

        # 再真调一次只读工具，证明不只是「列得出来」
        probe_tool = "status" if "status" in names else names[0]
        send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
              "params": {"name": probe_tool, "arguments": {}}})
        cr = recv(3)
        ok_call = False
        if cr and "result" in cr:
            for it in (cr["result"].get("content") or []):
                if it.get("type") == "text" and it.get("text", "").strip():
                    ok_call = True
        print(("✓ " if ok_call else "✗ ") + f"实调 {probe_tool}() "
              + ("返回了内容" if ok_call else "没返回内容"))

        print(json.dumps({"server": server, "tools": names,
                          "probe_call": probe_tool, "probe_ok": ok_call},
                         ensure_ascii=False))
        return 0 if ok_call else 1
    finally:
        try:
            p.terminate()
            p.wait(timeout=5)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass


def fail(msg):
    print(f"✗ {msg}")
    print(json.dumps({"error": msg, "tools": []}, ensure_ascii=False))
    return 1


if __name__ == "__main__":
    sys.exit(main())
