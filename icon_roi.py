"""击倒图标：固定 ROI、二值 template、ZNCC/二值相似度。"""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

from paths import TEMPLATE_DIR

DEFAULT_TEMPLATE_BIN = TEMPLATE_DIR / "knockdown_bin.png"
DEFAULT_TEMPLATE_GRAY = TEMPLATE_DIR / "knockdown_gray.png"
DEFAULT_TEMPLATE_COLOR = TEMPLATE_DIR / "knockdown_color.jpg"
DEFAULT_TEMPLATE_META = TEMPLATE_DIR / "knockdown_meta.json"

DETECT_ROI_CX = 0.50
DETECT_ROI_CY = 0.555
DETECT_ROI_W = 0.04
DETECT_ROI_H = 0.03
BIN_THRESH = 205
DEFAULT_MATCH_THRESH = 0.75
DEFAULT_CLS_THRESH = 0.7


def detect_roi_rect(frame_shape: tuple[int, ...]) -> tuple[int, int, int, int]:
    h, w = frame_shape[:2]
    roi_w = max(8, int(w * DETECT_ROI_W))
    roi_h = max(8, int(h * DETECT_ROI_H))
    cx = int(w * DETECT_ROI_CX)
    cy = int(h * DETECT_ROI_CY)
    x0 = max(0, cx - roi_w // 2)
    y0 = max(0, cy - roi_h // 2)
    return x0, y0, min(w, x0 + roi_w), min(h, y0 + roi_h)


def crop_detect_roi(frame: np.ndarray) -> tuple[np.ndarray, tuple[int, int]]:
    x0, y0, x1, y1 = detect_roi_rect(frame.shape)
    return frame[y0:y1, x0:x1].copy(), (x0, y0)


def binarize_roi(roi_bgr: np.ndarray, *, thresh: int = BIN_THRESH) -> np.ndarray:
    gray = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    _, bw = cv2.threshold(gray, thresh, 255, cv2.THRESH_BINARY)
    return bw


def mask_to_box(mask: np.ndarray, min_pixels: int = 8) -> dict | None:
    ys, xs = np.where(mask > 128)
    if len(xs) < min_pixels:
        return None
    return {
        "x1": float(xs.min()),
        "y1": float(ys.min()),
        "x2": float(xs.max() + 1),
        "y2": float(ys.max() + 1),
    }


def ref_box_to_full(meta: dict, offset: tuple[int, int]) -> dict:
    ox, oy = offset
    b = meta["ref_box_roi"]
    return {
        "x1": b["x1"] + ox,
        "y1": b["y1"] + oy,
        "x2": b["x2"] + ox,
        "y2": b["y2"] + oy,
    }


def build_roi_template(
    frame: np.ndarray,
    out_dir: Path = TEMPLATE_DIR,
    *,
    frame_index: int | None = None,
    source: str | None = None,
) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    roi, offset = crop_detect_roi(frame)
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    bw = binarize_roi(roi)
    ref_box = mask_to_box(bw)
    if ref_box is None:
        raise RuntimeError("二值化后未找到前景，请调整 ROI 或换参考帧")

    cv2.imwrite(str(out_dir / "knockdown_bin.png"), bw)
    cv2.imwrite(str(out_dir / "knockdown_gray.png"), gray)
    cv2.imwrite(str(out_dir / "knockdown_color.jpg"), roi)
    meta = {
        "frame_index": frame_index,
        "source": source,
        "roi_offset": list(offset),
        "roi_size": [roi.shape[1], roi.shape[0]],
        "ref_box_roi": ref_box,
        "detect_roi": {
            "cx": DETECT_ROI_CX,
            "cy": DETECT_ROI_CY,
            "w": DETECT_ROI_W,
            "h": DETECT_ROI_H,
        },
    }
    DEFAULT_TEMPLATE_META.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta


def load_template_meta(meta_path: Path = DEFAULT_TEMPLATE_META) -> dict:
    if not meta_path.is_file():
        raise FileNotFoundError(f"模板元数据不存在: {meta_path}，请先运行: python main.py template --image <参考图>")
    return json.loads(meta_path.read_text(encoding="utf-8"))


def load_template() -> tuple[np.ndarray, np.ndarray, np.ndarray, dict]:
    tpl_bin = cv2.imread(str(DEFAULT_TEMPLATE_BIN), cv2.IMREAD_GRAYSCALE)
    tpl_gray = cv2.imread(str(DEFAULT_TEMPLATE_GRAY), cv2.IMREAD_GRAYSCALE)
    if tpl_bin is None or tpl_gray is None:
        raise FileNotFoundError("模板不存在，请先: python main.py template --image <参考图>")
    _, mask = cv2.threshold(tpl_bin, 128, 255, cv2.THRESH_BINARY)
    meta = load_template_meta()
    return tpl_bin, tpl_gray, mask, meta


def binary_similarity(roi_bgr: np.ndarray, tpl_bin: np.ndarray, tpl_mask: np.ndarray) -> float:
    roi_bin = binarize_roi(roi_bgr)
    if roi_bin.shape != tpl_bin.shape:
        roi_bin = cv2.resize(roi_bin, (tpl_bin.shape[1], tpl_bin.shape[0]), interpolation=cv2.INTER_AREA)
    fg = tpl_mask > 128
    if fg.sum() < 8:
        return 0.0
    return float((roi_bin[fg] == tpl_bin[fg]).mean())


def zncc_similarity(roi_bgr: np.ndarray, tpl_gray: np.ndarray) -> float:
    roi_gray = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2GRAY)
    if roi_gray.shape != tpl_gray.shape:
        roi_gray = cv2.resize(roi_gray, (tpl_gray.shape[1], tpl_gray.shape[0]), interpolation=cv2.INTER_AREA)
    a = roi_gray.astype(np.float32)
    b = tpl_gray.astype(np.float32)
    a = (a - a.mean()) / (a.std() + 1e-6)
    b = (b - b.mean()) / (b.std() + 1e-6)
    return float(np.clip((a * b).mean(), -1.0, 1.0))


def template_match_score(roi_bgr: np.ndarray, tpl_bin: np.ndarray, tpl_gray: np.ndarray, tpl_mask: np.ndarray) -> tuple[float, float, float]:
    bin_s = binary_similarity(roi_bgr, tpl_bin, tpl_mask)
    zncc_s = zncc_similarity(roi_bgr, tpl_gray)
    zncc_n = (zncc_s + 1.0) / 2.0
    combined = 0.5 * bin_s + 0.5 * zncc_n
    return combined, bin_s, zncc_s


def score_roi(
    roi_bgr: np.ndarray,
    tpl_bin: np.ndarray,
    tpl_gray: np.ndarray,
    tpl_mask: np.ndarray,
    *,
    match_thresh: float = DEFAULT_MATCH_THRESH,
) -> dict:
    combined, bin_s, zncc_s = template_match_score(roi_bgr, tpl_bin, tpl_gray, tpl_mask)
    return {
        "present": combined >= match_thresh,
        "score": combined,
        "bin_score": bin_s,
        "zncc_score": zncc_s,
    }


def detect_frame(
    frame: np.ndarray,
    tpl_bin: np.ndarray,
    tpl_gray: np.ndarray,
    tpl_mask: np.ndarray,
    meta: dict,
    *,
    match_thresh: float = DEFAULT_MATCH_THRESH,
    cls_prob: float | None = None,
    cls_thresh: float = DEFAULT_CLS_THRESH,
    require_classifier: bool = False,
    cls_only: bool = False,
) -> dict:
    roi, offset = crop_detect_roi(frame)
    if roi.size == 0:
        return {"present": False, "score": 0.0, "method": "none", "box": None}

    if cls_only:
        score = float(cls_prob) if cls_prob is not None else 0.0
        present = cls_prob is not None and cls_prob >= cls_thresh
        box = ref_box_to_full(meta, offset) if present else None
        if box is not None:
            box["score"] = score
        return {
            "present": present,
            "score": score,
            "bin_score": None,
            "zncc_score": None,
            "cls_prob": cls_prob,
            "method": "cls" if present else "none",
            "box": box,
        }

    s = score_roi(roi, tpl_bin, tpl_gray, tpl_mask, match_thresh=0.0)
    final = s["score"]
    method = "template"

    template_ok = final >= match_thresh
    if require_classifier and cls_prob is not None:
        present = template_ok and cls_prob >= cls_thresh
        method = "template+cls"
    else:
        present = template_ok
    box = ref_box_to_full(meta, offset) if present else None
    if box is not None:
        box["score"] = final
        box["bin_score"] = s["bin_score"]
        box["zncc_score"] = s["zncc_score"]

    return {
        "present": present,
        "score": final,
        "bin_score": s["bin_score"],
        "zncc_score": s["zncc_score"],
        "cls_prob": cls_prob,
        "method": method if present else "none",
        "box": box,
    }


def draw_detection(frame: np.ndarray, det: dict) -> np.ndarray:
    out = frame.copy()
    x0, y0, x1, y1 = detect_roi_rect(frame.shape)
    cv2.rectangle(out, (x0, y0), (x1, y1), (255, 200, 0), 1)
    if det.get("box"):
        b = det["box"]
        cv2.rectangle(out, (int(b["x1"]), int(b["y1"])), (int(b["x2"]), int(b["y2"])), (0, 0, 255), 2)
        label = f"{det['score']:.2f}"
        cv2.putText(out, label, (int(b["x1"]), max(int(b["y1"]) - 6, 0)), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 255), 2)
    return out
