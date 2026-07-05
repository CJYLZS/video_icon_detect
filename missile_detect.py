"""巡航导弹文字 OCR 检测（浅绿色"后自动引爆"计时器）。"""

from __future__ import annotations

import logging

import cv2
import numpy as np

MISSILE_ROI_X1 = 0.40
MISSILE_ROI_X2 = 0.60
MISSILE_ROI_Y1 = 0.13
MISSILE_ROI_Y2 = 0.20
MISSILE_KEYWORD = "后自动引爆"
MISSILE_PAD_BEFORE = 7.0
MISSILE_PAD_AFTER = 4.0
MIN_WHITE_PIXELS = 500

_ocr_engine = None


def _get_ocr():
    global _ocr_engine
    if _ocr_engine is None:
        from rapidocr import RapidOCR
        _ocr_engine = RapidOCR(
            params={
                "Global.use_det": False,
                "Global.use_cls": False,
            }
        )
        _logger = logging.getLogger("RapidOCR")
        _logger.setLevel(logging.ERROR)
        for _h in _logger.handlers:
            _h.setLevel(logging.ERROR)
    return _ocr_engine


def _crop_roi(frame: np.ndarray):
    h, w = frame.shape[:2]
    x1 = int(w * MISSILE_ROI_X1)
    x2 = int(w * MISSILE_ROI_X2)
    y1 = int(h * MISSILE_ROI_Y1)
    y2 = int(h * MISSILE_ROI_Y2)
    return frame[y1:y2, x1:x2]


def _binarize_green(roi: np.ndarray):
    g = roi[:, :, 1].astype(float)
    r = roi[:, :, 2].astype(float)
    b = roi[:, :, 0].astype(float)
    mask = ((g - r) > 20) & ((g - b) > 20) & (g > 120)
    binary = np.zeros((roi.shape[0], roi.shape[1], 3), dtype=np.uint8)
    binary[mask] = 255
    return binary, mask.sum()


def detect_missile_text(frame: np.ndarray) -> bool:
    roi = _crop_roi(frame)
    binary, white_count = _binarize_green(roi)
    if white_count < MIN_WHITE_PIXELS:
        return False
    engine = _get_ocr()
    output = engine(binary)
    if not output or not output.txts:
        return False
    for text in output.txts:
        if MISSILE_KEYWORD in text:
            return True
    return False
