# 和平精英击倒图标检测

固定 UI 槽位 ROI + 二值 template（前景一致率 + 灰度 ZNCC），可选轻量 CNN 分类器。默认使用分类器模式。

## 环境

使用 `uv` 管理虚拟环境和依赖：

```powershell
uv venv .venv --python 3.11
uv pip install -r requirements.txt
```

Windows-only，路径用 `\`。工具包装了 `tools/ffmpeg-8.1.1-essentials_build/bin/ffmpeg.exe`，不依赖系统 ffmpeg。

## 项目结构

```
video_icon_detect/
├── main.py                          # 薄 CLI 入口
├── src/icon_detect/
│   ├── constants.py                 # 全部路径/算法常量
│   ├── cover_candidates/             # JEV 视频候选封面扫描
│   ├── detection/                   # 检测核心
│   │   ├── roi.py                   # ROI 裁剪、二值化、模板匹配
│   │   ├── classifier.py            # CNN 二分类器
│   │   ├── infer.py                 # 检测流水线
│   │   └── missile.py               # 巡航导弹 OCR
│   ├── video/                       # 视频 I/O
│   │   ├── source.py                # FFmpegSource / FrameSource
│   │   ├── index.py                 # ffprobe PTS 索引
│   │   ├── sampling.py              # 帧采样计划
│   │   └── extract.py               # 帧导出
│   ├── montage/                     # 集锦
│   │   ├── planner.py               # 区间规划
│   │   └── exporter.py              # ffmpeg 导出
│   ├── cli/                         # CLI 层
│   │   ├── parser.py                # argparse 定义
│   │   └── handlers.py              # 命令处理函数
│   └── utils/
│       ├── progress.py
│       └── preview.py
├── tools/                           # 独立工具脚本
│   ├── classify_dir.py
│   ├── profile_infer.py
│   └── visualize_thresholds.py
├── tests/                           # pytest
│   ├── conftest.py
│   └── test_cli_modes.py
└── template/                        # 模板与模型
    ├── knockdown_bin.png / knockdown_gray.png / knockdown_color.jpg
    ├── knockdown_meta.json
    └── icon_classifier.pt           # 训练后的 CNN 权重 (gitignored)
```

## 三种检测模式

| 模式 | 触发方式 | 判定逻辑 |
|---|---|---|
| **仅分类器 (默认)** | 无特殊标志 | CNN prob ≥ `--cls-thresh` (默认 0.8) |
| **模板 + 分类器** | `--use-classifier` | 模板匹配通过 **且** CNN prob ≥ `--cls-thresh` |
| **仅模板** | `--template-only` | 二值相似度 + ZNCC ≥ `--match-thresh` (默认 0.75) |

## 统一入口

```powershell
.venv\Scripts\python.exe main.py <subcommand>
```

| 子命令 | 功能 |
|---|---|
| `extract` | 从视频按帧号/时间/范围导出图片 |
| `export-icons` | 按 `*_icon.jpg` 清单从视频重新导出原始帧 |
| `template` | 从单张整帧图片生成二值模板 |
| `train` | 训练 CNN 分类器 (可选但推荐) |
| `infer` | 对视频或帧目录推理 |
| `clip` | 检测击杀并生成集锦 montage |
| `clip-batch` | 批量剪辑多个视频 |
| `clip-extract` | 从集锦 manifest 中提取特定片段的帧 (用于补充训练样本) |

`clip` / `clip-batch` 默认在主剪辑完成后，对输出目录中的同名主成片执行 JEV 候选封面扫描；扫描结果写入 `<stem>/cover_candidates/`，仅作为人工挑选素材。JEV 服务未启动时提示并跳过，不影响剪辑。可用 `--no-cover-candidates` 关闭；`--cover-fps`、`--jev-url`、`--jev-model` 可调整采样和服务。标题生成、网页 ChatGPT 封面美化及投稿仍须手动单独触发。人工从 `cover_candidates/` 选一张放到视频同级目录并命名为 `封面.jpg`，然后可用 `node scripts/pipeline.mjs` 执行组合流水线。扫描与发布细节见 [候选封面说明](docs/cover-candidates.md) 和 [发布流水线说明](docs/publishing.md)。

### 1. 构造模板

```powershell
.venv\Scripts\python.exe main.py template --image frames_140_160\frame_00153.jpg
```

产物在 `template/`；ROI 检查图在 `output/roi_check/`。

### 2. 抽帧

```powershell
.venv\Scripts\python.exe main.py extract --video test.mp4 --output-dir frames_38_45 --start-sec 38 --end-sec 45

