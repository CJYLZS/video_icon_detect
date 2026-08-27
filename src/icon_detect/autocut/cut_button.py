"""尾部"返回大厅(N秒)"按钮检测：OCR 粗扫 + 帧级细化定位目标倒数出现帧。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..utils.progress import create_pbar
from ..video.source import FrameSource

# 按钮 ROI（占整帧比例）。2360x1640 下约等于 (1380,1500)-(2280,1630)，
# 覆盖 "返回大厅(N秒)" 按钮，避免右侧 "分享战绩" 干扰。
BTN_ROI_X1 = 0.585
BTN_ROI_Y1 = 0.9146
BTN_ROI_X2 = 0.966
BTN_ROI_Y2 = 0.994

COUNTDOWN_RE = re.compile(r"返回大厅\s*[（(]\s*(\d{1,3})\s*秒?")

COARSE_STEP = 6  # 粗扫步长（60fps 下 = 10Hz）


@dataclass(frozen=True)
class CutResult:
    """检测到的截断点（该帧及其后全部剪掉）。"""

    frame_idx: int
    t_sec: float
    value: int  # 该帧显示的倒数秒数


def parse_countdown(texts) -> int | None:
    """从 OCR 文本列表解析倒数秒数；未读到返回 None。"""
    for t in texts or []:
        m = COUNTDOWN_RE.search(t)
        if m:
            return int(m.group(1))
    return None


def _ocr_roi(engine, frame: np.ndarray) -> int | None:
    h, w = frame.shape[:2]
    roi = frame[int(h * BTN_ROI_Y1): int(h * BTN_ROI_Y2), int(w * BTN_ROI_X1): int(w * BTN_ROI_X2)]
    out = engine(roi)
    texts = list(out.txts) if out and out.txts else []
    return parse_countdown(texts)


def detect_countdown_cut(
    video_path: Path,
    *,
    total_frames: int,
    fps: float,
    start_sec: float,
    target_value: int = 113,
    show_progress: bool = True,
) -> CutResult | None:
    """在 [start_sec, 视频结束] 范围扫描倒数按钮。

    返回第一个显示 value <= target_value 的帧；未检出返回 None。
    粗扫 10Hz 定位候选区间后，按帧细化到精确帧。
    """
    from ..detection.missile import _get_ocr

    engine = _get_ocr()
    start_frame = max(0, int(round(start_sec * fps)))
    if start_frame >= total_frames:
        return None

    samples: list[tuple[int, int | None]] = []  # (frame_idx, value)
    iter_range = range(start_frame, total_frames, COARSE_STEP)
    pbar = (
        create_pbar(len(iter_range), desc="倒数按钮扫描")
        if show_progress
        else None
    )
    with FrameSource(video_path, None) as src:
        for idx in iter_range:
            frame = src.load(idx)
            if frame is None:
                break
            samples.append((idx, _ocr_roi(engine, frame)))
            if pbar:
                pbar.update(1)
            first = samples[-1][1]
            if first is not None and first <= target_value:
                break
    if pbar:
        pbar.close()

    # 情况 1：粗扫直接读到 value <= target —— 在上一个有效样本与本样本之间逐帧细化
    hit_i = next(
        (i for i, (_, v) in enumerate(samples) if v is not None and v <= target_value), None
    )
    if hit_i is not None:
        lo = samples[hit_i - 1][0] + 1 if hit_i > 0 else samples[0][0]
        hi = min(samples[hit_i][0] + 2, total_frames - 1)  # 扩 2 帧给稳定性确认留空间
        return _refine_frame(engine, video_path, lo, hi, target_value, fps)

    # 情况 2：视频结束仍未到 target —— 无截断
    return None


def _refine_frame(
    engine,
    video_path: Path,
    lo: int,
    hi: int,
    target_value: int,
    fps: float,
) -> CutResult | None:
    """在 (lo, hi] 内逐帧 OCR，返回第一个"连续两帧 value <= target"的帧。

    倒数数字切换存在 1 帧左右的过渡/交叉淡化帧，单帧 OCR 可能抖动；
    要求下一帧也读到 <= target 才确认，保证截断点落在稳定显示目标值的帧上。
    """
    with FrameSource(video_path, None) as src:
        prev_hit: int | None = None
        for idx in range(lo, hi + 1):
            frame = src.load(idx)
            if frame is None:
                break
            value = _ocr_roi(engine, frame)
            if value is not None and value <= target_value:
                if prev_hit is not None and idx == prev_hit + 1:
                    return CutResult(frame_idx=prev_hit, t_sec=prev_hit / fps, value=value)
                prev_hit = idx
            else:
                prev_hit = None
        if prev_hit is not None:
            # 窗口末尾命中但无下一帧可确认（hi 为粗扫确认帧），直接采用
            return CutResult(frame_idx=prev_hit, t_sec=prev_hit / fps, value=target_value)
    # 细化窗口内 OCR 全部失败时，退回窗口末尾（保守：多留一帧）
    return CutResult(frame_idx=hi, t_sec=hi / fps, value=target_value)
