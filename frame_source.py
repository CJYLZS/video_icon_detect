"""从视频或抽帧目录按帧号读取 BGR 图像。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import cv2
import numpy as np

from paths import FFMPEG


def load_bgr(path: Path) -> np.ndarray:
    frame = cv2.imread(str(path))
    if frame is None:
        raise FileNotFoundError(f"无法读取图片: {path}")
    return frame


def open_video(video_path: Path) -> tuple[cv2.VideoCapture, int, float]:
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"无法打开视频: {video_path}")
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    return cap, total, fps


def _normalize_orientation_deg(value: float) -> int:
    deg = int(round(value)) % 360
    candidates = (0, 90, 180, 270)
    nearest = min(candidates, key=lambda x: abs(x - deg))
    if abs(nearest - deg) <= 2:
        return nearest
    return 0


def _apply_orientation(frame: np.ndarray, orientation_deg: float) -> np.ndarray:
    deg = _normalize_orientation_deg(orientation_deg)
    if deg == 90:
        return cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
    if deg == 180:
        return cv2.rotate(frame, cv2.ROTATE_180)
    if deg == 270:
        return cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return frame


def _get_capture_orientation(cap: cv2.VideoCapture) -> float:
    prop = getattr(cv2, "CAP_PROP_ORIENTATION_META", None)
    if prop is None:
        return 0.0
    return float(cap.get(prop))


def _resolve_orientation_for_manual_apply(cap: cv2.VideoCapture) -> float:
    orientation_deg = _get_capture_orientation(cap)
    auto_prop = getattr(cv2, "CAP_PROP_ORIENTATION_AUTO", None)
    if auto_prop is None:
        return orientation_deg
    cap.set(auto_prop, 0)
    if cap.get(auto_prop) >= 0.5:
        return 0.0
    return orientation_deg


def find_frame_image_path(image_dir: Path, idx: int) -> Path | None:
    """按帧号查找图片，兼容 frame_00123.jpg 与 frame_00123_t00012345ms.jpg。"""
    for ext in (".jpg", ".png"):
        plain = image_dir / f"frame_{idx:05d}{ext}"
        if plain.is_file():
            return plain
    for ext in (".jpg", ".png"):
        tagged = sorted(
            p
            for p in image_dir.glob(f"frame_{idx:05d}_*{ext}")
            if "_icon" not in p.stem
        )
        if tagged:
            return tagged[0]
    return None


def parse_frame_index_from_path(path: Path) -> int | None:
    stem = path.stem
    if not stem.startswith("frame_"):
        return None
    frame_str = stem[6:].split("_", 1)[0]
    if frame_str.isdigit():
        return int(frame_str)
    return None


def resolve_media_source(video: Path, image_dir: Path | None) -> tuple[Path | None, Path | None]:
    if image_dir and image_dir.is_dir():
        return None, image_dir
    if video.is_file():
        return video, None
    return None, image_dir


def _read_video_frame_at(
    cap: cv2.VideoCapture,
    idx: int,
    orientation_deg: float,
) -> np.ndarray | None:
    if not cap.set(cv2.CAP_PROP_POS_FRAMES, idx):
        return None
    ok, frame = cap.read()
    if not ok:
        return None
    return _apply_orientation(frame, orientation_deg)


class FrameSource:
    """按帧号读图/读视频；视频下复用 VideoCapture，升序帧号优先顺序解码。"""

    def __init__(self, video_path: Path | None, image_dir: Path | None) -> None:
        self._video_path = video_path
        self._image_dir = image_dir
        self._cap: cv2.VideoCapture | None = None
        self._orientation_deg = 0.0
        self._next_frame: int | None = None

    def __enter__(self) -> FrameSource:
        if self._video_path is not None and self._image_dir is None:
            cap = cv2.VideoCapture(str(self._video_path))
            if not cap.isOpened():
                raise RuntimeError(f"无法打开视频: {self._video_path}")
            self._cap = cap
            self._orientation_deg = _resolve_orientation_for_manual_apply(cap)
        return self

    def __exit__(self, *args: object) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None
        self._next_frame = None

    def _load_video(self, idx: int) -> np.ndarray | None:
        assert self._cap is not None
        cap = self._cap
        orient = self._orientation_deg

        if self._next_frame is None or idx < self._next_frame:
            frame = _read_video_frame_at(cap, idx, orient)
            if frame is not None:
                self._next_frame = idx + 1
            return frame

        frame = None
        for _ in range(idx - self._next_frame + 1):
            ok, raw = cap.read()
            if not ok:
                self._next_frame = None
                return None
            frame = raw
        self._next_frame = idx + 1
        return _apply_orientation(frame, orient)

    def load(self, idx: int) -> np.ndarray | None:
        if self._image_dir is not None:
            path = find_frame_image_path(self._image_dir, idx)
            if path is None:
                return None
            return cv2.imread(str(path))
        if self._cap is None:
            return None
        return self._load_video(idx)

    def position_msec(self) -> float:
        if self._cap is None:
            return 0.0
        return float(self._cap.get(cv2.CAP_PROP_POS_MSEC) or 0.0)


class FFmpegSource:
    """FFmpeg pipe 视频帧源（持续流式解码），和 clip-extract 统一使用 FFmpeg 解码。"""

    def __init__(self, video_path: Path, pts_index = None) -> None:
        self._video_path = video_path
        self._pts_index = pts_index
        self._proc: subprocess.Popen | None = None
        self._w = 0
        self._h = 0
        self._frame_bytes = 0
        self._next_idx = 0

    def __enter__(self) -> FFmpegSource:
        cap = cv2.VideoCapture(str(self._video_path))
        if not cap.isOpened():
            raise RuntimeError(f"无法打开视频: {self._video_path}")
        self._w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self._h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        cap.release()
        self._frame_bytes = self._w * self._h * 3
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        if self._proc:
            try:
                self._proc.stdout.close()
            except Exception:
                pass
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except Exception:
                self._proc.kill()
            self._proc = None

    def load(self, idx: int) -> np.ndarray | None:
        if self._proc is None:
            start_sec = 0.0
            if self._pts_index and 0 <= idx < len(self._pts_index.pts_sec):
                start_sec = self._pts_index.pts_sec[idx]
            self._start(start_sec)
            self._next_idx = idx

        if idx < self._next_idx:
            return None

        while self._next_idx < idx:
            self._proc.stdout.read(self._frame_bytes)
            self._next_idx += 1

        raw = self._proc.stdout.read(self._frame_bytes)
        if len(raw) != self._frame_bytes:
            return None
        self._next_idx = idx + 1
        return np.frombuffer(raw, np.uint8).reshape((self._h, self._w, 3))

    def position_msec(self) -> float:
        if self._pts_index and self._next_idx > 0:
            i = min(self._next_idx - 1, len(self._pts_index.pts_sec) - 1)
            return self._pts_index.pts_sec[i] * 1000.0
        return 0.0

    def _start(self, start_sec: float) -> None:
        self.close()
        cmd = [
            str(FFMPEG),
            "-ss", f"{start_sec:.6f}",
            "-i", str(self._video_path),
            "-vsync", "0",
            "-pix_fmt", "bgr24",
            "-f", "rawvideo",
            "-an",
            "-nostats", "-loglevel", "error",
            "pipe:",
        ]
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


def load_frame(video_path: Path | None, image_dir: Path | None, idx: int) -> np.ndarray | None:
    """读单帧；批量读取请用 FrameSource。"""
    if video_path is not None and image_dir is None:
        with FFmpegSource(video_path) as src:
            return src.load(idx)
    with FrameSource(video_path, image_dir) as src:
        return src.load(idx)
