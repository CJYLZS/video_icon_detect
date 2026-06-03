"""击倒图标检测 — 统一 CLI 入口。"""

from __future__ import annotations

import argparse
from pathlib import Path

from clip import run_clip
from classifier import train_classifier
from extract import export_frames_from_icons, extract_frames
from frame_source import FrameSource, load_bgr, resolve_media_source
from icon_roi import (
    DEFAULT_MATCH_THRESH,
    DEFAULT_TEMPLATE_BIN,
    DEFAULT_TEMPLATE_META,
    DETECT_ROI_CX,
    DETECT_ROI_CY,
    DETECT_ROI_H,
    DETECT_ROI_W,
    build_roi_template,
    detect_roi_rect,
)
from infer import FrameDetection, InferConfig, iter_detections, save_detection_image
from paths import CLIPS_DIR, OUTPUT_DIR, ROI_CHECK_DIR
from preview import export_roi_preview
from sampling import resolve_frame_indices, resolve_time_window, sample_step


def cmd_export_icons(args: argparse.Namespace) -> None:
    export_frames_from_icons(
        args.video,
        args.icon_dir,
        args.output_dir,
        prefix=args.prefix,
        ext=args.ext,
        remove_stale=not args.keep_stale,
    )


def cmd_extract(args: argparse.Namespace) -> None:
    if not args.video.is_file():
        raise FileNotFoundError(f"视频不存在: {args.video}")

    indices, total, video_fps = resolve_frame_indices(args, args.video)
    if not indices:
        raise SystemExit("没有可提取的帧")

    _print_sampling_plan(
        args.video, indices, video_fps, total, sample_fps=args.fps, action="提取"
    )
    extract_frames(args.video, args.output_dir, indices, prefix=args.prefix, ext=args.ext)


def cmd_template(args: argparse.Namespace) -> None:
    if not args.image.is_file():
        raise FileNotFoundError(f"图片不存在: {args.image}")

    frame = load_bgr(args.image)
    meta = build_roi_template(
        frame,
        source=str(args.image.resolve()),
        frame_index=None,
    )
    tag = args.image.stem
    preview_dir = export_roi_preview(frame, meta, tag, args.preview_dir)
    print(f"模板已写入: {DEFAULT_TEMPLATE_BIN.parent.resolve()}")
    print(f"参考框(ROI 内): {meta['ref_box_roi']}")
    print(f"ROI 预览: {preview_dir.resolve()}")


def cmd_train(args: argparse.Namespace) -> None:
    if not DEFAULT_TEMPLATE_BIN.is_file() or not DEFAULT_TEMPLATE_META.is_file():
        raise SystemExit("请先运行: python main.py template --image <参考图>")

    if args.positive_dir:
        if not args.positive_dir.is_dir():
            raise SystemExit("--positive-dir 必须是目录")
        if not args.negative_dir:
            raise SystemExit("使用 --positive-dir 时请至少指定一个 --negative-dir")
        train_classifier(
            positive_dirs=[args.positive_dir],
            negative_dirs=args.negative_dir,
            epochs=args.epochs,
            batch_size=args.batch_size,
            lr=args.lr,
        )
        return

    if args.image_dir is None or not args.image_dir.is_dir():
        raise SystemExit("请指定 --image-dir，或使用 --positive-dir + --negative-dir")
    if args.positive is None and args.positive_from is None:
        raise SystemExit("使用 --image-dir 时请指定 --positive 或 --positive-from")

    positive_frames = set(args.positive) if args.positive else None
    train_classifier(
        args.image_dir,
        positive_frames=positive_frames,
        positive_from=args.positive_from,
        negative_dirs=args.negative_dir,
        epochs=args.epochs,
        batch_size=args.batch_size,
        lr=args.lr,
    )


def _format_hit_line(item: FrameDetection) -> str:
    det = item.detection
    head = f"帧 {item.frame_index:5d} ({item.time_sec:6.2f}s) [{det['method']}]"
    if det.get("bin_score") is None:
        return f"{head} score={det['score']:.3f}"
    line = (
        f"{head} score={det['score']:.3f} "
        f"bin={det['bin_score']:.3f} zncc={det['zncc_score']:.3f}"
    )
    if item.cls_prob is not None:
        line += f" cls={item.cls_prob:.3f}"
    return line


