"""视频/图片帧读写与抽帧。"""

from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent
DEFAULT_VIDEO = ROOT / "test.mp4"
OUTPUT_DIR = ROOT / "output"
ROI_CHECK_DIR = OUTPUT_DIR / "roi_check"


def _normalize_orientation_deg(value: float) -> int:
    """将 OpenCV 返回的方向角规整到 0/90/180/270。"""
    deg = int(round(value)) % 360
    # 某些容器会写入接近整角的浮点值，统一吸附到最近整角。
    candidates = (0, 90, 180, 270)
    nearest = min(candidates, key=lambda x: abs(x - deg))
    if abs(nearest - deg) <= 2:
        return nearest
    return 0


def _apply_orientation(frame: np.ndarray, orientation_deg: float) -> np.ndarray:
    """按视频旋转元数据纠正画面方向。"""
    deg = _normalize_orientation_deg(orientation_deg)
    if deg == 90:
        return cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
    if deg == 180:
        return cv2.rotate(frame, cv2.ROTATE_180)
    if deg == 270:
        return cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return frame


def _get_capture_orientation(cap: cv2.VideoCapture) -> float:
    prop = getattr(cv2, "CAP_PROP_ORIENTATION_META", None)
    if prop is None:
        return 0.0
    return float(cap.get(prop))


def _resolve_orientation_for_manual_apply(cap: cv2.VideoCapture) -> float:
    """决定是否需要手动旋转，避免与 OpenCV 自动旋转重复。"""
    orientation_deg = _get_capture_orientation(cap)
    auto_prop = getattr(cv2, "CAP_PROP_ORIENTATION_AUTO", None)
    if auto_prop is None:
        return orientation_deg

    # 优先关闭 OpenCV 自动旋转，统一走手动旋转。
    cap.set(auto_prop, 0)
    auto_now = cap.get(auto_prop)
    if auto_now >= 0.5:
        # 后端仍强制自动旋转，避免二次旋转。
        return 0.0
    return orientation_deg


def load_bgr(path: Path) -> np.ndarray:
    frame = cv2.imread(str(path))
    if frame is None:
        raise FileNotFoundError(f"无法读取图片: {path}")
    return frame


def sec_to_frame(sec: float, fps: float, total: int) -> int:
    return min(max(int(round(sec * fps)), 0), total - 1)


def open_video(video_path: Path) -> tuple[cv2.VideoCapture, int, float]:
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"无法打开视频: {video_path}")
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    return cap, total, fps


def sample_step(video_fps: float, extract_fps: float) -> int:
    """按目标抽帧频率（张/秒）换算为每隔多少帧取一帧。"""
    if extract_fps <= 0:
        raise ValueError("--fps 必须为正数")
    return max(1, int(round(video_fps / extract_fps)))


def _frame_time_at(
    cap: cv2.VideoCapture,
    idx: int,
    total: int,
    cache: dict[int, float],
) -> float:
    if idx in cache:
        return cache[idx]
    if idx < 0 or idx >= total:
        return float("inf")
    cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
    ok, _frame = cap.read()
    if not ok:
        return float("inf")
    t_sec = (cap.get(cv2.CAP_PROP_POS_MSEC) or 0.0) / 1000.0
    cache[idx] = t_sec
    return t_sec


def _locate_first_frame_at_or_after(
    cap: cv2.VideoCapture,
    target_sec: float,
    total: int,
    cache: dict[int, float],
) -> int:
    lo, hi = 0, total - 1
    ans = total
    while lo <= hi:
        mid = (lo + hi) // 2
        t_mid = _frame_time_at(cap, mid, total, cache)
        if t_mid >= target_sec:
            ans = mid
            hi = mid - 1
        else:
            lo = mid + 1
    return ans


