"""
pipeline-service — OpenMontage + ComfyUI + ffmpeg(ASS) 字幕烧录的轻量编排服务

设计目标：
  - 让 n8n（或任何 HTTP 客户端）能以 REST 方式驱动 ComfyUI 生成 + 字幕烧录。
  - 不依赖 OpenMontage 运行时，零 AGPL 负担；但可直接加载 OpenMontage 已打包的
    workflow JSON（默认指向 OpenMontage 的 tools/_comfyui/workflows/）。
  - ComfyUI 独占本地 GPU；本服务只是普通 CPU 进程。

环境变量：
  COMFYUI_SERVER_URL   默认 http://localhost:8188
  COMFYUI_WORKFLOWS_DIR 默认 /home/qmr/github/ai-video-studio/OpenMontage/tools/_comfyui/workflows
  OUTPUT_DIR            生成产物根目录，默认 ./output
  POLL_TIMEOUT          ComfyUI 等待超时（秒），默认 3600
"""
import os
import sys
import json
import time
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# 统一资源护栏（与 MCP server 共用同一份实现）
# 两种部署都能找到：容器里与 app.py 同目录；本机跑时在上一级 orchestration/
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import comfy_guard  # noqa: E402

COMFYUI_SERVER_URL = os.environ.get("COMFYUI_SERVER_URL", "http://localhost:8188")
WORKFLOWS_DIR = Path(os.environ.get(
    "COMFYUI_WORKFLOWS_DIR",
    "/home/qmr/github/ai-video-studio/OpenMontage/tools/_comfyui/workflows"))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "./output"))
POLL_TIMEOUT = int(os.environ.get("POLL_TIMEOUT", "3600"))
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "5"))

# 资源护栏：提交前预检 + 任务后按需释放（详见 docs/RESOURCE_GUARD.md）
AUTO_GUARD = os.environ.get("COMFY_AUTO_GUARD", "1") == "1"
AUTO_RELEASE = os.environ.get("COMFY_AUTO_RELEASE", "1") == "1"
MIN_RAM_GB = float(os.environ.get("GUARD_MIN_RAM_GB", "12"))
MIN_VRAM_GB = float(os.environ.get("GUARD_MIN_VRAM_GB", "6"))

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Workflow 注册表：名字 -> (文件名, 注入节点映射)
# 注入映射按 OpenMontage 已打包的 4 个 workflow 的真实节点 ID 对齐。
# 想用更新的模型：把新导出的 API-format JSON 丢进 WORKFLOWS_DIR，
# 在 WORKFLOW_OVERRIDES 里加一条即可，零改码。
# ---------------------------------------------------------------------------
WORKFLOW_REGISTRY = {
    "flux2-txt2img": {
        "file": "flux2-txt2img.json",
        "prompt_node": "4", "prompt_field": "text",
        "size_node": "6",                 # width/height
        "steps_node": "10",               # Flux2Scheduler.steps
        "seed_node": "7", "seed_field": "noise_seed",
        "output_node": "13",
    },
    "wan22-t2v-4step": {
        "file": "wan22-t2v-4step.json",
        "prompt_node": "2", "prompt_field": "text",
        "size_node": "11",                # width/height/length
        "seed_node": "12", "seed_field": "noise_seed",
        "output_node": "16",
    },
    "wan22-i2v-4step": {
        "file": "wan22-i2v-4step.json",
        "prompt_node": "93", "prompt_field": "text",
        "image_node": "97", "image_field": "image",
        "size_node": "98",                # width/height/length (WanImageToVideo)
        "seed_node": "86", "seed_field": "noise_seed",
        "output_node": "108",
    },
    "ace-step-1-t2a": {
        "file": "ace-step-1-t2a.json",
        "prompt_node": "2", "prompt_field": "tags",
        "lyrics_node": "2", "lyrics_field": "lyrics",
        "duration_node": "4", "duration_field": "seconds",
        "seed_node": "8", "seed_field": "seed",
        "output_node": "10",
    },
}

