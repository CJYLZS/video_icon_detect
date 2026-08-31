# AGENTS.md — 和平精英击倒图标检测

## Environment

- **Python 3.11** (`.python-version`). Use `uv` to manage the virtual environment and dependencies:
  ```
  uv venv .venv --python 3.11
  uv pip install -r requirements.txt
  ```
- **Windows-only**. Paths use `\` separators. Shell is PowerShell.
- **Bundled FFmpeg** at `tools/ffmpeg-8.1.1-essentials_build/bin/` (`src/icon_detect/constants.py`). Do not rely on system ffmpeg/ffprobe.

## Entrypoint

Single CLI: `python main.py <subcommand>`

| Command | Purpose |
|---|---|
| `extract` | Export frames from video |
| `export-icons` | Re-export frames listed in `*_icon.jpg` outputs |
| `template` | Build binary template from a full-frame screenshot |
| `train` | Train optional CNN classifier |
| `infer` | Run detection on video or frame directory |
| `clip` | Detect kills → montage → **auto-cut + BGM synth（默认开启）**，每视频一个输出目录 |
| `clip-batch` | Batch the full pipeline across all videos in a dir |
| `auto-cut` | Auto speed-ramp edit + BGM synth from a clip manifest JSON (`src/icon_detect/autocut/`) |
| `classify-dir` | CLS-only classification on a directory of images (`tools/classify_dir.py`) |

## Project structure

```
video_icon_detect/
├── main.py                          # Thin CLI entrypoint (adds src/ to sys.path)
├── src/icon_detect/                 # Main package
│   ├── constants.py                 # All path/algorithm constants (merged from paths.py + scatter)
│   ├── detection/                   # Detection core
│   │   ├── roi.py                   # ROI crop, binarize, template match (ZNCC + binary similarity)
│   │   ├── classifier.py            # CNN binary classifier (ConvNet on binarized ROIs)
│   │   ├── infer.py                 # Detection pipeline loop + InferConfig/FrameDetection dataclasses
│   │   └── missile.py               # Cruise missile OCR (RapidOCR on green text region)
│   ├── video/                       # Video I/O
│   │   ├── source.py                # FFmpegSource (pipe decode) + FrameSource (OpenCV) + load_bgr
│   │   ├── index.py                 # ffprobe PTS index builder/cache (VideoPtsIndex)
│   │   ├── sampling.py              # Frame-sampling plan from time ranges and FPS targets
│   │   └── extract.py               # Frame export to disk (ffmpeg or FFmpegSource)
│   ├── montage/                     # Highlight montage
│   │   ├── planner.py               # Hit grouping, interval merging, ClipPlan, save_ranges_json/save_clip_manifest
│   │   ├── exporter.py              # ffmpeg segment extraction, concat export, run_clip orchestration
│   │   └── layout.py                # 主流程输出布局（每视频一个目录三件套）+ 缓存判断纯函数
│   ├── autocut/                     # auto-cut：变速剪辑 + BGM 合成
│   │   ├── manifest.py              # 同名 JSON 清单解析 + ffprobe 媒体信息（fps/采样率）
│   │   ├── cut_button.py            # 尾部"返回大厅(N秒)"倒数 OCR 截断点检测
│   │   ├── landing.py               # 导弹落地切镜检测（暗角周期 + 帧差精化）
│   │   ├── summary.py               # 结算界面战局统计提取（积分/淘汰 + 不定字段 -> 战局统计.txt）
│   │   ├── render.py                # filter_complex 构造（帧号/样本坐标）+ ffmpeg 执行
│   │   └── pipeline.py              # AutoCutPlan 编排（检测 -> 计划 -> 渲染）
│   ├── cli/                         # CLI layer
│   │   ├── parser.py                # argparse builder (build_parser + _add_* helpers)
│   │   └── handlers.py              # All cmd_* command handlers + main() entrypoint
│   └── utils/                       # Utilities
│       ├── progress.py              # Terminal progress bar
│       └── preview.py               # ROI preview export (cropped + binarized debug images)
├── tools/                           # Standalone utility scripts (add src/ to sys.path)
│   ├── classify_dir.py
│   ├── profile_infer.py
│   └── visualize_thresholds.py
├── tests/                           # pytest test suite
│   ├── conftest.py                  # sys.path setup
│   └── test_cli_modes.py            # CLI mode flag tests (16 tests)
└── template/                        # Template & classifier resources
    ├── knockdown_bin.png / knockdown_gray.png / knockdown_color.jpg
    ├── knockdown_meta.json
    └── icon_classifier.pt           # Trained CNN weights (gitignored)
