# AGENTS.md — 和平精英击倒图标检测

## Environment

- **Python 3.11** (`.python-version`). Use `.venv` for dependencies:
  ```
  python -m venv .venv
  .venv\Scripts\pip install -r requirements.txt
  ```
- **Windows-only**. Paths use `\` separators. Shell is PowerShell.
- **Bundled FFmpeg** at `tools/ffmpeg-8.1.1-essentials_build/bin/` (`paths.py:6`). Do not rely on system ffmpeg/ffprobe.

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

## Architecture notes

- **Flat script project** — no package, no `setup.py`. Every `.py` is a direct module. Legacy entrypoints (`detect_icon.py`, `extract_frames.py`, `media_io.py`) are thin re-exports; prefer `main.py` for new work.
- **Detection pipeline**: `sampling.py` (frame plan) → `infer.py` (loop) → `icon_roi.py` (template match) + `classifier.py` (CNN). Three modes controlled by `--cls-only` flag and presence of `template/icon_classifier.pt`:
  1. Template-only (default)
  2. Template + Classifier (both must pass)
  3. Classifier-only (`--cls-only`)
- **ROI constants** in `icon_roi.py:20-25`: the icon is expected at 50% X, 55.5% Y of frame, ~4%×3% of frame size. Binary threshold is 185 (`icon_roi.py:24`).
- **Video seeking** relies on `video_index.py` — builds an ffprobe PTS index cached to `output/video_index/` keyed by file path + mtime + size. Stale cache = wrong results if video file changes but index isn't rebuilt.
- **Frame source** (`frame_source.py`) handles both video (`VideoCapture` with sequential decode optimization) and image directories (`frame_XXXXX.jpg`). Supports `CAP_PROP_ORIENTATION_META` for smartphone videos.
- **Missile text OCR** (`missile_detect.py`) runs RapidOCR on a green-tinted ROI looking for "后自动引爆".

## Development

- **No tests, no CI, no linting/typecheck config**. Do not try to run pytest, ruff, mypy, or GitHub Actions — they don't exist.
- `requirements.txt` is the sole dependency specification (5 packages: opencv, numpy, torch, rapidocr, onnxruntime).
- GPU (CUDA) is optional; PyTorch falls back to CPU.
- Template files live in `template/` and are required for detection. `template/icon_classifier.pt` is optional (needed only for classifier mode).
