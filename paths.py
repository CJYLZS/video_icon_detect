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

VIDEO_SUFFIXES = frozenset(
    {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".m4v", ".ts", ".wmv"}
)


def list_videos_in_dir(directory: Path) -> list[Path]:
    """返回目录下视频文件列表（仅顶层，按文件名排序）。"""
    if not directory.is_dir():
        raise NotADirectoryError(f"不是目录: {directory}")
    return sorted(
        (p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES),
        key=lambda p: p.name.lower(),
    )