```

## Architecture notes

- **Package-based project** — all core logic lives in `src/icon_detect/`. The root `main.py` is a thin entrypoint that adds `src/` to `sys.path` and calls `icon_detect.cli.handlers.main()`.
- **Detection pipeline**: `video/sampling.py` (frame plan) → `detection/infer.py` (loop) → `detection/roi.py` (template match) + `detection/classifier.py` (CNN). Three modes:
  1. **Classifier-only** (default, `cls_only=True`)
  2. **Template + Classifier** (`--use-classifier`, both must pass)
  3. **Template-only** (`--template-only`)
- **ROI constants** in `constants.py`: the icon is expected at 50% X, 55.5% Y of frame, ~4%×3% of frame size. Binary threshold is **205**.
- Default match threshold: **0.75**, default classifier threshold: **0.8**.
- **Video seeking** relies on `video/index.py` — builds an ffprobe PTS index cached to `output/video_index/` keyed by file path + mtime + size. Stale cache = wrong results if video file changes but index isn't rebuilt.
- **Frame source** (`video/source.py`) handles both video (`FFmpegSource` pipe decode + `FrameSource` OpenCV) and image directories (`frame_XXXXX.jpg`). Supports `CAP_PROP_ORIENTATION_META` for smartphone videos.
- **Missile text OCR** (`detection/missile.py`) runs RapidOCR on a green-tinted ROI looking for "后自动引爆".

## auto-cut

`python main.py auto-cut --video <clip.mp4>` — needs the clip manifest JSON (same stem, `.json`) written by `clip`; test assets: `data/output_with_missile_start.{mp4,json}`, `data/output_without_missile_start.{mp4,json}`, `data/0911.{mp4,json}`.

Behavior (spec: `auto-cut.md`):
1. Splits video at the JSON `tail` segment start (fallback: last 20s). Front part plays at `--speed` (1.45x), tail at original speed.
2. OCR-scans the tail for the "返回大厅(N秒)" countdown button; the first frame showing `--cut-value` (113) and everything after it are cut. Countdown start value varies per video (~111–120), so the target is matched by value, not by position. Confirmation = **3 consecutive frames** reading `<= target` (`STABLE_RUN`), searched forward up to `REFINE_HORIZON_SEC` (15s) from the coarse hit — coarse hits can be fade-in misreads (real case: "120秒" fade-in read as "1"/"20"), and the true 113 may appear seconds later. **Never fall back to the coarse-hit frame when no stable run is found** (the old 2-frame+fallback logic cut six seconds early at 120 on six real videos: 0901/0911/0916/1002/1013/1025). Audit note: trim end is exclusive, so a correct cut's LAST visible frame shows the previous second's value (usually 114, sometimes tail of 115 in a 1.5s window) — values 115–120 or fragments like "20" at the end mean an early cut.
3. BGM (`data/bgm.wav`, replace with `--bgm none`) aligns to frame 0, or to the missile-landing camera cut when segments[0] is a missile segment (vignette-ratio cycle + max-frame-diff refinement inside the missile span). `has_audio` (source has an audio track) and `has_bgm` (a second `-i` input exists) are **independent** flags in `build_filter_graph`: `--bgm none` on a video with audio keeps the original audio unmixed (`[fa]anull[aout]`) and must never emit `[1:a]`, or ffmpeg aborts with "Invalid file index 1 in filtergraph description".
4. Original audio is kept at its loudness (`amix normalize=0`, `alimiter` only for peak safety); BGM fades out over the last `--fade-sec` (15s).

Rendering performance & fade (learned from real renders — do not regress):
- Encoding defaults to `h264_nvenc -preset p7 -cq 18` + `-hwaccel cuda` when nvenc is available (RTX cards: ~55s wall for a 110s@60fps 2360x1640 clip vs several minutes of pegged CPU with libx264). `--cpu` forces libx264, `--gpu` errors if nvenc is missing.
- Fade-out is NOT `afade` alone: built-in curves fail perceptually (`tri` linear drops only -5dB at midpoint — inaudible for the first half; `exp` hits silence at ~2/3 of the window). Actual shape = dB-linear ramp via `volume=volume='if(lt(t\,ST)\,1\,pow(10\,-RATE*(t-ST)/20))':eval=frame` (60dB over the fade window, constant -4dB/s at 15s) + `afade=t=out:st=end-1:d=1` for the final digital-zero ramp. Fade window always ends at output end and clamps to the full clip when the clip is shorter than `--fade-sec`.

Coordinate-system rules (learned from real renders — do not regress):
- Source mp4s are multi-segment AAC stream-copy concatenations. Video is trimmed by **frame index** (`trim=start_frame/end_frame`); audio is **resynced to container pts first** (`aresample=SR:first_pts=0`) and only then trimmed by **sample count** (`atrim=start_sample/end_sample`).
  - **Never use `asetpts=N/SR/TB` on the source audio.** Each concat boundary carries a truncated packet duration marking that segment's AAC priming/padding samples as discardable. Container pts is the authority and matches video (0831.mp4: container audio 215.723s vs video 215.700s, 23ms apart), but the decoder emits all 1024 samples/frame (217.223s, 1.4995s more). `asetpts=N/SR/TB` rebuilds the timeline from decoded frame ordinals, keeping those discardable samples, so audio falls progressively behind as boundaries accumulate — 0831.mp4 had 91 truncated packets (66126 samples) and measured **1.36s of A/V drift at the end**, in sync at the start. `aresample`'s resync drops/pads to container pts; measured drift is zero across 20 probe points. The old comment here claimed the opposite (that container pts drifts ~0.7s) — that inverted rationale was the bug.
- `afade` st and BGM `atrim` end must use the **output** timeline (front segment shrinks by 1/speed); BGM delay = landing_sec / speed.


## 主流程（clip / clip-batch）

`clip` / `clip-batch` 已集成 auto-cut，默认开启，全流程一条命令：

```
python main.py clip --video <raw.mp4> --fps 10            # 默认输出 output/clips/<stem>/
python main.py clip-batch --input-dir data/test/input --output-dir data/test/output --fps 10
```

输出布局（`montage/layout.py`）—— 每个输入视频一个独立目录，内含四件套：
- `<stem>.mp4` 第一阶段成片（击杀集锦，自动剪辑前）
- `<stem>_bgm.mp4` 第二阶段成片（1.45x 变速 + 尾部倒数截断 + BGM 合成）
- `<stem>.json` 片段清单（auto-cut 的同名 JSON）
- `战局统计.txt` 结算界面提取的模式/结果/积分/淘汰/不定字段

关键规则（不要回退）：
- **缓存默认开启**：第一阶段成片+清单同时存在才跳过检测导出（只 mp4 无 json 视为不完整，会重跑）。`--force` 忽略全部缓存强制重做。
- **第二阶段缓存看渲染配方标记，不只看文件存在**（`montage/layout.py`）：成片渲染时把 `autocut-v<N>;speed=..;cut=..;fade=..` 写进 mp4 的 `comment` metadata（`-metadata comment=`），`autocut_cached` 用 ffprobe 读回比对，不一致就自动重做。
  - `AUTOCUT_RECIPE_VERSION` 是手工维护的常量：**`render.py` 滤镜语义每次实质变更都要 +1**，否则旧成片会被误判为可复用。当前 v1 = aresample 重同步版（修音画漂移）。
  - 参数部分覆盖 `--speed`/`--cut-value`/`--fade-sec` 变更；主流程侧一律走 `handlers._stage_recipe()`，它用 `inspect` 取 `plan_autocut` 的签名默认值，**不要在别处硬编码这三个值**（改了默认值而比对仍用旧值 = 静默失配）。
  - 无标记的历史成片 `read_recipe_tag` 返回 `None`，一律视为过期。
- **容错**：批量模式下单视频第一阶段失败不中断后续视频；第二阶段失败只告警，保留已生成的集锦。
- 编码加速对两阶段统一生效：`--gpu`/`--cpu` 三态覆盖（互斥），默认自动检测 nvenc → `h264_nvenc -preset p7 -cq 18` + `-hwaccel cuda`。
- `-o/--output` 与 batch 的 `--output-dir` 现在是**根目录**语义（其下建 `<stem>/` 子目录）；`--meta-json`/`--meta-dir`、`--segments-dir` 仍可选叠加。
- `--no-autocut` 只出集锦 + 清单；独立 `auto-cut` 子命令保留不变。
- **战局统计提取**（`autocut/summary.py`，独立于 auto-cut 阶段、有自己的缓存）：在尾段 0.5s 粗扫结算界面。识别特征 = 左上角含"模式"（如"突破模式—荣都战场"）+ 左下角 logo 模糊命中（美术字 OCR 常读作"平精美"，按 和平/平精/精英 双字匹配，举报/GAME 兜底）。统计块 ROI 约x[0.20,0.52] y[0.24,0.66]，按 OCR 几何行分组 + x 邻近配对解析；固定字段 积分/淘汰 的值在标签下一行，不定字段标签值同行。**必须解析出 积分/淘汰 之一才接受该帧**——结算流程有统计页→战绩表格页多页，表格页同样带模式/logo 特征，会误触发。已知限制：rec 偶发丢数字（"0点"读作"点"），放大也救不回，如实输出。
- **地图白名单纠错**：`summary.KNOWN_MAPS`（当前：荣都战场/沙漠绿洲/海湾基地，新图追加即可）。模式串按连字符（`-—－一`，OCR 常把 — 读作一）切出地图名，difflib 相似度对齐到白名单最近项（实测"茶都战场"→"荣都战场"）；相似度 <0.4 视为未知地图保留原样。
- **OCR 引擎陷阱（不要回退）**：`detection.missile._get_ocr()` 是 `Global.use_det=False` 的**纯识别**引擎，只适合单行文本（倒数按钮/模式名/logo 单行 ROI）。多行文本块必须用 `autocut/summary._get_stats_engine()`（det+rec 完整管线），否则整图返回空。
- 集锦导出的分段临时目录固定在 `output/tmp/kill_clips_*`（同盘 + 受限环境通常禁写平台 temp）。**不要用 `tempfile.mkdtemp`/`TemporaryDirectory`**：其 0o700 权限会让子进程 ffmpeg 打开目录内文件被拒（Permission denied）；用普通 `mkdir` + uuid 目录名，用完 `rmtree`。


## classify-dir

Standalone script: `python tools/classify_dir.py <image_dir>`

Runs cls-only classifier on all images in a directory. Prints per-file probability and summary.

```
python tools/classify_dir.py data/test
python tools/classify_dir.py data/test --thresh 0.9
python tools/classify_dir.py data/test -o output/roi_results --save-roi
```

| Flag | Default | Description |
|---|---|---|
| `image_dir` | (required) | Directory of images to classify |
| `--thresh` | 0.7 | Classification threshold |
| `-o` / `--output-dir` | None | Save ROI crops to this directory |
| `--save-roi` | False | Also save binarized ROI images |
| `--delete-misses` / `--no-delete-misses` | True | Delete all non-knockdown images (default on) |

## Development

- **Tests**: `pytest tests/ -q -p no:cacheprovider` (70 tests: 16 CLI mode + 27 auto-cut + 27 main-pipeline layout/recipe-cache/stats-parser/map-correction tests; the cache plugin is blocked by the file sandbox).
- **Dependencies**: managed via `uv pip install`. `requirements.txt` lists 6 packages (opencv-python, numpy, torch, rapidocr, onnxruntime, pytest).
- GPU (CUDA) is optional; PyTorch falls back to CPU.
- Template files live in `template/` and are required for detection. `template/icon_classifier.pt` is optional but required for the default classifier-only mode.
