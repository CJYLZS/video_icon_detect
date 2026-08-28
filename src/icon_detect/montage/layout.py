"""主流程输出布局：每个输入视频一个独立输出目录。

目录内固定三件套：
- <stem>.mp4       第一阶段成片（击杀集锦，自动剪辑前）
- <stem>_bgm.mp4   第二阶段成片（auto-cut 变速 + 尾部截断 + BGM 合成）
- <stem>.json      片段清单（save_clip_manifest，auto-cut 的同名 JSON）

缓存规则以"文件是否存在"为准：
- 第一阶段：clip 与 manifest 同时存在即视为缓存命中；
- 第二阶段：cut 存在即命中；--force 时全部视为未命中。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class VideoOutput:
    """单个输入视频的输出路径集。"""

    root: Path  # 本视频专属输出目录
    clip: Path  # 自动剪辑前：击杀集锦 mp4
    cut: Path  # 自动剪辑后：<stem>_bgm.mp4
    manifest: Path  # 片段清单 JSON
    stats: Path  # 战局统计 txt（结算界面提取）

    def missing_files(self) -> list[Path]:
        return [p for p in (self.clip, self.cut, self.manifest, self.stats) if not p.is_file()]


def build_video_output(output_dir: Path, stem: str) -> VideoOutput:
    """按输出根目录 + 输入视频 stem 计算三件套+统计文件路径。"""
    d = Path(output_dir) / stem
    return VideoOutput(
        root=d,
        clip=d / f"{stem}.mp4",
        cut=d / f"{stem}_bgm.mp4",
        manifest=d / f"{stem}.json",
        stats=d / "战局统计.txt",
    )


def stage_one_cached(vo: VideoOutput, *, force: bool = False) -> bool:
    """第一阶段（检测 + 集锦导出）是否可复用缓存。

    需要成片与清单同时存在：auto-cut 依赖同名 JSON，
    只剩 mp4 时视为缓存不完整，须重跑检测补写清单。
    """
    if force:
        return False
    return vo.clip.is_file() and vo.manifest.is_file()


def autocut_cached(vo: VideoOutput, *, force: bool = False) -> bool:
    """第二阶段（自动剪辑 + BGM）成片是否已存在。"""
    if force:
        return False
    return vo.cut.is_file()
