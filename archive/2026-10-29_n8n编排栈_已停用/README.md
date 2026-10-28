# n8n 编排栈（2026-10-29 停用归档）

这里原样保留 2026-10-26/27 搭的「n8n + pipeline-service」那一套，**现在不参与出片**。
归档而不是删除，因为它是唯一一份「外部系统用 HTTP 触发出片」的现成实现，
将来接客户自助下单/定时发布时能直接捡回来。

## 它当时是干什么的

```
Webhook (n8n) → HTTP 调 pipeline-service:8000 → ComfyUI 生成一段 → ffmpeg 烧 ASS → 返回
```

n8n 工作流只有 4 个节点（`n8n-workflows/n8n-workflow.json`）：
`Webhook 触发` → `调用 ComfyUI 生成` → `字幕烧录 (ffmpeg+ASS)` → `返回结果`。

## 为什么停

1. **它做不出口播片。** 现役一条片是 10 步：TTS 定板 → 留白 → ASR 强制对齐 →
   释放显存 → 分段出画面 → 调色 → 配乐 → 混音 → 生成 ASS → 烧字幕 → 验收。
   n8n 那条链只有「一段画面 + 烧字幕」，缺 TTS、缺词级时间戳、缺分段拼接、缺混音。
2. **现役流水线走 CLI，完全不经过它。** `bin/pipeline.py` 直接 subprocess 调
   `templates/scripts/*.py`，`bin/vstudio.py pipeline` 是唯一入口。
   `bin/` 里对 n8n 的全部引用只剩「服务在线检查」那三行。
3. **它只有一段 demo 的实测记录**，没有跑过任何一条真实交付的片子。

## 真要恢复怎么弄

```bash
cd /home/qmr/github/ai-video-studio
bash archive/2026-10-29_n8n编排栈_已停用/start-native.sh          # 起 pipeline-service :8000
bash archive/2026-10-29_n8n编排栈_已停用/start-native.sh --n8n    # 再起 n8n :5678
```

注意三件事（都是当时踩过的坑，别重踩）：

1. **ComfyUI 只监听 `127.0.0.1:8188`**，容器走 `host.docker.internal`(172.17.0.1) 会被拒
   → pipeline-service 必须 `network_mode: host`。
2. n8n 在 bridge 网络里要访问 pipeline-service，地址写 `http://host.docker.internal:8000`，
   不能写 `http://pipeline-service:8000`。
3. WorkBuddy 的 shell 会注入 `DOCKER_HOST=unix:///run/user/1000/podman/podman.sock`，
   那个 socket 不存在 → 用 docker 前先 `unset DOCKER_HOST`。

`comfy_guard.py` 已经搬去 `bin/comfy_guard.py` —— 它是**运维工具**（内存/显存预检、
批量出片时的 watch 守护），不属于这套 demo 栈，现在主线在用。

## 一套栈是怎么被判定成"该归档"的

判据只有一条：**现役代码里有没有人调它。** 不是"看起来像不像多余"。
`grep -rn "n8n" bin/*.py` 只命中三行 health 检查 → 归档。
