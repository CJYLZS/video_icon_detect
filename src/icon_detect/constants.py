"""项目路径和算法常量（合并自 paths.py + 各处散落常量）。"""

from pathlib import Path

# ---- 项目根目录 ----
ROOT = Path(__file__).resolve().parent.parent.parent

# ---- FFmpeg ----
_FFMPEG_BIN = ROOT / "tools" / "ffmpeg-8.1.1-essentials_build" / "bin"
FFMPEG = _FFMPEG_BIN / "ffmpeg.exe"
FFPROBE = _FFMPEG_BIN / "ffprobe.exe"

# ---- 路径 ----
TEMPLATE_DIR = ROOT / "template"
DEFAULT_VIDEO = ROOT / "test.mp4"
OUTPUT_DIR = ROOT / "output"
ROI_CHECK_DIR = OUTPUT_DIR / "roi_check"
CLIPS_DIR = OUTPUT_DIR / "clips"
VIDEO_INDEX_DIR = OUTPUT_DIR / "video_index"

# ---- 模板文件路径 ----
DEFAULT_TEMPLATE_BIN = TEMPLATE_DIR / "knockdown_bin.png"
DEFAULT_TEMPLATE_GRAY = TEMPLATE_DIR / "knockdown_gray.png"
DEFAULT_TEMPLATE_COLOR = TEMPLATE_DIR / "knockdown_color.jpg"
DEFAULT_TEMPLATE_META = TEMPLATE_DIR / "knockdown_meta.json"

# ---- 分类器路径 ----
CLASSIFIER_PATH = TEMPLATE_DIR / "icon_classifier.pt"
CLASSIFIER_INPUT_CHANNELS = 1
CLASSIFIER_INPUT_BASE_W = 96

# ---- 检测 ROI 参数 ----
DETECT_ROI_CX = 0.50
DETECT_ROI_CY = 0.555
DETECT_ROI_W = 0.04
DETECT_ROI_H = 0.03
BIN_THRESH = 205

# ---- 检测阈值 ----
DEFAULT_MATCH_THRESH = 0.75
DEFAULT_CLS_THRESH = 0.8

# ---- 剪辑 ----
TAIL_APPEND_SEC = 20.0
FIXED_DURATION_ACCEPT_MIN = 2.5
FIXED_DURATION_ACCEPT_MAX = 3.0
# 固定时长模式默认开启：168 帧 = 2.8s @ 60fps（配合默认 --output-fps 60）
DEFAULT_FIXED_DURATION_FRAMES = 168

# ---- 导弹检测 ----
MISSILE_ROI_X1 = 0.40
MISSILE_ROI_X2 = 0.60
MISSILE_ROI_Y1 = 0.13
MISSILE_ROI_Y2 = 0.20
MISSILE_KEYWORD = "后自动引爆"
MISSILE_PAD_BEFORE = 7.0
MISSILE_PAD_AFTER = 4.0
MIN_WHITE_PIXELS = 500

# ---- 视频 ----
VIDEO_SUFFIXES = frozenset(
    {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".m4v", ".ts", ".wmv"}
)


def list_videos_in_dir(directory: Path) -> list[Path]:
    if not directory.is_dir():
        raise NotADirectoryError(f"不是目录: {directory}")
    return sorted(
        (p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES),
        key=lambda p: p.name.lower(),
    )
