"""将视频帧导出为图片文件。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import cv2

from frame_source import FrameSource, parse_frame_index_from_path
from paths import FFMPEG
from video_index import load_or_build_index


def extract_frames(
    video_path: Path,
    output_dir: Path,
    indices: list[int],
    *,
    prefix: str = "frame",
    ext: str = "jpg",
) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    saved = 0

    with FrameSource(video_path, None) as src:
        for idx in indices:
            frame = src.load(idx)
            if frame is None:
                print(f"读取帧 {idx} 失败")
                continue
            t_sec = src.position_msec() / 1000.0
            t_ms = int(round(t_sec * 1000.0))
            out_path = output_dir / f"{prefix}_{idx:05d}_t{t_ms:08d}ms.{ext}"
            cv2.imwrite(str(out_path), frame)
            print(f"帧 {idx} ({t_sec:.3f}s) -> {out_path.name}")
            saved += 1

    print(f"\n完成: 导出 {saved}/{len(indices)} 帧 -> {output_dir}")
    return saved


def extract_frame_at_sec(video_path: Path, t_sec: float, out_path: Path) -> None:
    """按呈现时间（秒）用 ffmpeg 导出单帧，与 icon 检测时 PTS 对齐。"""
    if not FFMPEG.is_file():
        raise FileNotFoundError(f"ffmpeg 不存在: {FFMPEG}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(FFMPEG),
        "-hide_banner",
        "-y",
        "-ss",
        f"{t_sec:.6f}",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-q:v",
        "2",
        str(out_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"ffmpeg 导出失败: {err}")
    if not out_path.is_file():
        raise RuntimeError(f"ffmpeg 未生成文件: {out_path}")


def export_frames_from_icons(
    video_path: Path,
    icon_dir: Path,
    output_dir: Path | None = None,
    *,
    prefix: str = "frame",
    ext: str = "jpg",
    remove_stale: bool = True,
) -> int:
    """
    以 *_icon.jpg 为清单，按各帧 PTS 从视频导出与推理一致的原始帧。

    文件名保留 icon 中的帧号，时间戳用 ffprobe PTS（非 OpenCV 帧号 seek）。
    """
    if not icon_dir.is_dir():
        raise FileNotFoundError(f"目录不存在: {icon_dir}")
    if not video_path.is_file():
        raise FileNotFoundError(f"视频不存在: {video_path}")

    out_root = output_dir or icon_dir
    out_root.mkdir(parents=True, exist_ok=True)
    index = load_or_build_index(video_path)

    icons = sorted(icon_dir.glob(f"{prefix}_*_icon.{ext}"))
    if not icons:
        raise FileNotFoundError(f"未找到 {prefix}_*_icon.{ext}: {icon_dir}")

    saved = 0
    for icon_path in icons:
        idx = parse_frame_index_from_path(icon_path)
        if idx is None:
            print(f"跳过（无法解析帧号）: {icon_path.name}")
            continue
        t_sec = index.pts_for_frame(idx)
        t_ms = int(round(t_sec * 1000.0))
        out_path = out_root / f"{prefix}_{idx:05d}_t{t_ms:08d}ms.{ext}"

        if remove_stale:
            for old in out_root.glob(f"{prefix}_{idx:05d}_t*.{ext}"):
                if old != out_path and old.is_file():
                    old.unlink()

        extract_frame_at_sec(video_path, t_sec, out_path)
        print(f"帧 {idx} (PTS {t_sec:.3f}s) <- {icon_path.name} -> {out_path.name}")
        saved += 1

    print(f"\n完成: 按 icon 导出 {saved}/{len(icons)} 帧 -> {out_root}")
    return saved