def _print_roi(sample) -> None:
    rx0, ry0, rx1, ry1 = detect_roi_rect(sample.shape)
    print(
        f"ROI: ({DETECT_ROI_CX:.0%},{DETECT_ROI_CY:.0%}) "
        f"{DETECT_ROI_W:.0%}×{DETECT_ROI_H:.0%} -> ({rx0},{ry0})-({rx1},{ry1})"
    )


def _ensure_template(*, cls_only: bool) -> None:
    if cls_only:
        if not DEFAULT_TEMPLATE_META.is_file():
            raise SystemExit("模板元数据不存在，请先: python main.py template --image <参考图>")
    elif not DEFAULT_TEMPLATE_BIN.is_file():
        raise SystemExit("模板不存在，请先: python main.py template --image <参考图>")


def _print_detect_mode(
    *,
    cls_only: bool,
    use_classifier: bool,
    cls_thresh: float,
    label: str = "模式",
) -> None:
    if cls_only:
        print(f"{label}: 仅分类器 (cls>={cls_thresh})")
    elif use_classifier:
        print(f"{label}: template + 分类器 (cls>={cls_thresh})")
    else:
        print(f"{label}: 固定 ROI 二值 template + ZNCC")


def _print_sampling_plan(
    video: Path,
    indices: list[int],
    fps: float,
    total: int,
    *,
    sample_fps: float | None,
    action: str,
) -> None:
    print(f"视频: {video.name}, 总帧数: {total}, 原始 fps: {fps:.2f}")
    if sample_fps is not None:
        step = sample_step(fps, sample_fps)
        print(f"采样频率: {sample_fps} 张/秒 -> 每隔 {step} 帧取 1 张")
    print(f"将{action} {len(indices)} 帧: {indices[:8]}{'...' if len(indices) > 8 else ''}")


def _infer_config_from_args(args: argparse.Namespace, indices: list[int], fps: float) -> InferConfig:
    return InferConfig(
        video=args.video,
        indices=indices,
        fps=fps,
        image_dir=args.image_dir,
        match_thresh=args.match_thresh,
        use_classifier=args.use_classifier or args.cls_only,
        cls_only=args.cls_only,
        cls_thresh=args.cls_thresh,
    )


def cmd_infer(args: argparse.Namespace) -> None:
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if not args.video.is_file():
        raise FileNotFoundError(f"视频不存在: {args.video}")

    _ensure_template(cls_only=args.cls_only)
    _print_detect_mode(
        cls_only=args.cls_only,
        use_classifier=args.use_classifier,
        cls_thresh=args.cls_thresh,
    )

    indices, total, fps = resolve_frame_indices(args, args.video)
    if not indices:
        raise SystemExit("无检测帧，请指定 --fps 或时间范围（--start-sec/--end-sec）")

    _print_sampling_plan(
        args.video, indices, fps, total, sample_fps=args.fps, action="检测"
    )
    thresh = args.cls_thresh if args.cls_only else args.match_thresh
    kind = "cls" if args.cls_only else "模板"
    print(f"阈值 ({kind}): {thresh:.2f}")

    config = _infer_config_from_args(args, indices, fps)

    verbose = len(indices) <= 35
    skipped = 0
    hits: list[FrameDetection] = []

    det_video, det_image_dir = resolve_media_source(args.video, args.image_dir)
    with FrameSource(det_video, det_image_dir) as frame_src:
        roi_printed = False
        for item in iter_detections(config, frame_src=frame_src):
            if item is None:
                skipped += 1
                continue
            if not roi_printed:
                _print_roi(item.frame)
                roi_printed = True
            if item.detection["present"]:
                hits.append(item)
                line = _format_hit_line(item)
                print(f"\n--- {line} ---" if verbose else line)
            elif verbose:
                print(f"\n--- 帧 {item.frame_index} ({item.time_sec:.2f}s) 未检出 ---")

            if args.save_images and (item.detection["present"] or not args.save_hits_only):
                save_detection_image(args.output_dir, item)

    if skipped:
        print(f"警告: {skipped}/{len(indices)} 帧无法读取（请检查 frames 文件名是否与帧号一致）")
    print(f"\n完成: {len(hits)}/{len(indices)} 帧命中")
    if hits:
        print(f"命中帧: {[h.frame_index for h in hits]}")


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
    parser.add_argument("--use-classifier", action="store_true", help="模板与分类器同时通过才命中")
    parser.add_argument(
        "--cls-only",
        action="store_true",
        help="仅用分类器判定（cls>=阈值即命中，忽略模板阈值）",
    )
    parser.add_argument("--cls-thresh", type=float, default=0.5)