DEFAULT_CLIENT_ID = "openmontage-pipeline"


# ---------------------------------------------------------------------------
# ComfyUI REST 客户端（submit / poll / download / upload）
# 契约参考 docs.comfy.org ComfyUI server comms routes。
# ---------------------------------------------------------------------------
class ComfyUIClient:
    def __init__(self, server_url: str):
        self.base = server_url.rstrip("/")

    def is_available(self) -> bool:
        try:
            r = requests.get(f"{self.base}/system_stats", timeout=5)
            return r.status_code == 200
        except Exception:
            return False

    def upload_image(self, local_path: Path) -> str:
        with open(local_path, "rb") as f:
            r = requests.post(f"{self.base}/upload/image",
                              files={"image": (local_path.name, f, "image/png")},
                              timeout=60)
        r.raise_for_status()
        return r.json()["name"]

    def submit(self, workflow: dict) -> str:
        payload = {"prompt": workflow, "client_id": DEFAULT_CLIENT_ID}
        r = requests.post(f"{self.base}/prompt", json=payload, timeout=120)
        r.raise_for_status()
        data = r.json()
        if data.get("node_errors"):
            raise HTTPException(400, f"ComfyUI node_errors: {data['node_errors']}")
        return data["prompt_id"]

    def poll(self, prompt_id: str, timeout: int = POLL_TIMEOUT) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = requests.get(f"{self.base}/history/{prompt_id}", timeout=30)
            r.raise_for_status()
            hist = r.json()
            if prompt_id in hist:
                return hist[prompt_id]
            time.sleep(POLL_INTERVAL)
        raise TimeoutError(f"ComfyUI job {prompt_id} exceeded {timeout}s")

    def download(self, filename: str, subfolder: str, ftype: str, dest: Path) -> Path:
        params = {"filename": filename, "subfolder": subfolder, "type": ftype}
        r = requests.get(f"{self.base}/view", params=params, timeout=300)
        r.raise_for_status()
        dest.write_bytes(r.content)
        return dest

    # ---- 资源护栏 ----
    def preflight(self) -> None:
        """提交前预检：不够自动释放再复检，仍不够直接 503（拒绝白烧 GPU 时间）。"""
        if not AUTO_GUARD:
            return
        if not comfy_guard.preflight(MIN_RAM_GB, MIN_VRAM_GB, auto_free=True):
            raise HTTPException(503, "资源不足：可用内存/显存低于水位且释放后仍不够。"
                                     " 请重启 ComfyUI 或调低分辨率/帧数后再提交。")

    def release(self, force: bool = False) -> bool:
        if not AUTO_RELEASE and not force:
            return False
        if not force:
            m = comfy_guard.mem_info()
            vf = comfy_guard.vram_info()[0]
            if m["avail"] >= MIN_RAM_GB * 1.5 and (vf is None or vf >= MIN_VRAM_GB * 1.5):
                return False
        comfy_guard.do_free()
        return True


def load_workflow(name: str, workflow_path: str | None) -> tuple[dict, dict]:
    if workflow_path:
        p = Path(workflow_path)
    else:
        if name not in WORKFLOW_REGISTRY:
            raise HTTPException(400, f"未知 workflow '{name}'，可用: {list(WORKFLOW_REGISTRY)}")
        p = WORKFLOWS_DIR / WORKFLOW_REGISTRY[name]["file"]
    if not p.exists():
        raise HTTPException(404, f"workflow 文件不存在: {p}")
    return json.loads(p.read_text()), WORKFLOW_REGISTRY.get(name, {})


def inject(workflow: dict, node: str | None, field: str | None, value):
    if node and field and value is not None and node in workflow:
        workflow[node]["inputs"][field] = value


