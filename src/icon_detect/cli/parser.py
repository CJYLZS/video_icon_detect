"""CLI agrparse 定义。"""

from __future__ import annotations

import argparse
from pathlib import Path

from icon_detect.constants import (
    CLIPS_DIR,
    DEFAULT_CLS_THRESH,
    DEFAULT_FIXED_DURATION_FRAMES,
    DEFAULT_MATCH_THRESH,
    FIXED_DURATION_ACCEPT_MAX,
    FIXED_DURATION_ACCEPT_MIN,
    OUTPUT_DIR,
    ROI_CHECK_DIR,
)


def _add_time_range_group(parser: argparse.ArgumentParser) -> None:
    r = parser.add_argument_group("范围")
    r.add_argument("--start-sec", type=float, default=None, help="起始时间（秒）；省略则从 0 开始")
    r.add_argument("--end-sec", type=float, default=None, help="结束时间（秒）；省略则到视频结束")
    r.add_argument("--step", type=int, default=1, help="每隔多少帧取 1 张（与 --fps 二选一，--fps 优先）")


def _add_fps_arg(parser: argparse.ArgumentParser, *, required: bool) -> None:
    kwargs: dict = {
        "type": float,
        "metavar": "HZ",
        "help": "采样/抽帧频率（每秒张数），按视频原始 fps 换算步长",
    }
    if required:
        kwargs["required"] = True
    else:
        kwargs["default"] = None
    parser.add_argument("--fps", **kwargs)


def _add_classifier_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--match-thresh", type=float, default=DEFAULT_MATCH_THRESH)
    parser.add_argument(
        "--use-classifier",
        action="store_true",
        help="同时使用模板+分类器判定（默认仅分类器）",
    )
    parser.add_argument(
        "--template-only",
        action="store_true",
        help="仅使用模板匹配（默认仅分类器）",
    )
    parser.add_argument("--cls-thresh", type=float, default=DEFAULT_CLS_THRESH)


def _add_detect_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--image-dir", type=Path, help="可选，从已抽帧目录读图")
    _add_classifier_args(parser)


