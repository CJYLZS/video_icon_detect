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

> `extract` 在时间过滤上使用真实帧时间戳（PTS），可正确处理可变帧率（VFR）视频。
```

### 2. 构造模板（指定一张含图标的截图）

```powershell
.\.venv\Scripts\python.exe main.py template --image frames_140_160\frame_00153.jpg
```

产物在 `template/`；ROI 检查图在 `output/roi_check/`。

### 3. 训练分类器（可选）

```powershell
# 显式指定正样本帧号（推荐）
.\.venv\Scripts\python.exe main.py train --image-dir frames --positive 2725 2713 2701 2677 2665

# 或按阈值：>= 153 为正样本
.\.venv\Scripts\python.exe main.py train --image-dir frames_140_160 --positive-from 153
```

### 4. 推理

```powershell
.\.venv\Scripts\python.exe main.py infer --image-dir frames_140_160 --output-dir output\icon_detect
```

对视频：

```powershell
.\.venv\Scripts\python.exe main.py infer --video test.mp4 --start-frame 140 --end-frame 160
```

启用分类器：`infer ... --use-classifier`（需先 `train`）。

## 模块

| 文件 | 说明 |
|------|------|
| `main.py` | CLI 入口 |
| `icon_roi.py` | ROI、二值化、template 匹配 |
| `media_io.py` | 视频/帧读写、抽帧 |
| `classifier.py` | 可选 CNN 分类器 |
| `preview.py` | 模板 ROI 预览图 |

`extract_frames.py` / `detect_icon.py` 仅为旧命令兼容，新用法请用 `main.py`。

## ROI 参数

在 `icon_roi.py` 顶部 `DETECT_ROI_*` 按分辨率比例定义；换布局后重调并重新 `template`。

## 目录

- `template/` — 模板与 `knockdown_meta.json`
- `output/` — 检测结果（git 忽略）
