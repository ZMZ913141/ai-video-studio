# 剪映级字幕（逐词高亮 / Karaoke）

`capcut_ass.py` 把 **word-level 时间戳** 转成剪映风格的 ASS 字幕：
未读的字是灰色，被说到时**从左到右填充成高亮色**，并带轻微"弹一下"的缩放。

原理：ASS 的 `\kf` 标签先用 SecondaryColour 绘制音节，再用 PrimaryColour 从左往右覆盖
——这正是剪映/抖音那种"逐词点亮"的效果；`\t` 变换负责弹跳动画。

## 1. 拿到 word-level 时间戳

推荐 **faster-whisper**（本地、免费、自带 word_timestamps）：

```bash
pip install faster-whisper
python - <<'PY'
from faster_whisper import WhisperModel
m = WhisperModel("small", device="cpu", compute_type="int8")
segs, _ = m.transcribe("input.mp4", word_timestamps=True)
import json
json.dump({"segments":[{"words":list(s.words)} for s in segs]},
          open("words.json","w"), ensure_ascii=False)
PY
```

也接受任意格式：`[{"word":"x","start":0,"end":1}]` 或 whisperx / faster-whisper 的 segments。

## 2. 生成 ASS

```bash
python capcut_ass.py -i words.json -o captions.ass \
  --preset capcut --width 1080 --height 1920
```

预设：

| preset | 效果 |
|---|---|
| `capcut` | 白字高亮，未读灰，黑描边，底部居中，带弹跳（默认） |
| `capcut-highlight` | 说到变**黄色**（剪映常见"关键词高亮"） |
| `capcut-flat` | 无弹跳，纯填充（兼容性最好） |

常用覆盖：`--font "思源黑体" --font-size 72 --no-pop`

## 3. 烧录

```bash
ffmpeg -i input.mp4 -vf "subtitles=captions.ass" \
  -c:v libx264 -crf 18 -c:a copy out.mp4
```
也可直接调本栈 `pipeline-service` 的 `POST /burn`。

## 4. 想要更高阶的效果？

ASS karaoke 已经覆盖 90% 剪映观感。若要**入场动画、逐字弹跳、花字模板**这类
真正动效级的字幕，走 **Remotion**：
OpenMontage 自带 `tools/video/remotion_caption_burn.py`（TikTok 风格逐词字幕），
与 `remotion-composer/` 配套。代价是要起 Node 渲染，比 ffmpeg 慢。