def _locate_last_frame_at_or_before(
    cap: cv2.VideoCapture,
    target_sec: float,
    total: int,
    cache: dict[int, float],
) -> int:
    lo, hi = 0, total - 1
    ans = -1
    while lo <= hi:
        mid = (lo + hi) // 2
        t_mid = _frame_time_at(cap, mid, total, cache)
        if t_mid <= target_sec:
            ans = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return ans


def _collect_indices_for_seconds(
    cap: cv2.VideoCapture,
    points: list[float],
    total: int,
    cache: dict[int, float],
) -> list[int]:
    out: set[int] = set()
    for sec in points:
        idx = _locate_first_frame_at_or_after(cap, max(0.0, sec), total, cache)
        if 0 <= idx < total:
            out.add(idx)
    return sorted(out)


def _collect_indices_for_range(
    cap: cv2.VideoCapture,
    *,
    start_sec: float,
    end_sec: float,
    total: int,
    step: int,
    extract_fps: float | None,
    cache: dict[int, float],
) -> list[int]:
    lo = _locate_first_frame_at_or_after(cap, start_sec, total, cache)
    hi = _locate_last_frame_at_or_before(cap, end_sec, total, cache)
    if lo > hi or lo >= total or hi < 0:
        return []
    if extract_fps is None:
        return list(range(lo, hi + 1, max(1, step)))

    # 按真实时间等间隔采样，避免 VFR 下 frame/fps 误差。
    interval = 1.0 / extract_fps
    t = start_sec
    out: set[int] = set()
    while t <= end_sec + 1e-9:
        idx = _locate_first_frame_at_or_after(cap, t, total, cache)
        if idx > hi:
            break
        out.add(idx)
        t += interval
    return sorted(out)


def _last_valid_timestamp(cap: cv2.VideoCapture, total: int, cache: dict[int, float]) -> float:
    # 某些容器最后几帧可能 seek/read 失败，向前回退查找可读时间戳。
    for idx in range(total - 1, max(-1, total - 12), -1):
        t = _frame_time_at(cap, idx, total, cache)
        if t != float("inf"):
            return t
    return float("inf")


def collect_extract_indices(
    args: argparse.Namespace,
    video_path: Path,
    total: int,
    video_fps: float,
) -> list[int]:
    if args.frame:
        return sorted({i for i in args.frame if 0 <= i < total})

    extract_fps = getattr(args, "fps", None)
    has_time_args = (
        bool(args.seconds)
        or args.start_sec is not None
        or args.end_sec is not None
        or extract_fps is not None
    )
    if not has_time_args:
        raise SystemExit("请指定 --frame、--seconds、--fps，或时间范围参数（--start-sec/--end-sec）")

    cap, actual_total, _ = open_video(video_path)
    total = min(total, actual_total)
    cache: dict[int, float] = {}
    try:
        if args.seconds:
            return _collect_indices_for_seconds(cap, args.seconds, total, cache)

        max_sec = _last_valid_timestamp(cap, total, cache)
        if max_sec == float("inf"):
            return []
        start_sec = max(0.0, args.start_sec or 0.0)
        end_sec = args.end_sec if args.end_sec is not None else max_sec
        if start_sec > end_sec:
            start_sec, end_sec = end_sec, start_sec
        end_sec = min(end_sec, max_sec)

        step = max(1, args.step)
        return _collect_indices_for_range(
            cap,
            start_sec=start_sec,
            end_sec=end_sec,
            total=total,
            step=step,
            extract_fps=extract_fps,
            cache=cache,
        )
    finally:
        cap.release()


