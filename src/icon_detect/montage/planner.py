"""剪辑区间规划：hit 分组、区间合并、ClipPlan 构建、元数据导出。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from ..constants import FIXED_DURATION_ACCEPT_MAX, FIXED_DURATION_ACCEPT_MIN, TAIL_APPEND_SEC
from ..video.index import VideoPtsIndex


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
    hit_frames: list[int]
    kills_before_merge: list[TimeRange]
    kills: list[TimeRange]
    clips_before_overlap: list[LabeledTimeRange]
    clips: list[LabeledTimeRange]
    missile_frames: list[int] | None = None
    missile_kills_before: list[TimeRange] | None = None
    missile_kills: list[TimeRange] | None = None
    fixed_duration_normalized: int = 0

    @property
    def hit_count(self) -> int:
        return len(self.hit_frames)

    @property
    def missile_hit_count(self) -> int:
        return len(self.missile_frames) if self.missile_frames is not None else 0

    @property
    def icon_event_count(self) -> int:
        return len(self.kills)

    @property
    def missile_event_count(self) -> int:
        return len(self.missile_kills) if self.missile_kills is not None else 0

    @property
    def knockdown_count(self) -> int:
        return self.icon_event_count

    @property
    def clip_count(self) -> int:
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
    video_duration: float,
    pad_before: float = 2.0,
    pad_after: float = 0.5,
) -> list[TimeRange]:
    if not kills:
        return []
    clips: list[TimeRange] = []
    for k in kills:
        start = max(0.0, k.start_sec - pad_before)
        end = min(video_duration, k.end_sec + pad_after)
        if end > start:
            clips.append(TimeRange(start, end))
    return clips


def normalize_clip_to_fixed_duration(
    kill: TimeRange,
    video_duration: float,
    target_frames: int,
    output_fps: float,
    *,
    pad_before: float = 2.0,
    pad_after: float = 0.5,
    accept_min_sec: float = FIXED_DURATION_ACCEPT_MIN,
    accept_max_sec: float = FIXED_DURATION_ACCEPT_MAX,
) -> TimeRange | None:
    natural_dur = kill.duration_sec + pad_before + pad_after
    if natural_dur < accept_min_sec or natural_dur > accept_max_sec:
        return None

    target_sec = target_frames / output_fps

    # 主路径：前垫固定不动，只调整后垫 → 击杀瞬间在段落内的偏移量恒定。
    # 长度精确 = target_frames，代价是后垫长度可变（0.3~0.8s 等）。
    start = max(0.0, kill.start_sec - pad_before)
    tail_sec = start + target_sec - kill.end_sec
    if tail_sec >= 0.0:
        end = min(video_duration, start + target_sec)
        if end > start:
            return TimeRange(start, end)
        return None

    # 回退路径：击杀过长导致后垫为负（无法在不截断击杀的前提下凑满前垫+目标长度），
    # 退回两侧调整（偏长削前垫 / 偏短补后垫），保证击杀画面完整，代价是偏移量不再恒定。
    natural_frames = natural_dur * output_fps
    excess_frames = natural_frames - target_frames

    adj_before = pad_before
    adj_after = pad_after

    if excess_frames > 0:
        adj_before = max(0.0, pad_before - excess_frames / output_fps)
    else:
        adj_after = pad_after + abs(excess_frames) / output_fps

    start = max(0.0, kill.start_sec - adj_before)
    end = min(video_duration, kill.end_sec + adj_after)
    if end <= start:
        return None
    return TimeRange(start, end)


def append_video_tail(
    clips: list[LabeledTimeRange],
    video_duration: float,
    *,
    tail_sec: float = TAIL_APPEND_SEC,
) -> list[LabeledTimeRange]:
    if video_duration <= 0 or tail_sec <= 0:
        return clips
    tail_start = max(0.0, video_duration - tail_sec)
    tail = LabeledTimeRange(start_sec=tail_start, end_sec=video_duration, type="tail")
    if tail.duration_sec <= 0:
        return clips
    return [*clips, tail]


def merge_overlapping_clip_ranges(ranges: list[LabeledTimeRange]) -> list[LabeledTimeRange]:
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
    fixed_duration_frames: int | None = None,
    fixed_duration_fps: float = 10.0,
    fixed_duration_accept_min: float = FIXED_DURATION_ACCEPT_MIN,
    fixed_duration_accept_max: float = FIXED_DURATION_ACCEPT_MAX,
) -> ClipPlan:
    hit_times = [index.pts_for_frame(f) for f in sorted(hit_frames)]
    kills_before = group_hits_to_kill_intervals(hit_times, max_hit_gap=max_hit_gap)
    kills = merge_adjacent_kill_intervals(kills_before, gap_sec=merge_gap)

    use_fixed = fixed_duration_frames is not None and fixed_duration_frames > 0
    kd_clips: list[LabeledTimeRange] = []
    normalized = 0
    for kill in kills:
        if use_fixed:
            adjusted = normalize_clip_to_fixed_duration(
                kill,
                video_duration=index.duration_sec,
                target_frames=fixed_duration_frames,
                output_fps=fixed_duration_fps,
                pad_before=pad_before,
                pad_after=pad_after,
                accept_min_sec=fixed_duration_accept_min,
                accept_max_sec=fixed_duration_accept_max,
            )
            if adjusted is not None:
                kd_clips.append(
                    LabeledTimeRange(start_sec=adjusted.start_sec, end_sec=adjusted.end_sec, type="knockdown")
                )
                normalized += 1
                continue
        start = max(0.0, kill.start_sec - pad_before)
        end = min(index.duration_sec, kill.end_sec + pad_after)
        if end > start:
            kd_clips.append(LabeledTimeRange(start_sec=start, end_sec=end, type="knockdown"))

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
        fixed_duration_normalized=normalized,
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
            "fixed_duration_normalized": plan.fixed_duration_normalized,
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
