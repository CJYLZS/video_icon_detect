"""击倒图标检测 — 统一 CLI 入口。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from icon_detect.cli.handlers import main

if __name__ == "__main__":
    main()