def extract_frames(
    video_path: Path,
    output_dir: Path,
    indices: list[int],
    *,
    prefix: str = "frame",
    ext: str = "jpg",
) -> int:
    cap, total, fps = open_video(video_path)
    orientation_deg = _resolve_orientation_for_manual_apply(cap)
    output_dir.mkdir(parents=True, exist_ok=True)

    saved = 0
    for idx in indices:
        if idx < 0 or idx >= total:
            print(f"跳过帧 {idx}（有效范围 0..{total - 1}）")
            continue
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame = cap.read()
        if not ok:
            print(f"读取帧 {idx} 失败")
            continue
        frame = _apply_orientation(frame, orientation_deg)
        t_sec = (cap.get(cv2.CAP_PROP_POS_MSEC) or 0.0) / 1000.0
        t_ms = int(round(t_sec * 1000.0))
        out_path = output_dir / f"{prefix}_{idx:05d}_t{t_ms:08d}ms.{ext}"
        cv2.imwrite(str(out_path), frame)
        print(f"帧 {idx} ({t_sec:.3f}s) -> {out_path.name}")
        saved += 1

    cap.release()
    print(f"\n完成: 导出 {saved}/{len(indices)} 帧 -> {output_dir}")
    return saved


def find_frame_image_path(image_dir: Path, idx: int) -> Path | None:
    """按帧号查找图片，兼容 frame_00123.jpg 与 frame_00123_t00012345ms.jpg。"""
    for ext in (".jpg", ".png"):
        plain = image_dir / f"frame_{idx:05d}{ext}"
        if plain.is_file():
            return plain
    for ext in (".jpg", ".png"):
        tagged = sorted(image_dir.glob(f"frame_{idx:05d}_*{ext}"))
        if tagged:
            return tagged[0]
    return None


def parse_frame_index_from_path(path: Path) -> int | None:
    """从 frame_00123.jpg / frame_00123_t00012345ms.jpg 解析帧号。"""
    stem = path.stem
    if not stem.startswith("frame_"):
        return None
    frame_str = stem[6:].split("_", 1)[0]
    if frame_str.isdigit():
        return int(frame_str)
    return None


def list_image_dir_frames(image_dir: Path) -> list[int]:
    indices: list[int] = []
    for p in sorted(image_dir.glob("frame_*.jpg")):
        idx = parse_frame_index_from_path(p)
        if idx is not None:
            indices.append(idx)
    return sorted(set(indices))


def resolve_infer_indices(
    total: int,
    fps: float,
    *,
    frames: list[int] | None,
    seconds: list[float] | None,
    image_dir: Path | None,
    start_frame: int | None = None,
    end_frame: int | None = None,
    step: int = 1,
) -> list[int]:
    step = max(1, step)
    if image_dir and image_dir.is_dir():
        indices = list_image_dir_frames(image_dir)
    elif frames:
        indices = sorted({i for i in frames if 0 <= i < total})
    elif seconds:
        indices = sorted({sec_to_frame(s, fps, total) for s in seconds})
    elif start_frame is not None or end_frame is not None:
        start = 0 if start_frame is None else max(0, start_frame)
        end = (total - 1) if end_frame is None else min(total - 1, end_frame)
        if start > end:
            start, end = end, start
        indices = list(range(start, end + 1, step))
    else:
        return []

    if (start_frame is not None or end_frame is not None) and image_dir:
        lo = start_frame if start_frame is not None else 0
        hi = end_frame if end_frame is not None else total - 1
        indices = [i for i in indices if lo <= i <= hi and (i - lo) % step == 0]
    return indices


def resolve_media_source(video: Path, image_dir: Path | None) -> tuple[Path | None, Path | None]:
    if image_dir and image_dir.is_dir():
        return None, image_dir
    if video.is_file():
        return video, None
    return None, image_dir


def load_frame(video_path: Path | None, image_dir: Path | None, idx: int) -> np.ndarray | None:
    if image_dir:
        path = find_frame_image_path(image_dir, idx)
        if path is None:
            return None
        return cv2.imread(str(path))
    if video_path is None:
        return None
    cap = cv2.VideoCapture(str(video_path))
    orientation_deg = _resolve_orientation_for_manual_apply(cap)
    cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
    ok, frame = cap.read()
    cap.release()
    if not ok:
        return None
    return _apply_orientation(frame, orientation_deg)
