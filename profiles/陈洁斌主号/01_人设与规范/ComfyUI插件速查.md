# ComfyUI 插件速查（洁斌资产项目专用）

> 2026-10-26 实测沉淀。ComfyUI 本体在 `/cfu/ComfyUI`，API `http://127.0.0.1:8188`，2747 个节点。
> 原则：生图优先 Qwen-Image-2.1，不理想再退 SatoDiveH3；其它需求优先在 ComfyUI 插件里找。

---

## 一、Qwen-Image-2.1 生图（定版配方，已验证）

### 模型组合（16G 显存实测可跑）
| 部件 | 文件 | 说明 |
|---|---|---|
| UNet | `diffusion_models/qwen_image_2.1_turbo_int8_convrot.safetensors` | **turbo 8 步，优先用**；非 turbo 版 20 步慢一倍多 |
| 文本编码器 | `text_encoders/qwen3vl_8b_int8_convrot.safetensors` | **必须 Qwen3-VL-8B 架构**，配 `CLIPLoader type=qwen_image` |
| VAE | `vae/qwen_image_2.1_vae_bf16.safetensors` | |

### 节点链（API 格式）
```
UNETLoader → KSampler.model
CLIPLoader(type=qwen_image) → TextEncodeQwenImage21.clip
VAELoader → TextEncodeQwenImage21.vae → VAEDecode
LoadImage(底图) → TextEncodeQwenImage21 的 images.image_1（API 扁平键写法）
TextEncodeQwenImage21 → positive/negative/latent（latent 尺寸自动跟随第一张参考图）
KSampler → VAEDecode → SaveImage
```
参数：turbo 用 **steps 8 / cfg 1.0 / euler / simple / denoise 1.0**；`resolution=0` 让输出保持底图尺寸。
脚本：`样片_主号测试_2026-10-26/gen_still_cb_qwen.py`（支持 PLAN=A/B/C、SEED、CLIP 环境变量覆盖）。

### 三个坑（都踩过）
1. **`qwen3.5_9b_..._pe_i2i/pe_t2i` 不是编辑编码器**！它们是"提示词改写器"（Prompt Enhancer，给 T8 改写节点用）。拿它配 `type=qwen_image` 会走错架构分支，参考图被完全无视 → 输出纯文生图（换了三次脸就是这么来的）。
2. **编辑提示词要指令式短句**：`Change the background into X. Preserve the man ... exactly as in the original image.` 长清单重新罗列人物特征（re-inventory）反而把编辑带偏。
3. 参考图传参是 autogrow 扁平键：`"images.image_1": ["节点ID", 0]`，不是嵌套 dict。

### 图生图提示词模板（换背景）
```
Change the plain white background of this photo into <场景一句话>：墙面/植物/灯/景深。
Preserve the man, his rimless rectangular glasses, his black brushed-up hair, his
cream-white mandarin-collar shirt with frog closures, his pose and the original
chest-up framing exactly as in the original image. Real photography quality, vertical 9:16.
```

### 后备路线：SatoDiveH3Image（Qwen 不理想时用）
- 模型：Minimax-h3_fl2va_pruned_int8_convrot + qwen3vl_32b_minimax CLIPLoader(type=minimax) + h3 video vae
- 参数：30 步 er_sde/simple、turbo LoRA `minimax_h3_turbo_v4_step600_ema` 0.38、`ref_image_1..9` + 提示词里 `<Picture N>` 引用、`ref_image_size=max` 保身份
- 强项：参考图身份保持极强；弱项：慢、编辑指令遵循不如 Qwen

---

## 二、T8 提示词技巧（comfyui-minimax-h3-prompt-enhancer-T8）

### 文生图 8 步法（official_skills/qwen-image-2.1/SKILL.md）
1. **拆需求**：用户定死的（文字/物体/数量/颜色/位置/比例）逐字保留；没说的你来补——短需求也要写满
2. **定画幅**：比例只写进 `wh_ratio` 字段，绝不写进描述文本
3. **开头一句**（约 20 词）：媒介+风格+主体+背景，`The image is a vertical realistic photograph of ...`
4. **先列清单再动笔**：8-14 个位置短语（角落/边缘/中心都要覆盖）+ 所有要出现的文字
5. **走框描述**：多区域图按 背景→顶部→左中右→底部；单主体图按 背景→姿态→头脸→身体服装→手中物；1/3 句子以位置短语开头
6. **画面文字**：直角双引号原文照录（中文 stay 中文）；远处看不清的就写 blurred，别编字母
7. **光线单独一句**：光源、方向、软硬、留下的影子
8. **收尾一句**：`The overall composition ...` 平衡/色调/风格/氛围，只写一句

**通篇规则**：观察者视角、现在时、陈述句；禁 "masterpiece/8K/highly detailed"；颜色必须带修饰（deep navy / muted olive / pale cream）；写材质不写光秃名词；枚举不概括（禁 several/various）；人物写可见表面（年龄用人生阶段不用数字）；物理自洽（影子方向/反光/比例）。总长约 20 句 400-500 词，与需求长短无关。

