"""对 positive 每张图裁 ROI，用多个阈值二值化后横向拼接输出。"""

from pathlib import Path
import cv2
import numpy as np
from icon_roi import crop_detect_roi, binarize_roi

POSITIVE_DIR = Path("data/positive")
OUT_DIR = Path("data/positive_roi")
THRESHOLDS = [185, 190, 195, 200, 205, 210, 215, 220]

OUT_DIR.mkdir(parents=True, exist_ok=True)

paths = sorted(POSITIVE_DIR.glob("*.jpg"))
print(f"找到 {len(paths)} 张 positive 图片")

for p in paths:
    frame = cv2.imread(str(p))
    if frame is None:
        print(f"  跳过（无法读取）: {p.name}")
        continue

    roi, _ = crop_detect_roi(frame)
    cells = [roi.copy()]
    for th in THRESHOLDS:
        bw = binarize_roi(roi, thresh=th)
        cells.append(cv2.cvtColor(bw, cv2.COLOR_GRAY2BGR))

    combined = np.hstack(cells)
    out_path = OUT_DIR / p.name
    cv2.imwrite(str(out_path), combined)

print(f"已输出到 {OUT_DIR}/")