def _add_clip_common_args(parser: argparse.ArgumentParser) -> None:
    _add_time_range_group(parser)
    _add_fps_arg(parser, required=True)
    _add_detect_args(parser)
    parser.add_argument("--gpu", action="store_true", help="导出时使用 CUDA + NVENC 硬件加速编码（需 NVIDIA GPU）")
    parser.add_argument(
        "--output-fps",
        type=float,
        default=60,
        metavar="HZ",
        help="导出视频帧率（默认 60fps）",
    )
    parser.add_argument("--pad-before", type=float, default=2, help="击杀开始前保留秒数")
    parser.add_argument("--pad-after", type=float, default=0.5, help="击杀结束后保留秒数")
    parser.add_argument(
        "--max-hit-gap",
        type=float,
        default=2.0,
        help="相邻命中时刻合并为同一击杀的最大间隔（秒）",
    )
    parser.add_argument(
        "--merge-gap",
        type=float,
        default=2.0,
        help="相邻击杀区间合并的最大间隔（秒，按击杀结束~下一击杀开始）",
    )
    parser.add_argument("--no-progress", action="store_true", help="关闭进度条")
    parser.add_argument("--missile", action="store_true", help="同时检测巡航导弹（OCR）")
    parser.add_argument("--missile-pad-before", type=float, default=7.0, help="导弹片段前延秒数")
    parser.add_argument("--missile-pad-after", type=float, default=4.0, help="导弹片段后延秒数")
    parser.add_argument(
        "--no-missile-prefix",
        action="store_true",
        help="不在集锦开头额外复制导弹片段（默认会复制一份按时间排序放在开头）",
    )
    parser.add_argument(
        "--fixed-duration-frames",
        type=int,
        default=DEFAULT_FIXED_DURATION_FRAMES,
        metavar="N",
        help=f"固定每段剪辑帧数，默认开启 {DEFAULT_FIXED_DURATION_FRAMES} 帧（2.8s@60fps）；"
        "前垫固定不变、只调整后垫，击杀偏移量恒定；传 0 可禁用",
    )
    parser.add_argument(
        "--fixed-duration-min",
        type=float,
        default=FIXED_DURATION_ACCEPT_MIN,
        metavar="SEC",
        help=f"自然时长低于此值不执行固定时长调整（默认 {FIXED_DURATION_ACCEPT_MIN}s）",
    )
    parser.add_argument(
        "--fixed-duration-max",
        type=float,
        default=FIXED_DURATION_ACCEPT_MAX,
        metavar="SEC",
        help=f"自然时长高于此值不执行固定时长调整（默认 {FIXED_DURATION_ACCEPT_MAX}s）",
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="和平精英击倒图标检测（固定 ROI + 二值 template）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  python main.py extract --video test.mp4 --output-dir frames --start-sec 38 --end-sec 45 --fps 5
  python main.py template --image frames/positive/frame_02671_t00044715ms.jpg
  python main.py train --positive-dir frames/positive --negative-dir frames/negative --epochs 80
  python main.py infer --video test.mp4 --image-dir frames --start-sec 88 --end-sec 90 --fps 5 --output-dir output/icon_detect
  python main.py clip --video test.mp4 --fps 10 -o output/clips/highlight.mp4
  python main.py clip --video test.mp4 --start-sec 70 --end-sec 80 --fps 10
  python main.py clip-batch --input-dir input_videos --output-dir output/clips --fps 10
  python main.py clip --video test.mp4 --fps 10 --use-classifier -o output/clips/highlight.mp4
  python main.py clip --video test.mp4 --fps 10 --template-only -o output/clips/highlight.mp4
""",
    )
    sub = p.add_subparsers(dest="command", required=True)

    from icon_detect.cli.handlers import (
        cmd_auto_cut,
        cmd_clip,
        cmd_clip_batch,
        cmd_clip_extract,
        cmd_export_icons,
        cmd_extract,
        cmd_infer,
        cmd_template,
        cmd_train,
    )

    ex = sub.add_parser("extract", help="从视频提取帧")
    ex.add_argument("--video", type=Path, required=True)
    ex.add_argument("--output-dir", type=Path, required=True)
    g = ex.add_mutually_exclusive_group()
    g.add_argument("--frame", type=int, action="append")
    g.add_argument("--seconds", type=float, action="append")
    _add_time_range_group(ex)
    _add_fps_arg(ex, required=False)
    ex.add_argument("--prefix", default="frame")
    ex.add_argument("--ext", default="jpg", choices=("jpg", "png"))
    ex.set_defaults(func=cmd_extract)

    eo = sub.add_parser(
        "export-icons",
        help="按 *_icon.jpg 清单，用 PTS 从视频导出与推理一致的原始帧",
    )
    eo.add_argument("--video", type=Path, required=True)
    eo.add_argument(
        "--icon-dir",
        type=Path,
        required=True,
        help="含 frame_XXXXX_icon.jpg 的目录（如 frames/negative）",
    )
    eo.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="输出目录，默认写入 --icon-dir",
    )
    eo.add_argument("--prefix", default="frame")
    eo.add_argument("--ext", default="jpg", choices=("jpg", "png"))
    eo.add_argument(
        "--keep-stale",
        action="store_true",
        help="保留同帧号下其它 _t*.jpg（默认删除后再写入）",
    )
    eo.set_defaults(func=cmd_export_icons)

    tpl = sub.add_parser("template", help="从单张图片构造模板")
    tpl.add_argument("--image", type=Path, required=True, help="含击倒图标的整帧截图")
    tpl.add_argument("--preview-dir", type=Path, default=ROI_CHECK_DIR)
    tpl.set_defaults(func=cmd_template)

    tr = sub.add_parser("train", help="训练可选 ROI 分类器")
    tr.add_argument(
        "--image-dir",
        type=Path,
        help="混合帧目录（需配合 --positive / --positive-from 打标签）",
    )
    tr.add_argument(
        "--positive-dir",
        type=Path,
        help="正样本目录（目录内图片均为正样本，需配合 --negative-dir）",
    )
    label = tr.add_mutually_exclusive_group(required=False)
    label.add_argument(
        "--positive",
        type=int,
        nargs="+",
        metavar="FRAME",
        help="正样本帧号列表，其余为负样本",
    )
    label.add_argument(
        "--positive-from",
        type=int,
        metavar="N",
        help=">= N 的帧为正样本（其余为负样本）",
    )
    tr.add_argument(
        "--negative-dir",
        type=Path,
        action="append",
        default=None,
        metavar="DIR",
        help="负样本目录（可重复）；目录模式必填，混合模式为额外负样本",
    )
    tr.add_argument("--epochs", type=int, default=40)
    tr.add_argument("--batch-size", type=int, default=8)
    tr.add_argument("--lr", type=float, default=1e-3)
    tr.set_defaults(func=cmd_train)

    ce = sub.add_parser("clip-extract", help="从原视频（优先）或 clip 视频提取指定片段的帧（用于补充训练样本）")
    ce.add_argument("--meta-json", type=Path, required=True, metavar="JSON", help="clip 输出的片段清单 JSON")
    ce.add_argument("--segment", type=int, required=True, metavar="N", help="片段序号（1-based）")
    ce.add_argument("--source-video", type=Path, default=None, metavar="VIDEO", help="原视频路径（默认从 JSON 读取 source_video；未提供则用 clip 视频）")
    ce.add_argument("--output-dir", type=Path, default=Path("data/negative"), help="输出目录（默认 data/negative/）")
    ce.add_argument("--fps", type=float, required=True, metavar="HZ", help="采样频率（每秒帧数）")
    ce.add_argument("--prefix", default="frame", help="输出文件名前缀（默认 frame）")
    ce.add_argument("--ext", default="png", choices=("jpg", "png"), help="输出图片格式（默认 png，无损，保留分类器精度）")
    ce.set_defaults(func=cmd_clip_extract)

    inf = sub.add_parser("infer", help="对视频按时间范围推理")
    inf.add_argument("--video", type=Path, required=True)
    inf.add_argument(
        "--image-dir",
        type=Path,
        help="可选，从已抽帧目录读取图片（帧号须与 --video 时间采样一致）",
    )
    inf.add_argument("--output-dir", type=Path, default=OUTPUT_DIR / "icon_detect")
    _add_time_range_group(inf)
    _add_fps_arg(inf, required=False)
    _add_classifier_args(inf)

    inf.add_argument("--save-images", action=argparse.BooleanOptionalAction, default=True)
    inf.add_argument(
        "--save-hits-only",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="仅保存检出击倒图标的帧（默认开启；--no-save-hits-only 保存全部检测帧）",
    )
    inf.set_defaults(func=cmd_infer)

    cl = sub.add_parser("clip", help="检测击杀并剪辑集锦")
    cl.add_argument("--video", type=Path, required=True)
    cl.add_argument("-o", "--output", type=Path, default=CLIPS_DIR / "highlight.mp4")
    cl.add_argument(
        "--segments-dir",
        type=Path,
        default=None,
        help="可选，额外保存各剪辑片段 mp4",
    )
    cl.add_argument(
        "--meta-json",
        type=Path,
        default=None,
        help="可选，导出击杀/剪辑区间 JSON",
    )
    _add_clip_common_args(cl)
    cl.set_defaults(func=cmd_clip)

    cb = sub.add_parser("clip-batch", help="批量检测击杀并剪辑集锦（输出文件名与输入一致）")
    cb.add_argument("--input-dir", type=Path, required=True, help="输入视频目录")
    cb.add_argument("--output-dir", type=Path, required=True, help="输出视频目录")
    cb.add_argument(
        "--segments-dir",
        type=Path,
        default=None,
        help="可选，为每个视频创建子目录保存各剪辑片段",
    )
    cb.add_argument(
        "--meta-dir",
        type=Path,
        default=None,
        help="可选，为每个视频导出 {stem}.json 区间元数据",
    )
    _add_clip_common_args(cb)
    cb.set_defaults(func=cmd_clip_batch)

    ac = sub.add_parser("auto-cut", help="自动剪辑（前段 1.45x + 尾部按钮截断）并合成 BGM")
    ac.add_argument("--video", type=Path, required=True, help="输入视频（clip 产物，需同名 JSON 清单）")
    ac.add_argument("-o", "--output", type=Path, default=None, help="输出路径（默认 <video>_bgm.mp4）")
    ac.add_argument("--bgm", type=Path, default=None, help="BGM 音频（默认 data/bgm.wav；传 none 可禁用）")
    ac.add_argument("--json", type=Path, default=None, help="同名清单 JSON 路径（默认与视频同名 .json）")
    ac.add_argument("--speed", type=float, default=1.45, help="前段播放倍速（默认 1.45）")
    ac.add_argument(
        "--cut-value",
        type=int,
        default=113,
        help="尾部\"返回大厅(N秒)\"截断阈值：首次显示 N 秒的帧及其后全部剪掉（默认 113）",
    )
    ac.add_argument("--fade-sec", type=float, default=15.0, help="音频结尾淡出时长（默认 15s）")
    ac.add_argument(
        "--tail-sec",
        type=float,
        default=20.0,
        help="无 JSON 清单时的前后分切兜底：倒数 N 秒处（默认 20s）",
    )
    ac.add_argument("--dry-run", action="store_true", help="只检测并打印计划/ffmpeg 命令，不渲染")
    ac.add_argument("--no-progress", action="store_true", help="关闭进度条")
    g = ac.add_mutually_exclusive_group()
    g.add_argument(
        "--gpu",
        action="store_true",
        help="强制使用 h264_nvenc + cuda 硬解（默认自动：检测到 nvenc 即启用）",
    )
    g.add_argument("--cpu", action="store_true", help="强制 libx264 软编码（不用 GPU）")
    ac.set_defaults(func=cmd_auto_cut)

    return p