def extract_artifact(history: dict, output_node: str, dest: Path) -> Path:
    outputs = history.get("outputs", {}).get(output_node, {})
    for key in ("images", "gifs", "audio"):
        items = outputs.get(key)
        if items:
            item = items[0]
            return ComfyUIClient(COMFYUI_SERVER_URL).download(
                item["filename"], item.get("subfolder", ""), item.get("type", "output"), dest)
    raise HTTPException(500, f"output_node {output_node} 未产出可下载文件: {outputs}")


# ---------------------------------------------------------------------------
# API 模型
# ---------------------------------------------------------------------------
class GenerateReq(BaseModel):
    workflow: str = Field(..., description="注册表名或任意名字（配合 workflow_path）")
    workflow_path: str | None = None
    prompt: str | None = None
    lyrics: str | None = None
    width: int | None = None
    height: int | None = None
    length: int | None = None           # 视频帧数
    steps: int | None = None
    seed: int | None = None
    duration: int | None = None         # 音频秒数
    reference_image: str | None = None  # 本地图片路径（i2v）
    output_dir: str | None = None
    filename_prefix: str | None = None


class BurnReq(BaseModel):
    video_path: str
    subtitle_path: str
    output_path: str | None = None
    # 可选 ASS 样式覆盖（直接拼进 subtitles 滤镜的 :force_style）
    font_size: int | None = None
    primary_color: str | None = None    # &HBBGGRR 形式


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="OpenMontage+ComfyUI Pipeline Service")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])
client = ComfyUIClient(COMFYUI_SERVER_URL)


@app.get("/health")
def health():
    return {"comfyui_reachable": client.is_available(),
            "server": COMFYUI_SERVER_URL,
            "workflows": list(WORKFLOW_REGISTRY)}


@app.get("/workflows")
def list_workflows():
    return {"registry": list(WORKFLOW_REGISTRY),
            "dir": str(WORKFLOWS_DIR)}


@app.post("/generate")
def generate(req: GenerateReq):
    if not client.is_available():
        raise HTTPException(503, f"ComfyUI 不可达: {COMFYUI_SERVER_URL}")
    workflow, meta = load_workflow(req.workflow, req.workflow_path)

    # 文本/标签注入
    inject(workflow, meta.get("prompt_node"), meta.get("prompt_field"), req.prompt)
    inject(workflow, meta.get("lyrics_node"), meta.get("lyrics_field"), req.lyrics)
    # 尺寸注入
    if req.width and meta.get("size_node") in workflow:
        workflow[meta["size_node"]]["inputs"]["width"] = req.width
    if req.height and meta.get("size_node") in workflow:
        workflow[meta["size_node"]]["inputs"]["height"] = req.height
    if req.length and meta.get("size_node") in workflow:
        workflow[meta["size_node"]]["inputs"]["length"] = req.length
    # 步进 / 时长 / 种子
    inject(workflow, meta.get("steps_node"), "steps", req.steps)
    inject(workflow, meta.get("duration_node"), meta.get("duration_field"), req.duration)
    inject(workflow, meta.get("seed_node"), meta.get("seed_field"), req.seed)
    # 图片（i2v）：先上传，再把服务器文件名写回 LoadImage
    if req.reference_image:
        server_name = client.upload_image(Path(req.reference_image))
        inject(workflow, meta.get("image_node"), meta.get("image_field"), server_name)
    if req.filename_prefix and meta.get("output_node") in workflow:
        on = meta["output_node"]
        wf_out = workflow[on]["inputs"]
        if "filename_prefix" in wf_out:
            wf_out["filename_prefix"] = req.filename_prefix

    out_dir = Path(req.output_dir) if req.output_dir else (OUTPUT_DIR / req.workflow)
    out_dir.mkdir(parents=True, exist_ok=True)

    client.preflight()          # 提交前预检
    prompt_id = client.submit(workflow)
    try:
        history = client.poll(prompt_id)
    finally:
        client.release()        # 任务结束按需腾资源
    dest = out_dir / f"{req.workflow}_{uuid.uuid4().hex[:8]}"
    artifact = extract_artifact(history, meta.get("output_node", "13"), dest)
    return {"prompt_id": prompt_id, "output_path": str(artifact),
            "workflow": req.workflow}


