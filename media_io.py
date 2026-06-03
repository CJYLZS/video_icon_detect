"""兼容层：新代码请直接从 paths / frame_source / sampling / extract 导入。"""

from extract import extract_frames
from frame_source import (
    FrameSource,
    find_frame_image_path,
    load_bgr,
    load_frame,
    open_video,
    parse_frame_index_from_path,
    resolve_media_source,
)
from paths import DEFAULT_VIDEO, OUTPUT_DIR, ROI_CHECK_DIR, ROOT, TEMPLATE_DIR
from sampling import collect_extract_indices, resolve_frame_indices, sample_step

__all__ = [
    "DEFAULT_VIDEO",
    "FrameSource",
    "OUTPUT_DIR",
    "ROI_CHECK_DIR",
    "ROOT",
    "TEMPLATE_DIR",
    "collect_extract_indices",
    "extract_frames",
    "find_frame_image_path",
    "load_bgr",
    "load_frame",
    "open_video",
    "parse_frame_index_from_path",
    "resolve_frame_indices",
    "resolve_media_source",
    "sample_step",
]
