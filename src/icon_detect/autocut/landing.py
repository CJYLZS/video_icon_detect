"""导弹落地切镜检测：暗角（瞄准镜遥测视图）进出分析 + 帧差兜底。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from ..utils.progress import create_pbar
from ..video.source import FrameSource

SMALL_W = 240          # 帧差/暗角分析用的下采样宽度
CORNER_FRAC = 0.12     # 四角取样比例
COARSE_STEP = 6        # 粗扫步长（60fps 下 = 10Hz）
PERSIST = 3            # 连续 N 个样本满足才算进入/退出
ENTER_THRESH = 0.70    # 四角/中心亮度比低于此 -> 进入暗角视图
EXIT_THRESH = 0.80     # 高于此 -> 离开暗角视图


@dataclass(frozen=True)
class AlignResult:
    """BGM 对齐点（输入视频时间轴）。"""

    frame_idx: int
    t_sec: float
    method: str  # "vignette" | "frame-diff"


def _vignette_ratio(gray: np.ndarray) -> float:
    h, w = gray.shape
    ch, cw = max(1, int(h * CORNER_FRAC)), max(1, int(w * CORNER_FRAC))
    corners = np.concatenate(
        [
            gray[:ch, :cw],
            gray[:ch, -cw:],
            gray[-ch:, :cw],
            gray[-ch:, -cw:],
        ]
    ).astype(np.float32)
    center = gray[h // 4: 3 * h // 4, w // 4: 3 * w // 4].astype(np.float32)
    return float(corners.mean()) / (float(center.mean()) + 1e-3)


class _LandingScanner:
    def __init__(self) -> None:
        self.prev_small: np.ndarray | None = None
        self.best_diff = -1.0
        self.best_diff_idx = -1

    def feed(self, frame: np.ndarray, idx: int) -> tuple[float, float]:
        """返回 (帧差, 暗角比)。"""
        scale = SMALL_W / frame.shape[1]
        small = cv2.resize(frame, (SMALL_W, max(1, int(frame.shape[0] * scale))))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        if self.prev_small is None:
            diff = 0.0
        else:
            diff = float(np.abs(gray.astype(np.int16) - self.prev_small.astype(np.int16)).mean())
        self.prev_small = gray
        if diff > self.best_diff:
            self.best_diff = diff
            self.best_diff_idx = idx
        return diff, _vignette_ratio(gray)


def detect_missile_landing(
    video_path: Path,
    *,
    start_frame: int,
    end_frame: int,
    fps: float,
    show_progress: bool = True,
) -> AlignResult:
    """在导弹片段 [start_frame, end_frame] 内寻找落地切镜帧。

    主信号：暗角比自常态进入低区（瞄准镜视图）后再恢复的瞬间；
    若全程未出现暗角周期，退化为片段内帧差峰值。
    """
    scanner = _LandingScanner()
    n_samples = max(0, (end_frame - start_frame) // COARSE_STEP + 1)
    pbar = create_pbar(n_samples, desc="导弹切镜扫描") if show_progress else None

    enter_idx: int | None = None
    candidate: int | None = None
    state_hi: list[bool] = []  # 暗角比连续高于 EXIT 的样本滑窗

    with FrameSource(video_path, None) as src:
        for idx in range(start_frame, end_frame + 1, COARSE_STEP):
            frame = src.load(idx)
            if frame is None:
                break
            _, ratio = scanner.feed(frame, idx)
            if pbar:
                pbar.update(1)

            if enter_idx is None:
                if ratio < ENTER_THRESH:
                    if candidate is None:
                        candidate = idx  # 潜在进入点（待确认）
                else:
                    candidate = None
                if candidate is not None and (idx - candidate) >= (PERSIST - 1) * COARSE_STEP:
                    enter_idx = candidate
                    state_hi = []
            else:
                state_hi.append(ratio > EXIT_THRESH)
                if len(state_hi) > PERSIST:
                    state_hi.pop(0)
                if len(state_hi) == PERSIST and all(state_hi):
                    coarse_exit = idx - (PERSIST - 1) * COARSE_STEP
                    if pbar:
                        pbar.close()
                    return _refine_exit(
                        video_path,
                        enter_idx=enter_idx,
                        coarse_exit=coarse_exit,
                        fps=fps,
                        method="vignette",
                    )
            # 已进入暗角但本样本仍低于 EXIT：保持等待
    if pbar:
        pbar.close()

    if enter_idx is not None:
        # 进入暗角后到片段结束都未恢复（异常情况）：以片段末帧兜底
        return AlignResult(frame_idx=end_frame, t_sec=end_frame / fps, method="vignette-end")
    return AlignResult(
        frame_idx=scanner.best_diff_idx, t_sec=scanner.best_diff_idx / fps, method="frame-diff"
    )


def _refine_exit(
    video_path: Path,
    *,
    enter_idx: int,
    coarse_exit: int,
    fps: float,
    method: str,
) -> AlignResult:
    """在粗扫退出窗口内逐帧计算帧差，取最大帧差帧作为切镜点。

    暗角比在切镜前会因低空明亮地形逐渐变浅，比率阈值定位不到硬切；
    而硬切（遥测视图 -> 地面视野）是窗口内最大的整帧突变。
    """
    lo = max(enter_idx + COARSE_STEP, coarse_exit - 60)
    hi = coarse_exit + 30
    scanner = _LandingScanner()
    best_idx = -1
    best_diff = -1.0
    with FrameSource(video_path, None) as src:
        for idx in range(lo, hi + 1):
            frame = src.load(idx)
            if frame is None:
                break
            scale = SMALL_W / frame.shape[1]
            small = cv2.resize(frame, (SMALL_W, max(1, int(frame.shape[0] * scale))))
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            if scanner.prev_small is not None:
                diff = float(
                    np.abs(gray.astype(np.int16) - scanner.prev_small.astype(np.int16)).mean()
                )
                if diff > best_diff:
                    best_diff = diff
                    best_idx = idx
            scanner.prev_small = gray
    if best_idx < 0:
        best_idx = coarse_exit
    return AlignResult(frame_idx=best_idx, t_sec=best_idx / fps, method=method)


def _frame_ratio(frame: np.ndarray) -> float:
    scale = SMALL_W / frame.shape[1]
    small = cv2.resize(frame, (SMALL_W, max(1, int(frame.shape[0] * scale))))
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    return _vignette_ratio(gray)