@app.get("/guard/status")
def guard_status():
    """资源现状：内存 / swap / 显存 / ComfyUI 存活。"""
    m = comfy_guard.mem_info()
    vf, vt = comfy_guard.vram_info()
    return {"ram_total_gb": round(m["total"], 1), "ram_avail_gb": round(m["avail"], 1),
            "swap_used_gb": round(m["swap_used"], 1), "swap_total_gb": round(m["swap_total"], 1),
            "vram_free_gb": None if vf is None else round(vf, 1),
            "vram_total_gb": None if vt is None else round(vt, 1),
            "comfyui_alive": vf is not None,
            "watermark": {"min_ram_gb": MIN_RAM_GB, "min_vram_gb": MIN_VRAM_GB}}


@app.post("/guard/free")
def guard_free():
    """强制卸载全部模型并释放内存/显存（等释放生效才返回）。"""
    before = comfy_guard.mem_info()["avail"]
    after = comfy_guard.do_free()
    m = comfy_guard.mem_info()
    vf = comfy_guard.vram_info()[0]
    return {"ram_avail_before_gb": round(before, 1),
            "ram_avail_after_gb": None if after is None else round(after, 1),
            "vram_free_gb": None if vf is None else round(vf, 1)}


@app.post("/guard/preflight")
def guard_preflight(min_ram: float = MIN_RAM_GB, min_vram: float = MIN_VRAM_GB):
    """提交任务前的资源预检；不足则自动释放后复检。ok=false 时别开跑。"""
    okv = comfy_guard.preflight(min_ram, min_vram, auto_free=True)
    m = comfy_guard.mem_info()
    vf = comfy_guard.vram_info()[0]
    return {"ok": okv, "ram_avail_gb": round(m["avail"], 1),
            "vram_free_gb": None if vf is None else round(vf, 1)}


@app.post("/burn")
def burn(req: BurnReq):
    video = Path(req.video_path)
    sub = Path(req.subtitle_path)
    if not video.exists():
        raise HTTPException(404, f"视频不存在: {video}")
    if not sub.exists():
        raise HTTPException(404, f"字幕不存在: {sub}")
    out = Path(req.output_path) if req.output_path else video.with_suffix("").with_name(
        video.stem + "_sub.mp4")
    out.parent.mkdir(parents=True, exist_ok=True)

    style = []
    if req.font_size:
        style.append(f"FontSize={req.font_size}")
    if req.primary_color:
        style.append(f"PrimaryColour={req.primary_color}")
    style_arg = f":force_style='{','.join(style)}'" if style else ""

    # 字幕路径需转义，避免空格/特殊字符
    sub_esc = str(sub).replace("'", "'\\''")
    vf = f"subtitles='{sub_esc}'{style_arg}"
    cmd = ["ffmpeg", "-y", "-i", str(video), "-vf", vf,
           "-c:a", "copy", "-c:v", "libx264", "-crf", "18", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise HTTPException(500, f"ffmpeg 烧录失败:\n{r.stderr[-2000:]}")
    return {"output_path": str(out), "engine": "ffmpeg+ASS"}


@app.post("/render-kdenlive")
def render_kdenlive(req: dict):
    """可选：用 Kdenlive headless 渲染 .mlt 工程（人工精修时间线后调用）。
    需本机安装 kdenlive 且已有 .mlt 工程文件。"""
    mlt = req.get("mlt_path")
    out = req.get("output_path", "kdenlive_render.mp4")
    if not mlt or not Path(mlt).exists():
        raise HTTPException(400, "需提供已存在的 .mlt 工程路径")
    cmd = ["kdenlive", "--render", f"--url={mlt}", f"--out={out}"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise HTTPException(500, f"kdenlive 渲染失败:\n{r.stderr[-2000:]}")
    return {"output_path": out, "engine": "kdenlive"}
