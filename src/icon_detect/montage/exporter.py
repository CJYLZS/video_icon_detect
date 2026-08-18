"""集锦导出：ffmpeg 裁剪、拼接、run_clip 编排。"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from ..constants import DEFAULT_FIXED_DURATION_FRAMES, FIXED_DURATION_ACCEPT_MAX, FIXED_DURATION_ACCEPT_MIN, FFMPEG
from ..detection.infer import InferConfig, collect_combined, collect_hit_frame_indices
from ..detection.missile import warmup_missile_ocr
from ..utils.progress import print_step
from ..video.index import load_or_build_index
from .planner import (
    ClipPlan,
    LabeledTimeRange,
    TimeRange,
    _fmt_range,
    build_clip_plan,
    save_clip_manifest,
    save_ranges_json,
)


def _extract_segment(
    video: Path, out: Path, seg: TimeRange, *, source_fps: float, use_cuda: bool = False
) -> None:
    if not FFMPEG.is_file():
        raise FileNotFoundError(f"ffmpeg 不存在: {FFMPEG}")
    cmd = [str(FFMPEG), "-y"]
    if use_cuda:
        cmd += ["-hwaccel", "cuda"]
    cmd += ["-ss", f"{seg.start_sec:.3f}", "-i", str(video)]
    cmd += ["-t", f"{seg.duration_sec:.3f}"]
    if use_cuda:
        cmd += ["-c:v", "h264_nvenc", "-preset", "p7", "-cq", "18"]
    else:
        cmd += ["-c:v", "libx264", "-preset", "fast", "-crf", "18"]
    cmd += ["-r", str(round(source_fps)), "-fps_mode", "cfr"]
    cmd += ["-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"ffmpeg 切片失败: {err}")


def export_highlight_video(
    video: Path,
    clips: list[LabeledTimeRange],
    output_path: Path,
    *,
    segments_dir: Path | None = None,
    show_progress: bool = True,
    use_cuda: bool = False,
    source_fps: float = 30.0,
) -> Path:
    import sys
    from tqdm import tqdm

    if not clips:
        raise ValueError("无剪辑区间")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    n = len(clips)

    if n == 1:
        if show_progress:
            tqdm.write(f"  导出: {_fmt_range(clips[0])}")
        _extract_segment(video, output_path, clips[0], source_fps=source_fps, use_cuda=use_cuda)
        return output_path

    with tempfile.TemporaryDirectory(prefix="kill_clips_") as tmp:
        tmp_dir = Path(tmp)
        parts: list[Path] = []
        pbar = tqdm(total=n, desc="导出", unit="段", ncols=100, file=sys.stdout) if show_progress else None
        for i, seg in enumerate(clips):
            part = tmp_dir / f"part_{i:03d}.mp4"
            _extract_segment(video, part, seg, source_fps=source_fps, use_cuda=use_cuda)
            parts.append(part)
            if segments_dir is not None:
                segments_dir.mkdir(parents=True, exist_ok=True)
                seg_out = segments_dir / f"segment_{i:03d}.mp4"
                shutil.copy2(part, seg_out)
            if pbar:
                pbar.set_postfix_str(_fmt_range(seg))
                pbar.update(1)
        if pbar:
            pbar.close()

        if show_progress:
            tqdm.write("  拼接中...")

        list_file = tmp_dir / "concat.txt"
        list_file.write_text(
            "\n".join(f"file '{p.resolve().as_posix()}'" for p in parts),
            encoding="utf-8",
        )
        cmd = [
            str(FFMPEG),
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_file),
            "-c",
            "copy",
            str(output_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(f"ffmpeg 拼接失败: {err}")

    return output_path


def run_clip(
    config: InferConfig,
    *,
    output_path: Path,
    segments_dir: Path | None = None,
    meta_path: Path | None = None,
    pad_before: float = 2,
    pad_after: float = 0.5,
    max_hit_gap: float = 2.0,
    merge_gap: float = 2.0,
    show_progress: bool = True,
    enable_missile: bool = False,
    missile_pad_before: float = 5.0,
    missile_pad_after: float = 5.0,
    missile_prefix: bool = True,
    use_cuda: bool = False,
    output_fps: float = 60,
    fixed_duration_frames: int = DEFAULT_FIXED_DURATION_FRAMES,
    fixed_duration_accept_min: float = FIXED_DURATION_ACCEPT_MIN,
    fixed_duration_accept_max: float = FIXED_DURATION_ACCEPT_MAX,
) -> ClipPlan:
    if show_progress:
        mode_tag = "检测" if not enable_missile else "击倒 + 导弹检测"
        print_step(1, 3, f"建立时间索引并{mode_tag}（{len(config.indices)} 帧）...")

    index = load_or_build_index(config.video)

    if enable_missile:
        warmup_missile_ocr()
        hit_frames, missile_frames = collect_combined(
            config, enable_missile=True, show_progress=show_progress
        )
    else:
        hit_frames = collect_hit_frame_indices(config, show_progress=show_progress)
        missile_frames = []

    if not hit_frames and not missile_frames:
        raise SystemExit("未检测到任何事件，无法生成集锦")

    plan = build_clip_plan(
        hit_frames,
        index,
        pad_before=pad_before,
        pad_after=pad_after,
        max_hit_gap=max_hit_gap,
        merge_gap=merge_gap,
        missile_frames=missile_frames if missile_frames else None,
        missile_pad_before=missile_pad_before,
        missile_pad_after=missile_pad_after,
        missile_prefix=missile_prefix,
        fixed_duration_frames=fixed_duration_frames,
        fixed_duration_fps=output_fps,
        fixed_duration_accept_min=fixed_duration_accept_min,
        fixed_duration_accept_max=fixed_duration_accept_max,
    )

    if show_progress:
        print_step(2, 3, "规划剪辑区间...")
        print(f"      {plan.summary_line()}")
        if plan.kill_merged_count:
            print(f"      （相邻图标时段按 {merge_gap}s 合并了 {plan.kill_merged_count} 段）")
        if fixed_duration_frames:
            print(
                f"      （固定时长模式：目标 {fixed_duration_frames} 帧 "
                f"({fixed_duration_frames / output_fps:.1f}s@{output_fps:.0f}fps)，"
                f"已归一 {plan.fixed_duration_normalized} 段，"
                f"接受范围 [{fixed_duration_accept_min:.1f}s, {fixed_duration_accept_max:.1f}s]）"
            )
        if plan.missile_event_count:
            print(f"      （导弹区间前 {missile_pad_before}s 后 {missile_pad_after}s）")
        if missile_prefix and plan.missile_event_count:
            print(f"      （导弹片段已复制到集锦开头，共 {plan.missile_event_count} 段）")
        if plan.clip_merged_count:
            print(f"      （集锦区间重叠合并了 {plan.clip_merged_count} 段）")
        elif plan.clip_count == (plan.icon_event_count + plan.missile_event_count):
            print("      （每段事件对应一段集锦；未再合并时后两项相同）")
        print(
            "      说明: 图标时段由采样命中合并得到，间隔小于阈值的相邻段会算作同一段，"
            "可能少于或多于真实击倒次数"
        )
    if meta_path is not None:
        save_ranges_json(meta_path, plan, merge_gap=merge_gap)
        if show_progress:
            print(f"      区间 JSON: {meta_path}")

    manifest_path = output_path.with_suffix(".json")
    save_clip_manifest(
        manifest_path,
        plan.clips,
        index,
        source_fps=output_fps,
        source_video=config.video,
    )
    if show_progress:
        print(f"      片段清单: {manifest_path}")

    if show_progress:
        total_dur = sum(c.duration_sec for c in plan.clips)
        fps_note = f"@{output_fps:.0f}fps" if output_fps != config.fps else ""
        print_step(3, 3, f"ffmpeg 导出 {plan.clip_count} 段（总时长约 {total_dur:.1f}s{fps_note}）...")

    export_highlight_video(
        config.video,
        plan.clips,
        output_path,
        segments_dir=segments_dir,
        show_progress=show_progress,
        use_cuda=use_cuda,
        source_fps=output_fps,
    )
    return plan
