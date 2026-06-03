"""按时间/帧号规划待处理的视频帧索引。"""

from __future__ import annotations

import argparse
from pathlib import Path

from frame_source import open_video
from video_index import VideoPtsIndex, load_or_build_index


def sample_step(video_fps: float, extract_fps: float) -> int:
    if extract_fps <= 0:
        raise ValueError("--fps 必须为正数")
    return max(1, int(round(video_fps / extract_fps)))


def resolve_frame_indices(
    args: argparse.Namespace,
    video_path: Path,
) -> tuple[list[int], int, float]:
    """根据 CLI 参数解析帧号列表，并返回 (indices, total, fps)。"""
    _, total, fps = open_video(video_path)
    indices = collect_extract_indices(args, video_path, total, fps)
    return indices, total, fps


def _collect_indices_for_seconds(index: VideoPtsIndex, points: list[float], total: int) -> list[int]:
    out: set[int] = set()
    for sec in points:
        idx = index.locate_first_at_or_after(max(0.0, sec))
        if 0 <= idx < total:
            out.add(idx)
    return sorted(out)


def _collect_indices_for_range(
    index: VideoPtsIndex,
    *,
    start_sec: float,
    end_sec: float,
    total: int,
    step: int,
    extract_fps: float | None,
) -> list[int]:
    lo = index.locate_first_at_or_after(start_sec)
    hi = index.locate_last_at_or_before(end_sec)
    if lo > hi or lo >= total or hi < 0:
        return []
    if extract_fps is None:
        return list(range(lo, hi + 1, max(1, step)))

    interval = 1.0 / extract_fps
    t = start_sec
    out: set[int] = set()
    while t <= end_sec + 1e-9:
        idx = index.locate_first_at_or_after(t)
        if idx > hi:
            break
        out.add(idx)
        t += interval
    return sorted(out)


def collect_extract_indices(
    args: argparse.Namespace,
    video_path: Path,
    total: int,
    video_fps: float,
) -> list[int]:
    frames = getattr(args, "frame", None)
    seconds = getattr(args, "seconds", None)
    if frames:
        return sorted({i for i in frames if 0 <= i < total})

    extract_fps = getattr(args, "fps", None)
    has_time_args = (
        bool(seconds)
        or args.start_sec is not None
        or args.end_sec is not None
        or extract_fps is not None
    )
    if not has_time_args:
        raise SystemExit("请指定 --frame、--seconds、--fps，或时间范围参数（--start-sec/--end-sec）")

    rebuild = getattr(args, "rebuild_index", False)
    index = load_or_build_index(video_path, rebuild=rebuild)
    total = min(total, len(index.pts_sec))

    if seconds:
        return _collect_indices_for_seconds(index, seconds, total)

    start_sec = max(0.0, args.start_sec or 0.0)
    end_sec = args.end_sec if args.end_sec is not None else index.max_sec
    if start_sec > end_sec:
        start_sec, end_sec = end_sec, start_sec
    end_sec = min(end_sec, index.max_sec)

    step = max(1, args.step)
    return _collect_indices_for_range(
        index,
        start_sec=start_sec,
        end_sec=end_sec,
        total=total,
        step=step,
        extract_fps=extract_fps,
    )
