"""
om-comfyui MCP server 的 ComfyUI 客户端（与 pipeline-service/app.py 同源逻辑，
自包含、不依赖 OpenMontage 运行时，零 AGPL 负担）。

复用 OpenMontage 已打包 workflow 的真实节点 ID：
  flux2-txt2img / wan22-t2v-4step / wan22-i2v-4step / ace-step-1-t2a
"""
import os
import sys
import json
import time
import uuid
from pathlib import Path

import requests

# 复用统一的资源护栏（显存/内存），避免 MCP 与 pipeline-service 两份实现漂移
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "orchestration"))
import comfy_guard  # noqa: E402

COMFYUI_SERVER_URL = os.environ.get("COMFYUI_SERVER_URL", "http://localhost:8188")
WORKFLOWS_DIR = Path(os.environ.get(
    "COMFYUI_WORKFLOWS_DIR",
    "/home/qmr/github/ai-video-studio/OpenMontage/tools/_comfyui/workflows"))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "./output"))
POLL_TIMEOUT = int(os.environ.get("POLL_TIMEOUT", "3600"))
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "5"))
DEFAULT_CLIENT_ID = "openmontage-mcp"

# 资源护栏开关：提交前预检（不足则自动释放，仍不足直接拒绝，避免白烧 GPU 时间）
AUTO_GUARD = os.environ.get("COMFY_AUTO_GUARD", "1") == "1"
MIN_RAM_GB = float(os.environ.get("GUARD_MIN_RAM_GB", "12"))
MIN_VRAM_GB = float(os.environ.get("GUARD_MIN_VRAM_GB", "6"))
# 任务结束后是否自动腾资源：只有在「余量低于水位 1.5 倍」时才真释放，
# 连续作业时避免无谓的模型反复加载/卸载
AUTO_RELEASE = os.environ.get("COMFY_AUTO_RELEASE", "1") == "1"

WORKFLOW_REGISTRY = {
    "flux2-txt2img": {
        "file": "flux2-txt2img.json",
        "prompt_node": "4", "prompt_field": "text",
        "size_node": "6", "steps_node": "10",
        "seed_node": "7", "seed_field": "noise_seed", "output_node": "13",
    },
    "wan22-t2v-4step": {
        "file": "wan22-t2v-4step.json",
        "prompt_node": "2", "prompt_field": "text",
        "size_node": "11", "seed_node": "12", "seed_field": "noise_seed",
        "output_node": "16",
    },
    "wan22-i2v-4step": {
        "file": "wan22-i2v-4step.json",
        "prompt_node": "93", "prompt_field": "text",
        "image_node": "97", "image_field": "image",
        "size_node": "98", "seed_node": "86", "seed_field": "noise_seed",
        "output_node": "108",
    },
    "ace-step-1-t2a": {
        "file": "ace-step-1-t2a.json",
        "prompt_node": "2", "prompt_field": "tags",
        "lyrics_node": "2", "lyrics_field": "lyrics",
        "duration_node": "4", "duration_field": "seconds",
        "seed_node": "8", "seed_field": "seed", "output_node": "10",
    },
}


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
            raise RuntimeError(f"ComfyUI node_errors: {data['node_errors']}")
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
        """提交前预检：不够就自动释放再复检，仍不够抛异常（拒绝开跑）。"""
        if not AUTO_GUARD:
            return
        if not comfy_guard.preflight(MIN_RAM_GB, MIN_VRAM_GB, auto_free=True):
            raise RuntimeError(
                "资源不足，已拒绝提交（宁可不开工，也不跑到一半被内核 OOM 杀）。"
                " 处理：重启 ComfyUI 释放累积内存，或调低分辨率/帧数。")

    def release(self, force: bool = False) -> bool:
        """任务后腾资源。force=False 时只在余量低于水位 1.5 倍才真释放。"""
        if not AUTO_RELEASE and not force:
            return False
        if not force:
            m = comfy_guard.mem_info()
            vf = comfy_guard.vram_info()[0]
            room_ok = (m["avail"] >= MIN_RAM_GB * 1.5 and
                       (vf is None or vf >= MIN_VRAM_GB * 1.5))
            if room_ok:
                return False
        comfy_guard.do_free()
        return True


def generate(workflow: str, workflow_path: str | None = None, prompt: str | None = None,
             lyrics: str | None = None, width: int | None = None, height: int | None = None,
             length: int | None = None, steps: int | None = None, seed: int | None = None,
             duration: int | None = None, reference_image: str | None = None,
             output_dir: str | None = None, filename_prefix: str | None = None) -> dict:
    client = ComfyUIClient(COMFYUI_SERVER_URL)
    if not client.is_available():
        raise RuntimeError(f"ComfyUI 不可达: {COMFYUI_SERVER_URL}")

    if workflow_path:
        p = Path(workflow_path)
    else:
        if workflow not in WORKFLOW_REGISTRY:
            raise ValueError(f"未知 workflow '{workflow}'，可用: {list(WORKFLOW_REGISTRY)}")
        p = WORKFLOWS_DIR / WORKFLOW_REGISTRY[workflow]["file"]
    if not p.exists():
        raise FileNotFoundError(f"workflow 文件不存在: {p}")

    wf = json.loads(p.read_text())
    meta = WORKFLOW_REGISTRY.get(workflow, {})

    def inject(node, field, value):
        if node and field and value is not None and node in wf:
            wf[node]["inputs"][field] = value

    inject(meta.get("prompt_node"), meta.get("prompt_field"), prompt)
    inject(meta.get("lyrics_node"), meta.get("lyrics_field"), lyrics)
    if width and meta.get("size_node") in wf:
        wf[meta["size_node"]]["inputs"]["width"] = width
    if height and meta.get("size_node") in wf:
        wf[meta["size_node"]]["inputs"]["height"] = height
    if length and meta.get("size_node") in wf:
        wf[meta["size_node"]]["inputs"]["length"] = length
    inject(meta.get("steps_node"), "steps", steps)
    inject(meta.get("duration_node"), meta.get("duration_field"), duration)
    inject(meta.get("seed_node"), meta.get("seed_field"), seed)
    if reference_image:
        server_name = client.upload_image(Path(reference_image))
        inject(meta.get("image_node"), meta.get("image_field"), server_name)
    if filename_prefix and meta.get("output_node") in wf:
        out = wf[meta["output_node"]]["inputs"]
        if "filename_prefix" in out:
            out["filename_prefix"] = filename_prefix

    out_dir = Path(output_dir) if output_dir else (OUTPUT_DIR / workflow)
    out_dir.mkdir(parents=True, exist_ok=True)

    client.preflight()          # 提交前预检：不足则自动释放，仍不足就拒绝
    prompt_id = client.submit(wf)
    try:
        history = client.poll(prompt_id)
    finally:
        # 无论成功失败都腾一次资源（只在余量不足时真的释放）
        client.release()
    outputs = history.get("outputs", {}).get(meta.get("output_node", "13"), {})
    for key in ("images", "gifs", "audio"):
        items = outputs.get(key)
        if items:
            item = items[0]
            dest = out_dir / f"{workflow}_{uuid.uuid4().hex[:8]}"
            artifact = client.download(item["filename"], item.get("subfolder", ""),
                                       item.get("type", "output"), dest)
            return {"prompt_id": prompt_id, "output_path": str(artifact), "workflow": workflow}
    raise RuntimeError(f"output_node {meta.get('output_node')} 未产出文件: {outputs}")