# 按频率抽帧：每秒 1 张
.venv\Scripts\python.exe main.py extract --video test.mp4 --output-dir frames --fps 1

# 指定时间段 + 频率
.venv\Scripts\python.exe main.py extract --video test.mp4 --output-dir frames --start-sec 10 --end-sec 60 --fps 2
```

`extract` 使用真实帧时间戳 (PTS)，可正确处理 VFR 视频。

### 3. 训练分类器 (推荐)

目录结构：

```text
frames/
  positive/   # 含击倒图标
  negative/   # 无图标 / 误检 hard negative
```

```powershell
# 推荐：按目录训练
.venv\Scripts\python.exe main.py train --positive-dir frames\positive --negative-dir frames\negative --epochs 80

# 混合目录 + 帧号打标签
.venv\Scripts\python.exe main.py train --image-dir frames_140_160 --positive-from 153

# 混合目录 + 额外负样本
.venv\Scripts\python.exe main.py train --image-dir frames_mix --positive 2665 2671 --negative-dir frames\negative
```

### 4. 推理

```powershell
# 对帧目录 (默认仅分类器)
.venv\Scripts\python.exe main.py infer --image-dir frames_140_160 --output-dir output\icon_detect

# 对视频
.venv\Scripts\python.exe main.py infer --video test.mp4 --start-sec 140 --end-sec 160

# 切换到模板+分类器模式
.venv\Scripts\python.exe main.py infer --video test.mp4 --fps 10 --use-classifier

# 仅模板模式
.venv\Scripts\python.exe main.py infer --video test.mp4 --fps 10 --template-only
```

### 5. 击杀集锦剪辑

在指定时间窗内按 `--fps` 采样检测击倒图标（省略 `--start-sec`/`--end-sec` 则处理整片），自动合并击杀区间并导出集锦：

```powershell
# 默认仅分类器模式
.venv\Scripts\python.exe main.py clip --video test.mp4 --fps 10 -o output/clips/highlight.mp4

# 限定时间范围 + 元数据导出
.venv\Scripts\python.exe main.py clip --video test.mp4 --start-sec 70 --end-sec 80 --fps 10 `
  --meta-json output/clips/ranges.json

# 模板+分类器模式
.venv\Scripts\python.exe main.py clip --video test.mp4 --fps 10 --use-classifier -o output/clips/highlight.mp4

# 启用巡航导弹检测
.venv\Scripts\python.exe main.py clip --video test.mp4 --fps 10 --missile -o output/clips/highlight.mp4
```

规则：每段击杀取 `开始-pad_before ~ 结束+pad_after`；相邻击杀间隔 < `--merge-gap` 则合并；集锦末尾追加原视频最后 20 秒。

#### 批量剪辑

```powershell
.venv\Scripts\python.exe main.py clip-batch --input-dir input_videos --output-dir output/clips --fps 10
```

输出文件名与输入视频同名。 默认完成后会对输出目录中的同名主成片扫描 JEV 候选封面，保存到各目录的 `cover_candidates/`。JEV 未启动时提示并跳过；可用 `--no-cover-candidates` 关闭。标题生成、网页 ChatGPT 封面美化和视频上传仍由人工单独触发；候选封面选定后命名为同级目录的 `封面.jpg`，发布工具会优先读取它。

### 6. 其他工具

| 脚本 | 用途 |
|---|---|
| `tools/classify_dir.py` | 对目录下所有图片执行 cls-only 分类，可保存 ROI / 删除负样本 |
| `tools/profile_infer.py` | infer 流水线分段计时 |
| `tools/visualize_thresholds.py` | 多阈值二值化横向拼接对比 |

```powershell
.venv\Scripts\python.exe tools/classify_dir.py data/test --thresh 0.9 -o output/roi_results --save-roi
.venv\Scripts\python.exe tools/profile_infer.py 75 80 10
```

## ROI 参数

在 `src/icon_detect/constants.py` 顶部 `DETECT_ROI_*` 按分辨率比例定义；换布局后重调并重新 `template`。

## 测试

```powershell
uv pip install pytest
.venv\Scripts\python.exe -m pytest tests/ -v
```

## 目录

- `template/` — 模板与分类器权重 (`.pt` 文件 gitignored)
- `output/` — 检测结果、集锦、视频索引缓存 (gitignored)
- `tools/` — 独立工具脚本
- `tests/` — pytest 测试
