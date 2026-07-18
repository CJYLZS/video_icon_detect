"""击倒图标检测流水线（读帧 + 模板/分类器）。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import cv2
import numpy as np

from classifier import classifier_prob, load_classifier
from frame_source import FFmpegSource, FrameSource, resolve_media_source
from missile_detect import detect_missile_text
from progress import print_progress
from icon_roi import (
    DEFAULT_CLS_THRESH,
    DEFAULT_MATCH_THRESH,
    crop_detect_roi,
    detect_frame,
    draw_detection,
    load_template,
    load_template_meta,
)


@dataclass
class InferConfig:
    video: Path
    indices: list[int]
    fps: float
    image_dir: Path | None = None
    match_thresh: float = DEFAULT_MATCH_THRESH
    use_classifier: bool = False
    cls_only: bool = False
    cls_thresh: float = DEFAULT_CLS_THRESH


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


def _load_classifier_bundle(use_classifier: bool, *, cls_only: bool = False):
    if not use_classifier and not cls_only:
        return None, None
    bundle = load_classifier()
    if not bundle:
        raise FileNotFoundError("分类器不存在，请先 python main.py train ...")
    return bundle


def _iter_with_source(
    config: InferConfig,
    src: FrameSource | FFmpegSource,
) -> Iterator[FrameDetection | None]:
    if config.cls_only:
        tpl_bin = tpl_gray = tpl_mask = None
        meta = load_template_meta()
    else:
        tpl_bin, tpl_gray, tpl_mask, meta = load_template()
    cls_model, cls_device = _load_classifier_bundle(
        config.use_classifier, cls_only=config.cls_only
    )

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
            require_classifier=config.use_classifier and not config.cls_only,
            cls_only=config.cls_only,
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
    frame_src: FrameSource | FFmpegSource | None = None,
) -> Iterator[FrameDetection | None]:
    """逐帧检测；无法读取时 yield None。可传入已打开的 FrameSource/FFmpegSource。"""
    if frame_src is not None:
        yield from _iter_with_source(config, frame_src)
        return
    det_video, det_image_dir = resolve_media_source(config.video, config.image_dir)
    if det_video is not None:
        from video_index import load_or_build_index as _vi_load
        pts_index = _vi_load(det_video)
        with FFmpegSource(det_video, pts_index) as src:
            yield from _iter_with_source(config, src)
    else:
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


def collect_hit_frame_indices(
    config: InferConfig,
    *,
    show_progress: bool = False,
) -> list[int]:
    """检测命中帧号（升序），不保留图像。"""
    total = len(config.indices)
    hits: list[int] = []
    for n, item in enumerate(iter_detections(config), start=1):
        if item is not None and item.detection["present"]:
            hits.append(item.frame_index)
        if show_progress:
            print_progress(n, total, label="检测", detail=f"命中 {len(hits)}")
    return hits


def collect_combined(
    config: InferConfig,
    *,
    enable_missile: bool = False,
    show_progress: bool = False,
) -> tuple[list[int], list[int]]:
    """单次遍历：击倒优先，未命中时尝试导弹 OCR。
    返回 (hit_frames, missile_frames)，均为帧号列表。"""
    total = len(config.indices)
    hits: list[int] = []
    missiles: list[int] = []
    for n, item in enumerate(iter_detections(config), start=1):
        if item is None:
            continue
        if item.detection["present"]:
            hits.append(item.frame_index)
        elif enable_missile and detect_missile_text(item.frame):
            missiles.append(item.frame_index)
        if show_progress:
            detail = f"击倒 {len(hits)}"
            if enable_missile:
                detail += f" 导弹 {len(missiles)}"
            print_progress(n, total, label="检测", detail=detail)
    return hits, missiles


def save_detection_image(output_dir: Path, item: FrameDetection) -> None:
    out = output_dir / f"frame_{item.frame_index:05d}_icon.jpg"
    cv2.imwrite(str(out), draw_detection(item.frame, item.detection))
