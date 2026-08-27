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
| `clip` | Generate kill highlight montage |
| `clip-batch` | Batch clip across all videos in `input/` |
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
│   │   └── exporter.py              # ffmpeg segment extraction, concat export, run_clip orchestration
│   ├── autocut/                     # auto-cut：变速剪辑 + BGM 合成
│   │   ├── manifest.py              # 同名 JSON 清单解析 + ffprobe 媒体信息（fps/采样率）
│   │   ├── cut_button.py            # 尾部"返回大厅(N秒)"倒数 OCR 截断点检测
│   │   ├── landing.py               # 导弹落地切镜检测（暗角周期 + 帧差精化）
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
2. OCR-scans the tail for the "返回大厅(N秒)" countdown button; the first frame showing `--cut-value` (113) — confirmed on 2 consecutive frames — and everything after it are cut. Countdown start value varies per video (~111–119), so the target is matched by value, not by position.
3. BGM (`data/bgm.wav`, replace with `--bgm none`) aligns to frame 0, or to the missile-landing camera cut when segments[0] is a missile segment (vignette-ratio cycle + max-frame-diff refinement inside the missile span).
4. Original audio is kept at its loudness (`amix normalize=0`, `alimiter` only for peak safety); BGM fades out over the last `--fade-sec` (15s).

Rendering performance & fade (learned from real renders — do not regress):
- Encoding defaults to `h264_nvenc -preset p7 -cq 18` + `-hwaccel cuda` when nvenc is available (RTX cards: ~55s wall for a 110s@60fps 2360x1640 clip vs several minutes of pegged CPU with libx264). `--cpu` forces libx264, `--gpu` errors if nvenc is missing.
- Fade-out is NOT `afade` alone: built-in curves fail perceptually (`tri` linear drops only -5dB at midpoint — inaudible for the first half; `exp` hits silence at ~2/3 of the window). Actual shape = dB-linear ramp via `volume=volume='if(lt(t\,ST)\,1\,pow(10\,-RATE*(t-ST)/20))':eval=frame` (60dB over the fade window, constant -4dB/s at 15s) + `afade=t=out:st=end-1:d=1` for the final digital-zero ramp. Fade window always ends at output end and clamps to the full clip when the clip is shorter than `--fade-sec`.

Coordinate-system rules (learned from real renders — do not regress):
- Source mp4s are multi-segment AAC stream-copy concatenations: container pts drifts from content time (~0.7s by the end), so video is trimmed by **frame index** (`trim=start_frame/end_frame`) and audio by **sample count** (`asetpts=N/SR/TB` first, then `atrim=start_sample/end_sample`).
- `afade` st and BGM `atrim` end must use the **output** timeline (front segment shrinks by 1/speed); BGM delay = landing_sec / speed.


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

- **Tests**: `pytest tests/ -q -p no:cacheprovider` (30 tests: 16 CLI mode + 14 auto-cut unit tests; the cache plugin is blocked by the file sandbox).
- **Dependencies**: managed via `uv pip install`. `requirements.txt` lists 6 packages (opencv-python, numpy, torch, rapidocr, onnxruntime, pytest).
- GPU (CUDA) is optional; PyTorch falls back to CPU.
- Template files live in `template/` and are required for detection. `template/icon_classifier.pt` is optional but required for the default classifier-only mode.
