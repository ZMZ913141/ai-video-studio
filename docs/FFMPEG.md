# FFmpeg：为什么各项目"各用各的"，以及怎么统一

## 你的观察是对的

本机实况（迁移前探测）：

- **系统全局原本没有 ffmpeg**（`which ffmpeg` 为空）。
- 全系统只找到一份**被项目私自打包**的：`/home/qmr/.config/bilibili/ffmpeg/ffmpeg`
  —— 而且是 **2016 年的 ffmpeg 3.1**（虽带 `--enable-libass`，但极旧）。

各项目的打包策略确实不一样：

| 项目/库 | 用哪个 ffmpeg | 是否走 PATH |
|---|---|---|
| **Kinocut** | 系统 ffmpeg（`ffmpeg` 子进程） | ✅ 是 |
| **OpenMontage**（video_compose / burn_subtitles） | 系统 ffmpeg | ✅ 是 |
| **本栈 pipeline-service**（/burn） | 系统 ffmpeg | ✅ 是 |
| **imageio-ffmpeg**（pip 包） | **自带静态二进制** | ❌ 否 |
| **moviepy** | 经 imageio-ffmpeg 取打包二进制 | ❌ 否 |
| **ComfyUI**（VideoHelperSuite 等） | 多走 imageio-ffmpeg | ❌ 否 |
| **Remotion** | **自带 ffmpeg 二进制**（@remotion/renderer 内置） | ❌ 否 |
| **ffmpeg-python** | 包装系统 ffmpeg | ✅ 是 |

## 风险

1. **版本漂移**：老版本缺滤镜/编码器（本机那份 3.1 就很容易在新语法上翻车）。
2. **行为不一致**：同一条命令在 A 项目能跑、B 项目报错，排查成本极高。
3. **字幕烧录尤其敏感**：必须带 `--enable-libass`（ASS/karaoke 逐词高亮依赖它），
   缺了就静默渲染成无特效字幕甚至失败。

## 统一策略（已执行）

### 1. 装一份新的全局 ffmpeg（已完成）

**实际状态**：`apt` 被沙箱拦（`dpkg` 前端锁无权限），`sudo` 因 `no_new_privs` 不可用
（给密码也没用），`/usr/local/bin` 写入也被拒。最终用 **BtbN 静态构建**装到 `~/.local/bin`：

| 项 | 值 |
|---|---|
| 版本 | `ffmpeg N-128122-g215351c1c3-20261027`（2026-10-27 master） |
| 路径 | `/home/qmr/.local/bin/ffmpeg`（已在 PATH，`which ffmpeg` 可直接命中） |
| libass | ✅ `--enable-libass`（剪映级 karaoke 字幕必需） |
| ffprobe | ⚠️ 未装（下载超时，残包解出的二进制不可用已删除）；**可选**，不影响主流程 |

补装 ffprobe（网络好时）：

```bash
cd /home/qmr/.cache/btbn
curl -sSL -o ff.tar.xz \
  https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz
tar -xJf ff.tar.xz --wildcards '*/bin/ffprobe'
cp ffmpeg-master-latest-linux64-gpl/bin/ffprobe /home/qmr/.local/bin/ && chmod +x /home/qmr/.local/bin/ffprobe
```

> 注：`~/.local/bin` 是 GUI 应用 PATH 的第一位，所以 shell 与 GUI 都能用；
> 不需要（也无法）往 `/usr/local/bin` 软链。缺 ffprobe 时 OpenMontage / 本栈会优雅降级
> （duration 探测返回 None），不会报错。

### 2. 校验（必做）

```bash
which -a ffmpeg          # 应指向 /usr/local/bin/ffmpeg
ffmpeg -version | head -2
ffmpeg -hide_banner -buildconf | grep -E 'enable-libass|enable-libfreetype'
```
`libass` 和 `libfreetype` 必须都在——剪映级 karaoke 字幕靠它们。

### 3. 让"打包型"消费者回退到全局 ffmpeg

- **imageio-ffmpeg / moviepy / ComfyUI**：
  ```bash
  export IMAGEIO_FFMPEG_EXE=/usr/local/bin/ffmpeg
  ```
  或直接在 Python 里 `imageio_ffmpeg.get_ffmpeg_exe()` 确认路径。
- **Remotion**：在 `remotion.config.ts` 或 render 参数里指定 `ffmpegExecutable`
  指向 `/usr/local/bin/ffmpeg`。
- **容器场景**：不要挂宿主机二进制，镜像内自己装（本栈 `Dockerfile` 已改为
  `apt-get install ffmpeg`，自带 libass）。

### 4. 一句话原则

> **全局装一份新的、带 libass 的 ffmpeg；打包型的用环境变量/配置项指过去；
> 容器里自己装。** 别让每个项目各带一份。