def _add_detect_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--image-dir", type=Path, help="可选，从已抽帧目录读图")
    _add_classifier_args(parser)


def cmd_clip(args: argparse.Namespace) -> None:
    if not args.video.is_file():
        raise FileNotFoundError(f"视频不存在: {args.video}")
    _ensure_template(cls_only=args.cls_only)
    _print_detect_mode(
        cls_only=args.cls_only,
        use_classifier=args.use_classifier,
        cls_thresh=args.cls_thresh,
        label="检测模式",
    )

    start_sec, end_sec = resolve_time_window(args, args.video)
    args.start_sec = start_sec
    args.end_sec = end_sec

    indices, total, fps = resolve_frame_indices(args, args.video)
    if not indices:
        raise SystemExit("无检测帧")

    step = sample_step(fps, args.fps)
    print(f"视频: {args.video.name}, 总帧数: {total}, 原始 fps: {fps:.2f}")
    print(f"剪辑范围: {start_sec:.2f}s ~ {end_sec:.2f}s, 采样 {args.fps}/s (步长 {step})")
    config = _infer_config_from_args(args, indices, fps)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    plan = run_clip(
        config,
        window_end=end_sec,
        output_path=args.output,
        segments_dir=args.segments_dir,
        meta_path=args.meta_json,
        pad_before=args.pad_before,
        pad_after=args.pad_after,
        max_hit_gap=args.max_hit_gap,
        merge_gap=args.merge_gap,
        show_progress=not args.no_progress,
    )

    print(f"\n{plan.summary_line()}")
    print(f"\n集锦片段 {plan.clip_count} 段:")
    for i, c in enumerate(plan.clips, 1):
        print(f"  #{i} {c.start_sec:.2f}s ~ {c.end_sec:.2f}s ({c.duration_sec:.2f}s)")
    print(f"\n集锦已导出: {args.output.resolve()}")
    if args.meta_json:
        print(f"区间 JSON: {args.meta_json.resolve()}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="和平精英击倒图标检测（固定 ROI + 二值 template）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  python main.py extract --video test.mp4 --output-dir frames --start-sec 38 --end-sec 45 --fps 5
  python main.py template --image frames/positive/frame_02671_t00044715ms.jpg
  python main.py train --positive-dir frames/positive --negative-dir frames/negative --epochs 80
  python main.py infer --video test.mp4 --image-dir frames --start-sec 88 --end-sec 90 --fps 5 --output-dir output/icon_detect
  python main.py clip --video test.mp4 --fps 10 --use-classifier -o output/clips/highlight.mp4
  python main.py clip --video test.mp4 --start-sec 70 --end-sec 80 --fps 10 --use-classifier
""",
    )
    sub = p.add_subparsers(dest="command", required=True)

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
    _add_time_range_group(cl)
    _add_fps_arg(cl, required=True)
    _add_detect_args(cl)
    cl.add_argument("--pad-before", type=float, default=1.0, help="击杀开始前保留秒数")
    cl.add_argument("--pad-after", type=float, default=1.0, help="非最后一段击杀结束后保留秒数")
    cl.add_argument(
        "--max-hit-gap",
        type=float,
        default=2.0,
        help="相邻命中时刻合并为同一击杀的最大间隔（秒）",
    )
    cl.add_argument(
        "--merge-gap",
        type=float,
        default=2.0,
        help="相邻击杀区间合并的最大间隔（秒，按击杀结束~下一击杀开始）",
    )
    cl.add_argument("--no-progress", action="store_true", help="关闭进度条")
    cl.set_defaults(func=cmd_clip)

    return p


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
