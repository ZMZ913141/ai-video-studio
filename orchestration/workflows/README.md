# ComfyUI Workflow 扩展指南

本目录用来放 **API 格式**（不是可视化画布格式）的 ComfyUI workflow JSON。
服务启动时会从 `COMFYUI_WORKFLOWS_DIR`（默认 OpenMontage 的
`tools/_comfyui/workflows/`）加载以下已打包模型：

| 名字 | 用途 | 输出节点 |
|------|------|----------|
| `flux2-txt2img` | FLUX2 文生图 | 13 |
| `wan22-t2v-4step` | WAN2.2 14B 文生视频（4步加速） | 16 |
| `wan22-i2v-4step` | WAN2.2 14B 图生视频 | 108 |
| `ace-step-1-t2a` | ACE-Step v1 文生音乐 | 10 |

## 换成「最新模型」只需 3 步

1. 在 ComfyUI 里搭好新工作流，菜单 `Export (API Format)` 导出 JSON。
2. 放进 `COMFYUI_WORKFLOWS_DIR`（或指向你的目录）。
3. 在 `pipeline-service/app.py` 的 `WORKFLOW_REGISTRY` 里加一条，填对
   `file` + 注入节点映射（`prompt_node` / `size_node` / `seed_node` / `output_node`）。

之后 n8n / 任意 HTTP 客户端用 `{"workflow":"你的名字","prompt":"..."}` 调用即可，
**零改码**。图片编辑、TTS（F5-TTS / IndexTTS 等 ComfyUI 节点）、新视频模型都按此接入。

> 提示：节点 ID 用 ComfyUI 的「Node ID 显示」或看导出的 JSON key 即可确认。
