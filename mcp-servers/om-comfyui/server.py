"""
om-comfyui — 把 OpenMontage/ComfyUI 的"生成"能力暴露成 MCP 工具。
这样 Claude Code / Cursor 里的同一个 agent 既能调 Kinocut 做剪辑烧字幕，
又能调本服务做 ComfyUI 生成，无需在多个系统间切换。

运行（需安装依赖）：
  uv run server.py            # 或 python server.py（在已装依赖的 venv 里）
注册到 Claude Code：
  claude mcp add om-comfyui -- uvx --from /abs/path/om-comfyui mcp-server
  # 或本地 venv：
  claude mcp add om-comfyui -- /abs/path/.venv/bin/python /abs/path/server.py
"""
from mcp.server.fastmcp import FastMCP
from comfy_client import generate, WORKFLOW_REGISTRY, COMFYUI_SERVER_URL

mcp = FastMCP("om-comfyui")


@mcp.tool()
def comfyui_generate(
    workflow: str,
    prompt: str | None = None,
    lyrics: str | None = None,
    width: int | None = None,
    height: int | None = None,
    length: int | None = None,
    steps: int | None = None,
    seed: int | None = None,
    duration: int | None = None,
    reference_image: str | None = None,
    output_dir: str | None = None,
    filename_prefix: str | None = None,
    workflow_path: str | None = None,
) -> dict:
    """调用 ComfyUI 生成图/视频/音乐。

    Args:
        workflow: 注册名(flux2-txt2img/wan22-t2v-4step/wan22-i2v-4step/ace-step-1-t2a)
                  或配合 workflow_path 用任意导出的 API-format JSON。
        prompt: 正向提示词（图/视频用）；音乐场景为风格标签。
        lyrics: 音乐歌词（ace-step 可选）。
        width/height: 输出尺寸。
        length: 视频帧数（如 81≈5s@16fps）。
        steps: 采样步数。
        seed: 随机种子。
        duration: 音频秒数。
        reference_image: i2v 的本地参考图路径。
        output_dir: 产物目录。
        filename_prefix: 文件名前缀。
        workflow_path: 自定义 workflow JSON 绝对路径。
    """
    return generate(
        workflow=workflow, workflow_path=workflow_path, prompt=prompt, lyrics=lyrics,
        width=width, height=height, length=length, steps=steps, seed=seed,
        duration=duration, reference_image=reference_image,
        output_dir=output_dir, filename_prefix=filename_prefix,
    )


@mcp.tool()
def list_workflows() -> dict:
    """列出可用的 ComfyUI workflow 注册名。"""
    return {"registry": list(WORKFLOW_REGISTRY),
            "comfyui_server": COMFYUI_SERVER_URL}


@mcp.tool()
def comfyui_health() -> dict:
    """检查 ComfyUI 是否可达。"""
    from comfy_client import ComfyUIClient
    return {"comfyui_reachable": ComfyUIClient(COMFYUI_SERVER_URL).is_available(),
            "server": COMFYUI_SERVER_URL}


@mcp.tool()
def comfyui_status() -> dict:
    """看资源现状：内存/swap/显存 + ComfyUI 是否在跑。出片前先调这个。"""
    import comfy_guard
    m = comfy_guard.mem_info()
    vf, vt = comfy_guard.vram_info()
    return {"ram_total_gb": round(m["total"], 1), "ram_avail_gb": round(m["avail"], 1),
            "swap_used_gb": round(m["swap_used"], 1), "swap_total_gb": round(m["swap_total"], 1),
            "vram_free_gb": None if vf is None else round(vf, 1),
            "vram_total_gb": None if vt is None else round(vt, 1),
            "comfyui_alive": vf is not None}


@mcp.tool()
def comfyui_free() -> dict:
    """强制卸载 ComfyUI 全部模型并释放内存/显存（会等释放真的生效才返回）。
    什么时候用：出片前腾地方；或连续跑了很多任务后内存累积。"""
    import comfy_guard
    before = comfy_guard.mem_info()["avail"]
    after = comfy_guard.do_free()
    m = comfy_guard.mem_info()
    vf = comfy_guard.vram_info()[0]
    return {"ram_avail_before_gb": round(before, 1),
            "ram_avail_after_gb": None if after is None else round(after, 1),
            "ram_avail_now_gb": round(m["avail"], 1),
            "vram_free_gb": None if vf is None else round(vf, 1)}


if __name__ == "__main__":
    mcp.run()
