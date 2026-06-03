"""项目路径常量。"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent
_FFMPEG_BIN = ROOT / "tools" / "ffmpeg-8.1.1-essentials_build" / "bin"
FFMPEG = _FFMPEG_BIN / "ffmpeg.exe"
FFPROBE = _FFMPEG_BIN / "ffprobe.exe"

TEMPLATE_DIR = ROOT / "template"
DEFAULT_VIDEO = ROOT / "test.mp4"
OUTPUT_DIR = ROOT / "output"
ROI_CHECK_DIR = OUTPUT_DIR / "roi_check"
CLIPS_DIR = OUTPUT_DIR / "clips"
VIDEO_INDEX_DIR = OUTPUT_DIR / "video_index"
