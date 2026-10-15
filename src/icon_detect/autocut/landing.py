"""导弹落地切镜检测：暗角（瞄准镜遥测视图）进出分析 + 帧差精化。

扫描窗口仅限导弹片段末尾的保留后缀（missile_pad_after，OCR 倒计时消失后保留的秒数）
加一小段预滚：落地切镜必然发生在倒计时文本消失之后（命中/引爆即刻切镜），
即 [末次 OCR 命中, 片段结尾] 内；全程扫描反而会被飞行视角的瞬时暗角下探
（如 0914 在 2.1s 处 <0.70 持续 0.4s）触发的假"进入->退出"周期带偏。
窗口内未检出切镜时回退到导弹片段结尾（BGM 于片段边界进入）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from ..constants import MISSILE_PAD_AFTER
from ..utils.progress import create_pbar
from ..video.source import FrameSource

SMALL_W = 240          # 帧差/暗角分析用的下采样宽度
CORNER_FRAC = 0.12     # 四角取样比例
COARSE_STEP = 6        # 粗扫步长（60fps 下 = 10Hz）
PERSIST = 3            # 连续 N 个样本满足才算进入/退出
ENTER_THRESH = 0.70    # 四角/中心亮度比低于此 -> 进入暗角视图
EXIT_THRESH = 0.80     # 高于此 -> 离开暗角视图
PREROLL_SEC = 1.0      # 后缀窗口前预滚：保证窗口起点处于暗角低区、状态机能完成"进入"确认


@dataclass(frozen=True)
class AlignResult:
    """BGM 对齐点（输入视频时间轴）。"""

    frame_idx: int
    t_sec: float
    method: str  # "vignette" | "suffix-end"


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

    def feed(self, frame: np.ndarray, idx: int) -> float:
        """推进一帧，返回暗角比。"""
        scale = SMALL_W / frame.shape[1]
        small = cv2.resize(frame, (SMALL_W, max(1, int(frame.shape[0] * scale))))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        self.prev_small = gray
        return _vignette_ratio(gray)


def detect_missile_landing(
    video_path: Path,
    *,
    start_frame: int,
    end_frame: int,
    fps: float,
    suffix_sec: float = MISSILE_PAD_AFTER,
    show_progress: bool = True,
) -> AlignResult:
    """在导弹片段 [start_frame, end_frame] 的末尾后缀内寻找落地切镜帧。

    扫描窗口 = [end - (suffix_sec + PREROLL_SEC), end]：
    主信号：暗角比自暗角低区（瞄准镜视图）恢复到常态的瞬间（退出点），
    再以窗口内逐帧帧差峰值定位硬切帧；
    窗口内未出现完整暗角周期（含极少数落地不切镜的情况）时，
    回退到导弹片段结尾帧。
    """
    lo_frame = max(start_frame, end_frame - int(round((suffix_sec + PREROLL_SEC) * fps)))
    scanner = _LandingScanner()
    n_samples = max(0, (end_frame - lo_frame) // COARSE_STEP + 1)
    pbar = create_pbar(n_samples, desc="导弹切镜扫描") if show_progress else None

    def _fallback() -> AlignResult:
        return AlignResult(frame_idx=end_frame, t_sec=end_frame / fps, method="suffix-end")

    enter_idx: int | None = None
    candidate: int | None = None
    state_hi: list[bool] = []  # 暗角比连续高于 EXIT 的样本滑窗

    with FrameSource(video_path, None) as src:
        for idx in range(lo_frame, end_frame + 1, COARSE_STEP):
            frame = src.load(idx)
            if frame is None:
                break
            ratio = scanner.feed(frame, idx)
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

    # 进入暗角后到窗口结束都未恢复，或全程未进入暗角：回退导弹片段结尾
    return _fallback()


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