### 编辑契约 v1（resources/qwen-image-edit-t8.md）
- **开头即操作**，改动区域要无可误解；区域之外的一切靠"指原图"保留
- **指原图，别重列**：面部/产品/配饰/构图 refer to the original image，不要长清单重发明
- 不加未要求的清理或物体；小改动不硬凑长文
- 多图用 `<image1>/<image2>` token 并说明各自角色（画布 vs 风格参考）；单图自然提及即可
- 局部编辑跟随目标图构图；扩图才改画幅；4K 之类不算尺寸

### 改写节点（可选自动化）
`QwenImage21PromptEnhancerT8`：输入中文/英文 brief → 输出 rewritten_prompt + wh_ratio。
5 个渠道：贞贞平价小屋（云 API）/ OpenAI 兼容 / **本地 GGUF（离线，Qwen-Image-2.1-PE-I2I/T2I.Q5 gguf 就在 `/cfu/ComfyUI/models/LLM`）** / Strata 桥接。
`rewrite_profile=编辑专用(T8)` 只在图像编辑模式生效：只改指定部分、`<imageN>` 标记、沿用原画布。
配套 `T8PromptInspector` 检查提示词。

---

## 三、H3 视频提示词结构（出正片直接套用）

official_skills/h3-prompt-writing/references/base-en.txt（15KB，含完整案例）。

### 三段式核心字段
```
integrated_multimodal_description: [Shot 1] ...（主体，按时间轴）
overall_soundscape: ...（1-4 句环境音/动作音/非语言人声，全片汇总）
non_diegetic_music: ...（1-3 句观众才能听到的配乐，写乐器/速度/强弱变化，禁抽象情绪词）
```

### 语法规则
- 镜头：`[Shot 1]` 无时间戳；之后 `[Shot 2] At 00:03.500, the camera cuts to ...`，切镜时间严格递增且在时长内
- 风格写在 Shot 1 开头：Live-action, cinematic / 2D-animated / 3D CG / claymation / vintage film
- 运镜 = 类型+幅度+速度：`The camera pushes in with small amplitude at slow speed toward ...`（类型：Zoom/Push/Pan/Truck/Tilt/Pedestal/Arc/Tracking/Static/Shake/POV/Roll）
- 说话人：稳定 ID `(S1)`，首次出现给音色/语速描述；`The man (S1) says: <d>[Chinese] 台词原文</d>`——`<d>` 里只放语言标签+台词原文，逐字保留
- **旁白**：`says in an off-screen voiceover: <d>...</d> while his lips remain completely closed.`
- 画内文字：英文双引号原文照录 `A red neon sign reading "营业中" glows above the doorway.`
- 台词跨切镜用 `<scenetrans>`；被片尾截断用 `<cutoff>`
- 参考标签 `<Picture 1>` / `<Video 1>` / `<Audio 1>` 全段一致；Ref2VA 六段式：subject_definitions → summary → retention_analysis → detailed_description → overall_soundscape → non_diegetic_music
- 描述总时长必须匹配视频时长（4-15 秒）

### 视觉风格库（docs/visual-styles.md）
`T8 Color Style Config` + `T8 Creator Style Config` 两个节点 → 接 H3/Seedance 增强器的 `color_style_config` / `creator_style_config` 输入口：
- 200 套色彩：六主色+比例的**软参考**，非 LUT；默认"仅色彩"，选"色彩+光影"才带光位建议
- 400 条导演方法：具体构图/运镜/调度方法，不把人名当生成指令
- 边界：用户原事实、台词、时长、LOCK 优先；固定单镜会自动滤掉需要切镜的方法；实验性功能，需 Off/On 对照验证

---

## 四、常用节点速查

| 需求 | 节点 |
|---|---|
| Qwen 文生图/图生图编码 | `TextEncodeQwenImage21`（原生）/ `TextEncodeQwenImage21GH`（孤海工具箱，带 mode=裁剪/填充 + 局部 mask，横竖互转就用它） |
| Qwen 提示词改写 | `QwenImage21PromptEnhancerT8` |
| H3 静帧 | `SatoDiveH3Image` / `FizgigH3StillLatent+Decode` |
| H3 视频 | `MiniMaxH3ImageToVideo` / `MiniMaxH3ReferenceToVideo` / TimelineDirector 全家 |
| H3 人脸 | `H3FaceSelect` `H3FaceTrackCrop` `H3FaceStitch` `H3FaceMaskSAM`（FaceRefine 插件） |
| TTS | ComfyUI-Breeze-TTS-2（BreezeTTS2VoiceDirection） |
| 人声净化 | comfy-mss（pymss_mss_separate + Apollo） |
| LoRA（Qwen2.1） | `qwen-image-2.1-consistency`（人物一致性）、`qwen-image-2.1-outpaint-v2`（扩图/横竖转）在 `models/loras/qwen2.1/` |
| 视频合成 | VHS（VideoHelperSuite）、KJNodes、LayerStyle |

## 五、运行纪律
- 每个工作流前后 `comfy_free.py` 释放显存（16G 红线）
- Qwen 全家桶（6.8G UNet + 8.9G 编码器）靠 ComfyUI 自动卸载编码器才能塞进 16G，跑时别叠加其它模型
- 生成完把成品拷回工作区归档，output 里的失败版本及时清进回收站
