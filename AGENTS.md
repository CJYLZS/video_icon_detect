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

- **Tests**: `pytest tests/ -v` (16 tests covering CLI mode flag resolution).
- **Dependencies**: managed via `uv pip install`. `requirements.txt` lists 6 packages (opencv-python, numpy, torch, rapidocr, onnxruntime, pytest).
- GPU (CUDA) is optional; PyTorch falls back to CPU.
- Template files live in `template/` and are required for detection. `template/icon_classifier.pt` is optional but required for the default classifier-only mode.
