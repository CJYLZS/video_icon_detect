# 和平精英击倒图标检测

固定 UI 槽位 ROI + 二值 template（前景一致率 + 灰度 ZNCC），可选轻量 CNN 分类器。

## 环境

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
```

使用 `.\.venv\Scripts\python.exe`，勿直接用系统 `python`。

## 统一入口 `main.py`

| 子命令 | 功能 |
|--------|------|
| `extract` | 从视频按帧号/时间/范围导出图片 |
| `template` | 从**单张整帧图片**生成二值模板 |
| `train` | 在帧目录上训练可选分类器 |
| `infer` | 对视频或帧目录推理 |

### 1. 抽帧

```powershell
.\.venv\Scripts\python.exe main.py extract --video test.mp4 --output-dir frames_38_45 --start-sec 38 --end-sec 45

# 按频率抽帧：每秒 1 张（30fps 视频约每隔 30 帧取 1 张）
.\.venv\Scripts\python.exe main.py extract --video test.mp4 --output-dir frames --fps 1

# 指定时间段 + 抽帧频率
.\.venv\Scripts\python.exe main.py extract --video test.mp4 --output-dir frames --start-sec 10 --end-sec 60 --fps 2
```

`extract` 在时间过滤上使用真实帧时间戳（PTS），可正确处理可变帧率（VFR）视频。

### 2. 构造模板（指定一张含图标的截图）

```powershell
.\.venv\Scripts\python.exe main.py template --image frames_140_160\frame_00153.jpg
```

产物在 `template/`；ROI 检查图在 `output/roi_check/`。

### 3. 训练分类器（可选）

`frames/` 目录结构：

```text
frames/
  positive/   # 含击倒图标
  negative/   # 无图标 / 误检 hard negative
```

```powershell
# 推荐：按目录训练（正/负已分好；输入为 ROI 二值图，与模板一致）
.\.venv\Scripts\python.exe main.py train --positive-dir frames\positive --negative-dir frames\negative --epochs 80

# 混合目录 + 帧号打标签（旧方式）
.\.venv\Scripts\python.exe main.py train --image-dir frames_140_160 --positive-from 153

# 混合目录 + 额外负样本目录
.\.venv\Scripts\python.exe main.py train --image-dir frames_mix --positive 2665 2671 --negative-dir frames\negative
```

### 4. 推理

```powershell
.\.venv\Scripts\python.exe main.py infer --image-dir frames_140_160 --output-dir output\icon_detect
```

对视频：

```powershell
.\.venv\Scripts\python.exe main.py infer --video test.mp4 --start-frame 140 --end-frame 160
```

启用分类器（需先 `train`）：

```powershell
# 模板 + 分类器（两者都通过才命中）
.\.venv\Scripts\python.exe main.py infer --video test.mp4 --fps 10 --use-classifier

# 仅分类器（只看 cls 概率，忽略模板阈值）
.\.venv\Scripts\python.exe main.py infer --video test.mp4 --fps 10 --cls-only
```

`clip` 同样支持 `--use-classifier` 与 `--cls-only`。

### 击杀集锦剪辑 `clip`

在指定时间窗内按 `--fps` 采样检测击倒图标（省略 `--start-sec` / `--end-sec` 则处理整片视频），自动合并击杀区间并导出集锦：

```powershell
.\.venv\Scripts\python.exe main.py clip --video test.mp4 --fps 10 --use-classifier -o output/clips/highlight.mp4
.\.venv\Scripts\python.exe main.py clip --video test.mp4 --start-sec 70 --end-sec 80 --fps 10 --use-classifier `
  --meta-json output/clips/ranges.json
```

规则：每段击杀取「开始−1s ~ 结束+1s」；相邻击杀间隔 &lt; 2s 则合并；**最后一段**延伸到 `min(--end-sec, 视频结束)`（未指定 `--end-sec` 时为视频结尾）。

## 模块

| 文件 | 说明 |
|------|------|
| `main.py` | CLI 入口 |
| `infer.py` | 检测流水线（读帧 + 模板/分类器） |
| `clip.py` | 击杀区间合并与 ffmpeg 集锦导出 |
| `frame_source.py` | `FrameSource`、视频/图片读帧 |
| `sampling.py` | 按时间范围规划帧索引 |
| `extract.py` | 视频抽帧导出 |
| `icon_roi.py` | ROI、二值化、template 匹配 |
| `video_index.py` | ffprobe PTS 索引 |
| `classifier.py` | 可选 CNN（ROI 二值化；输入宽高比与 `template/roi_size` 一致，默认约 96×50） |
| `preview.py` | 模板 ROI 预览图 |
| `paths.py` | 根目录、ffmpeg/ffprobe、输出目录等路径常量 |
| `media_io.py` | 兼容 re-export（旧 import 仍可用） |

`extract_frames.py` / `detect_icon.py` 仅为旧命令兼容，新用法请用 `main.py`。

## ROI 参数

在 `icon_roi.py` 顶部 `DETECT_ROI_*` 按分辨率比例定义；换布局后重调并重新 `template`。

## 目录

- `template/` — 模板与 `knockdown_meta.json`
- `output/` — 检测结果（git 忽略）
