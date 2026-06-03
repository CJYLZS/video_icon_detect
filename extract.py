"""将视频帧导出为图片文件。"""

from __future__ import annotations

from pathlib import Path

import cv2

from frame_source import FrameSource, open_video


def extract_frames(
    video_path: Path,
    output_dir: Path,
    indices: list[int],
    *,
    prefix: str = "frame",
    ext: str = "jpg",
) -> int:
    _, total, _ = open_video(video_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    saved = 0

    with FrameSource(video_path, None) as src:
        for idx in indices:
            if idx < 0 or idx >= total:
                print(f"跳过帧 {idx}（有效范围 0..{total - 1}）")
                continue
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
