"""击倒图标检测 — 统一 CLI 入口。"""

from __future__ import annotations

import argparse
from pathlib import Path

from classifier import train_classifier
from extract import extract_frames
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
from paths import OUTPUT_DIR, ROI_CHECK_DIR
from preview import export_roi_preview
from sampling import resolve_frame_indices, sample_step


def cmd_extract(args: argparse.Namespace) -> None:
    if not args.video.is_file():
        raise FileNotFoundError(f"视频不存在: {args.video}")

    indices, total, video_fps = resolve_frame_indices(args, args.video)
    if not indices:
        raise SystemExit("没有可提取的帧")

    print(f"视频: {args.video.name}, 总帧数: {total}, 原始 fps: {video_fps:.2f}")
    if args.fps is not None:
        step = sample_step(video_fps, args.fps)
        print(f"抽帧频率: {args.fps} 张/秒 -> 每隔 {step} 帧取 1 张")
    print(f"将提取 {len(indices)} 帧: {indices[:8]}{'...' if len(indices) > 8 else ''}")
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
    if not args.image_dir.is_dir():
        raise SystemExit("--image-dir 必须是包含 frame_XXXXX.jpg 的目录")
    if not DEFAULT_TEMPLATE_BIN.is_file() or not DEFAULT_TEMPLATE_META.is_file():
        raise SystemExit("请先运行: python main.py template --image <参考图>")

    positive_frames = set(args.positive) if args.positive else None
    train_classifier(
        args.image_dir,
        positive_frames=positive_frames,
        positive_from=args.positive_from,
        epochs=args.epochs,
        batch_size=args.batch_size,
        lr=args.lr,
    )


def _format_hit_line(item: FrameDetection) -> str:
    det = item.detection
    line = (
        f"帧 {item.frame_index:5d} ({item.time_sec:6.2f}s) [{det['method']}] "
        f"score={det['score']:.3f} bin={det['bin_score']:.3f} zncc={det['zncc_score']:.3f}"
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


def cmd_infer(args: argparse.Namespace) -> None:
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if not args.video.is_file():
        raise FileNotFoundError(f"视频不存在: {args.video}")

    if not DEFAULT_TEMPLATE_BIN.is_file():
        raise SystemExit("模板不存在，请先: python main.py template --image <参考图>")

    if args.use_classifier:
        print(f"模式: template + 分类器 (cls>={args.cls_thresh})")
    else:
        print("模式: 固定 ROI 二值 template + ZNCC")

    indices, total, fps = resolve_frame_indices(args, args.video)
    if not indices:
        raise SystemExit("无检测帧，请指定 --fps 或时间范围（--start-sec/--end-sec）")

    print(f"视频: {args.video.name}, 总帧数: {total}, 原始 fps: {fps:.2f}")
    if args.fps is not None:
        step = sample_step(fps, args.fps)
        print(f"采样频率: {args.fps} 张/秒 -> 每隔 {step} 帧取 1 张")
    print(f"将检测 {len(indices)} 帧: {indices[:8]}{'...' if len(indices) > 8 else ''}")
    print(f"检测 {len(indices)} 帧, 阈值={args.match_thresh:.2f}")

    config = InferConfig(
        video=args.video,
        indices=indices,
        fps=fps,
        image_dir=args.image_dir,
        match_thresh=args.match_thresh,
        use_classifier=args.use_classifier,
        cls_thresh=args.cls_thresh,
    )

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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="和平精英击倒图标检测（固定 ROI + 二值 template）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  python main.py extract --video test.mp4 --output-dir frames --start-sec 38 --end-sec 45 --fps 5
  python main.py template --image frames/frame_00153.jpg
  python main.py train --image-dir frames --positive-from 153
  python main.py infer --video test.mp4 --image-dir frames --start-sec 88 --end-sec 90 --fps 5 --output-dir output/icon_detect
""",
    )
    sub = p.add_subparsers(dest="command", required=True)

    ex = sub.add_parser("extract", help="从视频提取帧")
    ex.add_argument("--video", type=Path, required=True)
    ex.add_argument("--output-dir", type=Path, required=True)
    g = ex.add_mutually_exclusive_group()
    g.add_argument("--frame", type=int, action="append")
    g.add_argument("--seconds", type=float, action="append")
    r = ex.add_argument_group("范围")
    r.add_argument("--start-sec", type=float)
    r.add_argument("--end-sec", type=float)
    r.add_argument("--step", type=int, default=1, help="每隔多少帧取 1 张（与 --fps 二选一，--fps 优先）")
    ex.add_argument(
        "--fps",
        type=float,
        default=None,
        metavar="HZ",
        help="抽帧频率（每秒导出张数），按视频原始 fps 自动换算步长；可单独使用表示抽全片",
    )
    ex.add_argument("--prefix", default="frame")
    ex.add_argument("--ext", default="jpg", choices=("jpg", "png"))
    ex.set_defaults(func=cmd_extract)

    tpl = sub.add_parser("template", help="从单张图片构造模板")
    tpl.add_argument("--image", type=Path, required=True, help="含击倒图标的整帧截图")
    tpl.add_argument("--preview-dir", type=Path, default=ROI_CHECK_DIR)
    tpl.set_defaults(func=cmd_template)

    tr = sub.add_parser("train", help="训练可选 ROI 分类器")
    tr.add_argument("--image-dir", type=Path, required=True)
    label = tr.add_mutually_exclusive_group(required=True)
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
    r = inf.add_argument_group("范围")
    r.add_argument("--start-sec", type=float)
    r.add_argument("--end-sec", type=float)
    r.add_argument("--step", type=int, default=1, help="每隔多少帧取 1 张（与 --fps 二选一，--fps 优先）")
    inf.add_argument(
        "--fps",
        type=float,
        default=None,
        metavar="HZ",
        help="采样频率（每秒检测张数），按视频原始 fps 自动换算步长；可单独使用表示检测全片",
    )
    inf.add_argument("--match-thresh", type=float, default=DEFAULT_MATCH_THRESH)
    inf.add_argument("--use-classifier", action="store_true")
    inf.add_argument("--cls-thresh", type=float, default=0.5)
    inf.add_argument("--save-images", action=argparse.BooleanOptionalAction, default=True)
    inf.add_argument(
        "--save-hits-only",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="仅保存检出击倒图标的帧（默认开启；--no-save-hits-only 保存全部检测帧）",
    )
    inf.set_defaults(func=cmd_infer)

    return p


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
