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

# 细化确认：连续 STABLE_RUN 帧读到 <= target 才认定为截断点。
# 渐变/淡入残影会造成单帧误读（实测"120秒"淡入帧被读成"1"），两帧确认不够。
STABLE_RUN = 3
# 细化搜索相对粗扫命中点向前扩展的秒数：若粗扫命中的是误读帧，
# 真 113 秒可能晚 (起始倒数 - target) 秒才出现。
REFINE_HORIZON_SEC = 15.0


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
        # 粗扫命中可能是淡入/渐变帧的单帧误读（实测"120秒"淡入被读成"1"），
        # 真 113 秒可能晚数秒才出现，因此细化窗口向前扩展 REFINE_HORIZON_SEC 秒。
        hi = min(
            samples[hit_i][0] + int(REFINE_HORIZON_SEC * fps),
            total_frames - 1,
        )
        return _refine_frame(engine, video_path, lo, hi, target_value, fps)

    # 情况 2：视频结束仍未到 target —— 无截断
    return None


def first_stable_run(
    samples: list[tuple[int, int | None]],
    target_value: int,
    need_run: int = STABLE_RUN,
) -> int | None:
    """从 (帧号, 倒数值|None) 序列中找出首个"连续 need_run 帧 <= target"的起始帧号。

    单帧/两帧的偶发误读（渐变残影）不足以确认；找不到稳定段返回 None，
    调用方应视为"未检出截断点"而不是回退到某个误读帧。
    """
    run_start: int | None = None
    run_len = 0
    for idx, v in samples:
        if v is not None and v <= target_value:
            if run_len == 0:
                run_start = idx
            run_len += 1
            if run_len >= need_run:
                return run_start
        else:
            run_len = 0
            run_start = None
    return None


def _refine_frame(
    engine,
    video_path: Path,
    lo: int,
    hi: int,
    target_value: int,
    fps: float,
) -> CutResult | None:
    """在 [lo, hi] 逐帧 OCR，返回首个"连续 STABLE_RUN 帧 <= target"的帧。

    找不到稳定段返回 None（宁可保留到视频结尾，也不在误读帧处错剪）。
    """
    samples: list[tuple[int, int | None]] = []
    with FrameSource(video_path, None) as src:
        for idx in range(lo, hi + 1):
            frame = src.load(idx)
            if frame is None:
                break
            samples.append((idx, _ocr_roi(engine, frame)))
    start = first_stable_run(samples, target_value)
    if start is None:
        return None
    value = next(v for i, v in samples if i >= start and v is not None)
    return CutResult(frame_idx=start, t_sec=start / fps, value=value)
