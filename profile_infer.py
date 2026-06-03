"""对 infer 流水线做分段计时。"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from frame_source import FrameSource, resolve_media_source
from icon_roi import DEFAULT_TEMPLATE_BIN, crop_detect_roi, detect_frame, load_template
from sampling import resolve_frame_indices


def profile_infer(
    video: Path,
    *,
    start_sec: float,
    end_sec: float,
    extract_fps: float,
    image_dir: Path | None = None,
    match_thresh: float = 0.75,
) -> dict[str, float]:
    tpl_bin, tpl_gray, tpl_mask, meta = load_template()

    t0 = time.perf_counter()
    args = argparse.Namespace(
        start_sec=start_sec,
        end_sec=end_sec,
        fps=extract_fps,
        step=1,
        frame=None,
        seconds=None,
        rebuild_index=False,
    )
    indices, _, _fps = resolve_frame_indices(args, video)
    t_index = time.perf_counter() - t0

    det_video, det_image_dir = resolve_media_source(video, image_dir)
    t_load = 0.0
    t_detect = 0.0
    hits = 0

    with FrameSource(det_video, det_image_dir) as frame_src:
        for idx in indices:
            t1 = time.perf_counter()
            frame = frame_src.load(idx)
            t2 = time.perf_counter()
            if frame is None:
                t_load += t2 - t1
                continue
            crop_detect_roi(frame)
            det = detect_frame(
                frame,
                tpl_bin,
                tpl_gray,
                tpl_mask,
                meta,
                match_thresh=match_thresh,
                cls_prob=None,
                require_classifier=False,
            )
            t3 = time.perf_counter()
            t_load += t2 - t1
            t_detect += t3 - t2
            if det["present"]:
                hits += 1

    t_total = time.perf_counter() - t0
    t_loop = t_load + t_detect
    t_other = max(0.0, t_total - t_index - t_loop)

    return {
        "frames": len(indices),
        "hits": hits,
        "total_s": t_total,
        "index_s": t_index,
        "load_s": t_load,
        "detect_s": t_detect,
        "other_s": t_other,
    }


def _print_stats(stats: dict[str, float]) -> None:
    n = stats["frames"]
    total = stats["total_s"]
    print(f"帧数: {n}, 命中: {stats['hits']}")
    print(f"总耗时: {total:.3f}s")
    loop = stats["load_s"] + stats["detect_s"]
    for key, label in (
        ("index_s", "索引/选帧"),
        ("load_s", "读帧(FrameSource)"),
        ("detect_s", "detect_frame(模板)"),
        ("other_s", "其余"),
    ):
        sec = stats[key]
        pct = 100.0 * sec / total if total else 0
        per = sec / n if n else 0
        print(f"  {label}: {sec:.3f}s ({pct:.1f}%), 均 {per*1000:.2f}ms/帧")
    if loop > 0:
        print(f"  读帧占循环: {100.0 * stats['load_s'] / loop:.1f}%")


def main() -> None:
    if not DEFAULT_TEMPLATE_BIN.is_file():
        raise SystemExit("缺少模板，请先运行 template")

    video = Path("test.mp4")
    start_sec = float(sys.argv[1]) if len(sys.argv) > 1 else 75.0
    end_sec = float(sys.argv[2]) if len(sys.argv) > 2 else 80.0
    extract_fps = float(sys.argv[3]) if len(sys.argv) > 3 else 10.0

    print(f"=== infer {start_sec}-{end_sec}s fps={extract_fps}（仅分段计时）===")
    stats = profile_infer(
        video,
        start_sec=start_sec,
        end_sec=end_sec,
        extract_fps=extract_fps,
    )
    _print_stats(stats)


if __name__ == "__main__":
    main()
