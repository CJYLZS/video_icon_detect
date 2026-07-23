"""ffprobe 视频 PTS 索引：全片 demux packet，缓存到磁盘。"""

from __future__ import annotations

import bisect
import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..constants import FFPROBE, VIDEO_INDEX_DIR

INDEX_VERSION = 2


@dataclass(frozen=True)
class VideoPtsIndex:
    frame_indices: tuple[int, ...]
    pts_sec: tuple[float, ...]
    duration_sec: float

    def locate_first_at_or_after(self, target_sec: float) -> int:
        i = bisect.bisect_left(self.pts_sec, target_sec)
        if i >= len(self.pts_sec):
            return len(self.frame_indices)
        return self.frame_indices[i]

    def locate_last_at_or_before(self, target_sec: float) -> int:
        i = bisect.bisect_right(self.pts_sec, target_sec) - 1
        if i < 0:
            return -1
        return self.frame_indices[i]

    @property
    def max_sec(self) -> float:
        return self.pts_sec[-1] if self.pts_sec else 0.0

    def pts_for_frame(self, frame_idx: int) -> float:
        if not self.pts_sec:
            return 0.0
        i = bisect.bisect_left(self.frame_indices, frame_idx)
        if i < len(self.frame_indices) and self.frame_indices[i] == frame_idx:
            return self.pts_sec[i]
        if i > 0:
            return self.pts_sec[i - 1]
        return self.pts_sec[0]

    def frame_for_pts(self, pts_sec: float) -> int:
        if not self.frame_indices:
            return 0
        i = bisect.bisect_left(self.pts_sec, pts_sec)
        if i >= len(self.pts_sec):
            return self.frame_indices[-1]
        if i > 0 and abs(self.pts_sec[i - 1] - pts_sec) < abs(self.pts_sec[i] - pts_sec):
            return self.frame_indices[i - 1]
        return self.frame_indices[i]


def _video_cache_key(video_path: Path) -> str:
    resolved = str(video_path.resolve())
    st = video_path.stat()
    digest = hashlib.sha256(f"{resolved}|{st.st_mtime_ns}|{st.st_size}".encode()).hexdigest()
    return digest[:16]


def index_cache_path(video_path: Path) -> Path:
    return VIDEO_INDEX_DIR / f"{video_path.stem}_{_video_cache_key(video_path)}.json"


def _parse_pts_lines(text: str) -> list[float]:
    out: list[float] = []
    for line in text.splitlines():
        s = line.strip().rstrip(",")
        if not s or s == "N/A":
            continue
        out.append(float(s))
    return out


def build_pts_index(video_path: Path, *, probe_packets: int | None = None) -> VideoPtsIndex:
    if not FFPROBE.is_file():
        raise FileNotFoundError(f"ffprobe 不存在: {FFPROBE}")
    if not video_path.is_file():
        raise FileNotFoundError(f"视频不存在: {video_path}")

    cmd = [
        str(FFPROBE),
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "packet=pts_time",
        "-of",
        "csv=p=0",
    ]
    if probe_packets is not None:
        cmd.extend(["-read_intervals", f"%+#{probe_packets}"])
    cmd.append(str(video_path))

    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"ffprobe 失败: {err}")

    demux_pts = _parse_pts_lines(proc.stdout)
    if not demux_pts:
        raise RuntimeError("ffprobe 未返回任何 packet 时间戳")

    pairs = sorted(enumerate(demux_pts), key=lambda x: x[1])
    frame_indices = tuple(range(len(pairs)))
    pts_sec = tuple(t for _, t in pairs)
    return VideoPtsIndex(
        frame_indices=frame_indices,
        pts_sec=pts_sec,
        duration_sec=pts_sec[-1],
    )


def save_index_cache(video_path: Path, index: VideoPtsIndex, cache_path: Path) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    st = video_path.stat()
    payload = {
        "version": INDEX_VERSION,
        "source": str(video_path.resolve()),
        "mtime_ns": st.st_mtime_ns,
        "size": st.st_size,
        "frame_count": len(index.pts_sec),
        "duration_sec": index.duration_sec,
        "frame_indices": list(index.frame_indices),
        "pts_sec": list(index.pts_sec),
    }
    cache_path.write_text(json.dumps(payload), encoding="utf-8")


def _load_index_cache(cache_path: Path, video_path: Path) -> VideoPtsIndex | None:
    if not cache_path.is_file():
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if data.get("version") != INDEX_VERSION:
        return None
    st = video_path.stat()
    if data.get("mtime_ns") != st.st_mtime_ns or data.get("size") != st.st_size:
        return None
    if data.get("source") != str(video_path.resolve()):
        return None
    return VideoPtsIndex(
        frame_indices=tuple(data["frame_indices"]),
        pts_sec=tuple(data["pts_sec"]),
        duration_sec=float(data["duration_sec"]),
    )


def load_or_build_index(video_path: Path, *, rebuild: bool = False) -> VideoPtsIndex:
    cache_path = index_cache_path(video_path)
    if not rebuild:
        cached = _load_index_cache(cache_path, video_path)
        if cached is not None:
            return cached

    print(f"正在建立 PTS 索引（ffprobe packet）: {video_path.name} ...")
    index = build_pts_index(video_path)
    save_index_cache(video_path, index, cache_path)
    print(f"索引已缓存: {cache_path} ({len(index.pts_sec)} 帧, {index.duration_sec:.2f}s)")
    return index
