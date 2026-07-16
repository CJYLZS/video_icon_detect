"""根据击倒检测生成击杀集锦片段并导出视频。"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from infer import InferConfig, collect_combined, collect_hit_frame_indices
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


@dataclass(frozen=True)
class LabeledTimeRange(TimeRange):
    type: str = ""


@dataclass
class ClipPlan:
    """检测命中 -> 图标连续段 -> 集锦片段 的完整规划。"""

    hit_frames: list[int]
    kills_before_merge: list[TimeRange]
    kills: list[TimeRange]
    clips_before_overlap: list[LabeledTimeRange]
    clips: list[LabeledTimeRange]
    missile_frames: list[int] | None = None
    missile_kills_before: list[TimeRange] | None = None
    missile_kills: list[TimeRange] | None = None

    @property
    def hit_count(self) -> int:
        return len(self.hit_frames)

    @property
    def missile_hit_count(self) -> int:
        return len(self.missile_frames) if self.missile_frames is not None else 0

    @property
    def icon_event_count(self) -> int:
        """击倒图标连续出现的时段数（≠ 真实击倒次数，近距多杀会被合并）。"""
        return len(self.kills)

    @property
    def missile_event_count(self) -> int:
        return len(self.missile_kills) if self.missile_kills is not None else 0

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
        merged = merge_overlapping_clip_ranges(self.clips_before_overlap)
        return len(self.clips_before_overlap) - len(merged)

    def summary_line(self) -> str:
        base = (
            f"采样命中 {self.hit_count} 次 -> "
            f"图标时段 {self.icon_event_count} 段"
        )
        if self.missile_hit_count:
            base += f"，导弹命中 {self.missile_hit_count} 次 -> {self.missile_event_count} 段"
        base += f" -> 集锦 {self.clip_count} 段"
        return base


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


TAIL_APPEND_SEC = 12.0


def kill_intervals_to_clip_ranges(
    kills: list[TimeRange],
    *,
    video_duration: float,
    pad_before: float = 2.0,
    pad_after: float = 0.5,
) -> list[TimeRange]:
    """击杀区间 -> 剪辑区间：前后各留 pad（含最后一段）。"""
    if not kills:
        return []
    clips: list[TimeRange] = []
    for k in kills:
        start = max(0.0, k.start_sec - pad_before)
        end = min(video_duration, k.end_sec + pad_after)
        if end > start:
            clips.append(TimeRange(start, end))
    return clips


def append_video_tail(
    clips: list[LabeledTimeRange],
    video_duration: float,
    *,
    tail_sec: float = TAIL_APPEND_SEC,
) -> list[LabeledTimeRange]:
    """在集锦末尾追加原视频最后 tail_sec 秒（独立片段，type='tail'）。"""
    if video_duration <= 0 or tail_sec <= 0:
        return clips
    tail_start = max(0.0, video_duration - tail_sec)
    tail = LabeledTimeRange(start_sec=tail_start, end_sec=video_duration, type="tail")
    if tail.duration_sec <= 0:
        return clips
    return [*clips, tail]


def merge_overlapping_clip_ranges(ranges: list[LabeledTimeRange]) -> list[LabeledTimeRange]:
    """合并重叠或相接的剪辑区间；不同 type 重叠合并后 type 标记为 'mixed'。"""
    if not ranges:
        return []
    ordered = sorted(ranges, key=lambda r: r.start_sec)
    out = [ordered[0]]
    for r in ordered[1:]:
        prev = out[-1]
        if r.start_sec <= prev.end_sec:
            merged_type = prev.type if prev.type == r.type else "mixed"
            out[-1] = LabeledTimeRange(
                start_sec=prev.start_sec,
                end_sec=max(prev.end_sec, r.end_sec),
                type=merged_type,
            )
        else:
            out.append(r)
    return out


def build_clip_plan(
    hit_frames: list[int],
    index: VideoPtsIndex,
    *,
    pad_before: float = 2.0,
    pad_after: float = 0.5,
    max_hit_gap: float = 2.0,
    merge_gap: float = 2.0,
    missile_frames: list[int] | None = None,
    missile_pad_before: float = 5.0,
    missile_pad_after: float = 5.0,
    missile_prefix: bool = True,
) -> ClipPlan:
    hit_times = [index.pts_for_frame(f) for f in sorted(hit_frames)]
    kills_before = group_hits_to_kill_intervals(hit_times, max_hit_gap=max_hit_gap)
    kills = merge_adjacent_kill_intervals(kills_before, gap_sec=merge_gap)
    kd_clips = [
        LabeledTimeRange(start_sec=c.start_sec, end_sec=c.end_sec, type="knockdown")
        for c in kill_intervals_to_clip_ranges(
            kills,
            video_duration=index.duration_sec,
            pad_before=pad_before,
            pad_after=pad_after,
        )
    ]

    m_kills_before: list[TimeRange] = []
    m_kills: list[TimeRange] = []
    m_clips: list[LabeledTimeRange] = []
    if missile_frames:
        m_times = [index.pts_for_frame(f) for f in sorted(missile_frames)]
        m_kills_before = group_hits_to_kill_intervals(m_times, max_hit_gap=max_hit_gap)
        m_kills = merge_adjacent_kill_intervals(m_kills_before, gap_sec=merge_gap)
        m_clips = [
            LabeledTimeRange(start_sec=c.start_sec, end_sec=c.end_sec, type="missile")
            for c in kill_intervals_to_clip_ranges(
                m_kills,
                video_duration=index.duration_sec,
                pad_before=missile_pad_before,
                pad_after=missile_pad_after,
            )
        ]

    all_clips_before = sorted(kd_clips + m_clips, key=lambda r: r.start_sec)
    clips = append_video_tail(
        merge_overlapping_clip_ranges(all_clips_before),
        index.duration_sec,
    )
    if missile_prefix and m_clips:
        clips = sorted(m_clips, key=lambda r: r.start_sec) + clips
    return ClipPlan(
        hit_frames=sorted(hit_frames),
        kills_before_merge=kills_before,
        kills=kills,
        clips_before_overlap=all_clips_before,
        clips=clips,
        missile_frames=sorted(missile_frames) if missile_frames else None,
        missile_kills_before=m_kills_before if missile_frames else None,
        missile_kills=m_kills if missile_frames else None,
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
    if plan.missile_frames is not None:
        payload["summary"]["missile_hits"] = plan.missile_hit_count
        payload["summary"]["missile_events"] = plan.missile_event_count
        payload["missile_frames"] = plan.missile_frames
        if plan.missile_kills_before:
            payload["missile_intervals_before_merge"] = [asdict(k) for k in plan.missile_kills_before]
        if plan.missile_kills:
            payload["missile_intervals"] = [asdict(k) for k in plan.missile_kills]
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def save_clip_manifest(
    output_path: Path,
    clips: list[LabeledTimeRange],
    index: VideoPtsIndex,
    *,
    source_fps: float,
    source_video: Path,
) -> None:
    """生成片段偏移清单 JSON，写入 output_path（同目录、与视频同名）。"""
    segments: list[dict] = []
    output_offset = 0.0

    for i, clip in enumerate(clips):
        src_start = clip.start_sec
        src_end = clip.end_sec
        src_duration = clip.duration_sec
        src_start_frame = index.frame_for_pts(src_start)
        src_end_frame = index.frame_for_pts(src_end)

        out_start = output_offset
        out_end = output_offset + src_duration

        segments.append({
            "segment": i + 1,
            "type": clip.type,
            "source": {
                "start_sec": round(src_start, 3),
                "end_sec": round(src_end, 3),
                "duration_sec": round(src_duration, 3),
                "start_frame": src_start_frame,
                "end_frame": src_end_frame,
            },
            "output": {
                "start_sec": round(out_start, 3),
                "end_sec": round(out_end, 3),
                "duration_sec": round(src_duration, 3),
                "start_frame": int(out_start * source_fps),
                "end_frame": int(out_end * source_fps),
            },
        })
        output_offset += src_duration

    payload = {
        "source_video": str(source_video.resolve()),
        "source_duration_sec": round(index.duration_sec, 3),
        "source_fps": source_fps,
        "output_video": str(output_path.resolve()),
        "output_duration_sec": round(output_offset, 3),
        "total_segments": len(segments),
        "segments": segments,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    if not segments:
        print("      警告: 无片段数据，清单为空")


def _extract_segment(
    video: Path, out: Path, seg: TimeRange, *, use_cuda: bool = False
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
) -> Path:
    """将多段剪辑区间拼接为单个输出视频。"""
    if not clips:
        raise ValueError("无剪辑区间")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    n = len(clips)

    if n == 1:
        if show_progress:
            print_progress(1, 1, label="导出", detail=_fmt_range(clips[0]))
        _extract_segment(video, output_path, clips[0], use_cuda=use_cuda)
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
            _extract_segment(video, part, seg, use_cuda=use_cuda)
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
) -> ClipPlan:
    if show_progress:
        mode_tag = "检测" if not enable_missile else "击倒 + 导弹检测"
        print_step(1, 3, f"建立时间索引并{mode_tag}（{len(config.indices)} 帧）...")

    index = load_or_build_index(config.video)

    if enable_missile:
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
    )

    if show_progress:
        print_step(2, 3, "规划剪辑区间...")
        print(f"      {plan.summary_line()}")
        if plan.kill_merged_count:
            print(f"      （相邻图标时段按 {merge_gap}s 合并了 {plan.kill_merged_count} 段）")
        if plan.missile_event_count:
            print(f"      （导弹区间前 {missile_pad_before}s 后 {missile_pad_after}s）")
        if missile_prefix and plan.missile_event_count:
            print(f"      （导弹片段已复制到集锦开头，共 {plan.missile_event_count} 段）")
        if plan.clip_merged_count:
            print(f"      （集锦区间重叠合并了 {plan.clip_merged_count} 段）")
        elif plan.clip_count == (plan.icon_event_count + plan.missile_event_count):
            print(
                "      （每段事件对应一段集锦；未再合并时后两项相同）"
            )
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
        source_fps=config.fps,
        source_video=config.video,
    )
    if show_progress:
        print(f"      片段清单: {manifest_path}")

    if show_progress:
        total_dur = sum(c.duration_sec for c in plan.clips)
        print_step(3, 3, f"ffmpeg 导出 {plan.clip_count} 段（总时长约 {total_dur:.1f}s）...")

    export_highlight_video(
        config.video,
        plan.clips,
        output_path,
        segments_dir=segments_dir,
        show_progress=show_progress,
        use_cuda=use_cuda,
    )
    return plan
