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


def collect_extract_indices(args: argparse.Namespace, total: int, video_fps: float) -> list[int]:
    if args.frame:
        return sorted({i for i in args.frame if 0 <= i < total})
    if args.seconds:
        return sorted({sec_to_frame(s, video_fps, total) for s in args.seconds})

    extract_fps = getattr(args, "fps", None)
    if args.start_frame is not None or args.end_frame is not None:
        start = args.start_frame if args.start_frame is not None else 0
        end = args.end_frame if args.end_frame is not None else total - 1
    elif args.start_sec is not None or args.end_sec is not None:
        start = sec_to_frame(args.start_sec or 0.0, video_fps, total)
        end = sec_to_frame(
            args.end_sec if args.end_sec is not None else (total - 1) / video_fps,
            video_fps,
            total,
        )
    elif extract_fps is not None:
        start, end = 0, total - 1
    else:
        raise SystemExit("请指定 --frame、--seconds、--fps，或范围参数（--start-frame 等）")

    start = max(0, min(start, total - 1))
    end = max(0, min(end, total - 1))
    if start > end:
        start, end = end, start

    if extract_fps is not None:
        step = sample_step(video_fps, extract_fps)
    else:
        step = max(1, args.step)
    return list(range(start, end + 1, step))


def extract_frames(
    video_path: Path,
    output_dir: Path,
    indices: list[int],
    *,
    prefix: str = "frame",
    ext: str = "jpg",
) -> int:
    cap, total, fps = open_video(video_path)
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
        out_path = output_dir / f"{prefix}_{idx:05d}.{ext}"
        cv2.imwrite(str(out_path), frame)
        print(f"帧 {idx} ({idx / fps:.2f}s) -> {out_path.name}")
        saved += 1

    cap.release()
    print(f"\n完成: 导出 {saved}/{len(indices)} 帧 -> {output_dir}")
    return saved


def list_image_dir_frames(image_dir: Path) -> list[int]:
    indices: list[int] = []
    for p in sorted(image_dir.glob("frame_*.jpg")):
        stem = p.stem
        if stem.startswith("frame_") and stem[6:].isdigit():
            indices.append(int(stem[6:]))
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
        for ext in (".jpg", ".png"):
            path = image_dir / f"frame_{idx:05d}{ext}"
            if path.is_file():
                return cv2.imread(str(path))
        return None
    if video_path is None:
        return None
    cap = cv2.VideoCapture(str(video_path))
    cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
    ok, frame = cap.read()
    cap.release()
    return frame if ok else None
