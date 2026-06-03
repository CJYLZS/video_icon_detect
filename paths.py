"""项目路径常量。"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_VIDEO = ROOT / "test.mp4"
OUTPUT_DIR = ROOT / "output"
ROI_CHECK_DIR = OUTPUT_DIR / "roi_check"
