"""模板 ROI 预览导出。"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from icon_roi import binarize_roi, crop_detect_roi, detect_roi_rect


def export_roi_preview(
    frame: np.ndarray,
    meta: dict,
    tag: str,
    out_dir: Path,
) -> Path:
    roi, (ox, oy) = crop_detect_roi(frame)
    bw = binarize_roi(roi)
    x0, y0, x1, y1 = detect_roi_rect(frame.shape)
    out_dir.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_dir / f"{tag}_roi_crop.jpg"), roi)
    cv2.imwrite(str(out_dir / f"{tag}_roi_bin.jpg"), bw)
    vis = frame.copy()
    cv2.rectangle(vis, (x0, y0), (x1, y1), (255, 200, 0), 1)
    b = meta["ref_box_roi"]
    cv2.rectangle(
        vis,
        (int(b["x1"] + ox), int(b["y1"] + oy)),
        (int(b["x2"] + ox), int(b["y2"] + oy)),
        (0, 255, 0),
        2,
    )
    cv2.imwrite(str(out_dir / f"{tag}_full_with_roi.jpg"), vis)
    return out_dir
