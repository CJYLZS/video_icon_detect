"""主流程输出布局：每个输入视频一个独立输出目录。

目录内固定三件套：
- <stem>.mp4       第一阶段成片（击杀集锦，自动剪辑前）
- <stem>_bgm.mp4   第二阶段成片（auto-cut 变速 + 尾部截断 + BGM 合成）
- <stem>.json      片段清单（save_clip_manifest，auto-cut 的同名 JSON）

缓存规则：
- 第一阶段：clip 与 manifest 同时存在即视为缓存命中；
- 第二阶段：cut 存在【且】其内嵌的渲染配方标记与当前一致才命中
  （标记写在 mp4 的 comment metadata 里，见 autocut_recipe_tag）；
- --force 时全部视为未命中。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

# 第二阶段渲染配方版本：render.py 的滤镜语义每次发生实质变化都要 +1，
# 使已生成的旧成片自动失效重做（历史成片无标记，同样视为过期）。
#   v1: aresample=SR:first_pts=0 重同步替代 asetpts=N/SR/TB，修复音画累积漂移
AUTOCUT_RECIPE_VERSION = 1

# 内嵌标记的前缀，便于从 comment 里辨识（用户自己写的 comment 不会被误判）
_RECIPE_PREFIX = "autocut-v"


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


def autocut_recipe_tag(*, speed: float, cut_value: int, fade_sec: float) -> str:
    """当前渲染配方标记：版本号 + 影响成片内容的关键参数。

    写入成片的 comment metadata，供 autocut_cached 比对。
    版本号覆盖"滤镜逻辑变了"，参数覆盖"命令行参数变了"，两者任一不同即需重做。
    格式扁平且不含引号/换行，可安全作为 ffmpeg -metadata 的值。
    """
    return (
        f"{_RECIPE_PREFIX}{AUTOCUT_RECIPE_VERSION};"
        f"speed={speed:g};cut={cut_value:d};fade={fade_sec:g}"
    )


def read_recipe_tag(video: Path) -> str | None:
    """读回成片内嵌的配方标记；无标记/读取失败返回 None（一律视为过期）。"""
    from ..constants import FFPROBE

    if not video.is_file():
        return None
    try:
        proc = subprocess.run(
            [
                str(FFPROBE), "-v", "error",
                "-show_entries", "format_tags=comment",
                "-of", "default=nw=1:nk=1",
                str(video),
            ],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    tag = (proc.stdout or "").strip()
    return tag if tag.startswith(_RECIPE_PREFIX) else None


def autocut_cached(
    vo: VideoOutput,
    *,
    recipe: str,
    force: bool = False,
) -> bool:
    """第二阶段成片是否可复用：文件存在且内嵌配方标记与 recipe 一致。

    历史成片没有标记（返回 None），渲染逻辑或参数变更后标记不匹配，
    两种情况都判为未命中，自动重做——不必手动删文件。
    """
    if force:
        return False
    if not vo.cut.is_file():
        return False
    return read_recipe_tag(vo.cut) == recipe
