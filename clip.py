"""根据击倒检测生成击杀集锦片段并导出视频。"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from infer import InferConfig, collect_hit_frame_indices
from paths import FFMPEG
from progress import print_progress, print_step
from video_index import VideoPtsIndex, load_or_build_index


@dataclass(frozen=True)
class TimeRange:
    start_sec: float
    end_sec: float

    @property
    def duration_sec(self) -> float:
        return max(0.0, self.end_sec - self.start_sec)


@dataclass
class ClipPlan:
    """检测命中 -> 图标连续段 -> 集锦片段 的完整规划。"""

    hit_frames: list[int]
    kills_before_merge: list[TimeRange]
    kills: list[TimeRange]
    clips_before_overlap: list[TimeRange]
    clips: list[TimeRange]

    @property
    def hit_count(self) -> int:
        return len(self.hit_frames)

    @property
    def icon_event_count(self) -> int:
        """击倒图标连续出现的时段数（≠ 真实击倒次数，近距多杀会被合并）。"""
        return len(self.kills)

    @property
    def knockdown_count(self) -> int:
        """兼容旧名，同 icon_event_count。"""
        return self.icon_event_count

    @property
    def clip_count(self) -> int:
        """最终集锦片段数（留白 + 区间合并后）。"""
        return len(self.clips)

    @property
    def kill_merged_count(self) -> int:
        return len(self.kills_before_merge) - len(self.kills)

    @property
    def clip_merged_count(self) -> int:
        return len(self.clips_before_overlap) - len(self.clips)

    def summary_line(self) -> str:
        return (
            f"采样命中 {self.hit_count} 次 -> "
            f"图标时段 {self.icon_event_count} 段 -> "
            f"集锦 {self.clip_count} 段"
        )


def group_hits_to_kill_intervals(
    hit_times: list[float],
    *,
    max_hit_gap: float = 2.0,
) -> list[TimeRange]:
    """将离散命中时刻合并为击杀区间（开始~结束）。"""
    if not hit_times:
        return []
    times = sorted(hit_times)
    start = end = times[0]
    out: list[TimeRange] = []
    for t in times[1:]:
        if t - end <= max_hit_gap:
            end = t
        else:
            out.append(TimeRange(start, end))
            start = end = t
    out.append(TimeRange(start, end))
    return out


def merge_adjacent_kill_intervals(
    kills: list[TimeRange],
    *,
    gap_sec: float = 2.0,
) -> list[TimeRange]:
    """相邻击杀区间若间隔 < gap_sec 则合并。"""
    if not kills:
        return []
    merged = [kills[0]]
    for k in kills[1:]:
        prev = merged[-1]
        if k.start_sec - prev.end_sec < gap_sec:
            merged[-1] = TimeRange(prev.start_sec, max(prev.end_sec, k.end_sec))
        else:
            merged.append(k)
    return merged


def kill_intervals_to_clip_ranges(
    kills: list[TimeRange],
    *,
    window_end: float,
    video_duration: float,
    pad_before: float = 2.0,
    pad_after: float = 0.5,
) -> list[TimeRange]:
    """击杀区间 -> 剪辑区间：前后各留 pad；最后一段延伸到 min(window_end, 视频结束)。"""
    if not kills:
        return []
    clips: list[TimeRange] = []
    tail_end = min(window_end, video_duration)
    for i, k in enumerate(kills):
        start = max(0.0, k.start_sec - pad_before)
        if i == len(kills) - 1:
            end = tail_end
        else:
            end = min(video_duration, k.end_sec + pad_after)
        if end > start:
            clips.append(TimeRange(start, end))
    return clips


def merge_overlapping_clip_ranges(ranges: list[TimeRange]) -> list[TimeRange]:
    """合并重叠或相接的剪辑区间。"""
    if not ranges:
        return []
    ordered = sorted(ranges, key=lambda r: r.start_sec)
    out = [ordered[0]]
    for r in ordered[1:]:
        prev = out[-1]
        if r.start_sec <= prev.end_sec:
            out[-1] = TimeRange(prev.start_sec, max(prev.end_sec, r.end_sec))
        else:
            out.append(r)
    return out


def build_clip_plan(
    hit_frames: list[int],
    index: VideoPtsIndex,
    *,
    window_end: float,
    pad_before: float = 2.0,
    pad_after: float = 0.5,
    max_hit_gap: float = 2.0,
    merge_gap: float = 2.0,
) -> ClipPlan:
    hit_times = [index.pts_for_frame(f) for f in sorted(hit_frames)]
    kills_before = group_hits_to_kill_intervals(hit_times, max_hit_gap=max_hit_gap)
    kills = merge_adjacent_kill_intervals(kills_before, gap_sec=merge_gap)
    clips_before = kill_intervals_to_clip_ranges(
        kills,
        window_end=window_end,
        video_duration=index.duration_sec,
        pad_before=pad_before,
        pad_after=pad_after,
    )
    clips = merge_overlapping_clip_ranges(clips_before)
    return ClipPlan(
        hit_frames=sorted(hit_frames),
        kills_before_merge=kills_before,
        kills=kills,
        clips_before_overlap=clips_before,
        clips=clips,
    )


def _fmt_range(r: TimeRange) -> str:
    return f"{r.start_sec:.2f}s ~ {r.end_sec:.2f}s ({r.duration_sec:.2f}s)"


def save_ranges_json(path: Path, plan: ClipPlan, *, merge_gap: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    kill_intervals = [asdict(k) for k in plan.kills]
    clip_ranges = [asdict(c) for c in plan.clips]
    payload = {
        "summary": {
            "sample_hits": plan.hit_count,
            "icon_events": plan.icon_event_count,
            "icon_events_before_merge_gap": len(plan.kills_before_merge),
            "highlight_clips": plan.clip_count,
            "merged_by_merge_gap": plan.kill_merged_count,
            "merged_by_overlap": plan.clip_merged_count,
            "merge_gap_sec": merge_gap,
            "note": "icon_events 为 UI 图标连续时段，间隔<merge_gap 的相邻段会合并，不等于真实击倒次数",
            "knockdown_events": plan.icon_event_count,
        },
        "hit_frames": plan.hit_frames,
        "icon_event_intervals": kill_intervals,
        "highlight_clip_ranges": clip_ranges,
        "knockdown_intervals": kill_intervals,
        "kill_intervals": kill_intervals,
        "clip_ranges": clip_ranges,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _extract_segment(video: Path, out: Path, seg: TimeRange) -> None:
    if not FFMPEG.is_file():
        raise FileNotFoundError(f"ffmpeg 不存在: {FFMPEG}")
    # -ss 必须在 -i 之前：输入侧 seek，避免每段从文件头解码到起点（长视频会极慢）
    cmd = [
        str(FFMPEG),
        "-y",
        "-ss",
        f"{seg.start_sec:.3f}",
        "-i",
        str(video),
        "-t",
        f"{seg.duration_sec:.3f}",
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "18",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"ffmpeg 切片失败: {err}")


def export_highlight_video(
    video: Path,
    clips: list[TimeRange],
    output_path: Path,
    *,
    segments_dir: Path | None = None,
    show_progress: bool = True,
) -> Path:
    """将多段剪辑区间拼接为单个输出视频。"""
    if not clips:
        raise ValueError("无剪辑区间")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    n = len(clips)

    if n == 1:
        if show_progress:
            print_progress(1, 1, label="导出", detail=_fmt_range(clips[0]))
        _extract_segment(video, output_path, clips[0])
        return output_path

    with tempfile.TemporaryDirectory(prefix="kill_clips_") as tmp:
        tmp_dir = Path(tmp)
        parts: list[Path] = []
        for i, seg in enumerate(clips):
            if show_progress:
                print_progress(
                    i + 1,
                    n + 1,
                    label="导出",
                    detail=f"切片 {i + 1}/{n} {_fmt_range(seg)}",
                )
            part = tmp_dir / f"part_{i:03d}.mp4"
            _extract_segment(video, part, seg)
            parts.append(part)
            if segments_dir is not None:
                segments_dir.mkdir(parents=True, exist_ok=True)
                seg_out = segments_dir / f"segment_{i:03d}.mp4"
                shutil.copy2(part, seg_out)

        if show_progress:
            print_progress(n + 1, n + 1, label="导出", detail="拼接中...")

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
    window_end: float,
    output_path: Path,
    segments_dir: Path | None = None,
    meta_path: Path | None = None,
    pad_before: float = 2,
    pad_after: float = 0.5,
    max_hit_gap: float = 2.0,
    merge_gap: float = 2.0,
    show_progress: bool = True,
) -> ClipPlan:
    if show_progress:
        print_step(1, 3, f"建立时间索引并检测（{len(config.indices)} 帧）...")

    index = load_or_build_index(config.video)
    hit_frames = collect_hit_frame_indices(config, show_progress=show_progress)
    if not hit_frames:
        raise SystemExit("未检测到击倒图标，无法生成集锦")

    plan = build_clip_plan(
        hit_frames,
        index,
        window_end=window_end,
        pad_before=pad_before,
        pad_after=pad_after,
        max_hit_gap=max_hit_gap,
        merge_gap=merge_gap,
    )

    if show_progress:
        print_step(2, 3, "规划剪辑区间...")
        print(f"      {plan.summary_line()}")
        if plan.kill_merged_count:
            print(f"      （相邻图标时段按 {merge_gap}s 合并了 {plan.kill_merged_count} 段）")
        if plan.clip_merged_count:
            print(f"      （集锦区间重叠合并了 {plan.clip_merged_count} 段）")
        elif plan.icon_event_count == plan.clip_count:
            print(
                "      （每段图标时段对应一段集锦；未再合并时后两项相同）"
            )
        print(
            "      说明: 图标时段由采样命中合并得到，间隔小于阈值的相邻段会算作同一段，"
            "可能少于或多于真实击倒次数"
        )
    if meta_path is not None:
        save_ranges_json(meta_path, plan, merge_gap=merge_gap)
        if show_progress:
            print(f"      区间 JSON: {meta_path}")

    if show_progress:
        total_dur = sum(c.duration_sec for c in plan.clips)
        print_step(3, 3, f"ffmpeg 导出 {plan.clip_count} 段（总时长约 {total_dur:.1f}s）...")

    export_highlight_video(
        config.video,
        plan.clips,
        output_path,
        segments_dir=segments_dir,
        show_progress=show_progress,
    )
    return plan
