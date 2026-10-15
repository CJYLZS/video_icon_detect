"""同名 JSON 清单解析 + 输入视频媒体信息探测。"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..constants import FFPROBE


@dataclass(frozen=True)
class MediaInfo:
    """ffprobe 实测的输入视频基本信息。"""

    width: int
    height: int
    fps: float
    duration_sec: float
    has_audio: bool
    sample_rate: int = 0  # 音频采样率（无音频流时为 0）


@dataclass(frozen=True)
class ManifestInfo:
    """同名 clip 清单中 auto-cut 需要的信息（全部为输入视频时间轴）。"""

    path: Path | None
    tail_start_sec: float | None
    missile_span: tuple[float, float] | None  # (start_sec, end_sec)，仅当首段为导弹
    missile_pad_after_sec: float | None = None  # 清单记录的导弹片段后延秒数（落地切镜搜索后缀）


def probe_media_info(video_path: Path) -> MediaInfo:
    """用 ffprobe 读取宽度/高度/帧率/时长/是否有音频流。"""
    if not FFPROBE.is_file():
        raise FileNotFoundError(f"ffprobe 不存在: {FFPROBE}")
    cmd = [
        str(FFPROBE),
        "-v", "error",
        "-show_entries",
        "stream=width,height,avg_frame_rate,r_frame_rate,codec_type,sample_rate:format=duration",
        "-of", "json",
        str(video_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe 失败: {(proc.stderr or proc.stdout or '').strip()}")
    data = json.loads(proc.stdout or "{}")
    streams = data.get("streams") or []
    if not streams:
        raise RuntimeError(f"ffprobe 未返回流信息: {video_path}")

    v = next((s for s in streams if s.get("codec_type") == "video"), streams[0])
    width = int(v.get("width", 0))
    height = int(v.get("height", 0))
    fps = _parse_rate(v.get("avg_frame_rate")) or _parse_rate(v.get("r_frame_rate")) or 30.0
    fmt = data.get("format") or {}
    duration = float(fmt.get("duration", 0.0) or 0.0)
    if duration <= 0:
        duration = float(v.get("duration", 0.0) or 0.0)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    sample_rate = 0
    if has_audio:
        a = next(s for s in streams if s.get("codec_type") == "audio")
        sample_rate = int(a.get("sample_rate", 0) or 0)
        if sample_rate <= 0:
            raise RuntimeError(f"ffprobe 未返回音频采样率: {video_path}")
    if width <= 0 or height <= 0 or duration <= 0:
        raise RuntimeError(f"ffprobe 返回的媒体信息不完整: {video_path}")
    return MediaInfo(
        width=width,
        height=height,
        fps=fps,
        duration_sec=duration,
        has_audio=has_audio,
        sample_rate=sample_rate,
    )


def _parse_rate(text: str | None) -> float | None:
    if not text:
        return None
    try:
        if "/" in text:
            num, den = text.split("/", 1)
            den_f = float(den)
            return float(num) / den_f if den_f else None
        return float(text)
    except (TypeError, ValueError):
        return None


def load_manifest(video_path: Path, json_path: Path | None = None) -> ManifestInfo:
    """读取与视频同名的 clip 清单 JSON；不存在时返回空信息。"""
    if json_path is None:
        json_path = video_path.with_suffix(".json")
    if not json_path.is_file():
        return ManifestInfo(path=None, tail_start_sec=None, missile_span=None)

    data = json.loads(json_path.read_text(encoding="utf-8"))
    segments = data.get("segments") or []

    tail_start: float | None = None
    for seg in reversed(segments):
        if seg.get("type") == "tail":
            out = seg.get("output") or {}
            start = out.get("start_sec")
            if start is not None:
                tail_start = float(start)
            break

    missile_span: tuple[float, float] | None = None
    if segments:
        first = segments[0]
        if first.get("type") == "missile":
            out = first.get("output") or {}
            s, e = out.get("start_sec"), out.get("end_sec")
            if s is not None and e is not None and float(e) > float(s):
                missile_span = (float(s), float(e))

    return ManifestInfo(path=json_path, tail_start_sec=tail_start, missile_span=missile_span)
