"""击倒图标检测流水线（读帧 + 模板/分类器）。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import cv2
import numpy as np

from classifier import classifier_prob, load_classifier
from frame_source import FrameSource, resolve_media_source
from icon_roi import DEFAULT_MATCH_THRESH, crop_detect_roi, detect_frame, draw_detection, load_template


@dataclass
class InferConfig:
    video: Path
    indices: list[int]
    fps: float
    image_dir: Path | None = None
    match_thresh: float = DEFAULT_MATCH_THRESH
    use_classifier: bool = False
    cls_thresh: float = 0.5


@dataclass
class FrameDetection:
    frame_index: int
    time_sec: float
    frame: np.ndarray
    detection: dict
    cls_prob: float | None


@dataclass
class InferResult:
    detections: list[FrameDetection]
    skipped: int


def _load_classifier_bundle(use_classifier: bool):
    if not use_classifier:
        return None, None
    bundle = load_classifier()
    if not bundle:
        raise FileNotFoundError("分类器不存在，请先 python main.py train ...")
    return bundle


def _iter_with_source(
    config: InferConfig,
    src: FrameSource,
) -> Iterator[FrameDetection | None]:
    tpl_bin, tpl_gray, tpl_mask, meta = load_template()
    cls_model, cls_device = _load_classifier_bundle(config.use_classifier)

    for idx in config.indices:
        frame = src.load(idx)
        if frame is None:
            yield None
            continue
        roi, _ = crop_detect_roi(frame)
        cls_p = (
            classifier_prob(roi, cls_model, cls_device)
            if cls_model is not None
            else None
        )
        det = detect_frame(
            frame,
            tpl_bin,
            tpl_gray,
            tpl_mask,
            meta,
            match_thresh=config.match_thresh,
            cls_prob=cls_p,
            cls_thresh=config.cls_thresh,
            require_classifier=config.use_classifier,
        )
        t_sec = idx / config.fps if config.fps else 0.0
        yield FrameDetection(
            frame_index=idx,
            time_sec=t_sec,
            frame=frame,
            detection=det,
            cls_prob=cls_p,
        )


def iter_detections(
    config: InferConfig,
    *,
    frame_src: FrameSource | None = None,
) -> Iterator[FrameDetection | None]:
    """逐帧检测；无法读取时 yield None。可传入已打开的 FrameSource。"""
    if frame_src is not None:
        yield from _iter_with_source(config, frame_src)
        return
    det_video, det_image_dir = resolve_media_source(config.video, config.image_dir)
    with FrameSource(det_video, det_image_dir) as src:
        yield from _iter_with_source(config, src)


def run_infer(config: InferConfig) -> InferResult:
    hits: list[FrameDetection] = []
    skipped = 0
    for item in iter_detections(config):
        if item is None:
            skipped += 1
        elif item.detection["present"]:
            hits.append(item)
    return InferResult(detections=hits, skipped=skipped)


def save_detection_image(output_dir: Path, item: FrameDetection) -> None:
    out = output_dir / f"frame_{item.frame_index:05d}_icon.jpg"
    cv2.imwrite(str(out), draw_detection(item.frame, item.detection))
